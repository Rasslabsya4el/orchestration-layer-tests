// Дополнение tab (#340 unit 8): the add-on's read `addon:fast` (the repo's own collector `fast`; the add-on's sections
// are merged into that payload). Plain ES module; render(snapshot, ui) is pure and returns an HTML string.
// The cards: the remote host, the runs (running with their live builds, queued, recent, history, lanes, daemon), the
// readings and the status line. A repo without the add-on gets one line; a repo with no collector gets the error
// text only. `esc` comes from shell.js; the classes are the page's own (style.css): .tbl .tr .th .lb .r .mu .bar
// .banner .tblw .w .sq .sqs .er .ok.
import { esc } from "./shell.js";

export const id = "addon";
export const title = "Дополнение";
export const order = 60;
export const endpoints = ["addon:fast"];

// The keys only the add-on's sections carry: a payload with none of them is a repo without the add-on.
const ADDON_KEYS = ["runs", "remote", "readings"];
// The host card is named by the payload (`remote.title`); a payload without a title gets this word.
const HOST_TITLE = "Add-on";
const NO_ADDON = "у этого репо нет дополнения";
// The history is a long list: the first rows of it are drawn, the count of all of them is said.
const HISTORY_SHOWN = 20;
const LANE_WORD = { run: "прогон", agents: "агенты", you: "резерв", free: "свободно" };
const KIND_WORD = { benchmark: "benchmark", replay: "replay", tests: "тесты", request: "билд по запросу" };
const RUN_WORD = { done: "готово", failed: "упал", stopped: "остановлен", cancelled: "отменён" };
const RUN_HEADS = ["задача", "имя", "тикет", "хост", "слоты", "прогресс", "осталось", "вид"];
const QUEUE_HEADS = ["задача", "имя", "тикет", "хост", "слоты", "приоритет", "ожидание", "возраст"];
const DONE_HEADS = ["задача", "имя", "вид", "итог", "сборки", "дата", "длительность"];

const arr = v => (Array.isArray(v) ? v : []);
const num = v => (v == null || !Number.isFinite(Number(v)) ? "—" : String(Math.round(Number(v) * 10) / 10));
const text = v => (v == null || v === "" ? "—" : esc(v));
const ruForm = (n, one, few, many) => {
  const a = Math.abs(Number(n)) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
};

// Share of cap in percent, 0 to 100; 0 when the cap is missing or not positive.
function pct(used, cap) {
  const u = Number(used), c = Number(cap);
  if (!Number.isFinite(u) || !Number.isFinite(c) || c <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((u / c) * 100)));
}

function bar(p, cls) {
  return `<div class="bar"><i class="${cls}" style="width:${p}%"></i></div>`;
}

function errorCard(what, msg) {
  return `<div class="banner" data-error="${esc(what)}">Нет данных: ${esc(what)} — ${esc(msg)}</div>`;
}

// A duration for a human reader: seconds under 90, minutes under 90 min, then hours and minutes (a machine's age_s).
function humanSec(s) {
  const n = Number(s);
  if (!Number.isFinite(n)) return "—";
  if (n < 90) return `${Math.round(n)} s`;
  if (n < 5400) return `${Math.round(n / 60)} min`;
  return `${Math.floor(n / 3600)} h ${Math.round((n % 3600) / 60)} min`;
}

// `2026-10-19T09:33:09+00:00` -> `2026-10-19 09:33`
function stamp(v) {
  return v == null ? "—" : String(v).replace("T", " ").slice(0, 16);
}

// The run kind as the collector words it: a known kind's word, else the collector's own name of it.
const kindOf = j => KIND_WORD[j.what] ?? (j.kind_name || "");

// The load of one host (its CPU and RAM): the total bars, and the split by owner.
function hostLoad(m) {
  const ramUsed = Number(m.ram_total) - Number(m.ram_free);
  const cpuSplit = Object.entries(m.cpu || {}).map(([k, v]) => `${esc(k)} ${num(v)}`).join(" · ");
  return `<div class="lb"><span>CPU ${num(m.cpu_total)} %</span></div>` + bar(pct(m.cpu_total, 100), "k-p") +
    (cpuSplit ? `<div class="mu">${cpuSplit}</div>` : "") +
    `<div class="lb"><span>RAM ${num(ramUsed)} из ${num(m.ram_total)} GB</span></div>` + bar(pct(ramUsed, m.ram_total), "k-p");
}

