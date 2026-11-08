"use strict";
// The scoreboard under Find stocks: how the stocks recorded in the picks log did against the S&P 500. It uses api(), esc() and $()
// from app.js and saveFile() and plural() from runs.js, and talks only to this server.
let boardBusy = false, boardLog = null;
const SHOWN_PICKS = 60;                             // stocks listed one by one; the CSV file has them all

const bPct = x => (x > 0 ? "+" : "") + (x * 100).toFixed(1) + "%";

function describeLog(log) {
  if (!log || !log.stocks) {
    return "Nothing is recorded yet. A search is recorded when \"Record the stocks shown\" is ticked. Come back a few weeks after your first recorded search: " +
      "one week of prices says almost nothing.";
  }
  return "Your picks log holds " + plural(log.stocks, "stock", "stocks") + " from " + plural(log.lists, "list", "lists") + ", shown between " + log.first + " and " + log.last +
    ". This compares each stock's price when it was shown with its price on later days, and with the S&P 500 over the same days. It costs nothing: it only downloads prices.";
}

// One line about a group of stocks: how many, their average move, the market's, and ahead or behind in words as well as a symbol.
function statLine(label, s) {
  if (!s) return `<li><strong>${esc(label)}:</strong> nothing has reached this yet.</li>`;
  const ahead = s.avg_excess >= 0;
  return `<li><strong>${esc(label)}:</strong> ${plural(s.n, "stock", "stocks")}, ${bPct(s.avg_stock)} on average against ${bPct(s.avg_market)} for the S&amp;P 500: ` +
    `<strong><span aria-hidden="true">${ahead ? "▲" : "▼"}</span> ${Math.abs(s.avg_excess * 100).toFixed(1)} points ${ahead ? "ahead of" : "behind"} it</strong>; ${s.beat} of ${s.n} beat it.</li>`;
}

function horizonLines(stats, horizons, only) {
  const lines = [statLine("Today", stats.to_date)];
  horizons.forEach(h => { if (!only || only.indexOf(h) >= 0) lines.push(statLine("After " + h + " sessions", stats[String(h)])); });
  return lines.join("");
}

function moveText(m) {
  return m ? bPct(m.stock) + " against " + bPct(m.market) : "not yet";
}

function pickLine(p, horizons) {
  if (!p.to_date) return `<li><strong>${esc(p.symbol)}</strong> (${esc(p.label)}, shown ${esc(p.shown)}): ${esc(p.note || "nothing to compare yet")}.</li>`;
  const later = horizons.filter(h => p.horizons[String(h)]).map(h => "after " + h + " sessions " + moveText(p.horizons[String(h)]));
  return `<li><strong>${esc(p.symbol)}</strong> ${esc(p.name)} (${esc(p.label)}, shown ${esc(p.shown)}, ${p.met} of 5 conditions): today ${moveText(p.to_date)}${later.length ? "; " + later.map(esc).join("; ") : ""}.</li>`;
}

