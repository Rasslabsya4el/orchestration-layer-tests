import { PX, FONT, besc, textWidth, fit, svgDoc as svgBase } from './draw'
import { EM_CHARS, EM_REG, EM_SB } from './fontmetrics'
import { K, ROLE, GRID, PITCH, CARD_PITCH, LABEL_PITCH, HEAD_PITCH, SP, RADIUS, recolor } from './tokens'
import { WORK_KEY, WORK_NAME, PROJECT_ICONS, HOST_ICON, SLOTS_LABEL, PROJECT_ACT_ICON } from './project'
import type { Cell, Fast, Issue, Machine, PipelineRow, Progress, Split, Tickets } from '../types'

/** Attributes of every tag with `'` instead of `"` (a value holding a `'` keeps its `"`): the JSON the pane's size guard measures loses a backslash per quote. */
export const sq = (svg: string) => svg.replace(/<[a-zA-Z][^>]*>/g, tag => tag.replace(/(\s[\w:-]+)="([^"']*)"/g, "$1='$2'"))
/** #837 (size guard): a right-aligned word (`text-anchor='end' class='..'`) is `class='.. e'` with one rule `.e{text-anchor:end}` in the picture's style sheet, not an attribute of
 *  18 characters a word (76 in the open square); `flat` (test/kit.ts) writes the attribute back for the readers of the old markup. */
const anchorEnd = (svg: string) => {
  let used = false
  const out = svg.replace(/<(?:text|tspan)[^>]*>/g, tag => tag.replace(/ text-anchor='end' class='([^']*)'/, (_m, c) => { used = true; return ` class='${c} e'` }))
  return used ? out.replace('</style>', '.e{text-anchor:end}</style>') : out
}
/** #837 (size guard): a shape (rect / path / circle / polygon / use, no class and no id of its own) a picture draws alike twice or more is ONE
 *  element `id='qN'` in the picture's <defs> and a `<use href='#qN'/>` where it stood (same place in the paint order, same inherited paint);
 *  an element with a class (the spinner, the pulse) stays as it is, and so does a picture with a clipPath or a mask in its body.
 *  Only a copy that saves characters is made. `flat` (test/kit.ts) writes every one out again, so the readers of the old markup read the same. */
const LEAF = /<(?:rect|path|circle|polygon|use)\s[^>]*\/>/g
const dedupe = (svg: string) => {
  const at = svg.indexOf('</defs>'), hasDefs = at >= 0
  const from = hasDefs ? at + 7 : svg.indexOf('</style>') + 8
  const body = svg.slice(from)
  if (from < 8 || /<(?:clipPath|mask)\b/.test(body)) return svg
  const count = new Map<string, number>()
  for (const t of body.match(LEAF) ?? []) if (!/\s(?:class|id)=/.test(t)) count.set(t, (count.get(t) ?? 0) + 1)
  const ids = new Map<string, string>()
  let defs = ''
  for (const [t, n] of count) {
    const id = `q${ids.size}`
    const use = `<use href='#${id}'/>`.length, def = t.replace(/^<(\w+)/, `<$1 id='${id}'`).length
    if (n * (t.length - use) - def <= 0) continue
    ids.set(t, id)
    defs += t.replace(/^<(\w+)/, `<$1 id='${id}'`)
  }
  if (!ids.size) return svg
  const out = body.replace(LEAF, t => (ids.has(t) ? `<use href='#${ids.get(t)}'/>` : t))
  return hasDefs ? svg.slice(0, at) + defs + '</defs>' + out : svg.slice(0, from) + `<defs>${defs}</defs>` + out
}
/** #1094 unit 3b: the passes a picture runs after its markup is built (kit's pack); the add-on fills it at load, so a pane with no add-on runs none */
export const PICTURE_PASSES: ((svg: string) => string)[] = []
export const pack = (svg: string) => PICTURE_PASSES.reduce((s, pass) => pass(s), dedupe(anchorEnd(svg)))
export const svgDoc = (W: number, H: number, body: string, defs = '') => pack(sq(recolor(svgBase(W, H, body, defs))))

// Octicons (MIT, GitHub) and Lucide (ISC) paths, verbatim from the research set (scratchpad 9a5f7c98 research/icons.json):
// [source, inner markup]; Octicons draw on a 16 viewBox and fill, Lucide on a 24 viewBox and stroke. Only the icons this pane uses.
export const ICONS: Record<string, ['o' | 'l', string]> = {
  "bot.hubot.octicons": ["o", "<path d=\"M0 8a8 8 0 0 1 16 0v5.25a.75.75 0 0 1-1.5 0V8a6.5 6.5 0 1 0-13 0v5.25a.75.75 0 0 1-1.5 0Zm3-1.25C3 5.784 3.784 5 4.75 5h6.5c.966 0 1.75.784 1.75 1.75v1.5A1.75 1.75 0 0 1 11.25 10h-6.5A1.75 1.75 0 0 1 3 8.25Zm1.47-.53a.75.75 0 0 0 0 1.06l1.5 1.5a.75.75 0 0 0 1.06 0L8 7.81l.97.97a.75.75 0 0 0 1.06 0l1.5-1.5a.749.749 0 0 0-.326-1.275.749.749 0 0 0-.734.215l-.97.97-.97-.97a.75.75 0 0 0-1.06 0l-.97.97-.97-.97a.75.75 0 0 0-1.06 0Zm1.03 6.03a.75.75 0 0 1 .75-.75h3.5a.75.75 0 0 1 0 1.5h-3.5a.75.75 0 0 1-.75-.75Z\"/>"],
  "message.octicons": ["o", "<path d=\"M1 2.75C1 1.784 1.784 1 2.75 1h10.5c.966 0 1.75.784 1.75 1.75v7.5A1.75 1.75 0 0 1 13.25 12H9.06l-2.573 2.573A1.458 1.458 0 0 1 4 13.543V12H2.75A1.75 1.75 0 0 1 1 10.25Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h2a.75.75 0 0 1 .75.75v2.19l2.72-2.72a.749.749 0 0 1 .53-.22h4.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25Z\"/>"],
  "git-merge": ["o", "<path d=\"M5.45 5.154A4.25 4.25 0 0 0 9.25 7.5h1.378a2.251 2.251 0 1 1 0 1.5H9.25A5.734 5.734 0 0 1 5 7.123v3.505a2.25 2.25 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.95-.218ZM4.25 13.5a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Zm8.5-4.5a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5ZM5 3.25a.75.75 0 1 0 0 .005V3.25Z\"/>"],
  "merge-queue.octicons": ["o", "<path d=\"M3.75 4.5a1.25 1.25 0 1 0 0-2.5 1.25 1.25 0 0 0 0 2.5ZM3 7.75a.75.75 0 0 1 1.5 0v2.878a2.251 2.251 0 1 1-1.5 0Zm.75 5.75a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Zm5-7.75a1.25 1.25 0 1 1-2.5 0 1.25 1.25 0 0 1 2.5 0Zm5.75 2.5a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0Zm-1.5 0a.75.75 0 1 0-1.5 0 .75.75 0 0 0 1.5 0Z\"/>"],
  "cpu.octicons": ["o", "<path d=\"M6.5.75V2h3V.75a.75.75 0 0 1 1.5 0V2h1.25c.966 0 1.75.784 1.75 1.75V5h1.25a.75.75 0 0 1 0 1.5H14v3h1.25a.75.75 0 0 1 0 1.5H14v1.25A1.75 1.75 0 0 1 12.25 14H11v1.25a.75.75 0 0 1-1.5 0V14h-3v1.25a.75.75 0 0 1-1.5 0V14H3.75A1.75 1.75 0 0 1 2 12.25V11H.75a.75.75 0 0 1 0-1.5H2v-3H.75a.75.75 0 0 1 0-1.5H2V3.75C2 2.784 2.784 2 3.75 2H5V.75a.75.75 0 0 1 1.5 0Zm5.75 11.75a.25.25 0 0 0 .25-.25v-8.5a.25.25 0 0 0-.25-.25h-8.5a.25.25 0 0 0-.25.25v8.5c0 .138.112.25.25.25ZM5.75 5h4.5a.75.75 0 0 1 .75.75v4.5a.75.75 0 0 1-.75.75h-4.5a.75.75 0 0 1-.75-.75v-4.5A.75.75 0 0 1 5.75 5Zm.75 4.5h3v-3h-3Z\"/>"],
  "monitor.octicons": ["o", "<path d=\"M14.25 1c.966 0 1.75.784 1.75 1.75v7.5A1.75 1.75 0 0 1 14.25 12h-3.727c.099 1.041.52 1.872 1.292 2.757A.752.752 0 0 1 11.25 16h-6.5a.75.75 0 0 1-.565-1.243c.772-.885 1.192-1.716 1.292-2.757H1.75A1.75 1.75 0 0 1 0 10.25v-7.5C0 1.784.784 1 1.75 1ZM1.75 2.5a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h12.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25ZM9.018 12H6.982a5.72 5.72 0 0 1-.765 2.5h3.566a5.72 5.72 0 0 1-.765-2.5Z\"/>"],
  "failed.octicons": ["o", "<path d=\"M2.344 2.343h-.001a8 8 0 0 1 11.314 11.314A8.002 8.002 0 0 1 .234 10.089a8 8 0 0 1 2.11-7.746Zm1.06 10.253a6.5 6.5 0 1 0 9.108-9.275 6.5 6.5 0 0 0-9.108 9.275ZM6.03 4.97 8 6.94l1.97-1.97a.749.749 0 0 1 1.275.326.749.749 0 0 1-.215.734L9.06 8l1.97 1.97a.749.749 0 0 1-.326 1.275.749.749 0 0 1-.734-.215L8 9.06l-1.97 1.97a.749.749 0 0 1-1.275-.326.749.749 0 0 1 .215-.734L6.94 8 4.97 6.03a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018Z\"/>"],
  "pause.octicons": ["o", "<path d=\"M5 2h1a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Zm5 0h1a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-1a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z\"/>"],
  "done.octicons": ["o", "<path d=\"M0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm1.5 0a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Zm10.28-1.72-4.5 4.5a.75.75 0 0 1-1.06 0l-2-2a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018l1.47 1.47 3.97-3.97a.751.751 0 0 1 1.042.018.751.751 0 0 1 .018 1.042Z\"/>"],
  "waiting.octicons": ["o", "<path d=\"M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Zm7-3.25v2.992l2.028.812a.75.75 0 0 1-.557 1.392l-2.5-1A.751.751 0 0 1 7 8.25v-3.5a.75.75 0 0 1 1.5 0Z\"/>"],
  "hourglass.octicons": ["o", "<path d=\"M2.75 1h10.5a.75.75 0 0 1 0 1.5h-.75v1.25a4.75 4.75 0 0 1-1.9 3.8l-.333.25a.25.25 0 0 0 0 .4l.333.25a4.75 4.75 0 0 1 1.9 3.8v1.25h.75a.75.75 0 0 1 0 1.5H2.75a.75.75 0 0 1 0-1.5h.75v-1.25a4.75 4.75 0 0 1 1.9-3.8l.333-.25a.25.25 0 0 0 0-.4L5.4 7.55a4.75 4.75 0 0 1-1.9-3.8V2.5h-.75a.75.75 0 0 1 0-1.5ZM11 2.5H5v1.25c0 1.023.482 1.986 1.3 2.6l.333.25c.934.7.934 2.1 0 2.8l-.333.25a3.251 3.251 0 0 0-1.3 2.6v1.25h6v-1.25a3.251 3.251 0 0 0-1.3-2.6l-.333-.25a1.748 1.748 0 0 1 0-2.8l.333-.25a3.251 3.251 0 0 0 1.3-2.6Z\"/>"],
  "flask.octicons": ["o", "<path d=\"M5 5.782V2.5h-.25a.75.75 0 0 1 0-1.5h6.5a.75.75 0 0 1 0 1.5H11v3.282l3.666 5.76C15.619 13.04 14.543 15 12.767 15H3.233c-1.776 0-2.852-1.96-1.899-3.458Zm-2.4 6.565a.75.75 0 0 0 .633 1.153h9.534a.75.75 0 0 0 .633-1.153L12.225 10.5h-8.45ZM9.5 2.5h-3V6c0 .143-.04.283-.117.403L4.73 9h6.54L9.617 6.403A.746.746 0 0 1 9.5 6Z\"/>"],
  "running": ["l", "<path d=\"M21 12a9 9 0 1 1-6.219-8.56\" />"],
  "issue-opened": ["o", "<path d=\"M8 9.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z\"/><path d=\"M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Z\"/>"],
  "search.octicons": ["o", "<path d=\"M10.68 11.74a6 6 0 0 1-7.922-8.982 6 6 0 0 1 8.982 7.922l3.04 3.04a.749.749 0 0 1-.326 1.275.749.749 0 0 1-.734-.215ZM11.5 7a4.499 4.499 0 1 0-8.997 0A4.499 4.499 0 0 0 11.5 7Z\"/>"],
  "file-code.octicons": ["o", "<path d=\"M4 1.75C4 .784 4.784 0 5.75 0h5.586c.464 0 .909.184 1.237.513l2.914 2.914c.329.328.513.773.513 1.237v8.586A1.75 1.75 0 0 1 14.25 15h-9a.75.75 0 0 1 0-1.5h9a.25.25 0 0 0 .25-.25V6h-2.75A1.75 1.75 0 0 1 10 4.25V1.5H5.75a.25.25 0 0 0-.25.25v2.5a.75.75 0 0 1-1.5 0Zm1.72 4.97a.75.75 0 0 1 1.06 0l2 2a.75.75 0 0 1 0 1.06l-2 2a.749.749 0 0 1-1.275-.326.749.749 0 0 1 .215-.734l1.47-1.47-1.47-1.47a.75.75 0 0 1 0-1.06ZM3.28 7.78 1.81 9.25l1.47 1.47a.751.751 0 0 1-.018 1.042.751.751 0 0 1-1.042.018l-2-2a.75.75 0 0 1 0-1.06l2-2a.751.751 0 0 1 1.042.018.751.751 0 0 1 .018 1.042Zm8.22-6.218V4.25c0 .138.112.25.25.25h2.688l-.011-.013-2.914-2.914-.013-.011Z\"/>"],
  "book.octicons": ["o", "<path d=\"M0 1.75A.75.75 0 0 1 .75 1h4.253c1.227 0 2.317.59 3 1.501A3.743 3.743 0 0 1 11.006 1h4.245a.75.75 0 0 1 .75.75v10.5a.75.75 0 0 1-.75.75h-4.507a2.25 2.25 0 0 0-1.591.659l-.622.621a.75.75 0 0 1-1.06 0l-.622-.621A2.25 2.25 0 0 0 5.258 13H.75a.75.75 0 0 1-.75-.75Zm7.251 10.324.004-5.073-.002-2.253A2.25 2.25 0 0 0 5.003 2.5H1.5v9h3.757a3.75 3.75 0 0 1 1.994.574ZM8.755 4.75l-.004 7.322a3.752 3.752 0 0 1 1.992-.572H14.5v-9h-3.495a2.25 2.25 0 0 0-2.25 2.25Z\"/>"],
  "file-directory.octicons": ["o", "<path d=\"M1.75 1A1.75 1.75 0 0 0 0 2.75v10.5C0 14.216.784 15 1.75 15h12.5A1.75 1.75 0 0 0 16 13.25v-8.5A1.75 1.75 0 0 0 14.25 3H7.5a.25.25 0 0 1-.2-.1l-.9-1.2C6.07 1.26 5.55 1 5 1H1.75ZM1.5 2.75a.25.25 0 0 1 .25-.25H5a.25.25 0 0 1 .2.1l.9 1.2c.33.44.85.7 1.4.7h6.75a.25.25 0 0 1 .25.25v8.5a.25.25 0 0 1-.25.25H1.75a.25.25 0 0 1-.25-.25V2.75Z\"/>"],
  "git-commit.octicons": ["o", "<path d=\"M11.93 8.5a4.002 4.002 0 0 1-7.86 0H.75a.75.75 0 0 1 0-1.5h3.32a4.002 4.002 0 0 1 7.86 0h3.32a.75.75 0 0 1 0 1.5Zm-1.43-.75a2.5 2.5 0 1 0-5 0 2.5 2.5 0 0 0 5 0Z\"/>"],
  "git-pull-request": ["o", "<path d=\"M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z\"/>"],
  "globe.octicons": ["o", "<path d=\"M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM5.78 8.75a9.64 9.64 0 0 0 1.363 4.177c.255.426.542.832.857 1.215.245-.296.551-.705.857-1.215A9.64 9.64 0 0 0 10.22 8.75Zm4.44-1.5a9.64 9.64 0 0 0-1.363-4.177c-.307-.51-.612-.919-.857-1.215a9.927 9.927 0 0 0-.857 1.215A9.64 9.64 0 0 0 5.78 7.25Zm-5.944 1.5H1.543a6.507 6.507 0 0 0 4.666 5.5c-.123-.181-.24-.365-.352-.552-.715-1.192-1.437-2.874-1.581-4.948Zm-2.733-1.5h2.733c.144-2.074.866-3.756 1.58-4.948.12-.197.237-.381.353-.552a6.507 6.507 0 0 0-4.666 5.5Zm10.181 1.5c-.144 2.074-.866 3.756-1.58 4.948-.12.197-.237.381-.353.552a6.507 6.507 0 0 0 4.666-5.5Zm2.733-1.5a6.507 6.507 0 0 0-4.666-5.5c.123.181.24.365.353.552.714 1.192 1.436 2.874 1.58 4.948Z\"/>"],
  "eye.octicons": ["o", "<path d=\"M8 2c1.981 0 3.671.992 4.933 2.078 1.27 1.091 2.187 2.345 2.637 3.023a1.62 1.62 0 0 1 0 1.798c-.45.678-1.367 1.932-2.637 3.023C11.67 13.008 9.981 14 8 14c-1.981 0-3.671-.992-4.933-2.078C1.797 10.83.88 9.576.43 8.898a1.62 1.62 0 0 1 0-1.798c.45-.677 1.367-1.931 2.637-3.022C4.33 2.992 6.019 2 8 2ZM1.679 7.932a.12.12 0 0 0 0 .136c.411.622 1.241 1.75 2.366 2.717C5.176 11.758 6.527 12.5 8 12.5c1.473 0 2.825-.742 3.955-1.715 1.124-.967 1.954-2.096 2.366-2.717a.12.12 0 0 0 0-.136c-.412-.621-1.242-1.75-2.366-2.717C10.824 4.242 9.473 3.5 8 3.5c-1.473 0-2.825.742-3.955 1.715-1.124.967-1.954 2.096-2.366 2.717ZM8 10a2 2 0 1 1-.001-3.999A2 2 0 0 1 8 10Z\"/>"],
  "chevron-right.octicons": ["o", "<path d=\"M6.22 3.22a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.751.751 0 0 1-1.042-.018.751.751 0 0 1-.018-1.042L9.94 8 6.22 4.28a.75.75 0 0 1 0-1.06Z\"/>"],
  "issue-closed": ["o", "<path d=\"M11.28 6.78a.75.75 0 0 0-1.06-1.06L7.25 8.69 5.78 7.22a.75.75 0 0 0-1.06 1.06l2 2a.75.75 0 0 0 1.06 0l3.5-3.5Z\"/><path d=\"M16 8A8 8 0 1 1 0 8a8 8 0 0 1 16 0Zm-1.5 0a6.5 6.5 0 1 0-13 0 6.5 6.5 0 0 0 13 0Z\"/>"],
  "circle.octicons": ["o", "<path d=\"M0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm8-6.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13Z\"/>"],
  "draft": ["l", "<circle cx=\"12\" cy=\"12\" r=\"9.5\" stroke-dasharray=\"3.9 3.56\"/>"],
  "person": ["l", "<circle cx=\"12\" cy=\"7.5\" r=\"4\"/><path d=\"M4.5 21v-1.5a5 5 0 0 1 5-5h5a5 5 0 0 1 5 5V21\"/>"],
  "skip": ["l", "<circle cx=\"12\" cy=\"12\" r=\"9.5\"/><path d=\"M5.3 5.3l13.4 13.4\"/>"],
  "target": ["l", "<circle cx=\"12\" cy=\"12\" r=\"10\"/><circle cx=\"12\" cy=\"12\" r=\"6\"/><circle cx=\"12\" cy=\"12\" r=\"2\"/>"],
  // #15: a subagent row's role (Lucide, ISC; lucide-icons/lucide main, fetched 2026-10-16): worker, worker-strong, researcher-find, researcher-how, any other type (bot)
  "wrench": ["l", "<path d=\"M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z\"/>"],
  "hammer": ["l", "<path d=\"m15 12-9.373 9.373a1 1 0 0 1-3.001-3L12 9\"/><path d=\"m18 15 4-4\"/><path d=\"m21.5 11.5-1.914-1.914A2 2 0 0 1 19 8.172v-.344a2 2 0 0 0-.586-1.414l-1.657-1.657A6 6 0 0 0 12.516 3H9l1.243 1.243A6 6 0 0 1 12 8.485V10l2 2h1.172a2 2 0 0 1 1.414.586L18.5 14.5\"/>"],
  "list": ["l", "<path d=\"M3 5h.01\"/><path d=\"M3 12h.01\"/><path d=\"M3 19h.01\"/><path d=\"M8 5h13\"/><path d=\"M8 12h13\"/><path d=\"M8 19h13\"/>"],
  "book-open": ["l", "<path d=\"M12 5v16\"/><path d=\"M20.001 19A2 2 0 0022 17V5a2 2 0 00-1.999-2L16 3.002A5 5 0 0012 5a5 5 0 00-4-2H4a2 2 0 00-2 2v12a2 2 0 001.999 2H8a5 5 0 014 2 5 5 0 014-2z\"/>"],
  "bot": ["l", "<path d=\"M12 8V4H8\"/><rect width=\"16\" height=\"12\" x=\"4\" y=\"8\" rx=\"2\"/><path d=\"M2 14h2\"/><path d=\"M20 14h2\"/><path d=\"M15 13v2\"/><path d=\"M9 13v2\"/>"],
  "chart": ["l", "<path d=\"M3 3v18h18\"/><path d=\"M7 16v-5\"/><path d=\"M12 16V7\"/><path d=\"M17 16v-8\"/>"],
  "terminal.octicons": ["o", "<path d=\"M0 2.75C0 1.784.784 1 1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0 1 14.25 15H1.75A1.75 1.75 0 0 1 0 13.25Zm1.75-.25a.25.25 0 0 0-.25.25v10.5c0 .138.112.25.25.25h12.5a.25.25 0 0 0 .25-.25V2.75a.25.25 0 0 0-.25-.25ZM7.25 8a.749.749 0 0 1-.22.53l-2.25 2.25a.749.749 0 0 1-1.275-.326.749.749 0 0 1 .215-.734L5.44 8 3.72 6.28a.749.749 0 0 1 .326-1.275.749.749 0 0 1 .734.215l2.25 2.25c.141.14.22.331.22.53Zm1.5 1.5h3a.75.75 0 0 1 0 1.5h-3a.75.75 0 0 1 0-1.5Z\"/>"],
}
Object.assign(ICONS, PROJECT_ICONS)

// #683 r4c (approved 09.10 23:5x, design in the MODS chat 37114277): every row of
// the main tab is ONE svg picture on a shared grid, and the native presses (a ticket Link, a plain Button over a
// title) lie over a row in an absolute Box at the column of their fact: a desktop Box `position: absolute` is placed
// in cells (top in rows, left in columns of 7.8 px) against its parent, so only a row's own top edge is reachable,
// never a y inside one card picture. A picture leaves a pressed cell's text out: the native element draws it.
// Colour = GitHub meaning (active, done, failed, wait); identity colours only in the machine cards.
// One pane column in CSS px (measured live 09.10 21:4x at 100 % zoom: 123 columns = a 960-px card content).
export const PX_PER_COL = 7.8
/** #837 u17: a card picture's slot is the pane's columns minus this many px (the pane's gutter 9.4 + the tile's border and padding 20, measured live at 123 columns: slot 930) */
export const CARD_CHROME_PX = 29.4
// #837 unit 11, autopilot comparison 00:08Z: a run title (running / waiting row) is the baseline 50ea5412's Button, its colours are K.titleText / K.titleDim; #837 b9b B3: it is drawn on the
// column x like the head and the other rows' titles (the Button's 6 px offset is gone: the title is drawn here, the Button is a ghost)
export const PILL = 20
/** #837 pass 2 (baseline 50ea5412): a card strip row = a 20-px pill + 6 px air = 26 px (CARD_PITCH); a table row is 19 px (GRID.row) */
export const AIR = CARD_PITCH - PILL
export const TRACK = 24
export const RAIL = GRID.rail // the left column: icons and the rail line, never text
export const P0 = RAIL + 4
// #683 r5 (owner 10.10: «так много строк что тяжеловато считывается»): a nested row is shifted right, INDENT px a
// level (0..2): its pill, its name and its rail mark move; the tick / meta / num / act / time columns stay on the grid.
export const INDENT = GRID.indent

export const hex = (c: string) => [1, 3, 5].map(i => parseInt(c.slice(i, i + 2), 16))
export const rgb = (c: number[]) => `rgb(${c.map(v => Math.round(v)).join(',')})`
export const mix = (a: number[], b: number[], t: number) => a.map((v, i) => v + ((b[i] ?? 0) - v) * t)
// A stable pseudo-random value per (a, b, c): the dither and the twinkle land on the same pixels every redraw.
export const hash = (a: number, b: number, c: number) => {
  let h = (a * 374761393 + b * 668265263 + c * 2147483647) | 0
  h = Math.imul(h ^ (h >>> 13), 1274126177)
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296
}
// The dither is one <pattern> tile, w px wide, repeated under the fill (a dot path per pixel over a whole bar cost
// ~8k chars, #683 r3); `rows` are the dots' y, `dens` the share lit; each dot twinkles in one of four phases.
export const dither = (id: string, w: number, h: number, rows: number[], dens: number, seed: number, paint: string) => {
  const m = new Map<string, string>()
  for (let col = 0; col * 3 < w; col++)
    rows.forEach((y, r) => {
      if (hash(col, r, seed) > dens) return
      const cls = `t${Math.floor(hash(col, r, seed + 1) * 4)}`
      m.set(cls, `${m.get(cls) ?? ''}M${col * 3} ${y}h2v2h-2z`)
    })
  return `<pattern id="${id}" width="${w}" height="${h}" patternUnits="userSpaceOnUse"><g ${paint}>${[...m].map(([c, d]) => `<path class="${c}" d="${d}"/>`).join('')}</g></pattern>`
}

export const icon = (name: string, cx: number, cy: number, size: number, color: string, extra = '', id = '') => {
  const ic = ICONS[name]
  if (!ic) return `<circle cx="${cx}" cy="${cy}" r="${size / 4}" fill="${color}"/>`
  const s = size / (ic[0] === 'l' ? 24 : 16)
  const paint = ic[0] === 'l'
    ? `fill="none" stroke="${color}" stroke-width="${(1.5 / s).toFixed(2)}" stroke-linecap="round" stroke-linejoin="round"`
    : `fill="${color}"`
  return `<g${id ? ` id="${id}"` : ''} transform="translate(${n1(cx - size / 2)} ${n1(cy - size / 2)}) scale(${+s.toFixed(4)})" ${paint}><g ${extra}>${ic[1]}</g></g>`
}

// ---- #771: the pictures of the rows. A row picture draws its icon as ONE element: a single-path icon as a <path> that
// carries its own transform and paint (the nested <g><g> a table's icon keeps costs ~30 characters each; an icon that
// spins keeps the inner <g class="sp">: a CSS animation of `transform` would replace the path's own transform).
const ONE_PATH = /^<path d="([^"]*)"\s*\/>$/
const iconPlace = (ic: ['o' | 'l', string], cx: number, cy: number, size: number, color: string) => {
  const s = size / (ic[0] === 'l' ? 24 : 16)
  const paint = ic[0] === 'l'
    ? `fill="none" stroke="${color}" stroke-width="${(1.5 / s).toFixed(2)}" stroke-linecap="round" stroke-linejoin="round"`
    : `fill="${color}"`
  return { at: `transform="translate(${n1(cx - size / 2)} ${n1(cy - size / 2)}) scale(${+s.toFixed(4)})"`, paint }
}
export const rowIcon = (name: string, cx: number, cy: number, size: number, color: string, extra = '', id = '') => {
  const ic = ICONS[name]
  if (!ic) return icon(name, cx, cy, size, color, extra, id)
  const { at, paint } = iconPlace(ic, cx, cy, size, color)
  const one = !extra && ONE_PATH.exec(ic[1])
  if (one) return `<path${id ? ` id="${id}"` : ''} ${at} ${paint} d="${one[1]}"/>`
  return `<g${id ? ` id="${id}"` : ''} ${at} ${paint}>${extra ? `<g ${extra}>${ic[1]}</g>` : ic[1]}</g>`
}
/** What a row draws its icons, its dither and its ids through: alone (a pressed row: its own picture, the ids the row
 *  always had) or shared (a run of rows with no press in ONE picture: the dither tile of one colour and each icon used
 *  twice or more are defined once in the picture's <defs> and reused). */
