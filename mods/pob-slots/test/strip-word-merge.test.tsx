import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
import { actIcon } from '../hooks/kit'

// #112: the «ждёт GitHub» word draws an icon (ACT_ICON's case-insensitive GitHub rule; the case-sensitive git rule misses it)
test('the «ждёт GitHub» word draws an action icon', async () => {
  if (!actIcon('ждёт GitHub')) throw new Error('actIcon(«ждёт GitHub») has no icon')
})

// #112: a session strip's word that names a merge («ждёт мерджа», «мерджит») is shown only while the merge line holds a
// request of that session (one of its tickets in merge.running or merge.waiting); otherwise the raw word from its last
// tool call shows. A session whose last tool result names the rate limit, or whose last background command waits for
// a reset, shows «ждёт GitHub». The seam is the drawn pane (the desktop surface): the `fast` pull carries the sessions
// and the merge line, Haiku's word is faked through the word cache. fs and model are mocked: the live cache is never read
// or written, and no model is called.
const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }

const drawnTexts = (tree: any): string[] => {
  const out: string[] = []
  const walk = (x: any) => {
    if (Array.isArray(x)) { for (const v of x) walk(v); return }
    if (!x || typeof x !== 'object') return
    if (typeof x.source === 'string') out.push(...[...x.source.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1]!))
    for (const v of Object.values(x)) walk(v)
  }
  walk(tree)
  return out
}

// a chat session holding ticket 4; its last tool call is a read (the raw word «читает код»)
const session = (over: any = {}) => ({
  sid: 'sid-s1', name: 'chat-a', kind: 'chat', busy: true, issue: 4, held: [4], model: 'Claude Opus 5.5', ctx: 120_000,
  age: '1 min', last: '', digest: 'Read: register.tsx', dk: 'dk-1', subagents: [], ...over,
})
const waitingOn4 = { running: null, waiting: [{ branch: 'i4-x', issues: [4], position: 1, queued: '2026-10-17T14:00:00+00:00',
  waited_s: 120, eta_start_s: 300, waited_h: '2 min', eta_start_h: '5 min', takes_h: '3 min' }] }

// the harness may hand the path back in another spelling: the cache file is matched by its name
const isCache = (p: any) => /activity\.json$/.test(String(p))

const setup = async ($: any, on: any) => {
  const clock = mock.clock(on)
  let agents: any[] = []
  let merge: any = { running: null, waiting: [] }
  let cache: Record<string, any> = {}
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-17T15:00:00+00:00', agents, merge })
      : e.argv[2] === 'tickets' ? '{"goals":[],"loose":[],"issues":{}}' : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  // the word cache is the only file the words read; every other path is absent, and no write reaches the disk
  on('fs.exists', async (_$: any, e: any) => ({ value: isCache(e.path) && Object.keys(cache).length > 0 }) as any)
  on('fs.read', async (_$: any, e: any) => ({ value: isCache(e.path) ? JSON.stringify(cache) : '' }) as any)
  on('fs.write', async () => ({ value: undefined }) as any)
  on('model.complete', async () => ({ value: { text: '', isAnswered: false, usage: {} } }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  // one session's Haiku word (the cache entry whose dk matches its digest, so no model call is made)
  const mount = async (next: { agents: any[]; merge?: any; words?: Record<string, string> }) => {
    agents = next.agents
    merge = next.merge ?? { running: null, waiting: [] }
    cache = Object.fromEntries(Object.entries(next.words ?? {}).map(([sid, text]) => [sid, { dk: 'dk-1', text, at: 1 }]))
    await clock.advance(6_000) // one fast tick (5 s): the pull runs with the new state before the pane is drawn
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: PROPS })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return drawnTexts(tree)
  }
  return { mount }
}

