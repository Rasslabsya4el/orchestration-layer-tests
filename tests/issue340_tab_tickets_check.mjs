// Node check for web/monitor/tab-tickets.js (#340 unit 4, the Тикеты tab). Run by tests/test_issue340_tab_tickets.py.
// Argument: the fixture path (tests/fixtures/monitor/tickets.json, a real collect.py tickets() capture).
// Prints one JSON object: { <check name>: { ok, detail } }. No DOM, no network.
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const fixturePath = process.argv[2] ?? path.join(root, 'tests', 'fixtures', 'monitor', 'tickets.json');
const fixture = JSON.parse(readFileSync(fixturePath, 'utf-8'));
const data = fixture.tickets.data;
const tab = await import(pathToFileURL(path.join(root, 'web', 'monitor', 'tab-tickets.js')).href);

// The repo list as GET /api/repos gives it (shell.js reposOf): id is the switch's key, label the full owner/name.
const REPO = 'Rasslabsya4el/orchestration-layer';
const REPO_ID = 'orchestration-layer';
const REPOS = [{ id: REPO_ID, label: REPO }];
const EIGHT = ['work', 'owner', 'ready', 'blocked', 'open', 'triage', 'closed', 'dropped'];
const WORD = {
  work: 'held', owner: 'ready-for-human', blocked: 'blocked', ready: 'ready-for-agent',
  triage: 'needs-triage', open: 'open', closed: 'closed', dropped: 'not planned',
};

const results = {};
function check(name, fn) {
  try {
    const detail = fn();
    results[name] = { ok: true, detail: detail == null ? '' : String(detail) };
  } catch (e) {
    results[name] = { ok: false, detail: String((e && e.message) || e) };
  }
}
function eq(got, want, what) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a !== b) throw new Error(`${what}: got ${a}, want ${b}`);
}
function ok(cond, what) {
  if (!cond) throw new Error(what);
}

// --- independent expectations, computed from the fixture alone (not from the tab's code) ---
const issue = n => data.issues[String(n)];
const roots = [...data.goals, ...data.loose];
function kidsOf(n) { return (issue(n).kids || []).filter(k => issue(k)); }
function subtreeNums(n, seen = new Set()) {
  for (const k of kidsOf(n)) if (!seen.has(k)) { seen.add(k); subtreeNums(k, seen); }
  return seen;
}
function allNodes() {
  const seen = new Set();
  for (const r of roots) { seen.add(r); subtreeNums(r, seen); }
  return [...seen];
}
function expectedState(n, held = new Set()) {
  const i = issue(n);
  if (!i.open && i.reason === 'NOT_PLANNED') return 'dropped';
  if (i.open && held.has(n)) return 'work';
  return i.state;
}

// --- html helpers ---
// Rows are the chunks that start at `<div class="tr" data-n=`; the header row has no data-n and is skipped.
function rowsOf(html) {
  return html.split('<div class="tr').slice(1).filter(c => /^" data-n="\d+"/.test(c)).map(c => {
    const attr = name => { const m = new RegExp(` ${name}="([^"]*)"`).exec(c); return m ? m[1] : null; };
    return {
      n: Number(attr('data-n')),
      depth: Number(attr('data-depth')),
      state: attr('data-state'),
      held: attr('data-held'),
      blocked: attr('data-blocked'),
      parked: attr('data-parked'),
      toggle: attr('data-toggle'),
      html: c,
    };
  });
}
function legendEntries(html) {
  const m = /<div class="mkey" data-legend="1">([\s\S]*?)<\/div>/.exec(html);
  if (!m) return null;
  return [...m[1].matchAll(/<span data-state="([^"]+)">([\s\S]*?)<\/span>/g)].map(x => ({ state: x[1], text: x[2].replace(/<[^>]+>/g, '').trim() }));
}

function render(snapshot, ui) { return tab.render(snapshot, ui); }
const snapOf = (extra = {}) => ({ tickets: fixture.tickets, repos: REPOS, ...extra });
// The ui the shell gives (shell.js createShell): open(key), toggle, copy, repo (the selected repo id), setRepo.
const uiWith = (openKeys, repo = REPO_ID) => {
  const set = new Set(openKeys);
  return { open: k => set.has(k), toggle: () => {}, copy: () => Promise.resolve(true), repo, setRepo: () => {} };
};
const allOpenKeys = () => allNodes().filter(n => kidsOf(n).length > 0).map(n => `tk-${n}`);

