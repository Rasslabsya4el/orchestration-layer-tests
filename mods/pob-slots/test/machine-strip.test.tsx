import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
import { K, ROLE } from '../hooks/tokens'

// #114 u2: the PC card's 10-minute CPU strip draws the same sources as the CPU bar: one part per source, the same
// colour, the same share. The seam is the drawn pane (as machine-sources.test.tsx): a `fast` pull carries the machine
// payload, the drawn tree is read back. The strip is the stacked polygons under the label `CPU, 10 мин`; each polygon's
// height is its source's share of the strip's 44-px height (kit.tsx `machineSvg`, H = 44).
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }
const STRIP_H = 44

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
// the strip's parts, in drawing order: each stacked polygon is `top` (the newest bucket's top edge first, then the older
// buckets) followed by `bottom` (reversed), so the newest bucket is top[n-1] and bottom[0]; its height is the part's share
const strip = (svg: string) => [...svg.matchAll(/<polygon points="([^"]+)" fill="([^"]+)"/g)].map(m => {
  const pts = m[1]!.trim().split(' ').map(p => p.split(',').map(Number) as [number, number])
  const n = pts.length / 2
  const top = pts[n - 1]!
  const bottom = pts[n]!
  return { fill: m[2]!, share: (bottom[1] - top[1]) / STRIP_H * 100 }
})

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
// the strip needs history: a pull's sample is folded in only when the payload changes (the signature), and a strip of
// one bucket is not drawn (`собираю…`). Three payloads with the same CPU shares and different RAM give three folds, so
// the strip draws three buckets, each of the one CPU payload (the newest bucket is the drawn sample).
const RAM_STEPS = [{ ...RAM, work: 4 }, { ...RAM, work: 5 }, { ...RAM, work: 6 }]
const mountHeld = async (mount: (m: any) => Promise<any>, cpu: any = CPU, merge?: any) => {
  let tree: any
  for (const r of RAM_STEPS) tree = await mount(merge ? { ...machine(cpu, r), merge } : machine(cpu, r))
  return tree
}
// #119: a merge running takes its share out of `cpu` (the collector's `merge.cpu`): the six sources then sum to 92, the merge is 8
const MERGE = { cpu: 8, ram_mb: 1024 }
const CPU_MERGE = { ...CPU, work: 17 }

test('the 10-minute CPU strip has as many parts as the CPU bar has sources, in the same colours', async ($, on) => {
  const { mount } = await setup($, on)
  const svg = pcSource(await mountHeld(mount))
  const { segs } = bar(svg, 'CPU')
  const parts = strip(svg)
  if (parts.length !== segs.length) throw new Error(`the strip draws ${parts.length} parts, the CPU bar ${segs.length} sources`)
  parts.forEach((p, i) => {
    if (p.fill.toLowerCase() !== segs[i]!.fill.toLowerCase()) throw new Error(`strip part ${i} is ${p.fill}, the bar's source ${i} is ${segs[i]!.fill}`)
  })
})

test('from one sample the strip\'s parts equal the CPU bar\'s shares per source (within 0.1 %)', async ($, on) => {
  const { mount } = await setup($, on)
  const svg = pcSource(await mountHeld(mount))
  const { BW, segs } = bar(svg, 'CPU')
  const parts = strip(svg)
  segs.forEach((s, i) => {
    const barShare = s.w / BW * 100
    if (Math.abs(parts[i]!.share - barShare) > 0.1) throw new Error(`source ${i}: the strip is ${parts[i]!.share.toFixed(2)} %, the bar ${barShare.toFixed(2)} %`)
  })
})

// the hue of a hex colour, in degrees (0-360), from its RGB channels
const hueOf = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min
  if (d === 0) return 0
  const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4
  return (h * 60 + 360) % 360
}
// the angle between two colours' hues (0-180 degrees)
const hueGap = (a: string, b: string) => { const d = Math.abs(hueOf(a) - hueOf(b)); return Math.min(d, 360 - d) }

