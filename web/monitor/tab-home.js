// Главная: rows 1-4 of the pane's main tab, assembled from four panel modules (each one has its own file and owner):
// home-agents.js (row 1), home-merge.js (row 2), home-pc.js (row 3), home-footer.js (row 4).
// A panel is `panel(snapshot, ui) -> html`; it carries its own data-panel / data-row markers and its own error text.
import { agentsPanel } from "./home-agents.js";
import { mergePanel } from "./home-merge.js";
import { pcPanel } from "./home-pc.js";
import { footerPanel } from "./home-footer.js";

export const id = "home";
export const title = "Главная";
export const order = 10;
export const endpoints = ["fast", "tickets"];

export function render(snapshot, ui) {
  return [agentsPanel, mergePanel, pcPanel, footerPanel].map(panel => panel(snapshot, ui)).join("\n");
}