// row 2: no merge request of this session, the model's word names a merge: the raw word shows instead
test('a session with no merge request does not show «ждёт мерджа» from the model; the raw word shows', async ($, on) => {
  const { mount } = await setup($, on)
  const texts = await mount({ agents: [session()], merge: { running: null, waiting: [] }, words: { 'sid-s1': 'ждёт мерджа' } })
  if (texts.some(t => t.includes('ждёт мерджа'))) throw new Error(`«ждёт мерджа» shown with no merge request: ${texts}`)
  if (!texts.some(t => t.includes('читает код'))) throw new Error(`the raw word «читает код» is not drawn: ${texts}`)
})

// row 3: its merge request waits in merge.waiting: «ждёт мерджа» stays
test('a session with its merge request in merge.waiting keeps «ждёт мерджа»', async ($, on) => {
  const { mount } = await setup($, on)
  const texts = await mount({ agents: [session()], merge: waitingOn4, words: { 'sid-s1': 'ждёт мерджа' } })
  if (!texts.some(t => t.includes('ждёт мерджа'))) throw new Error(`«ждёт мерджа» not drawn with its merge waiting: ${texts}`)
})

// row 4: the last tool result names the rate limit: «ждёт GitHub»
test('a session whose last tool result names the API rate limit shows «ждёт GitHub»', async ($, on) => {
  const { mount } = await setup($, on)
  const digest = 'Bash: gh issue view 4\nresult: gh: API rate limit exceeded'
  const texts = await mount({ agents: [session({ digest, dk: 'dk-2' })], merge: { running: null, waiting: [] } })
  if (!texts.some(t => t.includes('ждёт GitHub'))) throw new Error(`«ждёт GitHub» not drawn for a rate-limited session: ${texts}`)
})

// row 4, the other half: a running background wait until the reset shows «ждёт GitHub» too
test('a session waiting in a background command until the GitHub reset shows «ждёт GitHub»', async ($, on) => {
  const { mount } = await setup($, on)
  const digest = 'Bash: Wait for the GitHub reset at 15:12'
  const texts = await mount({ agents: [session({ digest, dk: 'dk-3' })], merge: { running: null, waiting: [] } })
  if (!texts.some(t => t.includes('ждёт GitHub'))) throw new Error(`«ждёт GitHub» not drawn for a reset wait: ${texts}`)
})

// #119 row 1: no ticket of this session in merge.running/merge.waiting; its last tool line is `merge-status` (a Bash read
// of the merge queue). The raw word must not be «мерджит» (the merge clause of rawWord is gated by the merge state); the
// word it gives with that clause removed is «смотрит логи».
test('#119: a session with no merge request whose last tool line is merge-status does not show «мерджит»', async ($, on) => {
  const { mount } = await setup($, on)
  const digest = 'Bash: python -m tools.agentops merge-status'
  const texts = await mount({ agents: [session({ digest, dk: 'dk-4' })], merge: { running: null, waiting: [] } })
  if (texts.some(t => t.includes('мерджит'))) throw new Error(`«мерджит» shown with no merge request: ${texts}`)
  if (!texts.some(t => t.includes('смотрит логи'))) throw new Error(`the raw word «смотрит логи» is not drawn: ${texts}`)
})

// #119 row 2: the same session with its ticket 4 in merge.running: «мерджит» shows
test('#119: the same session with its ticket in merge.running shows «мерджит»', async ($, on) => {
  const { mount } = await setup($, on)
  const digest = 'Bash: python -m tools.agentops merge-status'
  const running = { running: { kind: 'merge', branch: 'i4-x', branches: ['i4-x'], issues: [4], phase: 'suite', started: '2026-10-17T14:00:00+00:00',
    percent: 50, tests_done: 10, tests_total: 20, failed: 0, eta_s: 60, eta_h: '1 min', elapsed_h: '1 min', workers: 1 }, waiting: [] }
  const texts = await mount({ agents: [session({ digest, dk: 'dk-5' })], merge: running })
  if (!texts.some(t => t.includes('мерджит'))) throw new Error(`«мерджит» not drawn with its ticket in merge.running: ${texts}`)
})
