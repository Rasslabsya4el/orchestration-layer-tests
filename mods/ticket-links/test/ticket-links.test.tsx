import { test } from 'claude-code/testing'

// #176: a reply on the desktop surface with a GitHub issue link is drawn by the mod as one Markdown whose bare issue
// URLs read `[#<n>](<url>)`; every other reply is left to the app (`next(e)`). The seam is the drawn tree.

const URL5 = 'https://github.com/o/r/issues/5'
const URL172 = 'https://github.com/o/r/issues/172'
const LIST = `- [#172](${URL172}) — x`

// every node of the drawn tree, depth first
const walk = (x: any, f: (n: any) => void) => {
  if (!x || typeof x !== 'object') return
  f(x)
  for (const c of (x.children ?? x.props?.children ?? [])) walk(c, f)
}
// every string in the drawn tree (text children and string props alike)
const allStrings = (x: any, out: string[] = []): string[] => {
  if (typeof x === 'string') out.push(x)
  else if (Array.isArray(x)) for (const v of x) allStrings(v, out)
  else if (x && typeof x === 'object') for (const v of Object.values(x)) allStrings(v, out)
  return out
}
// the props of every Markdown node in the drawn tree
const markdowns = (tree: any): any[] => {
  const out: any[] = []
  walk(tree, n => { if (n.type === 'Markdown' || n.type?.name === 'Markdown') out.push(n.props ?? {}) })
  return out
}

const mountReply = async ($: any, props: any, surface: 'desktop' | 'terminal' = 'desktop') =>
  $.ui.mount({ plugin: 'ticket-links', surface, component: 'AssistantMessage', props: { isFirstOfReply: true, ...props } })

// the bottom hook: the app's own tree, drawn when the mod passes the reply on with next(e)
const appHook = (on: any) => on('ui.render', { component: 'AssistantMessage' }, ($: any, e: any) => {
  const { Text } = $.ui.resolve(e)
  return <Text key="app">app</Text>
})

const expectMarkdown = async ($: any, text: string, want: string, why: string, extra: object = {}) => {
  const ui = await mountReply($, { text, ...extra })
  const md = markdowns(await ui.drawn())
  if (md.length !== 1) throw new Error(`${why}: expected one Markdown, drew ${md.length}`)
  if (md[0].text !== want) throw new Error(`${why}: Markdown text ${JSON.stringify(md[0].text)} is not ${JSON.stringify(want)}`)
  await ui.unmount()
}

test('row 1: desktop, a list item with a short issue link is drawn as one Markdown of the same text', async $ => {
  await expectMarkdown($, LIST, LIST, 'row 1')
})

test('row 2: desktop, a bare issue URL is shortened to [#5](url) in the Markdown text', async $ => {
  await expectMarkdown($, `see ${URL5} now`, `see [#5](${URL5}) now`, 'row 2')
})

test('new: a bare issue URL inside an inline code span is not rewritten; another bare URL of the text is', async $ => {
  await expectMarkdown($, `code \`${URL5}\` and ${URL172}`, `code \`${URL5}\` and [#172](${URL172})`, 'code span')
})

test('new: an issue URL in an autolink is not rewritten; another bare URL of the text is', async $ => {
  await expectMarkdown($, `<${URL5}> and ${URL172}`, `<${URL5}> and [#172](${URL172})`, 'autolink')
})

// row 3: each case is left to the app, so the bottom hook's tree is what is drawn
const leftToApp = async ($: any, on: any, props: any, surface: 'desktop' | 'terminal', why: string) => {
  appHook(on)
  const ui = await mountReply($, props, surface)
  const tree = await ui.drawn()
  if (markdowns(tree).length !== 0) throw new Error(`${why}: the mod drew a Markdown, the reply was not left to the app`)
  if (!allStrings(tree).includes('app')) throw new Error(`${why}: the app's tree is not drawn`)
  await ui.unmount()
}

test('row 3: desktop, no issue link is left to the app', async ($, on) => {
  await leftToApp($, on, { text: 'plain reply with no link' }, 'desktop', 'no issue link')
})

test('row 3: desktop, issue link plus a file link is left to the app', async ($, on) => {
  await leftToApp($, on, { text: `see ${URL5} and [a.ts](src/a.ts:3)` }, 'desktop', 'issue link plus file link')
})

// row 12: a fence whose opening info string is a shell (bash, sh, ...) keeps its Run button, so the app draws the block
test('row 3 (row 12): desktop, issue link plus a shell-tagged fenced code block is left to the app', async ($, on) => {
  await leftToApp($, on, { text: `see ${URL5}\n\`\`\`bash\ngh issue view 5\n\`\`\`` }, 'desktop', 'issue link plus bash fenced block')
})

