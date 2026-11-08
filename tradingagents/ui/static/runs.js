"use strict";
// The run form and the list of analyses. It uses api(), esc() and $() from app.js and talks only to this server.
const RATING_MARK = {Buy: "▲▲", Overweight: "▲", Hold: "●", Underweight: "▼", Sell: "▼▼"};
const JOB_STATUS = {waiting: ["○", "WAITING"], running: ["▶", "RUNNING"], done: ["✓", "DONE"], failed: ["✕", "FAILED"], stopped: ["■", "STOPPED"]};
const STAGE_MARK = {done: ["✓", "DONE"], now: ["▶", "NOW"], waiting: ["○", "WAITING"]};
const ACTIVE = ["waiting", "running"];
const SHOWN = 6;                                  // newest analyses listed openly; the rest sit under "Earlier analyses"

let runOpts = null, setupReady = typeof window.acReady === "boolean" ? window.acReady : null, setupError = false;
let jobs = [], pollTimer = null, submitting = false;
const lastStatus = {}, keepOpen = {};

const usd = x => "$" + Number(x).toFixed(2);
const dur = s => (s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s");
const plural = (n, one, many) => n + " " + (n === 1 ? one : many);
const provName = () => (typeof PRETTY !== "undefined" && PRETTY[runOpts.provider.id]) || runOpts.provider.id;

// ---------------------------------------------------------------- the form
function parseTickers() { return $("r-tickers").value.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean); }
function uniqueTickers() { return Array.from(new Set(parseTickers().map(s => s.toUpperCase()))); }

function fillRunForm() {
  const o = runOpts, d = o.defaults;
  $("r-date").max = o.today; $("r-date").value = o.today;
  $("r-analysts").innerHTML = o.analysts.map(a =>
    `<label class="inline"><input type="checkbox" value="${esc(a.id)}" checked><span><strong>${esc(a.label)}</strong><br><span class="hint">${esc(a.help)}</span></span></label>`).join("");
  $("r-depth").innerHTML = o.depths.map(x => `<option value="${x.value}">${esc(x.label)}</option>`).join("");
  $("r-depth").value = String(d.depth);
  $("r-passes").innerHTML = o.passes.map(n => `<option value="${n}">${n}</option>`).join("");
  $("r-passes").value = String(d.passes);
  const langs = o.languages.slice(); if (langs.indexOf(d.language) < 0) langs.push(d.language);
  $("r-language").innerHTML = langs.map(l => `<option>${esc(l)}</option>`).join("") + '<option value="__other">Other (type it)</option>';
  $("r-language").value = d.language;
  $("r-holdings").innerHTML = '<option value="">None</option>' + o.holdings.map(h => {
    const bits = [plural(h.positions, "position", "positions")];
    if (h.cash !== null && h.cash !== undefined) bits.push("cash " + Number(h.cash).toLocaleString() + (h.currency ? " " + h.currency : ""));
    if (h.profile) bits.push("with your profile text");
    return `<option value="${esc(h.id)}">${esc(h.id)} (${esc(bits.join(", "))})</option>`;
  }).join("");
  $("r-selloff").checked = d.selloff; $("r-scorecard").checked = d.scorecard;
  updateParsed(); updateDateNote(); updateDepthHint(); updateHoldingsHint(); updateLanguage(); updateEstimate();
}

function updateParsed() {
  const t = uniqueTickers();
  $("r-parsed").textContent = t.length ? "Will analyze " + plural(t.length, "symbol", "symbols") + ": " + t.join(", ") + (t.length > runOpts.max_tickers ? ". That is more than " + runOpts.max_tickers + "; remove some." : ".") : "";
}

function updateDateNote() {
  const note = $("r-date-note"), v = $("r-date").value;
  const past = runOpts && v && v < runOpts.today;
  note.hidden = !past;
  note.textContent = past ? "NOTE: this is not today. Data that only exists for today (the company profile, insider trades and financial statements) is left out of an earlier-dated run, so the analysis is thinner." : "";
}

function updateDepthHint() { const x = runOpts.depths.find(d => String(d.value) === $("r-depth").value); $("h-depth").textContent = x ? x.help : ""; }

function updateHoldingsHint() {
  const id = $("r-holdings").value, o = runOpts;
  if (id) { $("h-holdings").textContent = "Privacy: the contents of " + id + ", including any profile text in it, are sent to " + provName() + " inside the prompts of every run."; return; }
  $("h-holdings").textContent = "No holdings file: the trader, risk team and portfolio manager are told nothing about what you own, so their advice is general." +
    (o.holdings.length ? "" : " To use one, save a holdings .json file in " + o.holdings_folders.join(" or ") + " and reload this page.");
}

