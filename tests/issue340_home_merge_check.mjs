// #340 unit 3b review-fix (Главная, Merge panel): web/monitor/home-merge.js and web/monitor/home-util.js, rendered on the
// recorded `fast` snapshot (tests/fixtures/monitor/fast.json, a real pull) and on synthetic variants of its merge read.
// Plain node 24 ES module: it prints one JSON object {check: {ok, detail}} on stdout;
// tests/test_issue340_home_merge.py runs it and asserts each named check.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const fixture = JSON.parse(readFileSync(join(here, "fixtures", "monitor", "fast.json"), "utf-8"));
const EMPTY_TICKETS = { at_ms: 0, refresh_s: 15, data: { at: "", goals: [], loose: [], groups: [], issues: {} } };

const report = {};
const check = (name, ok, detail = "") => { report[name] = { ok: !!ok, detail: String(detail) }; };

// A ui stub: the shell's ui (open, toggle, copy, repo, setRepo) as the panel uses it.
function uiStub(openKeys = []) {
  return {
    open: k => openKeys.includes(k),
    toggle: () => {},
    copy: () => Promise.resolve(true),
    repo: "test",
    setRepo: () => {},
  };
}

// A snapshot: a fresh copy of the fixture, `mutate(data, snapshot)` applied to it, the tickets read as given.
function snap(mutate = () => {}, tickets = EMPTY_TICKETS) {
  const s = { fast: structuredClone(fixture), tickets: structuredClone(tickets) };
  mutate(s.fast.data, s);
  return s;
}

const baseMerge = () => structuredClone(fixture.data.merge);
// The fixture's merge read with the given keys replaced (the rest of the read unchanged).
const mergeWith = over => snap(d => { d.merge = { ...baseMerge(), ...over }; });
// A running holder with the fields the collector sends, overridden by `over`.
const running = over => ({
  kind: "merge", branches: ["i342"], issues: [342], phase: "suite", started: "", queued: null,
  percent: null, tests_done: null, tests_total: null, failed: 0, eta_h: "5 min", elapsed_h: "3 min", ...over,
});
// The full run on main with the fields the collector sends, overridden by `over`.
const fullRun = over => ({ commit: "abc1234", percent: 40, failed: 0, elapsed_s: 300, eta_s: 600, ...over });

// Only the text of the HTML, tags dropped.
const text = html => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

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

// A module of web/monitor/ loaded by its file name, or null with the load error kept for the detail.
async function loadModule(file) {
  try {
    return { mod: await import(pathToFileURL(join(root, "web", "monitor", file)).href), error: null };
  } catch (e) {
    return { mod: null, error: e };
  }
}

const home = await loadModule("home-merge.js");
const util = await loadModule("home-util.js");

const render = (s, ui = uiStub()) => {
  if (!home.mod) throw new Error(`home-merge.js does not load: ${home.error && home.error.message}`);
  return String(home.mod.mergePanel(s, ui));
};

const fixtureMerge = fixture.data.merge;

await run("module_contract", () => {
  if (!home.mod) return { ok: false, detail: `load: ${home.error && home.error.message}` };
  const h = render(snap());
  const ok = typeof home.mod.mergePanel === "function" && h.includes('data-panel="merge"') && h.includes('data-row="2"');
  return { ok, detail: `mergePanel is a function: ${typeof home.mod.mergePanel === "function"}` };
});

// 1. A progress track under the running merge: the bar's fill is tests done / total.
await run("running_track_from_tests", () => {
  const h = render(mergeWith({ running: running({ tests_done: 50, tests_total: 200 }) }));
  const ok = h.includes('data-track="25"') && h.includes('class="bar"');
  return { ok, detail: `track at 25 % (50 / 200): ${h.includes('data-track="25"')}` };
});

await run("running_track_falls_back_to_percent", () => {
  const h = render(mergeWith({ running: running({ tests_done: null, tests_total: null, percent: 40 }) }));
  return { ok: h.includes('data-track="40"'), detail: `track at the read's own percent 40: ${h.includes('data-track="40"')}` };
});

