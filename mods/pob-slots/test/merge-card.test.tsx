import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
// #37: the Merge card's running row for a commit holder shows the commit's message as its task and its ticket `#N`
// (or the word `commit` when the message names no ticket); a merge holder's row is unchanged. The seam is the drawn
// pane (the desktop surface): the `fast` pull carries `merge` (what `merge-status --json` gives the collector), the
// drawn tree is read back. Every string of the tree counts, a drawn text or a link's label alike.
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

const allStrings = (x: any, out: string[] = []): string[] => {
  if (typeof x === 'string') out.push(x)
  else if (Array.isArray(x)) for (const v of x) allStrings(v, out)
  else if (x && typeof x === 'object') for (const v of Object.values(x)) allStrings(v, out)
  return out
}
// the words a reader sees in the drawn pictures (an icon's alt is not drawn, so it does not count as a cell's text)
const drawnTexts = (all: string[]): string[] =>
  all.flatMap(s => [...s.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1]!))

// the human durations the collector adds (merge_shape's eta_h / elapsed_h), and the phase fields of a suite in progress
const runningOf = (over: any) => ({
  branches: [], issues: [], phase: 'suite', started: '2026-10-16T13:44:36+02:00', percent: 76, tests_done: 1221,
  tests_total: 1606, failed: 0, eta_s: 200, eta_h: '3 min', elapsed_h: '9 min', workers: 6, ...over,
})

const setup = async ($: any, on: any, bodyColumns: number = PROPS.bodyColumns) => {
  const clock = mock.clock(on)
  let merge: any = {}
  let tickets: any = { goals: [], loose: [], issues: {} }
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-16T10:00:00+00:00', agents: [], merge })
      : e.argv[2] === 'tickets' ? JSON.stringify(tickets) : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const mount = async (next: any, tix: any = null) => {
    merge = next
    if (tix) {
      // the tickets pull runs every 180 s (EVERY.tickets): advance past one pull so the cache holds this fixture
      tickets = tix
      await clock.advance(181_000)
      await clock.settle()
    }
    // the signature leaves `merge` out, so the pane redraws only once SHOW_EVERY (15 s) has passed: advance past it,
    // which runs the fast pulls (every 5 s) with the new merge before the pane is drawn
    await clock.advance(16_000)
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: { ...PROPS, bodyColumns } })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return tree
  }
  return { mount }
}

test('a commit holder with a ticket shows its message as the task and #N as the ticket', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount({ running: runningOf({ kind: 'commit', message: 'Hermes finances agent joins the home targets',
    issues: [29] }), waiting: [], landed: [] })
  const drawn = drawnTexts(allStrings(tree))
  // the task cell is clipped to its column ("Hermes finances a…"): the drawn text must be the message's first words
  const task = drawn.find(t => t.startsWith('Hermes'))?.replace(/…$/, '') ?? ''
  if (task.length < 'Hermes finances'.length || !'Hermes finances agent joins the home targets'.startsWith(task))
    throw new Error(`the commit's message is not drawn as the task: ${drawn}`)
  if (!drawn.includes('#29')) throw new Error(`the ticket #29 is not drawn as a cell: ${drawn}`)
})

test('a commit holder with no ticket shows its message and the word commit as the ticket', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount({ running: runningOf({ kind: 'commit', message: 'Tidy the lock file', issues: [] }), waiting: [], landed: [] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.some(t => t.includes('Tidy the lock file'))) throw new Error(`the message is not drawn as the task: ${drawn}`)
  if (!drawn.includes('commit')) throw new Error(`the word commit is not drawn as the ticket cell: ${drawn}`)
})

test('a merge holder keeps its branch as the task and its ticket (unchanged)', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount({ running: runningOf({ kind: 'merge', branch: '13-port', branches: ['13-port'], issues: [13] }),
    waiting: [], landed: [] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.some(t => t.includes('13-port'))) throw new Error(`the merge branch is not drawn as the task: ${drawn}`)
  if (!drawn.includes('#13')) throw new Error(`the merge ticket #13 is not drawn as a cell: ${drawn}`)
})

