"use strict";
// The Find stocks panel: a free search (no AI) that checks stocks against the analysis's five deal conditions, in code.
// It uses api(), esc() and $() from app.js and saveFile(), dur() and runOpts from runs.js, and talks only to this server.
const COND_BADGE = {PASS: ["ok", "✓", "PASS"], FAIL: ["not_working", "✕", "FAIL"], UNKNOWN: ["needs_setup", "?", "UNKNOWN"]};
const PICK_TOP = 5;                                // how many "Select the top" ticks
let findOpts = null, findJob = null, findTimer = null, findBusy = false, findFails = 0, findData = null, findFresh = false;

const shownValue = (s, stored) => Number((stored * s.scale).toPrecision(12));
const storedValue = (s, shown) => Number((shown / s.scale).toPrecision(12));
const money = v => {
  if (v === null || v === undefined) return "n/a";
  const sizes = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]], a = Math.abs(v), hit = sizes.find(x => a >= x[0]);
  return (v < 0 ? "-" : "") + (hit ? (a / hit[0]).toFixed(1) + hit[1] : a.toFixed(0));
};
const cap = s => (s ? String(s).charAt(0).toUpperCase() + String(s).slice(1) : "");
const recipeNow = () => { const r = document.querySelector('input[name="f-recipe"]:checked'); return r ? r.value : "beaten-down"; };

// ---------------------------------------------------------------- the form
function fillFind() {
  $("f-recipes").innerHTML = findOpts.recipes.map((r, i) =>
    `<label class="inline"><input type="radio" name="f-recipe" value="${esc(r.id)}"${i === 0 ? " checked" : ""}><span><strong>${esc(r.label)}</strong><br><span class="hint">${esc(r.help)}</span></span></label>`).join("");
  $("f-settings").innerHTML = findOpts.settings.map(s => {
    const shown = shownValue(s, s.default);
    return `<div class="field" data-setting="${esc(s.name)}"><label for="fs-${esc(s.name)}">${esc(s.label)} (${esc(s.unit)})</label>` +
      `<input id="fs-${esc(s.name)}" type="number" inputmode="decimal" step="any" min="${s.min}" max="${s.max}" value="${shown}" aria-describedby="hs-${esc(s.name)}">` +
      `<p class="hint" id="hs-${esc(s.name)}">${esc(s.help)} The usual number is ${shown}.</p></div>`;
  }).join("");
  $("f-record-text").textContent = "Record the stocks shown in my picks log" + (findOpts.ledger_path ? " (" + findOpts.ledger_path + ")" : "") +
    ", so that later I can see how this way of looking did.";
  updateRecipe();
}

function updateRecipe() {
  const recipe = recipeNow();
  $("f-list-block").hidden = recipe !== "list";
  findOpts.settings.forEach(s => { document.querySelector(`[data-setting="${s.name}"]`).hidden = s["for"].indexOf(recipe) < 0; });
  $("f-start").textContent = recipe === "list" ? "Check these stocks" : "Find stocks";
}

function updateFindGate() { $("f-start").disabled = !findOpts || findBusy; }

function resetSettings() {
  findOpts.settings.forEach(s => { $("fs-" + s.name).value = shownValue(s, s.default); });
}

// The settings that differ from the usual ones, in the picker's own units. Throws {message, field} for a bad entry.
function collectSettings(recipe) {
  const out = {};
  for (const s of findOpts.settings) {
    if (s["for"].indexOf(recipe) < 0) continue;
    const el = $("fs-" + s.name), raw = el.value.trim(), n = Number(raw), bad = text => { throw {message: s.label + ": " + text, field: el}; };
    if (raw === "" || !isFinite(n)) bad("type a number.");
    if (s.integer && !Number.isInteger(n)) bad("type a whole number.");
    if (n < s.min || n > s.max) bad("type a number from " + s.min + " to " + s.max + ".");
    if (n !== shownValue(s, s.default)) out[s.name] = storedValue(s, n);
  }
  return out;
}

