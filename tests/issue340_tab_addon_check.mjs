// Node check for web/monitor/tab-addon.js (#340 unit 8). Run by tests/test_issue340_tab_addon.py.
// Prints one JSON object on stdout: the cells of each drawn row, so the test asserts the outcome, not the markup.
// Renders tests/fixtures/monitor/addon_fast.json; no DOM, no network.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import * as tab from '../web/monitor/tab-addon.js';

const fixture = JSON.parse(readFileSync(fileURLToPath(new URL('./fixtures/monitor/addon_fast.json', import.meta.url)), 'utf8'));
const clone = x => JSON.parse(JSON.stringify(x));
// The tab's own source: "no repo name" is about what the code hardcodes, not the data it draws.
const tabSource = readFileSync(fileURLToPath(new URL('../web/monitor/tab-addon.js', import.meta.url)), 'utf8');
const NOW_MS = 1761249407000; // the envelope's time: a build started at 1761249107 has run 5 min
const envelope = data => ({ at_ms: NOW_MS, refresh_s: 2, data });
const plain = s => s.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
// The cells (text of each span) of every row drawn as data-row=kind.
const cellsOf = (html, kind) => [...html.matchAll(new RegExp(`<div class="tr" data-row="${kind}"[^>]*>(.*?)</div>`, 'gs'))]
  .map(m => [...m[1].matchAll(/<span[^>]*>(.*?)<\/span>/g)].map(x => x[1]));
// The laptop card's text lines (its `mu` lines) and its squares.
const laptopOf = html => {
  const part = html.slice(html.indexOf('data-part="remote"'), html.indexOf('data-part="runs"'));
  return {
    squares: (part.match(/data-lane=/g) || []).length,
    caption: (part.match(/<b>Слоты<\/b><span class="r">([^<]*)</) || [])[1] ?? null,
    cores: (part.match(/<div class="mu">([^<]*ядер[^<]*)</) || [])[1] ?? null,
    lines: [...part.matchAll(/<div class="mu"[^>]*>([^<]*)</g)].map(m => m[1]),
  };
};

// The fixture as the server answers it: the envelope of `addon:fast`.
const html = tab.render({ 'addon:fast': envelope(fixture) }, {});
// The host card's name comes from the payload (`remote.title`); a payload without one gets the neutral fallback.
const titled = structuredClone(fixture);
titled.remote.title = 'Build host <b>';
const cardName = h => (h.match(/data-part="remote"><div class="lb"><b>([^<]*)<\/b>/) || [])[1] ?? null;
const titledHtml = tab.render({ 'addon:fast': envelope(titled) }, {});
const runs = fixture.runs;

// A repo without the add-on: the core fast payload (no runs, remote or readings keys).
const noAddonHtml = tab.render({ 'addon:fast': envelope({ at: '12:00:00', machine: fixture.machine, version: 1 }) }, {});

// A repo with no collector: the server's error answer (data null, the error names the repo).
const noCollectorHtml = tab.render({ 'addon:fast': { at_ms: 1, refresh_s: 2, data: null, error: 'monitor not installed in owner/other-repo' } }, {});

// Errors: a failed read shows its error text (escaped), with or without the last good data.
const failedHtml = tab.render({ 'addon:fast': { at_ms: 1, refresh_s: 2, data: null, error: 'owner/other-repo: exit 1: <boom> & more' } }, {});
const failedWithData = tab.render({ 'addon:fast': { at_ms: 1, refresh_s: 2, data: fixture, error: 'read failed: fast refresh' } }, {});
const missing = tab.render({}, {});

// The laptop offline with no `lost` time: the last sight (`seen`) is the time the card names.
const seenOnly = clone(fixture);
delete seenOnly.remote.offline.lost;
delete seenOnly.remote.offline.lost_ago;
seenOnly.remote.offline.seen = '18:40';
seenOnly.remote.offline.seen_ago = '1 h 23 min';
const seenHtml = tab.render({ 'addon:fast': envelope(seenOnly) }, {});

// esc: the status line is escaped, never drawn as markup.
const injected = clone(fixture);
injected.status = '<img src=x onerror=1> PoB';
const injectedHtml = tab.render({ 'addon:fast': envelope(injected) }, {});

const readingsPart = html.slice(html.indexOf('data-part="readings"'));
const readingsRows = readingsPart.split('data-row="reading"').slice(1).map(part => plain(part.split('data-row="reading"')[0]));
const firstReading = plain(readingsPart.slice(readingsPart.indexOf('data-row="reading"')).split('</section>')[0]);

console.log(JSON.stringify({
  id: tab.id,
  title: tab.title,
  order: tab.order,
  endpoints: tab.endpoints,
  laptop_card: html.includes('data-part="remote"'),
  runs_card: html.includes('data-part="runs"'),
  readings_card: html.includes('data-part="readings"'),
  status_shown: html.includes('PoB 0/0 slots'),
  slots_shown: html.includes('data-part="lanes"'),
  daemon_shown: html.includes('data-part="daemon"'),
  running_cells: cellsOf(html, 'running'),
  running_expected: runs.running.length,
  stale_marked: html.includes('data-stale="1"'),
  build_cells: cellsOf(html, 'build'),
  queued_cells: cellsOf(html, 'queued'),
  queued_expected: runs.queued.length,
  recent_expected: runs.recent.length,
  recent_rows: cellsOf(html, 'recent').length,
  history_cells: cellsOf(html, 'history').slice(0, 2),
  history_rows: cellsOf(html, 'history').length,
  history_expected: Math.min(20, runs.history.length),
  readings_rows: readingsRows.length,
  readings_expected: fixture.readings.rows.length,
  readings_sub: (html.match(/обновлено ([^<]*)</) || [])[1] ?? null,
  first_reading: firstReading,
  card_name_default: cardName(html),
  card_name_given: cardName(titledHtml),
  laptop: laptopOf(html),
  laptop_seen_only: laptopOf(seenHtml),
  no_addon_line: noAddonHtml.includes('у этого репо нет дополнения'),
  no_addon_has_cards: noAddonHtml.includes('data-part='),
  no_collector_banner: noCollectorHtml.includes('monitor not installed in owner/other-repo'),
  no_collector_line: noCollectorHtml.includes('у этого репо нет дополнения'),
  no_collector_no_placeholder: !noCollectorHtml.includes('нет данных'),
  failed_error_shown: failedHtml.includes('owner/other-repo: exit 1: &lt;boom&gt; &amp; more') && !failedHtml.includes('<boom>'),
  failed_with_data_shows_both: failedWithData.includes('read failed: fast refresh') && failedWithData.includes('data-part="runs"'),
  missing_envelope_shows_error: missing.includes('data-error'),
  status_escaped: injectedHtml.includes('&lt;img') && !injectedHtml.includes('<img'),
  no_repo_name: !/Poe-Junkroan|poe-junkroan|Rasslabsya4el|PoB/i.test(tabSource),
}));
