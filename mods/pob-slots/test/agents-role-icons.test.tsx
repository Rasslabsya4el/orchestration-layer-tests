import { test, mock } from 'claude-code/testing'
import { ICONS } from '../hooks/kit'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
import { sizeOf, COST, TARGET } from '../hooks/budget'

// #15: a subagent row shows its role as an icon, not the text `<role>: ` before its title. The seam is the drawn pane
// (the desktop surface, whose rows are pictures): the agents come in through the `fast` pull, the drawn tree is read back.
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
// the drawn texts of every picture: the words a reader sees (an alt is not drawn)
const drawnTexts = (tree: any): string[] =>
  strings(tree, 'source').flatMap(s => [...s.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1]!))
// the first path data of an icon: what its drawn picture must carry
const firstD = (name: string) => /d="([^"]*)"/.exec(ICONS[name]![1])![1]!

const agent = (subagents: any[]) => ({
  sid: 'sid-chat-1', name: 'chat-a', kind: 'chat', busy: true, issue: null, held: [],
  model: 'Claude Opus 5.5', ctx: 120_000, age: '1 min', last: '', subagents,
})
const sub = (name: string, type: string, i = 0) => ({
  name, type, issue: null, model: 'Claude Haiku 5.5', ctx: 5_000 + i, age: '1 min', busy: true,
})

// The pane on the desktop surface. One clock and one set of handlers per test (the harness takes them once); the `fast`
// pull answers the agents `mount` last set, and `mount` returns the drawn tree of the pane with them.
const setup = async ($: any, on: any) => {
  const clock = mock.clock(on)
  let agents: any[] = []
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'fast' ? JSON.stringify({ at: '2026-10-16T10:00:00+00:00', agents })
      : e.argv[2] === 'tickets' ? '{"goals":[],"loose":[],"issues":{}}' : '{}',
    stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const mount = async (next: any[]) => {
    agents = next
    await clock.advance(6_000) // one fast tick (5 s): the pull runs with the new agents before the pane is drawn
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'pob-slots', surface: 'desktop', component: 'Pane', requestId: PANE, props: PROPS })
    await clock.settle()
    const tree = await ui.drawn()
    await ui.unmount()
    return tree
  }
  return { mount }
}

const ROLES: [string, string][] = [
  ['worker', 'wrench'],
  ['worker-strong', 'hammer'],
  ['researcher-find', 'list'],
  ['researcher-how', 'book-open'],
  ['reviewer-standards', 'search.octicons'],
  ['reviewer-spec', 'eye.octicons'],
]

for (const [role, icon] of ROLES) {
  test(`a ${role} row draws the ${icon} icon and no "${role}: " text`, async ($, on) => {
    const { mount } = await setup($, on)
    const tree = await mount([agent([sub(`${role}: Fix the parser`, role)])])
    if (!strings(tree, 'source').some(s => s.includes(`d="${firstD(icon)}"`)))
      throw new Error(`${role}: the ${icon} icon is not drawn`)
    if (drawnTexts(tree).some(t => t.includes(`${role}:`)))
      throw new Error(`${role}: the prefix is still drawn as text: ${drawnTexts(tree)}`)
    if (!drawnTexts(tree).some(t => t.includes('Fix the parser')))
      throw new Error(`${role}: the title is not drawn: ${drawnTexts(tree)}`)
    if (!strings(tree, 'alt').some(a => a.includes(role)))
      throw new Error(`${role}: the role is not in the alt text`)
  })
}

// a role that is not in the map draws bot, whatever its name (no name-pattern rule)
for (const type of ['foo-agent', 'bench-agent']) {
  test(`an unknown type ${type} draws the bot icon and names ${type} in its alt`, async ($, on) => {
    const { mount } = await setup($, on)
    const tree = await mount([agent([sub(`${type}: Check the cache`, type)])])
    if (!strings(tree, 'source').some(s => s.includes(`d="${firstD('bot')}"`)))
      throw new Error(`the bot icon is not drawn for ${type}`)
    if (drawnTexts(tree).some(t => t.includes(`${type}:`)))
      throw new Error(`the prefix is still drawn as text: ${drawnTexts(tree)}`)
    if (!strings(tree, 'alt').some(a => a.includes(type)))
      throw new Error(`${type} is not in the alt text`)
  })
}

const ROLE_ICONS = ['wrench', 'hammer', 'list', 'book-open', 'search.octicons', 'eye.octicons', 'bot']
const drawsAnyRoleIcon = (tree: any) => ROLE_ICONS.some(i => strings(tree, 'source').some(s => s.includes(`d="${firstD(i)}"`)))
const doneSub = (name: string, type: string, i = 0) => ({ ...sub(name, type, i), busy: false })
// the x of a drawn text whose words are exactly `title` (its picture's own column)
const xOf = (tree: any, title: string): string | undefined => {
  for (const s of strings(tree, 'source')) for (const m of s.matchAll(/<text x="([\d.]+)"[^>]*>([^<]*)<\/text>/g)) if (m[2] === title) return m[1]
  return undefined
}

