// #972: THE budget of the pane's drawn tree: the one place that knows the host's limit and what a row of each kind costs.
//
// #983: a tab is first drawn WHOLE (every list in full, a design cap of a list its only cut) and measured: under TARGET it is the draw, exact, nothing charged by estimate.
// Only a tab that does not fit whole goes through the three steps below.
//
// The desktop host blanks a pane whose drawn tree (the serialized tree of the SHOWN tab) is over HOST_LIMIT characters; past it our code does
// nothing, the pane is blank. So a tab is bounded by construction, never by today's row counts:
//   1. the tab is drawn once with every growing list empty and measured (`sizeOf`): its fixed part, exact (the tab bar, the card heads, the machine
//      cards, an open build with its stats, the switches of Расходы);
//   2. every growing list is a LANE: its rows as units, each charged what a row of its kind costs at the dearest (`COST`, held as upper bounds by
//      test/worst-case-972.test.tsx, which draws distinct rows with the widest titles and fails when a charge is under the drawn size);
//   3. `layout` shares the room left (TARGET less the fixed part) between the lanes: a lane that fits is drawn whole with no pager; the lanes that
//      do not fit share what is left equally and are cut into pages («новее» / «старее»); a row with child rows that does not fit keeps as many
//      children as fit and one «ещё N» row.
// The sum of the charges is never over the room, a charge is never under what is drawn, so the drawn tree is never over TARGET.
// No list takes a page size from a literal of its own: a design cap (`max`: «Готово» 6 runs a page, a run's builds 8 a page) only narrows what the budget allows.

/** the host blanks a pane whose drawn tree is over this many characters (#837 b3, #972) */
export const HOST_LIMIT = 100_000
/** what every tab and every group of Расходы is held under: the host's limit less a 5,000 margin */
export const TARGET = 95_000
/** the host stamps `,"press":{"plugin":"<name>","handle":N}` on every element with a handler: 51 characters with a 9-character plugin name and a 10-digit handle (the longest a 32-bit handle gets; the test host's are 9 and 10 digits) */
export const PRESS_STAMP = 51
/** a row's line of a picture's alt (the words a screen reader gets) and a free line of text (an error, a note) are cut to these many characters: a row's size never follows the length of a title */
export const ALT_MAX = 160
export const LINE_MAX = 200

/** the drawn size of a built element: its serialized length as the host holds it. The hook's own tree already carries a short stamp on every element with a handler
 *  (`,"press":{"plugin":"","handle":N}`); the host writes its own there, PRESS_STAMP characters: each stamp found is counted as that. */
const STAMP = /,"press":\{"plugin":"[^"]*","handle":\d+\}/g
export const sizeOf = (el: unknown): number => {
  const s = JSON.stringify(el) ?? ''
  let stamps = 0
  const bare = s.replace(STAMP, () => { stamps++; return '' })
  return bare.length + stamps * PRESS_STAMP
}

/** What ONE row of each kind adds to the drawn tree (characters), the row light included. Upper bounds: test/worst-case-972.test.tsx draws every kind
 *  with distinct rows and the widest titles and fails when the pane's charge for a state is under that state's drawn size. A `...Extra` is what the FIRST
 *  rows of a list add once on top (the shapes and icons its picture then shares), charged whenever the list has a row. */
