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
  $("r-selloff").checked = d.selloff; $("r-scorecard").checked = d.scorecard; $("r-open").checked = d.open_report !== false;
  $("r-style").innerHTML = o.styles.map(s => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join("");
  $("r-style").value = d.style || "standard"; updateStyleHint();
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

function updateStyleHint() { const x = runOpts.styles.find(s => s.id === $("r-style").value); $("h-style").textContent = x ? x.help : ""; }

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
  else if (setupReady === false) { msg = "NOT READY: something marked REQUIRED needs fixing first. Open Setup and settings at the top of the page, fix the items marked NEEDS SETUP or NOT WORKING, then come back."; ok = false; }
  else if (runOpts.provider.key_env && !runOpts.provider.key_set) { msg = "NOT READY: no key is saved for " + provName() + ". Open Setup and settings at the top of the page, paste the key and save."; ok = false; }
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
      selloff: $("r-selloff").checked, scorecard: $("r-scorecard").checked, open_report: $("r-open").checked, style: $("r-style").value});
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
const styleLabel = id => ((runOpts && runOpts.styles.find(s => s.id === id)) || {label: id}).label;

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
  if (j.style && j.style !== "standard") parts.push(`<p><span class="tag">LENS</span> This analysis used the <strong>${esc(styleLabel(j.style))}</strong> investing style: it changes how the same facts are weighed, and it has not been tested against outcomes.</p>`);
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
  const name = esc(j.ticker + "_" + j.date), id = esc(j.id);
  parts.push(`<div class="row">` +
    (r.report_dir ? `<button type="button" data-open-report="${id}">Open the HTML report</button>` : "") +
    `<button type="button" class="secondary" data-save="csv" data-id="${id}" data-name="${name}">Save as CSV</button>` +
    (r.report_dir ? `<button type="button" class="secondary" data-save="pdf" data-id="${id}" data-name="${name}">Save as PDF</button>` : "") + `</div>`);
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
  const meta = `${depth || "Depth " + j.depth} research, ${plural(j.passes, "pass", "passes")}, analysts: ${labels || j.analysts.join(", ")}, language: ${j.language}, holdings: ${j.holdings || "none"}.` + (j.style && j.style !== "standard" ? ` Investing style: ${styleLabel(j.style)}.` : "");
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
  if (ACTIVE.indexOf(j.status) < 0) {          // finished, failed or stopped: it can be taken off the list (its saved report stays)
    body += `<div class="row"><button type="button" class="secondary" data-remove="${esc(j.id)}" data-name="${esc(j.ticker)}" aria-label="Remove ${esc(j.ticker)} for ${esc(j.date)} from the list">Remove from the list</button></div>`;
  }
  const notes = (j.notes || []).map(n => `<p class="hint">${esc(n)}</p>`).join("");
  const slot = j.status === "waiting" ? "" : `<div class="live-slot" data-slot="${esc(j.id)}"></div>`;     // the live view lives here
  return `<li class="job job-${j.status}" data-job="${esc(j.id)}" data-sig="${esc(sig(j))}"><div class="job-head"><h3>${esc(j.ticker)} <span class="muted">for ${esc(j.date)}</span></h3>` +
    `<div class="badge job-${j.status}"><span aria-hidden="true">${st[0]}</span><span>${st[1]}</span></div></div><p class="hint">${esc(meta)}</p>${notes}${body}${slot}</li>`;
}

function syncList(ul, items) {
  const ids = items.map(j => j.id).join(",");
  if (ul.dataset.ids !== ids) { ul.innerHTML = items.map(jobItem).join(""); ul.dataset.ids = ids; return; }
  items.forEach(j => {
    const li = ul.querySelector('[data-job="' + j.id + '"]'); if (!li) return;
    if (li.dataset.sig !== sig(j)) {
      const slot = li.querySelector(".live-slot");                 // keep the live view's own page state through a redraw
      li.outerHTML = jobItem(j);
      const fresh = ul.querySelector('[data-job="' + j.id + '"]'), made = fresh && fresh.querySelector(".live-slot");
      if (slot && made) made.replaceWith(slot);
      return;
    }
    const e = li.querySelector("[data-elapsed]"); if (e) e.textContent = elapsedText(j);
    const l = li.querySelector("[data-latest]"); if (l) l.textContent = latestText(j);
  });
}

