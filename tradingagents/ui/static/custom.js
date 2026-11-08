"use strict";
// The form for the Custom search under Find stocks. The server describes every filter (its label, units, limits and starting value), so
// this script only draws them and reads them back. It uses api(), esc() and $() from app.js and plural() from runs.js, and talks only to
// this server. picker.js calls fillCustom() once the options have loaded and collectCustom() when a search starts.
let customSpec = null;

const cid = (id, side) => "c-" + id + (side ? "-" + side : "");
const critValue = (c, side) => $(cid(c.id, side)).value.trim();

function fieldHtml(c) {
  const sides = c.sides.map(side => {
    const label = side === "min" ? "At least" : "At most", start = side === "min" && c.default_min !== null ? c.default_min : "";
    return `<label class="bound" for="${cid(c.id, side)}">${label}<input id="${cid(c.id, side)}" type="number" step="any" inputmode="decimal" value="${start}" aria-describedby="ch-${esc(c.id)}"></label>`;
  }).join("");
  return `<fieldset class="crit"><legend>${esc(c.label)} <span class="muted">(${esc(c.unit)})</span></legend><div class="pair">${sides}</div><p class="hint" id="ch-${esc(c.id)}">${esc(c.help)}</p></fieldset>`;
}

function flagHtml(f) {
  return `<label class="inline"><input type="checkbox" id="${cid(f.id)}" data-flag="${esc(f.id)}" aria-describedby="ch-${esc(f.id)}"><span><strong>${esc(f.label)}</strong><br><span class="hint" id="ch-${esc(f.id)}">${esc(f.help)}</span></span></label>`;
}

function whichHtml(spec) {
  const sectors = spec.sectors.map((s, i) => `<label class="inline"><input type="checkbox" class="c-sector" id="c-sector-${i}" value="${esc(s)}"><span>${esc(s)}</span></label>`).join("");
  return `<fieldset class="crit"><legend>Sectors</legend><p class="hint">Tick one or more. None ticked means every sector.</p><div class="sector-grid">${sectors}</div></fieldset>
    <label class="inline"><input type="checkbox" id="c-otc"><span><strong>Include over-the-counter listings.</strong><br><span class="hint">By default only stocks on Nasdaq, NYSE and NYSE American are searched. Over-the-counter listings include foreign companies' shares that are thinly traded and often duplicate a listing you can already search.</span></span></label>
    <fieldset class="crit"><legend>Leave these out</legend><label class="bound" for="c-exclude">Stock symbols<input id="c-exclude" type="text" autocomplete="off" spellcheck="false" placeholder="TSLA X" aria-describedby="ch-exclude"></label><p class="hint" id="ch-exclude">Separated by spaces or commas. These are never shown.</p></fieldset>`;
}

function groupHtml(spec, g) {
  let body = g.id === "which" ? whichHtml(spec) : "";
  const crits = spec.criteria.filter(c => c.group === g.id), flags = spec.flags.filter(f => f.group === g.id);
  body += crits.map(fieldHtml).join("") + flags.map(flagHtml).join("");
  return `<details class="advanced custom-group" id="cg-${esc(g.id)}"><summary><span class="btn-like secondary">${esc(g.label)}</span><span class="hint-inline" id="cs-${esc(g.id)}"></span></summary><div class="grid">${body}</div></details>`;
}

