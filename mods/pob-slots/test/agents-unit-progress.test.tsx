import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
// #129: a ticket row of an ORCH draws its unit progress «k/n» at the head of its act text when its held entry carries
// `units` (#151 unit 4: on every state, closed and finished-only included; the `ticket_units` map is in issue151-units.test.tsx);
// a ticket with no units keeps its text. The seam is the drawn pane
// (the desktop surface): the agents come in through the `fast` pull, the tickets through the `tickets` pull, and the alt text
// of each ticket row is read back.
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

// every string under `key` in the drawn tree, depth first
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
// the alt lines that name ticket #n
const ticketLines = (tree: any, n: number): string[] =>
  strings(tree, 'alt').flatMap(a => a.split('\n')).filter(l => l.includes(`#${n} `))

// the Tickets entry of #129 in the shape kit.tsx's Issue reads (labels, blockers, kids, parent, reason, state all present)
const TICKETS = (open: boolean) => JSON.stringify({ goals: [], loose: [], issues: { '129': { n: 129, title: 'Units', open, reason: null, labels: ['ready-for-agent'], parent: null, blockers: [], kids: [], state: open ? 'open' : 'closed' } } })

const setup = async ($: any, on: any, tickets: string) => {
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
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: PROPS })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return tree
  }
  return { mount }
}

// an ORCH session with `busy` subagents in work on ticket 129 and `fin` finished ones; `held` is the ticket's held entry's
// extra fields (`{}` for a plain held entry), or 'none' for no held entry at all (the ticket is then known only by its subagents)
const orch = (held: any, busy: number, fin: number) => {
  const subs = [
    ...Array.from({ length: busy }, (_, i) => ({ name: `worker: Unit ${i}`, type: 'worker', issue: 129, model: 'Claude Haiku 5.5', ctx: 5_000 + i, age: '1 min', busy: true })),
    ...Array.from({ length: fin }, (_, i) => ({ name: `worker: Done ${i}`, type: 'worker', issue: 129, model: 'Claude Haiku 5.5', ctx: 6_000 + i, age: '2 min', busy: false })),
  ]
  return {
    sid: 'sid-orch-1', name: 'ORCH 05dd320c', kind: 'background', busy: true, issue: null,
    held: held === 'none' ? [] : [{ number: 129, url: '', claimed: false, claimed_at: null, subagents: busy + fin, ...held }],
    model: 'Claude Sonnet 5.5', ctx: 40_000, age: '1 min', last: '', subagents: subs,
  }
}
const UNITS = { done: 3, total: 7 }
const WITH = '3/7 · в работе: 2 воркера · 14 отработали'
const WITHOUT = 'в работе: 2 воркера · 14 отработали'

test('a ticket in work with units draws «3/7» at the head of its act text', async ($, on) => {
  const { mount } = await setup($, on, TICKETS(true))
  const tree = await mount([orch({ units: UNITS }, 2, 14)])
  const lines = ticketLines(tree, 129)
  if (!lines.some(l => l.includes(`Units: ${WITH}`))) throw new Error(`the ticket act text is not "${WITH}": ${lines}`)
})

test('the same ticket with no units draws the act text as before (no «части»)', async ($, on) => {
  const { mount } = await setup($, on, TICKETS(true))
  const tree = await mount([orch({}, 2, 14)])
  const lines = ticketLines(tree, 129)
  if (!lines.some(l => l.includes(`Units: ${WITHOUT}`))) throw new Error(`the ticket act text is not "${WITHOUT}": ${lines}`)
  if (lines.some(l => l.includes('части'))) throw new Error(`a ticket with no units draws «части»: ${lines}`)
})

// #151 unit 4: units are drawn on every state, a closed ticket included: «k/n» leads, then «закрыт»
test('a closed ticket with units draws «3/7 · закрыт · …»', async ($, on) => {
  const { mount } = await setup($, on, TICKETS(false))
  const tree = await mount([orch({ units: UNITS }, 0, 14)])
  const lines = ticketLines(tree, 129)
  if (!lines.some(l => l.includes('Units: 3/7 · закрыт · 14 отработали'))) throw new Error(`the closed ticket act text is not "3/7 · закрыт · 14 отработали": ${lines}`)
})

test('a held ticket (state 2) with units draws «3/7» before «ещё не взят»', async ($, on) => {
  const { mount } = await setup($, on, TICKETS(true))
  const tree = await mount([orch({ units: UNITS }, 0, 0)])
  const lines = ticketLines(tree, 129)
  if (!lines.some(l => l.includes('Units: 3/7 · ещё не взят'))) throw new Error(`the held ticket act text is not "3/7 · ещё не взят": ${lines}`)
})

// state 3: no held entry, only finished subagents on the ticket: its text is as before unless the session has no `ticket_units` entry for it
test('a ticket known only by its finished subagents (state 3) draws its text as before, no «части»', async ($, on) => {
  const { mount } = await setup($, on, TICKETS(true))
  const tree = await mount([orch('none', 0, 14)])
  const lines = ticketLines(tree, 129)
  if (!lines.some(l => l.includes('Units: 14 отработали'))) throw new Error(`the state-3 act text is not "14 отработали": ${lines}`)
  if (lines.some(l => l.includes('части'))) throw new Error(`a state-3 ticket draws «части»: ${lines}`)
})
