import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
import { pipeLeft, pipeWord, UNKNOWN_DASH } from '../hooks/kit'

// #171 unit 2: a pipeline row waiting after its merge (`wait`: the parked ticket's reason and date) or sent back by the brain (`returned`)
// draws its own word; the right column shows the time left to the wait's date. The seam is the drawn pane, as in pipeline-section.test.tsx;
// `pipeLeft` and `pipeWord` are read on their own with fixed dates.
// 123 columns = the grid's design width (register.tsx COLS)
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 123, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

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

const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
const HOUR = 3_600_000, DAY = 24 * HOUR
// one row of the collector's `pipeline` (the fixed shape, #6); `wait` and `returned` are absent unless given (old payloads)
const row = (over: any) => ({
  number: 0, title: '', stage: 'ready', failed: false, workers: 0, strong: 0, researchers: 0,
  merge_position: null, merge_percent: null, started_at: ago(120), stage_since: ago(40), ...over,
})
// a local wall-clock instant, as the collector's ISO stamps without an offset are read
const at = (y: number, mo: number, d: number, h: number, mi: number) => new Date(y, mo - 1, d, h, mi).getTime()
const UNTIL = '2026-10-24T19:30:00'

const setup = async ($: any, on: any) => {
  const clock = mock.clock(on)
  let pipeline: any = undefined
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-17T10:00:00+00:00', agents: [], merge: {}, ...(pipeline === undefined ? {} : { pipeline }) })
      : e.argv[2] === 'tickets' ? '{"goals":[],"loose":[],"issues":{}}' : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  // the Progress tab drawn with the given `pipeline`
  const progress = async (next: any) => {
    pipeline = next
    await clock.advance(6_000) // one fast tick (5 s): the pull runs with the new pipeline before the pane is drawn
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: PROPS })
    await clock.settle()
    await ui.press({ key: 'tab-progress' })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return tree
  }
  return { progress }
}

// a parked ticket after its merge: stage accept, wait with its reason and a date 6 d 10 h 30 min ahead of the render's clock
test('a waiting row draws ждёт with its reason and the time left, not приёмка', async ($, on) => {
  const { progress } = await setup($, on)
  const until = new Date(NOW + 6 * DAY + 10 * HOUR + 30 * 60_000).toISOString()
  const tree = await progress([row({ number: 124, title: 'Parked ticket', stage: 'accept', wait: { reason: 'until 2026-10-24 19:30', until } })])
  const all = allStrings(tree)
  const texts = drawnTexts(all)
  const at0 = texts.indexOf('Parked ticket')
  if (at0 < 0) throw new Error(`the parked row is not drawn: ${texts}`)
  const mine = texts.slice(at0).join('\n')
  if (!mine.includes('ждёт')) throw new Error(`the waiting row lacks its word «ждёт»: ${mine}`)
  if (!mine.includes('через 6 д 10 ч')) throw new Error(`the waiting row lacks the time left «через 6 д 10 ч»: ${mine}`)
  if (mine.includes('приёмка')) throw new Error(`the waiting row is drawn as «приёмка»: ${mine}`)
})

test('a returned row draws возвращён', async ($, on) => {
  const { progress } = await setup($, on)
  const tree = await progress([row({ number: 136, title: 'Returned ticket', stage: 'ready', returned: true })])
  const texts = drawnTexts(allStrings(tree))
  const at0 = texts.indexOf('Returned ticket')
  if (at0 < 0) throw new Error(`the returned row is not drawn: ${texts}`)
  const mine = texts.slice(at0).join('\n')
  if (!mine.includes('возвращён')) throw new Error(`the returned row lacks its word «возвращён»: ${mine}`)
})

test('an accept row without wait or returned still draws приёмка', async ($, on) => {
  const { progress } = await setup($, on)
  const tree = await progress([row({ number: 135, title: 'Accept ticket', stage: 'accept', wait: null, returned: false })])
  const texts = drawnTexts(allStrings(tree))
  const at0 = texts.indexOf('Accept ticket')
  if (at0 < 0) throw new Error(`the accept row is not drawn: ${texts}`)
  const mine = texts.slice(at0).join('\n')
  if (!mine.includes('приёмка')) throw new Error(`the accept row lacks its word «приёмка»: ${mine}`)
})

test('pipeLeft: the time left in the human form, past the date «срок вышел», no date a dash', () => {
  const cases: [string | null, number, string][] = [
    [UNTIL, at(2026, 10, 18, 9, 0), 'через 6 д 10 ч'],
    [UNTIL, at(2026, 10, 24, 14, 10), 'через 5 ч 20 мин'],
    [UNTIL, at(2026, 10, 24, 19, 0), 'через 30 мин'],
    [UNTIL, at(2026, 10, 22, 19, 30), 'через 2 д'],
    [UNTIL, at(2026, 10, 24, 20, 0), 'срок вышел'],
    [null, at(2026, 10, 18, 9, 0), UNKNOWN_DASH],
  ]
  for (const [iso, now, want] of cases) {
    const got = pipeLeft(iso, now)
    if (got !== want) throw new Error(`pipeLeft(${iso}, ${new Date(now).toISOString()}) = «${got}», want «${want}»`)
  }
})

test('pipeWord: a wait shows its reason cut to 40 characters, a failed line stays упало, a returned line says so', () => {
  const long = pipeWord(row({ stage: 'accept', wait: { reason: 'x'.repeat(60), until: null } }) as any)
  if (!long.t.startsWith('ждёт: ')) throw new Error(`wait word «${long.t}»`)
  const reason = long.t.slice('ждёт: '.length)
  if ([...reason].length > 40 || !reason.endsWith('…')) throw new Error(`reason not cut to 40 with an ellipsis: «${reason}»`)
  if (long.icon !== 'waiting.octicons') throw new Error(`wait icon ${long.icon}`)
  const failed = pipeWord(row({ failed: true, wait: { reason: 'until x', until: null } }) as any)
  if (failed.t !== 'упало') throw new Error(`a failed waiting line reads «${failed.t}»`)
  const back = pipeWord(row({ stage: 'ready', returned: true }) as any)
  if (back.t !== 'возвращён') throw new Error(`returned word «${back.t}»`)
  const plain = pipeWord(row({ stage: 'accept' }) as any)
  if (plain.t !== 'приёмка') throw new Error(`accept word «${plain.t}»`)
})
