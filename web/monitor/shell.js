// Page shell (#340 unit 2): tab order, poll cadence, banner text, tooltip placement, the repo switch, the reader's
// place on a re-render and the render driver. Plain ES module, no DOM at import; the driver takes its fetch, clock,
// document-like stub, tab importer and clipboard from the caller, so node 24 runs it with stubs
// (tests/issue340_page_shell_check.mjs).

// Cadence when no answer has said one (seconds). A refresh_s in the answer always wins. `repos` is the repo list.
export const DEFAULT_REFRESH_S = { fast: 2, tickets: 15, slow: 600, orch: 600, github: 600, repos: 300 };
const MIN_POLL_MS = 200;
const RETRY_TABS_MS = 2000;
const REPO_MS = DEFAULT_REFRESH_S.repos * 1000;
const GAP = 8;

// The scroll boxes a re-render keeps (their position is restored by index), and the attributes a focused control is keyed by.
export const KEEP_SCROLL = ".tblw, .epg, .gscroll";
const FOCUS_ATTRS = ["data-toggle", "data-repo", "data-set"];

// The stage while the tab has no answer yet: a short placeholder, never an empty stage.
const LOADING_HTML = '<p class="loading">Загрузка…</p>';

export function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// A data-set attribute "key:value" -> { key, value }; the value keeps any later colons; null without a key.
export function parseSet(text) {
  const s = String(text ?? "");
  const cut = s.indexOf(":");
  if (cut <= 0) return null;
  return { key: s.slice(0, cut), value: s.slice(cut + 1) };
}

// Tabs by `order` (a number; a tab without one goes last), ties by id.
export function sortTabs(tabs) {
  const rank = t => (t.order == null || !Number.isFinite(Number(t.order)) ? Infinity : Number(t.order));
  return tabs.slice().sort((a, b) => {
    const x = rank(a), y = rank(b);
    if (x !== y) return x < y ? -1 : 1;
    const i = String(a.id), j = String(b.id);
    return i < j ? -1 : i > j ? 1 : 0;
  });
}

// Every mode the tabs read, once each, in tab order.
export function endpointsOf(tabs) {
  const out = [];
  for (const t of tabs) for (const m of t.endpoints) if (!out.includes(m)) out.push(m);
  return out;
}

// Milliseconds until the next read of `mode`: the answer's refresh_s, else the mode's default.
export function pollIntervalMs(mode, refreshS) {
  const s = Number(refreshS);
  const seconds = refreshS != null && Number.isFinite(s) && s > 0 ? s : (DEFAULT_REFRESH_S[mode] ?? 600);
  return Math.max(MIN_POLL_MS, Math.round(seconds * 1000));
}

export function bannerText(mode, error) {
  return `Нет данных: ${mode} — ${error}`;
}

// failures: mode -> error text. Null when nothing failed (the banner hides).
export function bannerFor(failures) {
  const lines = Object.entries(failures).map(([mode, error]) => bannerText(mode, error));
  return lines.length ? lines.join(" · ") : null;
}

function decodePart(s) {
  try { return decodeURIComponent(s); } catch { return s; }   // a bad escape: keep the raw text
}

