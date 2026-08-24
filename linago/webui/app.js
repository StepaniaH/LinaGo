/* LinaGo console logic: token gate, tab routing, REST calls. */

const TOKEN_KEY = "linago_token";
const LANG_KEY = "linago_ui_lang";

let token = localStorage.getItem(TOKEN_KEY) || "";
let config = null;

const I18N = {
  zh: {
    "gate.title": "访问令牌",
    "gate.hint": "粘贴缓存目录中 web-token 文件里的内容。",
    "gate.submit": "解锁",
    "providers.title": "Providers / BYOK",
    "providers.new": "新建",
    "providers.key_ph": "已保存则留空保持不变",
    "compare.title": "对比输出",
    "compare.hint": "勾选参与对比的 provider（最多 4 个）。留空则只使用默认 provider。",
    "appearance.title": "外观",
    "appearance.apply": "应用并重渲染样式",
    "preview.title": "翻译",
    "preview.source": "原文",
    "preview.target": "译文",
    "lang.title": "语言与动作",
    "actions.title": "动作模板",
    "actions.add": "添加动作",
    "hotkeys.title": "快捷键",
    "hotkeys.hint":
      "绑定保存在 hyprland.conf 中；这里生成片段供复制。「仅本会话」通过 hyprctl keyword 生效，重载后失效。",
    "diag.title": "诊断",
    "diag.refresh": "重新检查",
    "diag.providers": "Provider 连通性",
    "save": "保存",
    "edit": "编辑",
    "cancel": "取消",
    "delete": "删除",
    "copy": "复制",
    "apply.session": "应用（仅本会话）",
    "saved": "已保存",
    "test": "测试",
  },
  en: {
    "gate.title": "Access token",
    "gate.hint": "Paste the contents of the web-token file in the cache directory.",
    "gate.submit": "Unlock",
    "providers.title": "Providers / BYOK",
    "providers.new": "New",
    "providers.key_ph": "leave empty to keep the stored key",
    "compare.title": "Compare output",
    "compare.hint": "Pick providers for side-by-side panes (max 4). Empty keeps the single default pane.",
    "appearance.title": "Appearance",
    "appearance.apply": "Apply and re-render stylesheet",
    "preview.title": "Translate",
    "preview.source": "Source",
    "preview.target": "Translation",
    "lang.title": "Language & actions",
    "actions.title": "Action templates",
    "actions.add": "Add action",
    "hotkeys.title": "Hotkeys",
    "hotkeys.hint":
      "Binds live in hyprland.conf; copy a snippet below. Session-only apply runs hyprctl keyword and does not persist.",
    "diag.title": "Diagnostics",
    "diag.refresh": "Re-run checks",
    "diag.providers": "Provider reachability",
    "save": "Save",
    "edit": "Edit",
    "cancel": "Cancel",
    "delete": "Delete",
    "copy": "Copy",
    "apply.session": "Apply (session)",
    "saved": "Saved",
    "test": "Test",
  },
};

function lang() {
  return localStorage.getItem(LANG_KEY) || "zh";
}

function t(key) {
  const table = I18N[lang()] || I18N.zh;
  return table[key] ?? key;
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPh);
  });
  document.getElementById("lang-toggle").textContent =
    lang() === "zh" ? "EN" : "中文";
}

function providerUrl(name) {
  return "/api/providers/" + encodeURIComponent(name);
}

async function api(method, path, body) {
  const options = { method, headers: { "X-LinaGo-Token": token } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("console unreachable");
  }
  if (response.status === 401) {
    showGate();
    throw new Error("unauthorized");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 2200);
}

/* ── tabs ─────────────────────────────────────────────────── */
document.getElementById("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  activateTab(button.dataset.tab);
});

function activateTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll("section.tab").forEach((sec) => {
    sec.classList.toggle("hidden", sec.id !== `tab-${name}`);
  });
  const loaders = {
    providers: loadProviders,
    compare: loadCompare,
    appearance: loadAppearance,
    language: loadLanguage,
    hotkeys: loadHotkeys,
    diagnostics: loadDiagnostics,
  };
  loaders[name]?.().catch(showError);
}

document.getElementById("lang-toggle").addEventListener("click", () => {
  localStorage.setItem(LANG_KEY, lang() === "zh" ? "en" : "zh");
  applyI18n();
});

/* ── providers ────────────────────────────────────────────── */
async function loadProviders() {
  config = await api("GET", "/api/config");
  renderProviderTable();
  renderProviderSelects();
}

function renderProviderTable() {
  const tbody = document.querySelector("#prov-table tbody");
  tbody.innerHTML = "";
  for (const [name, p] of Object.entries(config.providers)) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${name}</td><td>${p.type}</td><td>${p.model}</td>
      <td><span class="badge ${p.has_key ? "on" : "off"}">${p.has_key ? "key" : "—"}</span></td>
      <td class="row end">
        <button data-test="${name}">${t("test")}</button>
        <button data-edit="${name}">${t("edit")}</button>
        <button class="danger" data-del="${name}">${t("delete")}</button>
      </td>`;
    row.querySelector("[data-test]").onclick = async () => {
      const reply = await api("POST", "/api/test-provider", { name });
      const btn = row.querySelector("[data-test]");
      btn.textContent = (reply.ok ? "✓ " : "✕ ") + reply.detail;
      setTimeout(() => (btn.textContent = t("test")), 2500);
    };
    row.querySelector("[data-edit]").onclick = () => openProviderForm(name);
    row.querySelector("[data-del]").onclick = async () => {
      await api("DELETE", providerUrl(name));
      toast(t("saved"));
      await loadProviders();
    };
    tbody.append(row);
  }
}

function openProviderForm(name) {
  const form = document.getElementById("prov-form");
  form.classList.remove("hidden");
  form.dataset.name = name || "";
  document.getElementById("prov-form-title").textContent =
    name ? `provider: ${name}` : t("providers.new");
  form.reset();
  if (name) {
    const p = config.providers[name];
    if (p) {
      form.name.value = name;
      form.name.readOnly = true;
      form.type.value = p.type;
      form.label.value = p.label;
      form.base_url.value = p.base_url;
      form.model.value = p.model;
      form.timeout.value = p.timeout ?? "";
      form.temperature.value = p.temperature ?? "";
      form.max_tokens.value = p.max_tokens ?? "";
    }
  } else {
    form.name.readOnly = false;
  }
}

document.getElementById("prov-new").addEventListener("click", () => openProviderForm(""));
document.getElementById("prov-cancel").addEventListener("click", () => {
  document.getElementById("prov-form").classList.add("hidden");
});

document.getElementById("prov-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const name = (form.dataset.name || form.name.value.trim()).trim();
  const payload = {
    type: form.type.value,
    label: form.label.value.trim(),
    base_url: form.base_url.value.trim(),
    model: form.model.value.trim(),
  };
  for (const field of ["timeout", "temperature", "max_tokens"]) {
    if (form[field].value !== "") payload[field] = Number(form[field].value);
  }
  const apiKey = form.api_key.value.trim();
  if (apiKey) payload.api_key = apiKey;
  try {
    await api("PUT", providerUrl(name), payload);
  } catch (err) {
    showError(err);
    return;
  }
  form.classList.add("hidden");
  toast(t("saved"));
  await loadProviders();
});

/* ── compare ──────────────────────────────────────────────── */
async function loadCompare() {
  config = await api("GET", "/api/config");
  const box = document.getElementById("compare-list");
  box.innerHTML = "";
  for (const name of Object.keys(config.providers)) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = name;
    input.checked = config.compare.includes(name);
    label.append(input, `${config.providers[name].label} (${name})`);
    box.append(label);
  }
}

document.getElementById("compare-save").addEventListener("click", async () => {
  const names = [...document.querySelectorAll("#compare-list input:checked")]
    .map((el) => el.value);
  await api("PUT", "/api/compare", { providers: names });
  toast(t("saved"));
});

/* ── appearance ───────────────────────────────────────────── */
async function loadAppearance() {
  config = await api("GET", "/api/config");
  const resolved = await api("GET", "/api/appearance");
  const form = document.getElementById("appear-form");
  form.preset.value = config.appearance.preset || "dark";
  form.bg_alpha.value = Number(resolved.bg_alpha);
  form["bg-alpha-out"].value = Number(resolved.bg_alpha).toFixed(2);
  form.font_scale.value = Number(resolved.font_scale || 1);
  form["font-scale-out"].value = Number(resolved.font_scale || 1).toFixed(2);
  updatePreview(resolved);
}

function updatePreview(params) {
  const previewEl = document.getElementById("preview");
  previewEl.style.background = `rgba(${params.surface}, ${params.bg_alpha})`;
  previewEl.style.color = params.text_fg;
  previewEl.style.borderRadius = params.radius_card;
  previewEl.querySelectorAll(".pv-source")[0].style.color = params.src_fg;
  previewEl.querySelectorAll(".pv-text")[0].style.fontSize = params.text_px;
  previewEl.querySelectorAll(".pv-label").forEach((el) => {
    el.style.color = params.muted_fg;
  });
}

document.getElementById("appear-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const patch = {
    preset: form.preset.value,
    font_scale: Number(form.font_scale.value),
    bg_alpha: Number(form.bg_alpha.value),
  };
  if (form.accent.value) patch.accent = form.accent.value;
  const resolved = await api("PUT", "/api/appearance", patch);
  updatePreview(resolved.resolved ?? resolved);
  toast(t("saved"));
});

/* ── language & actions ───────────────────────────────────── */
async function loadLanguage() {
  config = await api("GET", "/api/config");
  const form = document.getElementById("lang-form");
  form.lang.value = config.lang;
  form.ocr_engine.value = config.ocr.engine;
  form.tesseract_langs.value = config.ocr.tesseract_langs;
  form.memory_enabled.checked = config.memory_enabled;
  form.history_enabled.checked = config.history_enabled;
  form.app_action.value = "";

  const tts = form.tts_provider;
  tts.innerHTML = '<option value="">—</option>';
  for (const name of Object.keys(config.providers)) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    if (config.tts_provider === name) option.selected = true;
    tts.append(option);
  }

  renderActionsTable(config.actions);
}

function renderActionsTable(actions) {
  const tbody = document.querySelector("#actions-table tbody");
  tbody.innerHTML = "";
  for (const [name, template] of Object.entries(actions)) {
    addActionButtonRow(name, template);
  }
}

function addActionButtonRow(name = "", template = "") {
  const row = document.createElement("tr");
  row.innerHTML = `
    <td style="width:160px"><input class="a-name" value="${name}" placeholder="name"></td>
    <td><textarea class="a-tpl" placeholder="{source} → {target}: ${"{text}"}">${template}</textarea></td>
    <td style="width:60px"><button class="danger">✕</button></td>`;
  row.querySelector(".danger").onclick = () => row.remove();
  document.querySelector("#actions-table tbody").append(row);
}

document.getElementById("action-add").addEventListener("click", () => addActionButtonRow());

document.getElementById("actions-save").addEventListener("click", async () => {
  const actions = {};
  document.querySelectorAll("#actions-table tr").forEach((row) => {
    const name = row.querySelector(".a-name").value.trim();
    const template = row.querySelector(".a-tpl").value.trim();
    if (name && template) actions[name] = template;
  });
  await api("PUT", "/api/actions", actions);
  toast(t("saved"));
});

document.getElementById("lang-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const appPatch = {};
  if (form.lang.value.trim()) appPatch.lang = form.lang.value.trim();
  if (form.app_action.value.trim()) appPatch.action = form.app_action.value.trim();
  await api("PUT", "/api/settings", {
    app: appPatch,
    ocr: {
      engine: form.ocr_engine.value,
      tesseract_langs: form.tesseract_langs.value.trim(),
    },
    tts: form.tts_provider.value ? { provider: form.tts_provider.value } : {},
    memory: { enabled: form.memory_enabled.checked },
    history: { enabled: form.history_enabled.checked },
  });
  toast(t("saved"));
});

/* ── hotkeys ──────────────────────────────────────────────── */
async function loadHotkeys() {
  const data = await api("GET", "/api/hotkeys");
  const list = document.getElementById("hk-list");
  list.innerHTML = "";
  const launcher = document.getElementById("hk-launcher");
  for (const item of data.suggestions) {
    const line = item.line.replace("<path-to>/run.sh", launcher.value);
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<code style="flex:1">${line}</code>`;
    const copyButton = document.createElement("button");
    copyButton.textContent = t("copy");
    copyButton.onclick = async () => {
      await navigator.clipboard.writeText(line);
      toast(t("saved"));
    };
    const applyButton = document.createElement("button");
    applyButton.textContent = t("apply.session");
    applyButton.onclick = async () => {
      const rhs = line.split("=")[1] ?? "";
      const args = ["bind", ...rhs.split(",").map((part) => part.trim())];
      try {
        const reply = await api("POST", "/api/hotkeys/apply", { args });
        document.getElementById("hk-status").textContent =
          reply.ok ? "" : reply.output || "failed";
        if (reply.ok) toast(t("saved"));
      } catch (err) {
        document.getElementById("hk-status").textContent = err.message;
      }
    };
    row.append(copyButton, applyButton);
    list.append(row);
  }
  launcher.onchange = () => loadHotkeys();
}

