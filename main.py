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
from astrbot.api.web import json_response, error_response, request
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

PLUGIN_NAME = "quote_collocter"

# 群友语录图片存放根目录（遵循 AstrBot 存储大文件规范：data/plugin_data/{插件名}）
QUOTES_ROOT = Path("data") / "plugin_data" / "quote_collocter"

# 全局设置文件（供 WebUI Page 与插件共享读写）
GLOBAL_SETTINGS_PATH = QUOTES_ROOT / "settings.json"

# 默认稀有度权重（面板与 _conf_schema.json 默认值保持一致）
DEFAULT_RARITY_WEIGHTS = {
    "weight_5": 3,
    "weight_4": 5,
    "weight_3": 8,
    "weight_2": 4,
    "weight_1": 2,
}

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}

# 戳一戳触发的趣味文本
POKE_TEXTS = [
    "再戳的话……说不定下一张就是你的！",
    "我会一直一直看着你👀",
    "给我出列！",
]

# 语录稀有度文本（对应文件名首位数字 5..1，不在映射内的数字按 1=新卡 处理）
RARITY_TEXT = {
    5: "恭喜抽到SSSR卡",
    4: "您抽到了SSR卡",
    3: "您抽到了SR卡",
    2: "您抽到了R卡",
    1: "您抽到了新卡",
}


def parse_quote_rarity(filename: str) -> tuple[int, str]:
    """解析 '<0-5>.<string>.<ext>' 格式的文件名。

    返回 (稀有度数字, 展示名)。
    展示名 = 文件名中的 [string] + 文件扩展名。
    不符合格式的文件名按稀有度 1（新卡）处理，展示名为去掉编号后的原文。
    """
    p = Path(filename)
    ext = p.suffix
    stem = p.stem
    parts = stem.split(".", 1)
    if len(parts) == 2 and parts[0].isdigit():
        rarity = int(parts[0])
        if rarity not in RARITY_TEXT:
            rarity = 1
        string = parts[1]
    else:
        rarity = 1
        string = stem
    return rarity, f"{string}{ext}"


def quote_chain(path: str) -> list:
    """根据语录图片路径构建 [图片 + 稀有度文本] 消息链。"""
    rarity, display = parse_quote_rarity(Path(path).name)
    text = f"{RARITY_TEXT[rarity]}\n{display}"
    return [Comp.Image.fromFileSystem(path), Comp.Plain(text=text)]