// --- module contract ---
check('module_exports', () => {
  eq([tab.id, tab.title, tab.order, tab.endpoints], ['tickets', 'Тикеты', 20, ['tickets', 'fast']], 'exports');
  ok(typeof tab.render === 'function', 'render is a function');
});

// --- collapsed: every goal and loose ticket rendered once, with its row fields ---
const collapsedHtml = render(snapOf(), uiWith([]));
const collapsedRows = rowsOf(collapsedHtml);

check('legend_eight_states', () => {
  const entries = legendEntries(collapsedHtml);
  ok(entries, 'legend block (data-legend) present');
  eq(entries.map(e => e.state), EIGHT, 'legend states in pane order');
  for (const e of entries) eq(e.text, WORD[e.state], `legend word for ${e.state}`);
  return `${entries.length} of 8 states`;
});

check('section_titles', () => {
  ok(collapsedHtml.includes('Цели'), 'Цели card');
  ok(collapsedHtml.includes('Вне целей'), 'Вне целей card');
});

check('roots_rendered_n_of_n', () => {
  const top = collapsedRows.filter(r => r.depth === 0);
  const got = top.map(r => r.n).sort((a, b) => a - b);
  const want = [...roots].sort((a, b) => a - b);
  eq(got, want, 'root rows');
  return `${top.length} of ${roots.length} (goals ${data.goals.length} + loose ${data.loose.length})`;
});

check('root_rows_state_dot', () => {
  for (const r of collapsedRows.filter(x => x.depth === 0)) {
    eq(r.state, expectedState(r.n), `state of #${r.n}`);
    ok(/<i class="d" style="[^"]+"><\/i>/.test(r.html), `state dot of #${r.n}`);
  }
  return `${roots.length} of ${roots.length} dots`;
});

check('root_rows_link_from_repo', () => {
  for (const r of collapsedRows.filter(x => x.depth === 0)) {
    ok(r.html.includes(`href="https://github.com/${REPO}/issues/${r.n}"`), `link of #${r.n}`);
  }
  return `${roots.length} of ${roots.length} links to ${REPO}`;
});

check('root_rows_done_count', () => {
  let withKids = 0;
  for (const r of collapsedRows.filter(x => x.depth === 0)) {
    const all = [...subtreeNums(r.n)];
    if (all.length === 0) { ok(!/class="num">\d+\/\d+</.test(r.html), `no done count for leaf #${r.n}`); continue; }
    withKids++;
    const closed = all.filter(k => !issue(k).open).length;
    ok(r.html.includes(`class="num">${closed}/${all.length}<`), `done ${closed}/${all.length} of #${r.n}`);
  }
  return `${withKids} trees with done/total`;
});

check('collapsed_hides_children', () => {
  const deeper = collapsedRows.filter(r => r.depth > 0).length;
  eq(deeper, 0, 'child rows while collapsed');
  for (const r of collapsedRows.filter(x => x.depth === 0 && kidsOf(x.n).length > 0)) {
    eq(r.toggle, `tk-${r.n}`, `toggle key of #${r.n}`);
    ok(r.html.includes('aria-expanded="false"'), `collapsed toggle of #${r.n}`);
  }
  return `0 child rows, ${roots.filter(n => kidsOf(n).length > 0).length} toggles collapsed`;
});

// --- expanded: every node of every tree rendered once, marks and counts on each ---
const openedHtml = render(snapOf(), uiWith(allOpenKeys()));
const openedRows = rowsOf(openedHtml);
const nodes = allNodes();

check('expanded_all_n_of_n', () => {
  const got = openedRows.map(r => r.n).sort((a, b) => a - b);
  eq(got, [...nodes].sort((a, b) => a - b), 'every node of every tree');
  return `${openedRows.length} of ${nodes.length} nodes`;
});

check('expanded_children_depth_and_state', () => {
  for (const r of openedRows) {
    eq(r.state, expectedState(r.n), `state of #${r.n}`);
  }
  // a direct child sits one level under its parent
  const byN = new Map(openedRows.map(r => [r.n, r]));
  for (const g of data.goals) for (const k of kidsOf(g)) eq(byN.get(k).depth, 1, `depth of child #${k}`);
  return `${openedRows.length} of ${nodes.length} states`;
});

