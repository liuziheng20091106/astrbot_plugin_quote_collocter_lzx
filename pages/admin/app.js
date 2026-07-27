// 群友语录管理面板前端
// 与 AstrBot Dashboard 通过 window.AstrBotPluginPage bridge 通信
const bridge = window.AstrBotPluginPage;

const RARITY_DEFS = [
  { key: "weight_5", label: "SSSR(5)", color: "var(--sssr)" },
  { key: "weight_4", label: "SSR(4)", color: "var(--ssr)" },
  { key: "weight_3", label: "SR(3)", color: "var(--sr)" },
  { key: "weight_2", label: "R(2)", color: "var(--r)" },
  { key: "weight_1", label: "新卡(1)", color: "var(--new)" },
];

const $ = (id) => document.getElementById(id);

function renderRaritySliders(weights) {
  const wrap = $("rarityWeights");
  wrap.innerHTML = "";
  for (const def of RARITY_DEFS) {
    const val = Number(weights?.[def.key] ?? 0);
    const row = document.createElement("div");
    row.className = "rarity-row";
    row.innerHTML = `
      <span class="rarity-name" style="color:${def.color}">${def.label}</span>
      <input type="range" min="0" max="20" step="0.1" data-key="${def.key}" value="${val}" />
      <input type="number" min="0" step="0.1" data-key-num="${def.key}" value="${val}" />
    `;
    const slider = row.querySelector('input[type=range]');
    const num = row.querySelector('input[type=number]');
    slider.addEventListener("input", () => {
      num.value = slider.value;
    });
    num.addEventListener("input", () => {
      slider.value = num.value;
    });
    wrap.appendChild(row);
  }
}

function collectRarityWeights() {
  const out = {};
  for (const def of RARITY_DEFS) {
    const num = document.querySelector(`input[data-key-num="${def.key}"]`);
    out[def.key] = Number(num?.value ?? 0);
  }
  return out;
}

function fillSettings(s) {
  $("default_submit_mode").value = String(s.default_submit_mode ?? 0);
  $("default_cooldown").value = s.default_cooldown ?? 10;
  $("poke_quote_probability").value = s.poke_quote_probability ?? 0.85;
  $("recent_window").value = s.recent_window ?? 8;
  renderRaritySliders(s.rarity_weights || {});
  renderPityInputs(s.pity_config || {});
}

function renderPityInputs(pity) {
  const wrap = $("pityGrid");
  if (!wrap) return;
  wrap.innerHTML = "";
  for (const def of RARITY_DEFS) {
    // weight_X → pity_X
    const key = def.key.replace("weight_", "pity_");
    const val = Number(pity?.[key] ?? 0);
    const row = document.createElement("div");
    row.className = "rarity-row";
    row.innerHTML = `
      <span class="rarity-name" style="color:${def.color}">${def.label}</span>
      <input type="range" min="0" max="100" step="1" data-pity="${key}" value="${val}" />
      <input type="number" min="0" step="1" data-pity-num="${key}" value="${val}" />
    `;
    const slider = row.querySelector('input[type=range]');
    const num = row.querySelector('input[type=number]');
    slider.addEventListener("input", () => {
      num.value = slider.value;
    });
    num.addEventListener("input", () => {
      slider.value = num.value;
    });
    wrap.appendChild(row);
  }
}

function collectPity() {
  const out = {};
  for (const def of RARITY_DEFS) {
    const key = def.key.replace("weight_", "pity_");
    const num = document.querySelector(`input[data-pity-num="${key}"]`);
    out[key] = Math.max(0, Math.round(Number(num?.value ?? 0)));
  }
  return out;
}

async function loadSettings() {
  try {
    const s = await bridge.apiGet("settings");
    fillSettings(s);
  } catch (e) {
    $("settingsMsg").textContent = "加载设置失败：" + e.message;
  }
}

async function saveSettings() {
  const payload = {
    default_submit_mode: Number($("default_submit_mode").value),
    default_cooldown: Number($("default_cooldown").value),
    poke_quote_probability: Number($("poke_quote_probability").value),
    recent_window: Number($("recent_window").value),
    rarity_weights: collectRarityWeights(),
    pity_config: collectPity(),
  };
  const msg = $("settingsMsg");
  msg.classList.remove("ok", "err");
  try {
    const saved = await bridge.apiPost("settings/save", payload);
    fillSettings(saved);
    msg.classList.add("ok");
    msg.textContent = "保存成功";
  } catch (e) {
    msg.classList.add("err");
    msg.textContent = "保存失败：" + e.message;
  }
}

function row(cells) {
  const tr = document.createElement("tr");
  for (const c of cells) {
    const td = document.createElement("td");
    td.textContent = c ?? "";
    tr.appendChild(td);
  }
  return tr;
}

async function loadLog() {
  const tbody = $("logTable").querySelector("tbody");
  tbody.innerHTML = "";
  const msg = $("logMsg");
  msg.classList.remove("ok", "err");
  msg.textContent = "";

  const group = $("groupFilter").value.trim();
  const limit = Number($("logLimit").value) || 200;
  try {
    const params = { limit };
    if (group) params.group_id = group;
    const res = await bridge.apiGet("submit-log", params);
    const items = res?.items || [];
    if (!items.length) {
      msg.textContent = "暂无日志";
      return;
    }
    for (const it of items) {
      tbody.appendChild(row([it.time, it.group_id, it.user_id, it.filename]));
    }
    msg.classList.add("ok");
    msg.textContent = `共 ${res?.total ?? items.length} 条`;
  } catch (e) {
    msg.classList.add("err");
    msg.textContent = "加载日志失败：" + e.message;
  }
}

function applyI18n(i18n) {
  if (!i18n) return;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    const v = i18n[key] || i18n[`admin.${key}`];
    if (v) el.textContent = v;
  });
}

async function main() {
  await bridge.ready();
  applyI18n(bridge.getI18n());
  renderRaritySliders({});
  await loadSettings();
  await loadLog();

  $("saveSettings").addEventListener("click", saveSettings);
  $("refreshLog").addEventListener("click", loadLog);

  // 主题/语言变化时刷新文案
  bridge.onContext(() => applyI18n(bridge.getI18n()));
}

main();