async function submitFind(e) {
  e.preventDefault();
  const err = $("f-error"); err.textContent = "";
  const recipe = recipeNow(), text = $("f-text").value;
  let settings;
  try { settings = collectSettings(recipe); } catch (x) { err.textContent = x.message; if (x.field) x.field.focus(); return; }
  if (recipe === "list" && !text.trim()) { err.textContent = "Type at least one stock symbol, or load a file."; $("f-text").focus(); return; }
  findBusy = true; findFresh = true; updateFindGate();
  $("f-results").hidden = true; $("f-results").innerHTML = "";
  try {
    const job = await api("/api/picker/run", {recipe, text: recipe === "list" ? text : "", settings, record: $("f-record").checked});
    findJob = job.id; findFails = 0;
    $("f-progress").hidden = false; $("f-progress-h").textContent = "Searching";
    drawProgress(job);
    findTimer = setTimeout(pollFind, 1000);
  } catch (x) {
    err.textContent = "Not started: " + x.message; findBusy = false; updateFindGate();
  }
}

async function loadListFile(e) {
  const file = e.target.files && e.target.files[0], info = $("f-fileinfo"), err = $("f-error");
  err.textContent = ""; info.textContent = "";
  if (!file) return;
  if (file.size > 1000000) { err.textContent = file.name + " is larger than 1 MB, which is too big for a list of stocks."; e.target.value = ""; return; }
  try {
    const text = await file.text();
    $("f-text").value = text;
    info.textContent = "Loaded " + file.name + " (" + plural(text.split(/\r?\n/).filter(l => l.trim()).length, "row", "rows") + ") into the box above. Press Check these stocks.";
  } catch (_) { err.textContent = "Could not read " + file.name + "."; }
  e.target.value = "";                                   // so choosing the same file again still counts
}

// ---------------------------------------------------------------- progress
function drawProgress(j) {
  $("f-status").textContent = (j.recipe === "list" ? "Checking your stocks" : "Searching the market") + "... " + dur(j.elapsed || 0) + " so far. This costs nothing.";
  $("f-log").innerHTML = j.messages.slice(-8).map(m => `<li>${esc(m)}</li>`).join("");
}

function finishFind() { findBusy = false; updateFindGate(); }

async function pollFind() {
  clearTimeout(findTimer);
  let j;
  try { j = await api("/api/picker/jobs/" + encodeURIComponent(findJob)); findFails = 0; }
  catch (x) {
    if (++findFails >= 5 || /not known/.test(x.message)) { finishFind(); $("f-error").textContent = "Lost track of the search: " + x.message; return; }
    findTimer = setTimeout(pollFind, 1500); return;
  }
  drawProgress(j);
  if (j.state === "running") { findBusy = true; updateFindGate(); findTimer = setTimeout(pollFind, 1000); return; }
  finishFind();
  if (j.state === "failed") {
    $("f-progress").hidden = false; $("f-progress-h").textContent = "Stopped";
    $("f-error").textContent = "The search did not finish: " + j.error;
    $("f-announce").textContent = "The search did not finish.";
    return;
  }
  $("f-progress").hidden = true;
  drawResults(j);
  document.dispatchEvent(new Event("ac-search-done"));              // the scoreboard re-reads the picks log
}

// ---------------------------------------------------------------- the results
const metBadge = n => (n >= 4 ? ["ok", "✓"] : n === 3 ? ["needs_setup", "!"] : ["off", "○"]);

function flagsOf(r) {
  const f = [];
  if (r.sector_driven === "yes") f.push(["SECTOR-DRIVEN", "The " + (r.sector_fund || "sector") + " fund fell with it, so this looks more like a sector selloff than a market panic."]);
  if (r.low_pe_check === "yes") f.push(["LOW P/E: CHECK", "A forward P/E of " + r.forward_pe + "x is so low that the market probably expects earnings to fall (for example at the top of a cycle), so the valuation check may flatter this stock."]);
  if (r.note) f.push(["DATA TROUBLE", r.note]);
  return f;
}

