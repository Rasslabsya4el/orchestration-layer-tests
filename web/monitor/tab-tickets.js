// Тикеты tab (#340 unit 4, parity row 5): the «Цели» / «Вне целей» tree of the Тикеты pane, drawn from the tickets read
// (collect.py tickets(): goals, loose, issues). Plain ES module, no DOM at import; render(snapshot, ui) is pure.
// The rules are the pane's: register.tsx ticketsTab; kit.tsx STATE_WORD, STATE_ICON, stateOf, subtree and heldIssues.
// The shell gives ui.open(key) / ui.toggle(key) / ui.repo (shell.js createShell).
import { esc } from "./shell.js";

export const id = "tickets";
export const title = "Тикеты";
export const order = 20;
export const endpoints = ["tickets", "fast"];

// The eight drawn states in the pane's legend order, and the words the pane draws them with (#1079: GitHub's names).
const STATES = ["work", "owner", "ready", "blocked", "open", "triage", "closed", "dropped"];
const WORD = {
  work: "held", owner: "ready-for-human", blocked: "blocked", ready: "ready-for-agent",
  triage: "needs-triage", open: "open", closed: "closed", dropped: "not planned",
};
// A state's dot: colour fill, or a ring for the open and not-planned states (the pane draws icons; these are the same shapes).
const DOT = {
  work: "background:var(--run)",
  owner: "background:var(--acc)",
  ready: "background:var(--ready)",
  blocked: "background:var(--ag)",
  triage: "background:var(--fnt)",
  open: "background:none;box-shadow:inset 0 0 0 1.5px var(--mut)",
  closed: "background:#48484a",
  dropped: "background:none;box-shadow:inset 0 0 0 1.5px var(--fnt);opacity:.6",
};
const INDENT = 18;   // px per tree level
const MARK_HELD = "◆", MARK_BLOCKED = "⧗", MARK_PARKED = "‖";

// The shell's contract is ui.open(key); a ui without it expands nothing.
function isOpen(ui, key) {
  return !!(ui && typeof ui.open === "function" && ui.open(key));
}

// The tickets ORCH holds, from the fast read when the page has one (the pane's heldIssues). The tickets read alone does
// not say what is held. A fast read that is missing, carries an error, or whose data is not an object gives no held
// ticket; a list that is not a list is read as empty.
function heldSet(fast) {
  const out = new Set();
  if (!fast || fast.error != null || !fast.data || typeof fast.data !== "object") return out;
  const data = fast.data;
  const list = v => (Array.isArray(v) ? v : []);
  const add = v => { const x = Number(v); if (Number.isFinite(x)) out.add(x); };
  for (const a of list(data.agents)) {
    if (!a || typeof a !== "object") continue;
    if (a.busy && a.issue) add(a.issue);
    for (const h of list(a.held)) add(h && typeof h === "object" ? h.number : h);
    for (const s of list(a.subagents)) if (s && s.issue) add(s.issue);
  }
  const running = data.runs && typeof data.runs === "object" ? list(data.runs.running) : [];
  for (const j of running) if (j && j.issue) add(j.issue);
  return out;
}

// One notice line above the tab when the fast read failed (an error, or no data object); none when the read is good or absent.
function heldNotice(fast) {
  if (!fast || typeof fast !== "object") return "";
  const err = fast.error != null && String(fast.error) !== "" ? String(fast.error) : null;
  if (!err && fast.data && typeof fast.data === "object") return "";
  return `<div class="none" data-held-notice="1">Метки «held» недоступны: ${esc(err ?? "нет данных")}</div>`;
}

// The pane's stateOf / iconState: a closed not-planned ticket is `dropped`, an open held one is `work`, else its state.
function stateOf(i, held) {
  if (!i.open && i.reason === "NOT_PLANNED") return "dropped";
  if (i.open && held.has(i.n)) return "work";
  return STATES.includes(i.state) ? i.state : "open";
}

// The issue web address base of the repo the reader chose: the label of the entry in snapshot.repos whose id is ui.repo,
// as https://github.com/<label>/issues/. Null when there is no such entry or its label has no `/` (links are then plain numbers).
function issueBase(snapshot, ui) {
  const repos = Array.isArray(snapshot.repos) ? snapshot.repos : [];
  const want = ui && ui.repo != null ? String(ui.repo) : null;
  const entry = want == null ? null : repos.find(r => r && String(r.id) === want);
  const label = entry ? String(entry.label ?? "") : "";
  return label.includes("/") ? `https://github.com/${label}/issues/` : null;
}

