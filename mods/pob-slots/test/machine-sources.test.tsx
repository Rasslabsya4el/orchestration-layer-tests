import { test, mock } from 'claude-code/testing'
import { WORK_NAME, PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
// the PC card's CPU and RAM bars draw each source as its own segment with its own legend label. The seam is the
// drawn pane (the desktop surface): a `fast` pull carries the machine payload (one key per source in `cpu` and `ram`),
// the drawn tree is read back. A missing key of an older collector reads as 0.
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

// every object in the drawn tree, depth first
const each = (x: any, f: (o: any) => void) => {
  if (Array.isArray(x)) { for (const v of x) each(v, f); return }
  if (!x || typeof x !== 'object') return
  f(x)
  for (const v of Object.values(x)) each(v, f)
}
// the host's pictures are written with single quotes (the pack's `sq`): read them with double ones
const strings = (tree: any, key: string): string[] => {
  const out: string[] = []
  each(tree, o => { if (typeof o[key] === 'string') out.push(o[key].replace(/'/g, '"')) })
  return out
}
// the words a reader sees in a picture (an alt is not drawn)
const drawnTexts = (svg: string): string[] => [...svg.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1]!)
// the PC card's load picture: the one picture with a CPU bar
const pcSource = (tree: any): string => {
  const s = strings(tree, 'source').find(x => x.includes('clipPath id="clCPU"'))
  if (s === undefined) throw new Error('the PC card draws no CPU bar')
  return s
}
// one bar's segments, in drawing order: the track (the bar's own width) is the first rect of the bar's group, the rest are the parts
const bar = (svg: string, name: string) => {
  const g = new RegExp(`<g clip-path="url\\(#cl${name}\\)">([\\s\\S]*?)</g>`).exec(svg)
  if (!g) throw new Error(`no ${name} bar drawn`)
  const rects = [...g[1]!.matchAll(/<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"[^>]*fill="([^"]+)"/g)]
    .map(r => ({ x: +r[1]!, w: +r[2]!, fill: r[3]! }))
  const [track, ...segs] = rects
  if (!track) throw new Error(`the ${name} bar has no track`)
  return { BW: track.w, segs }
}

const setup = async ($: any, on: any) => {
  const clock = mock.clock(on)
  let machine: any = undefined
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-16T10:00:00+00:00', agents: [], machine })
      : e.argv[2] === 'tickets' ? '{"goals":[],"loose":[],"issues":{}}' : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const mount = async (next: any) => {
    machine = next
    // the signature leaves `machine` out, so the pane redraws only once SHOW_EVERY (15 s) has passed: advance past it,
    // which runs the fast pulls (every 5 s) with the new machine before the pane is drawn
    await clock.advance(16_000)
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: PROPS })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return tree
  }
  return { mount }
}

// the six sources of a full payload: their shares of the CPU (% of the machine, summing to cpu_total 100) and of the RAM (GB of 32)
const CPU = { work: 25, agents: 20, app: 15, tests: 10, you: 10, unmeasured: 20 }
const RAM = { work: 4, agents: 3, app: 2, tests: 1, you: 2, unmeasured: 4 }
const machine = (cpu: any, ram: any) => ({ cores: 8, cpu, cpu_total: 100, ram, ram_total: 32, ram_free: 16 })

test('the CPU and RAM bars draw each source with its own legend label (agents, Claude app, tests, other, unmeasured, work)', async ($, on) => {
  const { mount } = await setup($, on)
  const svg = pcSource(await mount(machine(CPU, RAM)))
  const texts = [...drawnTexts(svg)]
  for (const label of ['agents', 'Claude app', 'tests', 'other', 'unmeasured', WORK_NAME]) {
    if (!texts.some(t => t === label || t.startsWith(`${label} `))) throw new Error(`the legend label "${label}" is not drawn: ${texts}`)
  }
})

test('the CPU bar draws six segments, each its share of the bar, that together fill the bar (100 %)', async ($, on) => {
  const { mount } = await setup($, on)
  const { BW, segs } = bar(pcSource(await mount(machine(CPU, RAM))), 'CPU')
  const shares = [CPU.work, CPU.agents, CPU.app, CPU.tests, CPU.you, CPU.unmeasured]
  if (segs.length !== shares.length) throw new Error(`the CPU bar draws ${segs.length} segments, expected ${shares.length}`)
  segs.forEach((s, i) => {
    if (Math.abs(s.w - (shares[i]! / 100) * BW) > 0.2) throw new Error(`segment ${i} is ${s.w} px, its share of ${BW} px is ${(shares[i]! / 100) * BW}`)
  })
  const sum = segs.reduce((a, s) => a + s.w, 0)
  if (Math.abs(sum - BW) > 0.5) throw new Error(`the CPU segments sum to ${sum} px, the bar is ${BW} px`)
  if (new Set(segs.map(s => s.fill)).size !== shares.length) throw new Error(`two sources share a colour: ${segs.map(s => s.fill)}`)
})

test('the RAM bar never exceeds its width: a payload whose parts are half the total draws half the bar', async ($, on) => {
  const { mount } = await setup($, on)
  const { BW, segs } = bar(pcSource(await mount(machine(CPU, RAM))), 'RAM')
  const sum = segs.reduce((a, s) => a + s.w, 0)
  if (sum > BW + 0.5) throw new Error(`the RAM segments sum to ${sum} px, over the bar's ${BW} px`)
  if (Math.abs(sum - BW * (16 / 32)) > 0.5) throw new Error(`the RAM segments sum to ${sum} px, half of ${BW} px is ${BW / 2}`)
})

test('a payload from an older collector (no app, tests, unmeasured) draws its own parts and no label for the missing ones', async ($, on) => {
  const { mount } = await setup($, on)
  const svg = pcSource(await mount(machine({ work: 60, agents: 30, you: 10 }, { work: 8, agents: 4, you: 4 })))
  const { BW, segs } = bar(svg, 'CPU')
  const sum = segs.reduce((a, s) => a + s.w, 0)
  if (Math.abs(sum - BW) > 0.5) throw new Error(`the CPU segments sum to ${sum} px, the bar is ${BW} px`)
  const texts = drawnTexts(svg)
  for (const label of ['Claude app', 'tests', 'unmeasured']) {
    if (texts.some(t => t === label || t.startsWith(`${label} `))) throw new Error(`a missing key draws "${label}": ${texts}`)
  }
})
