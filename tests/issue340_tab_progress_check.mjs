// #340 unit 6 (tab Прогресс, parity rows 8-9): node check of web/monitor/tab-progress.js on the real captured
// snapshots (tests/fixtures/monitor/progress_fast.json, progress_tickets.json, and progress_tickets_zero.json, the same
// capture with zero-count groups). Prints one JSON object: { <check name>: { ok, detail } }. The expected values are
// written here from the spec and the collector's rules, never recomputed by the tab's own code. The clock is fixed
// (Date.now) so every duration is exact.
import { readFileSync } from "node:fs";

const FIXED = Date.parse("2026-10-23T20:00:00Z");
Date.now = () => FIXED;

const report = {};
const check = (name, ok, detail = "") => { report[name] = { ok: !!ok, detail: String(detail) }; };
const fixture = name => JSON.parse(readFileSync(new URL(`./fixtures/monitor/${name}`, import.meta.url), "utf-8"));

// independent expectations (the spec's formats, not the tab's code)
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const dur = s => {
  const v = Math.max(0, Math.round(s));
  if (v < 90) return `${v} с`;
  const m = Math.round(v / 60);
  return m < 90 ? `${m} мин` : m % 60 === 0 ? `${m / 60} ч` : `${Math.floor(m / 60)} ч ${m % 60} мин`;
};
const sinceText = iso => {
  const ms = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(ms) ? dur((FIXED - ms) / 1000) : "—";
};
const leftText = iso => {
  const ms = Date.parse(iso);
  if (ms <= FIXED) return "срок вышел";
  const min = Math.max(1, Math.floor((ms - FIXED) / 60000));
  const d = Math.floor(min / 1440), h = Math.floor((min % 1440) / 60), m = min % 60;
  if (d) return `через ${d} д${h ? ` ${h} ч` : ""}`;
  if (h) return `через ${h} ч${m ? ` ${m} мин` : ""}`;
  return `через ${m} мин`;
};
const cut40 = s => { const a = [...s]; return a.length > 40 ? `${a.slice(0, 39).join("")}…` : s; };
const STAGES = ["ready", "orch", "work", "merge", "accept"];
const statusText = r => {
  if (r.failed) return "упало";
  let t;
  if (r.wait) t = `ждёт: ${cut40(r.wait.reason ?? "")}`;
  else if (r.returned) t = "возвращён";
  else if (r.stage === "accept") t = "приёмка";
  else if (r.stage === "ready") t = "ждёт ORCH";
  else if (r.stage === "orch") t = "ORCH режет";
  else if (r.stage === "work") t = `${r.workers} worker${r.strong ? ` (${r.strong} strong)` : ""} · ${r.researchers} researcher`;
  else if (r.merge_percent != null) t = `merge: suite ${r.merge_percent} %`;
  else t = r.merge_position != null ? `ждёт merge, ${r.merge_position}-й` : "ждёт merge";
  if (r.units) t += ` · ${r.units.done}/${r.units.total}`;
  return t;
};
const timeAtStage = r => {
  const until = r.wait?.until;
  if (until && Number.isFinite(Date.parse(until))) return leftText(until);
  return sinceText(r.stage_since);
};
const countOf = (seg, s) => seg.split(`data-st="${s}"`).length - 1;
// a row or a group line: from its opening tag to the first closing div (neither has a div inside)
const rowSeg = (html, marker) => {
  const at = html.indexOf(marker);
  if (at < 0) return null;
  const end = html.indexOf("</div>", at);
  return end < 0 ? null : html.slice(at, end);
};
// one card: from its opening tag to its closing section
const cardSeg = (html, card) => {
  const at = html.indexOf(`data-card="${card}"`);
  if (at < 0) return null;
  const end = html.indexOf("</section>", at);
  return end < 0 ? null : html.slice(at, end);
};
const GITHUB = "https://github.com/";

// load the tab; a failed import is a failed check, not a crash
let tab = null;
try {
  tab = await import("../web/monitor/tab-progress.js");
} catch (e) {
  check("load", false, `import: ${e.message}`);
}