function renderRuns() {
  const first = jobs.slice(0, SHOWN), rest = jobs.slice(SHOWN);
  $("runs-empty").hidden = jobs.length > 0;
  $("jobs-tools").hidden = !jobs.some(j => ACTIVE.indexOf(j.status) < 0);
  $("save-all-csv").hidden = !jobs.some(j => j.status === "done");
  syncList($("runs"), first);
  $("runs-old").hidden = rest.length === 0;
  $("runs-old-label").textContent = "Earlier analyses (" + rest.length + ")";
  syncList($("runs-older"), rest);
  jobs.forEach(ensureLive);
}

// ---------------------------------------------------------------- the live view
// What each agent says and asks for, as it happens, and each agent's report once filed. Fed by /api/runs/{id}/live.
const liveState = {};
// The switch above the list. Off: the live views are hidden and nothing is fetched for them; the steps and result stay.
const liveOn = () => document.documentElement.dataset.live !== "off";
function setLive(on) {
  document.documentElement.dataset.live = on ? "on" : "off";
  $("live-toggle").checked = on;
  pref("ac-live", on ? "on" : "off");
}
const STATE_MARK = {waiting: ["○", "WAITING", "job-waiting"], working: ["▶", "WORKING", "job-running"], done: ["✓", "DONE", "job-done"], missing: ["–", "NO REPORT", "job-stopped"]};
const EVENT_TAG = {thought: "THOUGHT", request: "REQUESTED", data: "RECEIVED", system: "NOTE"};
const isOpen = (key, fallback) => (key in keepOpen ? keepOpen[key] : fallback);
const slotOf = id => document.querySelector('.live-slot[data-slot="' + id + '"]');

function eventHtml(e) {
  let text = e.text || "";
  if (e.kind === "data") text = (e.size ? Number(e.size).toLocaleString() + " characters. " : "") + text.replace(/\s+/g, " ").slice(0, 240) + (text.length > 240 ? "..." : "");
  return `<li class="ev ev-${esc(e.kind)}"><span class="ev-tag">${EVENT_TAG[e.kind] || "NOTE"}</span> <span class="ev-time">${esc(e.t || "")}</span> <span class="ev-text">${esc(text)}</span></li>`;
}

function agentIdFor(st, source) {
  const s = String(source || "").toLowerCase(), a = st.agents.find(x => x.title.toLowerCase() === s);
  return a ? a.id : "team";
}

function repHtml(jobId, a, sec) {
  const key = "report:" + jobId + ":" + a.id;
  return `<details class="repd" data-keep="${esc(key)}"${isOpen(key, false) ? " open" : ""}><summary><span class="btn-like secondary">${a.group === "analysts" ? "Final report" : "What it said"} (${Number((sec.text || "").length).toLocaleString()} characters)</span></summary>` +
    `<div class="reptext" tabindex="0" role="region" aria-label="${esc(a.title)}: the text">${esc(sec.text || "")}</div></details>`;
}

