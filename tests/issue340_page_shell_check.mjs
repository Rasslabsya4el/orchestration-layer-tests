// Node check for web/monitor/shell.js (#340 unit 2, the page shell). Run by tests/test_issue340_page_shell.py.
// Prints one JSON object: { <check name>: { ok, detail } }. No DOM, no network: the driver gets stubs.
import {
  sortTabs, pollIntervalMs, bannerText, bannerFor, tabFromHash, parseHash, hashFor, repoFromHash, reposOf,
  tipPlace, esc, copyControl, createShell, captureView, restoreView, DEFAULT_REFRESH_S,
} from '../web/monitor/shell.js';
import * as shellModule from '../web/monitor/shell.js';   // parseSet is read through the namespace: a missing export fails one check only

const REPO_MS = DEFAULT_REFRESH_S.repos * 1000;

const results = {};

async function check(name, fn) {
  try {
    const detail = await fn();
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

const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() {
  for (let i = 0; i < 20; i++) await tick();
}

// Runs every pending timer set for `ms` (a timer set by a run is not run here) and settles.
async function fireDue(pending, ms) {
  const due = pending.filter(p => p.ms === ms);
  for (const p of due) pending.splice(pending.indexOf(p), 1);
  for (const p of due) await p.fn();
  await settle();
}

// The poll intervals waiting, without the repo list's own refresh.
const pollMs = pending => pending.map(p => p.ms).filter(ms => ms !== REPO_MS);

// tabs: id -> tab module; answers: url (with query) or path -> value | Error | Promise | (callNumber) => value
function harness({ tabs, answers = {}, copy = () => true, hash: startHash = '', failImport = () => false }) {
  const pending = [];
  let hashValue = startHash;
  const clock = {
    setTimeout(fn, ms) { pending.push({ fn, ms }); return pending.length; },
    clearTimeout() {},
    now: () => 0,
  };
  const doc = {
    tabs: null, active: null, stages: [], banner: null, repos: 'unset', selected: null, hashWritten: undefined,
    setTabs(list, active) { this.tabs = list; this.active = active; },
    setStage(html) { this.stages.push(html); },
    setBanner(text) { this.banner = text; },
    setRepos(list, selected) { this.repos = list; this.selected = selected; },
    setHash(h) { hashValue = h; this.hashWritten = h; },
  };
  const allAnswers = { '/api/repos': [], ...answers };
  const hits = {};
  const copied = [];
  const fetchJson = async url => {
    hits[url] = (hits[url] || 0) + 1;
    const path = url.split('?')[0];
    const a = url in allAnswers ? allAnswers[url] : allAnswers[path];
    const v = typeof a === 'function' ? a(hits[url]) : a;
    if (v instanceof Error) throw v;
    return v;
  };
  const importHits = {};
  const importTab = async id => {
    importHits[id] = (importHits[id] || 0) + 1;
    if (failImport(id, importHits[id])) throw new Error('импорт ' + id + ' упал');
    const m = tabs[id];
    if (!m) throw new Error('no tab module ' + id);
    return m;
  };
  const shell = createShell({
    fetchJson, clock, doc, importTab, hash: () => hashValue,
    copy: t => { copied.push(t); return copy(t); },
  });
  return { shell, doc, pending, hits, copied, setHash: h => { hashValue = h; } };
}

const tab = (id, order, endpoints, render = () => '<i></i>') => ({ id, title: id.toUpperCase(), order, endpoints, render });
const REPOS = [{ id: 'alpha', label: 'Alpha' }, { id: 'orch', label: 'Orch' }];
const TABS_HOME = ['home'];

// ---- pure pieces -------------------------------------------------------------------------------

await check('sort_tabs_by_order', () => {
  const ids = sortTabs([{ id: 'c', order: 3 }, { id: 'a', order: 1 }, { id: 'b', order: 2 }]).map(t => t.id);
  eq(ids, ['a', 'b', 'c'], 'sorted ids');
});

await check('poll_interval_defaults_and_answer', () => {
  eq(pollIntervalMs('fast', undefined), 2000, 'fast default');
  eq(pollIntervalMs('tickets', null), 15000, 'tickets default');
  eq(pollIntervalMs('slow', undefined), 600000, 'slow default');
  eq(pollIntervalMs('orch', undefined), 600000, 'orch default');
  eq(pollIntervalMs('fast', 7), 7000, 'answered refresh_s');
});

await check('banner_names_mode_and_error', () => {
  const t = bannerText('tickets', 'HTTP 500');
  ok(t.includes('tickets') && t.includes('HTTP 500'), `banner text was ${t}`);
  eq(bannerFor({}), null, 'no failure, no banner');
  const both = bannerFor({ fast: 'boom', tickets: 'bang' });
  ok(both.includes('fast') && both.includes('boom') && both.includes('tickets') && both.includes('bang'), `joined ${both}`);
});

await check('tab_from_hash', () => {
  eq(tabFromHash('#cost', ['home', 'cost']), 'cost', 'known tab');
  eq(tabFromHash('#cost/orch', ['home', 'cost']), 'cost', 'known tab with a repo part');
  eq(tabFromHash('#nope/alpha', ['home', 'cost']), 'home', 'unknown tab falls to first');
  eq(tabFromHash('', []), null, 'no tabs');
});

await check('hash_parse_and_build', () => {
  eq(parseHash('#cost/Alpha%20x'), { tab: 'cost', repo: 'Alpha x' }, 'parse decodes the repo');
  eq(parseHash('#cost'), { tab: 'cost', repo: null }, 'no repo part');
  eq(parseHash(''), { tab: '', repo: null }, 'empty hash');
  eq(parseHash('#cost/'), { tab: 'cost', repo: null }, 'empty repo part is none');
  eq(hashFor('cost', 'Alpha x'), '#cost/Alpha%20x', 'build with a repo');
  eq(hashFor('home', null), '#home', 'build without a repo');
  eq(repoFromHash('#cost/orch', ['alpha', 'orch']), 'orch', 'known repo');
  eq(repoFromHash('#cost/zzz', ['alpha', 'orch']), null, 'unknown repo');
  eq(repoFromHash('#cost', ['alpha', 'orch']), null, 'no repo part');
});

await check('reposOf_api_list', () => {
  eq(reposOf([{ id: 'alpha', label: 'Alpha' }, { id: 'orch', label: 'Orch' }]), [{ id: 'alpha', label: 'Alpha' }, { id: 'orch', label: 'Orch' }], 'list');
  eq(reposOf([{ id: 'a', label: 'A' }, { label: 'no id' }]), [{ id: 'a', label: 'A' }], 'entries without an id are dropped');
  eq(reposOf([{ id: 'solo' }]), [{ id: 'solo', label: 'solo' }], 'label falls back to id');
  eq(reposOf([]), null, 'empty list');
  eq(reposOf(null), null, 'no answer');
  eq(reposOf({ id: 'x' }), null, 'not an array');
});

await check('tip_placement_below_and_above', () => {
  const view = { width: 1000, height: 600 };
  eq(tipPlace({ left: 100, top: 50, bottom: 70, width: 40 }, { width: 200, height: 30 }, view),
    { left: 20, top: 78 }, 'below, centred on the anchor');
  eq(tipPlace({ left: 100, top: 580, bottom: 600, width: 40 }, { width: 200, height: 30 }, view),
    { left: 20, top: 542 }, 'above when there is no room below');
});

await check('escape_and_copy_control', () => {
  eq(esc('<a href="x">&'), '&lt;a href=&quot;x&quot;&gt;&amp;', 'esc');
  ok(copyControl('a"b').includes('data-copy="a&quot;b"'), 'copy control carries the escaped text');
});

// ---- the reader's place: a stub DOM ----------------------------------------------------------------

function el({ cls = [], attrs = {}, children = [] } = {}) {
  return {
    cls: new Set(cls), attrs: { ...attrs }, children, scrollLeft: 0, scrollTop: 0, focusCalls: [],
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; },
    focus(opts) { this.focusCalls.push(opts); },
  };
}

// A root with every descendant in document order; supports the selectors shell.js uses (.cls, [attr], [attr="v"], comma lists).
function stubRoot(children) {
  const simple = (e, s) => {
    if (s.startsWith('.')) return e.cls.has(s.slice(1));
    const m = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(s);
    if (!m) throw new Error('stub selector not supported: ' + s);
    return m[2] === undefined ? e.getAttribute(m[1]) !== null : e.getAttribute(m[1]) === m[2];
  };
  const r = {
    children,
    descendants() {
      const out = [];
      const walk = n => { for (const c of n.children) { out.push(c); walk(c); } };
      walk(this);
      return out;
    },
    querySelectorAll(sel) {
      const parts = sel.split(',').map(s => s.trim());
      return this.descendants().filter(e => parts.some(p => simple(e, p)));
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] ?? null; },
    contains(e) { return this.descendants().includes(e); },
  };
  return r;
}

