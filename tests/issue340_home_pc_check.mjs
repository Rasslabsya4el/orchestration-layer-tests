// #340 unit 3c review-fix (web/monitor/home-pc.js and home-footer.js) rendered on the recorded `fast` snapshot
// (tests/fixtures/monitor/fast.json, a real pull, its `at` carries seconds). Plain node 24 ES module: it prints one JSON
// object {check: {ok, detail}} on stdout; tests/test_issue340_home_pc.py runs it and asserts each named check.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const fixture = JSON.parse(readFileSync(join(here, "fixtures", "monitor", "fast.json"), "utf-8"));

const report = {};
const check = (name, ok, detail = "") => { report[name] = { ok: !!ok, detail: String(detail) }; };

let pc = null, footer = null, loadError = null;
try {
  pc = await import(pathToFileURL(join(root, "web", "monitor", "home-pc.js")).href);
} catch (e) {
  loadError = e;
}
try {
  footer = await import(pathToFileURL(join(root, "web", "monitor", "home-footer.js")).href);
} catch (e) {
  loadError = loadError || e;
}

// A ui stub WITH a `state` object: the shell's ui as the panels use it (state is the tab's own keys, kept across renders).
function uiStub() {
  return {
    state: {},
    open: () => false,
    toggle: () => {},
    set: () => {},
    copy: () => Promise.resolve(true),
    repo: "test",
    setRepo: () => {},
  };
}

// The PC panel on the fixture; `mutate(data)` edits the copy of the pull, `at_ms` is the pull's time.
function pcHtml(mutate = () => {}, ui = uiStub(), at_ms = fixture.at_ms) {
  if (!pc) throw new Error(`home-pc.js does not load: ${loadError && loadError.message}`);
  const fast = structuredClone(fixture);
  fast.at_ms = at_ms;
  mutate(fast.data);
  return String(pc.pcPanel({ fast: fast }, ui));
}

function footerHtml(mutate = () => {}) {
  if (!footer) throw new Error(`home-footer.js does not load: ${loadError && loadError.message}`);
  const fast = structuredClone(fixture);
  mutate(fast.data);
  return String(footer.footerPanel({ fast: fast }, uiStub()));
}

// The visible text of an HTML fragment: tags out, spaces collapsed.
const text = html => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

// The text of one legend (`data-legend="cpu"` or `"ram"`), or null when it is not drawn.
function legend(html, which) {
  const m = html.match(new RegExp(`<div class="mkey" data-legend="${which}">([\\s\\S]*?)</div>`));
  return m ? text(m[1]) : null;
}

// Each named check runs alone: a throw is a failed check with its message, never a crash of the others.
function run(name, fn) {
  try {
    const r = fn();
    if (r && typeof r === "object" && "ok" in r) check(name, r.ok, r.detail);
    else check(name, r === true, r === true ? "" : "not true");
  } catch (e) {
    check(name, false, `threw: ${e && e.message}`);
  }
}

const count = (html, re) => (html.match(re) || []).length;

run("pc_ram_legend_in_gb_under_bar", () => {
  const h = pcHtml();
  const leg = legend(h, "ram");
  const under = h.indexOf('data-seg="ram-') >= 0 && h.indexOf('data-legend="ram"') > h.indexOf('data-seg="ram-');
  const need = ["agents 6.4 GB", "Claude app 2.9 GB", "other 12.7 GB", "unmeasured 1.8 GB"];
  const miss = need.filter(s => !leg || !leg.includes(s));
  return { ok: leg !== null && under && miss.length === 0, detail: `RAM legend: ${leg}; under its bar: ${under}; missing: ${miss.join(", ") || "none"}` };
});

run("pc_ram_legend_skips_visually_zero_parts", () => {
  // fixture: work 0.0 GB, tests 0.0 GB, merge 22 MB (0.02 GB, under 0.5 % of the bar): none of them is in the legend.
  const leg = legend(pcHtml(), "ram") || "";
  const zero = ["work", "tests", "merge"].filter(s => leg.includes(s));
  // a merge of 300 MB (0.3 GB, about 0.9 % of the bar) is a visible part: it is in the legend as `0.3 GB`.
  const big = legend(pcHtml(d => { d.machine.merge = { cpu: 0, cpu_raw: 0, ram_mb: 300 }; }), "ram") || "";
  const shown = big.includes("merge 0.3 GB");
  return { ok: zero.length === 0 && shown, detail: `zero parts drawn: ${zero.join(", ") || "none"}; 300 MB merge legend: ${big}` };
});

run("pc_cpu_legend_stays_percent", () => {
  const h = pcHtml();
  const leg = legend(h, "cpu");
  const need = ["agents 8.5 %", "other 34.7 %", "unmeasured 51.6 %", "merge 0 %"];
  const miss = need.filter(s => !leg || !leg.includes(s));
  const gb = leg && leg.includes("GB");
  return { ok: leg !== null && miss.length === 0 && !gb, detail: `CPU legend: ${leg}; missing: ${miss.join(", ") || "none"}` };
});

run("pc_history_no_merge_part_without_share", () => {
  const ui = uiStub();
  pcHtml(() => {}, ui, 1000000);
  const h = pcHtml(() => {}, ui, 1002000);
  const cols = count(h, /data-hist="work"/g);
  const merges = count(h, /data-hist="merge"/g);
  return { ok: cols === 2 && merges === 0, detail: `history columns ${cols}, merge parts ${merges} (no bucket holds a merge share)` };
});

run("pc_history_merge_part_with_share", () => {
  const ui = uiStub();
  pcHtml(() => {}, ui, 1000000);
  const h = pcHtml(d => { d.machine.merge.cpu = 4; }, ui, 1002000);
  const cols = count(h, /data-hist="work"/g);
  const merges = count(h, /data-hist="merge"/g);
  return { ok: cols === 2 && merges === 2, detail: `history columns ${cols}, merge parts ${merges} (one bucket holds a merge share: merge drawn in every column, as the pane does)` };
});

run("pc_no_facts_block", () => {
  const h = pcHtml();
  const bad = ["свободно RAM", "merge RAM", "mfacts"].filter(s => h.includes(s));
  return { ok: bad.length === 0, detail: `extra fact block text: ${bad.join(", ") || "none"}` };
});

run("footer_time_cut_to_hh_mm", () => {
  // the fixture's `at` is 19:22:04: the footer draws 19:22 and no seconds
  const h = footerHtml();
  const t = text(h);
  const ok = t.includes("обновлено 19:22") && !t.includes(":04") && !t.includes("19:22:");
  return { ok, detail: `footer text: ${t}` };
});

run("footer_version_bare_v_when_integer", () => {
  const h = footerHtml();
  const bare = /<span[^>]*>v1865<\/span>/.test(h);
  const orchLabel = h.includes("ORCH");
  return { ok: bare && !orchLabel, detail: `footer draws a bare v1865 span: ${bare}; ORCH label drawn: ${orchLabel}` };
});

run("footer_version_not_integer_draws_nothing", () => {
  const h = footerHtml(d => { d.version = 1.5; });
  const vSpan = /<span[^>]*>v[\d.]+<\/span>/.test(h);
  const ok = !vSpan && !h.includes("ORCH") && text(h).includes("обновлено 19:22");
  return { ok, detail: `non-integer version drawn: ${vSpan}; footer text: ${text(h)}` };
});

run("footer_version_error_still_shown", () => {
  const h = footerHtml(d => { d.version = null; d.version_error = "git rev-list failed"; });
  return { ok: h.includes("git rev-list failed"), detail: `footer error: ${h.includes("git rev-list failed")}` };
});

process.stdout.write(JSON.stringify(report));
