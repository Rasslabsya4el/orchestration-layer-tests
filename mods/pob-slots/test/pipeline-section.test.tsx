import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
import { sizeOf, COST, TARGET } from '../hooks/budget'

// #6 unit 2: the Progress tab's «Пайплайн» section, above the two count tables. The seam is the drawn pane (the desktop
// surface, whose rows are pictures): the `fast` pull carries `pipeline` (the collector's rows, already sorted), the
// Progress tab is opened, and the drawn tree is read back. A row's strip is read from the picture's own markup
// (`data-stage` of its group, `data-st` of each station): the stations before the stage are passed, the stage's is
// current, the later ones are hollow.
// 123 columns = the grid's design width (register.tsx COLS: "drawn for the 123-column pane"): at 60 columns the act column is 40 px and the words are cut to «…»
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
// one row of the collector's `pipeline` (the fixed shape, #6)
const row = (over: any) => ({
  number: 0, title: '', stage: 'ready', failed: false, workers: 0, strong: 0, researchers: 0,
  merge_position: null, merge_percent: null, started_at: ago(120), stage_since: ago(40), ...over,
})
// five rows, one per stage case, in the stage order: ready; ORCH; work with 2 workers (1 worker-strong) and 1 researcher;
// merge waiting second; merge at 40 %
const FIXTURE = [
  row({ number: 101, title: 'Ready ticket', stage: 'ready' }),
  row({ number: 102, title: 'Orch ticket', stage: 'orch' }),
  row({ number: 103, title: 'Work ticket', stage: 'work', workers: 2, strong: 1, researchers: 1 }),
  row({ number: 104, title: 'Merge waiting ticket', stage: 'merge', merge_position: 2 }),
  row({ number: 105, title: 'Merge running ticket', stage: 'merge', merge_percent: 40 }),
]
// the stage each row sits at: the index of its station among ready · ORCH · work · merge · done
const STAGE_INDEX: Record<string, number> = { ready: 0, orch: 1, work: 2, merge: 3, done: 4 }
// worst case, one kind of row at a time: distinct rows, each with the widest title a row can carry (a 200-char title of a 2-byte letter),
// the widest word of its kind (a work line with 9 workers, 3 of them strong, and 4 researchers; a merge at 100 %; a failed line), and the
// longest durations («47 ч 31 мин»)
const WORST_KINDS: { name: string; over: any }[] = [
  { name: 'ready', over: { stage: 'ready' } },
  { name: 'orch', over: { stage: 'orch' } },
  { name: 'work', over: { stage: 'work', workers: 9, strong: 3, researchers: 4 } },
  { name: 'merge 100 %', over: { stage: 'merge', merge_percent: 100 } },
  { name: 'failed work', over: { stage: 'work', failed: true, workers: 9, strong: 3, researchers: 4 } },
]
const worst = (n: number, over: any) => Array.from({ length: n }, (_, i) => row({
  number: 900 + i, title: `${i} ${'Ш'.repeat(200)}`, ...over, started_at: ago(2851), stage_since: ago(2851),
}))
// the same rows, the five kinds in turn (a pane's real mix: the shapes the rows share differ by kind)
const worstMix = (n: number) => Array.from({ length: n }, (_, i) => row({
  number: 900 + i, title: `${i} ${'Ш'.repeat(200)}`, ...WORST_KINDS[i % WORST_KINDS.length]!.over, started_at: ago(2851), stage_since: ago(2851),
}))

const setup = async ($: any, on: any, surface: 'desktop' | 'terminal' = 'desktop') => {
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
  // the Progress tab drawn with the given `pipeline` (absent when `next` is undefined)
  const progress = async (next: any) => {
    pipeline = next
    await clock.advance(6_000) // one fast tick (5 s): the pull runs with the new pipeline before the pane is drawn
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface, component: 'Pane', requestId: PANE, props: PROPS })
    await clock.settle()
    await ui.press({ key: 'tab-progress' })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return tree
  }
  return { progress }
}

test('each pipeline row is drawn at its stage, with its stage word and both times', async ($, on) => {
  const { progress } = await setup($, on)
  const tree = await progress(FIXTURE)
  const all = allStrings(tree)
  const texts = drawnTexts(all)
  // the section is drawn, and the rows come in the fixture's order
  if (!all.some(s => s.includes('Пайплайн'))) throw new Error(`the «Пайплайн» section is not drawn: ${texts}`)
  const titles = FIXTURE.map(r => r.title)
  const at = titles.map(t => texts.indexOf(t))
  if (at.some(i => i < 0) || at.some((v, k) => k > 0 && v <= at[k - 1]!)) throw new Error(`the rows are not drawn in order: ${texts}`)
  // each row's words: between its title and the next row's title (or the section's end)
  const words = ['ждёт ORCH', 'ORCH режет', '2 worker (1 strong) · 1 researcher', 'ждёт merge, 2-й', 'merge: suite 40 %']
  for (let k = 0; k < FIXTURE.length; k++) {
    const end = k + 1 < at.length ? at[k + 1]! : texts.length
    const mine = texts.slice(at[k]!, end)
    const r = FIXTURE[k]!
    if (!mine.includes(words[k]!)) throw new Error(`row #${r.number} (${r.stage}) lacks its word «${words[k]}»: ${mine}`)
    if (!mine.includes('2 ч')) throw new Error(`row #${r.number} lacks the time since its start «2 ч»: ${mine}`)
    if (!mine.includes('40 мин')) throw new Error(`row #${r.number} lacks the time at its stage «40 мин»: ${mine}`)
  }
  // the strip: one group per row, in row order, its stations passed / current / later at the row's stage
  const groups = all.flatMap(s => [...norm(s).matchAll(/<g data-stage="([a-z]+)">([\s\S]*?)<\/g>/g)].map(m => ({ stage: m[1]!, body: m[2]! })))
  if (groups.length !== FIXTURE.length) throw new Error(`${groups.length} strips drawn for ${FIXTURE.length} rows`)
  groups.forEach((g, k) => {
    const r = FIXTURE[k]!
    if (g.stage !== r.stage) throw new Error(`row #${r.number} drawn at stage ${g.stage}, its stage is ${r.stage}`)
    const cur = STAGE_INDEX[r.stage]!
    const want = [0, 1, 2, 3, 4].map(j => (j < cur ? 'passed' : j === cur ? 'current' : 'later'))
    const got = [...g.body.matchAll(/data-st="(\w+)"/g)].map(m => m[1])
    if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`row #${r.number} strip ${JSON.stringify(got)}, want ${JSON.stringify(want)}`)
  })
})