// The same stage twice: an old one (the user's scroll and focus) and a fresh one (after the re-render).
function stageLayout() {
  const tbl = el({ cls: ['tblw'] });
  const epg = el({ cls: ['epg'] });
  const gs = el({ cls: ['gscroll'] });
  const toggle = el({ attrs: { 'data-toggle': 'k1' } });
  const repo = el({ attrs: { 'data-repo': 'orch' } });
  return { tbl, epg, gs, toggle, repo, root: stubRoot([tbl, el({ children: [epg, toggle] }), gs, repo]) };
}

await check('capture_restore_view_keeps_place', () => {
  const old = stageLayout();
  old.tbl.scrollLeft = 30; old.tbl.scrollTop = 5;
  old.epg.scrollLeft = 100;
  old.gs.scrollLeft = 12;
  const view = captureView(old.root, old.toggle);
  eq(view.boxes.length, 3, 'three scroll boxes captured');
  eq(view.focus, { attr: 'data-toggle', value: 'k1' }, 'focus keyed by data-toggle');

  const fresh = stageLayout();
  restoreView(fresh.root, view);
  eq([fresh.tbl.scrollLeft, fresh.tbl.scrollTop, fresh.epg.scrollLeft, fresh.gs.scrollLeft],
    [30, 5, 100, 12], 'scroll positions restored by index');
  eq(fresh.toggle.focusCalls.length, 1, 'focus moved to the same key');
  eq(fresh.toggle.focusCalls[0], { preventScroll: true }, 'focus does not jump the page');
});