class QuoteShuffler:
    """群聊语录随机抽取器。

    采用「按稀有度加权不放回抽样 + 指针递增」的算法：
    一轮之内每张图片恰好出现一次（不重复），但高权重（按稀有度配置）
    的图片倾向排到队列更靠前，更早被抽到；一轮走完后重新加权洗牌。
    通过 recent_window 避免新一局开头立刻复现上一局末尾的图片。
    抽取队列持久化到群目录下的 order.json，重启后可继续。
    """

    def __init__(self, group_dir: Path, window: int, weights: dict | None = None):
        self.group_dir = group_dir
        self.order_file = group_dir / "order.json"
        self.window = max(1, int(window))
        # 稀有度 -> 权重，缺省按 parse_quote_rarity 的默认（1）再兜底 1.0
        self.weights = weights or {}

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

    def _weight_of(self, filename: str) -> float:
        """按文件名的稀有度查权重，未配置的稀有度回退到 1.0。"""
        rarity, _ = parse_quote_rarity(filename)
        return float(self.weights.get(rarity, 1.0) or 1.0)

    def _weighted_shuffle(self, images: list[str]) -> list[str]:
        """按稀有度权重做不放回抽样，返回一个全排列。

        高权重的图片更早被选中（排到队列更前面），一轮内仍每张各出现一次。
        所有权重之和 <= 0 时回退为均匀洗牌。
        """
        pool = list(images)
        weights = [self._weight_of(name) for name in pool]
        if sum(weights) <= 0:
            random.shuffle(pool)
            return pool
        out: list[str] = []
        while pool:
            i = random.choices(range(len(pool)), weights=weights, k=1)[0]
            out.append(pool.pop(i))
            weights.pop(i)
        return out

    def _build_order(self, images: list[str], avoid: set[str]) -> dict:
        """根据当前图片列表生成一个新的加权抽取顺序。"""
        queue = self._weighted_shuffle(images)
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
        # 注册 WebUI Page 后端 API
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings",
            self.web_get_settings,
            ["GET"],
            "Get quote plugin settings",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings/save",
            self.web_save_settings,
            ["POST"],
            "Save quote plugin settings",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/submit-log",
            self.web_submit_log,
            ["GET"],
            "Get submission log",
        )

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

    # ------------------------------------------------------------------ #
    # 全局设置（WebUI Page 与插件共享，settings.json）
    # ------------------------------------------------------------------ #
    def _default_global_settings(self) -> dict:
        """全局设置默认值，与 _conf_schema.json 镜像保持一致。"""
        return {
            "default_submit_mode": int(self.config.get("default_submit_mode", 0)),
            "default_cooldown": int(self.config.get("default_cooldown", 10)),
            "poke_quote_probability": float(
                self.config.get("poke_quote_probability", 0.85)
            ),
            "recent_window": int(self.config.get("recent_window", 8)),
            "rarity_weights": {
                **DEFAULT_RARITY_WEIGHTS,
                **(self.config.get("rarity_weights") or {}),
            },
        }

    def _load_global_settings(self) -> dict:
        """读取全局设置，缺失字段回退到默认值。"""
        data = self._default_global_settings()
        try:
            if GLOBAL_SETTINGS_PATH.exists():
                with open(GLOBAL_SETTINGS_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f) or {}
                # 逐字段合并，保留新加默认、用已保存值覆盖
                for k, v in data.items():
                    if k in saved:
                        if isinstance(v, dict):
                            merged = dict(v)
                            merged.update(saved.get(k) or {})
                            data[k] = merged
                        else:
                            data[k] = saved[k]
        except Exception as e:
            logger.warning(f"读取全局设置失败: {e}")
        return data

    def _save_global_settings(self, settings: dict) -> None:
        try:
            QUOTES_ROOT.mkdir(parents=True, exist_ok=True)
            with open(GLOBAL_SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存全局设置失败: {e}")

    def _load_settings(self, group_id: str) -> dict:
        """读取群级设置，合并全局设置作为默认。"""
        gs = self._load_global_settings()
        data = {
            "mode": int(gs.get("default_submit_mode", 0)),
            "cooldown": int(gs.get("default_cooldown", 10)),
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

    def _load_rarity_weights(self) -> dict:
        """优先从全局设置读取稀有度权重，整理成 {稀有度数字: 权重}。"""
        gs = self._load_global_settings()
        raw = gs.get("rarity_weights") or DEFAULT_RARITY_WEIGHTS
        return {
            5: float(raw.get("weight_5", DEFAULT_RARITY_WEIGHTS["weight_5"]) or 1.0),
            4: float(raw.get("weight_4", DEFAULT_RARITY_WEIGHTS["weight_4"]) or 1.0),
            3: float(raw.get("weight_3", DEFAULT_RARITY_WEIGHTS["weight_3"]) or 1.0),
            2: float(raw.get("weight_2", DEFAULT_RARITY_WEIGHTS["weight_2"]) or 1.0),
            1: float(raw.get("weight_1", DEFAULT_RARITY_WEIGHTS["weight_1"]) or 1.0),
        }

    def _shuffler(self, group_id: str) -> QuoteShuffler:
        if group_id not in self._shufflers:
            gs = self._load_global_settings()
            self._shufflers[group_id] = QuoteShuffler(
                self._group_dir(group_id),
                int(gs.get("recent_window", 8)),
                self._load_rarity_weights(),
            )
        return self._shufflers[group_id]

    # ------------------------------------------------------------------ #
    # 图片下载
    # ------------------------------------------------------------------ #
    async def _save_bytes(self, group_id: str, data: bytes, filename: str) -> str:
        """将图片字节写入群目录，按指定文件名保存，返回保存路径。"""
        d = self._ensure_group_dir(group_id)
        path = d / filename
        with open(path, "wb") as f:
            f.write(data)
        logger.info(f"语录图片已保存: {path}")
        return str(path)

    async def _download_image(
        self, event: AstrMessageEvent, file_id: str, group_id: str, filename: str
    ) -> str | None:
        """三级容错下载图片：本地缓存 -> 协议端 API -> URL 下载，保存为指定文件名。"""
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
                            return await self._save_bytes(group_id, f.read(), filename)
                except Exception as e:
                    logger.warning(f"读取本地缓存失败: {e}")

            # 2. 通过协议端 get_image API 获取
            try:
                result = await client.api.call_action("get_image", file=file_id)
                api_path = result.get("file") if result else None
                if api_path and os.path.exists(api_path):
                    logger.info(f"从协议端 API 读取图片: {api_path}")
                    with open(api_path, "rb") as f:
                        return await self._save_bytes(group_id, f.read(), filename)
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
                                    group_id, await resp.read(), filename
                                )
                            logger.error(f"从 URL 下载失败: HTTP {resp.status}")
                except Exception as e:
                    logger.error(f"从 URL 下载出错: {e}")

            logger.error("所有下载方式均失败")
            return None
        except Exception as e:
            logger.error(f"下载图片异常: {e}")
            return None

    async def _resolve_reply_image_file(
        self, event: AstrMessageEvent
    ) -> tuple[str, str] | None:
        """从被回复的消息中解析出 (图片 file_id, 推断的扩展名)。

        兼容数组/ CQ 码两种格式。扩展名优先取图 URL 后缀，否则取 file 字段后缀，
        无法判断时回退到 .jpg。
        """
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
            parts = chain if isinstance(chain, list) else []
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "image":
                    data = part.get("data", {}) or {}
                    file_id = data.get("file")
                    ext = self._detect_ext(data.get("url"), data.get("file"))
                    return file_id, ext
            if isinstance(chain, str):
                m = re.search(r"\[CQ:image,[^\]]*file=([^,\]]+)", chain)
                if m:
                    return m.group(1), self._detect_ext(None, m.group(1))
        except Exception as e:
            logger.error(f"获取引用消息图片失败: {e}")
        return None

    @staticmethod
    def _detect_ext(url: str | None, file: str | None) -> str:
        """从图片 URL 或 file 字段推断扩展名，回退 .jpg。"""
        for src in (url, file):
            if not src:
                continue
            lower = src.lower().split("?", 1)[0]
            for ext in IMAGE_EXTENSIONS:
                if lower.endswith(ext):
                    return ext
        return ".jpg"

    def _log_submission(
        self, group_id: str, user_id: str, filename: str, ts: float
    ) -> None:
        """将一次投稿记录以 JSONL 追加到插件数据目录下的 submit.log。"""
        QUOTES_ROOT.mkdir(parents=True, exist_ok=True)
        log_path = QUOTES_ROOT / "submit.log"
        record = {
            "ts": ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "group_id": str(group_id),
            "user_id": str(user_id),
            "filename": filename,
        }
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"写入投稿日志失败: {e}")

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
            yield event.chain_result(quote_chain(path))
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
    async def submit_quote(self, event: AstrMessageEvent, title: str = ""):
        """回复一张含图消息进行语录投稿，可选附带标题作为文件名"""
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

        # 必须（也仅）通过回复一条含图消息来投稿
        resolved = await self._resolve_reply_image_file(event)
        if not resolved:
            yield event.chain_result(
                [
                    Comp.At(qq=user_id),
                    Comp.Plain(
                        text=(
                            "\n请回复一条含图消息并发送“/语录投稿 [标题]”进行投稿。\n"
                            "示例：/语录投稿 杰克的奇妙比喻"
                        )
                    ),
                ]
            )
            event.stop_event()
            return
        file_id, ext = resolved

        # 决定文件名：未给标题则用“xxxx年xx月xx日xx:xx的投稿”
        ts = time.time()
        if title:
            stem = title.strip()
        else:
            stem = time.strftime("%Y年%m月%d日%H:%M的投稿", time.localtime(ts))
        filename = f"1.{stem}{ext}"

        path = await self._download_image(event, file_id, group_id, filename)
        if path and os.path.exists(path):
            # 投稿成功，写入投稿日志
            self._log_submission(group_id, user_id, filename, ts)
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
        cooldown = int(settings.get("cooldown", 10))
        last_poke = float(settings.get("last_poke", 0))

        if time.time() - last_poke < cooldown:
            remaining = cooldown - (time.time() - last_poke)
            logger.info(f"戳戳语录冷却中，剩余 {remaining:.0f} 秒")
            return

        settings["last_poke"] = time.time()
        self._save_settings(group_id, settings)

        gs = self._load_global_settings()
        prob = float(gs.get("poke_quote_probability", 0.85))
        if random.random() < prob:
            path = self._shuffler(group_id).next()
            if path:
                yield event.chain_result(quote_chain(path))
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

    # ------------------------------------------------------------------ #
    # WebUI Page 后端 API
    # ------------------------------------------------------------------ #
    async def web_get_settings(self):
        """读取全局设置，供 Page 渲染。"""
        return json_response(self._load_global_settings())

    async def web_save_settings(self):
        """保存全局设置，校验后写回并使已缓存的 shuffler 失效。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        cur = self._load_global_settings()

        # 默认投稿权限模式
        if "default_submit_mode" in payload:
            v = payload["default_submit_mode"]
            if int(v) not in (0, 1, 2):
                return error_response("default_submit_mode 必须是 0/1/2")
            cur["default_submit_mode"] = int(v)

        # 默认冷却（秒）
        if "default_cooldown" in payload:
            v = payload["default_cooldown"]
            if int(v) < 0:
                return error_response("default_cooldown 不能为负数")
            cur["default_cooldown"] = int(v)

        # 戳一戳触发概率
        if "poke_quote_probability" in payload:
            v = payload["poke_quote_probability"]
            if not (0 <= float(v) <= 1):
                return error_response("poke_quote_probability 必须在 0~1 之间")
            cur["poke_quote_probability"] = float(v)

        # 去重窗口
        if "recent_window" in payload:
            v = payload["recent_window"]
            if int(v) < 1:
                return error_response("recent_window 必须 >= 1")
            cur["recent_window"] = int(v)

        # 稀有度权重
        if "rarity_weights" in payload:
            rw = payload["rarity_weights"]
            if not isinstance(rw, dict):
                return error_response("rarity_weights 必须是对象")
            keys = ["weight_5", "weight_4", "weight_3", "weight_2", "weight_1"]
            for k in keys:
                if k in rw:
                    val = rw[k]
                    try:
                        f = float(val)
                    except (TypeError, ValueError):
                        return error_response(f"{k} 必须是数字")
                    if f < 0:
                        return error_response(f"{k} 不能为负")
                    cur.setdefault("rarity_weights", {})[k] = f

        self._save_global_settings(cur)
        # 使已缓存的 shuffler 失效，下次重建会读取新权重/窗口
        self._shufflers.clear()
        return json_response(cur)

    async def web_submit_log(self):
        """返回投稿日志（JSONL 解析），支持按群号过滤和限制条数。"""
        group_filter = request.query.get("group_id") or None
        try:
            limit = request.query.get("limit", 200, type=int)
        except Exception:
            limit = 200
        if limit < 0:
            limit = 200

        log_path = QUOTES_ROOT / "submit.log"
        records: list[dict] = []
        if log_path.exists():
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if group_filter and str(rec.get("group_id")) != str(
                            group_filter
                        ):
                            continue
                        records.append(rec)
            except Exception as e:
                logger.error(f"读取投稿日志失败: {e}")
        # 按时间倒序，限制条数
        records.sort(key=lambda r: float(r.get("ts", 0)), reverse=True)
        records = records[:limit]
        return json_response({"total": len(records), "items": records})

    async def terminate(self):
        """插件卸载时调用。"""
        self._shufflers.clear()
