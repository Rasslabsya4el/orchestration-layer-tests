// Node check for web/monitor/tab-merges.js (#340 unit 7). Run by tests/test_issue340_tab_merges.py.
// Prints one JSON object on stdout. Renders tests/fixtures/monitor/github.json; no DOM, no network.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import * as tab from '../web/monitor/tab-merges.js';

const fixture = JSON.parse(readFileSync(fileURLToPath(new URL('./fixtures/monitor/github.json', import.meta.url)), 'utf8'));
// The same place forms as tests/test_issue340_monitor_github.py (PLACE): a PC place always carries its reason.
const PLACE = /^(GitHub \d+\/\d+ parts done(, \d+ machines)?(; re-check of \d+ on the PC \(.+?\))?( \(\+ \d+ tests? on the PC\))?|PC \(.+\))$/;

const count = (html, re) => (html.match(re) || []).length;

const html = tab.render({ github: fixture }, {});
const merges = fixture.data.merges;
const minutes = fixture.data.minutes;
const placeValues = [...html.matchAll(/data-place="([^"]*)"/g)].map(m => m[1]);

// Errors: a part's own *_error and the envelope's error both show their text (escaped).
const withErrors = JSON.parse(JSON.stringify(fixture));
withErrors.data.minutes = null;
withErrors.data.minutes_error = 'FileNotFoundError: <no minutes> & more';
withErrors.data.merges = null;
withErrors.data.merges_error = 'PermissionError: merge.log';
withErrors.error = 'read failed: fast refresh';
const errHtml = tab.render({ github: withErrors }, {});
const errorShown =
  errHtml.includes('FileNotFoundError: &lt;no minutes&gt; &amp; more') &&
  errHtml.includes('PermissionError: merge.log') &&
  errHtml.includes('read failed: fast refresh') &&
  !errHtml.includes('<no minutes>');

console.log(JSON.stringify({
  id: tab.id,
  title: tab.title,
  order: tab.order,
  endpoints: tab.endpoints,
  merges_expected: merges.length,
  rows: count(html, /data-row="merge"/g),
  rows_with_place: placeValues.filter(v => PLACE.test(v)).length,
  machines_shown: html.includes('data-part="machines"') && html.includes(`${fixture.data.machines.used} из ${fixture.data.machines.total}`),
  minutes_expected: minutes.length,
  minutes_bars: count(html, /data-minutes="/g),
  error_shown: errorShown,
  bare_pc_rejected: !PLACE.test('PC'),
}));