// #85: a done row (rc 0) of a ticket whose other branch still waits or runs in the merge line does not read as the
// ticket's landing; its act text says the other branch is still in the line. A ticket with no other branch keeps today's text.
const NEW = 'влита ветка, ждёт вторая'
const landedOf = (over: any) => ({ branch: 'o1074', issues: [1074], queued: '2026-10-15T17:02:00+02:00',
  landed: '2026-10-15T17:18:00+02:00', queued_to_land_s: 960, rc: 0, took_h: '16 min', ...over })
const waitingOf = (over: any) => ({ branch: 'o1074e', issues: [1074], position: 1, queued: '2026-10-15T17:33:00+02:00',
  waited_s: 60, eta_start_s: 300, eta_start_h: '5 min', ...over })
// a drawn text the column may have clipped (`…`) counts as the full text when its start matches
const isNew = (t: string) => { const s = t.replace(/…$/, ''); return s.length >= 'влита ветка'.length && NEW.startsWith(s) }
const ticketsOf = (open: number[], closed: number[]) => ({ goals: [], loose: [], issues: {
  ...Object.fromEntries(open.map(n => [String(n), { title: `ticket ${n}`, state: 'open', open: true, labels: [] }])),
  ...Object.fromEntries(closed.map(n => [String(n), { title: `ticket ${n}`, state: 'closed', open: false, labels: [] }])) } })

test('a landed branch whose ticket has a second branch waiting is not drawn as landed', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: null, waiting: [waitingOf({})], landed: [landedOf({})] })
  const drawn = drawnTexts(allStrings(tree))
  if (drawn.filter(isNew).length !== 1) throw new Error(`the done row does not read "${NEW}" once: ${drawn}`)
  if (drawn.some(t => t.startsWith('влит в'))) throw new Error(`a bare "влит в" row for a ticket with a second branch in line: ${drawn}`)
})

test('a landed branch whose ticket has a second branch running is not drawn as landed', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: runningOf({ kind: 'merge', branch: 'o1074e', branches: ['o1074e'], issues: [1074] }),
    waiting: [], landed: [landedOf({})] })
  const drawn = drawnTexts(allStrings(tree))
  if (drawn.filter(isNew).length !== 1) throw new Error(`the done row does not read "${NEW}" once: ${drawn}`)
  if (drawn.some(t => t.startsWith('влит в'))) throw new Error(`a bare "влит в" row for a ticket with a second branch in line: ${drawn}`)
})

test('a landed branch with no other branch of its ticket keeps today\'s text (open ticket says so, closed does not)', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: null, waiting: [], landed: [landedOf({}), landedOf({ branch: 'o2000', issues: [2000] })] },
    ticketsOf([1074], [2000]))
  const drawn = drawnTexts(allStrings(tree))
  if (drawn.filter(t => /^влит в \d\d:\d\d, тикет открыт$/.test(t)).length !== 1)
    throw new Error(`the open ticket's landed row does not read "влит в HH:MM, тикет открыт" once: ${drawn}`)
  if (drawn.filter(t => /^влит в \d\d:\d\d$/.test(t)).length !== 1)
    throw new Error(`the closed ticket's landed row does not read "влит в HH:MM" once: ${drawn}`)
  if (drawn.some(isNew)) throw new Error(`a landed row with no other branch in line reads "${NEW}": ${drawn}`)
})

test('two landed tickets and a waiting third: no landed row reads as part landed', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: null, waiting: [waitingOf({ branch: 'o3000', issues: [3000] })],
    landed: [landedOf({}), landedOf({ branch: 'o2000', issues: [2000] })] })
  const drawn = drawnTexts(allStrings(tree))
  if (drawn.some(isNew)) throw new Error(`a landed row of another ticket reads "${NEW}": ${drawn}`)
  if (drawn.filter(t => t.startsWith('влит')).length !== 2) throw new Error(`the two landed rows are not both drawn: ${drawn}`)
})

// #116 B2: a did-not-land row (rc != 0) reads «не влит в HH:MM — <причина>» with its duration filled as a landed row's;
// the reason words are one table in register.tsx; the letters `rc` appear nowhere on the card, in a drawn text or not.
const failedOf = (over: any) => ({ branch: 'i109', issues: [109], queued: '2026-10-17T15:52:59+02:00',
  landed: '2026-10-17T16:37:19+02:00', ended: '2026-10-17T16:37:19+02:00', queued_to_land_s: 2660, rc: 1,
  reason: 'conflict', failed: null, took_h: '44 min', ...over })