test('no pipeline key, or an empty list, draws no section and the count tables as before', async ($, on) => {
  const { progress } = await setup($, on)
  for (const next of [undefined, []]) {
    const tree = await progress(next)
    const all = allStrings(tree)
    const texts = drawnTexts(all)
    if (all.some(s => s.includes('Пайплайн') || /data-stage=/.test(s))) throw new Error(`a section is drawn with pipeline ${JSON.stringify(next)}: ${texts}`)
    if (!all.some(s => s.includes('По тикетам')) || !all.some(s => s.includes('Остальное'))) throw new Error(`the count tables are not drawn: ${texts}`)
  }
})

test('terminal: the section is drawn with the fixture and the tab draws without error', async ($, on) => {
  const { progress } = await setup($, on, 'terminal')
  const tree = await progress(FIXTURE)
  const all = allStrings(tree)
  if (!all.some(s => s.includes('Пайплайн'))) throw new Error(`terminal: the «Пайплайн» section is not drawn: ${drawnTexts(all)}`)
  if (!all.some(s => s.includes('Ready ticket'))) throw new Error(`terminal: the first row is not drawn: ${all.slice(0, 40)}`)
})

test('worst case: the pipeline rows stay within the charges and the pane stays under TARGET', async ($, on) => {
  const { progress } = await setup($, on)
  const base = await progress(undefined)
  // one line, and N lines, of each kind and of the mix: the drawn growth never passes the charges (`pipeExtra` once, `pipeLine` a line)
  const check = (name: string, d1: number, dN: number, n: number) => {
    if (d1 > COST.pipeLine + COST.pipeExtra) throw new Error(`${name}: one line draws ${d1} characters over its charge ${COST.pipeLine + COST.pipeExtra}`)
    if (dN > n * COST.pipeLine + COST.pipeExtra) throw new Error(`${name}: ${n} lines draw ${dN} characters over their charge ${n * COST.pipeLine + COST.pipeExtra}`)
  }
  const N = 6
  for (const k of WORST_KINDS) {
    const one = await progress(worst(1, k.over))
    const many = await progress(worst(N, k.over))
    check(k.name, sizeOf(one) - sizeOf(base), sizeOf(many) - sizeOf(base), N)
  }
  const M = 10
  const m1 = await progress(worstMix(1)), mM = await progress(worstMix(M))
  check('mix', sizeOf(m1) - sizeOf(base), sizeOf(mM) - sizeOf(base), M)
  // a pane of 60 such lines pages the section under the budget: the drawn tree stays under TARGET
  const big = await progress(worstMix(60))
  if (sizeOf(big) > TARGET) throw new Error(`a pane of 60 pipeline lines draws ${sizeOf(big)} characters over TARGET ${TARGET}`)
})

// #154 row 4: a line at stage `accept` (after `merge`, the landed ticket that is still open) draws five dots, the fifth
// active (the first four passed), and its status word is «приёмка» with its icon; its time at the stage is its stage_since.
test('a row at stage accept draws five dots with the fifth active, and the word приёмка', async ($, on) => {
  const { progress } = await setup($, on)
  const tree = await progress([row({ number: 135, title: 'Accept ticket', stage: 'accept', stage_since: ago(40) })])
  const all = allStrings(tree)
  const texts = drawnTexts(all)
  if (!all.some(s => s.includes('Пайплайн'))) throw new Error(`the «Пайплайн» section is not drawn: ${texts}`)
  const groups = all.flatMap(s => [...norm(s).matchAll(/<g data-stage="([a-z]+)">([\s\S]*?)<\/g>/g)].map(m => ({ stage: m[1]!, body: m[2]! })))
  if (groups.length !== 1) throw new Error(`${groups.length} strips drawn for 1 row`)
  if (groups[0]!.stage !== 'accept') throw new Error(`the row is drawn at stage ${groups[0]!.stage}, want accept`)
  const got = [...groups[0]!.body.matchAll(/data-st="(\w+)"/g)].map(m => m[1])
  const want = ['passed', 'passed', 'passed', 'passed', 'current']
  if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`accept strip ${JSON.stringify(got)}, want ${JSON.stringify(want)}`)
  const at = texts.indexOf('Accept ticket')
  const mine = texts.slice(at, texts.length)
  if (!mine.includes('приёмка')) throw new Error(`accept row lacks its word «приёмка»: ${mine}`)
  if (!mine.includes('40 мин')) throw new Error(`accept row lacks the time at its stage «40 мин»: ${mine}`)
})