export type RowCtx = {
  icon: typeof rowIcon
  dither: (w: number, h: number, rows: number[], dens: number, seed: number, paint: string) => string
  /** #613: the id of a thin time-bar segment (`w` px wide, its top at `y`) defined once in the picture's <defs>; a row places it with <use> */
  seg: (w: number, y: number) => string
  /** #613: a shape that two or more rows of one picture draw alike (`content` has no fill of its own where `paint` is to apply):
   *  one <g> in the picture's <defs> and a <use> carrying `paint` each; drawn once, or in a picture of its own: `flat`
   *  (default `<g paint>content</g>`) as the row always drew it */
  share: (content: string, paint: string, flat?: string) => string
  /** the suffix of the ids a row's own defs carry (a track's gradient, mask and clip) */ sfx: string
  defs: string[]
}
const segDef = (id: string, w: number, y: number) => `<rect id="${id}" y="${y}" width="${w}" height="8" rx="1.5"/>`
const soloCtx = (): RowCtx => {
  const defs: string[] = []
  return { icon: rowIcon, defs, sfx: '', dither: (w, h, rows, dens, seed, paint) => { defs.push(dither('d', w, h, rows, dens, seed, paint)); return 'd' },
    seg: (w, y) => { defs.push(segDef('s', w, y)); return 's' },
    share: (content, paint, flat) => flat ?? `<g ${paint}>${content}</g>` }
}
const sharedCtx = () => {
  const defs: string[] = []
  const tiles = new Map<string, string>()
  const calls: Parameters<typeof rowIcon>[] = []
  const shapes: { content: string; paint: string; flat: string }[] = []
  const ctx: RowCtx = {
    defs, sfx: '',
    // a later row of the same tile (size, colour) reuses the first one's dots
    dither: (w, h, rows, dens, seed, paint) => {
      const key = `${w}|${h}|${paint}`
      let id = tiles.get(key)
      if (!id) { id = `d${tiles.size}`; tiles.set(key, id); defs.push(dither(id, w, h, rows, dens, seed, paint)) }
      return id
    },
    seg: (w, y) => {
      const key = `s|${w}|${y}`
      let id = tiles.get(key)
      if (!id) { id = `s${tiles.size}`; tiles.set(key, id); defs.push(segDef(id, w, y)) }
      return id
    },
    icon: (...a) => { calls.push(a); return `\u0001${calls.length - 1}\u0002` },
    share: (content, paint, flat) => { shapes.push({ content, paint, flat: flat ?? `<g ${paint}>${content}</g>` }); return `\u0003${shapes.length - 1}\u0004` },
  }
  /** `body` with each icon call resolved: an icon drawn twice or more in the picture is one def and a <use> each. */
  const finish = (body: string) => {
    const count = new Map<string, number>()
    for (const [name, , , , , extra, id] of calls) if (ICONS[name] && !extra && !id) count.set(name, (count.get(name) ?? 0) + 1)
    const ids = new Map<string, string>()
    const defsOut = [...defs]
    for (const [name, n] of count) {
      if (n < 2) continue
      const inner = ICONS[name]![1]
      const one = ONE_PATH.exec(inner)
      ids.set(name, `i${ids.size}`)
      defsOut.push(one ? `<path id="i${ids.size - 1}" d="${one[1]}"/>` : `<g id="i${ids.size - 1}">${inner}</g>`)
    }
    // #613: a shape two or more rows draw alike is one <g> in the defs
    const shapeN = new Map<string, number>()
    for (const sh of shapes) shapeN.set(sh.content, (shapeN.get(sh.content) ?? 0) + 1)
    const shapeId = new Map<string, string>()
    for (const [content, n] of shapeN) {
      if (n < 2) continue
      shapeId.set(content, `b${shapeId.size}`)
      defsOut.push(`<g id="b${shapeId.size - 1}">${content}</g>`)
    }
    const out = body.replace(/\u0003(\d+)\u0004/g, (_, k) => {
      const sh = shapes[Number(k)]!
      const ref = shapeId.get(sh.content)
      return ref ? `<use href="#${ref}" ${sh.paint}/>` : sh.flat
    }).replace(/\u0001(\d+)\u0002/g, (_, k) => {
      const a = calls[Number(k)]!
      const [name, cx, cy, size, color, extra, id] = a
      const ref = ids.get(name)
      if (!ref || extra || id) return rowIcon(...a)
      const { at, paint } = iconPlace(ICONS[name]!, cx, cy, size, color)
      return `<use href="#${ref}" ${at} ${paint}/>`
    })
    return { body: out, defs: defsOut.join('') }
  }
  return { ctx, finish }
}