await check('capture_restore_view_repo_focus_and_no_focus', () => {
  const old = stageLayout();
  const byRepo = captureView(old.root, old.repo);
  eq(byRepo.focus, { attr: 'data-repo', value: 'orch' }, 'focus keyed by data-repo');

  const outside = stageLayout();
  const none = captureView(old.root, outside.toggle);
  eq(none.focus, null, 'a focused element outside the stage is not kept');

  const fresh = stageLayout();
  restoreView(fresh.root, byRepo);
  eq(fresh.repo.focusCalls.length, 1, 'the repo button is focused again');
  restoreView(stubRoot([]), byRepo);   // a stage without the key: no error
});

// ---- driver ------------------------------------------------------------------------------------

await check('driver_tabs_sorted_by_order', async () => {
  const { shell, doc } = harness({
    tabs: { a: tab('a', 1, ['fast']), b: tab('b', 2, ['fast']) },
    answers: { '/api/tabs': ['b', 'a'], '/api/fast': { at_ms: 1, refresh_s: 2, data: {} } },
  });
  await shell.start();
  await settle();
  eq(doc.tabs.map(t => t.id), ['a', 'b'], 'tab buttons order');
  eq(doc.active, 'a', 'first tab active without a hash');
});

await check('driver_failed_fetch_banner_names_mode', async () => {
  const { shell, doc } = harness({
    tabs: { home: tab('home', 1, ['fast']), cost: tab('cost', 2, ['tickets']) },
    answers: {
      '/api/tabs': ['home', 'cost'],
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
      '/api/tickets': new Error('HTTP 500'),
    },
  });
  await shell.start();
  await settle();
  ok(doc.banner && doc.banner.includes('tickets') && doc.banner.includes('HTTP 500'), `banner was ${doc.banner}`);
});

await check('driver_tabs_banner_clears_after_load', async () => {
  const { shell, doc, pending } = harness({
    tabs: { home: tab('home', 1, ['fast']) },
    answers: {
      '/api/tabs': n => (n === 1 ? new Error('HTTP 503') : ['home']),
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
    },
  });
  await shell.start();
  await settle();
  ok(doc.banner && doc.banner.includes('tabs') && doc.banner.includes('HTTP 503'), `banner was ${doc.banner}`);
  await fireDue(pending, 2000);
  eq(doc.banner, null, 'banner gone once the tab list loads');
  eq(doc.tabs.map(t => t.id), ['home'], 'the tab is drawn after the retry');
});