function drawBoard(d) {
  const labels = {};
  d.recipes.forEach(r => { labels[r.id] = r.label; });
  let html = `<h4 id="b-results-h" tabindex="-1">The answer</h4>
    <p class="verdict">${esc(d.verdict)}</p>
    <p class="hint">Prices through ${esc(d.as_of || "n/a")}. ${esc(d.yardstick)}</p>` + d.warnings.map(w => `<p class="note">${esc(w)}</p>`).join("");
  d.recipes.forEach(r => {
    html += `<h4>${esc(r.label)}: ${plural(r.stocks, "stock", "stocks")} in ${plural(r.lists, "list", "lists")}, ${esc(r.first)} to ${esc(r.last)}</h4>
      <ul class="outcomes">${horizonLines(r.stats, d.horizons)}</ul>`;
    if (r.buckets.length > 1) {
      html += `<details class="advanced"><summary><span class="btn-like secondary">By how many conditions were met</span></summary>` +
        r.buckets.map(b => `<h5>${esc(b.label)} (${plural(b.stocks, "stock", "stocks")})</h5><ul class="outcomes">${horizonLines(b.stats, d.horizons, [21])}</ul>`).join("") + `</details>`;
    }
  });
  if (d.batches.length) {
    html += `<h4>Each list, newest first</h4><ul class="outcomes">` + d.batches.map(b => {
      const s = b.stats.to_date, when = b.sessions === 1 ? "1 session ago" : b.sessions + " sessions ago";
      if (!s) return `<li><strong>${esc(b.shown)}</strong> ${esc(b.label)}, ${plural(b.recorded, "stock", "stocks")}, ${when}: not measured yet.</li>`;
      const ahead = s.avg_excess >= 0;
      return `<li><strong>${esc(b.shown)}</strong> ${esc(b.label)}, ${plural(b.recorded, "stock", "stocks")}, ${when}: ${bPct(s.avg_stock)} against ${bPct(s.avg_market)}, ` +
        `<span aria-hidden="true">${ahead ? "▲" : "▼"}</span> ${Math.abs(s.avg_excess * 100).toFixed(1)} points ${ahead ? "ahead" : "behind"}.</li>`;
    }).join("") + `</ul>`;
  }
  if (d.picks.length) {
    const picks = d.picks.map(p => Object.assign({label: labels[p.recipe] || p.recipe}, p))
      .sort((a, b) => b.shown.localeCompare(a.shown) || a.rank - b.rank).slice(0, SHOWN_PICKS);
    html += `<details class="advanced"><summary><span class="btn-like secondary">Every stock, newest list first</span></summary>
      ${d.picks.length > SHOWN_PICKS ? `<p class="hint">The newest ${SHOWN_PICKS} of ${d.picks.length}. The CSV file has all of them.</p>` : ""}
      <ul class="outcomes">${picks.map(p => pickLine(p, d.horizons)).join("")}</ul></details>`;
    html += `<div class="row"><button type="button" class="secondary" id="b-save">Save every stock as a CSV file</button></div>
      <p class="result" id="b-save-result" role="status" aria-live="polite"></p><p class="error" id="b-save-error" role="alert"></p>`;
  }
  const box = $("b-results");
  box.innerHTML = html; box.hidden = false;
  const h = $("b-results-h"); h.scrollIntoView({block: "start"}); h.focus({preventScroll: true});
}

async function runBoard() {
  if (boardBusy) return;
  boardBusy = true; $("b-run").disabled = true; $("b-error").textContent = "";
  $("b-status").textContent = "Downloading prices for the recorded stocks. This takes a few seconds and costs nothing...";
  try {
    const d = await api("/api/picker/scoreboard", {});
    $("b-status").textContent = "Done.";
    drawBoard(d);
  } catch (x) {
    $("b-status").textContent = "";
    $("b-error").textContent = "Could not work out the scoreboard: " + x.message;
  } finally {
    boardBusy = false; $("b-run").disabled = !(boardLog && boardLog.stocks);
  }
}

document.addEventListener("click", e => {
  const t = e.target.closest("button"); if (!t) return;
  if (t.id === "b-run") runBoard();
  if (t.id === "b-save") saveFile("csv", null, "picker_scoreboard", {path: "/api/picker/scoreboard/csv", body: {}, out: "b-save-result", err: "b-save-error"});
});

// The picks log is read when the page loads, and again after each search (which may have recorded stocks in it).
async function refreshLog() {
  try {
    boardLog = (await api("/api/picker/options")).log;
    $("b-hint").textContent = describeLog(boardLog);
    $("b-run").disabled = boardBusy || !boardLog.stocks;
  } catch (x) { $("b-hint").textContent = "Could not read your picks log: " + x.message; }
}
document.addEventListener("ac-search-done", refreshLog);
refreshLog();
