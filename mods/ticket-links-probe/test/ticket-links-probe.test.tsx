import { test } from 'claude-code/testing'
import { createProbe, type LogIo, type Render } from '../hooks/probe'

// #176 row 13 (throwaway probe): each desktop AssistantMessage render appends one JSON line to the log, and the drawing
// is the tree next(e) returned. The test realm and the hook realm are apart, so the logic is tested here through
// createProbe() with an in-memory LogIo; the hook-level tests check only that the hook returns next(e)'s tree.

const LONG = 'Ответ с длинным текстом: ' + 'x'.repeat(300)
const PATH = '/probe/test.jsonl'
const AT = new Date(2026, 9, 18, 21, 18, 51)

// an in-memory file store with the probe's three file calls; `failWrites` makes every write refuse
const memIo = (failWrites = false) => {
  const files = new Map<string, string>()
  const io: LogIo = {
    exists: async p => files.has(p),
    read: async p => {
      const t = files.get(p)
      if (t === undefined) throw new Error(`ENOENT: ${p}`)
      return t
    },
    write: async (p, t) => {
      if (failWrites) throw new Error(`ENOTDIR: ${p}`)
      files.set(p, t)
    },
  }
  return { files, io }
}

// the log's lines, parsed
const rowsOf = (files: Map<string, string>): any[] =>
  (files.get(PATH) ?? '').split('\n').filter(l => l.trim() !== '').map(l => JSON.parse(l))

// one render as the hook would hand it over; a test overrides only what it checks
const render = (over: Partial<Render> = {}): Render => ({
  surface: 'desktop',
  text: 'reply',
  drawn: { type: 'Text', props: {} },
  trace: [],
  at: AT,
  session: async () => 'session-1',
  ...over,
})

// ---- the probe's logic, as createProbe() takes it ----

test('row 13: desktop, an isSummary block appends one line with isSummary true and the first 200 characters', async () => {
  const { files, io } = memIo()
  const probe = createProbe()
  await probe(io, PATH, render({ text: LONG, isSummary: true }))
  const got = rowsOf(files)
  if (got.length !== 1) throw new Error(`one line expected, the log holds ${got.length}`)
  if (got[0].isSummary !== true) throw new Error(`isSummary ${JSON.stringify(got[0].isSummary)} is not true`)
  if (got[0].text200 !== Array.from(LONG).slice(0, 200).join('')) throw new Error('text200 is not the first 200 characters')
  if (got[0].session !== 'session-1') throw new Error(`session ${JSON.stringify(got[0].session)} is not session-1`)
  if (got[0].local_time !== '2026-10-18T21:18:51.000' + localOffset(AT)) throw new Error(`local_time ${got[0].local_time}`)
})

test('row 13: a non-desktop surface writes nothing', async () => {
  const { files, io } = memIo()
  const probe = createProbe()
  await probe(io, PATH, render({ surface: 'terminal', text: 'terminal reply' }))
  if (rowsOf(files).length !== 0) throw new Error('a terminal render wrote a line')
})

test('row 13: a write that rejects does not throw', async () => {
  const { io } = memIo(true)
  const probe = createProbe()
  await probe(io, PATH, render({ text: 'write fails here' }))
})

test('row 13: a repeated identical (text, isSummary) render writes once; a changed one writes again', async () => {
  const { files, io } = memIo()
  const probe = createProbe()
  await probe(io, PATH, render({ text: 'same block', isSummary: true }))
  await probe(io, PATH, render({ text: 'same block', isSummary: true }))
  if (rowsOf(files).length !== 1) throw new Error(`a repeat wrote again: ${rowsOf(files).length} lines`)
  await probe(io, PATH, render({ text: 'same block', isSummary: false }))
  if (rowsOf(files).length !== 2) throw new Error(`a changed isSummary did not write: ${rowsOf(files).length} lines`)
})

test('row 13: drawn_by_mod is true when the tree next(e) resolved to is a Markdown', async () => {
  const { files, io } = memIo()
  const probe = createProbe()
  await probe(io, PATH, render({ text: 'drawn below', drawn: { type: 'Markdown', props: { text: 'drawn below' } } }))
  if (rowsOf(files)[0]?.drawn_by_mod !== true) throw new Error(`drawn_by_mod ${JSON.stringify(rowsOf(files)[0]?.drawn_by_mod)} is not true`)
})

