// Главная, row 3 (PC panel): CPU and RAM by work / agents / app / tests / other / unmeasured, the merge's share, and the
// 10-minute CPU history drawn from samples the panel keeps in ui.state.cpuHistory (the payload carries no history and no
// per-core data, so the per-core squares of the pane are not drawn). Names and colours follow the pane's card (kit.tsx RNAME, BAR_COL).
// Each bar has its own legend under it, as the pane's: the CPU's in percent, the RAM's in GB; a RAM part under 0.5 % of the
// bar is visually zero and is left out of its legend (the pane skips a part under 0.5 px).
// Accepted gap: the history is collected while this tab is drawn (the samples come from this panel's renders), so a stretch
// of time with another tab shown is not in it. The per-core squares are the add-on's, not this tab's.
import { esc } from "./shell.js";

const KEYS = ["work", "agents", "app", "tests", "you", "unmeasured", "merge"];
const LABEL = { work: "work", agents: "agents", app: "Claude app", tests: "tests", you: "other", unmeasured: "unmeasured", merge: "merge" };
const COLOUR = { work: "#3987e5", agents: "#d55181", app: "#9ad147", tests: "#2cbaa2", you: "#c9c9cf", unmeasured: "#636366", merge: "#d65cbe" };
const HIST_MS = 10 * 60 * 1000;
const HIST_COLS = 60;
const RAM_LEGEND_MIN_PCT = 0.5;

const num = v => (Number.isFinite(Number(v)) ? Number(v) : 0);
// One decimal at most, no trailing zero: 8.5, 34.7, 0 (the pane's own figures).
const tenths = v => String(Math.round(num(v) * 10) / 10);

// The sample of one pull: the six CPU sources the collector splits, plus the merge's own share.
function sampleOf(m) {
  const c = m.cpu ?? {};
  return {
    work: num(c.work), agents: num(c.agents), app: num(c.app), tests: num(c.tests),
    you: num(c.you), unmeasured: num(c.unmeasured), merge: num(m.merge?.cpu),
  };
}

// Keep one sample per distinct at_ms, drop those older than the window. A render without ui.state keeps nothing.
function keepHistory(ui, at_ms, sample) {
  const state = ui && ui.state ? ui.state : {};
  let list = Array.isArray(state.cpuHistory) ? state.cpuHistory : [];
  if (at_ms != null) {
    const last = list[list.length - 1];
    if (!last || last.at_ms !== at_ms) list = [...list, { at_ms, ...sample }];
    const cut = at_ms - HIST_MS;
    list = list.filter(s => s.at_ms >= cut);
  }
  state.cpuHistory = list;
  return list;
}

// The history as columns (at most HIST_COLS of them: consecutive samples averaged), each stacked from its CPU parts.
// The merge is a seventh part, drawn only when a column holds a merge share (no merge in the window: six parts, as the pane).
function historyChart(list) {
  if (!list.length) return "";
  const per = Math.max(1, Math.ceil(list.length / HIST_COLS));
  const cols = [];
  for (let i = 0; i < list.length; i += per) {
    const part = list.slice(i, i + per);
    const avg = {};
    for (const k of KEYS) avg[k] = part.reduce((a, s) => a + num(s[k]), 0) / part.length;
    cols.push(avg);
  }
  const stacked = cols.some(a => a.merge > 0) ? KEYS : KEYS.filter(k => k !== "merge");
  const body = cols.map(a => {
    const total = stacked.reduce((n, k) => n + a[k], 0);
    const segs = stacked.map(k => `<i data-hist="${k}" style="flex-grow:${a[k]};background:${COLOUR[k]}"></i>`).join("");
    return `<span style="flex:1 1 0;min-width:0;height:${Math.min(100, total)}%;display:flex;flex-direction:column-reverse">${segs}</span>`;
  }).join("");
  return `<div class="hist" role="img" aria-label="CPU за 10 мин" style="display:flex;align-items:flex-end;gap:1px;height:56px;border-bottom:1px solid var(--line)">${body}</div>`;
}

