import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
import { textWidth, PX } from '../hooks/draw'

// #151 unit 2 of 2 (draw-words): a unit count draws as «k/n» with no word «части», on the Agents tile's ticket row and on the
// Pipeline row's status text. An ORCH session row draws its tick text as the longest form that ends before the model column
// (layout (b): «k/N тикетов», else «k/N тик.», else «k/N»; k = the held tickets closed or merge-landed, N = the held ones).
// The seam is the drawn pane, read back as in issue151-units.test.tsx; the ends are measured with the code's own textWidth and px.
const PIPE_PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 123, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }
const agentsProps = (bodyColumns: number): any => ({ title: 'Monitor', isFocused: false, bodyColumns, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} })

// every string under a key in the drawn tree, depth first
const each = (x: any, f: (o: any) => void) => {
  if (Array.isArray(x)) { for (const v of x) each(v, f); return }
  if (!x || typeof x !== 'object') return
  f(x)
  for (const v of Object.values(x)) each(v, f)
}
const strings = (tree: any, key: string): string[] => {
  const out: string[] = []
  each(tree, o => { if (typeof o[key] === 'string') out.push(o[key].replace(/'/g, '"')) })
  return out
}
const allStrings = (x: any, out: string[] = []): string[] => {
  if (typeof x === 'string') out.push(x)
  else if (Array.isArray(x)) for (const v of x) allStrings(v, out)
  else if (x && typeof x === 'object') for (const v of Object.values(x)) allStrings(v, out)
  return out
}
// the alt lines that name ticket #n
const ticketLines = (tree: any, n: number): string[] =>
  strings(tree, 'alt').flatMap(a => a.split('\n')).filter(l => l.includes(`#${n} `))
// the drawn <text> elements of the picture: x and the words
const drawnTextEls = (tree: any): { x: number; t: string }[] =>
  [...allStrings(tree).join('\n').matchAll(/<text x='([\d.]+)'[^>]*>([^<]*)<\/text>/g)].map(m => ({ x: Number(m[1]), t: m[2]! }))
// the ORCH row's tick text and its model text, as drawn
const tickGeometry = (tree: any) => {
  const els = drawnTextEls(tree)
  return { tick: els.find(e => /^\d+\/\d+/.test(e.t)), model: els.find(e => e.t === 'Sonnet 5.5') }
}
// the tick text ends before the model column starts (its end: its x plus its width at the code's px, as the picture fits it)
const endsBefore = (tree: any) => {
  const g = tickGeometry(tree)
  if (!g.tick) throw new Error('the ORCH tick text is not drawn')
  if (!g.model) throw new Error('the ORCH model text is not drawn')
  const end = g.tick.x + textWidth(g.tick.t, PX)
  if (!(end < g.model.x)) throw new Error(`the tick text «${g.tick.t}» ends at ${end.toFixed(2)} px, not before the model column at ${g.model.x}`)
}

// the Tickets pull: one ticket per entry of `open` (true = open, false = closed)
const TICKETS = (open: Record<number, boolean>) => JSON.stringify({ goals: [], loose: [], issues: Object.fromEntries(Object.entries(open).map(([n, o]) => [n, {
  n: Number(n), title: `Issue ${n}`, open: o, reason: null, labels: ['ready-for-agent'], parent: null, blockers: [], kids: [], state: o ? 'open' : 'closed',
}])) })

// the Agents tile at `bodyColumns`: the fast pull's payload (`agents`, `merge`) is set per mount
const setupAgents = async ($: any, on: any, tickets: string, bodyColumns = 60) => {
  const clock = mock.clock(on)
  let fast: any = {}
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-17T10:00:00+00:00', ...fast })
      : e.argv[2] === 'tickets' ? tickets : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const mount = async (next: any) => {
    fast = next
    await clock.advance(6_000) // one fast tick (5 s): the pull runs with the new payload before the pane is drawn
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: agentsProps(bodyColumns) })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return tree
  }
  return { mount }
}

// (a) a ticket known only by its finished subagent (state 3) with ticket_units {3,3}: «3/3», no «части» anywhere
test('(a) a state-3 ticket row with ticket_units {3,3} draws «3/3» and no «части» anywhere', async ($, on) => {
  const { mount } = await setupAgents($, on, TICKETS({ 136: true }))
  const tree = await mount({ agents: [{
    sid: 'sid-orch-136', name: 'ORCH 0a1b2c3d', kind: 'background', busy: true, issue: null,
    held: [], model: 'Claude Sonnet 5.5', ctx: 40_000, age: '1 min', last: '', ticket_units: { '136': { done: 3, total: 3 } },
    subagents: [{ name: 'worker: Done 0', type: 'worker', issue: 136, model: 'Claude Haiku 5.5', ctx: 6_000, age: '2 min', busy: false }],
  }] })
  const lines = ticketLines(tree, 136)
  if (!lines.some(l => l.includes('Issue 136: 3/3 ·'))) throw new Error(`the state-3 ticket act text does not lead with «3/3»: ${lines}`)
  const texts = allStrings(tree)
  if (texts.some(s => s.includes('части'))) throw new Error(`a drawn string still says «части»: ${texts.filter(s => s.includes('части'))}`)
})

// Pipeline row: a status text with the units written as «k/n» after the stage word
const PIPE_STRICT = (over: any) => ({
  number: 0, title: '', stage: 'ready', failed: false, workers: 0, strong: 0, researchers: 0,
  merge_position: null, merge_percent: null,
  started_at: new Date(Date.now() - 120 * 60_000).toISOString(), stage_since: new Date(Date.now() - 40 * 60_000).toISOString(), ...over,
})
const setupPipeline = async ($: any, on: any) => {
  const clock = mock.clock(on)
  let pipeline: any = undefined
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-17T10:00:00+00:00', agents: [], merge: {}, ...(pipeline === undefined ? {} : { pipeline }) })
      : e.argv[2] === 'tickets' ? TICKETS({}) : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const progress = async (next: any) => {
    pipeline = next
    await clock.advance(6_000) // one fast tick (5 s): the pull runs with the new pipeline before the pane is drawn
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: PIPE_PROPS })
    await clock.settle()
    await ui.press({ key: 'tab-progress' })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return tree
  }
  return { progress }
}

