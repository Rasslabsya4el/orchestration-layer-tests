// The drawing kit the mods share (#798): the monitor's text measure, `fit`, the dark tokens and `svgDoc`.
// THE one source is this file. A plugin can import only inside its own
// folder (`claude plugin validate`: "outside the plugin's folder"), so `python -m tools.agentops mods
// install|test|status` copies it to `hooks/draw.ts` of each mod first (generated, gitignored; tools/agentops/mods.py).
// Pure functions of their arguments: no `$`, no state.

export const PX = 12
export const FONT = `Segoe UI Variable Text,Segoe UI,system-ui,sans-serif`
export const K = {
  tile: '#262628', text: '#f0f6fc', muted: '#9198a1', faint: '#656c76', rule: '#38383c', sel: '#1d3048',
  active: '#3fb950', done: '#ab7df8', failed: '#f85149', wait: '#d29922', idle: '#7d7d86', accent: '#4493f8',
  link: '#6ca5e9', // the host Link colour of a ticket `#N` (#837 u12: the autopilot's measurement of the baseline 50ea5412 picture, 108,165,233)
} as const
export const besc = (s: unknown) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
// Width of a run of text at `px` font size: the sans the desktop draws, Latin and Cyrillic alike.
export const textWidth = (s: unknown, px = PX, bold = false) => {
  let w = 0
  for (const ch of String(s)) w += /[MWШЩЖЮмшщжю@%]/.test(ch) ? 0.86 : /[il.,:;'|!ı·]/.test(ch) ? 0.3 : /[A-ZА-Я0-9#]/.test(ch) ? 0.64 : ch === ' ' ? 0.28 : 0.53
  return w * px * (bold ? 1.06 : 1)
}
export const fit = (v: unknown, room: number, px = PX, bold = false) => {
  const s = String(v ?? '')
  if (room <= 8) return ''
  if (textWidth(s, px, bold) <= room) return s
  let t = s
  while (t.length > 1 && textWidth(t + '…', px, bold) > room) t = t.slice(0, -1)
  return t.trimEnd() + '…'
}
const ANIM = `.t0,.t1,.t2,.t3{animation:tw 2.2s ease-in-out infinite}.t1{animation-duration:2.8s;animation-delay:-.7s}.t2{animation-duration:1.9s;animation-delay:-1.3s}.t3{animation-duration:3.3s;animation-delay:-.4s}@keyframes tw{0%,100%{opacity:1}50%{opacity:.4}}`
const SPIN = `.sp{animation:rot 1.4s linear infinite;transform-box:fill-box;transform-origin:center}@keyframes rot{to{transform:rotate(360deg)}}`
const PULSE = `.pu{animation:pu 1.6s ease-in-out infinite}@keyframes pu{0%,100%{opacity:1}50%{opacity:.35}}`
const REDUCE = `@media (prefers-reduced-motion:reduce){.t0,.t1,.t2,.t3,.sp,.pu{animation:none}}`
// Only the rules a picture uses go into its <style>: one svg per row makes a shared sheet cost per row.
const RULES: Record<string, string> = {
  ti: `.ti{font:600 14px ${FONT};fill:${K.text}}`, su: `.su{fill:${K.muted}}`, lb: `.lb{font-weight:600;fill:${K.muted}}`,
  nm: `.nm{font-weight:600;font-size:13px;fill:${K.text}}`, tx: `.tx{fill:${K.text}}`, mu: `.mu{fill:${K.muted}}`, fa: `.fa{fill:${K.faint}}`,
  ac: `.ac{fill:${K.accent}}`, lk: `.lk{fill:${K.link};font-size:14px;font-weight:400}`, bt: `.bt{font-size:14px}`, ok: `.ok{fill:${K.active}}`, dn: `.dn{fill:${K.done}}`, er: `.er{fill:${K.failed}}`, wt: `.wt{fill:${K.wait}}`,
  nu: `.nu{font-variant-numeric:tabular-nums}`, kn: `.kn{font-weight:600;fill:#fff;font-variant-numeric:tabular-nums}`, rn: `.rn{font:700 9.5px ${FONT}}`,
  kd: `.kd{font-weight:600;fill:#1c1c1e;font-variant-numeric:tabular-nums}`,
}
const sheet = (body: string) => {
  const used = new Set([...body.matchAll(/class="([^"]+)"/g)].flatMap(m => m[1]!.split(' ')))
  let css = `text{font:400 ${PX}px ${FONT};fill:${K.muted}}` + [...used].map(c => RULES[c] ?? '').join('')
  if (/class="t[0-3]"/.test(body)) css += ANIM
  if (used.has('sp')) css += SPIN
  if (used.has('pu')) css += PULSE
  if (/t[0-3]|sp|pu/.test([...used].join(' '))) css += REDUCE
  return `<style>${css}</style>`
}
export const svgDoc = (W: number, H: number, body: string, defs = '') =>
  `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${sheet(body)}${defs ? `<defs>${defs}</defs>` : ''}${body}</svg>`

// ---- the task-progress bar (#798): one bar cut into the plan's stages, in the monitor's look. Shapes only, no text
// glyphs as icons: a done stage is a filled part, the one in work a filled accent part with a ring and a pulse, a
// pending one an empty outlined part. Under it: the in-work stage's name (cut with `fit`) and the time it has run.
export type Stage = { title: string; status: 'done' | 'working' | 'next'; since?: number }

/** Seconds under 90 s, then minutes, then hours and minutes past 90 min. */
export const humanElapsed = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 90) return `${s} с`
  const m = Math.round(s / 60)
  return m < 90 ? `${m} мин` : `${Math.floor(m / 60)} ч ${m % 60} мин`
}

const BAR_Y = 4
const BAR_H = 8
const GAP = 4
const BAR_ROW = 22 // the line of text under the bar: baseline

export function taskProgressBarSvg(stages: Stage[], nowMs: number, W = 560): string {
  const n = stages.length
  if (n === 0) return ''
  const pad = 2
  const w = (W - 2 * pad - GAP * (n - 1)) / n
  const parts = stages.map((s, i) => {
    const x = (pad + i * (w + GAP)).toFixed(1)
    const common = `x="${x}" y="${BAR_Y}" width="${w.toFixed(1)}" height="${BAR_H}" rx="${BAR_H / 2}"`
    if (s.status === 'done') return `<rect class="sg-done" ${common} fill="${K.done}"/>`
    if (s.status === 'working')
      return `<rect class="sg-work pu" ${common} fill="${K.accent}" stroke="${K.text}" stroke-width="1"/>`
    return `<rect class="sg-next" ${common} fill="none" stroke="${K.faint}" stroke-width="1.5"/>`
  })
  const cur = stages.find(s => s.status === 'working')
  const done = stages.filter(s => s.status === 'done').length
  const right = cur
    ? cur.since === undefined ? `${done + 1} / ${n}` : humanElapsed(nowMs - cur.since)
    : `${done} / ${n}`
  const rightW = textWidth(right) + 8
  const name = cur ? fit(cur.title, W - 2 * pad - rightW - 8) : ''
  const text =
    (name ? `<text class="tx" x="${pad}" y="${BAR_ROW + BAR_Y + 4}">${besc(name)}</text>` : '') +
    `<text class="mu nu" x="${W - pad}" y="${BAR_ROW + BAR_Y + 4}" text-anchor="end">${besc(right)}</text>`
  return svgDoc(W, BAR_ROW + BAR_Y + 10, parts.join('') + text)
}