check('blocked_marks', () => {
  // The mark is for an open ticket with blockers whose drawn state is not `blocked` (the dot already says blocked).
  const withBlockers = n => issue(n).open && issue(n).blockers.length > 0;
  const want = nodes.filter(n => withBlockers(n) && expectedState(n) !== 'blocked').sort((a, b) => a - b);
  const dotBlocked = nodes.filter(n => withBlockers(n) && expectedState(n) === 'blocked');
  ok(dotBlocked.length > 0, 'fixture has an open blocked-state ticket with blockers (the rule is exercised)');
  const got = openedRows.filter(r => r.blocked === '1').map(r => r.n).sort((a, b) => a - b);
  eq(got, want, 'blocked rows');
  return `${got.length} of ${want.length} open blockers marked; ${dotBlocked.length} blocked-state tickets left to the dot`;
});

check('parked_marks', () => {
  const want = nodes.filter(n => issue(n).parked).sort((a, b) => a - b);
  const got = openedRows.filter(r => r.parked === '1').map(r => r.n).sort((a, b) => a - b);
  eq(got, want, 'parked rows');
  return `${got.length} of ${want.length} parked tickets marked`;
});

check('held_marks_from_fast', () => {
  // A synthetic fast read: one ORCH agent holds two open tickets (one as a number, one as {number}).
  const openNodes = nodes.filter(n => issue(n).open);
  const a = openNodes[0], b = openNodes[1];
  const fast = { at_ms: 1, refresh_s: 2, data: { agents: [{ busy: true, issue: a, held: [{ number: b }], subagents: [] }], runs: { running: [] } } };
  const html = render({ ...snapOf(), fast }, uiWith(allOpenKeys()));
  const rows = rowsOf(html);
  const held = new Set([a, b]);
  for (const n of held) {
    const r = rows.find(x => x.n === n);
    ok(r, `held #${n} rendered`);
    eq(r.held, '1', `held mark of #${n}`);
    eq(r.state, 'work', `state of held #${n}`);
    ok(r.html.includes('◆'), `held glyph of #${n}`);
  }
  eq(rows.filter(r => r.held === '1').length, held.size, 'held rows');
  return `${held.size} held tickets, state work and ◆ mark`;
});

check('no_held_without_fast', () => {
  eq(openedRows.filter(r => r.held === '1').length, 0, 'held rows without a fast read');
  return 'no fast read: no held marks';
});

// A failed or malformed fast read never throws and never marks a ticket as held; every row is still drawn.
const BAD_FAST = {
  'data null with error': { at_ms: 1, refresh_s: 2, data: null, error: 'collector timed out' },
  'agents not an array': { at_ms: 1, refresh_s: 2, data: { agents: 'bad' } },
  'runs.running not an array': { at_ms: 1, refresh_s: 2, data: { agents: [], runs: { running: 7 } } },
};
check('fast_failure_no_held', () => {
  for (const [label, fast] of Object.entries(BAD_FAST)) {
    const rows = rowsOf(render({ ...snapOf(), fast }, uiWith(allOpenKeys())));
    eq(rows.length, nodes.length, `${label}: rows drawn`);
    eq(rows.filter(r => r.held === '1').length, 0, `${label}: held rows`);
  }
  return `${Object.keys(BAD_FAST).length} bad fast reads: ${nodes.length} of ${nodes.length} rows, no held marks`;
});

check('stale_fast_error_no_held', () => {
  // Data kept from an earlier read but flagged with an error: no ticket is drawn as held from it.
  const a = nodes.find(n => issue(n).open);
  const fast = { at_ms: 1, refresh_s: 2, error: 'collector timed out', data: { agents: [{ busy: true, issue: a, held: [], subagents: [] }], runs: { running: [] } } };
  const rows = rowsOf(render({ ...snapOf(), fast }, uiWith(allOpenKeys())));
  eq(rows.filter(r => r.held === '1').length, 0, 'held rows from data with an error');
  eq(rows.length, nodes.length, 'rows still drawn');
  return 'data with error: no held marks';
});

check('ui_state_map_expands_nothing', () => {
  // A ui carrying only a `state` map (not the shell's open(key)) draws the roots and no child row.
  const state = Object.fromEntries(allOpenKeys().map(k => [k, true]));
  const rows = rowsOf(render(snapOf(), { state, repo: REPO_ID }));
  eq(rows.filter(r => r.depth > 0).length, 0, 'child rows from a state map');
  eq(rows.length, roots.length, 'root rows still drawn');
  return '0 child rows from a state map';
});

// --- the repo: the link is https://github.com/<label>/issues/<n> from the entry whose id is the chosen repo ---
check('no_link_without_repo', () => {
  // ui.repo null: no repo is chosen, so no link; rows still drawn.
  const html = render({ tickets: fixture.tickets, repos: REPOS }, uiWith([], null));
  ok(!html.includes('github.com'), 'no github link when no repo is chosen');
  ok(rowsOf(html).length === roots.length, 'rows still rendered without a repo');
  return 'rows rendered as plain numbers';
});

