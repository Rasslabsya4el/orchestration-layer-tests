// Прогресс tab (#340 unit 6): the pane's Progress tab, rows 8 (Пайплайн) and 9 («По тикетам» / «Остальное»).
// Pure: render(snapshot) -> html string. Reads snapshot.fast (its `pipeline` rows) and snapshot.tickets (its `groups`).
// A failed read shows its error text; the lists are whole (the pipeline has one line per ticket, all in the tab).

export const id = "progress";
export const title = "Прогресс";
export const order = 40;
export const endpoints = ["fast", "tickets"];

const STAGES = ["ready", "orch", "work", "merge", "accept"];
const STAGE_NAME = { ready: "ждёт ORCH", orch: "ORCH режет", work: "работа", merge: "merge", accept: "приёмка" };
// the groups in the order the pane draws them (ticket_groups.py GROUPS); a folder is the card «Остальное», all others «По тикетам»
const GROUP_ORDER = ["needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix", "parked", "autopilot",
  "folder", "blocked", "parked-on-run", "no label"];
const FOLDER = "folder";
const DASH = "—";

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// seconds -> seconds under 90, then minutes, then hours and minutes (the pane's dur)
function dur(s) {
  const v = Math.max(0, Math.round(s));
  if (v < 90) return `${v} с`;
  const m = Math.round(v / 60);
  return m < 90 ? `${m} мин` : m % 60 === 0 ? `${m / 60} ч` : `${Math.floor(m / 60)} ч ${m % 60} мин`;
}

// the time since an ISO stamp; a dash when the stamp does not read
function sinceText(iso, now) {
  const ms = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(ms) ? dur((now - ms) / 1000) : DASH;
}

// the time left to a Parked date: «через 6 д 10 ч», «через 5 ч 20 мин», «через 30 мин»; a past date «срок вышел»
function leftText(iso, now) {
  const ms = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(ms)) return DASH;
  if (ms <= now) return "срок вышел";
  const min = Math.max(1, Math.floor((ms - now) / 60000));
  const d = Math.floor(min / 1440), h = Math.floor((min % 1440) / 60), m = min % 60;
  if (d) return `через ${d} д${h ? ` ${h} ч` : ""}`;
  if (h) return `через ${h} ч${m ? ` ${m} мин` : ""}`;
  return `через ${m} мин`;
}

// a waiting reason cut to 40 characters, the last one an ellipsis
function cutReason(s, n = 40) {
  const a = [...String(s)];
  return a.length > n ? `${a.slice(0, n - 1).join("")}…` : String(s);
}

// a line's status word: failed `упало`; a wait `ждёт: <reason>`; `возвращён`; the stage's word; `· k/n` when it has units
function statusText(r) {
  let t;
  if (r.failed) return "упало";
  if (r.wait) t = `ждёт: ${cutReason(r.wait.reason ?? "")}`;
  else if (r.returned) t = "возвращён";
  else if (r.stage === "accept") t = "приёмка";
  else if (r.stage === "ready") t = "ждёт ORCH";
  else if (r.stage === "orch") t = "ORCH режет";
  else if (r.stage === "work") t = `${r.workers} worker${r.strong ? ` (${r.strong} strong)` : ""} · ${r.researchers} researcher`;
  else if (r.merge_percent != null) t = `merge: suite ${r.merge_percent} %`;
  else t = r.merge_position != null ? `ждёт merge, ${r.merge_position}-й` : "ждёт merge";
  if (r.units) t += ` · ${r.units.done}/${r.units.total}`;
  return t;
}

// the time at a line's stage: a wait with a readable Parked date counts down to it, else the time since the stage began
function atStageText(r, now) {
  const until = r.wait && r.wait.until;
  if (until && Number.isFinite(Date.parse(until))) return leftText(until, now);
  return sinceText(r.stage_since, now);
}

// the five stations of a line's strip: passed before its stage, current at it, later after it
function strip(stage, failed) {
  const cur = STAGES.indexOf(stage);
  const x = k => 6 + k * 19;
  const colour = failed ? "var(--bad)" : stage === "ready" ? "var(--mut)" : "var(--run)";
  const marks = STAGES.map((_s, k) => {
    if (k < cur) return `<circle data-st="passed" cx="${x(k)}" cy="6" r="3.2" style="fill:var(--mut)"/>`;
    if (k === cur) return `<circle data-st="current" cx="${x(k)}" cy="6" r="4.2" style="fill:${colour}"/>`;
    return `<circle data-st="later" cx="${x(k)}" cy="6" r="3.2" style="fill:var(--tile);stroke:var(--mut);stroke-width:1.2"/>`;
  });
  return `<svg class="strip" data-stage="${esc(stage)}" width="88" height="12" viewBox="0 0 88 12" role="img" `
    + `aria-label="${esc(STAGE_NAME[stage] ?? stage)}"><rect x="6" y="5.25" width="76" height="1.5" style="fill:var(--line2)"/>`
    + `${marks.join("")}</svg>`;
}

// the GitHub issues base of the repo the page shows (snapshot.repos entry whose id is ui.repo); "" when there is none
// or its label is not owner/name
function issueBase(snapshot, ui) {
  const id = ui ? ui.repo : undefined;
  const repos = snapshot && Array.isArray(snapshot.repos) ? snapshot.repos : [];
  const repo = id == null ? null : repos.find(r => r && r.id === id);
  const label = repo && typeof repo.label === "string" ? repo.label : "";
  return label.includes("/") ? `https://github.com/${label}/issues/` : "";
}

