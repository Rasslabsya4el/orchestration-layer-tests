// #340 unit 3a (Главная, Agents panel): web/monitor/home-agents.js rendered on the recorded `fast` snapshot
// (tests/fixtures/monitor/fast.json) and an empty `tickets` snapshot. Plain node 24 ES module: it prints one JSON
// object {check: {ok, detail}} on stdout; tests/test_issue340_home_agents.py runs it and asserts each named check.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const fixture = JSON.parse(readFileSync(join(here, "fixtures", "monitor", "fast.json"), "utf-8"));
const EMPTY_TICKETS = { at_ms: 0, refresh_s: 15, data: { at: "", goals: [], loose: [], groups: [], issues: {} } };

const report = {};
const check = (name, ok, detail = "") => { report[name] = { ok: !!ok, detail: String(detail) }; };

let mod = null;
let loadError = null;
try {
  mod = await import(pathToFileURL(join(root, "web", "monitor", "home-agents.js")).href);
} catch (e) {
  loadError = e;
}

// The panel's HTML for the recorded snapshot, with `mutate(data)` applied to a copy of its data first.
function render(mutate = () => {}) {
  if (!mod) throw new Error(`home-agents.js does not load: ${loadError && loadError.message}`);
  const s = { fast: structuredClone(fixture), tickets: structuredClone(EMPTY_TICKETS) };
  mutate(s.fast.data);
  return String(mod.agentsPanel(s, {}));
}

// The row that starts at `attr` (a `data-sub="…"` or `data-ticket="…"` marker) up to the next row's start.
function rowAt(html, attr) {
  const at = html.indexOf(attr);
  if (at < 0) return null;
  const next = html.indexOf('<div class="tr', at + attr.length);
  return html.slice(at, next < 0 ? html.length : next);
}

// The text of a row's tick cell (the тикет column), or null when the row has none.
const tickOf = row => (row == null ? null : (/<span class="c-mo">([^<]*)<\/span>/.exec(row)?.[1] ?? null));

// Each named check runs alone: a throw is a failed check with its message, never a crash of the others.
async function run(name, fn) {
  try {
    const r = await fn();
    if (r && typeof r === "object" && "ok" in r) check(name, r.ok, r.detail);
    else check(name, r === true, r === true ? "" : "not true");
  } catch (e) {
    check(name, false, `threw: ${e && e.message}`);
  }
}

const agents = fixture.data.agents;
const isOrch = a => /^orch\b/i.test(String(a.name ?? ""));
const orchs = agents.filter(isOrch);
const orchSubs = orchs.flatMap(a => a.subagents);
const ticketSubs = orchSubs.filter(s => s.issue != null);
const liveTicketSubs = ticketSubs.filter(s => s.busy);
const finishedTicketSubs = ticketSubs.filter(s => !s.busy);
const nonOrchTicketSubs = agents.filter(a => !isOrch(a)).flatMap(a => a.subagents).filter(s => s.issue != null);
const allSubs = agents.flatMap(a => a.subagents);

await run("module_contract", () => {
  if (!mod) return { ok: false, detail: `load: ${loadError && loadError.message}` };
  return { ok: typeof mod.agentsPanel === "function", detail: `agentsPanel is ${typeof mod.agentsPanel}` };
});

await run("card_subtitle_busy_and_idle", () => {
  const html = render();
  const busy = agents.filter(a => a.busy).length;
  const idle = agents.length - busy;
  const form = busy % 10 === 1 && busy % 100 !== 11 ? "работает" : "работают";
  const want = `${busy} ${form}, ${idle} без дела`;
  const got = /<span class="r">([^<]*)<\/span>/.exec(html)?.[1] ?? "";
  return { ok: got === want, detail: `card subtitle "${got}", want "${want}"` };
});

await run("chat_subagent_rows_keep_their_ticket", () => {
  const html = render();
  const bad = nonOrchTicketSubs.filter(s => tickOf(rowAt(html, `data-sub="${s.id}"`)) !== `#${s.issue}`);
  const ok = nonOrchTicketSubs.length > 0 && bad.length === 0;
  return { ok, detail: `${nonOrchTicketSubs.length - bad.length} of ${nonOrchTicketSubs.length} chat subagent rows show their #N` };
});

