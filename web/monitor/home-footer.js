// Главная, row 4 (footer panel): the time of the last pull (HH:MM, as the pane's `fresh`) and the ORCH version (drawn as the
// pane does: `v<N>`, only when it is an integer), with the error text of a failed read.
import { esc } from "./shell.js";

const errorLine = text => `<span style="color:var(--bad);white-space:normal;overflow-wrap:anywhere;width:100%">${esc(text)}</span>`;
// The collector's «19:22:04» without its seconds.
const hhmm = at => String(at).replace(/^(\d\d:\d\d):\d\d$/, "$1");

export function footerPanel(snapshot, ui) {
  const fast = snapshot?.fast ?? {};
  const d = fast.data ?? {};
  const parts = [];
  if (d.at) parts.push(`<span>обновлено ${esc(hhmm(d.at))}</span>`);
  if (Number.isInteger(d.version)) parts.push(`<span class="r">v${esc(d.version)}</span>`);
  if (d.version_error) parts.push(errorLine(d.version_error));
  if (fast.error) parts.push(errorLine(fast.error));
  if (!parts.length) parts.push(`<span>Загрузка…</span>`);
  return `<section class="lb" data-panel="footer" data-row="4" style="flex-wrap:wrap">${parts.join("")}</section>`;
}