function lineRow(r, now, base) {
  const dot = r.failed ? `<span class="d" style="background:var(--bad)"></span>`
    : r.stage === "ready" ? `<span class="d wait"></span>` : `<span class="d run"></span>`;
  const statusStyle = r.failed ? ` style="color:var(--bad)"` : "";
  const number = base ? `<a href="${esc(base)}${esc(r.number)}">#${esc(r.number)}</a>` : `#${esc(r.number)}`;
  return `<div class="tr" data-n="${esc(r.number)}" data-stage="${esc(r.stage)}" data-failed="${r.failed ? 1 : 0}">`
    + `<span>${dot}</span>`
    + `<span class="c-ti"><b>${number}</b> ${esc(r.title)}</span>`
    + `<span data-tip="${esc(STAGE_NAME[r.stage] ?? r.stage)}">${strip(r.stage, r.failed)}</span>`
    + `<span class="num" data-since>${sinceText(r.started_at, now)}</span>`
    + `<span class="c-st"${statusStyle} data-status>${esc(statusText(r))}</span>`
    + `<span class="num" data-at>${atStageText(r, now)}</span>`
    + `</div>`;
}

function fresh(at) {
  return `обновлено ${String(at ?? "-").replace(/^(\d\d:\d\d):\d\d$/, "$1")}`;
}

function banners(messages) {
  return messages.map(m => `<div class="banner" role="alert">${esc(m)}</div>`).join("");
}

function pipelineCard(fast, tickets, now, base) {
  const data = fast && fast.data ? fast.data : null;
  const errors = [];
  if (fast && fast.error) errors.push(String(fast.error));
  if (data && data.pipeline_error) errors.push(String(data.pipeline_error));
  if (tickets && tickets.data && tickets.data.pipeline_error) errors.push(String(tickets.data.pipeline_error));
  const rows = data && Array.isArray(data.pipeline) ? data.pipeline : null;
  let body;
  if (rows && rows.length) {
    body = `<div class="tblw"><div class="tbl">`
      + `<div class="tr th"><span></span><span>задача</span><span>этап</span><span class="num">с начала</span>`
      + `<span>статус</span><span class="num">на этапе</span></div>`
      + rows.map(r => lineRow(r, now, base)).join("")
      + `</div></div>`;
  } else if (rows) {
    body = `<div class="none">в пайплайне нет тикетов</div>`;
  } else {
    body = errors.length ? "" : `<div class="none">загружается</div>`;
  }
  const count = rows ? rows.length : DASH;
  const foot = data && data.at ? `<div class="none">${fresh(data.at)}</div>` : "";
  return `<section class="w c12" data-card="prog-pipe"><div class="lb"><b>Пайплайн</b><span class="r">${esc(count)}</span></div>`
    + `${banners(errors)}${body}${foot}</section>`;
}

const GROUP_GRID = "display:grid;grid-template-columns:minmax(8em,auto) 3em minmax(0,1fr) 4.5em;gap:0 10px;align-items:center";

// a group table: its head row, then one row per group, zero counts included; an empty table draws only its head
function groupCard(card, name, groups, own, tickets) {
  const error = tickets && tickets.error ? [String(tickets.error)] : [];
  const data = tickets && tickets.data ? tickets.data : null;
  let body;
  if (!data) {
    body = error.length ? "" : `<div class="none">загружается</div>`;
  } else {
    const head = `<div class="grow" data-head style="${GROUP_GRID};font-size:12px;color:var(--mut);padding:3px 0">`
      + `<span>метка</span><span class="num">тикетов</span><span></span><span class="num">доля</span></div>`;
    body = head + groups.map(([key, n]) => {
      const share = own ? (n / own * 100).toFixed(1) : "0.0";
      return `<div class="grow" data-group="${esc(key)}" data-n="${n}" data-share="${share}" `
        + `style="${GROUP_GRID};font-size:13px;padding:3px 0"><span class="c-ti">${esc(key)}</span>`
        + `<span class="num">${n}</span>`
        + `<span class="bar"><i class="k-p" style="width:${share}%"></i></span>`
        + `<span class="num">${share} %</span></div>`;
    }).join("");
  }
  const foot = data && data.at ? `<div class="none">${fresh(data.at)}</div>` : "";
  return `<section class="w c6" data-card="${card}"><div class="lb"><b>${name}</b><span class="r">${own}</span></div>`
    + `${banners(error)}${body}${foot}</section>`;
}

// the tickets' group counts split for the two cards, each in the pane's order
function groupsOf(tickets) {
  const data = tickets && tickets.data ? tickets.data : null;
  const counts = data && data.groups && typeof data.groups === "object" ? data.groups : {};
  const keys = [...GROUP_ORDER.filter(k => k in counts), ...Object.keys(counts).filter(k => !GROUP_ORDER.includes(k))];
  const pairs = keys.map(k => [k, Number(counts[k]) || 0]);
  const ticketPairs = pairs.filter(([k]) => k !== FOLDER);
  const folderPairs = pairs.filter(([k]) => k === FOLDER);
  const sum = list => list.reduce((a, [, n]) => a + n, 0);
  return { tickets: ticketPairs, other: folderPairs, ownTickets: sum(ticketPairs), ownOther: sum(folderPairs) };
}

// The tab's html: the Пайплайн card, then «По тикетам» and «Остальное». ui.repo picks the repo whose issues the numbers link to.
export function render(snapshot, ui = {}) {
  const fast = snapshot && snapshot.fast ? snapshot.fast : null;
  const tickets = snapshot && snapshot.tickets ? snapshot.tickets : null;
  const now = Date.now();
  const base = issueBase(snapshot, ui);
  const g = groupsOf(tickets);
  return `<div class="grid">`
    + pipelineCard(fast, tickets, now, base)
    + groupCard("prog-tickets", "По тикетам", g.tickets, g.ownTickets, tickets)
    + groupCard("prog-other", "Остальное", g.other, g.ownOther, tickets)
    + `</div>`;
}