await run("ticket_subagent_rows_have_no_ticket_number", () => {
  const html = render();
  const bad = liveTicketSubs.filter(s => {
    const row = rowAt(html, `data-sub="${s.id}"`);
    return row == null || tickOf(row) !== "";
  });
  const ok = liveTicketSubs.length > 0 && bad.length === 0;
  return { ok, detail: `${liveTicketSubs.length - bad.length} of ${liveTicketSubs.length} live ticket subagent rows drawn with an empty tick cell` };
});

await run("finished_ticket_subagents_are_counts_not_rows", () => {
  const html = render();
  const shown = finishedTicketSubs.filter(s => html.includes(`data-sub="${s.id}"`));
  const ok = finishedTicketSubs.length > 0 && shown.length === 0;
  return { ok, detail: `${shown.length} finished ticket subagents drawn as rows (of ${finishedTicketSubs.length})` };
});

// One ORCH's own block: from its `data-session` head row to the next session's head (a ticket number may sit under two ORCHs).
function sessionSlice(html, sid) {
  const at = html.indexOf(`data-session="${sid}"`);
  if (at < 0) return "";
  const next = html.indexOf('data-session="', at + 1);
  return html.slice(at, next < 0 ? html.length : next);
}

await run("ticket_row_counts_finished_subagents", () => {
  const html = render();
  const pairs = [];
  for (const a of orchs) {
    const block = sessionSlice(html, a.sid);
    for (const n of new Set(a.subagents.filter(s => s.issue != null).map(s => s.issue))) {
      const nF = a.subagents.filter(s => !s.busy && s.issue === n).length;
      if (nF > 0) pairs.push({ n, nF, ok: (rowAt(block, `data-ticket="${n}"`) ?? "").includes(`${nF} отработал`) });
    }
  }
  const bad = pairs.filter(p => !p.ok);
  const ok = pairs.length > 0 && bad.length === 0;
  return { ok, detail: `${pairs.length - bad.length} of ${pairs.length} ticket rows say how many subagents finished` };
});

await run("all_other_subagents_listed", () => {
  const html = render();
  const want = allSubs.length - finishedTicketSubs.length;
  const rows = (html.match(/data-sub="/g) || []).length;
  return { ok: rows === want, detail: `${rows} of ${want} subagent rows (every subagent but the finished ones under a ticket)` };
});

await run("parked_on_merge_wait_words", () => {
  const html = render(d => {
    const orch = d.agents.find(a => isOrch(a) && a.busy);
    orch.held = [{ number: 900, claimed: false }];
    d.merge = { ...d.merge, waiting: [{ issues: [900] }], running: null, landed: [] };
  });
  const row = rowAt(html, 'data-ticket="900"') ?? "";
  return { ok: row.includes("ждёт мерджа"), detail: `ticket 900 row: ${row.includes("ждёт мерджа") ? "ждёт мерджа" : "no merge wait word"}` };
});

await run("parked_on_needs_info_words", () => {
  const tickets = { at_ms: 0, refresh_s: 15, data: { at: "", goals: [], loose: [], groups: [], issues: {
    "901": { n: 901, title: "waits for an answer", open: true, labels: ["needs-info"], kids: [] },
  } } };
  const s = { fast: structuredClone(fixture), tickets };
  const orch = s.fast.data.agents.find(a => isOrch(a) && a.busy);
  orch.held = [{ number: 901, claimed: false }];
  s.fast.data.merge = { ...s.fast.data.merge, waiting: [], running: null, landed: [] };
  const html = String(mod.agentsPanel(s, {}));
  const row = rowAt(html, 'data-ticket="901"') ?? "";
  return { ok: row.includes("ждёт ответа"), detail: `ticket 901 row: ${row.includes("ждёт ответа") ? "ждёт ответа" : "no needs-info word"}` };
});

await run("escapes_text_from_payload", () => {
  const html = render(d => { d.agents[0].name = "<img src=x onerror=1>"; });
  return { ok: !html.includes("<img src=x") && html.includes("&lt;img"), detail: "session name escaped" };
});

process.stdout.write(JSON.stringify(report));