// The lane squares: one per lane of the daemon (a run, the agents' locks, the reserve, the free ones).
function lanesOf(runs) {
  const d = runs.daemon && typeof runs.daemon === "object" ? runs.daemon : {};
  const lanes = arr(runs.lanes);
  const avail = d.lanes_total != null ? Number(d.lanes_total) : Number(runs.total) || 0;
  const reserved = Number(d.reserved) || 0;
  const inUse = Math.max(Number(d.lanes_in_use) || 0, lanes.length);
  const busy = [...lanes].sort((a, b) => (a.role === "queue" ? 0 : 1) - (b.role === "queue" ? 0 : 1));
  const n = avail + reserved;
  const marks = Array.from({ length: n }, (_, i) =>
    i >= n - reserved ? "you" : i < inUse ? (busy[i] && busy[i].role !== "queue" ? "agents" : "run") : "free");
  const caption = `${inUse} / ${avail}` + (runs.queue_until ? ` · до ${runs.queue_until}` : "");
  return { marks, caption };
}

// One host's squares with their «in use / total» caption; `part` names the card's part (data-part).
function squares(s, part) {
  const sq = s.marks.map(k => `<span class="sq" data-lane="${k}" title="${esc(LANE_WORD[k])}">${k === "free" ? "□" : "■"}</span>`).join("");
  return `<div class="lb"><b>Слоты</b><span class="r">${esc(s.caption)}</span></div>` +
    `<div class="sqs" data-part="${part}">${sq}</div>`;
}

function table(heads, rows) {
  return `<div class="tblw"><div class="tbl"><div class="tr th">` +
    heads.map(h => `<span>${esc(h)}</span>`).join("") + `</div>${rows.join("")}</div></div>`;
}

function row(kind, cells, attrs = "") {
  return `<div class="tr" data-row="${kind}"${attrs}>` + cells.map(c => `<span>${text(c)}</span>`).join("") + `</div>`;
}

const staleText = j => `нет связи с ноутом, данные от ${j.stale_since ?? "?"}`;
const STAGE_ID = /\bP\d-#(\d+)(?:-\d+)?/g;

function progressOf(j) {
  const p = j.progress;
  if (!p || typeof p !== "object") return "";
  return `${num(p.done)}/${num(p.total)}` + (p.eta ? ` ~${p.eta}` : "");
}

// A running run: its row, then one row per build that is running now (progress.cells, drawn in the order the collector gives).
function runRows(j, nowS) {
  const p = j.progress && typeof j.progress === "object" ? j.progress : null;
  const left = j.stale ? staleText(j) : j.left_h ? `ещё ~${j.left_h}` : "оценки нет";
  const attrs = j.stale ? ' data-stale="1"' : "";
  const main = row("running", [j.tag, j.name, j.issue != null ? `#${j.issue}` : null, j.host, j.locks ?? j.workers,
    progressOf(j), left, kindOf(j)], attrs);
  const live = p ? arr(p.cells).filter(c => c.state === "running") : [];
  return [main, ...live.map(c => buildRow(c, nowS))];
}

// One running build: the place of its step («этап N / M»), its status word, the time since its start.
function buildRow(c, nowS) {
  const stage = c.started && c.stage_index && c.stage_count ? `этап ${c.stage_index} / ${c.stage_count}` : "";
  const elapsed = nowS != null && c.started ? humanSec(nowS - Number(c.started)) : "";
  return row("build", ["↳", c.name, c.class ?? c.asc, "", "", stage, c.status || c.stage || "", elapsed]);
}

// A queued run's reason to wait: the failed predecessor it holds on, the run it waits for, else the daemon's words.
function waitText(j) {
  if (j.held_by) return `держит упавший ${String(j.held_by.tag).replace(STAGE_ID, "#$1")}`;
  const w = j.wait;
  const what = j.waits_for ? `ждёт ${j.waits_for.tag}`
    : !w || w.kind === "lanes" ? "ждёт слот"
      : w.kind === "ram" ? "ждёт RAM"
        : String(w.text).replace(STAGE_ID, "#$1");
  const left = !w || w.kind === "lanes" || w.kind === "after" ? ((j.start_s ?? 0) >= 60 && j.start_h ? ` · ~${j.start_h}` : "") : "";
  return what + left;
}

