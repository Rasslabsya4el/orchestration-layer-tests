// Главная, row 2: the Merge panel, the pane's Merge card in the browser. `mergePanel(snapshot, ui) -> html`, pure.
// Reads snapshot.fast.data.merge (+ merge_error), snapshot.tickets.data.issues (titles and merge_lost, when the
// tickets read is there), and the reads' own error keys (fast.error, tickets.error), each shown as text in this panel.
// Lists are whole in the browser; only the landed branches are capped (LANDED_CAP) until the reader opens the rest.
import { esc } from "./shell.js";
import { dur, ru } from "./home-util.js";

const LANDED_CAP = 10;
const PHASE = { queue: "очередь", suite: "тесты", "re-check": "перепроверка", push: "пуш" };

// Why a branch did not land, by merge-status reason (the pane's table: one Russian text per reason).
// A reason not in it reads «не влит: <reason>».
const NOT_LANDED_WHY = new Map([
  ["conflict", () => "конфликт с main"],
  ["rebase-conflict", () => "конфликт при перестройке на main"],
  ["red-suite", f => (f == null ? "упали тесты" : `упали тесты: ${f}`)],
  ["red-on-base", () => "тесты красные уже на main"],
  ["target-check", () => "запрещённые слова проекта"],
  ["dirty", () => "незакоммиченные изменения"],
  ["nothing-to-merge", () => "нечего вливать"],
  ["setup-error", () => "ошибка запуска"],
  ["not-planned", () => "тикет закрыт как не планируемый"],
  ["worktree", () => "запуск не из основной копии"],
  ["tracked-caches", () => "в ветке закоммичены кэши"],
  ["red-on-branch", () => "тесты ветки красные"],
  ["shallow", () => "неполная история клона"],
  ["core-manifest", () => "правка файла ядра вне обновления"],
  ["merge-refused", () => "git отказал в слиянии"],
  ["suite-worktree", () => "тесты не подготовлены"],
  ["withdrawn", () => "снято"],
  ["superseded", () => "заменено новым коммитом"],
  ["stuck", () => "тесты зависли"],
  ["cut", () => "сорвано ради срочного тикета"],
  ["push-refused", () => "main не принял push"],
  ["diverged", () => "ветка разошлась с origin"],
  ["fetch-failed", () => "не скачан origin"],
  ["sync-failed", () => "main не синхронизирован"],
  ["lock-timeout", () => "очередь слияния занята"],
  ["failed", () => "слияние не прошло"],
  ["silent-holder", () => "слияние молчало, линию взяли"],
]);