test('row 3 (row 12): desktop, issue link plus a tilde shell-tagged fenced code block is left to the app', async ($, on) => {
  await leftToApp($, on, { text: `see ${URL5}\n~~~sh\necho hi\n~~~` }, 'desktop', 'issue link plus tilde sh fenced block')
})

test('row 10: desktop, isSummary true with no issue reference is left to the app', async ($, on) => {
  await leftToApp($, on, { text: 'Монитор починен, ещё в очереди', isSummary: true }, 'desktop', 'isSummary true, no issue reference')
})

test('row 3: terminal surface is left to the app', async ($, on) => {
  await leftToApp($, on, { text: LIST }, 'terminal', 'terminal surface')
})

test('new: an issue URL only in a code span or an autolink is no issue link, left to the app', async ($, on) => {
  await leftToApp($, on, { text: `see \`${URL5}\` and <${URL172}>` }, 'desktop', 'issue URL only in code and autolink')
})

const URL180 = 'https://github.com/o/r/issues/180'
const URL175 = 'https://github.com/o/r/issues/175'

test('row 7: desktop, a link labelled owner/repo#n for its own issue reads [#n](url)', async $ => {
  await expectMarkdown($, `[o/r#180](${URL180}) — x`, `[#180](${URL180}) — x`, 'row 7 owner/repo#n label')
})

test('row 7: desktop, a link labelled #n for its own issue reads [#n](url)', async $ => {
  await expectMarkdown($, `[#180](${URL180}) — x`, `[#180](${URL180}) — x`, 'row 7 #n label')
})

test('row 7: desktop, a link labelled with its own issue URL reads [#n](url)', async $ => {
  await expectMarkdown($, `[${URL180}](${URL180}) — x`, `[#180](${URL180}) — x`, 'row 7 URL label')
})

test('row 7: desktop, a link labelled with other text keeps its label', async $ => {
  const text = `[The busy-PC hold](${URL175})`
  await expectMarkdown($, text, text, 'row 7 other label')
})

const CORE = 'Rasslabsya4el/orchestration-layer'

test('row 8: desktop, a bare owner/repo#n of a core repo reads [#n](url)', async $ => {
  await expectMarkdown($, `see ${CORE}#7 now`, `see [#7](https://github.com/${CORE}/issues/7) now`, 'row 8 bare owner/repo#n')
})

test('row 8: desktop, a bare owner/repo#n of a core repo in a code span stays as written; another is written', async $ => {
  await expectMarkdown($, `code \`${CORE}#7\` and ${CORE}#8`, `code \`${CORE}#7\` and [#8](https://github.com/${CORE}/issues/8)`, 'row 8 code span')
})

test('row 8: desktop, an owner/repo#n of a core repo only in a code span is no issue link, left to the app', async ($, on) => {
  await leftToApp($, on, { text: `see \`${CORE}#7\` now` }, 'desktop', 'owner/repo#n only in code span')
})

test('row 8: desktop, a bare owner/repo#n of a core repo is matched case-insensitively', async $ => {
  await expectMarkdown($, 'see rasslabsya4el/Orchestration-Layer#7', 'see [#7](https://github.com/rasslabsya4el/Orchestration-Layer/issues/7)', 'row 8 case')
})

test('row 8 repo limit: desktop, a bare owner/repo#n of another repo is left to the app', async ($, on) => {
  await leftToApp($, on, { text: 'see o/r#7 now' }, 'desktop', 'owner/repo#n not a core repo')
})

test('row 8 repo limit: desktop, docs/agents#2 alone is left to the app', async ($, on) => {
  await leftToApp($, on, { text: 'see docs/agents#2 now' }, 'desktop', 'path-like docs/agents#2 alone')
})

test('row 8 repo limit: desktop, docs/agents#2 stays as written; a core repo reference is written', async $ => {
  await expectMarkdown(
    $,
    'see docs/agents#2 and Rasslabsya4el/Poe-Junkroan#1183',
    'see docs/agents#2 and [#1183](https://github.com/Rasslabsya4el/Poe-Junkroan/issues/1183)',
    'repo limit path-like and core repo',
  )
})

test('row 8 repo limit: desktop, a link to an issue URL of any repo keeps today\'s rewrite', async $ => {
  await expectMarkdown($, `[o/r#7](https://github.com/o/r/issues/7) — x`, `[#7](https://github.com/o/r/issues/7) — x`, 'row 8 link to any repo')
})

test('row 9: desktop, a bare #n counts as an issue link and the text stays unchanged', async $ => {
  await expectMarkdown($, 'I did not check #176 myself', 'I did not check #176 myself', 'row 9 bare #n')
})