function runsCard(runs, runsError, nowS) {
  if (!runs || typeof runs !== "object") return runsError ? errorCard("прогоны", runsError) : "";
  const running = arr(runs.running), queued = arr(runs.queued), recent = arr(runs.recent), history = arr(runs.history);
  const endLine = runs.queue_end_h ? ` · конец через ${runs.queue_end_h}` : "";
  const sub = `очередь: ${running.length} идёт, ${queued.length} ждёт${endLine}`;
  const parts = [];
  if (runsError) parts.push(errorCard("прогоны", runsError));
  parts.push(squares(lanesOf(runs), "lanes"));
  if (Number(runs.waiting) > 0) parts.push(`<div>${num(runs.waiting)} ${ruForm(runs.waiting, "ждёт", "ждут", "ждут")} слота</div>`);
  parts.push(`<div class="lb"><b>Идут</b><span class="r">${running.length}</span></div>`);
  if (running.length) parts.push(table(RUN_HEADS, running.flatMap(j => runRows(j, nowS))));
  parts.push(`<div class="lb"><b>В очереди</b><span class="r">${queued.length}</span></div>`);
  if (queued.length) {
    parts.push(table(QUEUE_HEADS, queued.map(j => row("queued", [j.tag, j.name, j.issue != null ? `#${j.issue}` : null,
      j.host, j.workers, j.priority, j.stale ? staleText(j) : waitText(j), j.age]))));
  }
  parts.push(`<div class="lb"><b>Недавние</b><span class="r">${recent.length}</span></div>`);
  if (recent.length) parts.push(table(DONE_HEADS, recent.map(j => doneRow("recent", j, j.took ?? j.dur))));
  const shown = history.slice(0, HISTORY_SHOWN);
  parts.push(`<div class="lb"><b>История</b><span class="r">${history.length}${history.length > shown.length ? `, первые ${shown.length}` : ""}</span></div>`);
  if (shown.length) parts.push(table(DONE_HEADS, shown.map(j => doneRow("history", j, j.dur))));
  const d = runs.daemon && typeof runs.daemon === "object" ? runs.daemon : null;
  if (d) {
    parts.push(`<section data-part="daemon"><div class="lb"><b>Демон</b><span class="r">${text(d.host)} · pid ${num(d.pid)}</span></div>` +
      `<div class="mu">слоты ${num(d.lanes_in_use)} из ${num(d.lanes_total)}, резерв ${num(d.reserved)}` +
      `${d.reserved_until ? ` до ${esc(d.reserved_until)}` : ""} · ${humanSec(d.age_s)} назад</div></section>`);
  }
  return `<section class="w" data-part="runs"><div class="lb"><b>Прогоны</b><span class="r">${esc(sub)}</span></div>` +
    parts.join("") + `</section>`;
}

// A finished run: its kind, its result word, its builds done of all, its start and its length.
function doneRow(kind, j, length) {
  const builds = j.rows_total ? `${num(j.rows_done ?? 0)}/${num(j.rows_total)}` : "";
  const when = j.date ? `${j.date} ${j.start ?? ""}`.trim() : j.start;
  return row(kind, [j.tag, j.name, kindOf(j) || j.kind_name, RUN_WORD[j.state] ?? j.state, builds, when, length]);
}

function remoteCard(lap) {
  if (!lap || typeof lap !== "object") return "";
  const m = lap.machine, o = lap.offline, r = lap.runs;
  const state = m ? "на связи" : o ? "offline" : "нет данных";
  const stateCls = m ? "ok" : o ? "er" : "mu";
  const cores = m?.cores ?? o?.cores;
  const ram = m?.ram_total != null ? `${Math.round(m.ram_total)} GB` : "";
  const sub = [cores != null ? `${cores} ядер` : "", ram].filter(Boolean).join(", ");
  const lines = [];
  if (m && typeof m === "object") lines.push(hostLoad(m));
  if (o && typeof o === "object") {
    // the time of the last sight when the link was lost, else the time of the last reading
    const since = o.lost ?? o.seen;
    const ago = o.lost ? o.lost_ago : o.seen_ago;
    lines.push(`<div class="mu">${since ? `нет связи с ${esc(since)}${ago ? ` · ${esc(ago)}` : ""}` : "нет связи"}</div>`);
    if (o.verdict) lines.push(`<div class="mu">${esc(o.verdict)}</div>`);
    for (const j of arr(o.running)) {
      const lanes = j.workers ? ` · ${num(j.workers)} ${ruForm(j.workers, "слот", "слота", "слотов")}` : "";
      lines.push(`<div class="mu" data-row="was">шло: ${text(j.tag)}${lanes}${j.total != null ? ` · ${num(j.done)}/${num(j.total)}` : ""}</div>`);
    }
  }
  if (m && r && Number(r.waiting) > 0) lines.push(`<div>${num(r.waiting)} ${ruForm(r.waiting, "ждёт", "ждут", "ждут")} слота</div>`);
  // the lane squares: the host's own when it answers; offline, its lane count with none in use
  let lanes = "";
  if (m && r) lanes = squares(lanesOf(r), "remote-lanes");
  else if (o && o.lanes_total != null) lanes = squares(lanesOf({ total: o.lanes_total, lanes: [], daemon: {} }), "remote-lanes");
  if (lap.error) lines.push(`<div class="mu" data-error="remote">${esc(lap.error)}</div>`);
  return `<section class="w" data-part="remote"><div class="lb"><b>${esc(lap.title || HOST_TITLE)}</b><span class="r ${stateCls}">${state}</span></div>` +
    (sub ? `<div class="mu">${esc(sub)}</div>` : "") + lanes + lines.join("") + `</section>`;
}