// ---- a card's title band: its own picture (the card's first row); `state` = a word right of the title line
export function headSvg(title: [string | null, string], sub: string, W: number, state?: { t: string; cls: string }) {
  const [tIcon, tText] = title
  // #837 b9b B9: the sub is a tspan of the title's own text, 12 px (dx) after the title's end as the host sets it: an x worked out from textWidth stood 5, 16 or 33 px off the real title (the font table is off by -7 .. +21 px)
  const body = (tIcon ? icon(tIcon, RAIL / 2, 12, 16, K.text) : '') + `<text x="${P0}" y="17" class="ti">${besc(tText)}` +
    (sub ? `<tspan dx="12" class="su" font-weight="400" font-size="12">${besc(sub)}</tspan>` : '') + '</text>' +
    (state ? `<text x="${W - 12}" y="17" text-anchor="end" class="${state.cls}">${besc(state.t)}</text>` : '')
  return svgDoc(W, 30, body)
}

// ---- the tab bar (#683 r5 pt 8, variant B picked by the owner 10.10 03:0x): no tile; each label is a native plain
// Button at a whole pane column (measured live 02:35: a label starts ~2 px right of its column and runs ~12.4 px per
// text unit), the selected one carries a 3-px accent underline on a 1-px rule across the pane, as GitHub's repo tabs.
// No hotkey digits, no count badges (the owner, 10.10 00:3x).
export const TABH = GRID.tab // the native label fills the first 20 px; the underline and the rule sit under it
export function tabsSvg(labels: string[], sel: number, W: number) {
  const segs: { x: number; w: number; col: number }[] = []
  let col = 1
  for (const l of labels) {
    const x = col * PX_PER_COL + 1
    const w = textWidth(l, 11.8) + 1 // live 03:0x: «Главная» is 45 px wide, the 12.4 estimate ran 7 px past it
    segs.push({ x, w, col })
    col = Math.ceil((x + w + 32) / PX_PER_COL)
  }
  const s = segs[sel]
  const body = `<rect x="0" y="${TABH - 1}" width="${W}" height="1" fill="${K.rule}"/>` +
    (s ? `<rect x="${(s.x - 2).toFixed(1)}" y="${TABH - 3}" width="${(s.w + 4).toFixed(1)}" height="3" rx="1.5" fill="${K.accent}"/>` : '')
  return { svg: svgDoc(W, TABH, body), cols: segs.map(g => g.col) }
}

// ---- THE table of the pane (#683 r5 (0)): ONE plain picture on the native 19-px line grid, so a native hover Box
// laid over each data row lines up with it (an interactive Svg is a frame that blinks on every redraw). Line 0 = the
// column labels and the 1-px rule; a group = a blank line (not before the first) + its label line + its rows; a row
// = a soft band (zebra) with its text. `rows` = the grid line of every data row, for the hover Boxes. The markup is
// kept small (the tree budget): the text of a column is one <text> of <tspan>s, all the ratio bars are five paths.
//
// #683 r5 (E) (owner 10.10: «должны ВСЕ таблицы быть одного вида/стиля»; «Если что то не умещается и многоточит,
// значит оно там быть не должно»): the Tickets, Cost and Progress tabs draw their tables with this very picture,
// `whole` = no word is ever cut: the columns come from tableFit (each as wide as its widest word, a column with no
// room dropped for the whole table), and the one column that is long by nature (`wrap`: a ticket's title) goes on
// onto further grid lines, its row then `spans` that many lines (band, hover Box). A cell drawn native (a ticket's
// Link, the tree's toggle) is left out of the picture and returned in `hot` with its row and x; a `dot` cell is a
// state dot, a `share` column a plain bar (no mark), `dx` shifts a cell right (the tree's indent).
/** #1094 unit 3b: a column's own drawing, named by the add-on (the versus bar: `ratio`) */
export type ColKind = 'ratio'
export type TCol = { label: string; x: number; end?: boolean; kind?: ColKind
  /** its words wrap inside `w` px onto further grid lines (whole tables) */ wrap?: boolean; w?: number
  /** #994: a title: ONE line in the `w` px the fit leaves it (as `wrap` takes them), cut with an ellipsis */ clip?: boolean
  /** a share bar this many px wide from x: a cell's `share` (0..1) fills it */ share?: number }
export type TCellV = string | { t: string; cls?: string; ratio?: number
  /** a muted run right after the text, in the same cell */ tail?: string
  /** drawn native over the row at the cell's x: a Link (`card` = the ticket whose hover card it carries) or a plain Button */
  href?: string; press?: Press; card?: number
  /** an icon of the pane's set instead of words: its name, its colour; `turn` = the degrees it is turned by (a
   *  chevron pointing down); with `press` the icon is drawn and the native press lies over it, its label blank */
  icon?: [string, string]; turn?: number
  /** the fill of a share bar, 0..1 */ share?: number
  /** px the cell stands right of its column (a tree's indent) */ dx?: number }
export type TGroup = { title?: string; rows: TCellV[][] }
export type THot = { row: number; x: number; t: string; href?: string; press?: Press; card?: number }
export const TROW = GRID.row
/** #671 u4: the engine's measured line height in px (a native Box's `top`/`height` are in these cells; the picture's row is TROW px). */
export const CELL_PX = 19
/** a table row's height in px: the table row of the baseline 50ea5412 (= TROW). Equal to CELL_PX, so a native Box (cells) covers it. */
export const LROW = TROW
/** A table row's grid line (picture y = line * LROW) in the engine's cells. */
export const cellTop = (line: number) => line * LROW / CELL_PX
export const cellH = (span: number) => Math.max(1, span * LROW / CELL_PX)
export const TBAND = GRID.band // a band's left edge: two pane columns, where the hover Box starts
export const BAR_W = 64
export const TICON = 12 // an icon of a table cell and of the legend
export const n1 = (v: number) => String(Math.round(v * 10) / 10)
export const n1r = (v: number) => Math.round(v * 10) / 10
/** `t` in lines no wider than `room`, broken at spaces; a single word wider than the room is broken where the room
 *  ends: every character is drawn, none is replaced by «…». */
export const wrapText = (t: string, room: number, px = PX): string[] => {
  const lines: string[] = []
  let cur = ''
  for (let word of t.split(/\s+/).filter(Boolean)) {
    const both = cur ? `${cur} ${word}` : word
    if (textWidth(both, px) <= room) { cur = both; continue }
    if (cur) lines.push(cur)
    while (word.length > 1 && textWidth(word, px) > room) {
      let k = word.length - 1
      while (k > 1 && textWidth(word.slice(0, k), px) > room) k--
      lines.push(word.slice(0, k))
      word = word.slice(k)
    }
    cur = word
  }
  if (cur) lines.push(cur)
  return lines.length ? lines : ['']
}
export function tableSvg(cols: TCol[], groups: TGroup[], TW: number, whole = false, uniq = false): { svg: string; rows: number[]; spans: number[]; hot: THot[] } {
  const rows: number[] = []
  const spans: number[] = []
  const hot: THot[] = []
  const zebra: string[] = []
  const titles: string[] = []
  const dots: string[] = []
  const iconDefs = new Map<string, string>() // `uniq`: each distinct icon is drawn once in <defs>, a row only places it with <use> (#671)
  const texts: { cls?: string; spans: string[] }[] = cols.map(() => ({ spans: [] }))
  const put = (ci: number, x: number, y: number, t: string, cls: string, tail?: string) => {
    const col = texts[ci]!
    col.cls ??= cls
    col.spans.push(`<tspan x="${x}" y="${y}"${cls !== col.cls ? ` class="${cls}"` : ''}>${besc(t)}</tspan>` + (tail ? `<tspan class="mu">${besc(tail)}</tspan>` : ''))
  }
  const track: string[] = [], mark: string[] = []
  const fills: Record<string, string[]> = {}
  let n = 1
  groups.forEach((g, gi) => {
    if (g.title) {
      if (gi) n++
      titles.push(`<tspan x="${P0}" y="${n * LROW + 14}">${besc(g.title)}</tspan>`)
      n++
    }
    g.rows.forEach((cells, ri) => {
      const y = n * LROW
      const obj = (ci: number) => { const v = cells[ci]; return v == null || v === '' ? null : typeof v === 'string' ? { t: v } as Exclude<TCellV, string> : v }
      // a whole table: the wrapped column's lines first, they say how many grid lines the row takes
      const wrapped = cols.map((c, ci) => { const o = obj(ci); return whole && c.wrap && o && !o.icon && !o.href && !o.press && o.card == null ? wrapText(o.t, Math.max(48, (c.w ?? TW - c.x - 12) - (o.dx ?? 0) - 4)) : null })
      const span = Math.max(1, ...wrapped.map(w => w?.length ?? 1))
      rows.push(n)
      spans.push(span)
      n += span
      if (ri % 2) zebra.push(span === 1 ? `<use href="#z" y="${y}"/>` : `<rect x="${TBAND}" y="${y + 1}" width="${TW - TBAND}" height="${span * LROW - 2}" rx="5" fill="${K.veil}" fill-opacity=".06"/>`)
      cols.forEach((c, ci) => {
        const o = obj(ci)
        if (!o) return
        if (c.kind === 'ratio') { // наш | bar + % | автор, a "versus" row: the bar is full at 200 %, its mark stands at 100 %
          if (o.ratio == null) return
          const f = Math.max(0, Math.min(2, o.ratio / 100)) / 2
          const col = o.ratio >= 100 ? K.active : o.ratio >= 80 ? K.idle : K.failed
          track.push(`M${c.x + 3} ${y + 10}h${BAR_W - 6}`)
          if (f > 0) (fills[col] ??= []).push(`M${c.x + 3} ${y + 10}h${n1(Math.max(0.1, BAR_W * f - 6))}`)
          mark.push(`M${c.x + BAR_W / 2} ${y + 5}v10`)
          if (o.t) put(ci, c.x + BAR_W + 44, y + 14, o.t, o.cls ?? 'tx')
          return
        }
        const x = n1r(c.x + (o.dx ?? 0))
        if (c.share) { // a share of a whole: the versus bar's track and fill, no mark
          if (o.share == null) return
          const f = Math.max(0, Math.min(1, o.share))
          track.push(`M${n1(x + 3)} ${y + 10}h${n1(c.share - 6)}`)
          if (f > 0) (fills[K.accent] ??= []).push(`M${n1(x + 3)} ${y + 10}h${n1(Math.max(0.1, c.share * f - 6))}`)
          return
        }
        if (o.icon) { // 12 px, centred on the line; under a native press it stands where the press's blank label does
          const cx = o.press ? x + 9 : c.end ? x - 6 : x + 7
          const rot = o.turn ? `transform="rotate(${o.turn} ${ICONS[o.icon[0]]?.[0] === 'l' ? '12 12' : '8 8'})"` : ''
          if (uniq && ICONS[o.icon[0]]) {
            const ik = `${o.icon[0]}|${o.icon[1]}|${o.turn ?? 0}`
            if (!iconDefs.has(ik)) iconDefs.set(ik, icon(o.icon[0], 0, 0, TICON, o.icon[1], rot, `i${iconDefs.size}`))
            dots.push(`<use href="#i${[...iconDefs.keys()].indexOf(ik)}" x="${n1r(cx)}" y="${n1r(y + LROW / 2)}"/>`)
          } else dots.push(icon(o.icon[0], n1r(cx), y + LROW / 2, TICON, o.icon[1], rot))
          if (!o.press) return
        }
        // #972: a ticket's number is drawn here (class ac, 2 px right of its column as the host Link stood), the pointer element over it is blank; a press's face (an icon) is drawn above
        if (o.href || o.press || o.card != null) {
          hot.push({ row: rows.length - 1, x, t: o.t, href: o.href, press: o.press, card: o.card })
          if (!o.press) put(ci, x + 2, y + 14, o.t || `#${o.card}`, 'ac')
          return
        }
        if (wrapped[ci]) { wrapped[ci]!.forEach((l, k) => put(ci, x, y + k * LROW + 14, l, o.cls ?? 'tx')); return }
        if (whole && c.clip) { put(ci, x, y + 14, fit(o.t, Math.max(48, (c.w ?? TW - c.x - 12) - (o.dx ?? 0) - 4)), o.cls ?? 'tx'); return }
        if (whole) { put(ci, x, y + 14, o.t, o.cls ?? 'tx', o.tail); return }
        const nx = cols[ci + 1]
        const room = nx ? Math.abs((nx.end ? nx.x - 70 : nx.x) - c.x) - 10 : TW - c.x - 12
        put(ci, c.x, y + 14, c.end ? o.t : fit(o.t, room), o.cls ?? 'tx')
      })
    })
  })
  const line = (d: string[], paint: string) => (d.length ? `<path d="${d.join('')}" fill="none" ${paint}/>` : '')
  // (a native label starts ~2 px right of its pane column: the label of a column of native cells stands over them)
  const body = cols.map(c => (whole && !c.label ? '' : `<text x="${c.x}" y="13" class="mu"${c.end ? ' text-anchor="end"' : ''}>${besc(c.label)}</text>`)).join('') +
    `<rect x="${TBAND}" y="${LROW - 1}" width="${TW - TBAND}" height="1" fill="${K.rule}"/>` + zebra.join('') +
    (titles.length ? `<text class="lb">${titles.join('')}</text>` : '') +
    line(track, `stroke="${K.veil}" stroke-opacity=".16" stroke-width="6" stroke-linecap="round"`) +
    Object.entries(fills).map(([col, d]) => line(d, `stroke="${col}" stroke-width="6" stroke-linecap="round"`)).join('') +
    line(mark, `stroke="${K.muted}" stroke-opacity=".6"`) + dots.join('') +
    texts.map((t, ci) => (t.spans.length
      ? `<text class="${t.cls}${cols[ci]!.end || cols[ci]!.kind === 'ratio' ? ' nu" text-anchor="end"' : '"'}>${t.spans.join('')}</text>` : '')).join('')
  const defs = (zebra.some(z => z.startsWith('<use')) ? `<rect id="z" x="${TBAND}" y="1" width="${TW - TBAND}" height="${LROW - 2}" rx="5" fill="${K.veil}" fill-opacity=".06"/>` : '') + [...iconDefs.values()].join('')
  return { svg: svgDoc(TW, n * LROW, body, defs), rows, spans, hot }
}
// The columns of a whole table, fitted to its own words (the owner's rule above, fitGrid's idea on a table). The
// trailing run of end-aligned and share columns is packed from the right edge, each as wide as its widest word (its
// label too; `w` = a width it never goes under, so the same column lines up across the tables of a tab); the others
// flow from the left, each at its designed x or right of the words before it (a column with a native cell lands on a
// whole pane column); the `wrap` column takes what is left between the two. While the words do not fit, the columns
// named in `drop` leave the table one at a time, in that order, with their cells.
export type TFit = TCol & { key: string; /** the px between this column's words and the next column's (default TGAP) */ gap?: number; /** its words' width, when not measured */ need?: number }
export const TGAP = 14
export const TWRAP_MIN = 140 // the least a wrapped column is worth drawing in
export const hotW = (o: Exclude<TCellV, string>) => (o.press ? textWidth(o.t, 14) + 10 : textWidth(o.t, 13) + 4) // a native label: 13 px, a Button's padding
export function tableFit(spec: TFit[], groups: TGroup[], drop: string[], TW: number): { cols: TCol[]; groups: TGroup[] } {
  const native = spec.map(() => false)
  const need = spec.map((c, ci) => {
    let w = Math.max(c.w ?? 0, textWidth(c.label))
    for (const g of groups) for (const cells of g.rows) {
      const v = cells[ci]
      if (v == null || v === '') continue
      const o = typeof v === 'string' ? { t: v } : v
      if (o.href || o.press || o.card != null) native[ci] = true
      w = Math.max(w, c.share ?? (o.icon ? TICON : o.href || o.press || o.card != null ? hotW(o) : textWidth(o.t + (o.tail ?? ''))))
    }
    return Math.ceil(c.need ?? (c.share ?? w))
  })
  for (let n = 0; ; n++) {
    const kept = spec.map((_, i) => i).filter(i => !drop.slice(0, n).includes(spec[i]!.key))
    const xs = new Map<number, number>()
    const ws = new Map<number, number>()
    let right = TW - 12
    let k = kept.length - 1
    for (; k >= 0; k--) {
      const i = kept[k]!, c = spec[i]!
      if (!c.end && !c.share) break
      xs.set(i, c.end ? right : right - need[i]!)
      right -= need[i]! + (c.gap ?? TGAP)
    }
    let at = 0
    let fits = true
    for (let j = 0; j <= k; j++) {
      const i = kept[j]!, c = spec[i]!
      let x = Math.max(c.x, Math.ceil(at))
      if (native[i]) x = n1r(Math.ceil(x / PX_PER_COL - 0.01) * PX_PER_COL)
      xs.set(i, x)
      if (c.wrap || c.clip) { ws.set(i, right - x); if (right - x < TWRAP_MIN) fits = false; at = right }
      else { at = x + need[i]! + (c.gap ?? TGAP); if (x + need[i]! > right) fits = false }
    }
    if (!fits && n < drop.length) continue
    return {
      cols: kept.map(i => { const { key: _k, gap: _g, need: _n, ...c } = spec[i]!; return { ...c, x: xs.get(i)!, ...(ws.has(i) ? { w: ws.get(i)! } : {}) } }),
      groups: groups.map(g => ({ ...g, rows: g.rows.map(cells => kept.map(i => cells[i] ?? '')) })),
    }
  }
}
// An icon and its word on one 19-px line (a ticket's state on its hover card, the card's author); `own` = the word
// in the icon's colour.
export function markSvg(name: string, color: string, t: string, own = false) {
  return svgDoc(Math.ceil(TICON + 8 + textWidth(t)), TROW, icon(name, TICON / 2 + 1, TROW / 2, TICON, color) + `<text x="${TICON + 6}" y="14"${own ? ` style="fill:${color}"` : ''}>${besc(t)}</text>`)
}