await run("running_track_empty_is_zero", () => {
  const h = render(snap());
  return { ok: h.includes('data-track="0"'), detail: `fixture commit with no tests read: track at 0: ${h.includes('data-track="0"')}` };
});

await run("full_run_track_and_row", () => {
  const h = render(mergeWith({ full_run: fullRun() }));
  const t = text(h);
  const need = ["full run on main abc1234", "тесты, ещё ~10 мин", "5 мин"];
  const miss = need.filter(s => !t.includes(s));
  const ok = miss.length === 0 && h.includes('data-track="40"');
  return { ok, detail: `missing: ${miss.join(", ") || "none"}; track 40: ${h.includes('data-track="40"')}` };
});

// 2. The commit holder lists every issue of its commit.
await run("commit_holder_lists_every_issue", () => {
  const h = render(mergeWith({ running: running({ kind: "commit", branches: [], issues: [344, 345], message: "two tickets" }) }));
  return { ok: text(h).includes("#344 #345"), detail: `tick cell of the commit: ${text(h).includes("#344 #345") ? "#344 #345" : "not both"}` };
});

// 3. Checks the original tests lack.
await run("core_update_running_row", () => {
  const h = render(mergeWith({ running: running({ kind: "core-update", branches: [], issues: [], phase: "push", sha: "0a1b2c3d", elapsed_h: "1 min", eta_h: null }) }));
  const t = text(h);
  const need = ["обновление ядра 0a1b2c3d", "пуш"];
  const miss = need.filter(s => !t.includes(s));
  const ok = miss.length === 0 && h.includes('data-track="0"');
  return { ok, detail: `missing: ${miss.join(", ") || "none"}; track drawn: ${h.includes('data-track="0"')}` };
});

await run("core_update_waiting_row", () => {
  const core = { kind: "core-update", branch: null, sha: "0a1b2c3d", issues: [], position: 2, eta_start_h: "10 min", takes_h: "3 min", waited_h: "2 min" };
  const h = render(mergeWith({ waiting: [core] }));
  const t = text(h);
  const need = ["обновление ядра 0a1b2c3d", "старт через ~10 мин, займёт ~3 мин", "ждёт 2 мин"];
  const miss = need.filter(s => !t.includes(s));
  return { ok: miss.length === 0, detail: `missing: ${miss.join(", ") || "none"}` };
});

await run("failed_running_merge", () => {
  const h = render(mergeWith({ running: running({ failed: 2, tests_done: 150, tests_total: 200, eta_h: "4 min" }) }));
  const t = text(h);
  const need = ["упало 2, ещё ~4 мин"];
  const miss = need.filter(s => !t.includes(s));
  const ok = miss.length === 0 && h.includes('class="d stuck"') && h.includes('data-track="75"');
  return { ok, detail: `missing: ${miss.join(", ") || "none"}; stuck dot: ${h.includes('class="d stuck"')}; track 75: ${h.includes('data-track="75"')}` };
});

await run("failed_full_run", () => {
  const h = render(mergeWith({ full_run: fullRun({ failed: 1 }) }));
  return { ok: text(h).includes("упало 1") && h.includes('class="d stuck"'), detail: `упало 1 on the full run: ${text(h).includes("упало 1")}` };
});

// 3 (cont.) A read that fails while the earlier data is held: the error is named, the rows stay.
await run("tickets_error_shown_with_rows", () => {
  const s = snap();
  s.tickets = { at_ms: 0, refresh_s: 15, error: "tickets read: timeout" };
  const t = text(render(s));
  const ok = t.includes("tickets read: timeout") && t.includes("i341-merge") && t.includes("brain.md: stopgap");
  return { ok, detail: `error named: ${t.includes("tickets read: timeout")}; rows kept: ${t.includes("i341-merge")}` };
});

