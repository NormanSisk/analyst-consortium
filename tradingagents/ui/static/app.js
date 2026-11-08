"use strict";
// The setup screen's script. It talks only to this server's /api, with the session token the page was served with.
const TOKEN = document.querySelector('meta[name="council-token"]').content;
const $ = id => document.getElementById(id);
const LABEL = {ok: "OK", needs_setup: "NEEDS SETUP", not_working: "NOT WORKING", off: "OFF", skipped: "SKIPPED"};
const SYMBOL = {ok: "✓", needs_setup: "!", not_working: "✕", off: "○", skipped: "–"};
const GROUPS = [["environment", "Your computer and folders"], ["ai", "AI connections"], ["data", "Data sources"], ["settings", "Your settings"]];
const PRETTY = {anthropic: "Anthropic (Claude)", openai: "OpenAI", google: "Google (Gemini)", xai: "xAI (Grok)", deepseek: "DeepSeek",
  azure: "Azure OpenAI", bedrock: "Amazon Bedrock", qwen: "Qwen", "qwen-cn": "Qwen (China)", glm: "GLM (Zhipu)", "glm-cn": "GLM (China)",
  minimax: "MiniMax", "minimax-cn": "MiniMax (China)", openrouter: "OpenRouter", mistral: "Mistral", kimi: "Kimi (Moonshot)", groq: "Groq",
  nvidia: "NVIDIA NIM", chatgpt: "ChatGPT plan (sign-in)", ollama: "Ollama (runs on this computer)", openai_compatible: "Other OpenAI-compatible"};
const FRIENDLY = {TRADINGAGENTS_LLM_PROVIDER: "provider", TRADINGAGENTS_QUICK_THINK_LLM: "quick model", TRADINGAGENTS_DEEP_THINK_LLM: "deep model",
  TRADINGAGENTS_RESEARCH_PASSES: "research passes", SEC_EDGAR_USER_AGENT: "SEC contact", TRADINGAGENTS_NO_SELLOFF_RULES: "selloff rules",
  TRADINGAGENTS_NO_SCORECARD: "shared scorecard"};
const FIELD_FOR = {"env.dotenv": "f-provider", "data.sec": "f-sec", "data.fred": "f-fred", "data.quiver": "f-quiver", "data.alphavantage": "f-alpha"};
const NEW_DATA_KEYS = {"f-fred": "FRED_API_KEY", "f-quiver": "QUIVER_API_KEY", "f-alpha": "ALPHA_VANTAGE_API_KEY"};

let state = null, results = [], initial = {};
const openGroups = {environment: true, ai: true, data: true, settings: true};

const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

async function api(path, body) {
  const opt = {headers: {"X-Council-Token": TOKEN}};
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (_) { /* not JSON */ }
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" && d ? d : (d ? "The server did not accept what the page sent. Reload the page and try again." : "The request failed (HTTP " + r.status + ")."));
  }
  return data;
}

// A POST whose answer is a file (CSV or PDF), returned as a Blob.
async function apiBlob(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"X-Council-Token": TOKEN, "Content-Type": "application/json"}, body: JSON.stringify(body)});
  if (!r.ok) {
    let d = null;
    try { d = await r.json(); } catch (_) { /* not JSON */ }
    throw new Error((d && typeof d.detail === "string" && d.detail) || "The request failed (HTTP " + r.status + ").");
  }
  return r.blob();
}

// ---------------------------------------------------------------- results
const badge =st => `<div class="badge ${st}"><span aria-hidden="true">${SYMBOL[st]}</span><span>${LABEL[st]}</span></div>`;

function fixTarget(r) {
  if (r.id.indexOf("ai.key.") === 0 || /^ai\.(quick|deep)\.call$/.test(r.id)) return "f-key";   // a failed live test is most often the key
  if (/^ai\.quick\.model$/.test(r.id)) return "f-quick";
  if (/^ai\.deep\.model$/.test(r.id)) return "f-deep";
  return FIELD_FOR[r.id] || null;
}