// ---- the head of a detail panel (a square of a run, a row of a list tab): ONE picture: the 64-px face (pixel art) at
// the left, the title, up to three muted lines, each fitted to what the native open buttons (`btnW` px at the
// right edge) leave.
export const FACE = 64
export function detailSvg(face: string | undefined, title: string, lines: { t: string; cls?: string }[], TW: number, btnW: number): string {
  const ls = lines.slice(0, 3)
  const x0 = face ? TBAND + FACE + 12 : P0
  const room = TW - x0 - btnW - 12
  // three lines beside a face are set tighter, so the head stays as tall as the face
  const tight = !!face && ls.length === 3
  const ys = tight ? [15, 31, 46, 60] : [17, 36, 54, 72]
  const body = (face ? `<image href="${face}" x="${TBAND}" width="${FACE}" height="${FACE}" style="image-rendering:pixelated"/>` : '') +
    `<text x="${x0}" y="${ys[0]}" class="ti">${besc(fit(title, room, 14, true))}</text>` +
    ls.map((l, i) => `<text x="${x0}" y="${ys[i + 1]}" class="${l.cls ?? 'mu'}">${besc(fit(l.t, room))}</text>`).join('')
  // a face is 64 px tall and takes a row's air under it (what follows is a table's label line); text alone ends 7 px
  // under its last baseline
  const need = ys[ls.length]! + 7
  return svgDoc(TW, face ? Math.max(FACE, tight ? 0 : need) + AIR : need, body)
}

// ---- one row = one picture on the shared grid; `hot` = where a native press lies over it ------------------------
export type RowCol = { key: string; x: number; w: number; align?: 'end'; num?: boolean; cls?: string
  /** #994: px the column's HEAD word stands right of `x` (a ticket column: its numbers are drawn a pane column right of it) */ hx?: number }
export type Press = { key: string; on: () => void; dim?: boolean }
/** #1094 unit 3b: a cell the add-on draws (a line's seeds, addon.tsx seedKind): where its drawing ends, where a title may run on up to, and the drawing into the row */
export type CellKind = { end: () => number; start: () => number; draw: (ctx: RowCtx, cy: number, parts: string[], hot: Hot[]) => void }
export type CellV = string | {
  t?: string; cls?: string; icon?: string; spin?: boolean; iconColor?: string; color?: string
  /** #837 b11 R3: the text stands where it would after an icon (18 px in) although this cell has none: a column of texts, some with an icon and some without, starts at one x */ inset?: boolean
  /** px the cell stands right of its column */ dx?: number
  /** a ticket Link (drawn native at this x) */ href?: string
  /** a plain Button (drawn native at this x) */ press?: Press
  /** a replay entry's time bar: `n` thin segments, `done` of them lit */ stages?: { n: number; done: number; color: string
    /** #773: a run's entries bar: `done` segments filled (no pulse), drawn as two dashed strokes, right-aligned in its column;
     *  the terminal prints the cell's words `t` instead */ solid?: boolean }
  /** #773: a run's priority / machine / slots as three small pictures (the terminal prints the cell's words `t`):
   *  `lit` of 3 rising bars, the host's icon, one small square per slot held, then `tail` (a finished row's date and start);
   *  `t` = all of it as words */
  pics?: { lit: number; host: string; slots: number; tail?: string }
  /** #711: an entry's pipeline: one zone per group of steps, `w` = each group's measured share of run time, `fill` =
   *  how far each is done (0..1), `at` = the group the entry is in now (it pulses) */
  zones?: { w: number[]; fill: number[]; at: number; color: string }
  kind?: CellKind
  /** status marks: an icon in its state's colour and its word, one after another */
  marks?: { icon: string; color: string; t: string; cls?: string }[]
}
/** #837 b8: the screen px one no-break space of the pane's Markdown link is underlined over (the press probe 2026-10-12: a link of 35 of them, 97 px) */
export const NBSP_PX = 97 / 35
/** #972: the blank character of a pressEl's label over a drawn face and its advance in screen px in the host's 13-px button font: the EM SPACE (U+2003): 14.75 screen px each in the host's button font, read off the real pane
 *  (#972 checkpoint 3, 2026-10-13 21:46: the Tickets tab's `#780` button of 12 of them lit 177 px), so a 300-px title is ~21 characters of the tree instead of ~108 no-break spaces
 *  (the tree's size against the host's 100,000-character limit: the heaviest fixture went from 97,292 to under 90,000). */
export const BLANK_CH = ' '
/** #994 (defect 10, the owner's «seeds hover square»): a blank label's light as the real pane draws it (read 14.10 06:2x off four lights: a title of 21 em spaces 280 px, a seed's
 *  2 em + 5 no-break spaces 48 px, a ticket number's 2 + 3 42 px, a chevron's 1 + 4 33 px): LABEL_EM px an em space, LABEL_NB a no-break space, plus LABEL_PAD of the host's own padding.
 *  BLANK_PX (13.3) and NBSP_PX (2.77) with no padding fit long titles only: every short face lit ~10 px wider than itself. */
export const LABEL_EM = 13, LABEL_NB = 3.1, LABEL_PAD = 7
export const BLANK_PX = 13.3 // #972 re-read on the real pane 13.10 23:05: four titles of 275–284 px lit 250–258 px with 14.75 (0.90 of the text, ~28 px short); 13.3 fits all four within 1 px
/** #837 b9 A8 (measured in the real pane, picture 12_home_hover2: two titles, underlines of 22 and 99 no-break spaces = 61 and 274 px, 2.77 px each, the same constant): the underline starts
 *  GHOST_INSET px left of its Box's cell edge, and the host draws a card's Segoe text narrower than `textWidth` says (38 / 231 / 36 px drawn against 53 / 269 / 43 estimated): a drawn word's width is
 *  `textWidth x GHOST_TEXT`, a figure that leaves every one of those three words a little short of its estimate (the underline runs on past it, never stops inside it). */
export const GHOST_INSET = 1.4
export const GHOST_TEXT = 0.88
/** #837 b11 R2: a drawn word's width is the sum of the MEASURED advances of Segoe UI (`fontmetrics.ts`, made by bin/gen_font_metrics.py: segoeui.ttf regular, seguisb.ttf semibold) at GHOST_PX picture px, not
 *  `textWidth x GHOST_TEXT` (a flat guess per character class that was 4 to 14 % off a string, by its letters). GHOST_PX is the size the owner's pane draws a row title at: 11.8 screen px on three titles
 *  of 36 to 77 chars (r10_runs / r10_merge pictures: 231.0 / 274.5 / 282 px) = 12.2 picture px at the card's fit 0.9657; semibold at 13.2 (the 13 px bold class x the same 12.2 / 12). A character the table does not hold keeps the old estimate. */
const emMap = (a: number[]) => new Map([...EM_CHARS].map((c, i) => [c, a[i]! / 1000] as const))
const EM_W = { reg: emMap(EM_REG), sb: emMap(EM_SB) }
export const GHOST_PX = 12.2
export const GHOST_PX_BOLD = 13.2
export const drawnW = (s: string, bold = false, px = bold ? GHOST_PX_BOLD : GHOST_PX) => {
  const m = bold ? EM_W.sb : EM_W.reg
  let w = 0
  for (const ch of String(s)) { const e = m.get(ch); w += e != null ? e * px : textWidth(ch, 14, bold) * GHOST_TEXT }
  return w
}
/** The link of a drawn span (markup x, w; `fit` = screen px a markup px; `e` = the cells the row's Box stands left of its card): its Box's `left` in cells and its no-break spaces, so the label starts on the
 *  cell edge nearest the span's first px (a Box stands on whole cells of 7.8 px: the start is off by at most half of one) and ends over its last px; at least 2 blanks (#972: BLANK_CH of BLANK_PX each). */
export const ghostSpan = (x: number, w: number, fit: number, e: number): { L: number; n: number } => {
  const L = Math.max(0, Math.round((x * fit + GHOST_INSET) / PX_PER_COL) - e)
  return { L, n: Math.max(2, Math.ceil(((x + w) * fit - ((L + e) * PX_PER_COL - GHOST_INSET)) / BLANK_PX)) }
}
// ---- the owner's rule (10.10): «Если что то не умещается и многоточит, значит оно там быть не должно». A grid keeps
// each column where the design put it while the widest words of the columns before it fit; words wider than their
// column push the later columns right (an end-aligned column stays at the pane's edge). When the pushed columns no
// longer fit before it, the columns named in `drop` leave the grid, one at a time in that order, until the rest fit:
// a fact with no room is not drawn at all (its row's alt and the open panel still say it), never cut with «…».
// `need` = the widest thing each column draws, in px (absent / 0 = nothing); `right` = the pill's inner right edge.
export const GRID_GAP = SP.m
export function fitGrid(grid: RowCol[], need: Record<string, number>, drop: string[], right: number): RowCol[] {
  let out = grid
  for (let n = 0; n <= drop.length; n++) {
    const cols = grid.filter(c => !drop.slice(0, n).includes(c.key))
    const ends = cols.filter(c => c.align === 'end' && need[c.key])
    // where the left-anchored words must end: before the end-aligned ones, else at the pill's edge
    const limit = ends.length ? Math.min(...ends.map(c => c.x + c.w - need[c.key]!)) - GRID_GAP : right
    let at = 0
    let fits = true
    out = cols.map(c => {
      if (c.align === 'end') return c
      const x = Math.max(c.x, Math.ceil(at))
      const own = Math.ceil(need[c.key] ?? 0)
      if (own) { at = x + own + GRID_GAP; if (x + own > limit) fits = false }
      const w = Math.max(c.w - (x - c.x), own)
      return x === c.x && w === c.w ? c : { ...c, x, w }
    })
    if (fits) break
  }
  return out
}
/** `t` with as many of its « · » parts, from the first, as fit `room` whole: a part with no room goes, no word is
 *  cut ('' when not even the first one fits). */
export const whole = (t: string, room: number, px = PX, bold = false) => {
  const parts = t.split(' · ')
  while (parts.length && textWidth(parts.join(' · '), px, bold) > room) parts.pop()
  return parts.join(' · ')
}
/** A level-0 row: `up` / `down` = the line at x RAIL/2 above / below its mark. A nested row (lvl 1..2) hangs off its
 *  parent's line (one INDENT left of its mark) by an elbow: `down` = that line goes on to a later sibling, `sub` = a
 *  line starts under its own mark (it has children), `outer` (lvl 2) = the level-0 line passes through the row. */
export type Rail = { icon?: string; dot?: boolean; ring?: boolean; num?: number; img?: string; color?: string; up?: boolean; down?: boolean; sub?: boolean; outer?: boolean; through?: boolean; lineColor?: string; size?: number; spin?: boolean }
export type Hot = { x: number; w: number; /** #837 b5: where the picture draws the link's text when that is not `x` (a cell's link is drawn one cell right of its press) */ tx?: number; label: string; href?: string; press?: Press; cls?: string; bold?: boolean; sel?: boolean; ctl?: boolean
  /** #837 b17: the press is a host Button whose own label is visible (the picture draws no text for it): a pager word, a run's title */ visible?: boolean
  /** #837 b8: where the picture draws this press's text or capsule (markup px); a ghost Markdown link lies over exactly that */ drawn?: { x: number; w: number }
  /** #994: a pager's icon button ('prev' / 'next'): its chevron drawn in the picture, `x`..`x + w` its face */ pg?: 'prev' | 'next'
  /** #995 send-back 2 row 3: the row's own press (its title's): the pane lays its Button over the row's whole pill */ title?: boolean }
