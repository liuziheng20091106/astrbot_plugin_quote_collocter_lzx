import json
import os
import random
import re
import time
from pathlib import Path

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

# 群友语录图片存放根目录（遵循 AstrBot 存储大文件规范：data/plugin_data/{插件名}）
QUOTES_ROOT = Path("data") / "plugin_data" / "quote_collocter"

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}

# 戳一戳触发的趣味文本
POKE_TEXTS = [
    "再戳的话……说不定下一张就是你的！",
    "我会一直一直看着你👀",
    "给我出列！",
]


class QuoteShuffler:
    """群聊语录随机抽取器。

    采用「打乱顺序 + 指针递增」的算法，保证一轮之内不会重复抽到同一张图片；
    同时通过 recent_window 避免新一局开头立刻复现上一局末尾的图片。
    抽取队列持久化到群目录下的 order.json，重启后可继续。
    """

    def __init__(self, group_dir: Path, window: int):
        self.group_dir = group_dir
        self.order_file = group_dir / "order.json"
        self.window = max(1, int(window))

    def _list_images(self) -> list[str]:
        if not self.group_dir.exists():
            return []
        return sorted(
            f.name
            for f in self.group_dir.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
        )

    def _load(self) -> dict:
        try:
            if self.order_file.exists():
                with open(self.order_file, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception as e:
            logger.warning(f"读取抽取队列失败，将重建: {e}")
        return {}

    def _save(self, data: dict) -> None:
        try:
            self.group_dir.mkdir(parents=True, exist_ok=True)
            with open(self.order_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存抽取队列失败: {e}")

    def _build_order(self, images: list[str], avoid: set[str]) -> dict:
        """根据当前图片列表生成一个新的打乱顺序。"""
        queue = list(images)
        random.shuffle(queue)
        # 尽量让新的开头不要是上一局末尾几张，避免连续重复
        head = queue[: self.window]
        tail = queue[self.window :]
        head = [x for x in head if x not in avoid] + [x for x in head if x in avoid]
        queue = head + tail
        return {"order": queue, "index": 0}

    def next(self) -> str | None:
        """返回下一张语录图片的绝对路径，无可用图片时返回 None。"""
        images = self._list_images()
        if not images:
            return None

        data = self._load()
        order = data.get("order") or []
        index = data.get("index", 0)

        # 队列与当前实际图片不一致（新增/删除/首次）时重建
        image_set = set(images)
        if not order or set(order) != image_set:
            avoid = (
                set(order[max(0, len(order) - index - self.window) :])
                if order
                else set()
            )
            new = self._build_order(images, avoid)
            order, index = new["order"], new["index"]

        if index >= len(order):
            avoid = set(order[len(order) - self.window :]) if order else set()
            new = self._build_order(images, avoid)
            order, index = new["order"], new["index"]

        target = order[index]
        index += 1
        self._save({"order": order, "index": index})
        return str(self.group_dir / target)


class QuotePlugin(Star):
    """群友语录收集插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 群 -> QuoteShuffler
        self._shufflers: dict[str, QuoteShuffler] = {}

    # ------------------------------------------------------------------ #
    # 路径与持久化
    # ------------------------------------------------------------------ #
    def _group_dir(self, group_id: str) -> Path:
        return QUOTES_ROOT / str(group_id)

    def _ensure_group_dir(self, group_id: str) -> Path:
        d = self._group_dir(group_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _settings_path(self, group_id: str) -> Path:
        return self._group_dir(group_id) / "settings.json"

    def _load_settings(self, group_id: str) -> dict:
        """读取群级设置，合并全局默认值。"""
        data = {
            "mode": int(self.config.get("default_submit_mode", 0)),
            "cooldown": int(self.config.get("default_cooldown", 10)),
            "last_poke": 0,
        }
        path = self._settings_path(group_id)
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data.update(json.load(f) or {})
        except Exception as e:
            logger.warning(f"读取群设置失败: {e}")
        return data

    def _save_settings(self, group_id: str, settings: dict) -> None:
        try:
            self._ensure_group_dir(group_id)
            with open(self._settings_path(group_id), "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存群设置失败: {e}")

    def _shuffler(self, group_id: str) -> QuoteShuffler:
        if group_id not in self._shufflers:
            self._shufflers[group_id] = QuoteShuffler(
                self._group_dir(group_id),
                int(self.config.get("recent_window", 8)),
            )
        return self._shufflers[group_id]

    # ------------------------------------------------------------------ #
    # 图片下载
    # ------------------------------------------------------------------ #
    async def _save_bytes(self, group_id: str, data: bytes, ext: str = ".jpg") -> str:
        """将图片字节写入群目录，返回保存路径。"""
        d = self._ensure_group_dir(group_id)
        # 用纳秒级时间戳 + 随机串，避免并发冲突
        filename = f"image_{int(time.time() * 1000)}_{random.randint(1000, 9999)}{ext}"
        path = d / filename
        with open(path, "wb") as f:
            f.write(data)
        logger.info(f"语录图片已保存: {path}")
        return str(path)

    async def _download_image(
        self, event: AstrMessageEvent, file_id: str, group_id: str
    ) -> str | None:
        """三级容错下载图片：本地缓存 -> 协议端 API -> URL 下载。"""
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot

            # 1. 优先从当前消息的本地缓存读取
            image_obj = next(
                (c for c in event.message_obj.message if isinstance(c, Comp.Image)),
                None,
            )
            if image_obj:
                try:
                    local_path = await image_obj.convert_to_file_path()
                    if local_path and os.path.exists(local_path):
                        logger.info(f"从本地缓存读取图片: {local_path}")
                        with open(local_path, "rb") as f:
                            return await self._save_bytes(group_id, f.read())
                except Exception as e:
                    logger.warning(f"读取本地缓存失败: {e}")

            # 2. 通过协议端 get_image API 获取
            try:
                result = await client.api.call_action("get_image", file=file_id)
                api_path = result.get("file") if result else None
                if api_path and os.path.exists(api_path):
                    logger.info(f"从协议端 API 读取图片: {api_path}")
                    with open(api_path, "rb") as f:
                        return await self._save_bytes(group_id, f.read())
            except Exception as e:
                logger.warning(f"通过 get_image API 下载失败: {e}")
                result = None

            # 3. 通过 URL 下载
            url = result.get("url") if result else None
            if url:
                logger.info(f"从 URL 下载图片: {url}")
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                return await self._save_bytes(
                                    group_id, await resp.read()
                                )
                            logger.error(f"从 URL 下载失败: HTTP {resp.status}")
                except Exception as e:
                    logger.error(f"从 URL 下载出错: {e}")

            logger.error("所有下载方式均失败")
            return None
        except Exception as e:
            logger.error(f"下载图片异常: {e}")
            return None

    async def _resolve_reply_image_file(self, event: AstrMessageEvent) -> str | None:
        """从被回复的消息中解析出图片 file_id（兼容数组/ CQ 码两种格式）。"""
        reply = next(
            (c for c in event.message_obj.message if isinstance(c, Comp.Reply)), None
        )
        if not reply:
            return None
        try:
            reply_id = int(reply.id) if str(reply.id).isdigit() else reply.id
            reply_msg = await event.bot.api.call_action("get_msg", message_id=reply_id)
            if not reply_msg or "message" not in reply_msg:
                return None
            chain = reply_msg["message"]
            if isinstance(chain, list):
                for part in chain:
                    if isinstance(part, dict) and part.get("type") == "image":
                        return part.get("data", {}).get("file")
            elif isinstance(chain, str):
                m = re.search(r"\[CQ:image,[^\]]*file=([^,\]]+)", chain)
                if m:
                    return m.group(1)
        except Exception as e:
            logger.error(f"获取引用消息图片失败: {e}")
        return None

    # ------------------------------------------------------------------ #
    # 指令：随机查看一条语录
    # ------------------------------------------------------------------ #
    @filter.command("语录", alias={"随机语录"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def random_quote(self, event: AstrMessageEvent):
        """随机查看一条本群语录"""
        group_id = str(event.message_obj.group_id)
        path = self._shuffler(group_id).next()
        if path:
            yield event.image_result(path)
        else:
            yield event.plain_result(
                "⭐本群还没有群友语录哦~\n请发送“/语录投稿+图片”来添加！"
            )
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：语录投稿
    # ------------------------------------------------------------------ #
    @filter.command("语录投稿", alias={"投稿语录"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def submit_quote(self, event: AstrMessageEvent):
        """投稿一张群友语录图片（可直接带图，或回复一条含图消息）"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        msg_id = str(event.message_obj.message_id)

        settings = self._load_settings(group_id)
        mode = int(settings.get("mode", 0))
        if mode == 0:
            yield event.plain_result(
                "⭐投稿系统未开启，请联系 bot 管理员使用 /投稿权限 开启"
            )
            event.stop_event()
            return
        if mode == 1 and not self._is_admin(event):
            yield event.plain_result("⭐权限不足，当前仅 bot 管理员可投稿")
            event.stop_event()
            return

        # 解析图片 file_id：当前消息图片 或 被回复消息中的图片
        image_comp = next(
            (c for c in event.message_obj.message if isinstance(c, Comp.Image)), None
        )
        file_id = (
            image_comp.file
            if image_comp
            else await self._resolve_reply_image_file(event)
        )

        if not file_id:
            yield event.chain_result(
                [
                    Comp.At(qq=user_id),
                    Comp.Plain(
                        text="\n你是不是忘发图啦？\n请“/语录投稿+图片”或“回复含图消息并发送 /语录投稿”"
                    ),
                ]
            )
            event.stop_event()
            return

        path = await self._download_image(event, file_id, group_id)
        if path and os.path.exists(path):
            yield event.chain_result(
                [Comp.Reply(id=msg_id), Comp.Plain(text="⭐语录投稿成功！")]
            )
        else:
            yield event.chain_result(
                [Comp.Reply(id=msg_id), Comp.Plain(text="⭐语录投稿失败，图片下载失败")]
            )
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：查看群状态
    # ------------------------------------------------------------------ #
    @filter.command("语录状态", alias={"投稿状态"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def quote_status(self, event: AstrMessageEvent):
        """查看本群语录数量、投稿权限与冷却设置"""
        group_id = str(event.message_obj.group_id)
        settings = self._load_settings(group_id)
        d = self._group_dir(group_id)
        count = (
            sum(
                1
                for f in d.iterdir()
                if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
            )
            if d.exists()
            else 0
        )
        mode_text = {0: "关闭", 1: "仅管理员", 2: "全体成员"}.get(
            int(settings.get("mode", 0)), "未知"
        )
        yield event.plain_result(
            f"⭐本群语录状态\n"
            f"  数量：{count} 张\n"
            f"  投稿权限：{mode_text}\n"
            f"  戳戳冷却：{int(settings.get('cooldown', 10))} 秒"
        )
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：设置投稿权限（仅管理员）
    # ------------------------------------------------------------------ #
    @filter.command("投稿权限", alias={"设置投稿权限"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def set_submit_mode(self, event: AstrMessageEvent, mode: int):
        """设置投稿权限模式：0=关闭 1=仅管理员 2=全体"""
        group_id = str(event.message_obj.group_id)
        if mode not in (0, 1, 2):
            yield event.plain_result(
                "⭐模式只能是 0、1、2\n  0：关闭\n  1：仅管理员\n  2：全体成员"
            )
            event.stop_event()
            return
        settings = self._load_settings(group_id)
        settings["mode"] = mode
        self._save_settings(group_id, settings)
        text = {0: "关闭", 1: "仅管理员", 2: "全体成员"}[mode]
        yield event.plain_result(f"⭐投稿权限已设置为：{text}")
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：设置戳戳冷却（仅管理员）
    # ------------------------------------------------------------------ #
    @filter.command("戳戳冷却", alias={"语录冷却"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def set_poke_cooldown(self, event: AstrMessageEvent, cooldown: int):
        """设置戳一戳触发语录的冷却时间（秒）"""
        group_id = str(event.message_obj.group_id)
        if cooldown < 0:
            yield event.plain_result("⭐冷却时间不能为负数")
            event.stop_event()
            return
        settings = self._load_settings(group_id)
        settings["cooldown"] = cooldown
        self._save_settings(group_id, settings)
        yield event.plain_result(f"⭐戳戳冷却已设置为：{cooldown} 秒")
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 监听：戳一戳触发随机语录
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_poke(self, event: AstrMessageEvent):
        """被戳一戳时随机发送一张语录（带冷却与去重）"""
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice":
            return
        if raw.get("notice_type") != "notify" or raw.get("sub_type") != "poke":
            return

        bot_id = raw.get("self_id")
        sender_id = raw.get("user_id")
        target_id = raw.get("target_id")
        if not (bot_id and sender_id and target_id):
            return
        # 只响应戳到 bot 自己
        if str(target_id) != str(bot_id):
            return

        group_id = str(event.message_obj.group_id)
        settings = self._load_settings(group_id)
        cooldown = int(
            settings.get("cooldown", self.config.get("default_cooldown", 10))
        )
        last_poke = float(settings.get("last_poke", 0))

        if time.time() - last_poke < cooldown:
            remaining = cooldown - (time.time() - last_poke)
            logger.info(f"戳戳语录冷却中，剩余 {remaining:.0f} 秒")
            return

        settings["last_poke"] = time.time()
        self._save_settings(group_id, settings)

        prob = float(self.config.get("poke_quote_probability", 0.85))
        if random.random() < prob:
            path = self._shuffler(group_id).next()
            if path:
                yield event.image_result(path)
                event.stop_event()
            # 无语录时静默
            return
        else:
            yield event.chain_result(
                [Comp.At(qq=sender_id), Comp.Plain(text=random.choice(POKE_TEXTS))]
            )
            event.stop_event()

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判断调用者是否为 bot 管理员。"""
        # AstrBot 管理员标识存在会话配置中
        try:
            admins = self.context.get_config().get("admins_id", []) or []
            return str(event.get_sender_id()) in {str(a) for a in admins}
        except Exception:
            return False

    async def terminate(self):
        """插件卸载时调用。"""
        self._shufflers.clear()
