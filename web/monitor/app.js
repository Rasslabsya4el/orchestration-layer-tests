// Page entry (#340 unit 2): wires shell.js to the page. Plain ES module, no build step, no external request.
import { createShell, captureView, restoreView, tipPlace, esc, parseSet } from "./shell.js";

const $ = sel => document.querySelector(sel);
const banner = $("#banner"), tabsEl = $("#tabs"), reposEl = $("#repos"), stage = $("#stage"), tipEl = $("#tip");

// A failed read or no answer throws, so the shell shows the banner and keeps its last good data.
async function fetchJson(url) {
  let res;
  try {
    res = await fetch(url, { cache: "no-store" });
  } catch {
    throw new Error("сервер не отвечает");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

let tipFor = null;
function hideTip() {
  tipFor = null;
  tipEl.classList.remove("show");
}

function showTip(el) {
  const text = el.dataset.tip;
  if (!text) return;
  tipFor = el;
  tipEl.textContent = text;
  tipEl.classList.add("show");
  const a = el.getBoundingClientRect(), t = tipEl.getBoundingClientRect();
  const p = tipPlace(
    { left: a.left, top: a.top, bottom: a.bottom, width: a.width },
    { width: t.width, height: t.height },
    { width: document.documentElement.clientWidth, height: innerHeight },
  );
  tipEl.style.left = `${p.left}px`;
  tipEl.style.top = `${p.top}px`;
}

// The tab the bar shows, and the tab the stage was last drawn for: the reader's place is kept only while they match.
let activeTab = null, stageTab = null;

const doc = {
  setTabs(list, active) {
    activeTab = active;
    tabsEl.innerHTML = list
      .map(t => `<button type="button" role="tab" data-tab="${esc(t.id)}" aria-selected="${t.id === active}">${esc(t.title)}</button>`)
      .join("");
  },
  setStage(html) {
    // a tooltip whose element was just replaced has no anchor left
    if (tipFor && !tipFor.isConnected) hideTip();
    // the scroll and the focus of the same tab survive its re-render; another tab starts at its top
    const keep = activeTab !== null && activeTab === stageTab;
    const view = keep ? captureView(stage, document.activeElement) : null;
    stage.innerHTML = html;
    stageTab = activeTab;
    if (view) restoreView(stage, view);
  },
  setBanner(text) {
    banner.hidden = !text;
    banner.textContent = text || "";
  },
  setRepos(list, selected) {
    reposEl.hidden = !list;
    reposEl.innerHTML = list
      ? list.map(r => `<button type="button" data-repo="${esc(r.id)}" aria-pressed="${r.id === selected}">${esc(r.label)}</button>`).join("")
      : "";
  },
  setHash(h) {
    location.hash = h;
  },
};

const shell = createShell({
  fetchJson,
  clock: { setTimeout: (fn, ms) => setTimeout(fn, ms) },
  doc,
  importTab: id => import(`/tab-${encodeURIComponent(id)}.js`),
  copy: text => navigator.clipboard.writeText(text),
  hash: () => location.hash,
});

document.addEventListener("click", e => {
  const tab = e.target.closest("[data-tab]");
  if (tab) { shell.select(tab.dataset.tab); return; }
  const repo = e.target.closest("[data-repo]");
  if (repo) { shell.ui.setRepo(repo.dataset.repo); return; }
  const toggle = e.target.closest("[data-toggle]");
  if (toggle) { shell.ui.toggle(toggle.dataset.toggle); return; }
  const set = e.target.closest("[data-set]");
  if (set) {
    const kv = parseSet(set.dataset.set);
    if (kv) shell.ui.set(kv.key, kv.value);
    return;
  }
  const copy = e.target.closest("[data-copy]");
  if (copy) shell.ui.copy(copy.dataset.copy);
});

addEventListener("hashchange", () => shell.route(location.hash));

// The styled tooltip: any element with data-tip; hover and keyboard focus place it the same way.
document.addEventListener("pointerover", e => {
  const el = e.target.closest?.("[data-tip]");
  if (el && el !== tipFor) showTip(el);
});
document.addEventListener("pointerout", e => {
  const el = e.target.closest?.("[data-tip]");
  if (el && !el.contains(e.relatedTarget)) hideTip();
});
document.addEventListener("focusin", e => {
  const el = e.target.closest?.("[data-tip]");
  if (el) showTip(el);
});
document.addEventListener("focusout", e => {
  if (e.target.closest?.("[data-tip]")) hideTip();
});
addEventListener("scroll", hideTip, true);

shell.start();
