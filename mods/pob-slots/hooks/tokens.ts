// The monitor's tokens, in one place (#793 unit 3c). The shared kit (`draw.ts`) holds the dark base
// tokens and writes them into every picture it makes; the pane takes its tokens from here: the base with the pane's
// own values and additions, and `recolor` writes the pane's values over the shared kit's in a finished picture.
// Every text token is >= 4.5:1 on the card (tile), the zebra stripe, the hover and the selected fill
// (test/design.test.tsx computes it); the page behind the card is #1a1a19, the card #262628.
import { K as BASE } from './draw'

export const K = {
  ...BASE,
  // #837 u12 (autopilot rule: a value that differs from the baseline 50ea5412 and is asked for by no quoted night-ticket row goes back):
  // `failed`, `muted` stay the shared kit's (= the baseline's picture values #f85149 / #9198a1); NATIVE text (a Text, a Muted) keeps the baseline's C.failed / C.muted
  nfailed: '#ff6961', nmuted: '#98989f',
  stripe: '#2d2d30', /* hoverVeil: the hover of a Box laid OVER a picture: the baseline's accent #4493f8 at 18 % (alpha 2e; #837 pass 2 P1: back to the colour of 50ea5412), translucent so the text under it stays (#837 D2) */ hover: '#343438', hoverVeil: '#4493f82e', press: '#161617', // press: the fill of a button pressed this moment (darker than the card)
  ink: '#1c1c1e', white: '#ffffff', veil: '#808080', // veil: the neutral of every bar track, at its own opacity
  owner: '#ff9f0a', hold: '#ffd60a', // a ticket that waits for the owner / for other tickets
  // #837 unit 11, autopilot comparison 00:08Z: a run title of a queue row is a native Button in the baseline 50ea5412, drawn in the host's text colour and its dimColor (read off the picture)
  titleText: '#f0efec', titleDim: '#c3c2b7',
} as const

/** #837 b5: `hoverVeil` split for a picture's rect (`fill` + `fill-opacity`): the highlight of a row has the pill's own shape, one rect in this colour. Derived from the token, written once. */
export const HOVER_FILL = K.hoverVeil.slice(0, 7)
export const HOVER_OPACITY = String(+(parseInt(K.hoverVeil.slice(7, 9), 16) / 255).toFixed(2)).replace(/^0\./, '.')

/** `svg` with the pane's token values where the shared kit wrote its own (its style sheet and its classes). */
export const recolor = (svg: string) => {
  let out = svg
  for (const k of Object.keys(BASE) as (keyof typeof BASE)[]) if (K[k] !== BASE[k]) out = out.split(BASE[k]).join(K[k])
  return out
}

// Machine identity (CPU / RAM / slots only, never a state): documented in DESIGN-SYSTEM.md.
export const ROLE = { work: '#3987e5', agents: '#d55181', you: '#c9c9cf' } as const

// The spacing scale, one 4-px base (#793 unit 3c F10): every gap, padding, indent, air, band edge and corner radius of
// the pane is one of these five. Row heights and column widths are multiples of 4 as well (TROW, TABH, RAIL, BAR_W).
// A 1-2 px hairline (a bar's segment gap, a tick, an underline) is a stroke, not spacing.
export const SP = { xs: 4, s: 8, m: 12, l: 16, xl: 24 } as const
export const SCALE: readonly number[] = Object.values(SP)
// A corner radius is on the same scale; a pill (radius = half the height) follows its own height.
export const RADIUS = { s: SP.xs, m: SP.s } as const

// The grid sizes that tests and the drawing share: a table row (the line grid of every picture), the tab bar, the
// rail column, a nested level, a table band's left edge. All multiples of the 4-px base.
// ONE row pitch (px between the tops of consecutive rows) on every tab (#822 F11): a pill's 20 px + AIR, a table row, a
// slot-square row, a bar row of the machine cards. A later change of the pitch is this one edit.
export const PITCH = SP.xl
// #837 pass 2 (baseline 50ea5412): a card strip row = the 20-px pill + 6 px air = 26 px; a table row = 19 px, one engine cell.
export const CARD_PITCH = 26
/** a card's label row (`Идёт`, `В очереди`, `Готово`) = 22 px at 50ea5412 (rowSvg); the column-head row takes the same */
export const LABEL_PITCH = 22
/** the column-head row of a card (F04, the owner's table rule of #793): the one row the baseline 50ea5412 does not have, each card is this much taller; the baseline's label height */
export const HEAD_PITCH = LABEL_PITCH
export const GRID = { row: 19, tab: 27, rail: 22, indent: SP.xl, band: SP.l } as const