// One stacked bar: each source a segment, its width its share of `whole`.
function stackBar(prefix, parts, whole) {
  const segs = KEYS.map(k => {
    const pct = whole > 0 ? Math.max(0, parts[k]) / whole * 100 : 0;
    return `<i data-seg="${prefix}-${k}" style="width:${pct.toFixed(2)}%;background:${COLOUR[k]}" title="${esc(LABEL[k])}: ${tenths(parts[k])}"></i>`;
  }).join("");
  return `<div class="bar">${segs}</div>`;
}

const swatch = k => `<i style="display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px;background:${COLOUR[k]}"></i>`;

// The CPU's legend under its bar: every source in percent, as the pane's CPU line.
function cpuLegend(sample) {
  const items = KEYS.map(k => `<span>${swatch(k)}${esc(LABEL[k])} ${tenths(sample[k])} %</span>`).join("");
  return `<div class="mkey" data-legend="cpu">${items}</div>`;
}

// The RAM's legend under its bar: each visible part in GB (`v.toFixed(1) GB`); a part under RAM_LEGEND_MIN_PCT of the bar
// is visually zero and left out. Nothing is drawn when no part is shown.
function ramLegend(parts, whole) {
  const share = k => (whole > 0 ? Math.max(0, parts[k]) / whole * 100 : 0);
  const items = KEYS.filter(k => share(k) >= RAM_LEGEND_MIN_PCT)
    .map(k => `<span>${swatch(k)}${esc(LABEL[k])} ${Math.max(0, parts[k]).toFixed(1)} GB</span>`).join("");
  return items ? `<div class="mkey" data-legend="ram">${items}</div>` : "";
}

export function pcPanel(snapshot, ui) {
  const fast = snapshot?.fast ?? {};
  const d = fast.data ?? {};
  const m = d.machine;
  const errors = [];
  if (d.machine_error) errors.push(d.machine_error);
  else if (!m && fast.error) errors.push(fast.error);
  const errLines = errors.map(e => `<div style="color:var(--bad);overflow-wrap:anywhere;font-size:12px">${esc(e)}</div>`).join("");

  if (!m) {
    const note = errors.length ? "" : `<div class="none">${fast.data == null ? "Загрузка…" : "нет данных о машине"}</div>`;
    return `<section class="w" data-panel="pc" data-row="3"><div class="lb"><b>PC</b></div>${errLines}${note}</section>`;
  }

  const sample = sampleOf(m);
  const ramTotal = num(m.ram_total);
  const ramUsed = m.ram_free != null ? ramTotal - num(m.ram_free) : null;
  const ramText = ramUsed != null
    ? `${Math.round(ramUsed)} / ${Math.round(ramTotal)} GB`
    : `${Math.round(ramTotal)} GB`;
  const ramParts = { ...sample };
  for (const k of ["work", "agents", "app", "tests", "you", "unmeasured"]) ramParts[k] = num((m.ram ?? {})[k]);
  ramParts.merge = num(m.merge?.ram_mb) / 1024;

  const history = historyChart(keepHistory(ui, fast.at_ms, sample));
  const sub = [m.cores != null ? `${esc(m.cores)} ядер` : "", ramTotal ? `${Math.round(ramTotal)} GB` : ""].filter(Boolean).join(", ");

  return `<section class="w" data-panel="pc" data-row="3">`
    + `<div class="lb"><b>PC</b><span class="r">${sub}</span></div>`
    + errLines
    + `<div class="mrow"><span>CPU</span>${stackBar("cpu", sample, 100)}<span class="v">${Math.round(num(m.cpu_total))} %</span></div>`
    + cpuLegend(sample)
    + `<div class="mrow" style="grid-template-columns:3.2em minmax(0,1fr) auto"><span>RAM</span>${stackBar("ram", ramParts, ramTotal)}<span class="v">${ramText}</span></div>`
    + ramLegend(ramParts, ramTotal)
    + (history ? `<div class="none">CPU за 10 мин</div>${history}` : "")
    + `</section>`;
}