export type RowSpec = {
  kind: 'strip' | 'track' | 'label' | 'gap' | 'head'
  /** a strip's nesting, 0..2: the row is shifted right by INDENT a level */ lvl?: number
  state?: keyof typeof K; color?: string; sel?: boolean; calm?: boolean; child?: boolean; bold?: boolean
  /** drawn faded: a remote-host row kept from its last reading while the link is down */ fade?: boolean
  /** #994: a placeholder («пусто», «ничего»): a strip that lights nothing under the pointer */ still?: boolean
  h?: number; text?: string; right?: string; rightPad?: number
  /** #837 b12: a second muted text of a label row, its END at x `end` (markup px), left of `right` */ lead?: { t: string; end: number }
  frac?: number; knob?: string; ticks?: number[]
  rail?: Rail; cells?: Record<string, CellV>; hot?: Hot[]
  /** #793 F14: a ticket row: rail icon, then the number (a native Link) at the name column, the name after it */
  ticket?: { n: number; href: string }
  /** the picture's alt (what a reader without it hears; the tests read it) */ alt?: string
}
/** #994: a pager's icon button: a 20-px face (its light 20 x 19, near square; under the 26 px a blank face is read by its ink), the chevron (octicons' chevron-right; turned for
 *  «previous») centred on the host Button's 19-px line; two of them PG_GAP apart */
export const PG_W = 20
export const PG_GAP = 6
export const PG_CY = 9.5
export const pagerIcon = (which: 'prev' | 'next', cx: number, cy: number, dim: boolean) =>
  icon('chevron-right.octicons', n1r(cx), cy, 16, dim ? K.faint : K.text, which === 'prev' ? 'transform="rotate(180 8 8)"' : '')
/** #837 b9a: the placeholder pill of a section with no rows (the queue's «пусто», a run's with no entries): a dot in the rail and one muted word */
export const emptyRow = (): RowSpec => ({ kind: 'strip', state: 'idle', calm: true, still: true, rail: { dot: true, color: K.idle }, cells: { name: { t: 'пусто', cls: 'fa' } } })
/** The width a ticket's number (a native Link, 13 px) takes in front of its name. */
export const ticketW = (n: number) => Math.ceil(textWidth(`#${n}`, 13) + 4) + 8
/** #837 b5: a strip row's pill as drawn, in the row's own coordinates (y 0 = the row's top): the row's hover highlight has this very shape. */
export type Pill = { x: number; y: number; w: number; h: number; rx: number }
function rowBody(r: RowSpec, i: number, cols: RowCol[], W: number, ctx: RowCtx): { body: string; h: number; hot: Hot[]; pill?: Pill } {
  const parts: string[] = []
  const defs = ctx.defs
  const hot: Hot[] = [...(r.hot ?? [])]
  const faded = (p: string[]) => (r.fade ? `<g opacity=".45">${p.join('')}</g>` : p.join(''))
  // a row is as high as it is drawn (a strip: its 20-px pill + 6 px air): no pitch padding (#837 pass 2, baseline 50ea5412)
  let hh = 0
  const pad = 0, padB = 0
  const seat = (h: number, _content?: number) => { hh = h; return h }
  const railAt = (x: number, a0: number, b0: number, col?: string) => {
    const a = a0 <= 0 ? -pad : a0, b = b0 >= hh ? hh + padB : b0
    return b > a ? `<rect x="${x - 0.75}" y="${a}" width="1.5" height="${(b - a).toFixed(1)}" fill="${col ?? K.rule}"/>` : ''
  }
  const railLine = (a: number, b: number, col?: string) => railAt(RAIL / 2, a, b, col)
  // only a strip is nested: a track, a label and a gap stay at level 0
  const lvl = r.kind === 'strip' ? Math.max(0, Math.min(2, r.lvl ?? 0)) : 0
  const off = lvl * INDENT
  // #793 F04: the column heads of a card's rows: muted words at each column's x (the cells' own `t`), as a table's head line
  if (r.kind === 'head') {
    const h = seat(HEAD_PITCH) // #837: the head is not a row of the baseline (the owner's F04): it takes the baseline's label height
    for (const col of cols) {
      const o = r.cells?.[col.key]
      const t = typeof o === 'string' ? o : o?.t ?? ''
      if (t) parts.push(`<text x="${n1(col.align === 'end' ? col.x + col.w : col.x + (col.hx ?? 0))}" y="16"${col.align === 'end' ? ' text-anchor="end"' : ''} class="mu">${besc(t)}</text>`)
    }
    return { body: parts.join(''), h, hot }
  }
  if (r.kind === 'gap' || r.kind === 'label') {
    const h = seat(r.kind === 'gap' ? r.h ?? 6 : LABEL_PITCH)
    if (r.rail?.through) parts.push(railLine(0, h, r.rail.lineColor))
    if (r.kind === 'label') {
      parts.push(`<text x="${P0 + 12}" y="15" class="lb">${besc(r.text ?? '')}</text>`)
      if (r.right) parts.push(`<text x="${W - 12 - (r.rightPad ?? 0)}" y="15" text-anchor="end" class="mu nu">${besc(r.right)}</text>`)
      if (r.lead?.t) parts.push(`<text x="${n1(r.lead.end)}" y="15" text-anchor="end" class="mu nu">${besc(r.lead.t)}</text>`)
      for (const hp of r.hot ?? []) if (hp.href) parts.push(`<text x="${n1(hp.x)}" y="15" class="lk">${besc(hp.label)}</text>`)
      // #994: a pager is two icon buttons: the icon drawn here, a blank host Button over its face; the end of the range is drawn dim and gets no Button (it lights nothing)
      for (const hp of r.hot ?? []) if (hp.pg) parts.push(pagerIcon(hp.pg, hp.x + hp.w / 2, PG_CY, !!hp.press?.dim))
      // #837 b17: a pager word (`ctl`, `visible`) is a host Button with its own visible label (the ghost link over a drawn word took 4 presses of 13 in the real pane): the picture draws nothing for it
    }
    return { body: parts.join(''), h, hot }
  }
  const ph = r.kind === 'track' ? TRACK : PILL
  // no cell wraps (#837 pass 2 rule 3, withdraws #822 N3): a row is ONE line; words with no room are cut as at baseline 50ea5412
  // #837 unit 11, autopilot comparison 00:08Z: a square a slot (at most 8), then the date after them, as the baseline 50ea5412 drew it in its 120 px meta column:
  // the squares stay, the date (the tail) is the one that is cut to the room left (`fit`)
  const h = seat(ph + AIR)
  const c = r.color ?? (r.state ? K[r.state] : K.idle)
  const cy = ph / 2
  // the rail column
  if (r.rail) {
    const rc = r.rail.color ?? c
    // a row with no mark of its own (a track) lets the line pass unbroken
    const bare = !r.rail.img && !r.rail.dot && !r.rail.ring && !r.rail.icon && r.rail.num == null
    const half = bare ? 0 : r.rail.img ? 9 : r.rail.dot || r.rail.ring ? 4 : 8
    const mx = RAIL / 2 + off // the mark's centre
    const lc = r.rail.lineColor
    if (!lvl) {
      if (r.rail.up) parts.push(railLine(0, cy - half, lc))
      if (r.rail.down) parts.push(railLine(cy + half, h, lc))
    } else {
      const px = mx - INDENT // the parent's line
      if (r.rail.outer) parts.push(railLine(0, h, lc))
      parts.push(railAt(px, 0, r.rail.down ? h : cy + 0.75, lc))
      parts.push(`<rect x="${px}" y="${cy - 0.75}" width="${INDENT - half}" height="1.5" fill="${lc ?? K.rule}"/>`)
      if (r.rail.sub) parts.push(railAt(mx, cy + half, h, lc))
    }
    if (r.rail.img) parts.push(`<image href="${r.rail.img}" x="${mx - 8}" y="${cy - 8}" width="16" height="16" style="image-rendering:pixelated"/>`)
    else if (r.rail.num != null) parts.push(`<circle cx="${mx}" cy="${cy}" r="7" fill="${K.tile}" stroke="${rc}" stroke-width="1.4"/><text x="${mx}" y="${cy + 3.6}" text-anchor="middle" class="rn" style="fill:${rc}">${besc(r.rail.num)}</text>`)
    else if (r.rail.ring) parts.push(`<circle cx="${mx}" cy="${cy}" r="2.8" fill="none" stroke="${rc}" stroke-width="1.4"/>`)
    else if (r.rail.dot) parts.push(`<circle cx="${mx}" cy="${cy}" r="3.2" fill="${rc}"/>`)
    else if (r.rail.icon) parts.push(ctx.icon(r.rail.icon, mx, cy, r.rail.size ?? 14, rc, r.rail.spin ? 'class="sp"' : ''))
  }
  const px0 = P0 + off
  let pill: Pill | undefined
  if (r.kind === 'track') {
    const frac = Math.max(0, Math.min(1, r.frac ?? 0))
    const fx = px0 + frac * (W - px0)
    const did = ctx.dither(72, ph, [1, 4, 7, 10, 13, 16, 19], 0.35, i + 1, `fill="${rgb(mix(hex(c), [255, 255, 255], 0.35))}"`)
    defs.push(`<linearGradient id="g${ctx.sfx}" x1="${px0}" x2="${fx.toFixed(1)}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="${c}" stop-opacity=".06"/><stop offset="1" stop-color="${c}" stop-opacity=".42"/></linearGradient>`)
    defs.push(`<linearGradient id="mg${ctx.sfx}" x1="${px0}" x2="${fx.toFixed(1)}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="${K.white}" stop-opacity=".1"/><stop offset="1" stop-color="${K.white}" stop-opacity=".5"/></linearGradient><mask id="m${ctx.sfx}"><rect x="${px0}" width="${W}" height="${ph}" fill="url(#mg${ctx.sfx})"/></mask>`)
    defs.push(`<clipPath id="c${ctx.sfx}"><rect x="${px0}" width="${W - px0}" height="${ph}" rx="${ph / 2}"/></clipPath>`)
    let ticks = ''
    for (const t of r.ticks ?? []) {
      const tx = px0 + t * (W - px0)
      ticks += `<rect x="${(tx - 1).toFixed(1)}" y="6" width="2" height="${ph - 12}" rx="1" fill="${K.white}" opacity="${tx < fx ? 0.55 : 0.25}"/>`
    }
    parts.push(`<g clip-path="url(#c${ctx.sfx})"><rect x="${px0}" width="${W - px0}" height="${ph}" fill="${K.veil}" fill-opacity=".14"/><rect x="${px0}" width="${(fx - px0).toFixed(1)}" height="${ph}" fill="url(#g${ctx.sfx})"/><g transform="translate(${px0} 0)"><rect width="${(fx - px0).toFixed(1)}" height="${ph}" fill="url(#${did})" mask="url(#m${ctx.sfx})"/></g>${ticks}</g>`)
    if (r.knob) {
      const kw = textWidth(r.knob, PX, true) + 20
      const kx = Math.max(px0 + kw / 2, Math.min(W - kw / 2, fx))
      parts.push(`<rect x="${(kx - kw / 2).toFixed(1)}" width="${kw.toFixed(1)}" height="${ph}" rx="${ph / 2}" fill="${c}"/><text x="${kx.toFixed(1)}" y="${ph / 2 + 4.2}" text-anchor="middle" class="kd">${besc(r.knob)}</text>`)
    }
    return { body: faded(parts), h, hot }
  }
  // a strip
  // #837 pass 2 (baseline 50ea5412): a strip is a full pill (rx = h/2) in the state's colour at .09 (calm) / .12 (child) / .17,
  // no zebra under it; the selected row = the selected fill with an accent outline. #613: the pill and a running row's
  // dither are shapes of the picture's defs, a <use> a row.
  // #837 b5: the pill's numbers are written once, here; the picture's rect and the hover highlight (`pill` of the result) both take them
  pill = { x: px0, y: 0, w: W - px0, h: ph, rx: ph / 2 }
  if (r.sel) parts.push(`<rect x="${pill.x}" width="${pill.w}" height="${pill.h}" rx="${pill.rx}" fill="${K.sel}"/><rect x="${px0 + 0.5}" y=".5" width="${W - px0 - 1}" height="${ph - 1}" rx="${ph / 2 - 0.5}" fill="none" stroke="${K.accent}" stroke-opacity=".55"/>`)
  else {
    const fillOp = r.calm ? 0.09 : r.child ? 0.12 : 0.17
    const pillRect = `<rect x="${pill.x}" width="${pill.w}" height="${pill.h}" rx="${pill.rx}"/>`
    const paint = `fill="${c}" fill-opacity="${fillOp}"`
    // #994 (defect 8): a busy row is ONE flat fill of its colour, no speckle texture over it
    parts.push(ctx.share(pillRect, paint))
  }
  // where a cell's drawing ends (a text: its width, a seed row: the last seed's capsule, anything else: its column's end)
  const drawnEnd = (c: RowCol): number => {
    const v = r.cells?.[c.key]
    if (v == null || v === '') return 0
    const o = typeof v === 'object' ? v : { t: v }
    if (o.kind) return o.kind.end()
    if (o.zones || o.pics || o.stages || o.marks) return c.x + c.w
    const dx = (o.dx ?? 0) + (r.ticket && c.key === 'name' ? ticketW(r.ticket.n) : 0)
    return Math.min(c.x + c.w, c.x + (c.key === 'name' ? off : 0) + dx + (o.icon || o.inset ? 18 : 0) + textWidth(o.t ?? '', PX, o.cls === 'nm' || (c.key === 'name' && !!r.bold)))
  }
  const realEnd = new Map<string, number>()
  for (const col of cols) {
    if (r.ticket && col.key === 'tick') continue // the number stands in front of the name (F14)
    if (r.ticket && col.key === 'name') {
      hot.push({ x: col.x + off, w: ticketW(r.ticket.n), label: `#${r.ticket.n}`, href: r.ticket.href, cls: 'ac' })
      parts.push(`<text x="${n1(col.x + off)}" y="${cy + 4.4}" class="lk">#${r.ticket.n}</text>`)
    }
    const v = r.cells?.[col.key]
    if (v == null || v === '') continue
    const o = typeof v === 'object' ? v : { t: v }
    const dx = (o.dx ?? 0) + (r.ticket && col.key === 'name' ? ticketW(r.ticket.n) : 0)
    // the name moves with its pill; every other column stays on the shared grid
    const x0 = col.x + (col.key === 'name' ? off : 0) + dx
    const room = col.w - (col.key === 'name' ? off : 0) - dx
    const bold = o.cls === 'nm' || (col.key === 'name' && !!r.bold)
    // #994 (defect 3): a selected row's name turns accent only when it has no class and colour of its own (a name drawn in its own bold colour keeps it)
    const cls = col.key === 'name' && r.sel && !o.color && o.cls !== 'nm' ? 'ac' : o.cls ?? (col.key === 'name' ? (r.bold ? 'nm' : r.calm ? 'mu' : 'tx') : col.cls ?? 'mu')
    if (o.zones) { // #711: an entry's pipeline: a zone per group, as wide as its measured share, filled as far as done
      const zw = zoneWidths(o.zones.w, room)
      let sx = x0
      zw.forEach((w, k) => {
        const f = Math.max(0, Math.min(1, o.zones!.fill[k] ?? 0))
        const now = k === o.zones!.at
        // the zone the entry is in always shows a lit sliver, so the pulse is seen from its first second
        const fw = now ? Math.max(Math.min(2, w), w * f) : w * f
        parts.push(`<rect x="${sx.toFixed(1)}" y="${cy - 4}" width="${w.toFixed(1)}" height="8" rx="1.5" fill="${K.veil}" fill-opacity=".3"/>`)
        if (fw > 0) parts.push(`<rect x="${sx.toFixed(1)}" y="${cy - 4}" width="${fw.toFixed(1)}" height="8" rx="1.5" fill="${o.zones!.color}"${now ? ' class="pu"' : ''}/>`)
        sx += w + ZONE_GAP
      })
      continue
    }
    if (o.pics) { // #773: priority (3 rising bars, `lit` of them bright), machine (its icon), slots (a small square each), then the row's text
      const { lit, host, slots } = o.pics
      const base = cy + 6
      let on = '', off2 = ''
      for (let b = 0; b < 3; b++) {
        const seg = `M${x0 + b * 4.5} ${base}v${-(4 + 4 * b)}h3v${4 + 4 * b}z`
        if (b < lit) on += seg; else off2 += seg
      }
      if (on) parts.push(`<path d="${on}" fill="${K.text}"/>`)
      if (off2) parts.push(`<path d="${off2}" fill="${K.muted}" fill-opacity=".35"/>`)
      let px = x0 + 18
      parts.push(ctx.icon(HOST_ICON[host] ?? 'monitor.octicons', px + 6.5, cy, 13, K.muted))
      px += 18
      const ns = Math.min(slots, 8)
      if (ns) { // a square a slot: 6 x 8 px outer (a 3 x 5 path, 3 px round stroke), pitch 8
        parts.push(`<path d="M${px + 1.5} ${cy - 2.5}h3v5h-3z${'m8 0h3v5h-3z'.repeat(ns - 1)}" fill="${K.muted}" stroke="${K.muted}" stroke-width="3" stroke-linejoin="round"/>`)
        px += ns * 8 + 2
      }
      if (o.pics.tail) {
        // #837 b3: the date may run on past the meta column into space where nothing is drawn on this row: its room ends 4 px before the next drawn cell (the num bar when
        // the row has one, else the act text); a row whose next cell is the bar keeps its old room (the bar's x less 4 is the column's end)
        const nx = cols.find(c => c.x > col.x && r.cells?.[c.key] != null && r.cells[c.key] !== '')
        // #837 b9b B2: the shared measure counts a digit 0.64 em, the date's are drawn 0.55 (measured): `fit` gets the room plus that surplus, and a cut one is trimmed to the measured width
        const dw = (s: string) => (s.match(/\d/g)?.length ?? 0) * 0.09 * PX
        const avail = Math.max(x0 + room, nx ? nx.x - 4 : 0) - px
        // #837 b10 S1: a date cell is never cut with «…»: the whole text, else its date (the part before the space), else nothing
        const t = [o.pics.tail, o.pics.tail.split(' ')[0]!].find(s => textWidth(s) - dw(s) <= avail) ?? ''
        if (t) parts.push(`<text x="${px.toFixed(1)}" y="${ph / 2 + 4.4}" class="${cls}">${besc(t)}</text>`)
      }
      continue
    }
    if (o.stages?.solid) { // #773: a run's entries: `n` segments (at most 12), `done` filled in the colour, the rest muted; two dashed strokes
      const { n, done, color } = o.stages
      const sw = (room - 1.5 * (n - 1)) / n
      const k = Math.max(0, Math.min(n, done))
      const dash = `stroke-width="8" stroke-dasharray="${+sw.toFixed(2)} 1.5"`
      const stroke = (from: number, cnt: number, paint: string) =>
        `<path d="M${n1(x0 + from * (sw + 1.5))} ${cy}h${n1(cnt * (sw + 1.5) - 1.5)}" ${dash} ${paint}/>`
      if (k > 0) parts.push(stroke(0, k, `stroke="${color}"`))
      if (k < n) parts.push(stroke(k, n - k, `stroke="${K.veil}" stroke-opacity=".3"`))
      continue
    }
    if (o.stages) { // a replay entry's time bar: thin segments, done in the class colour, the current one pulsing
      const { n, done, color } = o.stages
      const gap = 1.5
      const sw = (room - gap * (n - 1)) / n
      // #613: the segment is one <rect> in <defs>; a row places it with <use> under one <g> of its colour (done and current / not yet)
      const sid = ctx.seg(+sw.toFixed(1), cy - 4)
      // the done ones and the not yet ones are shapes the rows of one picture share (alike in n, done and x); the current one pulses
      const use = (k: number, extra = '') => `<use href="#${sid}" x="${n1(x0 + k * (sw + gap))}"${extra}/>`
      let lit = '', rest = ''
      for (let k = 0; k < n; k++) if (k < done) lit += use(k); else if (k > done) rest += use(k)
      if (lit) parts.push(ctx.share(lit, `fill="${color}"`))
      if (done >= 0 && done < n) parts.push(use(done, ` class="pu" fill="${color}"`))
      if (rest) parts.push(ctx.share(rest, `fill="${K.veil}" fill-opacity=".3"`))
      continue
    }
    if (o.kind) { o.kind.draw(ctx, cy, parts, hot); continue } // #1094 unit 3b: the add-on's seeds (addon.tsx seedKind)
    if (o.marks) {
      let mx = x0
      // a long word leaves every later mark its icon and up to MARK_KEEP px of its word, so the last one is not cut to nothing
      const MARK_KEEP = 96
      const later = (k: number) => o.marks!.slice(k + 1).reduce((s, m) => s + 14 + 17 + Math.min(textWidth(m.t), MARK_KEEP), 0)
      for (const [k, m] of o.marks.entries()) {
        const t = fit(m.t, x0 + room - mx - 17 - later(k))
        if (!t) break
        parts.push(ctx.icon(m.icon, mx + 6, cy, 13, m.color) + `<text x="${n1(mx + 17)}" y="${ph / 2 + 4.4}" class="${m.cls ?? 'mu'}">${besc(t)}</text>`)
        mx += 17 + textWidth(t) + 14
      }
      continue
    }
    // #837 b9a A4: an end-aligned text (a row's time) is cut only when it cannot stand between the end of the cell drawn before it + 12 screen px (x 0.9657) and the pill's right padding
    const room1 = col.align === 'end' ? Math.max(room, x0 + room - Math.max(0, ...cols.filter(c => c.x < col.x).map(c => Math.max(drawnEnd(c), realEnd.get(c.key) ?? 0))) - 12 / 0.9657) : room
    let ix = 0
    if (o.icon) {
      const ic = o.iconColor ?? ({ tx: K.text, mu: K.muted, fa: K.faint, er: K.failed, wt: K.wait, ok: K.active, dn: K.done } as Record<string, string>)[cls] ?? K.muted
      parts.push(ctx.icon(o.icon, x0 + 6, cy, 13, ic, o.spin ? 'class="sp"' : ''))
      ix = 18
    } else if (o.inset) ix = 18
    // #837 b9b B3: a plain text cell whose next drawn cell is an end-aligned text may run on past its column up to 12 screen px (the picture is fitted to its slot: W / (W - 33.4))
    // before that text
    const nc = o.press || o.href || col.align === 'end' ? undefined : cols.find(c => c.x > col.x && r.cells?.[c.key] != null && r.cells[c.key] !== '')
    const nv = nc?.align === 'end' ? r.cells![nc.key] : undefined
    const nw = nv == null ? 0 : typeof nv === 'string' ? textWidth(nv) : nv.t && !nv.zones && !nv.stages && !nv.pics && !nv.marks && !nv.kind && !nv.icon ? textWidth(nv.t) : 0
    const rx = nw ? Math.max(room, nc!.x + nc!.w - nw - 12 * W / (W - 33.4) - x0) : room
    // #837 b10 S2: a row TITLE (the name column's start-aligned text) is fitted by its DRAWN width (`drawnW`) and may run on up to 12 screen px before the next drawn cell
    const isTitle = col.key === 'name' && col.align !== 'end'
    // #837 b11 R1: the ticket column bounds the title of EVERY row of the card at its x, a row with no ticket included (its next drawn cell, the model / branch, stands further right: such a title ran on
    // 70 px past the others); a ticket number is drawn a cell later (PX_PER_COL) than the column, so the bound is the column's own x for all rows
    const isTick = (c: RowCol) => c.key === 'tick' && c.align !== 'end'
    const ns = isTitle ? Math.min(...cols.filter(c => c.x > col.x && (isTick(c) || (r.cells?.[c.key] != null && r.cells[c.key] !== ''))).map(c => {
      const w2 = r.cells![c.key], q = typeof w2 === 'object' && w2 != null ? w2 : { t: w2 ?? '' }
      if (q.kind) return Math.min(q.kind.start(), isTick(c) ? c.x : Infinity)
      if (isTick(c)) return c.x
      const own = q.zones || q.pics || q.stages || q.marks || !q.t
      return c.align === 'end' && !own ? c.x + c.w - textWidth(q.t!) : c.x + (q.dx ?? 0) + (q.href ? PX_PER_COL : 0)
    }), Infinity) : Infinity
    const tAvail = (ns < Infinity ? ns - 12 * W / (W - 33.4) - x0 : room) - ix
    // #972: every title is drawn in the picture (the host draws no word in a card row: the one pointer element over it is blank). A run's title (a press `run-...`) is drawn in the regular
    // weight at 14 px (class bt, #837 unit 13: the look of the old host Button); any other pressed title in its row's class. The fit measures at the SIZE it is drawn at: `bt` 14 px, bold 13 px, else 12 px
    // (#972 cause 3: a 14-px title measured at 12.2 px ran 15 % past its ticket number).
    const bt = col.key === 'name' && !!o.press && /^run-(pick|q|hist)-/.test(o.press.key)
    const dbold = bold && !bt
    const dpx = bt ? GHOST_PX * 14 / 12 : dbold ? GHOST_PX_BOLD : GHOST_PX
    const fitD = () => {
      const dwt = (z: string) => drawnW(z, dbold, dpx)
      if (tAvail <= 8) return ''
      let z = String(o.t ?? '')
      if (dwt(z) <= tAvail) return z
      while (z.length > 1 && dwt(z + '…') > tAvail) z = z.slice(0, -1)
      return z.trimEnd() + '…'
    }
    const t = isTitle ? fitD() : fit(o.t, col.align === 'end' ? room1 - ix : rx - ix, PX, bold)
    // where this text really ends (B3b may have run it past its column): the end-aligned text of a later column is fitted to what is left of it (A4)
    if (!o.press && !o.href && col.align !== 'end') realEnd.set(col.key, x0 + ix + textWidth(t, PX, bold))
    const ax = col.align === 'end' ? x0 + room : x0 + ix
    const nt = o.press ? (isTitle ? fitD() : fit(o.t, room - ix, PX, bold)) : t
    if (o.href || o.press) {
      if (nt) {
        // the pointer element's place (`Hot`): a link's number is drawn a cell right of its column (`tx`), a press's word where it is drawn (`drawn`)
        hot.push({ x: x0 + ix, w: isTitle ? Math.max(room - ix, drawnW(nt, dbold, dpx)) : room - ix, ...(o.href ? { tx: x0 + ix + PX_PER_COL } : { drawn: { x: x0 + ix, w: drawnW(nt, dbold, dpx) } }), label: nt, href: o.href, press: o.press, cls, bold, ...(isTitle && o.press && !o.href ? { title: true } : {}) })
        if (o.href) parts.push(`<text x="${n1(x0 + ix + PX_PER_COL)}" y="${ph / 2 + 4.4}" class="lk">${besc(nt)}</text>`)
        else {
          // #837 unit 13: a run's running / waiting title as the baseline's Button: regular weight 14 px, the host's dim unless it is the shown run (never bold)
          const tcls = bt ? 'bt' : cls
          const fill = bt ? (o.press!.dim ? K.titleDim : K.titleText) : o.press!.dim && !(r.sel && col.key === 'name') ? K.muted : ''
          parts.push(`<text x="${n1(x0 + ix)}" y="${ph / 2 + 4.4}" class="${tcls}"${fill ? ` style="fill:${fill}"` : ''}>${besc(nt)}</text>`)
        }
      }
      continue
    }
    parts.push(`<text x="${n1(ax)}" y="${ph / 2 + 4.4}"${col.align === 'end' ? ' text-anchor="end"' : ''} class="${cls}${col.num ? ' nu' : ''}"${o.color ? ` style="fill:${o.color}"` : ''}>${besc(t)}</text>`)
  }
  return { body: faded(parts), h, hot, pill }
}