await run("merge_error_keeps_last_good", () => {
  const h = render(snap(d => { d.merge_error = "exit 1: merge-status boom"; }));
  const t = text(h);
  const ok = t.includes("exit 1: merge-status boom") && t.includes("brain.md: stopgap") && t.includes("i341-merge");
  return { ok, detail: `error named: ${t.includes("exit 1: merge-status boom")}; last-good rows kept: ${t.includes("brain.md: stopgap")}` };
});

await run("fast_error_keeps_last_good", () => {
  const s = snap();
  s.fast.error = "сервер не отвечает";
  const t = text(render(s));
  const ok = t.includes("сервер не отвечает") && t.includes("brain.md: stopgap");
  return { ok, detail: `error named: ${t.includes("сервер не отвечает")}; rows kept: ${t.includes("brain.md: stopgap")}` };
});

// 4. One duration rule, in one shared module.
await run("human_durations_one_rule", () => {
  if (!util.mod) return { ok: false, detail: `home-util.js does not load: ${util.error && util.error.message}` };
  const { dur, ru, ageS } = util.mod;
  const cases = [
    [dur(0), "0 с"], [dur(89), "89 с"], [dur(90), "2 мин"], [dur(5400), "1 ч 30 мин"], [dur(7200), "2 ч"], [dur(7680), "2 ч 8 мин"],
    [ru("2 h 08 min"), "2 ч 8 мин"], [ru("1.9 min"), "2 мин"], [ru("4 s"), "4 с"], [ru("-"), "-"], [ageS("2 h 08 min"), 7680],
  ];
  const bad = cases.filter(([got, want]) => got !== want).map(([got, want]) => `${JSON.stringify(got)} != ${JSON.stringify(want)}`);
  return { ok: bad.length === 0, detail: bad.join("; ") || `${cases.length} of ${cases.length} as written` };
});

await run("no_local_duration_copies", () => {
  const src = readFileSync(join(root, "web", "monitor", "home-merge.js"), "utf-8");
  const imports = /from\s+"\.\/home-util\.js"/.test(src);
  const local = /(function|const)\s+(ageS|dur|ru)\b\s*[=(]/.test(src);
  return { ok: imports && !local && util.mod !== null, detail: `imports home-util: ${imports}; local copy left: ${local}` };
});

await run("no_bare_seconds_in_panel", () => {
  const big = snap(d => {
    d.merge = {
      ...baseMerge(),
      running: running({ elapsed_h: "2 h 08 min", eta_h: "1 h 5 min" }),
      full_run: fullRun({ elapsed_s: 1234, eta_s: 2000 }),
      waiting: [{ ...fixtureMerge.waiting[0], waited_h: "1 h 35 min", eta_start_h: "2 h 2 min", takes_h: "72 min" }],
    };
  });
  const t = text(render(big));
  const secs = [...t.matchAll(/(\d+)\s*с\b/g)].map(m => Number(m[1]));
  const bad = secs.filter(n => n >= 90);
  // 72 min stays minutes (under 90 min); 95 min and 122 min are hours and minutes
  const want = ["21 мин", "2 ч 8 мин", "33 мин", "1 ч 35 мин", "2 ч 2 мин", "72 мин"];
  const miss = want.filter(s => !t.includes(s));
  return { ok: bad.length === 0 && miss.length === 0, detail: `seconds drawn: ${secs.join(", ") || "none"}; at or over 90 s: ${bad.join(", ") || "none"}; missing minutes: ${miss.join(", ") || "none"}` };
});

await run("escapes_text_from_payload", () => {
  const h = render(mergeWith({ running: running({ branches: ["<img src=x>"] }) }));
  return { ok: !h.includes("<img src=x>") && h.includes("&lt;img"), detail: "branch name escaped" };
});

await run("no_hardcoded_repo_or_path", () => {
  const h = render(snap());
  const bad = ["orchestration-layer", "Rasslabsya4el", "C:\\", "C:/", "suite_out"].filter(s => h.includes(s));
  return { ok: bad.length === 0, detail: `found: ${bad.join(", ") || "none"}` };
});

process.stdout.write(JSON.stringify(report));