/* ── diagnostics ──────────────────────────────────────────── */
async function loadDiagnostics() {
  config = await api("GET", "/api/config");
  const report = await api("GET", "/api/doctor");
  const tbody = document.querySelector("#diag-table tbody");
  tbody.innerHTML = "";
  for (const check of report) {
    const row = document.createElement("tr");
    const state = check.ok
      ? '<span class="badge on">ok</span>'
      : check.warning
        ? '<span class="badge off">warn</span>'
        : '<span class="badge off">FAIL</span>';
    row.innerHTML = `<td>${check.name}</td><td>${state}</td><td>${check.detail}</td>`;
    tbody.append(row);
  }

  const probeList = document.getElementById("probe-list");
  probeList.innerHTML = "";
  for (const name of Object.keys(config.providers)) {
    const button = document.createElement("button");
    button.textContent = `${t("test")} · ${name}`;
    button.onclick = async () => {
      const reply = await api("POST", "/api/test-provider", { name });
      button.textContent = `${t("test")} · ${name}: ${reply.detail}`;
    };
    probeList.append(button);
  }
}

document.getElementById("diag-refresh").addEventListener("click", () => {
  loadDiagnostics().catch(showError);
});

/* ── gate & boot ──────────────────────────────────────────── */
function showGate() {
  document.getElementById("gate").classList.remove("hidden");
}

function showError(err) {
  const gateError = document.getElementById("gate-error");
  gateError.textContent = err.message;
  toast(err.message);
}

document.getElementById("gate-submit").addEventListener("click", async () => {
  token = document.getElementById("gate-token").value.trim();
  localStorage.setItem(TOKEN_KEY, token);
  document.getElementById("gate-error").textContent = "";
  try {
    await api("GET", "/api/config");
    document.getElementById("gate").classList.add("hidden");
    await loadProviders();
  } catch {
    /* keep the gate open */
  }
});

(async function boot() {
  applyI18n();
  try {
    await loadProviders();
  } catch {
    showGate();
  }
})();