await check('driver_tab_render_banner_clears', async () => {
  const { shell, doc, pending } = harness({
    tabs: {
      home: tab('home', 1, ['fast'], snap => {
        if (snap.fast.data.n === 1) throw new Error('boom');
        return `<p>ok${snap.fast.data.n}</p>`;
      }),
    },
    answers: {
      '/api/tabs': ['home'],
      '/api/fast': n => ({ at_ms: n, refresh_s: 2, data: { n } }),
    },
  });
  await shell.start();
  await settle();
  ok(doc.banner && doc.banner.includes('tab-home') && doc.banner.includes('boom'), `banner was ${doc.banner}`);
  await fireDue(pending, 2000);
  eq(doc.banner, null, 'banner gone after a good render');
  eq(doc.stages.at(-1), '<p>ok2</p>', 'the good render is on the stage');
});

await check('driver_tab_import_banner_clears', async () => {
  const { shell, doc, pending } = harness({
    tabs: { home: tab('home', 1, ['fast']), cost: tab('cost', 2, ['tickets']) },
    answers: {
      '/api/tabs': ['home', 'cost'],
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
      '/api/tickets': { at_ms: 1, refresh_s: 15, data: {} },
    },
    failImport: (id, n) => id === 'cost' && n === 1,
  });
  await shell.start();
  await settle();
  ok(doc.banner && doc.banner.includes('tab-cost'), `banner was ${doc.banner}`);
  eq(doc.tabs.map(t => t.id), ['home'], 'the failed tab is not drawn yet');
  await fireDue(pending, 2000);
  eq(doc.banner, null, 'banner gone after the tab imports');
  eq(doc.tabs.map(t => t.id), ['home', 'cost'], 'the tab is drawn after its import');
});

await check('driver_poll_interval_equals_answered_refresh_s', async () => {
  const seven = harness({
    tabs: { home: tab('home', 1, ['fast']) },
    answers: { '/api/tabs': ['home'], '/api/fast': { at_ms: 1, refresh_s: 7, data: {} } },
  });
  await seven.shell.start();
  await settle();
  eq(pollMs(seven.pending), [7000], 'next poll after refresh_s 7');

  const none = harness({
    tabs: { home: tab('home', 1, ['fast']) },
    answers: { '/api/tabs': ['home'], '/api/fast': { at_ms: 1, data: {} } },
  });
  await none.shell.start();
  await settle();
  eq(pollMs(none.pending), [2000], 'no refresh_s: fast default 2 s');
});

await check('driver_ui_state_survives_rerender', async () => {
  const { shell, doc, pending } = harness({
    tabs: {
      home: tab('home', 1, ['fast'], (snap, ui) => `<p>${ui.open('k') ? 'open' : 'closed'}:${snap.fast.data.n}</p>`),
    },
    answers: {
      '/api/tabs': ['home'],
      '/api/fast': n => ({ at_ms: n, refresh_s: 2, data: { n } }),
    },
  });
  await shell.start();
  await settle();
  eq(doc.stages.at(-1), '<p>closed:1</p>', 'first render');
  shell.ui.toggle('k');
  eq(doc.stages.at(-1), '<p>open:1</p>', 'toggle re-renders with the state');
  await fireDue(pending, 2000);
  eq(doc.stages.at(-1), '<p>open:2</p>', 'new data keeps the toggled state');
});

await check('driver_keeps_last_good_data_on_failure', async () => {
  const { shell, doc, pending } = harness({
    tabs: { home: tab('home', 1, ['fast'], snap => `<p>${snap.fast.data.k}</p>`) },
    answers: {
      '/api/tabs': ['home'],
      '/api/fast': n => (n === 1 ? { at_ms: 1, refresh_s: 2, data: { k: 'v' } } : new Error('down')),
    },
  });
  await shell.start();
  await settle();
  await fireDue(pending, 2000);
  ok(doc.banner && doc.banner.includes('fast') && doc.banner.includes('down'), `banner was ${doc.banner}`);
  eq(doc.stages.at(-1), '<p>v</p>', 'the last good data stays on the page, never an empty list');
});

await check('driver_banner_clears_on_recovery', async () => {
  const { shell, doc, pending } = harness({
    tabs: { home: tab('home', 1, ['fast']) },
    answers: {
      '/api/tabs': ['home'],
      '/api/fast': n => (n === 1 ? new Error('down') : { at_ms: n, refresh_s: 2, data: {} }),
    },
  });
  await shell.start();
  await settle();
  ok(doc.banner && doc.banner.includes('fast'), 'banner after the first failure');
  ok(pending.some(p => p.ms === 2000), 'retry at the fast default');
  await fireDue(pending, 2000);
  eq(doc.banner, null, 'banner gone after a good read');
});

