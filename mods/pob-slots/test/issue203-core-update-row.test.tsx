import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
// #203: the Merge card draws a core update holding or waiting in the merge line as its own rows, named
// `обновление ядра <sha8>`: one waiting row (kind "core-update" in `waiting`) and one running row (`running.kind`
// "core-update"), no ticket chip. The seam is the drawn pane (as merge-card.test.tsx): the `fast` pull carries
// `merge` (what `merge-status --json` gives the collector, human durations included), the drawn tree is read back.
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

// the human durations the collector adds (merge_shape's eta_h / elapsed_h / waited_h / eta_start_h)
const runningCore = (over: any) => ({
  kind: 'core-update', sha: 'abcd1234', branches: [], issues: [], phase: 'queue', started: '2026-10-16T13:44:36+02:00',
  percent: null, tests_done: null, tests_total: null, failed: 0, eta_s: null, elapsed_h: '3 min', ...over,
})
const waitingCore = (over: any) => ({
  kind: 'core-update', sha: 'abcd1234', position: 1, queued: '2026-10-16T13:50:00+02:00',
  waited_s: 60, eta_start_s: 300, waited_h: '1 min', eta_start_h: '5 min', ...over,
})
const NAME = 'обновление ядра abcd1234'

test('a waiting and a running core update draw two rows named обновление ядра <sha8>, no ticket chip', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: runningCore({}), waiting: [waitingCore({})], landed: [] })
  const drawn = drawnTexts(allStrings(tree))
  const named = drawn.filter(t => t === NAME)
  if (named.length !== 2) throw new Error(`the core update is not drawn as two rows named «${NAME}» (waiting and running): ${drawn}`)
  if (drawn.some(t => /^#\d+/.test(t))) throw new Error(`a core update row draws a ticket chip: ${drawn}`)
  if (drawn.includes('?')) throw new Error(`the running core update is drawn with the placeholder «?»: ${drawn}`)
})

test('a running core update alone draws its row named обновление ядра <sha8>', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: runningCore({}), waiting: [], landed: [] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.includes(NAME)) throw new Error(`the running core update is not drawn as «${NAME}»: ${drawn}`)
})

test('a waiting core update alone draws its waiting row named обновление ядра <sha8>', async ($, on) => {
  const { mount } = await setup($, on, 160)
  const tree = await mount({ running: null, waiting: [waitingCore({})], landed: [] })
  const drawn = drawnTexts(allStrings(tree))
  if (!drawn.includes(NAME)) throw new Error(`the waiting core update is not drawn as «${NAME}»: ${drawn}`)
})