function pickCard(r, titles) {
  const [cls, mark] = metBadge(r.conditions_met), flags = flagsOf(r);
  const place = [r.sector, r.industry].filter(Boolean).join(" / ");
  const pe = r.forward_pe ? r.forward_pe + "x" : "n/a";
  const change = x => (x === null || x === undefined ? "n/a" : (x > 0 ? "+" : "") + x + "%");
  const facts = [
    ["Below its 52-week high", r.below_high_pct === null || r.below_high_pct === undefined ? "n/a" : r.below_high_pct + "%"],
    ["The market explains (63 days / 21 days)", r.explains_63d_text + " / " + r.explains_21d_text],
    ["Price change (63 days / 21 days)", change(r.move_63d_pct) + " / " + change(r.move_21d_pct)],
    ["Forward P/E", pe + (r.peers_used ? ", compared with " + r.peers_used : "")],
    ["Price", r.price === null || r.price === undefined ? "n/a" : r.price],
    ["Market value", money(r.market_cap)],
  ];
  const conds = "abcde".split("").map(c => {
    const st = r[c + "_status"], b = COND_BADGE[st] || COND_BADGE.UNKNOWN;
    return `<li class="cond"><span class="badge small ${b[0]}"><span aria-hidden="true">${b[1]}</span><span>${b[2]}</span></span><span><strong>${esc(titles[c] || c)}.</strong> ${esc(cap(r[c + "_detail"]))}</span></li>`;
  }).join("");
  return `<li class="pick"><div class="pick-head">
    <label class="inline"><input type="checkbox" class="pick-sel" value="${esc(r.symbol)}"><span><span class="rank">${r.rank}.</span> <strong class="sym">${esc(r.symbol)}</strong> ${esc(r.name)}</span></label>
    <span class="badge small ${cls}"><span aria-hidden="true">${mark}</span><span>${r.conditions_met} of 5 conditions met</span></span></div>
    ${place ? `<p class="muted">${esc(place)}</p>` : ""}
    <dl class="facts">${facts.map(f => `<div><dt>${esc(f[0])}</dt><dd>${esc(f[1])}</dd></div>`).join("")}</dl>
    ${flags.map(f => `<p class="note"><strong>${esc(f[0])}:</strong> ${esc(f[1])}</p>`).join("")}
    <details class="advanced"><summary><span class="btn-like secondary">The five conditions</span></summary><ul class="conds">${conds}</ul></details></li>`;
}

function drawResults(j) {
  const r = j.result, rows = r.rows, titles = r.condition_titles || {}, box = $("f-results");
  const shown = rows.slice(0, r.top || rows.length), m = r.market;
  findData = {id: j.id, result: r};
  const name = r.recipe === "list" ? "Your stocks" : "Beaten-down quality";
  const market = "The S&P 500 is " + (-m.drawdown * 100).toFixed(1) + "% below its high (" + m.label + ")" +
    (m.vix !== null && m.vix_rank !== null ? "; the VIX is " + m.vix.toFixed(1) + ", higher than on " + Math.round(m.vix_rank * 100) + "% of days in the past year" : "") + ".";
  let html = `<h3 id="f-results-h" tabindex="-1">Results: ${esc(name)}</h3>
    <p class="hint">Prices as of ${esc(r.as_of || "n/a")}. ${esc(market)}</p>
    <h4>How many stocks at each step</h4>
    <ol class="funnel">${r.funnel.map(f => `<li><span class="n">${f.count}</span><span>${esc(f.step)}</span></li>`).join("")}</ol>`;
  const notes = (r.warnings || []).concat(j.skipped && j.skipped.length ? ["Not usable as symbols, so skipped: " + j.skipped.slice(0, 12).join(", ") + (j.skipped.length > 12 ? " and more" : "") + "."] : []);
  html += notes.map(n => `<p class="note">${esc(n)}</p>`).join("");
  if (!rows.length) {
    html += `<p class="note">No stocks to show. Loosen a setting under More settings (for example, lower how far below the high a stock must be) and try again.</p>`;
  } else {
    html += `<h4>${shown.length < rows.length ? "The top " + shown.length + " of " + rows.length : plural(shown.length, "stock", "stocks")}</h4>
      <p class="hint">The most conditions met come first; the score only breaks ties. Tick the ones to look at more closely. This is a quick check in code: it does not read filings, news or the debate.</p>
      <div class="row">
        <button type="button" class="secondary" id="f-top">Tick the top ${Math.min(PICK_TOP, shown.length)}</button>
        <button type="button" class="secondary" id="f-clear">Clear the ticks</button>
        <button type="button" id="f-send" disabled>Send the ticked stocks to the run form</button>
        <button type="button" class="secondary" id="f-save">Save all ${rows.length} as a CSV file</button>
      </div>
      <p class="result" id="f-sendmsg" role="status" aria-live="polite"></p>
      <p class="result" id="f-save-result" role="status" aria-live="polite"></p>
      <p class="error" id="f-save-error" role="alert"></p>`;
    if (j.recorded) html += `<p class="hint">Recorded ${plural(j.recorded, "stock", "stocks")} in ${esc(j.ledger_path || "your picks log")}.</p>`;
    html += `<ol class="picks">${shown.map(x => pickCard(x, titles)).join("")}</ol>`;
  }
  box.innerHTML = html; box.hidden = false;
  $("f-announce").textContent = rows.length ? "Done. " + plural(shown.length, "stock", "stocks") + " found, shown below." : "Done. No stocks to show.";
  if (findFresh) { findFresh = false; const h = $("f-results-h"); h.scrollIntoView({block: "start"}); h.focus({preventScroll: true}); }
}