// ---- the repo switch: GET /api/repos, ?repo= on every read, #<tab>/<repo> in the hash ----------

await check('driver_repos_api_default_first', async () => {
  const { shell, doc, hits } = harness({
    tabs: { home: tab('home', 1, ['fast']) },
    answers: {
      '/api/repos': REPOS,
      '/api/tabs': TABS_HOME,
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
    },
  });
  await shell.start();
  await settle();
  eq(doc.repos, REPOS, 'the switch lists the API entries');
  eq(doc.selected, 'alpha', 'the first entry is selected by default');
  ok(hits['/api/fast?repo=alpha'] === 1, `reads carry ?repo: ${Object.keys(hits)}`);
  eq(doc.hashWritten, '#home/alpha', 'the hash names the tab and the repo');
});

await check('driver_single_repo_hides_switch', async () => {
  const { shell, doc, hits } = harness({
    tabs: { home: tab('home', 1, ['fast']) },
    answers: {
      '/api/repos': [{ id: 'only', label: 'Only' }],
      '/api/tabs': TABS_HOME,
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
    },
  });
  await shell.start();
  await settle();
  eq(doc.repos, null, 'one entry: the switch is hidden');
  ok(hits['/api/fast?repo=only'] === 1, 'the one repo is still the read\'s repo');
});

await check('driver_repos_failure_hides_switch', async () => {
  const { shell, doc, hits } = harness({
    tabs: { home: tab('home', 1, ['fast']) },
    answers: {
      '/api/repos': new Error('HTTP 500'),
      '/api/tabs': TABS_HOME,
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
    },
  });
  await shell.start();
  await settle();
  eq(doc.repos, null, 'a failed repo call: the switch is hidden');
  ok(hits['/api/fast'] === 1, `reads go on without a repo: ${Object.keys(hits)}`);
});

await check('driver_repo_from_hash_survives_reload', async () => {
  const { shell, doc, hits } = harness({
    tabs: { home: tab('home', 1, ['fast']), cost: tab('cost', 2, ['tickets']) },
    answers: {
      '/api/repos': REPOS,
      '/api/tabs': ['home', 'cost'],
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
      '/api/tickets': { at_ms: 1, refresh_s: 15, data: {} },
    },
    hash: '#cost/orch',
  });
  await shell.start();
  await settle();
  eq(doc.active, 'cost', 'the tab from the hash');
  eq(doc.selected, 'orch', 'the repo from the hash');
  ok(hits['/api/tickets?repo=orch'] === 1, `the tab reads its repo: ${Object.keys(hits)}`);
  eq(doc.hashWritten, undefined, 'a hash that already names both is not rewritten');
});

await check('driver_repo_switch_rereads_now', async () => {
  const { shell, doc, hits } = harness({
    tabs: { home: tab('home', 1, ['fast'], snap => `<p>${snap.fast.data.name}</p>`) },
    answers: {
      '/api/repos': REPOS,
      '/api/tabs': TABS_HOME,
      '/api/fast?repo=alpha': { at_ms: 1, refresh_s: 2, data: { name: 'alpha-data' } },
      '/api/fast?repo=orch': { at_ms: 2, refresh_s: 2, data: { name: 'orch-data' } },
    },
  });
  await shell.start();
  await settle();
  eq(doc.stages.at(-1), '<p>alpha-data</p>', 'the first repo is drawn');
  shell.ui.setRepo('orch');
  eq(doc.stages.at(-1), '<p class="loading">Загрузка…</p>', 'the old repo is not left on the stage: the placeholder until the new repo answers');
  ok(hits['/api/fast?repo=orch'] === 1, 'the new repo is read at once');
  await settle();
  eq(doc.stages.at(-1), '<p>orch-data</p>', 'the new repo is drawn');
  eq(doc.hashWritten, '#home/orch', 'the hash follows the repo');
  eq(doc.selected, 'orch', 'the switch shows the new repo');
});