// (b) a Pipeline row with units {2,4} draws «2/4», and no «части» anywhere
test('(b) a Pipeline row with units {2,4} draws «2/4» and no «части» anywhere', async ($, on) => {
  const { progress } = await setupPipeline($, on)
  const tree = await progress([
    PIPE_STRICT({ number: 136, title: 'Units row', stage: 'work', workers: 2, researchers: 1, units: { done: 2, total: 4 } }),
  ])
  const alts = strings(tree, 'alt').flatMap(a => a.split('\n'))
  if (!alts.some(l => l.includes('#136 ') && l.includes('· 2/4'))) throw new Error(`the Pipeline row's alt lacks «· 2/4»: ${alts}`)
  const texts = allStrings(tree)
  if (!texts.some(s => s.includes('2/4'))) throw new Error(`the Pipeline row draws no «2/4»: ${texts}`)
  if (texts.some(s => s.includes('части'))) throw new Error(`a drawn string still says «части»: ${texts.filter(s => s.includes('части'))}`)
})

// (c) an ORCH session row holding N tickets. At 60 columns the tick gap to the model column is 37 px: «k/N» is the form drawn
// («1/3» and «0/3» are 21.7 px; «k/N тик.» is 47.8 px and «k/N тикетов» 69.6 px, neither fits).
const orchHolding = (numbers: number[]) => ({
  sid: 'sid-orch-151', name: 'ORCH 151c0ffe', kind: 'background', busy: true, issue: null,
  held: numbers.map(number => ({ number, claimed: false, claimed_at: null, subagents: 0 })),
  model: 'Claude Sonnet 5.5', ctx: 40_000, age: '1 min', last: '', subagents: [],
})
const HELD3 = [136, 137, 138]

test('(c1) an ORCH row holding 3 tickets, 1 closed, draws «1/3» at 60 columns and ends before the model column', async ($, on) => {
  const { mount } = await setupAgents($, on, TICKETS({ 136: false, 137: true, 138: true }), 60)
  const tree = await mount({ agents: [orchHolding(HELD3)], merge: {} })
  const g = tickGeometry(tree)
  if (g.tick?.t !== '1/3') throw new Error(`the ORCH tick text at 60 columns is «${g.tick?.t}», not «1/3»`)
  endsBefore(tree)
  if (allStrings(tree).some(s => s.includes('части'))) throw new Error(`a drawn string still says «части»`)
})