test('a row with no prefix draws no role icon and keeps its title', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount([agent([sub('Fix the parser', 'worker')])])
  if (drawsAnyRoleIcon(tree)) throw new Error('a row with no prefix draws a role icon')
  if (!drawnTexts(tree).some(t => t === 'Fix the parser')) throw new Error(`the title is not drawn whole: ${drawnTexts(tree)}`)
})

test('a title with a colon keeps its text and draws no role icon (Fix: parser, TODO: cache)', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount([agent([sub('Fix: parser', 'worker'), sub('TODO: cache', 'worker', 1)])])
  if (drawsAnyRoleIcon(tree)) throw new Error('a colon title draws a role icon')
  for (const t of ['Fix: parser', 'TODO: cache']) if (!drawnTexts(tree).some(d => d === t)) throw new Error(`${t} is not drawn whole: ${drawnTexts(tree)}`)
})

test('a done (muted, not busy) worker row draws the wrench, its title muted, its alt starts with the role', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount([agent([doneSub('worker: Fix the parser', 'worker')])])
  if (!strings(tree, 'source').some(s => s.includes(`d="${firstD('wrench')}"`))) throw new Error('the done worker row draws no wrench')
  if (!strings(tree, 'source').some(s => /<text[^>]*class="mu"[^>]*>Fix the parser<\/text>/.test(s))) throw new Error('the done row title is not muted')
  if (!strings(tree, 'alt').some(a => a.split('\n').some(l => l.startsWith('worker · ')))) throw new Error('the done row alt does not start with the role')
})

test('a case-different prefix still draws its role (Worker: and Researcher-Find: against their types)', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount([agent([sub('Worker: Fix the parser', 'worker'), sub('Researcher-Find: Look up the cache', 'researcher-find', 1)])])
  if (!strings(tree, 'source').some(s => s.includes(`d="${firstD('wrench')}"`))) throw new Error('Worker: draws no wrench')
  if (!strings(tree, 'source').some(s => s.includes(`d="${firstD('list')}"`))) throw new Error('Researcher-Find: draws no list')
  if (drawnTexts(tree).some(t => /^(Worker|Researcher-Find):/.test(t))) throw new Error(`a prefix is still drawn: ${drawnTexts(tree)}`)
})

test('a #N: prefix combined with a role draws the role icon and no #N or role text', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount([agent([sub('#12: worker: Fix the parser', 'worker')])])
  if (!strings(tree, 'source').some(s => s.includes(`d="${firstD('wrench')}"`))) throw new Error('the wrench is not drawn behind #12:')
  if (drawnTexts(tree).some(t => t.includes('#12') || t.includes('worker:'))) throw new Error(`a prefix is still drawn: ${drawnTexts(tree)}`)
  if (!drawnTexts(tree).some(t => t === 'Fix the parser')) throw new Error(`the title is not drawn whole: ${drawnTexts(tree)}`)
})

test('the alt of a role row starts with the role name', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount([agent([sub('worker: Fix the parser', 'worker')])])
  if (!strings(tree, 'alt').some(a => a.split('\n').some(l => l.startsWith('worker · Fix the parser')))) throw new Error(`the alt does not lead with the role: ${strings(tree, 'alt')}`)
})

test('a row without a role starts its title at the x of a role row in the same card', async ($, on) => {
  const { mount } = await setup($, on)
  const tree = await mount([agent([sub('worker: Fix the parser', 'worker'), sub('Fix: parser', 'worker', 1)])])
  const a = xOf(tree, 'Fix the parser'), b = xOf(tree, 'Fix: parser')
  if (a === undefined || b === undefined) throw new Error(`a title is not drawn at its own text: ${drawnTexts(tree)}`)
  if (a !== b) throw new Error(`the role title starts at x ${a}, the role-less title at x ${b}`)
})

// The .claude/agents stems check (every stem has a role-icon entry or falls under the bot rule) is
// tests/test_agents_role_icons.py: the claude-code test environment has no fs or process to list the directory.

// worst case: distinct rows, each with the widest title a row can carry (a 200-char title of a 2-byte letter), all roles in turn
const ROLE_CYCLE = [...ROLES.map(([r]) => r), 'foo-agent']
const worstSubs = (n: number) => Array.from({ length: n }, (_, i) => {
  const role = ROLE_CYCLE[i % ROLE_CYCLE.length]!
  return sub(`${role}: ${i} ${'Ш'.repeat(200)}`, role, i)
})

test('worst case: the subagent rows stay within the charges and the pane stays under TARGET', async ($, on) => {
  const { mount } = await setup($, on)
  const base = await mount([agent([])])
  const N = ROLE_CYCLE.length
  const full = await mount([agent(worstSubs(N))])
  if (drawnTexts(full).length <= drawnTexts(base).length) throw new Error('the sub rows are not drawn: the pull did not reach the pane')
  const delta = sizeOf(full) - sizeOf(base)
  const bound = N * COST.sub + COST.agentsExtra
  if (delta > bound) throw new Error(`${N} sub rows draw ${delta} characters over the charge ${bound}`)
  const many = await mount([agent(worstSubs(60))])
  if (sizeOf(many) > TARGET) throw new Error(`pane of 60 sub rows draws ${sizeOf(many)} characters over TARGET ${TARGET}`)
})
