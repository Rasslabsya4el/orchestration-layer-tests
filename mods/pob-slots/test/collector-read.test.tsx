import { test, mock } from 'claude-code/testing'
import { fastFromCollector, COMMAND } from '../hooks/register'
import { PANE, REPO } from '../hooks/project'
// #142 u2: the monitor's fast pull reads the file an always-running collector writes (COLLECTOR_FAST) instead of starting
// `collect.py fast`. The file is { at_ms, refresh_s, data }: fresh while now - at_ms <= 3 x refresh_s (refresh_s 5 when absent).
// fastFromCollector is the pure rule (no clock); the pane cases drive the pull through a fake `$` and the mocked clock.

const NOW = 1_000_000_000_000
const data = { at: '2026-10-17T15:00:00+00:00', agents: [] }
const file = (at_ms: number, refresh_s?: number, d: unknown = data) =>
  JSON.stringify({ at_ms, ...(refresh_s == null ? {} : { refresh_s }), data: d })

test('fastFromCollector: a fresh file gives its data', async () => {
  const got = fastFromCollector(file(NOW - 4_000, 5), NOW)
  if (JSON.stringify(got) !== JSON.stringify(data)) throw new Error(`got ${JSON.stringify(got)}, want the data`)
})

test('fastFromCollector: exactly 3 x refresh_s old is still fresh; one ms older is stale', async () => {
  if (fastFromCollector(file(NOW - 15_000, 5), NOW) == null) throw new Error('15 s old with refresh_s 5 is stale, want fresh')
  if (fastFromCollector(file(NOW - 15_001, 5), NOW) !== null) throw new Error('15.001 s old with refresh_s 5 is fresh, want null')
})

test('fastFromCollector: a file without refresh_s counts it as 5 s (stale past 15 s)', async () => {
  if (fastFromCollector(file(NOW - 16_000), NOW) !== null) throw new Error('16 s old without refresh_s is fresh, want null')
  if (fastFromCollector(file(NOW - 14_000), NOW) == null) throw new Error('14 s old without refresh_s is stale, want fresh')
})

test('fastFromCollector: a missing, unparsable or data-less file gives null', async () => {
  const cases: [string, string | null][] = [
    ['no file', null],
    ['invalid JSON', '{"at_ms":'],
    ['no data key', JSON.stringify({ at_ms: NOW, refresh_s: 5 })],
    ['data is not an object', file(NOW, 5, 'x')],
  ]
  for (const [name, text] of cases) {
    if (fastFromCollector(text, NOW) !== null) throw new Error(`${name}: want null`)
  }
})

// every string the drawn tree holds: a native Text's children, and the text inside an svg's markup
const shownLines = (tree: any): string[] => {
  const out: string[] = []
  const walk = (x: any) => {
    if (typeof x === 'string') { out.push(x); return }
    if (Array.isArray(x)) { for (const v of x) walk(v); return }
    if (!x || typeof x !== 'object') return
    if (typeof x.source === 'string') out.push(...[...x.source.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1]!))
    for (const v of Object.values(x)) walk(v)
  }
  walk(tree)
  return out
}
const FLAG = 'collector off: pulling directly'
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

// The fake world: the collector file is fresh or stale as `mode` says; every other path is absent; no write reaches the disk.
const world = (on: any) => {
  const w = { mode: 'fresh' as 'fresh' | 'stale', fastRuns: 0, fileReads: 0 }
  on('process.run', async (_$: any, e: any) => {
    if (e.argv[2] === 'fast') w.fastRuns++
    // the direct pull prints the same payload the collector's file holds (the agents card draws from its `agents` key)
    return { value: { exitCode: 0, stdout: e.argv[2] === 'tickets' ? '{"goals":[],"loose":[],"issues":{}}' : JSON.stringify(data),
      stderr: '', isStdoutTruncated: false } } as any
  })
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('fs.exists', async (_$: any, e: any) => ({ value: /collector-fast\.json$/.test(String(e.path)) }) as any)
  on('fs.read', async (_$: any, e: any) => {
    if (!/collector-fast\.json$/.test(String(e.path))) return { value: '' } as any
    w.fileReads++
    // the collector writes the file at each of its own ticks: fresh = written now, stale = written 16 s ago (refresh_s 5)
    return { value: file(w.mode === 'fresh' ? Date.now() : Date.now() - 16_000, 5) } as any
  })
  on('fs.write', async () => ({ value: undefined }) as any)
  return w
}

// row 1: a fresh file over 10 intervals of the default 5 s: no `collect.py fast` run, the file is read at every pull.
for (const pane of [1, 2, 3, 4]) {
  test(`pane ${pane}: a fresh collector file, 10 intervals: no process.run for fast`, async ($, on) => {
    const clock = mock.clock(on)
    const w = world(on)
    on('session.cwd', async () => ({ value: REPO }) as any)
    void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
    await clock.settle()
    for (let i = 0; i < 10; i++) { await clock.advance(5_000); await clock.settle() }
    if (w.fastRuns !== 0) throw new Error(`fast process runs: ${w.fastRuns}, want 0`)
    if (w.fileReads < 10) throw new Error(`the collector file was read ${w.fileReads} times, want at least 10 (the pane never pulled)`)
  })
}

// row 2: a stale file: one `collect.py fast` per pull and the fallback line; the next fresh read clears the line, no run.
test('a stale collector file: one fast process run per pull and the fallback line; a fresh read clears it', async ($, on) => {
  const clock = mock.clock(on)
  const w = world(on)
  w.mode = 'stale'
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const ui = await ($ as any).ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: PROPS })
  await clock.settle()
  if (w.fastRuns !== 1) throw new Error(`the first pull ran fast ${w.fastRuns} times, want 1`)
  for (let i = 0; i < 3; i++) { await clock.advance(5_000); await clock.settle() }
  if (w.fastRuns !== 4) throw new Error(`3 stale pulls ran fast ${w.fastRuns - 1} times, want exactly 3`)
  if (!shownLines(await ui.drawn()).includes(FLAG)) throw new Error(`the fallback line «${FLAG}» is not drawn`)

  w.mode = 'fresh'
  await clock.advance(5_000)
  await clock.settle()
  if (w.fastRuns !== 4) throw new Error(`a fresh read ran fast again: ${w.fastRuns}, want 4`)
  if (shownLines(await ui.drawn()).includes(FLAG)) throw new Error('the fallback line is still drawn after a fresh read')
})