test('(c2) an ORCH row holding 3 tickets, none closed, draws «0/3» at 60 columns and ends before the model column', async ($, on) => {
  const { mount } = await setupAgents($, on, TICKETS({ 136: true, 137: true, 138: true }), 60)
  const tree = await mount({ agents: [orchHolding(HELD3)], merge: {} })
  const g = tickGeometry(tree)
  if (g.tick?.t !== '0/3') throw new Error(`the ORCH tick text at 60 columns is «${g.tick?.t}», not «0/3»`)
  endsBefore(tree)
  if (allStrings(tree).some(s => s.includes('части'))) throw new Error(`a drawn string still says «части»`)
})

test('(c3) a ticket whose merge landed (rc 0) counts as done; a landed rc 1 does not: «1/3» at 60 columns', async ($, on) => {
  const { mount } = await setupAgents($, on, TICKETS({ 136: true, 137: true, 138: true }), 60)
  const tree = await mount({ agents: [orchHolding(HELD3)], merge: { landed: [{ rc: 0, issues: [137] }, { rc: 1, issues: [138] }] } })
  const g = tickGeometry(tree)
  if (g.tick?.t !== '1/3') throw new Error(`the ORCH tick text at 60 columns is «${g.tick?.t}», not «1/3» (landed 137 rc 0, 138 rc 1)`)
  endsBefore(tree)
  if (allStrings(tree).some(s => s.includes('части'))) throw new Error(`a drawn string still says «части»`)
})

// (d) at 123 columns the gap is 76 px and the tick slot 66 px: «1/3 тикетов» (69.6 px) does not fit, «1/3 тик.» (47.8 px) does
test('(d) at 123 columns an ORCH row holding 3 tickets, 1 closed, draws «1/3 тик.» and ends before the model column', async ($, on) => {
  const { mount } = await setupAgents($, on, TICKETS({ 136: false, 137: true, 138: true }), 123)
  const tree = await mount({ agents: [orchHolding(HELD3)], merge: {} })
  const g = tickGeometry(tree)
  if (g.tick?.t !== '1/3 тик.') throw new Error(`the ORCH tick text at 123 columns is «${g.tick?.t}», not «1/3 тик.»`)
  endsBefore(tree)
})

// (e) at 123 columns the two-digit count fits the short form: «10/12 тик.» is 63.1 px against the 66-px slot
test('(e) at 123 columns an ORCH row holding 12 tickets, 10 closed, draws «10/12 тик.» and ends before the model column', async ($, on) => {
  const nums = Array.from({ length: 12 }, (_, i) => 136 + i)
  const { mount } = await setupAgents($, on, TICKETS(Object.fromEntries(nums.map((n, i) => [n, i >= 10]))), 123)
  const tree = await mount({ agents: [orchHolding(nums)], merge: {} })
  const g = tickGeometry(tree)
  if (g.tick?.t !== '10/12 тик.') throw new Error(`the ORCH tick text at 123 columns is «${g.tick?.t}», not «10/12 тик.»`)
  endsBefore(tree)
})

// (f) at 60 columns a two-digit count has only the bare form «10/12» (37.08 px against the 37-px gap): it is drawn whole, not cut,
// and it reaches the model column's x by 0.08 px (the layout's exception, see the report)
test('(f) at 60 columns 12 held tickets, 10 closed, draw the bare «10/12» whole, touching the model column by 0.08 px at most', async ($, on) => {
  const nums = Array.from({ length: 12 }, (_, i) => 136 + i)
  const { mount } = await setupAgents($, on, TICKETS(Object.fromEntries(nums.map((n, i) => [n, i >= 10]))), 60)
  const tree = await mount({ agents: [orchHolding(nums)], merge: {} })
  const g = tickGeometry(tree)
  if (g.tick?.t !== '10/12') throw new Error(`the ORCH tick text at 60 columns is «${g.tick?.t}», not the bare «10/12»`)
  const over = g.tick!.x + textWidth(g.tick!.t, PX) - g.model!.x
  if (over > 0.1) throw new Error(`the bare «10/12» reaches ${over.toFixed(2)} px into the model column`)
})
