import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
// #151 unit 4: the unit progress «k/n» is drawn on every state of an ORCH's ticket row (states 3 and 4 included, a
// ticket known only by its finished subagents or closed), read from the session's `ticket_units` map (the payload contract),
// and on the Pipeline row when the row carries `units`. The seam is the drawn pane: the `fast` pull carries the agents or the
// pipeline, the Agents tile or the Progress tab is drawn, and the drawn text is read back.
const AGENTS_PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }
const PIPE_PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 123, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

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
// the host's pictures are written with single quotes: read them with double ones
const norm = (s: string) => s.replace(/'/g, '"')
// the words a reader sees in the drawn pictures, in drawing order
const drawnTexts = (all: string[]): string[] =>
  all.flatMap(s => [...norm(s).matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1]!))
// the alt lines that name ticket #n
const ticketLines = (tree: any, n: number): string[] =>
  strings(tree, 'alt').flatMap(a => a.split('\n')).filter(l => l.includes(`#${n} `))

// the Tickets pull: one ticket #136, open or closed
const TICKETS = (open: boolean) => JSON.stringify({ goals: [], loose: [], issues: { '136': { n: 136, title: 'Units', open, reason: null, labels: ['ready-for-agent'], parent: null, blockers: [], kids: [], state: open ? 'open' : 'closed' } } })
const EMPTY_TICKETS = '{"goals":[],"loose":[],"issues":{}}'

// an ORCH session with no held entry: ticket #136 is known only by its finished subagents (state 3, or 4 when closed); its
// unit progress comes from the session's `ticket_units` map, as the payload contract carries it
const orchWithUnits = (ticket_units: any) => ({
  sid: 'sid-orch-136', name: 'ORCH 0a1b2c3d', kind: 'background', busy: true, issue: null,
  held: [], model: 'Claude Sonnet 5.5', ctx: 40_000, age: '1 min', last: '', ticket_units,
  subagents: [{ name: 'worker: Done 0', type: 'worker', issue: 136, model: 'Claude Haiku 5.5', ctx: 6_000, age: '2 min', busy: false }],
})

const setupAgents = async ($: any, on: any, tickets: string) => {
  const clock = mock.clock(on)
  let agents: any[] = []
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-17T10:00:00+00:00', agents })
      : e.argv[2] === 'tickets' ? tickets : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const mount = async (next: any[]) => {
    agents = next
    await clock.advance(6_000) // one fast tick (5 s): the pull runs with the new agents before the pane is drawn
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: AGENTS_PROPS })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return tree
  }
  return { mount }
}

test('(a) a ticket known only by finished subagents (state 3) with ticket_units draws «3/3» first in its text', async ($, on) => {
  const { mount } = await setupAgents($, on, TICKETS(true))
  const tree = await mount([orchWithUnits({ '136': { done: 3, total: 3 } })])
  const lines = ticketLines(tree, 136)
  if (!lines.some(l => l.includes('Units: 3/3 ·'))) throw new Error(`the state-3 ticket act text does not lead with «3/3»: ${lines}`)
})

test('(b) a closed ticket (state 4) with ticket_units draws «3/3» first, before «закрыт»', async ($, on) => {
  const { mount } = await setupAgents($, on, TICKETS(false))
  const tree = await mount([orchWithUnits({ '136': { done: 3, total: 3 } })])
  const lines = ticketLines(tree, 136)
  if (!lines.some(l => l.includes('Units: 3/3 · закрыт'))) throw new Error(`the closed ticket act text does not lead with «3/3 · закрыт»: ${lines}`)
})

test('(a0) a ticket with no ticket_units entry (absent map) draws no «части»', async ($, on) => {
  const { mount } = await setupAgents($, on, TICKETS(true))
  const tree = await mount([orchWithUnits(undefined)])
  const lines = ticketLines(tree, 136)
  if (lines.length === 0) throw new Error('the ticket #136 row is not drawn')
  if (lines.some(l => l.includes('части'))) throw new Error(`a ticket with no ticket_units draws «части»: ${lines}`)
})

// ---- the Pipeline row ----

const setupPipeline = async ($: any, on: any) => {
  const clock = mock.clock(on)
  let pipeline: any = undefined
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-17T10:00:00+00:00', agents: [], merge: {}, ...(pipeline === undefined ? {} : { pipeline }) })
      : e.argv[2] === 'tickets' ? EMPTY_TICKETS : '{}',
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

const pipeRow = (over: any) => ({
  number: 0, title: '', stage: 'ready', failed: false, workers: 0, strong: 0, researchers: 0,
  merge_position: null, merge_percent: null,
  started_at: new Date(Date.now() - 120 * 60_000).toISOString(), stage_since: new Date(Date.now() - 40 * 60_000).toISOString(), ...over,
})

test('(c) a Pipeline row with units {3,3} draws «3/3»; a row with units null draws no «части»', async ($, on) => {
  const { progress } = await setupPipeline($, on)
  const tree = await progress([
    pipeRow({ number: 136, title: 'Units row', stage: 'work', workers: 2, researchers: 1, units: { done: 3, total: 3 } }),
    pipeRow({ number: 137, title: 'Plain row', stage: 'orch', units: null }),
  ])
  const all = allStrings(tree)
  const texts = drawnTexts(all)
  const iUnits = texts.indexOf('Units row')
  const iPlain = texts.indexOf('Plain row')
  if (iUnits < 0 || iPlain < iUnits) throw new Error(`the two rows are not drawn in order: ${texts}`)
  const mine = texts.slice(iUnits, iPlain)
  const plain = texts.slice(iPlain)
  if (!mine.some(t => t.includes('3/3'))) throw new Error(`the units row lacks «3/3»: ${mine}`)
  if (plain.some(t => t.includes('части'))) throw new Error(`the row with units null draws «части»: ${plain}`)
  // the same words live in the row's alt (the picture's own text of the row)
  const alts = strings(tree, 'alt').flatMap(a => a.split('\n'))
  if (!alts.some(l => l.includes('#136 ') && l.includes('3/3'))) throw new Error(`the units row's alt lacks «3/3»: ${alts}`)
  if (alts.some(l => l.includes('#137 ') && l.includes('части'))) throw new Error(`the null-units row's alt draws «части»: ${alts}`)
})