test('app and tests are not ticket-state tokens: each is at least 40 degrees of hue from the four states, the work and agents colours, and the other source, and not grey', async ($, on) => {
  const { mount } = await setup($, on)
  const { segs } = bar(pcSource(await mountHeld(mount)), 'CPU')
  // the bar's segments in order: work, agents, app, tests, you, unmeasured
  const app = segs[2]!.fill, tests = segs[3]!.fill
  const against: Record<string, string> = { wait: K.wait, done: K.done, active: K.active, failed: K.failed, work: ROLE.work, agents: ROLE.agents }
  for (const [name, src] of [['app', app], ['tests', tests]] as const) {
    for (const [other, c] of Object.entries(against)) {
      if (hueGap(src, c) < 40) throw new Error(`${name} ${src} is ${hueGap(src, c).toFixed(1)} degrees of hue from ${other} ${c}`)
    }
    const ch = [1, 3, 5].map(i => parseInt(src.slice(i, i + 2), 16))
    if (Math.max(...ch) - Math.min(...ch) < 77) throw new Error(`${name} ${src} is grey (channel spread under 0.3)`)
  }
  if (hueGap(app, tests) < 40) throw new Error(`app ${app} and tests ${tests} are ${hueGap(app, tests).toFixed(1)} degrees of hue apart`)
})

// #119 row 3: with a merge running, the strip draws the merge as a 7th part, as the bars do: as many parts, each share equal within 0.1 %
test('with a merge running the strip has as many parts as the bars, each share equal within 0.1 %, and the merge part in the merge colour', async ($, on) => {
  const { mount } = await setup($, on)
  const svg = pcSource(await mountHeld(mount, CPU_MERGE, MERGE))
  const { BW, segs } = bar(svg, 'CPU')
  if (segs.length !== 7) throw new Error(`the CPU bar draws ${segs.length} parts with a merge running, expected 7`)
  const parts = strip(svg)
  if (parts.length !== segs.length) throw new Error(`the strip draws ${parts.length} parts, the CPU bar ${segs.length}`)
  segs.forEach((s, i) => {
    const barShare = s.w / BW * 100
    if (Math.abs(parts[i]!.share - barShare) > 0.1) throw new Error(`part ${i}: the strip is ${parts[i]!.share.toFixed(2)} %, the bar ${barShare.toFixed(2)} %`)
    if (parts[i]!.fill.toLowerCase() !== s.fill.toLowerCase()) throw new Error(`strip part ${i} is ${parts[i]!.fill}, the bar's part ${i} is ${s.fill}`)
  })
})

// #119 row 4: the merge share's colour is not a ticket-state token: at least 40 degrees of hue from K.wait, K.done, K.active and
// K.failed (#114's hue-distance bar), not grey, and not equal to any other source's colour in the bars
test('the merge share\'s colour is at least 40 degrees of hue from the four states, not grey, and not any other source\'s colour', async ($, on) => {
  const { mount } = await setup($, on)
  const { segs } = bar(pcSource(await mountHeld(mount, CPU_MERGE, MERGE)), 'CPU')
  const merge = segs[6]!.fill
  const states: Record<string, string> = { wait: K.wait, done: K.done, active: K.active, failed: K.failed }
  for (const [name, c] of Object.entries(states)) {
    if (hueGap(merge, c) < 40) throw new Error(`merge ${merge} is ${hueGap(merge, c).toFixed(1)} degrees of hue from ${name} ${c}`)
  }
  const ch = [1, 3, 5].map(i => parseInt(merge.slice(i, i + 2), 16))
  if (Math.max(...ch) - Math.min(...ch) < 77) throw new Error(`merge ${merge} is grey (channel spread under 0.3)`)
  // the bars' other six parts: work, agents, app, tests, you, unmeasured
  segs.slice(0, 6).forEach((s, i) => {
    if (s.fill.toLowerCase() === merge.toLowerCase()) throw new Error(`merge ${merge} equals source part ${i} ${s.fill}`)
  })
})
