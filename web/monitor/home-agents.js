// Главная, row 1: the Agents panel (#340 unit 3a). `agentsPanel(snapshot, ui) -> html`, the same data as the pane's
// Agents card (the monitor mod's hooks/register.tsx agentsBlock): one strip per session, sections «Чаты» and «Фоновые сессии»,
// an ORCH's tickets with their working subagents (a finished one under a ticket is a count on the ticket row), every other
// subagent a row (no cap). Plain ES module, no DOM at import.
import { esc } from "./shell.js";

const DASH = "—";
const KNOWN_ROLES = new Set(["worker", "worker-strong", "researcher-find", "researcher-how", "reviewer-standards", "reviewer-spec"]);

// "86 s", "1.9 min", "1 h 12 min" -> seconds (the collector's ages), for ordering and for the Russian form.
function ageS(v) {
  let n = 0;
  for (const m of String(v ?? "").matchAll(/(\d+(?:\.\d+)?)\s*(s|min|h|d)\b/g)) {
    n += Number(m[1]) * { s: 1, min: 60, h: 3600, d: 86400 }[m[2]];
  }
  return n;
}

// Seconds -> seconds under 90, then minutes, then hours and minutes (the pane's `dur`).
function dur(s) {
  const v = Math.max(0, Math.round(s));
  if (v < 90) return `${v} с`;
  const m = Math.round(v / 60);
  return m < 90 ? `${m} мин` : m % 60 === 0 ? `${m / 60} ч` : `${Math.floor(m / 60)} ч ${m % 60} мин`;
}

// A collector time ("30 min", "2 h 19 min") in the Russian form; a string with no time in it stays as it is.
function ru(v) {
  const t = String(v ?? "");
  return /\d\s*(s|min|h|d)\b/.test(t) ? dur(ageS(t)) : t;
}

// The Russian noun form for a count: 1 сессия, 2 сессии, 5 сессий.
function ruForm(k, one, few, many) {
  return k % 10 === 1 && k % 100 !== 11 ? one : k % 10 >= 2 && k % 10 <= 4 && (k % 100 < 12 || k % 100 > 14) ? few : many;
}