if (tab) {
  check("load", true, "tab-progress.js imports");
  const fast = fixture("progress_fast.json");
  const tickets = fixture("progress_tickets.json");
  const zeroTickets = fixture("progress_tickets_zero.json");
  const rows = fast.data.pipeline;
  const groups = tickets.data.groups;
  const repos = [{ id: "r1", label: "owner/name" }];
  const ui = { repo: "r1" };

  check("contract", tab.id === "progress" && tab.title === "Прогресс" && tab.order === 40 &&
    JSON.stringify(tab.endpoints) === '["fast","tickets"]' && typeof tab.render === "function",
    `id=${tab.id} title=${tab.title} order=${tab.order} endpoints=${JSON.stringify(tab.endpoints)}`);

  let html = "";
  try {
    html = String(tab.render({ fast, tickets }, {}));
  } catch (e) {
    check("render_full", false, `render threw: ${e.message}`);
  }
  if (html) {
    check("render_full", html.length > 0, `${html.length} chars`);

    // pipeline card: its title and count (row 8)
    check("pipe_card", html.includes('data-card="prog-pipe"') && html.includes(`<b>Пайплайн</b><span class="r">${rows.length}</span>`),
      `count ${rows.length}`);

    // every pipeline line: ticket number, title, stage strip with its stations, status, the two times
    for (const r of rows) {
      const seg = rowSeg(html, `<div class="tr" data-n="${r.number}"`);
      const name = `row_${r.number}`;
      if (!seg) { check(name, false, "no line"); continue; }
      const k = STAGES.indexOf(r.stage);
      const parts = [
        seg.includes(`data-stage="${r.stage}"`),
        countOf(seg, "passed") === k,
        countOf(seg, "current") === 1,
        countOf(seg, "later") === 4 - k,
        seg.includes(`>#${r.number}</`) && seg.includes(`</b> ${esc(r.title)}</span>`),
        seg.includes(`data-status>${esc(statusText(r))}</span>`),
        seg.includes(`data-since>${sinceText(r.started_at)}</span>`),
        seg.includes(`data-at>${timeAtStage(r)}</span>`),
      ];
      check(name, parts.every(Boolean),
        `stage ${r.stage} status "${statusText(r)}" since "${sinceText(r.started_at)}" at "${timeAtStage(r)}" parts ${parts.join(",")}`);
    }

    // countdown: a waiting row whose Parked date is 5 h 31 min ahead shows its time left
    const parkedRow = { ...rows.find(r => r.wait), wait: { reason: "parked", until: new Date(FIXED + 331 * 60000).toISOString() } };
    const parkedHtml = String(tab.render({ fast: { ...fast, data: { ...fast.data, pipeline: [parkedRow] } }, tickets }, {}));
    check("countdown_parked", parkedHtml.includes("через 5 ч 31 мин") && parkedHtml.includes(`data-n="${parkedRow.number}"`),
      "через 5 ч 31 мин");

    // titles are escaped
    const hostile = { ...fast, data: { ...fast.data, pipeline: [{ ...rows[0], title: "<i>x</i>" }] } };
    const hostileHtml = String(tab.render({ fast: hostile, tickets }, {}));
    check("escapes_title", hostileHtml.includes("&lt;i&gt;x&lt;/i&gt;") && !hostileHtml.includes("<i>x</i>"), "title");

    // groups cards (row 9): «По тикетам» without folder, «Остальное» the folders
    const own = Object.entries(groups).filter(([k]) => k !== "folder").reduce((a, [, n]) => a + n, 0);
    const other = groups.folder ?? 0;
    check("groups_card_tickets", html.includes('data-card="prog-tickets"') && html.includes(`<b>По тикетам</b><span class="r">${own}</span>`),
      `own ${own}`);
    check("groups_card_other", html.includes('data-card="prog-other"') && html.includes(`<b>Остальное</b><span class="r">${other}</span>`),
      `folder ${other}`);

    // every group row, zero counts included: count and share of its own card, the share bar at that width
    for (const [key, n] of Object.entries(groups)) {
      const denom = key === "folder" ? other : own;
      const share = denom ? (n / denom * 100).toFixed(1) : "0.0";
      const seg = rowSeg(html, `<div class="grow" data-group="${esc(key)}"`);
      const ok = !!seg && seg.includes(`data-n="${n}"`) && seg.includes(`data-share="${share}"`) &&
        seg.includes(`width:${share}%`);
      check(`group_${key}`, ok, `n ${n} share ${share}%`);
    }

    // the pane's head row in each group table: метка / тикетов / доля
    for (const card of ["prog-tickets", "prog-other"]) {
      const seg = cardSeg(html, card) ?? "";
      check(`head_${card}`, seg.includes("data-head") && seg.includes(">метка<") && seg.includes(">тикетов<") && seg.includes(">доля<"),
        card);
    }

    // links: the number is a link to the issue when the chosen repo's label is owner/name; else a plain number
    const linked = String(tab.render({ fast, tickets, repos }, ui));
    check("link_repo_label", linked.includes(`href="${GITHUB}owner/name/issues/${rows[0].number}"`), `${GITHUB}owner/name/issues/${rows[0].number}`);
    const unchosen = String(tab.render({ fast, tickets, repos }, { repo: "other" }));
    check("link_no_entry_plain", !unchosen.includes(`href="${GITHUB}`) && unchosen.includes(`>#${rows[0].number}</`), "no entry");
    const noSlash = String(tab.render({ fast, tickets, repos: [{ id: "r1", label: "name" }] }, ui));
    check("link_label_without_slash_plain", !noSlash.includes(`href="${GITHUB}`) && noSlash.includes(`>#${rows[0].number}</`), "label name");
  }

  // error text: a failed read shows its text, not an empty list; a last good answer stays beside its error
  const errTickets = String(tab.render({ fast, tickets: { at_ms: FIXED, refresh_s: 15, data: null, error: "gh: HTTP 502" } }, {}));
  check("error_tickets", errTickets.includes("gh: HTTP 502") && errTickets.includes('data-card="prog-pipe"'), "gh: HTTP 502");

  const errPipe = String(tab.render({ fast: { at_ms: FIXED, refresh_s: 2, data: { at: "19:00:00", pipeline_error: "сбой чтения 42" } }, tickets }, {}));
  check("error_pipeline", errPipe.includes("сбой чтения 42"), "сбой чтения 42");

  const errLast = String(tab.render({ fast: { at_ms: FIXED, refresh_s: 2, data: fast.data, error: "timeout 60 s" }, tickets }, {}));
  check("error_keeps_last_good", errLast.includes("timeout 60 s") && errLast.includes(`data-n="${rows[0].number}"`), "timeout 60 s");

  // no data yet: the loading text, no throw
  let loading = "";
  try { loading = String(tab.render({}, {})); } catch (e) { loading = `threw ${e.message}`; }
  check("loading", loading.includes("загружается") && !loading.includes("threw"), "загружается");

  // zero-count case (ticket_groups.py: needs-triage and no label stay at 0; no folder key, so «Остальное» is empty)
  const zeroHtml = String(tab.render({ fast, tickets: zeroTickets }, {}));
  const zeroGroups = zeroTickets.data.groups;
  for (const [key, n] of Object.entries(zeroGroups)) {
    const denom = key === "folder" ? 0 : Object.entries(zeroGroups).filter(([k]) => k !== "folder").reduce((a, [, c]) => a + c, 0);
    const share = denom ? (n / denom * 100).toFixed(1) : "0.0";
    const seg = rowSeg(zeroHtml, `<div class="grow" data-group="${esc(key)}"`);
    const ok = !!seg && seg.includes(`data-n="${n}"`) && seg.includes(`data-share="${share}"`) && seg.includes(`width:${share}%`);
    check(`zero_group_${key}`, ok, `n ${n} share ${share}%`);
  }
  const otherCard = cardSeg(zeroHtml, "prog-other") ?? "";
  check("zero_card_other_head_only",
    otherCard.includes("data-head") && otherCard.includes(`<b>Остальное</b><span class="r">0</span>`) && !otherCard.includes("нет") &&
    !otherCard.includes("data-group"),
    "head only, no «нет»");
}

process.stdout.write(JSON.stringify(report));
