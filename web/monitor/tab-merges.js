// Page tab "Слияния" (#340 unit 7): where each merge's tests ran (the PC, or GitHub with its parts), the GitHub machines
// in use, and the free GitHub minutes of each repo against the cap. Plain ES module; render(snapshot, ui) is pure and
// returns an HTML string. Reads the `github` endpoint (tools/agentops/monitor_github.py collect()). `esc` comes from
// shell.js; the classes are the page's own (style.css): .tbl .tr .th .c-* .w .lb .r .bar .banner .tblw.
import { esc } from "./shell.js";

export const id = "merges";
export const title = "Слияния";
export const order = 50;
export const endpoints = ["github"];

const OUTCOME = {
  landed: { mark: "✓", word: "слито" },
  failed: { mark: "✗", word: "красный" },
  lost: { mark: "?", word: "нет результата" },
};

const num = v => (v == null || !Number.isFinite(Number(v)) ? "—" : String(Number(v)));

// Share of cap in percent, 0 to 100; 0 when the cap is missing or not positive.
function pct(used, cap) {
  const u = Number(used), c = Number(cap);
  if (!Number.isFinite(u) || !Number.isFinite(c) || c <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((u / c) * 100)));
}

function bar(p, cls) {
  return `<div class="bar"><i class="${cls}" style="width:${p}%"></i></div>`;
}

function errorCard(what, text) {
  return `<div class="banner" data-error="${esc(what)}">Нет данных: ${esc(what)} — ${esc(text)}</div>`;
}

function mergesPart(merges, err) {
  if (err) return errorCard("слияния", err);
  if (!Array.isArray(merges)) return errorCard("слияния", "нет данных");
  const head = `<div class="lb"><b>Слияния</b><span class="r">последние ${merges.length}</span></div>`;
  if (!merges.length) return `<section class="w" data-part="merges">${head}<div class="lb">Слияний в логе нет</div></section>`;
  const rows = merges.map(m => {
    const o = OUTCOME[m.outcome] ?? { mark: "?", word: String(m.outcome ?? "") };
    const issues = (Array.isArray(m.issues) ? m.issues : []).map(i => `#${i}`).join(" ");
    return `<div class="tr" data-row="merge" data-outcome="${esc(m.outcome)}">` +
      `<span class="c-st" title="${esc(o.word)}">${esc(o.mark)}</span>` +
      `<span class="c-ti" title="${esc(m.branch)}">${esc(m.branch)}</span>` +
      `<span class="c-ef">${esc(issues)}</span>` +
      `<span class="c-mo">${esc(m.at)}</span>` +
      `<span class="c-ef" data-place="${esc(m.place)}">${esc(m.place)}</span>` +
      `<span class="c-st">${esc(o.word)}</span></div>`;
  }).join("");
  const table = `<div class="tblw"><div class="tbl">` +
    `<div class="tr th"><span></span><span>ветка</span><span>задача</span><span>время</span><span>где прошли тесты</span><span>итог</span></div>` +
    `${rows}</div></div>`;
  return `<section class="w" data-part="merges">${head}${table}</section>`;
}

function machinesPart(machines, err) {
  if (err) return errorCard("машины GitHub", err);
  if (!machines || typeof machines !== "object") return errorCard("машины GitHub", "нет данных");
  const used = Number(machines.used), total = Number(machines.total);
  return `<section class="w" data-part="machines">` +
    `<div class="lb"><b>Машины GitHub</b><span class="r">${num(used)} из ${num(total)}</span></div>` +
    bar(pct(used, total), "k-p") +
    `</section>`;
}

function minutesPart(minutes, err) {
  if (err) return errorCard("минуты GitHub", err);
  if (!Array.isArray(minutes)) return errorCard("минуты GitHub", "нет данных");
  const rows = minutes.map(m => {
    const over = Number.isFinite(Number(m.used)) && Number.isFinite(Number(m.cap)) && Number(m.used) >= Number(m.cap);
    return `<div class="minutes" data-minutes="${esc(m.slug)}">` +
      `<div class="lb"><b>${esc(m.name)}</b><span class="r">${num(m.used)} из ${num(m.cap)} мин</span></div>` +
      bar(pct(m.used, m.cap), over ? "k-z" : "k-p") +
      `</div>`;
  }).join("");
  return `<section class="w" data-part="minutes"><div class="lb"><b>Минуты GitHub за месяц</b>` +
    `<span class="r">${minutes.length} репо</span></div>${rows}</section>`;
}

export function render(snapshot, ui) {
  const env = (snapshot && snapshot.github) || {};
  const data = env.data;
  const envelopeError = env.error ? errorCard("github", env.error) : "";
  if (!data || typeof data !== "object") return envelopeError + errorCard("github", "нет данных");
  return envelopeError +
    mergesPart(data.merges, data.merges_error) +
    machinesPart(data.machines, data.machines_error) +
    minutesPart(data.minutes, data.minutes_error);
}