await check('driver_stale_answer_dropped', async () => {
  let resolveAlpha;
  const alpha = new Promise(r => { resolveAlpha = r; });
  const { shell, doc } = harness({
    tabs: { home: tab('home', 1, ['fast'], snap => `<p>${snap.fast.data.name}</p>`) },
    answers: {
      '/api/repos': REPOS,
      '/api/tabs': TABS_HOME,
      '/api/fast?repo=alpha': alpha,
      '/api/fast?repo=orch': { at_ms: 2, refresh_s: 2, data: { name: 'orch-data' } },
    },
  });
  await shell.start();
  await settle();
  shell.ui.setRepo('orch');
  await settle();
  const mark = doc.stages.length;
  resolveAlpha({ at_ms: 1, refresh_s: 2, data: { name: 'alpha-data' } });
  await settle();
  eq(doc.stages.at(-1), '<p>orch-data</p>', 'the late answer for the old repo does not replace the new one');
  ok(!doc.stages.slice(mark).some(s => s.includes('alpha-data')), 'no alpha-data drawn after the late answer');
});

await check('driver_tab_switch_keeps_repo', async () => {
  const { shell, doc } = harness({
    tabs: { home: tab('home', 1, ['fast']), cost: tab('cost', 2, ['fast']) },
    answers: {
      '/api/repos': REPOS,
      '/api/tabs': ['home', 'cost'],
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
    },
  });
  await shell.start();
  await settle();
  eq(doc.hashWritten, '#home/alpha', 'start names the first tab and repo');
  shell.select('cost');
  eq(doc.hashWritten, '#cost/alpha', 'a tab click keeps the repo in the hash');
  eq(doc.active, 'cost', 'the tab is selected');
});

await check('driver_route_hash_change', async () => {
  const { shell, doc, hits, setHash } = harness({
    tabs: { home: tab('home', 1, ['fast']), cost: tab('cost', 2, ['tickets']) },
    answers: {
      '/api/repos': REPOS,
      '/api/tabs': ['home', 'cost'],
      '/api/fast?repo=alpha': { at_ms: 1, refresh_s: 2, data: {} },
      '/api/tickets?repo=orch': { at_ms: 1, refresh_s: 15, data: {} },
    },
  });
  await shell.start();
  await settle();
  setHash('#cost/orch');
  shell.route('#cost/orch');
  await settle();
  eq(doc.active, 'cost', 'the hash picks the tab');
  eq(doc.selected, 'orch', 'the hash picks the repo');
  ok(hits['/api/tickets?repo=orch'] === 1, 'the new tab reads the new repo');
});

// ---- the rest ----------------------------------------------------------------------------------

await check('driver_copy_control', async () => {
  const { shell, copied } = harness({
    tabs: { home: tab('home', 1, ['fast']) },
    answers: { '/api/tabs': ['home'], '/api/fast': { at_ms: 1, refresh_s: 2, data: {} } },
  });
  await shell.start();
  await settle();
  shell.ui.copy('abc');
  eq(copied, ['abc'], 'copy is passed to the injected copy');
});

// ---- unit 11: ui.state / ui.set, the first-read gate, the placeholder, snapshot.repos ------------

await check('parse_set_pair', () => {
  const parseSet = shellModule.parseSet;
  eq(parseSet('period:week'), { key: 'period', value: 'week' }, 'key and value');
  eq(parseSet('mode:a:b'), { key: 'mode', value: 'a:b' }, 'the value keeps later colons');
  eq(parseSet('nocolon'), null, 'no colon: nothing');
  eq(parseSet(':x'), null, 'empty key: nothing');
  eq(parseSet(undefined), null, 'no attribute: nothing');
});

await check('driver_ui_set_persists_and_rerenders', async () => {
  const states = [];
  const { shell, doc, pending } = harness({
    tabs: {
      home: tab('home', 1, ['fast'], (snap, ui) => {
        states.push(ui.state);
        return `<p>${ui.state.period ?? 'day'}:${snap.fast.data.n}</p>`;
      }),
      cost: tab('cost', 2, ['fast'], () => '<i>cost</i>'),
    },
    answers: {
      '/api/tabs': ['home', 'cost'],
      '/api/fast': n => ({ at_ms: n, refresh_s: 2, data: { n } }),
    },
  });
  await shell.start();
  await settle();
  eq(doc.stages.at(-1), '<p>day:1</p>', 'first render, nothing set yet');
  shell.ui.set('period', 'week');
  eq(doc.stages.at(-1), '<p>week:1</p>', 'set re-renders the active tab with the value');
  await fireDue(pending, 2000);
  eq(doc.stages.at(-1), '<p>week:2</p>', 'new data keeps the set value');
  shell.select('cost');
  shell.select('home');
  eq(doc.stages.at(-1), '<p>week:2</p>', 'a tab switch keeps the set value');
  ok(states.length >= 3 && states.every(s => s === states[0]), 'ui.state is one persistent object across renders');
});