function agentHtml(j, a, st) {
  const m = STATE_MARK[a.state] || STATE_MARK.waiting, key = "agent:" + j.id + ":" + a.id, sec = st.sections[a.id];
  const evs = st.events.filter(e => agentIdFor(st, e.source) === a.id);
  return `<li class="agent" data-agent="${esc(a.id)}"><details data-keep="${esc(key)}"${isOpen(key, a.state === "working") ? " open" : ""}>` +
    `<summary><span class="badge small ${m[2]}" data-astate data-state="${a.state}"><span aria-hidden="true">${m[0]}</span> ${m[1]}</span> <strong>${esc(a.title)}</strong> ` +
    `<span class="muted" data-acount>${evs.length ? "(" + plural(evs.length, "event", "events") + ")" : ""}</span></summary>` +
    `<ol class="feed" tabindex="0" role="region" aria-label="${esc(a.title)}: what it said and asked for" data-feed="${esc(a.id)}"${evs.length ? "" : " hidden"}>${evs.map(eventHtml).join("")}</ol>` +
    `<div data-repslot="${esc(a.id)}" data-rev="${sec ? esc(sec.rev) : ""}">${sec ? repHtml(j.id, a, sec) : ""}</div></details></li>`;
}

function placeholderHtml(j, open, message) {
  return `<details class="live" data-keep="viewer:${esc(j.id)}"${open ? " open" : ""}><summary><span class="btn-like secondary">${j.status === "running" ? "Watch the analysts live" : "See what the analysts said"}</span></summary>` +
    `<p class="hint">${esc(message || "Loading the live view...")}</p></details>`;
}

function drawLive(j, slot) {
  const st = liveState[j.id];
  const hadFocus = slot.contains(document.activeElement);
  if (!st || !st.agents.length) { slot.innerHTML = placeholderHtml(j, isOpen("viewer:" + j.id, j.status === "running"), "Nothing was recorded for this analysis."); return; }
  const groups = [];
  st.agents.forEach(a => {
    let g = groups.find(x => x.id === a.group);
    if (!g) { g = {id: a.group, title: a.group_title, items: []}; groups.push(g); }
    g.items.push(a);
  });
  const team = st.events.filter(e => agentIdFor(st, e.source) === "team");
  const key = "viewer:" + j.id, teamKey = "agent:" + j.id + ":team";
  slot.innerHTML = `<details class="live" data-keep="${esc(key)}" data-full="1"${isOpen(key, j.status === "running") ? " open" : ""}>` +
    `<summary><span class="btn-like secondary">${j.status === "running" ? "Watch the analysts live" : "See what the analysts said"}</span></summary>` +
    `<p class="hint">Each agent's own words and the data it asks for, as they happen. The AI models' private reasoning is not available from the providers, so it is not shown.` +
    (st.passes > 1 ? ` The research panel runs ${st.passes} debates side by side; once it finishes, the first debate is shown here and the vote is in the result.` : "") + `</p>` +
    groups.map(g => `<h4>${esc(g.title)}</h4><ul class="agents">${g.items.map(a => agentHtml(j, a, st)).join("")}</ul>`).join("") +
    `<ul class="agents"><li class="agent" data-agent="team"><details data-keep="${esc(teamKey)}"${isOpen(teamKey, false) ? " open" : ""}><summary><strong>Notes from the run</strong> <span class="muted" data-acount>${team.length ? "(" + plural(team.length, "event", "events") + ")" : ""}</span></summary>` +
    `<ol class="feed" tabindex="0" role="region" aria-label="Notes from the run" data-feed="team"${team.length ? "" : " hidden"}>${team.map(eventHtml).join("")}</ol></details></li></ul></details>`;
  st.rendered = st.events.length;
  slot.querySelectorAll("ol.feed").forEach(ol => { ol.scrollTop = ol.scrollHeight; });       // start at the newest line
  if (hadFocus) { const s = slot.querySelector("summary"); if (s) s.focus(); }
}