function row(r) {
  const problem = r.status === "needs_setup" || r.status === "not_working";
  const tag = r.required ? '<span class="tag">REQUIRED</span>' : "";
  const target = problem ? fixTarget(r) : null;
  return `<li class="check" data-id="${esc(r.id)}">${badge(r.status)}<div><h3>${esc(r.title)}${tag}</h3>` +
    `<p class="detail">${esc(r.detail)}</p>` +
    (problem && r.fix ? `<p class="fix"><strong>To fix:</strong> ${esc(r.fix)}</p>` : "") +
    (r.enables && (problem || r.status === "off") ? `<p class="adds"><strong>Adds:</strong> ${esc(r.enables)}.</p>` : "") +
    `</div><div class="actions col">` +
    (target ? `<button type="button" data-fix="${esc(target)}">Fix it here</button>` : "") +
    `<button type="button" class="secondary" data-test="${esc(r.id)}">Test again</button></div></li>`;
}

function render() {
  const c = {};
  Object.keys(LABEL).forEach(k => { c[k] = results.filter(r => r.status === k).length; });
  const ready = !results.some(r => r.required && (r.status === "needs_setup" || r.status === "not_working"));
  $("summary").innerHTML = `<p class="big">READY TO RUN: ${ready ? "YES" : "NO"}</p>` +
    `<p>${ready ? "Everything required works." : "Fix the items marked REQUIRED first."}</p>` +
    `<p class="counts">${c.ok} OK, ${c.needs_setup} need setup, ${c.not_working} not working, ${c.off} off, ${c.skipped} skipped.</p>`;
  $("groups").innerHTML = GROUPS.map(([id, title]) => {
    const rows = results.filter(r => r.group === id);
    if (!rows.length) return "";
    const attention = rows.filter(r => r.status === "needs_setup" || r.status === "not_working").length;
    return `<details class="group" data-group="${id}"${openGroups[id] ? " open" : ""}><summary><h2>${esc(title)}</h2>` +
      `<span class="count">${attention ? attention + " need attention" : "all fine"}</span></summary>` +
      `<ul class="checks">${rows.map(row).join("")}</ul></details>`;
  }).join("");
  return ready;
}

function setBusy(busy) {
  const b = $("recheck");
  b.disabled = busy; b.textContent = busy ? "Checking..." : "Check everything again";
  if (busy && !results.length) $("summary").innerHTML = '<p class="big">Checking...</p><p>This takes a few seconds.</p>';
}

async function runChecks(ids, forceLive) {
  setBusy(true);
  try {
    const rep = await api("/api/doctor", {live_ai: forceLive || $("live").checked, ids: ids || null});
    if (ids) {
      rep.results.forEach(n => { const i = results.findIndex(r => r.id === n.id); if (i >= 0) results[i] = n; else results.push(n); });
    } else {
      results = rep.results;
    }
    const ok = render();
    window.acReady = ok;
    document.dispatchEvent(new CustomEvent("ac-checks", {detail: {ready: ok, error: false}}));      // the run form listens
    return ok;
  } catch (e) {
    $("summary").innerHTML = `<p class="big">Could not run the checks</p><p>${esc(e.message)}</p>`;
    document.dispatchEvent(new CustomEvent("ac-checks", {detail: {ready: false, error: true}}));
    return false;
  } finally {
    setBusy(false);
  }
}

// ---------------------------------------------------------------- the configuration form
const providerInfo = id => state.choices.providers.find(p => p.id === id);
const snapshot = () => ({provider: $("f-provider").value, quick: $("f-quick").value.trim(), deep: $("f-deep").value.trim(),
  passes: $("f-passes").value, sec: $("f-sec").value.trim(), selloff: $("f-selloff").checked, scorecard: $("f-scorecard").checked});