function rowHtml(i, depth, ctx) {
  const st = stateOf(i, ctx.held);
  const kids = ctx.kidsOf(i);
  const all = ctx.subtreeOf(i);
  const closed = all.filter(k => !k.open).length;
  const expanded = kids.length > 0 && ctx.isOpen(`tk-${i.n}`);
  const held = ctx.held.has(i.n) && i.open;
  // The blocked mark is for an open ticket whose drawn state is not `blocked`: the dot already says blocked.
  const blockers = i.open && st !== "blocked" ? (i.blockers || []) : [];
  const parked = !!i.parked;

  const toggle = kids.length
    ? `<button type="button" data-toggle="tk-${i.n}" aria-expanded="${expanded}" aria-label="${expanded ? "свернуть" : "раскрыть"}" style="background:none;border:0;padding:0;width:14px;cursor:pointer;color:var(--mut)">${expanded ? "▾" : "▸"}</button>`
    : `<span style="display:inline-block;width:14px"></span>`;
  const number = `<b>#${i.n}</b>`;
  const link = ctx.base ? `<a href="${esc(ctx.base + i.n)}" target="_blank" rel="noopener">${number}</a>` : number;
  const name = `<span style="color:${i.open ? "var(--txt)" : "var(--mut)"}">${esc(i.title)}</span>`;
  const dot = `<span data-tip="${esc(WORD[st])}" style="display:flex"><i class="d" style="${DOT[st]}"></i></span>`;
  const marks = [
    held ? `<span data-tip="${esc("ORCH держит тикет")}" style="color:var(--run)">${MARK_HELD}</span>` : "",
    blockers.length ? `<span data-tip="${esc("блокеры: " + blockers.map(b => `#${b}`).join(", "))}" style="color:var(--bad)">${MARK_BLOCKED}</span>` : "",
    parked ? `<span data-tip="${esc("отложен (parked)")}" style="color:var(--mut)">${MARK_PARKED}</span>` : "",
  ].join("");

  return `<div class="tr" data-n="${i.n}" data-depth="${depth}" data-state="${st}" data-held="${held ? 1 : 0}" data-blocked="${blockers.length ? 1 : 0}" data-parked="${parked ? 1 : 0}">`
    + dot
    + `<span class="c-ti" style="padding-left:${depth * INDENT}px">${toggle} ${link} ${name}</span>`
    + `<span class="num">${all.length ? `${closed}/${all.length}` : ""}</span>`
    + `<span class="c-st">${esc(WORD[st])}</span>`
    + `<span class="c-ti" style="overflow:visible">${marks}</span>`
    + `<span></span>`
    + `</div>`;
}

// One issue and, when its key is open, its children (depth first, each once).
function treeRows(i, depth, ctx, out, seen) {
  if (seen.has(i.n)) return;
  seen.add(i.n);
  out.push(rowHtml(i, depth, ctx));
  if (ctx.kidsOf(i).length && ctx.isOpen(`tk-${i.n}`)) {
    for (const k of ctx.kidsOf(i)) treeRows(k, depth + 1, ctx, out, seen);
  }
}

function card(name, count, body) {
  return `<section class="w"><div class="lb"><b>${esc(name)}</b><span class="r">${esc(count)}</span></div>${body}</section>`;
}

function table(rows) {
  const head = `<div class="tr th"><span></span><span>тикет · название</span><span class="num">готово</span><span>состояние</span><span>отметки</span><span></span></div>`;
  return `<div class="tblw"><div class="tbl">${head}${rows.join("")}</div></div>`;
}

function legend() {
  const items = STATES.map(s => `<span data-state="${s}"><i class="d" style="${DOT[s]};display:inline-block;margin-right:6px;vertical-align:-1px"></i>${esc(WORD[s])}</span>`);
  return `<div class="mkey" data-legend="1">${items.join("")}</div>`;
}

function errorPanel(error) {
  return `<section class="w" data-error="1"><div class="lb"><b>Тикеты не прочитаны</b></div><div class="banner">${esc(error)}</div></section>`;
}

function loading() {
  return `<section class="w"><div class="none">загружается</div></section>`;
}

// The Тикеты tab. snapshot.tickets = {at_ms, refresh_s, data, error?}: a failed read shows its error text (over the last
// good tree, when there is one); the tree is never drawn empty in its place.
export function render(snapshot, ui) {
  const snap = snapshot || {};
  const top = heldNotice(snap.fast);
  const env = snap.tickets || null;
  const error = env && env.error != null && String(env.error) !== "" ? String(env.error) : null;
  if (!env || !env.data) return top + (error ? errorPanel(error) : loading());

  const data = env.data;
  const byN = n => (data.issues || {})[String(n)];
  const ctx = {
    held: heldSet(snap.fast),
    base: issueBase(snap, ui),
    kidsOf: i => (i.kids || []).map(k => byN(k)).filter(Boolean),
    subtreeOf: i => {
      const out = [], seen = new Set([i.n]);
      const walk = x => { for (const k of ctx.kidsOf(x)) if (!seen.has(k.n)) { seen.add(k.n); out.push(k); walk(k); } };
      walk(i);
      return out;
    },
    isOpen: key => isOpen(ui, key),
  };
  const roots = list => (list || []).map(n => byN(n)).filter(Boolean);
  const seen = new Set();
  const goalRows = [], looseRows = [];
  for (const g of roots(data.goals)) treeRows(g, 0, ctx, goalRows, seen);
  for (const l of roots(data.loose)) treeRows(l, 0, ctx, looseRows, seen);

  const parts = [top];
  if (error) parts.push(errorPanel(error));
  parts.push(legend());
  parts.push(card("Цели", String(roots(data.goals).length), goalRows.length ? table(goalRows) : `<div class="none">нет</div>`));
  parts.push(card("Вне целей", String(roots(data.loose).length), looseRows.length ? table(looseRows) : `<div class="none">нет</div>`));
  parts.push(`<div class="none">данные на ${esc(data.at ?? "")}</div>`);
  return parts.join("");
}