/** One row = one picture (a pressed row: its native presses lie over it, `hot`). */
export function rowSvg(r: RowSpec, i: number, cols: RowCol[], W: number): { svg: string; h: number; hot: Hot[]; pill?: Pill } {
  const ctx = soloCtx()
  const b = rowBody(r, i, cols, W, ctx)
  return { svg: svgDoc(W, b.h, b.body, ctx.defs.join('')), h: b.h, hot: b.hot, pill: b.pill }
}
/** #771: a run of rows with no native press as ONE picture, the rows one under the other: the style sheet, the dither
 *  tile of one colour and an icon drawn twice or more are written once. The drawn words are each row's own. */
export function rowsSvg(rows: { r: RowSpec; i: number; cols?: RowCol[] }[], cols: RowCol[], W: number, fit = 0, stretch = false): { svg: string; h: number; tops: number[]; hs: number[]; hot: Hot[][]; pills: (Pill | undefined)[] } {
  const { ctx, finish } = sharedCtx()
  let y = 0
  const out: string[] = []
  const tops: number[] = []
  const hs: number[] = []
  const hot: Hot[][] = []
  const pills: (Pill | undefined)[] = []
  rows.forEach(({ r, i, cols: own }, n) => {
    ctx.sfx = String(n)
    const b = rowBody(r, i, own ?? cols, W, ctx)
    // #837: a row with no drawing (a gap) is still a row of its own height: a zero-size `<g>` at its top marks it, so a picture's rows read back from its markup
    out.push(y ? `<g transform="translate(0 ${y})"${b.body ? `>${b.body}</g>` : '/>'}` : b.body)
    tops.push(y)
    hs.push(b.h)
    hot.push(b.hot)
    pills.push(b.pill)
    y += b.h
  })
  const done = finish(out.join(''))
  // #837 b9a B4: `fit` = the scale the host draws this picture at (its slot's width over W): the picture's height is padded (blank, at the bottom) so that
  // on the screen it is a WHOLE number of 19-px lines. Else the host centres it in the lines it reserves and its rows sit lower or higher by half the slack, as the rows below it grow (opening a run's section: 2 px).
  const H = fit ? Math.ceil(Math.ceil((y * fit) / CELL_PX - 1e-6) * CELL_PX / fit * 100) / 100 : y
  // #837 b18: `stretch` = the picture's root takes the host's box as it comes (`preserveAspectRatio="none"`): the host shrinks a card's picture to its slot (about 0.99), and the rows' overlay Boxes
  // are literal px; with the picture's y NOT scaled with its x, its rows keep their own pitch (PITCH px) and the overlay stands on them down the whole card.
  const svg = svgDoc(W, H, done.body, done.defs)
  return { svg: stretch ? svg.replace('<svg ', `<svg preserveAspectRatio='none' `) : svg, h: y, tops, hs, hot, pills }
}