function readingRow(r) {
  const name = [r.name ?? r.run_name ?? r.ref, r.var].filter(v => v != null && v !== "").join(" · ");
  const value = r.ratio_pct != null ? `${num(r.ratio_pct)} %`
    : r.ours != null ? [r.metric_label, r.ours_h ?? r.ours].filter(v => v != null && v !== "").join(" ") : "—";
  const cls = [r.class, r.asc].filter(v => v != null && v !== "").join(" · ");
  const errors = r.errors === 0 ? `<span class="ok">ok</span>`
    : r.errors ? `<span class="er">${num(r.errors)} ${ruForm(r.errors, "ошибка", "ошибки", "ошибок")}</span>` : "";
  const bits = [
    `<span class="c-ti">${text(name)}</span>`,
    `<span class="mu">${text(cls)}</span>`,
    `<span>${esc(value)}</span>`,
    r.life != null ? `<span>Life ${num(r.life)}</span>` : "",
    errors,
    r.warnings ? `<span class="er">${num(r.warnings)} ${ruForm(r.warnings, "предупреждение", "предупреждения", "предупреждений")}</span>` : "",
    r.floors_bad ? `<span class="er">ниже порога</span>` : "",
  ].join("");
  // the open build's facts: its rank, its stop and what the reader said about it (a [bad, text] pair each)
  const facts = [r.rank != null ? `ранг ${num(r.rank)}` : "", r.stop ? `стоп: ${esc(r.stop)}` : ""].filter(Boolean).join(" · ");
  const said = arr(r.said).map(s => {
    const [bad, t] = Array.isArray(s) ? s : [false, s];
    return `<div class="${bad ? "er" : "mu"}">${esc(t)}</div>`;
  }).join("");
  return `<div class="tr" data-row="reading">${bits}${facts ? `<div class="mu">${facts}</div>` : ""}${said}` +
    `${r.note ? `<div class="mu">${esc(r.note)}</div>` : ""}</div>`;
}

function readingsCard(rd) {
  if (!rd || typeof rd !== "object") return "";
  if (rd.error) return `<section class="w" data-part="readings">${errorCard("билды на чтение", rd.error)}</section>`;
  const rows = arr(rd.rows);
  const body = rows.length ? rows.map(readingRow).join("") : `<div class="mu">очередь пуста</div>`;
  // the runs the readings come from, each once, in the order of the rows
  const names = [...new Set(rows.map(r => r.run_name).filter(v => v != null && v !== ""))];
  return `<section class="w" data-part="readings"><div class="lb"><b>Билды на чтение</b>` +
    `<span class="r">${rows.length} ${ruForm(rows.length, "билд", "билда", "билдов")}</span></div>` +
    `<div class="mu">обновлено ${esc(stamp(rd.updated))}${names.length ? ` · ${names.map(n => esc(n)).join(" · ")}` : ""}</div>${body}</section>`;
}

export function render(snapshot, ui) {
  const env = (snapshot && snapshot[endpoints[0]]) || {};
  const data = env.data;
  const envelopeError = env.error ? errorCard("дополнение", env.error) : "";
  // no data: the error text is the whole answer (a repo with no collector says «monitor not installed in …»)
  if (!data || typeof data !== "object") return envelopeError || errorCard("дополнение", "нет данных");
  // data without the add-on's keys: the collector answered, and this repo has no add-on
  if (!ADDON_KEYS.some(k => k in data)) {
    return envelopeError + `<section class="w"><div class="lb">${NO_ADDON}</div></section>`;
  }
  const nowS = Number.isFinite(Number(env.at_ms)) ? Number(env.at_ms) / 1000 : null;
  const status = typeof data.status === "string" && data.status
    ? `<div class="lb" data-part="status"><b>${esc(data.status)}</b></div>` : "";
  return envelopeError + status + remoteCard(data.remote) + runsCard(data.runs, data.runs_error, nowS) + readingsCard(data.readings);
}