export const COST = {
  /** a pager: the counter and the two words, on a label row or in a card's corner */
  pager: 1_650, // #994: two icon buttons (one octicon path, drawn once, used twice), the counter, and the 10-px drop that sets a corner pager on the title line; #995: + the dim end's hidden Button
  // ---- Agents
  session: 1_480, sub: 1_640, ticket: 1_640, calm: 1_150, agentsExtra: 2_900,
  // ---- Merge
  mergeWait: 1_520, mergeExtra: 900, mergeDone: 1_520, mergeDoneExtra: 900, // #995: a landing of «Готово» (a page of them)
  // ---- Progress: «Пайплайн» (#6) a line = its ticket, its strip of five stations, its word and two times; `pipeExtra` = what the section adds once (its card, head row and label row).
  //      Measured by test/pipeline-section.test.tsx (worst case): a line 1,458–1,554 per kind, 1,727 and 1,844 in mixed sets; the section's fixed part 1,972–2,474. Charged above that.
  pipeLine: 1_900, pipeExtra: 2_600,
  // ---- a plain line of text under a card (an offline remote host's «шло:» line)
  note: 220,
  // ---- the tables (Расходы): a row = its base, a part a cell with a word, a part for the share bar, a part a character of its words (a wrapped title is drawn whole), and a part for a
  //      ticket's state icon with the press on its number (`tableCost` below)
  tableRow: 300, tableCell: 65, tableBar: 180, tableChar: 1.6, tablePress: 450, tableExtra: 400,
  // ---- Тикеты: a tree row is charged what a second copy of it adds to a table that already draws it, measured, plus `ticketSlack` (a later row's longer coordinates and keys);
  //      `ticketsExtra` = what the first rows add once (the state icons and the chevron the picture then shares)
  ticketSlack: 40, ticketsExtra: 4_000,
} as const

/** what a table row of these cells is charged (`COST.table*`): a cell is a word or `{ t, share?, icon?, card? }` */
export const tableCost = (cells: unknown[]): number => {
  let texts = 0, chars = 0, bar = false, press = false
  for (const c of cells) {
    const o = (typeof c === 'string' ? { t: c } : c ?? {}) as { t?: string; share?: number; icon?: unknown; card?: unknown; href?: unknown }
    if (o.share != null) bar = true
    if (o.icon != null || o.card != null || o.href != null) press = true
    if (o.t) { texts++; chars += String(o.t).length }
  }
  return COST.tableRow + texts * COST.tableCell + (bar ? COST.tableBar : 0) + (press ? COST.tablePress : 0) + Math.ceil(chars * COST.tableChar)
}

/** A unit of a lane: a row (its cost), or a row with child rows: `own` = the row itself (and the tail rows it always has), `kids` = each child row's cost, in drawing order,
 *  `more` = the one «ещё N» row drawn when some children are not. */
export type Unit = number | { own: number; kids: number[]; more: number }
/** A growing list of a tab. `extra` = charged once when the lane draws a row; `max` = the most units a page may hold by design; `at` = the page asked for (default: the pane's own page of this key). */
export type Lane = { key: string; units: Unit[]; extra?: number; max?: number; at?: number
  /** #994: a unit that must stay on the drawn page (the open or picked row): the page that holds it is drawn, whatever `at` asks */ pin?: number }
/** a unit as drawn: its index in the lane and how many of its child rows are drawn (the rest is one «ещё N» row) */
export type Shown = { i: number; kids: number }
export type LanePlan = {
  /** the characters the lane may take */ grant: number
  pages: Shown[][]; /** the drawn page's index */ page: number; /** the drawn page */ shown: Shown[]
  /** the drawn units' places among all, from 1: `from`–`to` из `total` */ from: number; to: number; total: number
  /** more than one page: the pager is drawn */ paged: boolean
  /** what the drawn page is charged */ charged: number
}

const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0)
const full = (u: Unit) => (typeof u === 'number' ? u : u.own + sum(u.kids))
/** the least a unit can be drawn at: the row alone with its «ещё N» row */
const least = (u: Unit) => (typeof u === 'number' ? u : Math.min(full(u), u.own + (u.kids.length ? u.more : 0)))

/** `room` shared between needs: a need that fits its equal share is granted whole, what it leaves goes to the others; the needs that do not fit share the rest equally.
 *  The grants never sum over `room` (room < 0 counts as 0). */
export function share(room: number, needs: number[]): number[] {
  const grant = needs.map(() => 0)
  let left = Math.max(0, room)
  let open = needs.map((_n, i) => i)
  while (open.length) {
    const each = left / open.length
    const fit = open.filter(i => needs[i]! <= each)
    if (!fit.length) { for (const i of open) grant[i] = Math.floor(each); break }
    for (const i of fit) { grant[i] = needs[i]!; left -= needs[i]! }
    open = open.filter(i => !fit.includes(i))
  }
  return grant
}

