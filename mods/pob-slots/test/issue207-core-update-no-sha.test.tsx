import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
// #207: a core update whose home head is not read yet carries no `sha` (unit 1: the python payload omits it); the Merge
// card draws its row (waiting or running) as `обновление ядра` with no `?` and no trailing space, and a row with a sha
// as `обновление ядра <sha8>` as before. The seam is the drawn pane, as issue203-core-update-row.test.tsx.
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

const setup = async ($: any, on: any, bodyColumns: number = PROPS.bodyColumns) => {
  const clock = mock.clock(on)
  let merge: any = {}
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-16T10:00:00+00:00', agents: [], merge })
      : e.argv[2] === 'tickets' ? JSON.stringify({ goals: [], loose: [], issues: {} }) : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const mount = async (next: any) => {
    merge = next
    // the signature leaves `merge` out, so the pane redraws only once SHOW_EVERY (15 s) has passed
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

// the payload the collector gives while the core update has not read the home head yet: no `sha` key at all
const runningNoSha = (over: any = {}) => ({
  kind: 'core-update', branches: [], issues: [], phase: 'queue', started: '2026-10-16T13:44:36+02:00',
  percent: null, tests_done: null, tests_total: null, failed: 0, eta_s: null, elapsed_h: '3 min', ...over,
})
const waitingNoSha = (over: any = {}) => ({
  kind: 'core-update', position: 1, queued: '2026-10-16T13:50:00+02:00',
  waited_s: 60, eta_start_s: 300, waited_h: '1 min', eta_start_h: '5 min', ...over,
})

test('a waiting and a running core update without sha draw two rows named обновление ядра, no ?', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: runningNoSha({}), waiting: [waitingNoSha({})], landed: [] })
  const drawn = drawnTexts(allStrings(tree))
  const named = drawn.filter(t => t === 'обновление ядра')
  if (named.length !== 2) throw new Error(`the core update without sha is not drawn as two rows named «обновление ядра» (waiting and running): ${drawn}`)
  if (drawn.some(t => t.includes('?'))) throw new Error(`a core update row without sha draws the placeholder «?»: ${drawn}`)
  if (drawn.some(t => t !== t.trimEnd())) throw new Error(`a core update row without sha draws a trailing space: ${JSON.stringify(drawn)}`)
})

test('a running core update without sha alone draws its row named обновление ядра', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: runningNoSha({}), waiting: [], landed: [] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.includes('обновление ядра')) throw new Error(`the running core update without sha is not drawn as «обновление ядра»: ${drawn}`)
})

test('a waiting core update with sha abcd1234 draws обновление ядра abcd1234, a running one without sha draws обновление ядра', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: runningNoSha({}), waiting: [waitingNoSha({ sha: 'abcd1234' })], landed: [] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.includes('обновление ядра abcd1234')) throw new Error(`the waiting core update with sha is not drawn as «обновление ядра abcd1234»: ${drawn}`)
  if (!drawn.includes('обновление ядра')) throw new Error(`the running core update without sha is not drawn as «обновление ядра»: ${drawn}`)
})
