import { test, mock } from 'claude-code/testing'
import { fastEveryMs, COMMAND } from '../hooks/register'
import { REPO } from '../hooks/project'
// #139: the monitor's fast pull interval comes from `monitor.refresh_s` (seconds) of config/orchestration.json.
// The rule is a pure helper (fastEveryMs): its number cases run without the clock.
const cases: [string, string | null, number][] = [
  ['no config file', null, 5000],
  ['refresh_s 2', '{"monitor":{"refresh_s":2}}', 2000],
  ['refresh_s 0.2 is clamped up to 1 s', '{"monitor":{"refresh_s":0.2}}', 1000],
  ['refresh_s 120 is clamped down to 60 s', '{"monitor":{"refresh_s":120}}', 60000],
  ['refresh_s not a number', '{"monitor":{"refresh_s":"x"}}', 5000],
  ['invalid JSON', '{"monitor":', 5000],
  ['no monitor key', '{}', 5000],
]

for (const [name, text, want] of cases) {
  test(`fastEveryMs: ${name} → ${want} ms`, async () => {
    const got = fastEveryMs(text)
    if (got !== want) throw new Error(`fastEveryMs(${JSON.stringify(text)}) = ${got}, want ${want}`)
  })
}

// The pane's clock: with refresh_s 2 the fast pull runs every 2 s, not every 5 s. The harness does not expose the
// registered `every` calls, so the interval is read from the fast pulls the collector receives (process.run argv[2] 'fast').
test('the pane pulls fast every refresh_s (2 s from the config), not every 5 s', async ($, on) => {
  const clock = mock.clock(on)
  const cfg = '{"monitor":{"refresh_s":2}}'
  let fast = 0
  on('process.run', async (_$: any, e: any) => {
    if (e.argv[2] === 'fast') fast++
    return { value: { exitCode: 0, stdout: e.argv[2] === 'tickets' ? '{"goals":[],"loose":[],"issues":{}}' : '{}',
      stderr: '', isStdoutTruncated: false } } as any
  })
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  // only the orchestration config exists; every other path is absent, and no write reaches the disk
  on('fs.exists', async (_$: any, e: any) => ({ value: /orchestration\.json$/.test(String(e.path)) }) as any)
  on('fs.read', async (_$: any, e: any) => ({ value: /orchestration\.json$/.test(String(e.path)) ? cfg : '' }) as any)
  on('fs.write', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const start = fast
  if (!(start >= 1)) throw new Error(`no immediate fast pull (${start})`)
  await clock.advance(1_900)
  await clock.settle()
  if (fast !== start) throw new Error(`a fast pull ran before 2 s: ${fast - start} extra`)
  await clock.advance(200)
  await clock.settle()
  if (fast - start !== 1) throw new Error(`expected one fast pull at 2 s, got ${fast - start}`)
})
