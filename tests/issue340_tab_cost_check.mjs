// Node runner for tests/test_issue340_tab_cost.py (#340 unit 5): renders web/monitor/tab-cost.js from the captured
// collector snapshots (tests/fixtures/monitor/slow.json, orch.json) for every period x grouping, plus the error
// cases, and prints one JSON object on stdout. Usage: node issue340_tab_cost_check.mjs <tab-cost.js> <fixture dir>
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const [, , tabPath, fixtureDir] = process.argv;
const tab = await import(pathToFileURL(resolve(tabPath)).href);
const slow = JSON.parse(readFileSync(resolve(fixtureDir, "slow.json"), "utf8"));
const orch = JSON.parse(readFileSync(resolve(fixtureDir, "orch.json"), "utf8"));

// The shell's answer shape: {at_ms, refresh_s, data, error?} per mode.
const snap = {
  slow: { at_ms: 0, refresh_s: 600, data: slow },
  orch: { at_ms: 0, refresh_s: 600, data: orch },
};
// ui: the period and grouping are carried in ui.state (the contract of this tab); the rest is the shell's ui.
const ui = (period, grouping) => ({
  state: { period, grouping },
  open: () => false,
  toggle: () => {},
  copy: () => Promise.resolve(true),
  repo: null,
  setRepo: () => {},
});

const out = {
  module: { id: tab.id, title: tab.title, order: tab.order, endpoints: tab.endpoints },
  renders: {},
  errors: {},
};
for (const p of ["day", "week", "month"]) {
  for (const g of ["roles", "spawn", "days"]) {
    out.renders[`${p}/${g}`] = String(tab.render(snap, ui(p, g)));
  }
}
// a failed slow read with no cut: the error card carries the reason
out.errors.readFailed = String(tab.render(
  { slow: { at_ms: 0, refresh_s: 600, data: {}, error: "gh: сервер не отвечает" }, orch: snap.orch }, ui("week", "roles")));
// the collector's own cut error for the chosen period, no cut
out.errors.cutFailed = String(tab.render(
  { slow: { at_ms: 0, refresh_s: 600, data: { cost_week: { cut_error: "ValueError('bad cut')" } } }, orch: snap.orch },
  ui("week", "roles")));
// no plan price in the payload: the % column leaves every table
const noPlan = structuredClone(slow);
noPlan.cost_week.cut.total.limit_usd_per_pct = null;
noPlan.cost_week.cut.total.limit_pct = null;
out.errors.noPlan = String(tab.render(
  { slow: { at_ms: 0, refresh_s: 600, data: noPlan }, orch: snap.orch }, ui("week", "roles")));
// nothing answered yet and no error: a waiting line, not an error card
out.errors.waiting = String(tab.render({ slow: { at_ms: 0, refresh_s: 600, data: {} }, orch: snap.orch }, ui("week", "roles")));
// a failed read with the old data still held: the tables are drawn and one line above them says the data is old
out.errors.stale = String(tab.render(
  { slow: { at_ms: 0, refresh_s: 600, data: slow, error: "gh: сервер не отвечает" }, orch: snap.orch }, ui("week", "roles")));
// the chosen repo (ui.repo) names the GitHub label the ticket numbers link to, through snapshot.repos
out.links = String(tab.render(
  { slow: snap.slow, repos: [{ id: "orch", label: "Rasslabsya4el/orchestration-layer" }] },
  { state: { period: "week", grouping: "days" }, repo: "orch", open: () => false, toggle: () => {}, copy: () => Promise.resolve(true), setRepo: () => {} }));

process.stdout.write(JSON.stringify(out));