test('row 9: desktop, a bare #n in a code span alone is left to the app', async ($, on) => {
  await leftToApp($, on, { text: 'see `#176` now' }, 'desktop', 'bare #n only in code span')
})

test('row 9: desktop, a #hex colour is left to the app', async ($, on) => {
  await leftToApp($, on, { text: 'use color #fff here' }, 'desktop', 'color #fff')
})

test('row 9: desktop, a #n inside a word, a URL fragment or a bracket group is left to the app', async ($, on) => {
  await leftToApp($, on, { text: 'see https://example.com/page#3 and [the #176 fix](https://example.com/a)' }, 'desktop', 'word, URL fragment, bracket group')
})

test('row 9: desktop, a bare #n next to a rewritten owner/repo#n is not wrapped twice', async $ => {
  await expectMarkdown(
    $,
    `${CORE}#7 and #8`,
    `[#7](https://github.com/${CORE}/issues/7) and #8`,
    'row 9 with a rewritten reference',
  )
})

test('row 10: desktop, a summary block with an issue reference is drawn by the mod as one Markdown of the rewritten text', async $ => {
  await expectMarkdown(
    $,
    'Монитор починен, Rasslabsya4el/orchestration-layer#191 ещё в очереди',
    'Монитор починен, [#191](https://github.com/Rasslabsya4el/orchestration-layer/issues/191) ещё в очереди',
    'row 10 summary with a core reference',
    { isSummary: true },
  )
})

// row 12: a fence is left to the app only when one of its opening fences has a shell info string (bash, sh, shell, zsh,
// console, powershell, pwsh, ps1, cmd, bat; first word, case-insensitive); an empty or other info string is drawn by the
// mod, with the fence's content unchanged
const URL71 = 'https://github.com/o/r/issues/71'
const HANDOFF = '```text\nhand-off\n```'

test('row 12: desktop, a link then a text fence is drawn by the mod; the fence content is unchanged', async $ => {
  const text = `[#71](${URL71})\n\n${HANDOFF}`
  await expectMarkdown($, text, text, 'row 12 text fence')
})

test('row 12: desktop, a link then a bash fence is left to the app', async ($, on) => {
  await leftToApp($, on, { text: `[#71](${URL71})\n\n\`\`\`bash\ngh issue view 71\n\`\`\`` }, 'desktop', 'row 12 bash fence')
})

test('row 12: desktop, a link then a fence with an empty info string is drawn by the mod', async $ => {
  const text = `[#71](${URL71})\n\n\`\`\`\nhand-off\n\`\`\``
  await expectMarkdown($, text, text, 'row 12 empty info fence')
})

test('row 12: desktop, a text fence and a bash fence in one reply is left to the app', async ($, on) => {
  await leftToApp(
    $,
    on,
    { text: `[#71](${URL71})\n\n${HANDOFF}\n\n\`\`\`bash\ngh issue view 71\n\`\`\`` },
    'desktop',
    'row 12 text and bash fences',
  )
})

test('row 12: desktop, a shell info string is matched case-insensitively and by its first word', async ($, on) => {
  await leftToApp($, on, { text: `see ${URL5}\n\`\`\`BASH title=x\nls\n\`\`\`` }, 'desktop', 'row 12 BASH with a title')
})

test('row 12: desktop, an issue URL inside a text fence stays as written; one outside is shortened', async $ => {
  await expectMarkdown(
    $,
    `see ${URL5}\n\n\`\`\`text\n${URL172}\n\`\`\``,
    `see [#5](${URL5})\n\n\`\`\`text\n${URL172}\n\`\`\``,
    'row 12 URL inside text fence',
  )
})

test('row 12: desktop, a block whose only issue reference is inside a text fence is left to the app', async ($, on) => {
  await leftToApp($, on, { text: `plain reply\n\n\`\`\`text\n${URL5}\n\`\`\`` }, 'desktop', 'row 12 reference only in text fence')
})

test('row 12: desktop, a non-https link inside a text fence does not make the block a file-link reply', async $ => {
  const text = `[#71](${URL71})\n\n\`\`\`text\n[a.ts](src/a.ts:3)\n\`\`\``
  await expectMarkdown($, text, text, 'row 12 file link inside fence')
})

test('row 12: desktop, an unclosed text fence runs to the end of the text; its content stays as written', async $ => {
  await expectMarkdown($, `see ${URL5}\n\`\`\`text\n${URL172}`, `see [#5](${URL5})\n\`\`\`text\n${URL172}`, 'row 12 unclosed fence')
})
