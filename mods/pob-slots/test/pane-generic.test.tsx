import { test, mock } from 'claude-code/testing'
import { makeRegistry, COMMAND } from '../hooks/register'
import { STATE_WORD } from '../hooks/kit'
import { PANE, REPO } from '../hooks/project'
import { ADDON } from '../hooks/addon'

// What holds for every project that carries the pane: the generic tabs draw, with the stub add-on and with a full one.
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

const walk = (x: any, f: (n: any) => void) => {
  if (!x || typeof x !== 'object') return
  f(x)
  for (const c of (x.children ?? x.props?.children ?? [])) walk(c, f)
}
const stats = async (ui: any) => {
  const tree = await ui.drawn()
  let nodes = 0, cards = 0
  walk(tree, n => { nodes++; if (String(n.props?.key ?? n.key ?? '').startsWith('card-')) cards++ })
  return { nodes, cards }
}

test('the registry holds the generic tabs and the add-on loads', async () => {
  const reg = makeRegistry(ADDON)
  const ids = reg.tabs.map(t => t.id)
  for (const id of ['tickets', 'cost', 'progress']) if (!ids.includes(id)) throw new Error(`tab ${id} missing from ${ids}`)
  if (!STATE_WORD || !PANE) throw new Error('kit tokens or project constants did not load')
})

for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: every generic tab draws`, async ($, on) => {
    const clock = mock.clock(on)
    // The collector answers an empty payload per pull; the tickets pull needs its three lists to be there.
    on('process.run', async (_$, e) => ({ value: { exitCode: 0, stdout: e.argv[2] === 'tickets' ? '{"goals":[],"loose":[],"issues":{}}' : '{}', stderr: '', isStdoutTruncated: false } }) as any)
    on('ui.open', async () => ({ value: { isOpen: true } }) as any)
    on('ui.status', async () => ({ value: undefined }) as any)
    on('session.cwd', async () => ({ value: REPO }) as any)
    void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface, component: 'Pane', requestId: PANE, props: PROPS })
    await clock.settle()
    const first = await stats(ui)
    if (!(first.nodes > 0)) throw new Error(`${surface}: nothing drawn at start`)
    // The main tab always carries the generic cards (Merge draws its own empty state): their head is drawn.
    if (!(first.cards > 0)) throw new Error(`${surface}: main tab draws no card`)
    for (const id of ['main', 'tickets', 'cost', 'progress']) {
      const buttons = await ui.findAll({ type: 'Button' })
      if (!buttons.some((b: any) => b.key === `tab-${id}`)) throw new Error(`${surface}: no tab-${id} button`)
      await ui.press({ key: `tab-${id}` })
      await clock.settle()
      const s = await stats(ui)
      // With empty data the slow tabs may draw an empty body without a card: only the node count holds for them.
      if (!(s.nodes > 0)) throw new Error(`${surface}: tab ${id} drew no nodes`)
      if (id === 'main' && !(s.cards > 0)) throw new Error(`${surface}: main tab lost its cards`)
    }
    await ui.unmount()
  })
}

// a collector with nothing to say answers `{}` for the tickets pull (no `goals`): the tickets and progress tabs
// draw their empty state and the render hook does not throw.
for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: tickets and progress tabs draw when the tickets pull answers {}`, async ($, on) => {
    const clock = mock.clock(on)
    on('process.run', async () => ({ value: { exitCode: 0, stdout: '{}', stderr: '', isStdoutTruncated: false } }) as any)
    on('ui.open', async () => ({ value: { isOpen: true } }) as any)
    on('ui.status', async () => ({ value: undefined }) as any)
    on('session.cwd', async () => ({ value: REPO }) as any)
    void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface, component: 'Pane', requestId: PANE, props: PROPS })
    await clock.settle()
    for (const id of ['tickets', 'progress']) {
      await ui.press({ key: `tab-${id}` })
      await clock.settle()
      const s = await stats(ui)
      if (!(s.nodes > 0)) throw new Error(`${surface}: tab ${id} drew ${s.nodes} nodes with an empty tickets answer`)
    }
    await ui.unmount()
  })
}