test('row 13: drawn_by_mod is false when ticket-links ran beneath and drew nothing', async () => {
  const { files, io } = memIo()
  const probe = createProbe()
  await probe(io, PATH, render({ text: 'left to the app', isSummary: true, trace: ['ticket-links'] }))
  if (rowsOf(files)[0]?.drawn_by_mod !== false) throw new Error(`drawn_by_mod ${JSON.stringify(rowsOf(files)[0]?.drawn_by_mod)} is not false`)
})

test('row 13: drawn_by_mod is null when ticket-links is not beneath the hook', async () => {
  const { files, io } = memIo()
  const probe = createProbe()
  await probe(io, PATH, render({ text: 'no mod beneath', trace: ['some-other-mod'] }))
  if (rowsOf(files)[0]?.drawn_by_mod !== null) throw new Error(`drawn_by_mod ${JSON.stringify(rowsOf(files)[0]?.drawn_by_mod)} is not null`)
})

test('row 13: isSummary absent is logged as null', async () => {
  const { files, io } = memIo()
  const probe = createProbe()
  await probe(io, PATH, render({ text: 'no summary flag' }))
  if (rowsOf(files)[0]?.isSummary !== null) throw new Error(`isSummary ${JSON.stringify(rowsOf(files)[0]?.isSummary)} is not null when absent`)
})

// the UTC offset of a date, as localIso writes it (+HH:MM or -HH:MM)
function localOffset(d: Date): string {
  const off = -d.getTimezoneOffset()
  const a = Math.abs(off)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${off >= 0 ? '+' : '-'}${p(Math.floor(a / 60))}:${p(a % 60)}`
}

// ---- the hook, through the test host: it returns next(e)'s tree and never changes the drawing ----

const mountReply = ($: any, props: any, surface: 'desktop' | 'terminal' = 'desktop') =>
  $.ui.mount({ plugin: 'ticket-links-probe', surface, component: 'AssistantMessage', props: { isFirstOfReply: true, ...props } })

// the bottom hook: the app's own tree, drawn when the probe passes the reply on with next(e)
const appHook = (on: any) => on('ui.render', { component: 'AssistantMessage' }, ($: any, e: any) => {
  const { Text } = $.ui.resolve(e)
  return <Text key="app">app</Text>
})

// every string in the drawn tree (text children and string props alike)
const allStrings = (x: any, out: string[] = []): string[] => {
  if (typeof x === 'string') out.push(x)
  else if (Array.isArray(x)) for (const v of x) allStrings(v, out)
  else if (x && typeof x === 'object') for (const v of Object.values(x)) allStrings(v, out)
  return out
}

test('row 13 hook: desktop, an isSummary block is drawn as next(e) returned it', async ($, on) => {
  appHook(on)
  const ui = await mountReply($, { text: LONG, isSummary: true })
  const tree = await ui.drawn()
  if (!allStrings(tree).includes('app')) throw new Error('the drawing is not the tree next(e) returned')
  await ui.unmount()
})

test('row 13 hook: terminal, the app tree is drawn', async ($, on) => {
  appHook(on)
  const ui = await mountReply($, { text: 'terminal reply' }, 'terminal')
  const tree = await ui.drawn()
  if (!allStrings(tree).includes('app')) throw new Error('the app tree is not drawn on terminal')
  await ui.unmount()
})

test('row 13 hook: a Markdown below the probe is returned unchanged', async ($, on) => {
  on('ui.render', { component: 'AssistantMessage' }, ($: any, e: any) => {
    const { Markdown } = $.ui.resolve(e)
    return <Markdown text={e.props.text} />
  })
  const ui = await mountReply($, { text: 'drawn below' })
  const tree = await ui.drawn()
  if (!allStrings(tree).includes('drawn below')) throw new Error('the Markdown next(e) returned is not the drawing')
  await ui.unmount()
})