// ---------------------------------------------------------------- ticks and sending
const ticked = () => Array.from(document.querySelectorAll("#f-results .pick-sel:checked")).map(i => i.value);

function updateTicks() {
  const send = $("f-send"); if (!send) return;
  const n = ticked().length;
  send.disabled = n === 0;
  send.textContent = n ? "Send the " + n + " ticked to the run form" : "Send the ticked stocks to the run form";
}

function tickTop() {
  const max = Math.min(PICK_TOP, typeof runOpts !== "undefined" && runOpts ? runOpts.max_tickers : PICK_TOP);
  document.querySelectorAll("#f-results .pick-sel").forEach((box, i) => { box.checked = i < max; });
  updateTicks();
}

function sendTicked() {
  const picked = ticked(), msg = $("f-sendmsg");
  if (!picked.length) return;
  const max = typeof runOpts !== "undefined" && runOpts ? runOpts.max_tickers : 10;
  if (picked.length > max) { msg.textContent = "You ticked " + picked.length + " stocks, but the run form takes up to " + max + " at a time. Untick some."; return; }
  const box = $("r-tickers"), before = box.value.trim();
  box.value = picked.join(" ");
  box.dispatchEvent(new Event("input", {bubbles: true}));            // the run form updates its count and cost
  const text = "Sent " + picked.join(", ") + " to Run an analysis" + (before ? ", replacing " + before : "") + ". Check the options there, then press Start analysis.";
  msg.textContent = text; $("f-announce").textContent = text;
  $("run").scrollIntoView({block: "start"}); box.focus();
}

function saveFind() {
  if (!findData) return;
  saveFile("csv", null, "stocks_" + (findData.result.as_of || "list"), {path: "/api/picker/export", body: {id: findData.id}, out: "f-save-result", err: "f-save-error"});
}

// ---------------------------------------------------------------- events
$("find-form").addEventListener("submit", submitFind);
$("find-form").addEventListener("change", e => { if (e.target.name === "f-recipe") updateRecipe(); });
$("f-reset").addEventListener("click", resetSettings);
$("f-file").addEventListener("change", loadListFile);
$("f-results").addEventListener("change", e => { if (e.target.classList.contains("pick-sel")) updateTicks(); });
$("f-results").addEventListener("click", e => {
  const t = e.target.closest("button"); if (!t) return;
  if (t.id === "f-top") tickTop();
  if (t.id === "f-clear") { document.querySelectorAll("#f-results .pick-sel").forEach(b => { b.checked = false; }); updateTicks(); }
  if (t.id === "f-send") sendTicked();
  if (t.id === "f-save") saveFind();
});

(async function initFind() {
  updateFindGate();
  try { findOpts = await api("/api/picker/options"); fillFind(); }
  catch (x) { $("f-error").textContent = "Could not load this panel: " + x.message; return; }
  updateFindGate();
  if (findOpts.latest) { findJob = findOpts.latest; findBusy = findOpts.busy; updateFindGate(); $("f-progress").hidden = !findOpts.busy; await pollFind(); }   // the last search, after a reload
})();