function updateLanguage() { $("r-language-other").hidden = $("r-language").value !== "__other"; }

function updateEstimate() {
  if (!runOpts) return;
  const n = Math.max(1, uniqueTickers().length), c = runOpts.costs[$("r-passes").value];
  if (!c) { $("r-estimate").textContent = ""; return; }
  let t = "Expect roughly " + c.minutes * n + " minutes for " + plural(n, "analysis", "analyses") +
    (c.measured ? " and about " + usd(c.usd * n) + " in total." : ". The cost is not known for your models.");
  if (+$("r-depth").value > 1) t += " Deeper research lengthens every debate, so expect more than this.";
  t += c.measured ? " (Measured with Anthropic's Haiku and Sonnet at Shallow depth.)" : " (Time was measured with Anthropic's models.)";
  $("r-estimate").textContent = t;
}

function updateGate() {
  const gate = $("run-gate"), btn = $("start");
  let msg = "", ok = true;
  if (!runOpts) { msg = "LOADING: the form is loading."; ok = false; }
  else if (setupError) { msg = "NOT KNOWN: the setup check could not run, so it is not known whether you are ready. See the left column."; ok = false; }
  else if (setupReady === null) { msg = "CHECKING: your setup is being checked first. This takes a few seconds."; ok = false; }
  else if (setupReady === false) { msg = "NOT READY: something marked REQUIRED needs fixing first. Open Configure connections below, fix the items marked NEEDS SETUP or NOT WORKING, then come back."; ok = false; }
  else if (runOpts.provider.key_env && !runOpts.provider.key_set) { msg = "NOT READY: no key is saved for " + provName() + ". Open Configure connections below, paste the key and save."; ok = false; }
  gate.hidden = !msg;
  gate.innerHTML = esc(msg) + (setupReady === false || (runOpts && runOpts.provider.key_env && !runOpts.provider.key_set) ? ' <button type="button" class="secondary" id="go-config">Go to the setup items</button>' : "");
  btn.disabled = !ok || submitting;
}

async function submitRun(e) {
  e.preventDefault();
  const err = $("run-error"), res = $("run-result");
  err.textContent = ""; res.textContent = "";
  const analysts = Array.from(document.querySelectorAll("#r-analysts input:checked")).map(i => i.value);
  let language = $("r-language").value;
  if (language === "__other") language = $("r-language-other").value.trim();
  const fail = (msg, field) => { err.textContent = msg; if (field) field.focus(); };
  if (!parseTickers().length) return fail("Type at least one stock symbol, for example NVDA.", $("r-tickers"));
  if (uniqueTickers().length > runOpts.max_tickers) return fail("Up to " + runOpts.max_tickers + " symbols at a time, please.", $("r-tickers"));
  if (!$("r-date").value) return fail("Pick a date.", $("r-date"));
  if (!analysts.length) return fail("Choose at least one analyst.", document.querySelector("#r-analysts input"));
  if (!language) return fail("Type the language for the report.", $("r-language-other"));
  submitting = true; updateGate();
  try {
    const r = await api("/api/runs", {tickers: uniqueTickers(), date: $("r-date").value, analysts, depth: +$("r-depth").value,
      passes: +$("r-passes").value, language, holdings: $("r-holdings").value || null,
      selloff: $("r-selloff").checked, scorecard: $("r-scorecard").checked});
    res.textContent = r.started.length === 1 ? "Started. It is listed under Your analyses." : r.started.length + " analyses queued. They run one after another and are listed under Your analyses.";
    await refreshRuns();
    const h = $("jobs-h"); h.tabIndex = -1; h.focus();
  } catch (x) {
    err.textContent = "Not started: " + x.message;
  } finally {
    submitting = false; updateGate();
  }
}

// ---------------------------------------------------------------- the list
function elapsedText(j) {
  if (j.status === "running") return "Running for " + dur(j.elapsed || 0) + ". Expect roughly " + j.expected_minutes + " minutes.";
  if (j.status === "waiting") return j.place ? "Waiting: number " + j.place + " in line." : "Waiting.";
  return j.elapsed === null || j.elapsed === undefined ? "" : "Took " + dur(j.elapsed) + ".";
}

function latestText(j) {
  const p = j.progress; if (!p) return "";
  if (!p.latest) return "No activity recorded yet.";
  return "Latest activity (" + dur(p.latest.seconds_ago) + " ago): " + p.latest.text;
}

function sig(j) {
  const c = Object.assign({}, j); delete c.elapsed; delete c.expected_minutes;
  if (c.progress) c.progress = {stages: c.progress.stages};
  return JSON.stringify(c);
}