/** an empty plan: the lane draws nothing (the measuring pass of a render; a lane the layout does not know) */
export const noPlan = (total = 0): LanePlan => ({ grant: 0, pages: [[]], page: 0, shown: [], from: 0, to: 0, total, paged: false, charged: 0 })

/** THE budget function. `room` = the characters the tab's lanes may take together (TARGET less the tab drawn with every lane empty); `at` = the page asked for, by lane key.
 *  Every lane first gets the least it can be drawn at (its dearest unit alone, child rows folded into «ещё N»), then `share` of the rest by need.
 *  `fits` = false: even the least does not fit the room (the fixed part alone is over the budget); the plans are then the least ones. */
export function layout(room: number, lanes: Lane[], at: Record<string, number> = {}): { plans: Record<string, LanePlan>; charged: number; fits: boolean } {
  const info = lanes.map(l => {
    const max = l.max && l.max > 0 ? l.max : Infinity
    if (!l.units.length) return { head: 0, max, min: 0, need: 0 }
    const head = (l.extra ?? 0) + COST.pager
    const fulls = l.units.map(full)
    let need = 0 // the dearest page with every child row drawn
    if (max === Infinity) need = sum(fulls)
    else for (let k = 0; k < fulls.length; k += max) need = Math.max(need, sum(fulls.slice(k, k + max)))
    return { head, max, min: head + Math.max(...l.units.map(least)), need: head + need }
  })
  const mins = sum(info.map(x => x.min))
  const fits = mins <= room
  const more = share(room - mins, info.map(x => x.need - x.min))
  const plans: Record<string, LanePlan> = {}
  let charged = 0
  lanes.forEach((l, n) => {
    const x = info[n]!
    if (!l.units.length) { plans[l.key] = noPlan(0); return }
    const grant = x.min + more[n]!
    const avail = grant - x.head
    const costOf = (u: Unit, kids: number) => (typeof u === 'number' ? u : kids >= u.kids.length ? full(u) : u.own + sum(u.kids.slice(0, kids)) + u.more)
    let shownAll: Shown[]
    const whole = sum(l.units.map(full)) <= avail && l.units.length <= x.max
    if (whole) shownAll = l.units.map((u, i) => ({ i, kids: typeof u === 'number' ? 0 : u.kids.length }))
    else {
      // a row with child rows takes at most half the lane when it is not alone (two rows a page at the least), and never less than its least form
      shownAll = l.units.map((u, i) => {
        if (typeof u === 'number') return { i, kids: 0 }
        const cap = Math.max(least(u), l.units.length > 1 ? avail / 2 : avail)
        if (full(u) <= cap) return { i, kids: u.kids.length }
        let kids = 0, spent = u.own + u.more
        while (kids < u.kids.length && spent + u.kids[kids]! <= cap) spent += u.kids[kids++]!
        return { i, kids }
      })
    }
    const pages: Shown[][] = [[]]
    let used = 0
    for (const s of shownAll) {
      const c = costOf(l.units[s.i]!, s.kids)
      const pg = pages[pages.length - 1]!
      if (pg.length && (used + c > avail || pg.length >= x.max)) { pages.push([s]); used = c } else { pg.push(s); used += c }
    }
    const pinned = l.pin != null && l.pin >= 0 ? pages.findIndex(pg => pg.some(s => s.i === l.pin)) : -1
    const page = pinned >= 0 ? pinned : Math.max(0, Math.min(pages.length - 1, Math.floor(l.at ?? at[l.key] ?? 0)))
    const shown = pages[page]!
    const from = sum(pages.slice(0, page).map(p => p.length)) + 1
    const cost = x.head + sum(shown.map(s => costOf(l.units[s.i]!, s.kids)))
    plans[l.key] = { grant, pages, page, shown, from, to: from + shown.length - 1, total: l.units.length, paged: pages.length > 1, charged: cost }
    charged += cost
  })
  return { plans, charged, fits }
}