// a duration cell is filled when a drawn text is a number of minutes or hours (ru() turns took_h into «44 мин»);
// the «Готово» count and the other digits of the card do not match it
const durationsDrawn = (drawn: string[]) => drawn.filter(t => /^\d+ ?(мин|min|ч|h)$/.test(t)).length

test('a did-not-land row for a conflict reads «не влит в HH:MM — конфликт с main» with its duration', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: null, waiting: [], landed: [failedOf({})] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.some(t => /^не влит в \d\d:\d\d — конфликт с main$/.test(t)))
    throw new Error(`the conflict row does not read «не влит в HH:MM — конфликт с main»: ${drawn}`)
  if (durationsDrawn(drawn) !== 1) throw new Error(`the did-not-land row's duration column is empty: ${drawn}`)
})

test('a did-not-land red-suite row reads «— упали тесты: N» with N the failed count', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: null, waiting: [], landed: [failedOf({ branch: 'i110', issues: [110],
    reason: 'red-suite', failed: 1 })] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.some(t => /^не влит в \d\d:\d\d — упали тесты: 1$/.test(t)))
    throw new Error(`the red-suite row does not read «— упали тесты: 1»: ${drawn}`)
  if (durationsDrawn(drawn) !== 1) throw new Error(`the did-not-land row's duration column is empty: ${drawn}`)
})

test('a did-not-land target-check row reads «— запрещённые слова проекта»', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: null, waiting: [], landed: [failedOf({ branch: 'i111', issues: [111],
    reason: 'target-check' })] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.some(t => /^не влит в \d\d:\d\d — запрещённые слова проекта$/.test(t)))
    throw new Error(`the target-check row does not read «— запрещённые слова проекта»: ${drawn}`)
})

test('a did-not-land row with an unknown reason reads «не влит: <reason as written>»', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: null, waiting: [], landed: [failedOf({ branch: 'i112', issues: [112],
    reason: 'odd-stop' })] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.some(t => t === 'не влит: odd-stop')) throw new Error(`the unknown reason does not read «не влит: odd-stop»: ${drawn}`)
})

test('no card row draws the letters rc, a did-not-land row included', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: null, waiting: [], landed: [failedOf({}),
    failedOf({ branch: 'i110', issues: [110], reason: 'red-suite', failed: 1 })] })
  const leaked = allStrings(tree).filter(s => /\brc\b/.test(s))
  if (leaked.length) throw new Error(`the card shows «rc» in: ${leaked}`)
})

// #123: every reason a did-not-land merge record can write has words on the Merge card, never its code. The claude-code
// test environment has no fs, so the list is written here; tests/test_issue123_reason_words.py holds it to the reasons
// merge.py and detach.py write at test time. NOT_LANDED_WHY in register.tsx is the one table of words.
const DID_NOT_LAND_REASONS = [
  'conflict', 'core-manifest', 'cut', 'diverged', 'dirty', 'failed', 'fetch-failed', 'lock-timeout', 'merge-refused',
  'not-planned', 'nothing-to-merge', 'push-refused', 'rebase-conflict', 'red-on-base', 'red-on-branch', 'red-suite',
  'setup-error', 'shallow', 'silent-holder', 'stuck', 'suite-worktree', 'superseded', 'sync-failed', 'target-check', 'tracked-caches',
  'withdrawn', 'worktree',
]

test('every reason a did-not-land merge record can write reads as words on the card, never as its code', async ($, on) => {
  const { mount } = await setup($, on, 160)
  for (const [i, reason] of DID_NOT_LAND_REASONS.entries()) {
    const tree = await mount({ running: null, waiting: [], landed: [failedOf({ branch: `r${i}`, issues: [900 + i], reason })] })
    const drawn = drawnTexts(allStrings(tree))
    const act = drawn.filter(t => t.startsWith('не влит'))
    if (act.length !== 1 || !/^не влит в \d\d:\d\d — .+$/.test(act[0]!))
      throw new Error(`the ${reason} row does not read «не влит в HH:MM — <words>»: ${drawn}`)
    const raw = drawn.find(t => t.includes(reason))
    if (raw !== undefined) throw new Error(`the ${reason} row shows its raw code somewhere on the card: ${raw}`)
    if (drawn.some(t => t.startsWith('не влит: '))) throw new Error(`the ${reason} row reads «не влит: <code>»: ${drawn}`)
  }
})