function ratingBlock(r) {
  if (r.review) return '<p class="rating"><span class="rating-word">NO RATING</span></p><p class="problem">The final decision had no rating that could be read, so it is kept for review and is not a position. Read the report, or run it again.</p>';
  return `<p class="rating"><span aria-hidden="true">${RATING_MARK[r.rating] || ""}</span> <span class="rating-word">${esc(String(r.rating).toUpperCase())}</span> <span class="muted">rating</span></p>`;
}

function resultHtml(j) {
  const r = j.result; if (!r) return "";
  const parts = [ratingBlock(r)];
  if (r.score !== null && r.score !== undefined) parts.push(`<p>Conviction score: <strong>${r.score} of 100</strong></p>`);
  if (r.borderline_text) parts.push(`<p>${r.borderline ? '<span class="tag">BORDERLINE</span> ' : ""}${esc(r.borderline_text)}</p>`);
  if (r.panel && r.panel.summary) {
    const split = r.panel.label === "borderline";
    parts.push(`<p><strong>Research panel:</strong> ${esc(r.panel.summary)}${split ? ' <span class="tag">SPLIT</span> The passes ranged from ' + esc((r.panel.range || []).join(" to ")) + "." : ""}</p>`);
  }
  if (r.summary) parts.push(`<h4>Executive summary</h4><p class="summary-text">${esc(r.summary)}</p>`);
  const meta = [];
  if (r.cost_usd !== null && r.cost_usd !== undefined) meta.push("cost about " + usd(r.cost_usd) + " at list prices");
  if (r.seconds) meta.push(dur(r.seconds));
  if (r.llm_calls) meta.push(r.llm_calls + " AI calls");
  if (r.fallbacks) meta.push(plural(r.fallbacks, "answer was", "answers were") + " read from plain text");
  if (meta.length) parts.push(`<p class="hint">${esc(meta.join(" · "))}</p>`);
  if (r.report_dir) parts.push(`<p class="hint">The full report is saved in <code>${esc(r.report_dir)}</code> (open complete_report.html there).</p>`);
  return `<div class="result-card">${parts.join("")}</div>`;
}

function stagesHtml(j) {
  const p = j.progress; if (!p) return "";
  return `<ol class="stages">${p.stages.map(s => {
    const m = STAGE_MARK[s.state];
    return `<li class="stage st-${s.state}"><span class="mark"><span aria-hidden="true">${m[0]}</span> ${m[1]}</span><span>${esc(s.label)}${s.note ? ` <span class="muted">(${esc(s.note)})</span>` : ""}</span></li>`;
  }).join("")}</ol><p class="hint" data-latest>${esc(latestText(j))}</p>`;
}

function jobItem(j) {
  const st = JOB_STATUS[j.status] || ["?", String(j.status).toUpperCase()];
  const labels = (runOpts ? runOpts.analysts : []).filter(a => j.analysts.indexOf(a.id) >= 0).map(a => a.label.replace(" analyst", "")).join(", ");
  const depth = runOpts ? (runOpts.depths.find(d => d.value === j.depth) || {}).label : "";
  const meta = `${depth || "Depth " + j.depth} research, ${plural(j.passes, "pass", "passes")}, analysts: ${labels || j.analysts.join(", ")}, language: ${j.language}, holdings: ${j.holdings || "none"}.`;
  let body = "";
  if (ACTIVE.indexOf(j.status) >= 0) {
    body += `<p class="elapsed" data-elapsed>${esc(elapsedText(j))}</p>` + stagesHtml(j) +
      `<div class="row"><button type="button" class="secondary" data-stop="${esc(j.id)}" data-name="${esc(j.ticker)}" data-running="${j.status === "running"}">${j.status === "running" ? "Stop this analysis" : "Remove from the queue"}</button></div>`;
  } else if (j.status === "done") {
    body += `<p class="elapsed" data-elapsed>${esc(elapsedText(j))}</p>` + resultHtml(j);
  } else {
    body += `<p class="problem"><strong>${j.status === "failed" ? "What happened:" : "Note:"}</strong> ${esc(j.error)}</p>`;
    if (j.detail) body += `<details data-keep="${esc(j.id)}"${keepOpen[j.id] ? " open" : ""}><summary><span class="btn-like secondary">Show the last lines of the log</span></summary><pre>${esc(j.detail)}</pre></details>`;
    if (j.log) body += `<p class="hint">The whole log is in <code>${esc(j.log)}</code>.</p>`;
  }
  const notes = (j.notes || []).map(n => `<p class="hint">${esc(n)}</p>`).join("");
  return `<li class="job job-${j.status}" data-job="${esc(j.id)}" data-sig="${esc(sig(j))}"><div class="job-head"><h3>${esc(j.ticker)} <span class="muted">for ${esc(j.date)}</span></h3>` +
    `<div class="badge job-${j.status}"><span aria-hidden="true">${st[0]}</span><span>${st[1]}</span></div></div><p class="hint">${esc(meta)}</p>${notes}${body}</li>`;
}