function fillForm() {
  const eff = state.effective, vals = state.settings.values, sec = state.settings.secrets;
  $("f-provider").innerHTML = state.choices.providers.map(p => `<option value="${esc(p.id)}">${esc(PRETTY[p.id] || p.id)}</option>`).join("");
  $("f-provider").value = eff.provider;
  $("f-quick").value = eff.quick; $("f-deep").value = eff.deep;
  const opts = state.choices.passes.slice(); if (opts.indexOf(eff.passes) < 0) opts.push(eff.passes);
  $("f-passes").innerHTML = opts.sort((a, b) => a - b).map(n => `<option>${n}</option>`).join("");
  $("f-passes").value = String(eff.passes);
  $("f-selloff").checked = vals.TRADINGAGENTS_NO_SELLOFF_RULES !== "1";
  $("f-scorecard").checked = vals.TRADINGAGENTS_NO_SCORECARD !== "1";
  $("f-sec").value = vals.SEC_EDGAR_USER_AGENT || "";
  Object.keys(NEW_DATA_KEYS).forEach(id => {
    $(id).value = "";
    $(id).placeholder = sec[NEW_DATA_KEYS[id]] ? "A key is saved. Type to replace it." : "";
  });
  $("f-key").value = "";
  initial = snapshot();
  providerChanged(false);
  costLine();
  $("where").textContent = state.dotenv.exists ? "Settings are saved in " + state.dotenv.path : "No .env file yet: saving creates " + state.dotenv.path;
}

function providerChanged(userChange) {
  const p = providerInfo($("f-provider").value), keyEnv = p && p.key_env;
  $("field-key").hidden = !keyEnv;
  $("l-key").textContent = keyEnv ? "API key (" + keyEnv + ")" : "API key";
  $("f-key").placeholder = keyEnv && state.settings.secrets[keyEnv] ? "A key is saved. Type to replace it." : "Paste your key here";
  $("h-key").textContent = keyEnv ? (state.settings.secrets[keyEnv] ? "A key is saved. Leave this empty to keep it."
    : "Saved only in the .env file on this computer, never shown again, and sent only to the provider.") : "";
  $("h-provider").textContent = p && p.sign_in ? "This provider uses your sign-in, not a key: run  tradingagents login " + p.id + "  in a terminal."
    : (p && !p.key_env ? "This provider needs no key here." : "");
  $("dl-models").innerHTML = (state.choices.models[p ? p.id : ""] || []).map(m => `<option value="${esc(m)}">`).join("");
  if (userChange && $("f-quick").value.trim() === initial.quick && $("f-deep").value.trim() === initial.deep && p.id !== initial.provider) {
    $("f-quick").value = ""; $("f-deep").value = "";   // they belong to the old provider
    $("f-quick").placeholder = $("f-deep").placeholder = "Type or pick a model name";
  }
}

function costLine() {
  const n = +$("f-passes").value, usd = (0.13 + 0.0675 * (n - 1)).toFixed(2), min = (4.5 + 0.45 * (n - 1)).toFixed(0);
  $("h-cost").textContent = `About $${usd} and ${min} minutes per analysis (measured with Anthropic Haiku and Sonnet; other providers differ). ` +
    (n === 1 ? "One pass can flip between neighboring ratings on a borderline stock." : "The report shows the median verdict and how many passes agreed.");
}

function collectUpdates() {
  const u = {}, now = snapshot();
  const diff = (name, a, b) => { if (a !== b) u[name] = a === "" ? null : a; };
  diff("TRADINGAGENTS_LLM_PROVIDER", now.provider, initial.provider);
  diff("TRADINGAGENTS_QUICK_THINK_LLM", now.quick, initial.quick);
  diff("TRADINGAGENTS_DEEP_THINK_LLM", now.deep, initial.deep);
  diff("TRADINGAGENTS_RESEARCH_PASSES", now.passes, initial.passes);
  diff("SEC_EDGAR_USER_AGENT", now.sec, initial.sec);
  if (now.selloff !== initial.selloff) u.TRADINGAGENTS_NO_SELLOFF_RULES = now.selloff ? null : "1";
  if (now.scorecard !== initial.scorecard) u.TRADINGAGENTS_NO_SCORECARD = now.scorecard ? null : "1";
  const p = providerInfo(now.provider);
  if (p && p.key_env && $("f-key").value.trim()) u[p.key_env] = $("f-key").value.trim();
  Object.keys(NEW_DATA_KEYS).forEach(id => { if ($(id).value.trim()) u[NEW_DATA_KEYS[id]] = $(id).value.trim(); });
  return u;
}