function updateLive(id) {
  const slot = slotOf(id), st = liveState[id], j = jobs.find(x => x.id === id);
  if (!slot || !st || !j) return;
  if (!slot.querySelector("[data-full]")) { drawLive(j, slot); return; }
  const fresh = st.events.slice(st.rendered);
  st.rendered = st.events.length;
  fresh.forEach(e => {
    const ol = slot.querySelector('[data-feed="' + agentIdFor(st, e.source) + '"]'); if (!ol) return;
    const stick = ol.scrollTop + ol.clientHeight >= ol.scrollHeight - 10;      // keep following only if the reader is at the end
    ol.hidden = false; ol.insertAdjacentHTML("beforeend", eventHtml(e));
    if (stick) ol.scrollTop = ol.scrollHeight;
  });
  st.agents.concat([{id: "team", title: "team"}]).forEach(a => {
    const li = slot.querySelector('[data-agent="' + a.id + '"]'); if (!li) return;
    const n = st.events.filter(e => agentIdFor(st, e.source) === a.id).length, c = li.querySelector("[data-acount]");
    if (c) c.textContent = n ? "(" + plural(n, "event", "events") + ")" : "";
    const b = li.querySelector("[data-astate]");
    if (b && b.dataset.state !== a.state) { const m = STATE_MARK[a.state] || STATE_MARK.waiting; b.className = "badge small " + m[2]; b.dataset.state = a.state; b.innerHTML = `<span aria-hidden="true">${m[0]}</span> ${m[1]}`; }
    const sec = st.sections[a.id], rs = li.querySelector("[data-repslot]");
    if (sec && rs && rs.dataset.rev !== sec.rev) { rs.dataset.rev = sec.rev; rs.innerHTML = repHtml(id, a, sec); }
  });
}

async function fetchLive(id) {
  const st = liveState[id] || (liveState[id] = {next: 0, events: [], sections: {}, agents: [], passes: 1, rendered: 0, busy: false, final: false});
  if (st.busy) return;
  st.busy = true;
  try {
    const have = Object.keys(st.sections).map(k => k + ":" + st.sections[k].rev).join(",");
    const d = await api("/api/runs/" + encodeURIComponent(id) + "/live?after=" + st.next + "&have=" + encodeURIComponent(have));
    if (d.next) st.next = d.next;
    st.agents = d.agents; st.passes = d.passes;
    d.events.forEach(e => st.events.push(e));
    d.sections.forEach(s => { st.sections[s.id] = {rev: s.rev, text: s.text !== null ? s.text : (st.sections[s.id] || {}).text}; });
  } finally { st.busy = false; }
}

async function loadLive(id) {
  try { await fetchLive(id); }
  catch (x) { const s = slotOf(id); if (s) s.innerHTML = `<p class="problem">Could not load the live view: ${esc(x.message)}</p>`; return; }
  updateLive(id);
}

function ensureLive(j) {
  if (j.status === "waiting" || !liveOn()) return;
  const slot = slotOf(j.id); if (!slot || slot.firstElementChild) return;
  const st = liveState[j.id], open = isOpen("viewer:" + j.id, j.status === "running");
  if (st && st.agents.length) { drawLive(j, slot); return; }
  slot.innerHTML = placeholderHtml(j, open);
  if (open) loadLive(j.id);
}

async function liveTick() {
  if (!liveOn()) return;
  const work = jobs.filter(j => j.status !== "waiting" && liveState[j.id] &&
    (j.status === "running" ? isOpen("viewer:" + j.id, true) : !liveState[j.id].final));
  await Promise.all(work.map(async j => {
    try { await fetchLive(j.id); } catch (_) { return; }
    updateLive(j.id);
    if (j.status !== "running") liveState[j.id].final = true;          // one last look after it ends, then stop asking
  }));
}

// ---------------------------------------------------------------- the report and saving files
function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

async function openReport(id) {
  const out = $("export-result"), err = $("export-error");
  err.textContent = ""; out.textContent = "";
  try {
    const r = await api("/api/runs/" + encodeURIComponent(id) + "/open-report", {});
    out.textContent = r.opened ? "The report is opening in your browser." : "The report could not be opened from here. Open complete_report.html in the folder shown on the result.";
  } catch (x) { err.textContent = "Could not open it: " + x.message; }
  out.scrollIntoView({block: "nearest"});
}

