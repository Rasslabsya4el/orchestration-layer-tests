import { test, mock } from 'claude-code/testing'
import { COMMAND } from '../hooks/register'
import { PANE, REPO } from '../hooks/project'

// #181 unit 2: the monitor pulls `tickets` every 15 s (EVERY.tickets), so a ticket change reaches the pane within 30 s.
// The seams: the drawn pane (the Тикеты tab, its ticket's title) and the process.run calls of the `tickets` mode.
// The fake collector keeps its tickets payload in `w.tickets`; a test changes it at t=0 and reads what the pane draws.
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 123, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

// one open ticket #136 in the loose list, with the given title
const ticketsPayload = (title: string) => JSON.stringify({ goals: [], loose: [136], issues: { '136': { n: 136, title, open: true, reason: null, labels: ['ready-for-agent'], parent: null, blockers: [], kids: [], state: 'open' } } })

// every string the drawn tree holds (native text, svg markup), joined so a title reads as one run of text
const allStrings = (x: any, out: string[] = []): string[] => {
  if (typeof x === 'string') out.push(x)
  else if (Array.isArray(x)) for (const v of x) allStrings(v, out)
  else if (x && typeof x === 'object') for (const v of Object.values(x)) allStrings(v, out)
  return out
}
// the host's pictures are written with single quotes: read them with double ones
const norm = (s: string) => s.replace(/'/g, '"')

// the fake world: the collector answers `tickets` with `w.tickets`; while `w.hold` is set the answer waits for `w.release()`
const world = (on: any) => {
  const w = { tickets: ticketsPayload('Old title'), ticketRuns: 0, hold: false, release: null as null | (() => void) }
  on('process.run', async (_$: any, e: any) => {
    if (e.argv[2] === 'tickets') {
      w.ticketRuns++
      if (w.hold) await new Promise<void>(r => { w.release = r })
    }
    return { value: { exitCode: 0, stdout: e.argv[2] === 'tickets' ? w.tickets : '{}', stderr: '', isStdoutTruncated: false } } as any
  })
  // #173: command.run starts the pane only when the session's cwd is this repo; the mount's requestId is the pane id (PANE)
  on('session.cwd', async () => ({ value: REPO }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  // no config file and no collector file: the defaults hold; no write reaches the disk
  on('fs.exists', async () => ({ value: false }) as any)
  on('fs.read', async () => ({ value: '' }) as any)
  on('fs.write', async () => ({ value: undefined }) as any)
  return w
}

// (a) a change made at t=0 (after the immediate pull) is drawn on the Тикеты tab by 30 s of fake time
test('(a) a ticket change made at t=0 is drawn on the Тикеты tab by 30 s', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  w.tickets = ticketsPayload('Changed title') // t = 0
  await clock.advance(30_000)
  await clock.settle()
  const ui = await ($ as any).ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: PROPS })
  await clock.settle()
  await ui.press({ key: 'tab-tickets' })
  await clock.settle()
  const tree = await ui.drawn()
  await ui.unmount()
  const shown = allStrings(tree).map(norm).join('\n')
  if (!shown.includes('Changed title')) throw new Error('at 30 s the Тикеты tab does not draw the changed title «Changed title»')
  if (shown.includes('Old title')) throw new Error('at 30 s the Тикеты tab still draws the old title «Old title»')
})

// (b) the tickets pull runs every 15 s: in 60 s of fake time the collector is asked at most 5 times (the start pull and 4 ticks),
// and at least 4 (the pull still runs on its period, not once)
test('(b) in 60 s of fake time the collector is asked for tickets 4 or 5 times, no storm', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  await clock.advance(60_000)
  await clock.settle()
  if (w.ticketRuns > 5) throw new Error(`the collector was asked for tickets ${w.ticketRuns} times in 60 s, want at most 5`)
  if (w.ticketRuns < 4) throw new Error(`the collector was asked for tickets ${w.ticketRuns} times in 60 s, want at least 4 (every 15 s)`)
})

// (c) the in-flight guard of `pull`: a tickets pull that never resolves for 40 s is not doubled by the ticks that fall inside it
test('(c) a slow tickets pull (no answer for 40 s) is not doubled by the ticks inside it', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const before = w.ticketRuns // the start pull (answered at once)
  w.hold = true
  await clock.advance(40_000) // ticks at 15 s and 30 s fall in the window; the 15 s pull never answers
  await clock.settle()
  const started = w.ticketRuns - before
  w.hold = false
  if (w.release) w.release()
  await clock.settle()
  if (started !== 1) throw new Error(`${started} tickets pulls started while one was in flight for 40 s, want exactly 1`)
})