function describe(names) {
  const parts = [];
  names.forEach(n => { const w = FRIENDLY[n] || "a key"; if (parts.indexOf(w) < 0) parts.push(w); });
  return parts.join(", ");
}

async function saveSettings(thenTest) {
  const out = $("save-result"), updates = collectUpdates(), now = snapshot();
  if (now.provider !== initial.provider && (!now.quick || !now.deep)) {
    out.textContent = "Choose a quick model and a deep model for this provider first."; return;
  }
  $("save").disabled = $("save-test").disabled = true;
  try {
    if (Object.keys(updates).length) {
      const r = await api("/api/settings", {updates});
      state = r.state; fillForm();
      out.textContent = "Saved: " + describe(r.changed) + ". Keys are saved but never shown again.";
    } else {
      out.textContent = "Nothing was changed.";
    }
    await runChecks(null, thenTest);
    $("configure").open = !results.every(r => !(r.required && (r.status === "needs_setup" || r.status === "not_working")));
  } catch (e) {
    out.textContent = "Not saved: " + e.message;
  } finally {
    $("save").disabled = $("save-test").disabled = false;
  }
}

// ---------------------------------------------------------------- display options and events
function pref(name, value) { try { if (value === undefined) return localStorage.getItem(name); localStorage.setItem(name, value); } catch (_) { /* storage blocked */ } return null; }
function setSize(v) { document.documentElement.dataset.size = v; document.querySelectorAll("#sizes button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.size === v))); pref("ac-size", v); }
function setBg(v) { document.documentElement.dataset.bg = v; document.querySelectorAll("#backgrounds button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.bg === v))); pref("ac-bg", v); }
function setTheme(v) { document.documentElement.dataset.theme = v; document.querySelectorAll("#themes button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.theme === v))); pref("ac-theme", v); }

document.addEventListener("click", e => {
  const t = e.target.closest("button"); if (!t) return;
  if (t.dataset.size) setSize(t.dataset.size);
  if (t.dataset.theme) setTheme(t.dataset.theme);
  if (t.dataset.bg) setBg(t.dataset.bg);
  if (t.dataset.test) runChecks([t.dataset.test]);
  if (t.dataset.fix) { $("configure").open = true; const f = $(t.dataset.fix); if (f && !f.closest("[hidden]")) { f.scrollIntoView({block: "center"}); f.focus(); } }
});
document.addEventListener("toggle", e => { const g = e.target.dataset && e.target.dataset.group; if (g) openGroups[g] = e.target.open; }, true);
$("recheck").addEventListener("click", () => runChecks(null));
$("f-provider").addEventListener("change", () => providerChanged(true));
$("f-passes").addEventListener("change", costLine);
$("show-key").addEventListener("click", e => { const f = $("f-key"), show = f.type === "password"; f.type = show ? "text" : "password"; e.target.textContent = show ? "Hide" : "Show"; e.target.setAttribute("aria-pressed", String(show)); });
$("config-form").addEventListener("submit", e => { e.preventDefault(); saveSettings(false); });
$("save-test").addEventListener("click", () => saveSettings(true));

(async function init() {
  setSize(pref("ac-size") || "normal"); setTheme(pref("ac-theme") || "light"); setBg(pref("ac-bg") || "market");
  try { state = await api("/api/state"); fillForm(); }
  catch (e) { $("summary").innerHTML = `<p class="big">Could not start</p><p>${esc(e.message)}</p>`; return; }
  const ready = await runChecks(null);
  $("configure").open = !ready;          // open for first-time setup, closed once everything works
})();