// Choosing where to save: browsers that can (Chrome, Edge) show a Save As window right away, then the file is made and
// written there. The window must open first, straight from the click, because making a PDF can take longer than a
// browser allows between a click and that window. Other browsers get an ordinary download.
// ``source`` lets another panel use the same flow: {path, body, out, err} (the request, and the ids of its two message lines).
async function saveFile(kind, ids, baseName, source) {
  const src = source || {path: "/api/export", body: {format: kind, ids}, out: "export-result", err: "export-error"};
  const out = $(src.out), err = $(src.err);
  err.textContent = ""; out.textContent = "";
  const ext = kind === "pdf" ? ".pdf" : ".csv", mime = kind === "pdf" ? "application/pdf" : "text/csv", suggested = baseName.replace(/\.(csv|pdf)$/, "") + ext;
  let handle = null;
  if (typeof window.showSaveFilePicker === "function") {
    try {
      handle = await window.showSaveFilePicker({suggestedName: suggested, types: [{description: kind === "pdf" ? "PDF document" : "CSV spreadsheet", accept: {[mime]: [ext]}}]});
    } catch (e) {
      if (e && e.name === "AbortError") { out.textContent = "Nothing was saved: you closed the Save As window."; return; }
      handle = null;                                                  // the window could not open: use a normal download
    }
  }
  out.textContent = kind === "pdf" ? "Making the PDF. This takes a few seconds..." : "Preparing the file...";
  try {
    const blob = await apiBlob(src.path, src.body);
    if (handle) {
      const w = await handle.createWritable(); await w.write(blob); await w.close();
      out.textContent = "Saved as " + handle.name + " in the folder you chose.";
    } else {
      download(blob, suggested);
      out.textContent = "Downloaded " + suggested + ". Your browser decides the folder; to choose it each time, turn on \"Ask where to save each file\" in its download settings.";
    }
  } catch (x) {
    out.textContent = "";
    err.textContent = "Not saved: " + x.message;
    if (handle && typeof handle.remove === "function") { try { await handle.remove(); } catch (_) { /* leave the empty file */ } }
  }
  out.scrollIntoView({block: "nearest"});
}