// An ISO stamp's clock as HH:MM (a naive or offset stamp reads its own clock; a Z stamp reads local time).
const hm = iso => {
  if (!iso) return "";
  const s = String(iso);
  if (/[+-]\d\d:\d\d$/.test(s) || !/Z$/.test(s)) return s.slice(11, 16);
  const d = new Date(s);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
const num = v => (v == null ? "-" : Math.round(v).toLocaleString("en-US"));
const coreName = sha => (sha ? `обновление ядра ${sha}` : "обновление ядра");

// One row of a `.tbl` grid: dot, name, ticket, status, knob, time. Every text is escaped here.
// `attr` is an already-escaped attribute string (the landed rows carry data-landed).
const tableRow = (dot, name, tick, status, statusCls, knob, time, attr = "") =>
  `<div class="tr"${attr ? ` ${attr}` : ""}><span class="d ${esc(dot)}"></span><span class="c-ti">${esc(name)}</span>`
  + `<span class="c-mo">${esc(tick)}</span><span class="c-st${statusCls ? ` ${esc(statusCls)}` : ""}">${esc(status)}</span>`
  + `<span class="num">${esc(knob)}</span><span class="num">${esc(time)}</span></div>`;

// The progress track under a running row (the pane's track row): a bar filled to `pct` percent of the run, a knob at the
// fill's end. `pct` is tests done / total, or the read's own percent when it has no test counts.
const track = pct => {
  const p = Math.min(100, Math.max(0, Math.round(pct)));
  return `<div class="tr"><span></span><div class="bar" data-track="${p}" style="grid-column:2 / -1">`
    + `<i style="width:${p}%;background:var(--run)"></i><i style="width:2px;background:var(--txt)"></i></div></div>`;
};

const table = (knobHead, rows) =>
  `<div class="tblw"><div class="tbl"><div class="tr th"><span></span><span>задача</span><span>тикет</span><span>статус</span>`
  + `<span class="num">${esc(knobHead)}</span><span class="num">длительность</span></div>${rows.join("")}</div></div>`;

const wrap = (sub, inner) =>
  `<section class="w full" data-panel="merge" data-row="2"><div class="lb"><b>Merge</b>${sub}</div>${inner}</section>`;

export function mergePanel(snapshot, ui) {
  const fast = snapshot?.fast ?? {};
  const data = fast.data ?? {};
  const m = data.merge ?? null;
  const tickets = snapshot?.tickets ?? {};
  const issues = tickets.data?.issues && typeof tickets.data.issues === "object" ? tickets.data.issues : {};
  const titleOf = n => (n != null ? issues[String(n)]?.title : undefined);

  // A failed read shows its error text in this panel (the merge read, the fast read, the tickets read).
  const errorLines = [fast.error, data.merge_error, tickets.error]
    .filter(Boolean).map(e => `<p class="banner">${esc(e)}</p>`).join("");

  if (!m) return wrap("", errorLines || '<p class="none">данных о слиянии нет</p>');

  const running = m.running ?? null;
  const full = m.full_run ?? null;
  const waiting = m.waiting ?? [];
  const runningCount = (running ? 1 : 0) + (full ? 1 : 0);
  const queueEnd = m.queue_end_h ? ` · вся ~${ru(m.queue_end_h)}${m.queue_until ? `, до ${m.queue_until}` : ""}` : "";
  const sub = `<span>${esc(`очередь: ${runningCount} идёт, ${waiting.length} ждёт${queueEnd}`)}</span>`;

  // Идёт: the running holder (one row per ticket of its batch), then the full run on main.
  const runRows = [];
  if (running) {
    const isCommit = running.kind === "commit";
    const isCore = running.kind === "core-update";
    const batch = running.issues?.length && !isCommit ? running.issues : [null];
    const failed = (running.failed ?? 0) > 0;
    const phase = PHASE[running.phase] ?? running.phase ?? "";
    const eta = running.eta_h ? `, ещё ~${ru(running.eta_h)}` : "";
    const status = failed ? `упало ${running.failed}${eta}` : `${phase}${eta}`;
    const tests = running.tests_done != null && running.tests_total
      ? `${num(running.tests_done)} / ${num(running.tests_total)}` : `${running.percent ?? 0} %`;
    const knob = running.phase === "suite" ? tests : phase;
    const pct = running.tests_done != null && running.tests_total
      ? (running.tests_done / running.tests_total) * 100 : (running.percent ?? 0);
    for (const n of batch) {
      const name = isCommit ? (running.message || "commit")
        : isCore ? coreName(running.sha)
          : (titleOf(n) ?? ((running.branches ?? []).join(", ") || "?"));
      // a commit holder lists every issue of its commit (the pane's commitTick), not only the first
      const tick = isCommit ? (running.issues?.length ? running.issues.map(x => `#${x}`).join(" ") : "commit") : n != null ? `#${n}` : "";
      runRows.push(tableRow(failed ? "stuck" : "run", name, tick, status, failed ? "stuck" : "run", knob, ru(running.elapsed_h)));
      runRows.push(track(pct));
    }
  }
  if (full) {
    const fail = (full.failed ?? 0) > 0;
    const eta = full.eta_s != null ? `, ещё ~${dur(full.eta_s)}` : "";
    runRows.push(tableRow(fail ? "stuck" : "run", `full run on main ${full.commit ?? ""}`, "",
      fail ? `упало ${full.failed}${eta}` : `тесты${eta}`, fail ? "stuck" : "run",
      `${full.percent ?? 0} %`, full.elapsed_s != null ? dur(full.elapsed_s) : ""));
    runRows.push(track(full.percent ?? 0));
  }

  // В очереди: the waiting merges and core updates, then the lost merges (a parked merge the tickets read marks merge_lost).
  const waitRows = waiting.map(w => {
    const core = w.kind === "core-update";
    const n = w.issues?.[0] ?? null;
    const name = core ? coreName(w.sha) : (titleOf(n) ?? w.branch ?? "");
    const start = w.eta_start_h ? `старт через ~${ru(w.eta_start_h)}` : `${w.position}-й в очереди`;
    const takes = w.takes_h ? `, займёт ~${ru(w.takes_h)}` : "";
    return tableRow("wait", name, core ? "" : n != null ? `#${n}` : "", `${start}${takes}`, "", "", w.waited_h ? `ждёт ${ru(w.waited_h)}` : "");
  });
  const lostRows = Object.keys(issues).filter(k => issues[k]?.merge_lost === true).map(k => {
    const n = Number(k);
    return tableRow("stuck", titleOf(n) ?? "", `#${n}`, "merge lost", "stuck", "", "");
  });
  const waitAll = [...waitRows, ...lostRows];

  // Готово: one row per branch (a refused or retried landing of a branch is an attempt, not a row); capped until opened.
  const seen = new Map();
  for (const l of m.landed ?? []) {
    const g = seen.get(l.branch);
    if (g) g.attempts++;
    else seen.set(l.branch, { ...l, attempts: 1 });
  }
  const landed = [...seen.values()];
  const landedOpen = ui?.open?.("merge-landed") === true;
  const landedShown = landedOpen ? landed : landed.slice(0, LANDED_CAP);

  const notLanded = l => {
    const at = l.ended ? ` в ${hm(l.ended)}` : "";
    if (!l.reason) return `не влит${at}`;
    const why = NOT_LANDED_WHY.get(l.reason);
    return why ? `не влит${at} — ${why(l.failed)}` : `не влит: ${l.reason}`;
  };
  const landedRow = l => {
    const n = l.issues?.[0] ?? null;
    const open = n != null && issues[String(n)]?.open === true;
    // another branch of this ticket still runs or waits in the merge line: this landing is not the ticket's end
    const sibling = n != null && ((!!running?.issues?.includes(n) && (running.branches ?? []).some(b => b !== l.branch))
      || waiting.some(w => w.branch !== l.branch && !!w.issues?.includes(n)));
    const tries = l.attempts > 1 ? `, с ${l.attempts}-й попытки` : "";
    const status = l.rc ? notLanded(l)
      : sibling ? `влита ветка, ждёт вторая${tries}`
        : `влит${l.landed ? ` в ${hm(l.landed)}` : ""}${open ? ", тикет открыт" : ""}${tries}`;
    return tableRow(l.rc ? "stuck" : "done", titleOf(n) ?? l.branch, n != null ? `#${n}` : "", status,
      l.rc ? "stuck" : "", "", ru(l.took_h), `data-landed="${esc(l.branch)}"`);
  };
  const toggle = landed.length > LANDED_CAP
    ? `<button type="button" class="copy" data-toggle="merge-landed">${landedOpen ? "свернуть" : `показать все ${landed.length}`}</button>`
    : "";

  const inner = `${errorLines}
  <div class="lb"><b>Идёт</b></div>${runRows.length ? table("тесты", runRows) : '<p class="none">ничего не идёт</p>'}
  <div class="lb"><b>В очереди</b></div>${waitAll.length ? table("", waitAll) : '<p class="none">в очереди никого</p>'}
  <div class="lb"><b>Готово</b><span>${landed.length}</span>${toggle}</div>${landed.length ? table("", landedShown.map(landedRow)) : '<p class="none">ничего не влито</p>'}`;
  return wrap(sub, inner);
}