const kctx = n => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));
const shortModel = m => String(m ?? "").replace(/^Claude\s+/, "").trim();
const isOrch = a => /^orch\b/i.test(String(a.name ?? ""));
const isChat = a => a.kind !== "background" && !isOrch(a) && !/^autopilot\b/i.test(String(a.name ?? ""));
const subName = s => String(s.name || s.type || "").replace(/^#\d+\s*[:·-]?\s*/, "");

// `<role>: <title>` of a subagent: the prefix is a role only when it is the subagent's own type or a known role.
function roleTitle(text, type) {
  const m = /^([a-z][a-z0-9-]*): ([\s\S]*)$/i.exec(text);
  if (!m) return null;
  const role = m[1].toLowerCase();
  if (role !== String(type ?? "").toLowerCase() && !KNOWN_ROLES.has(role)) return null;
  return m[2];
}

// The last tool call or result of a digest (a `say:` line is not an action).
function lastEvent(digest) {
  return String(digest ?? "").split("\n").filter(Boolean).reverse().find(l => !l.startsWith("say: ")) ?? "";
}

// What a busy agent does, in a few Russian words from its last tool call (the pane's rawWord, the fallback before a word exists).
function rawWord(digest, merging = false) {
  const lines = String(digest ?? "").split("\n").filter(Boolean);
  const last = lines.slice().reverse().find(l => !l.startsWith("say: ")) ?? "";
  const tool = /^(\w+)/.exec(last)?.[1] ?? "";
  if (/pytest|plugin test|\btest\b/i.test(last) && /^(Bash|PowerShell)/.test(tool)) return "гоняет тесты";
  if (/^(Edit|Write|MultiEdit|NotebookEdit)$/.test(tool)) return "правит код";
  if (/^(Read)$/.test(tool)) return "читает код";
  if (/^(Grep|Glob)$/.test(tool)) return "ищет в коде";
  if (/^(WebSearch|WebFetch)$/.test(tool) || /^mcp__.*browser/i.test(tool)) return "ищет в интернете";
  if (/^(Agent|Task|SendMessage)$/.test(tool)) return "ждёт воркера";
  if (/gh issue (create|edit)/.test(last)) return "пишет тикет";
  if (/gh issue (view|list)/.test(last)) return "читает тикет";
  if (merging && /merge/i.test(last)) return "мерджит";
  if (/^(Bash|PowerShell|Monitor)$/.test(tool)) return "смотрит логи";
  return lines.length ? "думает" : "";
}

// A session waits on GitHub's budget: its last result names the rate limit, or its last background wait is for a reset.
function waitsGithub(digest) {
  const last = lastEvent(digest);
  if (/^result: /.test(last)) return /API rate limit/i.test(last);
  return /^(Bash|PowerShell|Monitor)\b/.test(last) && /\b(wait|sleep|until)\b/i.test(last) && /\b(reset|rate limit)\b/i.test(last);
}

// The word of a busy session: «ждёт GitHub» on a rate limit, else the raw word (a merge word only while the merge line holds one of its tickets).
function sessionWord(a, merge, ownTickets) {
  if (waitsGithub(a.digest)) return "ждёт GitHub";
  const merging = ownTickets.some(n => merge?.running?.issues?.includes(n) || merge?.waiting?.some(w => w.issues?.includes(n)));
  return rawWord(a.digest, merging);
}

// The ticket's wait in plain words when its ORCH holds it but does not claim it (the pane's parkedOn).
// The pane's JOB_QUEUES branch (the repo add-on's job queues: «ждёт …» for a job of that add-on) is dropped here:
// add-on row P: unit 8 brings it back as the add-on's own adapter, so this panel never reads the add-on's queues.
function parkedOn(n, merge, issue) {
  if (merge?.running?.issues?.includes(n) || merge?.waiting?.some(w => w.issues?.includes(n))) return "ждёт мерджа";
  if (merge?.landed?.some(l => l.rc === 0 && l.issues?.includes(n))) return "смерджен, ждёт закрытия";
  if (issue?.labels?.includes("needs-info")) return "ждёт ответа";
  return null;
}

// One table row. lvl 0 = a session; 1 and up = nested (the .sub indent, deeper by 18 px per level beyond 1).
function row({ lvl = 0, attr = "", dot, name, nameMuted = false, word = "", tick = "", model = "", ctx = "", age = "" }) {
  const cls = lvl > 0 ? "tr sub" : "tr";
  const pad = lvl > 1 ? ` style="padding-left:${18 * lvl}px"` : "";
  const nm = nameMuted ? `<div class="c-st">${esc(name)}</div>` : `<div>${esc(name)}</div>`;
  const w = word ? `<div class="c-ef">${esc(word)}</div>` : "";
  return `<div class="${cls}"${attr}><span><i class="d ${dot}"></i></span>`
    + `<div class="c-ti"${pad}>${nm}${w}</div>`
    + `<span class="c-mo">${esc(tick)}</span><span class="c-ef">${esc(model)}</span>`
    + `<span class="num">${esc(ctx)}</span><span class="num">${esc(age)}</span></div>`;
}

const ageText = v => {
  const s = ru(v).trim();
  return s && s !== "-" ? s : DASH;
};
const ctxText = n => (n && n > 0 ? kctx(n) : DASH);
const modelText = m => shortModel(m) || DASH;
const byBusy = (x, y) => Number(!!y.busy) - Number(!!x.busy);
const byAgeAsc = (x, y) => ageS(x.age) - ageS(y.age);

// A subagent row: the role's title (or the name), its ticket, model, context, the word while busy, and its age.
// `withTick` = false under an ORCH's ticket row, which already names the ticket (the pane's subRow withTick).
function subRow(s, lvl, withTick = true) {
  const role = roleTitle(subName(s), s.type);
  const deep = (s.depth ?? 1) > 1 ? 1 : 0;
  const busy = !!s.busy;
  return row({
    lvl: lvl + deep,
    attr: ` data-sub="${esc(s.id ?? "")}"`,
    dot: busy ? "run" : "done",
    name: role ?? subName(s),
    nameMuted: !busy,
    word: busy ? rawWord(s.digest) : "",
    tick: withTick && s.issue != null ? `#${s.issue}` : "",
    model: modelText(s.model),
    ctx: ctxText(s.ctx),
    age: ageText(s.age),
  });
}

// The tickets of an ORCH: its held numbers and the issues of its subagents, each with its state, its working and finished subagents.
function orchTickets(a, tickets, merge) {
  const held = (a.held ?? []).map(h => (typeof h === "number" ? { number: h } : h));
  const subs = a.subagents ?? [];
  const issues = tickets?.data?.issues ?? {};
  const nums = [...new Set([...held.map(h => h.number), ...subs.map(s => s.issue).filter(n => n != null)])];
  const landed = new Set((merge?.landed ?? []).filter(l => l.rc === 0).flatMap(l => l.issues ?? []));
  const list = nums.map(n => {
    const h = held.find(x => x.number === n);
    const work = subs.filter(s => s.busy && s.issue === n).sort(byAgeAsc);
    const fin = subs.filter(s => !s.busy && s.issue === n).sort(byAgeAsc);
    const iss = issues[String(n)];
    const closed = iss?.open === false;
    const st = work.length ? 0 : closed ? 4 : h?.claimed ? 1 : h ? 2 : 3;
    const last = [...work, ...fin].sort(byAgeAsc)[0];
    const units = h?.units ?? a.ticket_units?.[String(n)] ?? null;
    return { n, work, fin, st, last, units, iss, closed, landed: landed.has(n) };
  });
  list.sort((x, y) => x.st - y.st || (x.st === 4 ? ageS(x.last?.age ?? "99 d") - ageS(y.last?.age ?? "99 d") : 0));
  return { list, merge, held };
}

function ticketRow(k, merge) {
  const nW = k.work.length, nF = k.fin.length;
  const said = [
    k.units ? `${k.units.done}/${k.units.total}` : "",
    k.st === 0 ? `в работе: ${nW} ${ruForm(nW, "воркер", "воркера", "воркеров")}`
      : k.st === 1 ? (nF ? "у ORCH в работе" : "взят, воркера пока нет")
        : k.st === 2 ? parkedOn(k.n, merge, k.iss) ?? (nF ? "отложен ORCH" : "ещё не взят")
          : k.st === 4 ? "закрыт" : "",
    nF ? `${nF} ${ruForm(nF, "отработал", "отработали", "отработали")}` : "",
  ].filter(Boolean).join(" · ");
  const dot = k.st === 4 ? "done" : k.st <= 1 ? "run" : "wait";
  const title = k.iss?.title;
  return row({
    lvl: 1,
    attr: ` data-ticket="${esc(k.n)}"`,
    dot,
    name: title ?? "тикет",
    nameMuted: k.st !== 0,
    word: said,
    tick: `#${k.n}`,
    age: ageText(k.last?.age),
  });
}

// One session and its nested rows: an ORCH's tickets (each with its subagents), the loose subagents, or a chat's subagents.
function sessionBlock(a, tickets, merge) {
  const orch = isOrch(a);
  const ownTickets = [...(a.held ?? []).map(h => (typeof h === "number" ? h : h?.number)), ...(a.issue != null ? [a.issue] : [])];
  const subs = a.subagents ?? [];
  let tick = a.issue != null ? `#${a.issue}` : "";
  let nested = "";
  if (orch) {
    const { list } = orchTickets(a, tickets, merge);
    const closedOrLanded = list.filter(k => k.closed || k.landed).length;
    tick = list.length ? `${closedOrLanded}/${list.length} тикетов` : "";
    for (const k of list) {
      nested += ticketRow(k, merge);
      // A working subagent is a row; a finished one is only the count on its ticket row (never a row).
      for (const s of k.work) nested += subRow(s, 2, false);
    }
    for (const s of subs.filter(s => s.issue == null).sort((x, y) => byBusy(x, y) || byAgeAsc(x, y))) nested += subRow(s, 1);
  } else {
    for (const s of subs.slice().sort((x, y) => byBusy(x, y) || byAgeAsc(x, y))) nested += subRow(s, 1);
  }
  const busy = !!a.busy;
  const head = row({
    attr: ` data-session="${esc(a.sid ?? "")}"`,
    dot: busy ? "run" : "done",
    name: orch ? "ORCH" : String(a.name ?? ""),
    word: busy ? sessionWord(a, merge, ownTickets) : "",
    tick,
    model: modelText(a.model),
    ctx: ctxText(a.ctx),
    age: ageText(a.age),
  });
  return head + nested;
}

export function agentsPanel(snapshot, ui) {
  const fast = snapshot?.fast ?? {};
  const d = fast.data ?? {};
  const tickets = snapshot?.tickets;
  const merge = d.merge;
  const agents = Array.isArray(d.agents) ? d.agents : [];
  const errors = [fast.error, d.agents_error].filter(e => e != null && String(e) !== "").map(e => `<p class="banner">${esc(e)}</p>`).join("");
  const chats = agents.filter(isChat).sort(byBusy);
  const bg = agents.filter(a => !isChat(a)).sort(byBusy);
  const section = (label, list) => `<div class="tr th"><span></span><span>${esc(label)}</span></div>`
    + (list.length ? list.map(a => sessionBlock(a, tickets, merge)).join("") : `<div class="tr"><span></span><span class="c-st">пусто</span></div>`);
  const body = `<div class="tblw"><div class="tbl">`
    + `<div class="tr th"><span></span><span>сессия · делает</span><span>тикет</span><span>модель</span><span class="num">контекст</span><span class="num">длительность</span></div>`
    + section("Чаты", chats)
    + section("Фоновые сессии", bg)
    + `</div></div>`;
  // The card subtitle, as the pane's Agents card: «N работает, M без дела».
  const busyN = agents.filter(a => a.busy).length;
  return `<section class="w full" data-panel="agents" data-row="1"><div class="lb"><b>Agents</b>`
    + `<span class="r">${busyN} ${ruForm(busyN, "работает", "работают", "работают")}, ${agents.length - busyN} без дела</span></div>`
    + errors + body + `</section>`;
}