function announce() {
  const say = [];
  jobs.forEach(j => {
    const was = lastStatus[j.id];
    if (was !== undefined && was !== j.status) {
      if (j.status === "running") say.push(j.ticker + " has started.");
      else if (j.status === "done") {
        say.push(j.ticker + " is done: " + (j.result && !j.result.review ? "rating " + j.result.rating + "." : "no readable rating."));
        showReportsWhere();                                  // the first report makes the folder
      }
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
    Object.keys(liveState).forEach(id => { if (!jobs.some(j => j.id === id)) delete liveState[id]; });     // one taken off the list
    announce(); renderRuns();
    await liveTick();
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

// The shortcut under the promotion in the left column: opens the folder that holds every saved report, and says where it is.
async function showReportsWhere() {
  try {
    const f = await api("/api/reports-folder");
    $("reports-where").textContent = (f.exists ? "Every saved report is in this folder: " : "Saved reports will go in this folder: ") + f.path;
  } catch (_) { $("reports-where").textContent = ""; }
}

async function openReports(btn) {
  $("reports-result").textContent = "";
  btn.disabled = true;
  try {
    const f = await api("/api/reports-folder/open", {});
    $("reports-result").textContent = f.opened
      ? "Opened the reports folder. Each analysis has its own folder inside it; open complete_report.html there."
      : (f.exists ? "Could not open it from here. Open this folder yourself: " + f.path
                  : "There are no saved reports yet. Run an analysis, and its report will be saved in " + f.path);
  } catch (x) { $("reports-result").textContent = "Could not open the reports folder: " + x.message; }
  btn.disabled = false;
  showReportsWhere();
}

// Take one analysis, or every finished one, off the list. Only the list and its small record are removed:
// saved reports, the run logs under the results folder and the decision memory stay where they are.
async function removeFromList(btn) {
  const all = !btn.dataset.remove, finished = jobs.filter(j => ACTIVE.indexOf(j.status) < 0);
  if (all && !finished.length) return;
  const ask = all
    ? "Clear " + plural(finished.length, "analysis", "analyses") + " from this list? Your saved reports are not deleted, and anything waiting or running stays."
    : "Remove the " + btn.dataset.name + " analysis from this list? Its saved report is not deleted.";
  if (!window.confirm(ask)) return;
  $("list-result").textContent = ""; $("list-error").textContent = "";
  btn.disabled = true;
  try {
    const out = all ? await api("/api/runs/clear-finished", {}) : await api("/api/runs/" + encodeURIComponent(btn.dataset.remove) + "/remove", {});
    const said = all ? "Cleared " + plural(out.removed, "analysis", "analyses") + " from the list. Your saved reports are still on your computer."
                     : "Removed " + btn.dataset.name + " from the list. Its saved report is still on your computer.";
    $("list-result").textContent = said; $("run-announce").textContent = said;
  } catch (x) { $("list-error").textContent = "Could not remove it: " + x.message; }
  btn.disabled = false;
  await refreshRuns();
  const heading = $("jobs-h"); heading.tabIndex = -1; heading.focus();          // the button that was pressed is gone
}

// ---------------------------------------------------------------- events
document.addEventListener("ac-checks", e => { setupReady = e.detail.ready; setupError = !!e.detail.error; updateGate(); });
document.addEventListener("toggle", e => {
  const k = e.target.dataset && e.target.dataset.keep;
  if (!k) return;
  keepOpen[k] = e.target.open;
  if (k.indexOf("viewer:") === 0 && e.target.open && !e.target.dataset.full) loadLive(k.slice(7));      // opened a finished analysis's view
}, true);
document.addEventListener("click", e => {
  const t = e.target.closest("button"); if (!t) return;
  if (t.id === "go-config") { $("configure").open = true; $("configure").scrollIntoView({block: "start"}); $("configure").querySelector("summary").focus(); }
  if (t.dataset.stop) stopJob(t);
  if (t.dataset.remove || t.id === "clear-finished") removeFromList(t);
  if (t.id === "open-reports") openReports(t);
  if (t.dataset.openReport) openReport(t.dataset.openReport);
  if (t.dataset.save) saveFile(t.dataset.save, [t.dataset.id], t.dataset.name);
  if (t.id === "save-all-csv") saveFile("csv", jobs.filter(j => j.status === "done").map(j => j.id), "analyses_" + (runOpts ? runOpts.today : "all"));
});
$("live-toggle").addEventListener("change", e => { setLive(e.target.checked); if (e.target.checked) { jobs.forEach(ensureLive); refreshRuns(); } });
$("run-form").addEventListener("submit", submitRun);
$("r-tickers").addEventListener("input", () => { updateParsed(); updateEstimate(); });
$("r-date").addEventListener("input", updateDateNote);
$("r-depth").addEventListener("change", () => { updateDepthHint(); updateEstimate(); });
$("r-passes").addEventListener("change", updateEstimate);
$("r-holdings").addEventListener("change", updateHoldingsHint);
$("r-style").addEventListener("change", updateStyleHint);
$("r-language").addEventListener("change", () => { updateLanguage(); if (!$("r-language-other").hidden) $("r-language-other").focus(); });

(async function initRuns() {
  setLive(pref("ac-live") !== "off");                       // remembered between visits; on unless it was turned off
  updateGate();
  showReportsWhere();
  try { runOpts = await api("/api/run-options"); fillRunForm(); }
  catch (x) { $("run-error").textContent = "Could not load the form: " + x.message; }
  updateGate();
  await refreshRuns();
})();