// ---- a MACHINE card: one picture (no press inside it; #995: no reserve form under it) --------------------
// #1018: the merge's share of the CPU / RAM bars is a fourth part; its legend word is `merge` (RNAME). #119: its colour is not the Merge card's
// «running merge» token K.active (a ticket state) but the merge colour of BAR_COL below, which the CPU strip draws too.
// #97 u2: each source of the load is its own part with its own legend word: `app` the Claude desktop app, `tests` the pytest processes, `you` the
// owner's other programs (the legend word «other»), `unmeasured` the memory or CPU no process holds.
// #114: `app` #9ad147 (lime, hue 84) and `tests` #2cbaa2 (teal, hue 170): each at least 40 degrees of hue from K.wait, K.done,
// K.active, K.failed and the work / agents colours, and from each other (the row-5 test in test/machine-strip.test.tsx); the CPU strip draws the same colours.
export const RNAME = { work: WORK_NAME, agents: 'agents', app: 'Claude app', tests: 'tests', you: 'other', unmeasured: 'unmeasured', merge: 'merge' } as const
export type Role = keyof typeof ROLE
export type BarRole = Role | 'app' | 'tests' | 'unmeasured' | 'merge'
// #119: `merge` is the bars' seventh part and the strip draws it as a seventh part too (when a bucket holds a merge share). #d65cbe is hue 312,
// about 50 degrees from K.done and from K.failed: the widest gap between the four state colours (the row-4 test in test/machine-strip.test.tsx).
const BAR_COL: Record<BarRole, string> = { ...ROLE, app: '#9ad147', tests: '#2cbaa2', unmeasured: K.faint, merge: '#d65cbe' }
const partOp = (role: BarRole) => role === 'you' || role === 'unmeasured' ? 0.55 : 0.8
export type SlotMark = 'run' | 'agents' | 'you' | 'free'
// #114: one share per source, in the bars' order: the CPU strip stacks the same six sources the bars draw; #119: `merge` (absent = 0) is the seventh
export const SOURCES = ['work', 'agents', 'app', 'tests', 'you', 'unmeasured'] as const
export type Split6 = Record<(typeof SOURCES)[number], number> & { merge?: number }
export const HIST_DRAW = 40 // buckets drawn in the 10-min CPU strip (one per 15 s)
// `m` null = the host out of reach: the same card, empty bars and `—`, its slots as they were at the last contact.
// `slotCap` = the slots' caption (in use / available, the owner's reservation); `hist` = up to HIST_DRAW buckets, the
// newest at the right edge (a fresh pane fills from the right: the strip is always 10 minutes wide).
const BAR_PITCH = 26, SLOT_PITCH = 30 // #837 pass 2: a machine card's bar row and slot row (baseline), not PITCH
export function machineSvg(title: string, sub: string, iconName: string, m: Machine | null, slots: SlotMark[], slotCap: string,
  hist: Split6[], W: number, state?: { t: string; cls: string }) {
  const L = 38, BX = 130, BW = W - 96 - BX - 14, VX = W - 12
  const out: string[] = []
  let y = 30 + PITCH // the title line (30) and the column heads (one row)
  out.push(`<text x="${L}" y="46" class="mu">ресурс</text><text x="${BX}" y="46" class="mu">занято</text><text x="${VX}" y="46" text-anchor="end" class="mu">сейчас</text>`, icon(iconName, RAIL / 2, 12, 16, K.text), `<text x="${P0}" y="17" class="ti">${besc(title)}</text><text x="${26 + textWidth(title, 14, true) + 12}" y="17" class="su">${besc(sub)}</text>`)
  if (state) out.push(`<text x="${VX}" y="17" text-anchor="end" class="${state.cls}">${besc(state.t)}</text>`)
  const label = (t: string, yy: number) => `<text x="${L}" y="${yy}" class="lb">${besc(t)}</text>`
  const stack = (name: string, parts: [BarRole, number][], total: number, value: string, fmt: (v: number) => string) => {
    const h = 20, r = h / 2
    out.push(label(name, y + 14.4), `<clipPath id="cl${name}"><rect x="${BX}" y="${y}" width="${BW}" height="${h}" rx="${r}"/></clipPath>`)
    let x = BX
    let segs = `<rect x="${BX}" y="${y}" width="${BW}" height="${h}" fill="${K.veil}" fill-opacity=".14"/>`
    for (const [role, v] of parts) {
      const w = Math.min(BW - (x - BX), (Math.min(total, v) / total) * BW) // #97 u2: the parts together never pass the bar's end
      if (w < 0.5) continue
      segs += `<rect x="${x.toFixed(1)}" y="${y}" width="${w.toFixed(1)}" height="${h}" fill="${BAR_COL[role]}" fill-opacity="${partOp(role)}"/>`
      x += w
    }
    out.push(`<g clip-path="url(#cl${name})">${segs}</g>`, `<text x="${VX}" y="${y + 14.4}" text-anchor="end" class="vl nu">${besc(value)}</text>`)
    // F2 (#822): every share is labelled under the bar, none inside; a legend that does not fit wraps whole onto the next row
    const shown = parts.filter(([, v]) => (Math.min(total, v) / total) * BW >= 0.5)
    y += BAR_PITCH // baseline 50ea5412: the bar's 20 px + 6
    if (shown.length) {
      let lx = BX
      for (const [role, v] of shown) {
        const words = `${RNAME[role]} ${fmt(v)}`
        const wd = textWidth(words) + 30
        if (lx > BX && lx + wd - 17 > VX) { lx = BX; y += PITCH }
        out.push(`<circle cx="${lx + 4}" cy="${y + PITCH / 2}" r="4" fill="${BAR_COL[role]}"/><text x="${lx + 13}" y="${y + PITCH / 2 + 4}" class="mu nu">${besc(words)}</text>`)
        lx += wd
      }
      y += PITCH // the legend is a row of its own
    }
  }
  const pct = (v: number) => `${Math.round(v)} %`
  if (m) {
    // #1018: the merge is the last part (0 / absent = no part: a part under 0.5 px is skipped, the legend with it)
    // #97 u2: a key an older collector does not send reads as 0
    const parts = (s: Split, merge: number): [BarRole, number][] => [['work', s[WORK_KEY] ?? 0], ['agents', s.agents ?? 0], ['app', s.app ?? 0],
      ['tests', s.tests ?? 0], ['you', s.you ?? 0], ['unmeasured', s.unmeasured ?? 0], ['merge', merge]]
    stack('CPU', parts(m.cpu, m.merge?.cpu ?? 0), 100, pct(m.cpu_total), pct)
    stack('RAM', parts(m.ram, (m.merge?.ram_mb ?? 0) / 1024), m.ram_total, `${Math.round(m.ram_total - m.ram_free)} / ${Math.round(m.ram_total)} GB`, v => `${v.toFixed(1)} GB`)
  } else {
    stack('CPU', [], 100, UNKNOWN_DASH, pct)
    stack('RAM', [], 1, UNKNOWN_DASH, pct)
  }
  if (slots.length) {
    out.push(label(SLOTS_LABEL, y + 14.4))
    slots.forEach((s, i) => {
      const sx = BX + i * 24
      out.push(s === 'free'
        ? `<rect x="${sx + 0.75}" y="${y + 1.75}" width="16.5" height="16.5" rx="${RADIUS.s}" fill="none" stroke="${K.faint}" stroke-width="1.5"/>`
        : `<rect x="${sx}" y="${y + 1}" width="18" height="18" rx="${RADIUS.s}" fill="${s === 'run' ? ROLE.work : ROLE[s === 'agents' ? 'agents' : 'you']}"${s === 'you' ? ' fill-opacity=".55"' : ''}/>`)
    })
    out.push(`<text x="${VX}" y="${y + 14.4}" text-anchor="end" class="vl nu">${besc(slotCap)}</text>`)
    y += SLOT_PITCH // baseline 50ea5412
  }
  const H = 44
  out.push(label('CPU, 10 мин', y + 14.4))
  out.push(`<rect x="${BX}" y="${y}" width="${BW}" height="${H}" rx="6" fill="${K.veil}" fill-opacity=".08"/>`)
  if (hist.length > 1) {
    const step = BW / (HIST_DRAW - 1)
    const X = (i: number) => BX + BW - (hist.length - 1 - i) * step
    const Y = (v: number) => y + H - (Math.min(100, v) / 100) * H
    // #114: the strip stacks the bars' six sources in the bars' order, each in its bar's colour and opacity (a part is the
    // share from the one below it up to its own top; the parts sum to the machine's CPU). #119: the merge is drawn last, as a
    // seventh part, only when a bucket holds a merge share (no merge in the window: six parts, as before)
    const withMerge = hist.some(s => (s.merge ?? 0) > 0)
    const stacked: (keyof Split6)[] = withMerge ? [...SOURCES, 'merge'] : [...SOURCES]
    const below = (s: Split6, k: number) => stacked.slice(0, k).reduce((a, n) => a + (s[n] ?? 0), 0)
    stacked.forEach((src, k) => {
      const top = hist.map((s, i) => `${X(i).toFixed(1)},${Y(below(s, k + 1)).toFixed(2)}`).join(' ')
      const bot = hist.map((s, i) => `${X(i).toFixed(1)},${Y(below(s, k)).toFixed(2)}`).reverse().join(' ')
      out.push(`<polygon points="${top} ${bot}" fill="${BAR_COL[src]}" fill-opacity="${partOp(src)}"/>`)
    })
  } else if (m) out.push(`<text x="${BX + 10}" y="${y + 26}" class="mu">собираю…</text>`)
  y += H + 8
  const style = `<style>text{font:400 ${PX}px ${FONT};fill:${K.muted}}.ti{font:600 14px ${FONT};fill:${K.text}}.su{fill:${K.muted}}
.lb{font:600 12px ${FONT};fill:${K.muted}}.mu{fill:${K.muted}}.vl{font-weight:600;font-size:13px;fill:${K.text}}.nu{font-variant-numeric:tabular-nums}
.sg{font-weight:600;fill:${K.white};font-variant-numeric:tabular-nums}.dk{fill:${K.ink}}.ok{fill:${K.active}}.wt{fill:${K.wait}}.er{fill:${K.failed}}</style>`
  return sq(`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${y}" viewBox="0 0 ${W} ${y}">${style}${out.join('')}</svg>`)
}

// What an agent does -> the icon in front of its words (one per action family).
export const ACT_ICON: [RegExp, string][] = [
  [/тикет/, 'issue-opened'], [/тесты/, 'flask.octicons'], [/причину/, 'search.octicons'], [/ищет в коде/, 'search.octicons'], [/код/, 'file-code.octicons'],
  [/доки/, 'book.octicons'], [/коммит/, 'git-commit.octicons'], [/мердж|merge/, 'git-merge'], [/пушит|ветку/, 'git-pull-request'],
  [/diff|git/, 'git-commit.octicons'], [/github/i, 'hourglass.octicons'], ...PROJECT_ACT_ICON, [/интернет|браузер|wiki/, 'globe.octicons'], [/экран/, 'eye.octicons'],
  [/сабагент|воркер|worker/, 'message.octicons'], [/логи|скрипт/, 'terminal.octicons'], [/нагрузку/, 'cpu.octicons'], [/отчёт/, 'message.octicons'], [/думает/, 'hourglass.octicons'],
]
export const actIcon = (w: string) => ACT_ICON.find(([re]) => re.test(w))?.[1]
// #15: a subagent's text is `<role>: <title>` (tools/hooks/agent_role_prefix.py). The prefix is a role only when it is the subagent's own type
// (case-insensitive) or a known role; then it is drawn as an icon in the name cell and cut from the title. A role with no icon of its own
// (any other type) draws `bot`. Any other prefix (`Fix: parser`, `TODO: cache`) keeps its text and draws no icon.
const ROLE_ICON = new Map<string, string>([['worker', 'wrench'], ['worker-strong', 'hammer'], ['researcher-find', 'list'], ['researcher-how', 'book-open'], ['reviewer-standards', 'search.octicons'], ['reviewer-spec', 'eye.octicons']])
export const roleOf = (text: string, type: string): { role: string; icon: string; title: string } | null => {
  const m = /^([a-z][a-z0-9-]*): ([\s\S]*)$/i.exec(text)
  if (!m) return null
  const role = m[1]!.toLowerCase()
  if (role !== type.toLowerCase() && !ROLE_ICON.has(role)) return null
  return { role, icon: ROLE_ICON.get(role) ?? 'bot', title: m[2]! }
}
// Times in one Russian format (owner, principle 7): the collector's "7 min", "1.9 min", "1 h 12 min", "86 s" are read
// back to seconds and written as `dur` writes them (72 min stays «72 мин»); a string with no time in it stays as it is.
// #699: a queue card's whole-queue end after its counts; `partial` = a job without an estimate, so not earlier than this
export const queueEnd = (h?: string, until?: string | null, partial?: boolean) =>
  h ? ` · ${partial ? 'вся не меньше' : 'вся'} ~${ru(h)}${until ? `, до ${until}` : ''}` : ''
export const ru = (v: string | null | undefined) => {
  const t = String(v ?? '')
  return /\d\s*(s|min|h|d)\b/.test(t) ? dur(ageS(t)) : t
}
// Seconds -> the same format: seconds under 90, then minutes, then hours and minutes.
export const dur = (s: number) => {
  const v = Math.max(0, Math.round(s))
  if (v < 90) return `${v} с`
  const m = Math.round(v / 60)
  return m < 90 ? `${m} мин` : m % 60 === 0 ? `${m / 60} ч` : `${Math.floor(m / 60)} ч ${m % 60} мин`
}
// The Russian noun form for a count: 1 слот, 2 слота, 5 слотов (11-14 take the third).
export const ruForm = (k: number, one: string, few: string, many: string) =>
  k % 10 === 1 && k % 100 !== 11 ? one : k % 10 >= 2 && k % 10 <= 4 && (k % 100 < 12 || k % 100 > 14) ? few : many
export const slotsRu = (n: number | null | undefined) => `${n ?? '?'} ${ruForm(n ?? 0, 'слот', 'слота', 'слотов')}`
// #711: an entry's bar: the px of each group's zone on a cell `room` wide. A zone is as wide as its group's measured
// share of run time (collect.py stage_shares), never under ZONE_MIN px (finish is 0.1 % of a run: still seen).
export const ZONE_GAP = 1.5
export const ZONE_MIN = 3
export function zoneWidths(w: number[], room: number): number[] {
  const free = room - ZONE_GAP * (w.length - 1)
  const sum = w.reduce((s, x) => s + x, 0) || 1
  const px = w.map(x => Math.max(ZONE_MIN, (x / sum) * free))
  const k = free / px.reduce((s, x) => s + x, 0)
  return px.map(x => x * k)
}
export const STATE_DOT: Record<Issue['state'] | 'work', [string, string]> = {
  work: ['●', K.active], owner: ['●', K.owner], blocked: ['●', K.hold], ready: ['●', K.accent],
  triage: ['○', K.muted], open: ['○', K.text], closed: ['●', K.rule],
}
// #1079: a state is drawn by the name GitHub and the Pocock skills write it (`open`, `closed`, `not planned`; a label's own name for a
// label state); a state the orchestration layer adds is `held` (ORCH holds the ticket) and not a Russian word.
export const STATE_WORD: Record<Issue['state'] | 'work' | 'dropped', string> = {
  work: 'held', owner: 'ready-for-human', blocked: 'blocked', ready: 'ready-for-agent',
  triage: 'needs-triage', open: 'open', closed: 'closed', dropped: 'not planned',
}
// The owner, 10.10 13:0x («тысячу раз уже обсуждали»): no state is drawn with a text character. On the desktop a
// ticket's state is an icon of the pane's set in the state's colour, the shapes GitHub draws (an open issue's ring
// with a dot, a closed one's tick, a draft's dashed ring, «not planned»'s crossed ring); the terminal, which has no
// pictures, keeps STATE_DOT.
export type TicketState = Issue['state'] | 'work' | 'dropped'
export const STATE_ICON: Record<TicketState, [string, string]> = {
  work: ['issue-opened', K.active], owner: ['person', K.owner], blocked: ['hourglass.octicons', K.hold], ready: ['bot.hubot.octicons', K.accent],
  triage: ['draft', K.muted], open: ['circle.octicons', K.text], closed: ['issue-closed', K.done], dropped: ['skip', K.muted],
}
// #1075: a ticket labelled `parked` is drawn by the pause icon (the Tickets tab's mark column, the Progress tab's `parked` line); its title is GitHub's own.
export const PARKED_ICON: [string, string] = ['pause.octicons', K.wait]
export const iconState = (i: Issue, held: Set<number>): TicketState => (!i.open && i.reason === 'NOT_PLANNED' ? 'dropped' : stateOf(i, held))
/** A «По тикетам» row's ticket: the Tickets entry when it has one, else what the collector sent in the row itself
 *  (a closed ticket under no open goal is not in Tickets); the same `iconState` decides the icon of both. */