await check('capture_restore_view_set_focus', () => {
  const setBtn = el({ attrs: { 'data-set': 'period:week' } });
  const root = stubRoot([setBtn]);
  const view = captureView(root, setBtn);
  eq(view.focus, { attr: 'data-set', value: 'period:week' }, 'focus keyed by data-set');
  const freshBtn = el({ attrs: { 'data-set': 'period:week' } });
  restoreView(stubRoot([freshBtn]), view);
  eq(freshBtn.focusCalls, [{ preventScroll: true }], 'the set button is focused again after a re-render');
});

await check('driver_first_read_error_renders_tab_card', async () => {
  const { shell, doc } = harness({
    tabs: {
      home: tab('home', 1, ['fast', 'tickets'], snap => `<p>${snap.fast.data.n}|${snap.tickets.error}</p>`),
    },
    answers: {
      '/api/tabs': ['home'],
      '/api/fast': { at_ms: 1, refresh_s: 2, data: { n: 'f' } },
      '/api/tickets': new Error('сеть упала'),
    },
  });
  await shell.start();
  await settle();
  eq(doc.stages.at(-1), '<p>f|сеть упала</p>', 'the tab draws its own error card on a first failed read');
  ok(doc.banner && doc.banner.includes('tickets') && doc.banner.includes('сеть упала'), `banner was ${doc.banner}`);
});

await check('driver_placeholder_until_every_endpoint_answers', async () => {
  let renders = 0;
  const { shell, doc } = harness({
    tabs: { home: tab('home', 1, ['fast', 'tickets'], () => { renders += 1; return '<p>home</p>'; }) },
    answers: {
      '/api/tabs': ['home'],
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
      '/api/tickets': () => new Promise(() => {}),
    },
  });
  shell.start();
  ok(doc.stages.length >= 1 && doc.stages.at(-1).includes('Загрузка'), `first stage was ${doc.stages.at(-1)}`);
  await settle();
  ok(doc.stages.at(-1).includes('Загрузка'), `placeholder stays while a read is out, stage was ${doc.stages.at(-1)}`);
  eq(renders, 0, 'the tab is not drawn before every endpoint has an answer');
  ok(doc.stages.every(s => s !== ''), 'the stage is never empty');
});

await check('driver_snapshot_repos_and_ui_repo', async () => {
  const { shell, doc } = harness({
    tabs: { home: tab('home', 1, ['fast'], (snap, ui) => `<p>${snap.repos.map(r => r.id).join(',')}|${ui.repo}</p>`) },
    answers: {
      '/api/repos': REPOS,
      '/api/tabs': ['home'],
      '/api/fast': { at_ms: 1, refresh_s: 2, data: {} },
    },
  });
  await shell.start();
  await settle();
  eq(doc.stages.at(-1), '<p>alpha,orch|alpha</p>', 'snapshot.repos is the API list, ui.repo the selected id');
  shell.ui.setRepo('orch');
  await settle();
  eq(doc.stages.at(-1), '<p>alpha,orch|orch</p>', 'after a repo change ui.repo is the new id');
});

// ---- unit 3c review-fix: ui.state is reset on a repo change, so one repo's samples never mix into another's ---------

await check('driver_repo_switch_resets_ui_state', async () => {
  const { shell, doc } = harness({
    tabs: { home: tab('home', 1, ['fast'], (snap, ui) => {
      const seen = ui.state.seen || (ui.state.seen = []);
      seen.push(snap.fast.data.name);
      return `<p>${seen.join(',')}</p>`;
    }) },
    answers: {
      '/api/repos': REPOS,
      '/api/tabs': TABS_HOME,
      '/api/fast?repo=alpha': { at_ms: 1, refresh_s: 2, data: { name: 'alpha-data' } },
      '/api/fast?repo=orch': { at_ms: 2, refresh_s: 2, data: { name: 'orch-data' } },
    },
  });
  await shell.start();
  await settle();
  eq(doc.stages.at(-1), '<p>alpha-data</p>', 'the first repo is drawn and its ui.state holds its own sample');
  shell.ui.setRepo('orch');
  await settle();
  eq(doc.stages.at(-1), '<p>orch-data</p>', 'after the switch ui.state is empty: the alpha sample is not mixed into orch');
});

console.log(JSON.stringify(results));
