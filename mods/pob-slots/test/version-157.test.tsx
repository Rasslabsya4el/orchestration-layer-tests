import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
// #157: the pane draws `v<count>` (the orchestration layer's build number) from the `fast` payload key `version`.
// The host window title is the static TITLE and the body has no title row, so the number stands beside the footer's
// «обновлено HH:MM». A null / absent version draws nothing. The seam is the drawn pane (desktop surface).
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

const allStrings = (x: any, out: string[] = []): string[] => {
  if (typeof x === 'string') out.push(x)
  else if (Array.isArray(x)) for (const v of x) allStrings(v, out)
  else if (x && typeof x === 'object') for (const v of Object.values(x)) allStrings(v, out)
  return out
}
// what a reader sees: plain strings, and the words inside the drawn pictures' <text> cells
const drawnTexts = (all: string[]): string[] =>
  all.flatMap(s => s.includes('<') ? [...s.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1]!) : [s])

const drawnWith = async ($: any, on: any, fastExtra: Record<string, unknown>) => {
  const clock = mock.clock(on)
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-16T10:00:00', agents: [], ...fastExtra })
      : e.argv[2] === 'tickets' ? JSON.stringify({ goals: [], loose: [], issues: {} }) : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  await clock.advance(16_000)
  await clock.settle()
  const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: PROPS })
  await clock.settle()
  const tree = await ui.drawn()
  await ui.unmount()
  return drawnTexts(allStrings(tree))
}

test('fast.version 7 is drawn as v7 beside the footer time', async ($, on) => {
  const drawn = await drawnWith($, on, { version: 7 })
  if (!drawn.some(t => /\bv7\b/.test(t))) throw new Error(`v7 is not drawn: ${drawn}`)
  if (!drawn.some(t => t.includes('обновлено'))) throw new Error(`the footer is gone: ${drawn}`)
})

test('fast.version null draws no version', async ($, on) => {
  const drawn = await drawnWith($, on, { version: null })
  if (drawn.some(t => /\bv\d/.test(t))) throw new Error(`a version is drawn for null: ${drawn}`)
  if (!drawn.some(t => t.includes('обновлено'))) throw new Error(`the footer is gone: ${drawn}`)
})

test('fast without a version key draws no version', async ($, on) => {
  const drawn = await drawnWith($, on, {})
  if (drawn.some(t => /\bv\d/.test(t))) throw new Error(`a version is drawn when absent: ${drawn}`)
})
