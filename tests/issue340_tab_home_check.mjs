// #340 unit 3 (tab Главная): web/monitor/tab-home.js rendered on the recorded `fast` snapshot
// (tests/fixtures/monitor/fast.json, a real pull) and an empty `tickets` snapshot. Plain node 24 ES module: it prints one
// JSON object {check: {ok, detail}} on stdout; tests/test_issue340_tab_home.py runs it and asserts each named check.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const fixture = JSON.parse(readFileSync(join(here, "fixtures", "monitor", "fast.json"), "utf-8"));
const EMPTY_TICKETS = { at_ms: 0, refresh_s: 15, data: { at: "", goals: [], loose: [], groups: [], issues: {} } };
const PANELS = ["agents", "merge", "pc", "footer"];

const report = {};
const check = (name, ok, detail = "") => { report[name] = { ok: !!ok, detail: String(detail) }; };

// A ui stub: the shell's ui (open, toggle, copy, repo, setRepo) as the tab uses it.
function uiStub(openKeys = []) {
  const toggled = [];
  return {
    toggled,
    open: k => openKeys.includes(k),
    toggle: k => { toggled.push(k); },
    copy: () => Promise.resolve(true),
    repo: "test",
    setRepo: () => {},
  };
}

function snap(mutate = () => {}, tickets = EMPTY_TICKETS) {
  const s = { fast: structuredClone(fixture), tickets: structuredClone(tickets) };
  mutate(s.fast.data, s);
  return s;
}

// The HTML of one panel: from its data-panel marker to the next panel's marker (or the end).
function panel(html, name) {
  const at = html.indexOf(`data-panel="${name}"`);
  if (at < 0) return "";
  let end = html.length;
  for (const other of PANELS) {
    if (other === name) continue;
    const k = html.indexOf(`data-panel="${other}"`, at + 1);
    if (k >= 0) end = Math.min(end, k);
  }
  return html.slice(at, end);
}

let tab = null;
let loadError = null;
try {
  tab = await import(pathToFileURL(join(root, "web", "monitor", "tab-home.js")).href);
} catch (e) {
  loadError = e;
}

const render = (s, ui = uiStub()) => {
  if (!tab) throw new Error(`tab-home.js does not load: ${loadError && loadError.message}`);
  return String(tab.render(s, ui));
};

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

const f = fixture.data;
const totalSubs = f.agents.reduce((n, a) => n + a.subagents.length, 0);
const landedBranches = new Set(f.merge.landed.map(l => l.branch)).size;

await run("module_contract", () => {
  if (!tab) return { ok: false, detail: `load: ${loadError && loadError.message}` };
  const endpoints = JSON.stringify(tab.endpoints);
  const ok = tab.id === "home" && tab.title === "Главная" && tab.order === 10 && endpoints === JSON.stringify(["fast", "tickets"])
    && typeof tab.render === "function";
  return { ok, detail: `id=${tab.id} title=${tab.title} order=${tab.order} endpoints=${endpoints}` };
});

await run("rows_1_to_4_present", () => {
  const h = render(snap());
  const found = [1, 2, 3, 4].filter(n => (h.match(new RegExp(`data-row="${n}"`, "g")) || []).length === 1);
  return { ok: found.length === 4, detail: `${found.length} of 4 rows (data-row 1 agents, 2 merge, 3 pc, 4 footer)` };
});