function syncList(ul, items) {
  const ids = items.map(j => j.id).join(",");
  if (ul.dataset.ids !== ids) { ul.innerHTML = items.map(jobItem).join(""); ul.dataset.ids = ids; return; }
  items.forEach(j => {
    const li = ul.querySelector('[data-job="' + j.id + '"]'); if (!li) return;
    if (li.dataset.sig !== sig(j)) { li.outerHTML = jobItem(j); return; }
    const e = li.querySelector("[data-elapsed]"); if (e) e.textContent = elapsedText(j);
    const l = li.querySelector("[data-latest]"); if (l) l.textContent = latestText(j);
  });
}

function renderRuns() {
  const first = jobs.slice(0, SHOWN), rest = jobs.slice(SHOWN);
  $("runs-empty").hidden = jobs.length > 0;
  syncList($("runs"), first);
  $("runs-old").hidden = rest.length === 0;
  $("runs-old-label").textContent = "Earlier analyses (" + rest.length + ")";
  syncList($("runs-older"), rest);
}

function announce() {
  const say = [];
  jobs.forEach(j => {
    const was = lastStatus[j.id];
    if (was !== undefined && was !== j.status) {
      if (j.status === "running") say.push(j.ticker + " has started.");
      else if (j.status === "done") say.push(j.ticker + " is done: " + (j.result && !j.result.review ? "rating " + j.result.rating + "." : "no readable rating."));
      else if (j.status === "failed") say.push(j.ticker + " failed.");
      else if (j.status === "stopped") say.push(j.ticker + " was stopped.");
    }
    lastStatus[j.id] = j.status;
  });
  if (say.length) $("run-announce").textContent = say.join(" ");
}

async function refreshRuns() {
  clearTimeout(pollTimer);
  let again = false;
  try {
    jobs = (await api("/api/runs")).runs;
    announce(); renderRuns();
    again = jobs.some(j => ACTIVE.indexOf(j.status) >= 0);
  } catch (_) {
    again = jobs.some(j => ACTIVE.indexOf(j.status) >= 0);        // a missed poll is not the end of a running analysis
  }
  if (again) pollTimer = setTimeout(refreshRuns, 2000);
}

async function stopJob(btn) {
  const running = btn.dataset.running === "true";
  if (running && !window.confirm("Stop the " + btn.dataset.name + " analysis? What it has done so far is lost, and the AI calls already made are still billed.")) return;
  btn.disabled = true;
  try { await api("/api/runs/" + encodeURIComponent(btn.dataset.stop) + "/cancel", {}); }
  catch (x) { $("run-error").textContent = "Could not stop it: " + x.message; }
  await refreshRuns();
}

// ---------------------------------------------------------------- events
document.addEventListener("ac-checks", e => { setupReady = e.detail.ready; setupError = !!e.detail.error; updateGate(); });
document.addEventListener("toggle", e => { const k = e.target.dataset && e.target.dataset.keep; if (k) keepOpen[k] = e.target.open; }, true);
document.addEventListener("click", e => {
  const t = e.target.closest("button"); if (!t) return;
  if (t.id === "go-config") { $("configure").open = true; $("configure").scrollIntoView({block: "start"}); $("configure").querySelector("summary").focus(); }
  if (t.dataset.stop) stopJob(t);
});
$("run-form").addEventListener("submit", submitRun);
$("r-tickers").addEventListener("input", () => { updateParsed(); updateEstimate(); });
$("r-date").addEventListener("input", updateDateNote);
$("r-depth").addEventListener("change", () => { updateDepthHint(); updateEstimate(); });
$("r-passes").addEventListener("change", updateEstimate);
$("r-holdings").addEventListener("change", updateHoldingsHint);
$("r-language").addEventListener("change", () => { updateLanguage(); if (!$("r-language-other").hidden) $("r-language-other").focus(); });

(async function initRuns() {
  updateGate();
  try { runOpts = await api("/api/run-options"); fillRunForm(); }
  catch (x) { $("run-error").textContent = "Could not load the form: " + x.message; }
  updateGate();
  await refreshRuns();
})();