// "#<tab>/<repo>" -> { tab, repo }; repo is null when the hash has no repo part.
export function parseHash(hash) {
  const raw = String(hash ?? "").replace(/^#/, "");
  const cut = raw.indexOf("/");
  const tab = decodePart(cut < 0 ? raw : raw.slice(0, cut));
  const repo = cut < 0 ? "" : decodePart(raw.slice(cut + 1));
  return { tab, repo: repo === "" ? null : repo };
}

// The hash for a tab and a repo (no repo part when repo is null or empty).
export function hashFor(tab, repo) {
  const head = `#${encodeURIComponent(tab)}`;
  return repo == null || repo === "" ? head : `${head}/${encodeURIComponent(repo)}`;
}

// The tab named by the hash when it is one of ids, else the first id (null when there are none).
export function tabFromHash(hash, ids) {
  const { tab } = parseHash(hash);
  if (ids.includes(tab)) return tab;
  return ids.length ? ids[0] : null;
}

// The repo named by the hash when it is one of ids, else null.
export function repoFromHash(hash, ids) {
  const { repo } = parseHash(hash);
  return repo != null && ids.includes(repo) ? repo : null;
}

// The repo list from GET /api/repos ([{id, label}]); entries without an id are dropped, the label falls back to the id.
// Null when there is no usable list.
export function reposOf(list) {
  if (!Array.isArray(list)) return null;
  const out = list
    .filter(r => r && typeof r === "object" && r.id != null && String(r.id) !== "")
    .map(r => ({ id: String(r.id), label: String(r.label ?? r.id) }));
  return out.length ? out : null;
}

// The one placement rule: centred under the anchor, above it only when there is no room below.
// anchor {left, top, bottom, width}, tip {width, height}, view {width, height}.
export function tipPlace(anchor, tip, view) {
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const left = clamp(anchor.left + anchor.width / 2 - tip.width / 2, GAP, view.width - tip.width - GAP);
  const below = anchor.bottom + GAP;
  const above = anchor.top - GAP - tip.height;
  const fitsBelow = below + tip.height <= view.height - GAP;
  const top = !fitsBelow && above > GAP ? above : below;
  return { left, top };
}

// The copy control: a button whose data-copy is the text to copy (the page's click handler does the copy).
export function copyControl(text) {
  return `<button type="button" class="copy" data-copy="${esc(text)}" aria-label="копировать">копировать</button>`;
}

// The reader's place in a stage: the scroll of each KEEP_SCROLL box (by index) and the focused control's key, if the
// focused element is inside the stage and carries one of FOCUS_ATTRS.
export function captureView(root, focused) {
  const boxes = Array.from(root.querySelectorAll(KEEP_SCROLL), el => ({ left: el.scrollLeft, top: el.scrollTop }));
  let focus = null;
  if (focused && root.contains(focused)) {
    for (const attr of FOCUS_ATTRS) {
      const value = focused.getAttribute(attr);
      if (value !== null) { focus = { attr, value }; break; }
    }
  }
  return { boxes, focus };
}

// Puts a captured place back on a fresh stage; a box or a key the new stage lacks is skipped.
export function restoreView(root, view) {
  const boxes = Array.from(root.querySelectorAll(KEEP_SCROLL));
  view.boxes.forEach((b, i) => {
    const el = boxes[i];
    if (el) { el.scrollLeft = b.left; el.scrollTop = b.top; }
  });
  if (view.focus) {
    const { attr, value } = view.focus;
    const el = Array.from(root.querySelectorAll(`[${attr}]`)).find(e => e.getAttribute(attr) === value);
    if (el) el.focus({ preventScroll: true });
  }
}

// The render driver. Injected:
//   fetchJson(url) -> parsed JSON (throws on a failed read or no answer)
//   clock.setTimeout(fn, ms)
//   doc: setTabs(list [{id, title}], activeId), setStage(html), setBanner(text | null),
//        setRepos(list [{id, label}] | null, selectedRepoId), setHash(hash)
//   importTab(id) -> the tab module (ES module: id, title, order, endpoints, render)
//   copy(text) -> copies (a promise or a value); hash() -> the current location hash
// Returns { start(), select(id), route(hash), ids(), ui }. ui is the second argument of every tab's render(snapshot, ui):
// ui.open(key), ui.toggle(key), ui.set(key, value), ui.state (a plain object the tab's set keys live in), ui.copy(text),
// ui.repo, ui.setRepo(id). Its state outlives every re-render and tab switch, and is emptied by a repo change. The snapshot a tab gets carries
// snapshot.repos (the GET /api/repos list, or null).
// Every read is `/api/<mode>` with `?repo=<id>` once a repo is chosen; a repo change starts every read again and drops
// an answer that comes for the old repo.
export function createShell({ fetchJson, clock, doc, importTab, copy = () => false, hash = () => "" }) {
  const state = {
    tabs: [], active: null, snap: {}, failures: {}, open: {},
    repos: null, repo: null,
    tabsLoaded: false,   // the tab list has been answered and its tabs imported: an empty tab bar is then a real one
    gen: 0,              // bumped on each repo change: a read of an older gen is dropped
    live: new Set(),     // the modes with one poll chain running in the current gen
  };
  const uiState = {};    // ui.state: the tabs' own keys, kept across every re-render

  const ui = {
    state: uiState,
    open: key => !!state.open[key],
    toggle: key => { state.open[key] = !state.open[key]; renderActive(); },
    set: (key, value) => { uiState[key] = value; renderActive(); },
    copy: text => {
      try { return Promise.resolve(copy(text)).then(() => true, () => false); } catch { return Promise.resolve(false); }
    },
    get repo() { return state.repo; },
    setRepo: name => changeRepo(name),
  };

  const tabIds = () => state.tabs.map(t => t.id);
  const repoIds = () => (state.repos ? state.repos.map(r => r.id) : []);
  // the switch shows only with two or more repos; one repo is still the repo the reads carry
  const shownRepos = () => (state.repos && state.repos.length >= 2 ? state.repos : null);
  const isOtherKey = key => key === "tabs" || key.startsWith("tab-");

  function fail(key, msg) {
    state.failures[key] = msg;
    doc.setBanner(bannerFor(state.failures));
  }

  function clearFail(key) {
    if (!(key in state.failures)) return;
    delete state.failures[key];
    doc.setBanner(bannerFor(state.failures));
  }

  // An endpoint is settled once it has data or an error: a tab is drawn once every endpoint of it is settled, and it
  // then shows its own error card for a failed one. Until then the stage shows the placeholder.
  const settled = s => !!s && (s.data != null || s.error != null);
  const ready = tab => tab.endpoints.every(m => settled(state.snap[m]));
  const snapshotView = () => ({ ...state.snap, repos: state.repos });

  function drawTabs() {
    doc.setTabs(state.tabs.map(t => ({ id: t.id, title: t.title })), state.active);
  }

  function renderActive() {
    const tab = state.tabs.find(t => t.id === state.active);
    if (!tab) { doc.setStage(state.tabsLoaded ? "" : LOADING_HTML); return; }
    if (!ready(tab)) { doc.setStage(LOADING_HTML); return; }
    try {
      doc.setStage(String(tab.render(snapshotView(), ui)));
      clearFail(`tab-${tab.id}`);
    } catch (e) {
      fail(`tab-${tab.id}`, `ошибка отрисовки: ${e.message}`);
      doc.setStage("");
    }
  }

  // The hash names the active tab and the repo; it is written only when it does not already.
  function writeHash() {
    if (state.active == null) return;
    const h = parseHash(hash());
    if (h.tab === state.active && h.repo === state.repo) return;
    doc.setHash(hashFor(state.active, state.repo));
  }

  function setActive(id) {
    state.active = id;
    drawTabs();
    renderActive();
  }

  function setRepoState(name) {
    state.repo = name;
    doc.setRepos(shownRepos(), name);
    restartReads();
  }

  // Every read starts again at the current repo: the old data and its failures go, one poll chain per mode. ui.state is
  // emptied in place (the same object, its keys gone): one repo's samples never mix into another repo's.
  function restartReads() {
    state.gen += 1;
    state.live = new Set();
    state.snap = {};
    for (const key of Object.keys(uiState)) delete uiState[key];
    for (const key of Object.keys(state.failures)) if (!isOtherKey(key)) delete state.failures[key];
    doc.setBanner(bannerFor(state.failures));
    startPolls();
    renderActive();
  }

  function startPolls() {
    for (const mode of endpointsOf(state.tabs)) {
      if (state.live.has(mode)) continue;   // the per-mode guard: a mode never has two chains in one gen
      state.live.add(mode);
      poll(mode, state.gen);
    }
  }

  async function poll(mode, gen) {
    if (gen !== state.gen) return;
    const repo = state.repo;
    const url = repo == null ? `/api/${mode}` : `/api/${mode}?repo=${encodeURIComponent(repo)}`;
    let entry = null, error = null;
    try { entry = await fetchJson(url); } catch (e) { error = e; }
    if (gen !== state.gen) return;   // the repo changed while this read was out: its answer is dropped
    if (error == null && (!entry || typeof entry !== "object")) error = new Error("ответ не объект");
    let refreshS = state.snap[mode] ? state.snap[mode].refresh_s : undefined;
    const prev = state.snap[mode];
    // every answer ends in an entry with data or an error, so the endpoint is settled and its tab can draw
    if (error != null) {
      // no answer: the last good data stays, the entry carries the error
      const msg = String((error && error.message) || error);
      state.snap[mode] = { at_ms: prev ? prev.at_ms : undefined, refresh_s: prev ? prev.refresh_s : undefined, data: prev ? prev.data : null, error: msg };
      fail(mode, msg);
    } else {
      // a failed read or an answer without data: the last good data stays, the banner names the error
      const failed = entry.error || (entry.data == null ? "ответ без данных" : null);
      if (failed) {
        state.snap[mode] = { at_ms: entry.at_ms, refresh_s: entry.refresh_s, data: prev ? prev.data : null, error: String(failed) };
        fail(mode, String(failed));
      } else {
        state.snap[mode] = { at_ms: entry.at_ms, refresh_s: entry.refresh_s, data: entry.data };
        clearFail(mode);
      }
      refreshS = entry.refresh_s;
    }
    clock.setTimeout(() => poll(mode, gen), pollIntervalMs(mode, refreshS));
    const active = state.tabs.find(t => t.id === state.active);
    if (active && active.endpoints.includes(mode)) renderActive();
  }

  // GET /api/repos. A failed call hides the switch; reads go on without a repo until a list comes.
  async function loadRepos() {
    let list = null;
    let failed = false;
    try {
      list = reposOf(await fetchJson("/api/repos"));
    } catch {
      failed = true;
    }
    clock.setTimeout(loadRepos, REPO_MS);
    if (failed && state.repos) return;   // a later failure keeps the list it has
    applyRepos(list);
  }

  function applyRepos(list) {
    state.repos = list;
    const ids = list ? list.map(r => r.id) : [];
    const kept = state.repo != null && ids.includes(state.repo) ? state.repo : null;
    const next = kept ?? (repoFromHash(hash(), ids) ?? (ids[0] ?? null));
    if (next !== state.repo) setRepoState(next);
    else doc.setRepos(shownRepos(), next);
    writeHash();
  }

  // A repo picked by the owner (the switch or the hash): the stage empties and every read starts at once.
  function changeRepo(name) {
    if (name === state.repo || !repoIds().includes(name)) return;
    setRepoState(name);
    writeHash();
  }

  async function importOne(id) {
    try {
      const m = await importTab(id);
      if (!m || typeof m.render !== "function" || !Array.isArray(m.endpoints)) throw new Error("не модуль вкладки");
      clearFail(`tab-${id}`);
      return { id: m.id ?? id, title: m.title ?? id, order: m.order, endpoints: m.endpoints, render: m.render };
    } catch (e) {
      fail(`tab-${id}`, String((e && e.message) || e));
      clock.setTimeout(() => retryImport(id), RETRY_TABS_MS);
      return null;
    }
  }

  // A tab whose module failed to load is imported again until it loads; then it joins the tab bar.
  async function retryImport(id) {
    const tab = await importOne(id);
    if (!tab || state.tabs.some(t => t.id === tab.id)) return;
    state.tabs = sortTabs([...state.tabs, tab]);
    const want = tabFromHash(hash(), tabIds());
    if (state.active == null || want === tab.id) state.active = want;
    showAll();
    writeHash();
    startPolls();
  }

  // The tab bar and the stage, from the state.
  function showAll() {
    drawTabs();
    renderActive();
  }

  async function start() {
    renderActive();   // the placeholder from the first call, before any answer
    let ids;
    try {
      ids = await fetchJson("/api/tabs");
      if (!Array.isArray(ids)) throw new Error("список вкладок не массив");
    } catch (e) {
      fail("tabs", String((e && e.message) || e));
      clock.setTimeout(start, RETRY_TABS_MS);
      return;
    }
    clearFail("tabs");
    await loadRepos();
    const loaded = [];
    for (const id of ids) {
      const tab = await importOne(id);
      if (tab) loaded.push(tab);
    }
    state.tabs = sortTabs(loaded);
    state.tabsLoaded = true;
    state.active = tabFromHash(hash(), tabIds());
    writeHash();
    showAll();
    startPolls();
  }

  // A tab click: the tab is drawn and the hash names it, with the repo.
  function select(id) {
    if (!state.tabs.some(t => t.id === id)) return;
    setActive(id);
    writeHash();
  }

  // A hash change: the repo and the tab it names; the hash is written back only if it differs from the state.
  function route(h) {
    if (!state.tabs.length) return;
    const repo = repoFromHash(h, repoIds());
    if (repo != null && repo !== state.repo) setRepoState(repo);
    const tab = tabFromHash(h, tabIds());
    if (tab !== state.active) setActive(tab);
    writeHash();
  }

  return { start, select, route, ids: tabIds, ui };
}
