import type { Register } from 'claude-code'
import { CORE_REPOS } from './repos'

// #176 (ticket chips on the desktop): a reply that holds a GitHub issue link is drawn here as one Markdown of the same
// text, where every issue reference outside a fence reads `[#<n>](<url>)`. Every other reply is passed to the app with
// next(e). A reply with a fence whose info string is a shell keeps its Run button, so the app draws it (#176 row 12).
// No state, no file, no clock, no model call: one text scan per drawing.

// a GitHub issue URL (the number is its last path part)
const ISSUE = /https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/issues\/\d+(?![\w/#-])/
// an issue URL as a link target, whole: owner, repo, number
const ISSUE_TARGET = /^https:\/\/github\.com\/([\w.-]+)\/([\w.-]+)\/issues\/(\d+)$/
// a bracket group with its (target), whole: label, target
const LINK = /^\[([^\][\n]*)\]\(([^)\n]*)\)$/
// a bare reference `<owner>/<repo>#<n>`, not inside a path or a longer word
const REF = /(?<![\w./#-])[\w-]+\/[\w.-]+#\d+(?![\w-])/
// a bare issue number `#<n>`, not inside a word, a URL fragment, an HTML entity or a longer token
const BARE_N = /(?<![\w&/#-])#\d+(?![\w-])/
// what the scan reads, left to right: a code span and an autolink are kept whole (a URL or a reference in them is not a
// link); a bracket group with its optional (target) is kept; a bare issue URL, a bare reference and a bare #n are read
const CODE_OR_LINK = /(`+)[^\n]*?(?<!`)\1(?!`)|<[a-z][\w+.-]*:[^\s<>]*>|\[[^\][\n]*\](?:\([^)\n]*\))?/
const SCAN = new RegExp(`${CODE_OR_LINK.source}|${ISSUE.source}|${REF.source}|${BARE_N.source}`, 'g')
// a link that is not https: or http: (a markdown link to a file path, or a bare file:// link): the app draws it
const FOREIGN = /\[[^\][\n]*\]\((?!https?:\/\/)[^)\n]*\)|\b(?!https?:\/\/)[a-z][\w+.-]*:\/\/\S/i
// the info strings (first word, lower case) of a fence the app draws with its Run button
const SHELL = new Set(['bash', 'sh', 'shell', 'zsh', 'console', 'powershell', 'pwsh', 'ps1', 'cmd', 'bat'])
// an opening fence: up to 3 spaces of indent, a run of ``` or ~~~, then the info string
const OPEN = /^ {0,3}(`{3,}|~{3,})(.*)$/
// a closing fence: up to 3 spaces of indent, a run of the same char, nothing else
const CLOSE = /^ {0,3}(`{3,}|~{3,})\s*$/

// a run of whole lines: a fenced block (its opening line, its content, its closing line if any) or the text between two
type Run = { fenced: boolean; info: string; lines: string[] }

// the text split into runs; joined back with '\n' they give the text again. A fence inside an open fence is content; an
// unclosed fence runs to the end of the text.
const runsOf = (text: string): Run[] => {
  const runs: Run[] = []
  let open: { char: string; len: number; run: Run } | null = null
  let last: Run | null = null
  for (const line of text.split('\n')) {
    if (open) {
      open.run.lines.push(line)
      const close = CLOSE.exec(line)
      if (close && close[1][0] === open.char && close[1].length >= open.len) open = null
      continue
    }
    const o = OPEN.exec(line)
    // a backtick info string holds no backtick (CommonMark), so such a line is no opener
    if (o && !(o[1][0] === '`' && o[2].includes('`'))) {
      const info = (o[2].trim().split(/\s+/)[0] ?? '').toLowerCase()
      const run: Run = { fenced: true, info, lines: [line] }
      runs.push(run)
      open = { char: o[1][0], len: o[1].length, run }
      last = run
      continue
    }
    if (!last || last.fenced) {
      last = { fenced: false, info: '', lines: [] }
      runs.push(last)
    }
    last.lines.push(line)
  }
  return runs
}

// the short form of an issue link
const shortLink = (n: string, url: string): string => `[#${n}](${url})`
// a bare `<owner>/<repo>` names a core repo, GitHub names compared case-insensitively
const isCoreRepo = (owner: string, repo: string): boolean => {
  const want = `${owner}/${repo}`.toLowerCase()
  return CORE_REPOS.some(r => r.toLowerCase() === want)
}

// the text with every issue reference written as `[#<n>](<url>)`; `linked` is true when it holds an issue link: a bare
// issue URL, a bare #<n>, a bare reference of a core repo, or a link whose target is an issue URL, outside a code span
// or an autolink
const shorten = (text: string): { text: string; linked: boolean } => {
  let linked = false
  const out = text.replace(SCAN, (m: string) => {
    if (m.startsWith('https://')) {
      linked = true
      return shortLink(m.slice(m.lastIndexOf('/') + 1), m)
    }
    if (m.startsWith('`') || m.startsWith('<')) return m
    if (m.startsWith('[')) {
      if (ISSUE.test(m)) linked = true
      // a link whose label is a reference to its own issue reads `[#<n>](<url>)`; any other label is kept
      const link = LINK.exec(m)
      const target = link ? ISSUE_TARGET.exec(link[2]) : null
      if (link && target && [`${target[1]}/${target[2]}#${target[3]}`, `#${target[3]}`, link[2]].includes(link[1])) {
        return shortLink(target[3], link[2])
      }
      return m
    }
    // a bare issue number is an issue link and reads as written
    if (m.startsWith('#')) {
      linked = true
      return m
    }
    // a bare reference `<owner>/<repo>#<n>` is an issue link only for a repo of the orchestration core; any other is kept
    const [owner, repo, n] = m.split(/[/#]/)
    if (!isCoreRepo(owner, repo)) return m
    linked = true
    return shortLink(n, `https://github.com/${owner}/${repo}/issues/${n}`)
  })
  return { text: out, linked }
}

export const register: Register = on => {
  on('ui.render', { component: 'AssistantMessage' }, ($, e, next) => {
    if (e.surface !== 'desktop') return next(e)
    const runs = runsOf(e.props.text)
    // a block with a shell-tagged fence keeps its Run button: the app draws the whole block
    if (runs.some(r => r.fenced && SHELL.has(r.info))) return next(e)
    // a link outside the fences decides; a fence's content is drawn as written, so a link in it is no link
    if (runs.some(r => !r.fenced && FOREIGN.test(r.lines.join('\n')))) return next(e)
    let linked = false
    const drawn = runs.map(r => {
      if (r.fenced) return r.lines.join('\n')
      const s = shorten(r.lines.join('\n'))
      linked = linked || s.linked
      return s.text
    }).join('\n')
    if (!linked) return next(e)
    const { Markdown } = $.ui.resolve(e)
    // a summary block (the app's condensed retelling) and its read-only onScreen are passed on as received; the
    // surface keeps a Markdown's own props only (MarkdownLeafProps), so drawn() shows neither (#176 row 10)
    const carried = {
      ...(e.props.isSummary ? { isSummary: e.props.isSummary } : {}),
      ...('onScreen' in e.props ? { onScreen: e.props.onScreen } : {}),
    }
    return <Markdown text={drawn} {...carried} />
  })
}