await run("agents_sessions_and_sections", () => {
  const p = panel(render(snap()), "agents");
  const sessions = (p.match(/data-session="/g) || []).length;
  const need = ["Чаты", "Фоновые сессии", "Brain: orch layer", "Opus 5.5", "199k", "30 мин", "2 ч 19 мин", "#341"];
  const miss = need.filter(s => !p.includes(s));
  return { ok: sessions === f.agents.length && miss.length === 0, detail: `${sessions} of ${f.agents.length} sessions; missing: ${miss.join(", ") || "none"}` };
});

await run("agents_whole_subagent_lists", () => {
  const p = panel(render(snap()), "agents");
  const rows = (p.match(/data-sub="/g) || []).length;
  // Under an ORCH ticket the pane shows finished subagents as a count, never rows (register.tsx): in the recorded
  // fixture that is 42 of the 59 subagents, so 17 rows are drawn, all of them whole (no cap).
  const shown = totalSubs - 42;
  return { ok: rows === shown, detail: `${rows} of ${shown} subagent rows (no cap; ${totalSubs - shown} finished ones are a count)` };
});

await run("agents_subagent_titles_and_words", () => {
  const p = panel(render(snap()), "agents");
  const miss = ["pause check in other repos", "Sonnet 5.5", "Haiku 5.5"].filter(s => !p.includes(s));
  return { ok: miss.length === 0, detail: `missing: ${miss.join(", ") || "none"}` };
});

await run("merge_running_queue_header_and_eta", () => {
  const p = panel(render(snap()), "merge");
  const need = ["очередь: 1 идёт, 1 ждёт", "вся ~20 мин, до 19:42", "brain.md: stopgap", "ещё ~10 мин"];
  const miss = need.filter(s => !p.includes(s));
  return { ok: miss.length === 0, detail: `missing: ${miss.join(", ") || "none"}` };
});

await run("merge_waiting_row", () => {
  const p = panel(render(snap()), "merge");
  const need = ["i341-merge", "старт через ~10 мин, займёт ~10 мин", "ждёт 5 мин", "В очереди"];
  const miss = need.filter(s => !p.includes(s));
  return { ok: miss.length === 0, detail: `missing: ${miss.join(", ") || "none"}` };
});

await run("merge_landed_reasons", () => {
  const p = panel(render(snap()), "merge");
  const need = ["не влит в 18:43 — незакоммиченные изменения", "не влит в 16:46 — упали тесты: 1", "влит в "];
  const miss = need.filter(s => !p.includes(s));
  return { ok: miss.length === 0, detail: `missing: ${miss.join(", ") || "none"}` };
});

await run("merge_landed_capped_then_toggle", () => {
  const closed = panel(render(snap(), uiStub()), "merge");
  const nClosed = (closed.match(/data-landed="/g) || []).length;
  const ui = uiStub(["merge-landed"]);
  const openHtml = panel(render(snap(), ui), "merge");
  const nOpen = (openHtml.match(/data-landed="/g) || []).length;
  const ok = nClosed === Math.min(10, landedBranches) && nOpen === landedBranches && closed.includes('data-toggle="merge-landed"');
  return { ok, detail: `closed ${nClosed}, open ${nOpen} of ${landedBranches} landed branches` };
});

await run("merge_lost_rows_from_tickets", () => {
  const tickets = { at_ms: 0, refresh_s: 15, data: { at: "", goals: [], loose: [], groups: [], issues: {
    "341": { n: 341, title: "A changed test kept out of the public test copy", open: true, merge_lost: true, kids: [] },
  } } };
  const p = panel(render(snap(s => {}, tickets)), "merge");
  const none = panel(render(snap()), "merge");
  const ok = p.includes("merge lost") && p.includes("#341") && !none.includes("merge lost");
  return { ok, detail: `lost row drawn with tickets: ${p.includes("merge lost")}; none without: ${!none.includes("merge lost")}` };
});

await run("pc_cpu_ram_split_and_sub", () => {
  const p = panel(render(snap()), "pc");
  const need = ["98 %", "24 / 32 GB", "12 ядер", "agents 8.5 %", "other 34.7 %", "unmeasured 51.6 %", "merge 0 %"];
  const miss = need.filter(s => !p.includes(s));
  const segs = (p.match(/data-seg="cpu-/g) || []).length;
  return { ok: miss.length === 0 && segs === 7, detail: `${segs} of 7 CPU segments; missing: ${miss.join(", ") || "none"}` };
});

await run("footer_time_and_version", () => {
  const p = panel(render(snap()), "footer");
  return { ok: p.includes("обновлено 19:22") && p.includes("v1865"), detail: p.replace(/<[^>]+>/g, "").trim() };
});

await run("merge_error_shown_in_panel", () => {
  const withData = panel(render(snap(d => { d.merge_error = "exit 1: merge-status boom"; })), "merge");
  const noData = panel(render(snap(d => { delete d.merge; d.merge_error = "exit 1: merge-status boom"; })), "merge");
  const ok = withData.includes("exit 1: merge-status boom") && withData.includes("brain.md: stopgap")
    && noData.includes("exit 1: merge-status boom") && !noData.includes("class=\"tbl\"");
  return { ok, detail: `with data: error + rows; without data: error only (${noData.includes("class=\"tbl\"") ? "table drawn" : "no table"})` };
});

await run("agents_error_shown_in_panel", () => {
  const p = panel(render(snap(d => { delete d.agents; d.agents_error = "agents read: timeout"; })), "agents");
  return { ok: p.includes("agents read: timeout") && !p.includes("data-session"), detail: `error line in agents panel: ${p.includes("agents read: timeout")}` };
});

await run("machine_error_shown_in_panel", () => {
  const p = panel(render(snap(d => { delete d.machine; d.machine_error = "psutil: access denied"; })), "pc");
  return { ok: p.includes("psutil: access denied") && !p.includes("data-seg="), detail: `error line in PC panel: ${p.includes("psutil: access denied")}` };
});

await run("version_error_shown_in_footer", () => {
  const p = panel(render(snap(d => { d.version = null; d.version_error = "git rev-list failed"; })), "footer");
  return { ok: p.includes("git rev-list failed"), detail: `footer error: ${p.includes("git rev-list failed")}` };
});

await run("fast_read_failure_keeps_data_and_names_error", () => {
  const s = snap();
  s.fast.error = "сервер не отвечает";
  const h = render(s);
  const footer = panel(h, "footer");
  const agents = panel(h, "agents");
  return { ok: footer.includes("сервер не отвечает") && agents.includes("data-session"), detail: `footer shows error; agents still listed` };
});

await run("no_empty_panel_where_data_exists", () => {
  const h = render(snap());
  const empties = ["agents", "merge", "pc"].filter(n => {
    const p = panel(h, n);
    return !p.includes('class="tbl"') && !p.includes('class="mrow"');
  });
  return { ok: empties.length === 0, detail: `empty panels: ${empties.join(", ") || "none"}` };
});

await run("escapes_text_from_payload", () => {
  const h = render(snap(d => { d.agents[0].name = "<img src=x onerror=1>"; }));
  return { ok: !h.includes("<img src=x") && h.includes("&lt;img"), detail: "name escaped" };
});

await run("no_tickets_snapshot_does_not_crash", () => {
  const s = { fast: structuredClone(fixture) };
  const h = render(s);
  return { ok: h.includes("Merge") && h.includes("data-row=\"2\""), detail: "render without a tickets snapshot" };
});

await run("no_hardcoded_repo_or_path", () => {
  const h = render(snap());
  const bad = ["orchestration-layer", "Rasslabsya4el", "C:\\", "C:/", "suite_out"].filter(s => h.includes(s));
  return { ok: bad.length === 0, detail: `found: ${bad.join(", ") || "none"}` };
});

process.stdout.write(JSON.stringify(report));