function resultsOptionsHtml(spec) {
  const sorts = spec.sorts.map(s => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join("");
  const minc = ['<option value="">Any number</option>'].concat([1, 2, 3, 4, 5].map(n => `<option value="${n}">At least ${n} of 5</option>`)).join("");
  const lim = n => spec.limits[n];
  const limit = n => `<div class="field"><label for="c-${n}">${esc(lim(n).label)}</label><input id="c-${n}" type="number" step="1" inputmode="numeric" min="${lim(n).min}" max="${lim(n).max}" value="${lim(n).usual}" aria-describedby="ch-${n}"><p class="hint" id="ch-${n}">From ${lim(n).min} to ${lim(n).max}.</p></div>`;
  return `<details class="advanced custom-group" id="cg-results"><summary><span class="btn-like secondary">Order and how many</span><span class="hint-inline" id="cs-results"></span></summary><div class="grid">
    <div class="field"><label for="c-sort">Order the results</label><select id="c-sort">${sorts}</select><p class="hint">Also decides which stocks are checked in depth when more pass than the number below.</p></div>
    <div class="field"><label for="c-minc">Deal conditions to meet</label><select id="c-minc" aria-describedby="ch-minc">${minc}</select><p class="hint" id="ch-minc">The five deal conditions the analysis uses. Leave on "Any number" to see every stock that passes your filters.</p></div>
    ${limit("examine")}${limit("check")}${limit("show")}</div></details>`;
}

function fillCustom(spec) {
  customSpec = spec;
  $("c-groups").innerHTML = spec.groups.map(g => groupHtml(spec, g)).join("") + resultsOptionsHtml(spec);
  $("cg-which").open = true;
  updateCustomCounts();
}

// ---------------------------------------------------------------- reading the form back
function setCount(id, n, none) { $(id).textContent = n ? "(" + n + " set)" : "(" + none + ")"; }

function updateCustomCounts() {
  if (!customSpec) return;
  const spec = customSpec, per = {};
  spec.criteria.forEach(c => { if (c.sides.some(side => critValue(c, side) !== "")) per[c.group] = (per[c.group] || 0) + 1; });
  spec.flags.forEach(f => { if ($(cid(f.id)).checked) per[f.group] = (per[f.group] || 0) + 1; });
  per.which = (per.which || 0) + (document.querySelectorAll(".c-sector:checked").length ? 1 : 0) + ($("c-otc").checked ? 1 : 0) + ($("c-exclude").value.trim() ? 1 : 0);
  spec.groups.forEach(g => setCount("cs-" + g.id, per[g.id] || 0, "none set"));
  const extra = ($("c-minc").value ? 1 : 0) + ($("c-sort").value !== spec.sorts[0].id ? 1 : 0) +
    ["examine", "check", "show"].filter(n => +$("c-" + n).value !== spec.limits[n].usual).length;
  setCount("cs-results", extra, "usual");
}

// The filters as the server wants them, in the units shown. Throws {message, field} for a bad entry.
function collectCustom() {
  const spec = customSpec, ranges = {};
  const bad = (text, field) => { throw {message: text, field}; };
  spec.criteria.forEach(c => {
    const pair = {};
    c.sides.forEach(side => {
      const raw = critValue(c, side), el = $(cid(c.id, side));
      if (raw === "") return;
      const n = Number(raw);
      if (!isFinite(n)) bad(c.label + ": type a number, or leave it blank.", el);
      if (n < c.low || n > c.high) bad(c.label + ": type a number from " + c.low + " to " + c.high + ".", el);
      pair[side] = n;
    });
    if ("min" in pair && "max" in pair && pair.min > pair.max) bad(c.label + ": the smallest number (" + pair.min + ") is above the largest (" + pair.max + ").", $(cid(c.id, "min")));
    if (Object.keys(pair).length) ranges[c.id] = pair;
  });
  const out = {ranges, flags: spec.flags.filter(f => $(cid(f.id)).checked).map(f => f.id),
    sectors: Array.from(document.querySelectorAll(".c-sector:checked")).map(b => b.value), otc: $("c-otc").checked,
    exclude: $("c-exclude").value, min_conditions: $("c-minc").value ? +$("c-minc").value : null, sort: $("c-sort").value};
  ["examine", "check", "show"].forEach(n => {
    const el = $("c-" + n), l = spec.limits[n], raw = el.value.trim();
    if (raw === "") { out[n] = null; return; }
    const v = Number(raw);
    if (!Number.isInteger(v) || v < l.min || v > l.max) bad(l.label + ": type a whole number from " + l.min + " to " + l.max + ".", el);
    out[n] = v;
  });
  return out;
}

// ---------------------------------------------------------------- buttons
function clearFilters() {
  customSpec.criteria.forEach(c => c.sides.forEach(side => { $(cid(c.id, side)).value = ""; }));
  customSpec.flags.forEach(f => { $(cid(f.id)).checked = false; });
  document.querySelectorAll(".c-sector").forEach(b => { b.checked = false; });
  $("c-otc").checked = false; $("c-exclude").value = ""; $("c-minc").value = "";
  $("c-count-text").textContent = ""; updateCustomCounts();
}

function resetFilters() {
  clearFilters();
  customSpec.criteria.forEach(c => { if (c.default_min !== null) $(cid(c.id, "min")).value = c.default_min; });
  $("c-sort").value = customSpec.sorts[0].id;
  ["examine", "check", "show"].forEach(n => { $("c-" + n).value = customSpec.limits[n].usual; });
  updateCustomCounts();
}

async function countMatches() {
  const out = $("c-count-text"), err = $("f-error"); err.textContent = "";
  let custom;
  try { custom = collectCustom(); } catch (x) { err.textContent = x.message; if (x.field) x.field.focus(); return; }
  $("c-count").disabled = true; out.textContent = "Asking Yahoo...";
  try {
    const r = await api("/api/picker/count", {custom});
    out.textContent = "Yahoo says about " + plural(r.matches, "stock matches", "stocks match") + " these filters so far. The forward P/E, analyst rating, price and deal-condition filters are applied afterward, so the final number is smaller, and only the largest " +
      (custom.examine || customSpec.limits.examine.usual) + " are looked at.";
  } catch (x) {
    out.textContent = ""; err.textContent = "Could not count: " + x.message;
  } finally { $("c-count").disabled = false; }
}

document.addEventListener("click", e => {
  const t = e.target.closest("button"); if (!t || !customSpec) return;
  if (t.id === "c-count") countMatches();
  if (t.id === "c-clear") clearFilters();
  if (t.id === "c-reset") resetFilters();
});
$("c-groups").addEventListener("input", updateCustomCounts);
$("c-groups").addEventListener("change", updateCustomCounts);