export const rowIssue = (t: Tickets | undefined, x: { number: number; title?: string; open?: boolean; reason?: string | null; state?: Issue['state'] }): Issue | undefined =>
  t?.issues[String(x.number)] ?? (x.title == null ? undefined : { n: x.number, title: x.title, open: !!x.open, reason: x.reason ?? null, labels: [], parent: null, blockers: [], kids: [], state: x.state ?? 'closed' })
/** The label of a native press that lies over an icon of the picture: two no-break spaces, nothing to read. */
export const BLANK = '\u00a0\u00a0'


export const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
export const num = (v: number | null | undefined) => (v == null ? '-' : Math.round(v).toLocaleString('en-US'))
export const ddmm = (v: string) => { const m = /(\d{4})-(\d{2})-(\d{2})/.exec(String(v)); return m ? `${m[3]}.${m[2]}` : String(v) }
export const words = (slug: string) => slug.split('_').filter(p => !/^z?\d+$/.test(p)).map(p => p[0].toUpperCase() + p.slice(1)).join(' ')
// "86 s", "1.9 min", "4 h 46 min" -> seconds (the collector's ages), for ordering only.
export const ageS = (v: string | undefined) => {
  let n = 0
  for (const m of String(v ?? '').matchAll(/(\d+(?:\.\d+)?)\s*(s|min|h|d)\b/g)) n += Number(m[1]) * ({ s: 1, min: 60, h: 3600, d: 86400 } as const)[m[2] as 's']
  return n
}
export const SUBS_DONE = 2 // finished subagents drawn as strips under their session
export const slots = (n: number | null | undefined) => `${n ?? '?'} slot${n === 1 ? '' : 's'}`
// #837 b9b B5: the one «unknown» mark: the machine card's value when the host is out of reach, an Agents row's empty model / context / duration
export const UNKNOWN_DASH = '—'
export const kctx = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n))
// #837 b9b B7: money of 1000 and more is grouped with the counts' own separator (`thousands`), cents rules as before
export const usd = (v: number) => `$${v >= 100 ? thousands(v) : v.toFixed(2)}`
// The Cost tab (#683 r5 step 1b): a token count as a human reads it, and a cost as a share of the weekly limit.
export const tokH = (n: number) => (n >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n))
// #1094: 1 % of the weekly limit at API prices is the payload's `total.limit_usd_per_pct` (the owner's plan); null = no plan = no figure
export const limPct = (v: number, perPct: number | null | undefined) => { if (perPct == null) return ''; const p = v / perPct; return p >= 10 ? `${Math.round(p)} %` : p >= 0.1 ? `${p.toFixed(1)} %` : p > 0 ? '<0.1 %' : '0 %' } // #837 b9b B8: number, space, sign
// A Russian plural by the number (1 сессия, 2 сессии, 5 сессий), thousands split by a plain space, a per-day rate.
export const plural = (n: number, a: string, b: string, c: string) => { const m100 = n % 100, m10 = n % 10; return m100 >= 11 && m100 <= 14 ? c : m10 === 1 ? a : m10 >= 2 && m10 <= 4 ? b : c }
export const thousands = (n: number) => String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
export const rate = (v: number) => (v >= 10 ? thousands(v) : v >= 0.05 ? v.toFixed(1) : v > 0 ? '<0.1' : '0')
// #683 r5 (E) (owner 10.10 12:1x: the pane is UI for a human, «хватит сюда нести тупо вериблы из кода»): the Tickets,
// Cost and Progress tabs draw no code word. A model id is shown as the model's name: `claude-opus-5-5` = «Opus 5.5»,
// `claude-haiku-4-5-20251001` = «Haiku 4.5» (no `claude-`, no trailing 8-digit date, the family capitalised, the
// version digits joined with a dot); an id of another shape is shown as it is.
export const MODEL_WORD: Record<string, string> = { '<synthetic>': 'служебные сообщения' }
export const modelName = (id: unknown) => {
  if (MODEL_WORD[String(id)]) return MODEL_WORD[String(id)]!
  const m = /^claude-([a-z]+)((?:-\d{1,3})+)(?:-\d{8})?$/.exec(String(id))
  return m ? `${m[1]![0]!.toUpperCase()}${m[1]!.slice(1)} ${m[2]!.slice(1).split('-').join('.')}` : String(id)
}
// The footer of a tab: the collector's «23:50:38» without its seconds.
export const fresh = (at: unknown) => `обновлено ${String(at ?? '-').replace(/^(\d\d:\d\d):\d\d$/, '$1')}`
// A triage label is drawn by its GitHub name (#1079), the same name the Tickets legend gives its state; the hover keys keep the label's own name.
export const LABEL_WORD: Record<string, string> = {
  'needs-triage': STATE_WORD.triage, 'needs-info': 'needs-info', 'ready-for-agent': STATE_WORD.ready,
  'ready-for-human': STATE_WORD.owner, wontfix: 'wontfix', 'no label': 'no label',
  // #927 / #841: the Progress card's lines that are no label (#1079: their names as the layer writes them)
  autopilot: 'autopilot', folder: 'folder', blocked: STATE_WORD.blocked, 'parked-on-run': 'parked-on-run', parked: 'parked',
}
const TRIAGE_LABELS = ['needs-triage', 'needs-info', 'ready-for-agent', 'ready-for-human', 'wontfix']
/** every line the Progress card can ever draw, in its order (#983: the worst case of the tab holds a ticket on each) */
export const PROGRESS_LINES = [...TRIAGE_LABELS, 'parked', 'autopilot', 'folder', 'blocked', 'parked-on-run', 'no label']
/** #995: the lines of the Progress tab's second card «Остальное» (no ticket's state: a folder); every other line is in «По тикетам» */
export const PROGRESS_OTHER = ['folder']
// #841's rule: each open ticket is counted ONCE, on the first line that fits: parked (#1075, first: a parked ticket keeps its role label, so it is counted here whatever its other labels); its triage label; label autopilot; a folder
// (label wayfinder:map / core-epic, or an open sub-ticket); an open blocker on it or on an ancestor; parked on a run; else none.
// Lines with no ticket are left out, except needs-triage and no label (#1079: the names as written).
export const progressLines = (issues: Record<string, Pick<Issue, 'n' | 'open' | 'labels' | 'parent' | 'blockers' | 'kids'> & { parked?: boolean; parked_on_run?: boolean }>): [string, number][] => {
  const ids = PROGRESS_LINES
  const count = new Map(ids.map(id => [id, 0]))
  const blockedUp = (i: { n: number; parent: number | null; blockers: number[] }) => {
    const seen = new Set<number>()
    for (let x: typeof i | undefined = i; x && !seen.has(x.n); x = x.parent == null ? undefined : issues[String(x.parent)]) {
      if (x.blockers.length) return true
      seen.add(x.n)
    }
    return false
  }
  for (const i of Object.values(issues)) {
    if (!i.open) continue
    // #1075: parked is checked first: a parked ticket keeps its role label, and it is counted on the parked line whatever other label it has
    const line = i.parked ? 'parked'
      : TRIAGE_LABELS.find(l => i.labels.includes(l))
      ?? (i.labels.includes('autopilot') ? 'autopilot'
        : i.labels.includes('wayfinder:map') || i.labels.includes('core-epic') || i.kids.some(k => issues[String(k)]?.open) ? 'folder'
          : blockedUp(i) ? 'blocked' : i.parked_on_run ? 'parked-on-run' : 'no label')
    count.set(line, count.get(line)! + 1)
  }
  return ids.map((id): [string, number] => [id, count.get(id)!]).filter(([id, n]) => n > 0 || id === 'needs-triage' || id === 'no label')
}
// #6: the Progress tab's «Пайплайн» lines. The five stations of a line's strip (an add-on's station after `work` is not in this part).
// #154: the fifth station is `accept` (a landed ticket still open, after `merge`); a stage not in this table draws every station `later`.
export const PIPE_STATIONS = ['ready', 'orch', 'work', 'merge', 'accept'] as const
/** a line's stations: `passed` before its stage, `current` at it, `later` after it (a stage not in the table has no current station) */
export const pipeStations = (stage: PipelineRow['stage']): ('passed' | 'current' | 'later')[] => {
  const cur = PIPE_STATIONS.indexOf(stage)
  return PIPE_STATIONS.map((_s, k) => (k < cur ? 'passed' : k === cur ? 'current' : 'later'))
}
/** #171: a waiting row's reason cut to `n` characters, the last one an ellipsis */
const cutReason = (s: string, n = 40) => {
  const a = [...s]
  return a.length > n ? `${a.slice(0, n - 1).join('')}…` : s
}
/** a line's stage word, its icon and its colour; a failed line is `упало` whatever its stage; a worker-strong counts as a worker, marked with its count;
 *  #171: a waiting row (`wait`) reads `ждёт: <reason>` and a row sent back by the brain (`returned`) reads `возвращён`, both before the stage's word */
export const pipeWord = (r: PipelineRow): { t: string; icon: string; color: string } => {
  if (r.failed) return { t: 'упало', icon: 'failed.octicons', color: K.failed }
  if (r.wait) return { t: `ждёт: ${cutReason(r.wait.reason ?? '')}`, icon: 'waiting.octicons', color: K.wait }
  if (r.returned) return { t: 'возвращён', icon: 'pause.octicons', color: K.wait }
  if (r.stage === 'accept') return { t: 'приёмка', icon: 'done.octicons', color: K.active }
  if (r.stage === 'ready') return { t: 'ждёт ORCH', icon: 'waiting.octicons', color: K.wait }
  if (r.stage === 'orch') return { t: 'ORCH режет', icon: 'bot.hubot.octicons', color: K.active }
  if (r.stage === 'work') return { t: `${r.workers} worker${r.strong ? ` (${r.strong} strong)` : ''} · ${r.researchers} researcher`, icon: 'wrench', color: K.active }
  if (r.merge_percent != null) return { t: `merge: suite ${r.merge_percent} %`, icon: 'git-merge', color: K.active }
  return { t: r.merge_position != null ? `ждёт merge, ${r.merge_position}-й` : 'ждёт merge', icon: 'waiting.octicons', color: K.wait }
}
/** a line's strip as one cell: five stations joined by a line across x0..x0+w; the group says the line's stage (`data-stage`), each station its state (`data-st`) */
export const pipeStripKind = (stage: PipelineRow['stage'], color: string, failed: boolean, x0: number, w: number): CellKind => {
  const st = pipeStations(stage)
  const pad = 6, pitch = (w - 2 * pad) / (st.length - 1)
  const x = (k: number) => x0 + pad + k * pitch
  return {
    start: () => x0,
    end: () => x0 + w,
    draw: (_ctx, cy, parts) => {
      // a class keeps each station's own markup: the picture's dedupe (`dedupe`) would turn alike stations of two lines into `<use>` and drop `data-st`
      const marks = st.map((s, k) => {
        if (s === 'current') return `<circle class="pst" data-st="${s}" cx="${n1(x(k))}" cy="${n1(cy)}" r="4.2" fill="${failed ? K.failed : color}"/>`
        if (s === 'passed') return `<circle class="pst" data-st="${s}" cx="${n1(x(k))}" cy="${n1(cy)}" r="3.2" fill="${K.muted}"/>`
        return `<circle class="pst" data-st="${s}" cx="${n1(x(k))}" cy="${n1(cy)}" r="3.2" fill="${K.tile}" stroke="${K.muted}" stroke-width="1.2"/>`
      })
      parts.push(`<g data-stage="${stage}"><rect x="${n1(x(0))}" y="${n1(cy - 0.75)}" width="${n1(x(st.length - 1) - x(0))}" height="1.5" fill="${K.rule}"/>${marks.join('')}</g>`)
    },
  }
}
/** a line's time since `iso` in the human format of `dur`; `—` when the collector sent no stamp */
export const pipeSince = (iso: string | null, now: number) => {
  const ms = iso ? Date.parse(iso) : NaN
  return Number.isFinite(ms) ? dur(Math.max(0, (now - ms) / 1000)) : UNKNOWN_DASH
}
/** #171: the time left from `now` to a waiting row's date `iso`, in the human form: «через 6 д 10 ч», «через 5 ч 20 мин», «через 30 мин» (whole minutes, at least one);
 *  a date at or before `now` is «срок вышел»; a date that does not read is a dash */
export const pipeLeft = (iso: string | null | undefined, now: number): string => {
  const ms = iso ? Date.parse(iso) : NaN
  if (!Number.isFinite(ms)) return UNKNOWN_DASH
  if (ms <= now) return 'срок вышел'
  const min = Math.max(1, Math.floor((ms - now) / 60_000))
  const d = Math.floor(min / 1440), h = Math.floor((min % 1440) / 60), m = min % 60
  if (d) return `через ${d} д${h ? ` ${h} ч` : ''}`
  if (h) return `через ${h} ч${m ? ` ${m} мин` : ''}`
  return `через ${m} мин`
}
// #837 b9b B10: EVERY line of the Progress card carries an icon of the pane's set, each line its own. The four lines that are a ticket state keep the Tickets legend's icon (STATE_ICON); the others take the closest the set has:
// needs-info = message (a question waits), autopilot = the legend's held icon (issue-opened, active), folder = file-directory (#995 send-back 1, defect 3: the owner's
// own word «папки» of his point 8 (#1079: the name is folder), said with what it counts: a map, an epic or a ticket with open sub-tickets; the book of before named nothing he knows), parked-on-run = waiting (the clock),
// no label = the legend's open ring (an open ticket nobody sorted); wontfix (shown only with a count) keeps the legend's crossed ring.
export const LABEL_ICON: Record<string, [string, string]> = {
  'needs-triage': STATE_ICON.triage, 'ready-for-agent': STATE_ICON.ready, 'ready-for-human': STATE_ICON.owner, blocked: STATE_ICON.blocked, wontfix: STATE_ICON.dropped,
  'needs-info': ['message.octicons', K.wait], parked: PARKED_ICON, autopilot: STATE_ICON.work, folder: ['file-directory.octicons', K.muted], 'parked-on-run': ['waiting.octicons', K.wait], 'no label': STATE_ICON.open,
}
// ours / author in percent: the runs card and the list tab colour it the same way.
export const pctCol = (p: number) => (p >= 100 ? K.active : p >= 80 ? K.text : K.nfailed)


// ---------------------------------------------------------------- tickets helpers

export function heldIssues(f: Fast | null): Set<number> {
  const s = new Set<number>()
  for (const a of f?.agents ?? []) {
    if (a.busy && a.issue) s.add(a.issue)
    for (const n of a.held) s.add(n)
    for (const sub of a.subagents) if (sub.issue) s.add(sub.issue)
  }
  for (const j of f?.runs?.running ?? []) if (j.issue) s.add(j.issue)
  return s
}

export const stateOf = (i: Issue, held: Set<number>): Issue['state'] | 'work' => (i.open && held.has(i.n) ? 'work' : i.state)

export function subtree(t: Tickets, n: number): Issue[] {
  const out: Issue[] = []
  const walk = (k: number) => {
    const i = t.issues[String(k)]
    if (!i) return
    for (const c of i.kids) { const ci = t.issues[String(c)]; if (ci) { out.push(ci); walk(c) } }
  }
  walk(n)
  return out
}

// A queue row set's entries (queueRows in register.tsx; the add-on's runs card makes them too).
export type QRun = { icon: string; alt: string; cells: Record<string, CellV>; frac?: number; knob?: string; state?: 'active' | 'failed'; kids?: RowSpec[]; stale?: boolean; sel?: boolean }
export type QDone = { state: 'done' | 'failed' | 'idle'; sel?: boolean; alt: string; cells: Record<string, CellV> }