check('no_link_for_bare_label', () => {
  // A label with no owner (no `/`) gives no link; the number stays plain.
  const html = render({ tickets: fixture.tickets, repos: [{ id: REPO_ID, label: REPO_ID }] }, uiWith([], REPO_ID));
  ok(!html.includes('github.com'), 'no link from a label without an owner');
  ok(rowsOf(html).length === roots.length, 'rows still rendered');
  return 'label without / gives plain numbers';
});

check('no_link_for_unknown_repo', () => {
  // ui.repo is not in the repo list: no entry, so no link.
  const html = render({ tickets: fixture.tickets, repos: REPOS }, uiWith([], 'missing-repo'));
  ok(!html.includes('github.com'), 'no link when ui.repo is not listed');
  return 'unlisted repo gives plain numbers';
});

check('link_follows_chosen_repo', () => {
  // The link comes from the label of the entry whose id is the chosen repo, never from a fixed one.
  const other = { id: 'other-repo', label: 'example-owner/other-repo' };
  const html = render({ tickets: fixture.tickets, repos: [...REPOS, other] }, uiWith([], other.id));
  ok(html.includes(`href="https://github.com/${other.label}/issues/`), 'link to the chosen repo');
  ok(!html.includes(`https://github.com/${REPO}/issues/`), 'no link to the other repo');
  return `links go to ${other.label}`;
});

// --- error: the error text shows, never an empty tree ---
check('error_panel_without_data', () => {
  const html = render({ tickets: { at_ms: 1, refresh_s: 15, data: null, error: 'gh: rate limit exceeded' } }, uiWith([]));
  ok(html.includes('data-error="1"'), 'error panel');
  ok(html.includes('gh: rate limit exceeded'), 'error text shown');
  eq(rowsOf(html).length, 0, 'rows without data');
  return 'error text in the panel, no rows';
});

check('error_keeps_last_good_tree', () => {
  const html = render({ tickets: { ...fixture.tickets, error: 'gh timed out' }, repos: REPOS }, uiWith([]));
  ok(html.includes('data-error="1"') && html.includes('gh timed out'), 'error text shown');
  eq(rowsOf(html).length, roots.length, 'last good rows still drawn');
  return `error text + ${roots.length} of ${roots.length} rows`;
});

check('error_text_escaped', () => {
  const html = render({ tickets: { at_ms: 1, refresh_s: 15, data: null, error: '<b>x</b>' } }, uiWith([]));
  ok(html.includes('&lt;b&gt;x&lt;/b&gt;') && !html.includes('<b>x</b>'), 'error text escaped');
  return 'escaped';
});

check('missing_snapshot_is_loading_not_empty', () => {
  const html = render({}, uiWith([]));
  ok(html.includes('загружается'), 'loading text');
  eq(rowsOf(html).length, 0, 'rows');
  return 'загружается';
});

check('held_notice_when_fast_fails', () => {
  // A fast read that failed (an error, or no data) shows one notice line above the legend; a good read shows none.
  const notice = html => /<div[^>]*data-held-notice="1"[^>]*>([\s\S]*?)<\/div>/.exec(html);
  const cases = [
    [{ at_ms: 1, refresh_s: 2, data: null, error: 'collector timed out' }, 'Метки «held» недоступны: collector timed out'],
    [{ at_ms: 1, refresh_s: 2, data: null }, 'Метки «held» недоступны: нет данных'],
    [{ at_ms: 1, refresh_s: 2, data: { agents: [] }, error: '<b>x</b>' }, 'Метки «held» недоступны: &lt;b&gt;x&lt;/b&gt;'],
  ];
  for (const [fast, want] of cases) {
    const html = render({ ...snapOf(), fast }, uiWith([]));
    const m = notice(html);
    ok(m, `notice for ${JSON.stringify(fast)}`);
    eq(m[1], want, 'notice text');
    ok(html.indexOf('data-held-notice') < html.indexOf('data-legend'), 'notice above the legend');
  }
  const good = render({ ...snapOf(), fast: { at_ms: 1, refresh_s: 2, data: { agents: [], runs: { running: [] } } } }, uiWith([]));
  ok(!notice(good), 'no notice on a good fast read');
  ok(!render(snapOf(), uiWith([])).includes('data-held-notice'), 'no notice when the snapshot has no fast read');
  return `${cases.length} failed reads: notice text above the legend; none on a good read`;
});

process.stdout.write(JSON.stringify(results));
