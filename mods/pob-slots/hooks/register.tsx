import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import { PX, textWidth, fit, besc } from './draw'
import { K, HOVER_FILL, HOVER_OPACITY, CARD_PITCH } from './tokens'
import { ADDON } from './addon'
import { createRecorder } from './staterec'
import { TARGET, COST, ALT_MAX, LINE_MAX, sizeOf, layout, noPlan, tableCost } from './budget'
import type { Lane, LanePlan, Unit } from './budget'
import type { Recorder, StateInput, StateFs, CardForm } from './staterec'
import { MOD_DIR, SCRIPT, COLLECTOR_FAST, REPO,REPO_NAME, GH, PANE, TITLE, WORK_KEY, WORD_CACHE, WORD_USAGE, REDRAW_LOG, SILENCE_FLAG, MACHINES, JOB_QUEUES, JOB_WAIT, OPENED_TEXT, PROJECT_WORDS } from './project'

import type { Agent, Cell, CostCut, CostMode, CpuSample, DaemonSlots, Fast, Issue, Job, Machine, Merge, MergeLanded, Orch, Progress, Reading, RunPick, Runs, Slow, Tab, Tickets } from '../types'
import { RAIL, TICON, n1, n1r, svgDoc, CELL_PX, PX_PER_COL, CARD_CHROME_PX, P0, INDENT, icon, headSvg, TABH, tabsSvg, TROW, BAR_W, tableSvg, tableFit, cellTop, cellH, markSvg, detailSvg, GRID_GAP, fitGrid, whole, rowSvg, rowsSvg, HIST_DRAW, machineSvg, actIcon, roleOf, queueEnd, ru, dur, ruForm, slotsRu, STATE_DOT, STATE_WORD, STATE_ICON, PARKED_ICON, iconState, rowIssue, BLANK, num, ddmm, words, ageS, SUBS_DONE, slots, kctx, usd, tokH, limPct, plural, thousands, rate, modelName, fresh, LABEL_WORD, progressLines, PROGRESS_OTHER, LABEL_ICON, UNKNOWN_DASH, pctCol, heldIssues, stateOf, subtree, BLANK_CH, BLANK_PX, NBSP_PX, LROW, emptyRow, drawnW, ghostSpan, PG_W, PG_GAP, PG_CY, pagerIcon, LABEL_EM, LABEL_NB, LABEL_PAD, pipeStripKind, pipeWord, pipeSince, pipeLeft } from './kit'
import type { Pill, QRun, QDone, TCol, TCellV, TGroup, THot, TFit, RowCol, CellV, Hot, RowSpec, SlotMark, Split6, TicketState } from './kit'

// #173: the one command name of the monitor in every repo (core code, not a project setting): /monitor opens the monitor
// of the repo the chat is in. Pinned here, where the hook that answers it is.
export const COMMAND = 'monitor'

const panelAtom = atom({ plugin: 'pob-slots', key: 'panel' } as const, null)
const runSelAtom = atom({ plugin: 'pob-slots', key: 'runSel' } as const, null)
const runPickAtom = atom({ plugin: 'pob-slots', key: 'runPick' } as const, null)
const histPageAtom = atom({ plugin: 'pob-slots', key: 'histPage' } as const, 0)
const gridPageAtom = atom({ plugin: 'pob-slots', key: 'gridPage' } as const, { run: null as string | null, page: 0 })
const TK_NATIVE = new Map<string, number>() // #983: what the native part of a ticket row weighs, by the row's form
const TK_COST = new Map<string, [number, number]>() // a ticket row's drawn weight and lines, by its cells and the table's columns (#671)
const tkPageAtom = atom({ plugin: 'pob-slots', key: 'tkPage' } as const, 0)
const fast = atom({ plugin: 'pob-slots', key: 'fast' } as const, null)
const tickets = atom({ plugin: 'pob-slots', key: 'tickets' } as const, null)
// #116 B2: the words a did-not-land merge's reason reads as on the Merge card: ONE table, keyed by merge_status's
// reason values. red-suite takes the failed count; a reason not in it reads «не влит: <reason>» (see notLanded).
const NOT_LANDED_WHY = new Map<string, (failed?: number | null) => string>([
  ['conflict', () => 'конфликт с main'],
  ['rebase-conflict', () => 'конфликт при перестройке на main'],
  ['red-suite', f => (f == null ? 'упали тесты' : `упали тесты: ${f}`)],
  ['red-on-base', () => 'тесты красные уже на main'],
  ['target-check', () => 'запрещённые слова проекта'],
  ['dirty', () => 'незакоммиченные изменения'],
  ['nothing-to-merge', () => 'нечего вливать'],
  ['setup-error', () => 'ошибка запуска'],
  ['not-planned', () => 'тикет закрыт как не планируемый'],
  ['worktree', () => 'запуск не из основной копии'],
  ['tracked-caches', () => 'в ветке закоммичены кэши'],
  ['red-on-branch', () => 'тесты ветки красные'],
  ['shallow', () => 'неполная история клона'],
  ['core-manifest', () => 'правка файла ядра вне обновления'],
  ['merge-refused', () => 'git отказал в слиянии'],
  ['suite-worktree', () => 'тесты не подготовлены'],
  ['stuck', () => 'тесты зависли'],
  ['cut', () => 'сорвано ради срочного тикета'],
  ['push-refused', () => 'main не принял push'],
  ['diverged', () => 'ветка разошлась с origin'],
  ['fetch-failed', () => 'не скачан origin'],
  ['sync-failed', () => 'main не синхронизирован'],
  ['lock-timeout', () => 'очередь слияния занята'],
  ['failed', () => 'слияние не прошло'],
  ['silent-holder', () => 'слияние молчало, линию взяли'],
])
const slow = atom({ plugin: 'pob-slots', key: 'slow' } as const, null)
const errors = atom({ plugin: 'pob-slots', key: 'errors' } as const, {})
// #142 u2: true while the fast pull starts `collect.py fast` itself (the collector's file is stale or missing); the agents card says so
const collectorOffAtom = atom({ plugin: 'pob-slots', key: 'collectorOff' } as const, false)
const tab = atom({ plugin: 'pob-slots', key: 'tab' } as const, 'main')
const expanded = atom({ plugin: 'pob-slots', key: 'expanded' } as const, [])
const costModeAtom = atom({ plugin: 'pob-slots', key: 'costMode' } as const, 'week')
const costGroupAtom = atom({ plugin: 'pob-slots', key: 'costGroup' } as const, 'roles') // #972: the group of tables Расходы shows
const listPageAtom = atom({ plugin: 'pob-slots', key: 'listPage' } as const, {}) // #972: the page of every paged list, by its lane key
const orchAtom = atom({ plugin: 'pob-slots', key: 'orch' } as const, null)
const selectedAtom = atom({ plugin: 'pob-slots', key: 'selected' } as const, null)
// #994 (defect 6): a tab that keeps an open item of its own (TabEntry `ownOpen`) holds it and its panel here: opening or closing it never closes another tab's open item
const readSelAtom = atom({ plugin: 'pob-slots', key: 'readSel' } as const, null)
const readPanelAtom = atom({ plugin: 'pob-slots', key: 'readPanel' } as const, null)
const pressedAtom = atom({ plugin: 'pob-slots', key: 'pressed' } as const, null) // #793 F20: the key of the button pressed this moment
const wordsAtom = atom({ plugin: 'pob-slots', key: 'words' } as const, {})
// #139: the fast pull's interval is not here: it is `monitor.refresh_s` (FAST_DEFAULT_MS when unset), read at start
const FAST_DEFAULT_MS = 5_000
const EVERY = { tickets: 15_000, slow: 600_000, orch: 900_000 } as const // #181: a ticket change reaches the pane within 30 s
// A fast pull every `monitor.refresh_s` (5 s default), but the pane is redrawn at most every SHOW_EVERY unless something the
// owner reacts to changed (a square, a run, an agent, a reservation): every redraw renumbers the
// desktop's press handles and a press carrying the old tree's handle is dropped (09.10: 16 + 3
// `ui_press not handled` in the desktop log), so redraws are rationed, not pulls.
const SHOW_EVERY = 15_000
/** #983: the pages of every paged list as the NEWEST render drew them, by lane key: a pager's press reads it at PRESS time (the host may call the handler of an older render, whose own page count is stale) */
const PAGES: Record<string, number> = {}
/** #994: the page each lane drew in the newest render (a pinned lane's page is not the atom's: a step starts from what is shown) */
const SHOWN: Record<string, number> = {}
/** a line of free text (an error, a note) cut to LINE_MAX characters */
const clipLine = (t: unknown, n = LINE_MAX) => { const x = String(t ?? ''); return x.length > n ? x.slice(0, n - 1) + '…' : x }
const WHOLE_GIVE_UP = 2.5 // #983: see `lane`
const HIST = 120 // CPU samples kept per host: 120 pulls of 5 s = 10 min, drawn as 40 buckets of 15 s


// ---------------------------------------------------------------- registry (#796 u4b)
// The pane is a list of main-tab cards and a list of tabs. Each entry has an `order` and a draw function; a card whose
// payload `key` is absent from the loaded payload (and has no `<key>_error` to show) is not drawn; Merge has no key: it draws its own empty state. The generic entries are the project-free monitor
// (Agents, Merge, PC; tabs Tickets, Cost, Progress); an add-on object brings the project's own entries.
// `draw` gets the render's own drawing functions (`Ctx`); the registry itself holds no state.
type Ctx = Record<string, (...a: any[]) => any>
export type CardEntry = { id: string; order: number; name: string; key?: string; draw: (c: Ctx) => any }
export type TabEntry = { id: string; order: number; label: string; draw: (c: Ctx) => any
  /** #994: the tab keeps an open item of its own (`selectedRd` / `bpRd`): opening or closing it never closes another tab's */ ownOpen?: true }
/** An add-on: extra cards and tabs, plus (all optional) `make` (its render-time cards, built from the pane's drawing context) and `signature` (the part of the redraw signature that is its own). */
export type Addon = { cards: CardEntry[]; tabs: TabEntry[]; make?: (x: Record<string, any>) => any; prepare?: (x: Record<string, any>) => Promise<void>; signature?: (v: Fast) => unknown; roleOrder?: string[] }

const GENERIC: Addon = {
  cards: [
    { id: 'agents', order: 10, name: 'Agents', key: 'agents', draw: c => c.agentsCard() },
    { id: 'merge', order: 20, name: 'Merge', draw: c => c.mergeCard() },
    { id: 'pc', order: 40, name: 'PC', draw: c => c.machineCard('pc') },
  ],
  tabs: [
    { id: 'tickets', order: 30, label: 'Тикеты', draw: c => c.ticketsTab() },
    { id: 'cost', order: 40, label: 'Расходы', draw: c => c.costTab() },
    { id: 'progress', order: 50, label: 'Прогресс', draw: c => c.progressTab() },
  ],
}


/** The ONE registry maker: generic entries plus the add-on's, each list sorted by `order` (the main tab is not in `tabs`: it is always first). */
export const makeRegistry = (addon: Addon): Addon => ({
  cards: [...GENERIC.cards, ...addon.cards].sort((a, b) => a.order - b.order),
  tabs: [...GENERIC.tabs, ...addon.tabs].sort((a, b) => a.order - b.order),
})

// ---------------------------------------------------------------- module

// The collector runs only where somebody looks: a terminal REPL (isInteractive), a desktop or
// phone client attaching (`session.attach`), or the pane's command typed. A desktop chat is an SDK session:
// its session.start says isInteractive false and surface null (debug log 05.10 22:32,
// cc_entrypoint=claude-desktop), so gating on isInteractive alone hid the command there. Background
// sessions (ORCH, autopilot, anomaly handlers) get none of the three and never poll.
let started = false
// #622: start() hands the render hook its fast pull (a reservation re-reads the daemon status at once).
let refreshFast: (() => Promise<void>) | null = null

// What the pane reacts to the moment a pull brings it (everything else waits for the SHOW_EVERY beat):
// a run or a square changing state, the slots a run holds, a queue change, an agent appearing or
// finishing, a reservation, a host link, the readings queue, an error, a ticket of the pipeline entering or leaving a stage (#6: its number, stage, failed).
const signature = (v: Fast) => JSON.stringify([
  v.machine_error, v.agents_error,
  v.agents?.map(a => [a.name, a.busy, a.issue, a.subagents.map(s => [s.name, s.busy])]),
  v.pipeline?.map(r => [r.number, r.stage, r.failed]),
  ADDON.signature?.(v),
])
let shownSig = ''  // signature of the payload the pane shows
let shownRaw = ''  // the whole payload it shows minus `at`: the same again = nothing to redraw
let shownAt = 0    // engine clock (ms) of the last fast atom update
let pendingHist: Record<string, CpuSample[]> = {} // CPU samples of the pulls not yet folded into the atom

// #683 r2: what each busy agent is doing, in a few Russian words Haiku writes from the collector's `digest`
// (its last transcript events). One file shared by every session running this pane, so two open chats never
// ask twice: an entry is { dk: the digest it was written from, text, at, claim }. A new digest is worded at most
// once a minute per agent, at most WORD_PER_PULL per pull; a claim younger than 90 s means another session is on
// it. Every call lands in WORD_USAGE (tokens, ms) for the cost check. Until a word exists the strip shows the
// digest's last own sentence, muted.
const WORD_MODEL = 'claude-haiku-5-5'
const WORD_EVERY = 60_000
const WORD_PER_PULL = 3
// #683 r3 (owner 09.10 23:0x): the words read like a service's loading status: 1-3 words, general, no details.
// A fixed vocabulary of verbs, an optional object (a ticket #N or one file name).
const WORD_VOCAB = ['пишет код', 'правит код', 'читает код', 'ищет в коде', 'ищет в интернете', 'гоняет тесты', 'ждёт тесты',
  'ждёт воркера', 'ждёт мерджа', 'ждёт GitHub', 'мерджит', 'пишет тикет', 'читает тикет', 'пишет отчёт', 'смотрит логи', ...PROJECT_WORDS, 'думает']
const WORD_SYSTEM = `You write the status of a monitor row that shows what a coding agent is doing right now, like a loading status.
Input: the agent's last actions, oldest first ("say:" = what it wrote, other lines = tool calls).
Output: ONE phrase from this list, judged by the LAST actions: ${WORD_VOCAB.join(', ')}.
You may replace "в коде" by "в #N" or add ONE object: a ticket number (#685) or one file name (collect.py), never both,
never more than 3 words in total (good: "гоняет тесты", "ищет в #685", "правит collect.py", "ждёт воркера").
Nothing else: no quotes, no period, no explanation, never Ukrainian.`
type Word = { dk?: string; text?: string; at?: number; claim?: number }
let wording = false
// Until Haiku has worded a digest, the same vocabulary from the last tool call by a fixed table (never a sentence).
// #119: the merge clause shows only while the session holds a merge request (`merging`); without it the line falls
// through to the word it gives with that clause removed (a Bash line: «смотрит логи»).
const rawWord = (digest?: string, merging = false) => {
  const lines = (digest ?? '').split('\n').filter(Boolean)
  const last = lines.reverse().find(l => !l.startsWith('say: ')) ?? ''
  const tool = /^(\w+)/.exec(last)?.[1] ?? ''
  if (/pytest|plugin test|\btest\b/i.test(last) && /^(Bash|PowerShell)/.test(tool)) return 'гоняет тесты'
  if (/^(Edit|Write|MultiEdit|NotebookEdit)$/.test(tool)) return 'правит код'
  if (/^(Read)$/.test(tool)) return 'читает код'
  if (/^(Grep|Glob)$/.test(tool)) return 'ищет в коде'
  if (/^(WebSearch|WebFetch)$/.test(tool) || /^mcp__.*browser/i.test(tool)) return 'ищет в интернете'
  if (/^(Agent|Task|SendMessage)$/.test(tool)) return 'ждёт воркера'
  if (/gh issue (create|edit)/.test(last)) return 'пишет тикет'
  if (/gh issue (view|list)/.test(last)) return 'читает тикет'
  if (merging && /merge/i.test(last)) return 'мерджит'
  if (/^(Bash|PowerShell|Monitor)$/.test(tool)) return 'смотрит логи'
  return lines.length ? 'думает' : ''
}
// #112: the digest's last event: a tool call or a tool result, not a `say:` line
const lastEvent = (digest?: string) => (digest ?? '').split('\n').filter(Boolean).reverse().find(l => !l.startsWith('say: ')) ?? ''
// #112: a session waits on GitHub's budget when its last tool result names the rate limit (bin/collect.py `digest`), or its
// last background command waits for a reset
const waitsGithub = (digest?: string) => {
  const last = lastEvent(digest)
  if (/^result: /.test(last)) return /API rate limit/i.test(last)
  return /^(Bash|PowerShell|Monitor)\b/.test(last) && /\b(wait|sleep|until)\b/i.test(last) && /\b(reset|rate limit)\b/i.test(last)
}
// #112: the two words that name a merge; one shows only while the merge line holds a request of its session
const MERGE_WORD = /^(ждёт мерджа|мерджит)$/
// A Haiku answer kept only when it is 1-3 words and starts with a vocabulary verb.
const VERBS = new Set(WORD_VOCAB.map(w => w.split(' ')[0]!))
const goodWord = (t: string) => t.split(/\s+/).length <= 3 && VERBS.has(t.split(/\s+/)[0]!)
async function wordAgents($: EngineInterface, v: Fast) {
  if (wording) return
  wording = true
  try {
    const want: { id: string; dk: string; digest: string }[] = []
    for (const a of v.agents ?? []) {
      if (a.busy && a.dk && a.digest) want.push({ id: a.sid, dk: a.dk, digest: a.digest })
      for (const s of a.subagents) if (s.busy && s.id && s.dk && s.digest) want.push({ id: s.id, dk: s.dk, digest: s.digest })
    }
    const load = async (): Promise<Record<string, Word>> => {
      try { return (await $.fs.exists(WORD_CACHE)) ? JSON.parse(await $.fs.read(WORD_CACHE)) : {} } catch { return {} }
    }
    let cache = await load()
    let calls = 0
    for (const w of want) {
      const now = Date.now()
      const old = cache[w.id]
      if (old?.dk === w.dk && (!old.text || goodWord(old.text))) continue
      if (old?.claim && now - old.claim < 90_000) continue
      if (old?.at && now - old.at < WORD_EVERY) continue
      if (calls >= WORD_PER_PULL) break
      calls++
      cache = await load()
      cache[w.id] = { ...cache[w.id], claim: now }
      await $.fs.write(WORD_CACHE, JSON.stringify(cache))
      const t0 = Date.now()
      let text = ''
      let usage: Record<string, unknown> = {}
      try {
        const r = await $.model.complete({ model: WORD_MODEL, system: WORD_SYSTEM, prompt: w.digest, maxTokens: 20, effort: 'low', timeoutMs: 30_000 })
        usage = { ...(r.usage ?? {}), answered: r.isAnswered }
        if (r.isAnswered) text = r.text.trim().split('\n')[0]!.replace(/^["«]|["»]$/g, '').replace(/\.$/, '').slice(0, 80)
        text = text.charAt(0).toLowerCase() + text.slice(1)
        if (/[іїєґ]/i.test(text)) { usage = { ...usage, ukrainian: true }; text = '' }
        if (text && !goodWord(text)) { usage = { ...usage, rejected: text }; text = '' }
      } catch (err) {
        usage = { error: String(err).slice(0, 120) }
      }
      cache = await load()
      cache[w.id] = text ? { dk: w.dk, text, at: Date.now() } : { ...cache[w.id], claim: undefined, at: Date.now() }
      // the file keeps only agents seen in the last day
      for (const [k, x] of Object.entries(cache)) if ((x.at ?? x.claim ?? 0) < Date.now() - 86_400_000) delete cache[k]
      await $.fs.write(WORD_CACHE, JSON.stringify(cache))
      try {
        const line = JSON.stringify({ ts: new Date().toISOString(), model: WORD_MODEL, id: w.id, ms: Date.now() - t0, ...usage })
        const before = (await $.fs.exists(WORD_USAGE)) ? await $.fs.read(WORD_USAGE) : ''
        await $.fs.write(WORD_USAGE, `${before}${line}\n`)
      } catch { /* the log is a measurement, never a reason to fail */ }
    }
    const words: Record<string, string> = {}
    for (const [k, x] of Object.entries(cache)) if (x.text && goodWord(x.text)) words[k] = x.text
    await update($, wordsAtom, old => (JSON.stringify(old) === JSON.stringify(words) ? old : words))
  } finally {
    wording = false
  }
}

// #771: one line per real redraw, to find which chat redraws how often (the desktop window grows and hangs).
// Time, the chat's session id, the open tab, the payload's length in characters (the drawn tree is only known
// inside ui.render; the payload that makes it is what the hook has here). The file is shared by every chat's
// CLI; past REDRAW_CAP it keeps its newest half. A failure is swallowed: the log never breaks a redraw.
const REDRAW_CAP = 1_000_000
// #821: while this file exists the pane's own work (every pull of collect.py, every redraw) is skipped and the pane keeps
// its last picture; removing it resumes within one fast tick. Checked at the top of each pull, never cached.
async function silenced($: EngineInterface) {
  try { return await $.fs.exists(SILENCE_FLAG) } catch { return false }
}
async function appendLog($: EngineInterface, rest: string) {
  try {
    const d = new Date()
    const p2 = (n: number) => String(n).padStart(2, '0')
    const ts = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}T${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`
    const sid = await $.session.id().catch(() => 'unknown')
    let before = (await $.fs.exists(REDRAW_LOG)) ? String(await $.fs.read(REDRAW_LOG)) : ''
    if (before.length > REDRAW_CAP) {
      before = before.slice(Math.floor(before.length / 2))
      before = before.slice(before.indexOf('\n') + 1)
    }
    await $.fs.write(REDRAW_LOG, `${before}${ts} ${sid} ${rest}\n`)
  } catch { /* a measurement, never a reason to fail */ }
}
async function logRedraw($: EngineInterface, tabKey: string, chars: number) {
  await appendLog($, `${tabKey} ${chars}`)
}
// #821: a pull or render error is one line `<time> <sid> error <kind> <message>` in the same log.
async function logError($: EngineInterface, kind: 'pull' | 'render', err: unknown) {
  const msg = String(err).replace(/[\s]+/g, ' ').slice(0, 200)
  await appendLog($, `error ${kind} ${msg}`)
}

// #944: the pane records its own state on every change (staterec.ts: state.jsonl + a ring of tree files). The recorder is made once per module; its fs forwards to the newest hook's `$.fs` calls (the loader allows `$` only spelled as a call: never bound or stored)
// (the hook's `$` is only known inside a hook). The StateInput field of this plugin's installed sizes = the character lengths of its own register.tsx / kit.tsx and plugin.json's version, read once (0 / '?' when unreadable).
const STATE_DIR = `${MOD_DIR}/bin/.cache`
let stateFs: StateFs | null = null
let recorder: Recorder | null = null
let entryInfo: Promise<StateInput['entry']> | null = null
function stateRecorder($: EngineInterface): Recorder {
  stateFs = { write: (p, t) => $.fs.write(p, t), read: p => $.fs.read(p), exists: p => $.fs.exists(p) }
  recorder ??= createRecorder({
    write: (p, t) => stateFs!.write(p, t),
    read: p => stateFs!.read(p),
    exists: p => stateFs!.exists(p),
  }, STATE_DIR)
  return recorder
}
function stateEntry($: EngineInterface): Promise<StateInput['entry']> {
  entryInfo ??= (async () => {
    const len = async (p: string) => { try { return (await $.fs.exists(p)) ? String(await $.fs.read(p)).length : 0 } catch { return 0 } }
    let version = '?'
    try { if (await $.fs.exists(`${MOD_DIR}/.claude-plugin/plugin.json`)) version = String(JSON.parse(String(await $.fs.read(`${MOD_DIR}/.claude-plugin/plugin.json`))).version ?? '?') } catch { /* unreadable: '?' */ }
    return { register: await len(`${MOD_DIR}/hooks/register.tsx`), kit: await len(`${MOD_DIR}/hooks/kit.tsx`), version }
  })()
  return entryInfo
}

// #139: the fast pull's interval is `monitor.refresh_s` (seconds) of config/orchestration.json: 5 s when the file, the key
// or a number is missing or unreadable, clamped to 1 s at least and 60 s at most. A pure rule, so its cases run without the clock.
export function fastEveryMs(configText: string | null): number {
  if (configText == null) return FAST_DEFAULT_MS
  let seconds: unknown
  try { seconds = (JSON.parse(configText) as { monitor?: { refresh_s?: unknown } } | null)?.monitor?.refresh_s } catch { return FAST_DEFAULT_MS }
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return FAST_DEFAULT_MS
  return Math.min(60_000, Math.max(1_000, Math.round(seconds * 1_000)))
}
async function fastEveryFrom($: EngineInterface) {
  const path = `${REPO}/config/orchestration.json`
  try {
    return fastEveryMs((await $.fs.exists(path)) ? String(await $.fs.read(path)) : null)
  } catch { return fastEveryMs(null) }
}

// #142 u2: the fast payload the always-running collector wrote to COLLECTOR_FAST, or null when the file is missing, unparsable,
// has no object `data`, or is older than 3 x its refresh_s (5 s when absent). A pure rule: `nowMs` is epoch ms, passed in.
export function fastFromCollector(fileText: string | null, nowMs: number): Fast | null {
  if (fileText == null) return null
  let parsed: { at_ms?: unknown; refresh_s?: unknown; data?: unknown } | null
  try { parsed = JSON.parse(fileText) } catch { return null }
  if (parsed == null || typeof parsed !== 'object') return null
  const at = parsed.at_ms
  if (typeof at !== 'number' || !Number.isFinite(at)) return null
  const rs = parsed.refresh_s
  const refresh = typeof rs === 'number' && Number.isFinite(rs) && rs > 0 ? rs : 5
  if (nowMs - at > 3 * refresh * 1000) return null
  const data = parsed.data
  if (data == null || typeof data !== 'object' || Array.isArray(data)) return null
  return data as Fast
}
// the collector's fast payload when its file is fresh (fastFromCollector); null = stale, missing or unreadable
async function collectorFast($: EngineInterface): Promise<Fast | null> {
  try {
    return fastFromCollector((await $.fs.exists(COLLECTOR_FAST)) ? String(await $.fs.read(COLLECTOR_FAST)) : null, Date.now())
  } catch { return null }
}

async function start($: EngineInterface) {
  if (started) return
  started = true
  const busy: Record<string, boolean> = {}
  const pull = async (mode: 'fast' | 'tickets' | 'slow' | 'orch', timeoutMs: number, force = false) => {
    if (busy[mode]) return
    if (await silenced($)) return
    busy[mode] = true
    try {
      // #142 u2: the fast pull reads the collector's fresh file; `collect.py fast` runs only when that file is stale or missing
      let value: unknown
      const fromFile = mode === 'fast' ? await collectorFast($) : null
      if (fromFile) {
        value = fromFile
        await update($, collectorOffAtom, () => false)
      } else {
        if (mode === 'fast') await update($, collectorOffAtom, () => true)
        const ran = await $.process.run(['python', SCRIPT, mode], { cwd: REPO, timeoutMs })
        if (ran.exitCode !== 0) throw new Error(ran.stderr.trim().split('\n').pop() || `exit ${ran.exitCode}`)
        value = JSON.parse(ran.stdout)
      }
      if (mode === 'fast') {
        const v = value as Fast
        const push = (host: string, m: Machine | undefined) => {
          // #114: the sample carries every source the bars draw (a key an older collector does not send reads as 0); #119: and the merge's share
          if (m) pendingHist[host] = [...(pendingHist[host] ?? []), { work: m.cpu[WORK_KEY] ?? 0, agents: m.cpu.agents ?? 0, app: m.cpu.app ?? 0,
            tests: m.cpu.tests ?? 0, you: m.cpu.you ?? 0, unmeasured: m.cpu.unmeasured ?? 0, merge: m.merge?.cpu ?? 0 }]
        }
        for (const [host, pick] of MACHINES) push(host, pick(v))
        void wordAgents($, v)
        const { at: _at, ...rest } = v
        const raw = JSON.stringify(rest)
        const sig = signature(v)
        const now = await $.clock.now()
        if (force || (raw !== shownRaw && (sig !== shownSig || now - shownAt >= SHOW_EVERY))) {
          const pend = pendingHist
          pendingHist = {}
          await update($, fast, old => {
            const hist = { ...(old?.hist ?? {}) }
            for (const [h, s] of Object.entries(pend)) hist[h] = [...(hist[h] ?? []), ...s].slice(-HIST)
            return { ...v, hist }
          })
          shownRaw = raw
          shownSig = sig
          shownAt = now
          void logRedraw($, await read($, tab), raw.length)
        }
      } else if (mode === 'orch') {
        await update($, orchAtom, () => value as Orch)
      } else if (mode === 'tickets') {
        await update($, tickets, () => value as Tickets)
      } else {
        await update($, slow, () => value as Slow)
      }
      await update($, errors, x => { const y = { ...x }; delete y[mode]; return y })
    } catch (err) {
      void logError($, 'pull', err)
      await update($, errors, x => ({ ...x, [mode]: String(err).slice(0, 160) }))
    } finally {
      busy[mode] = false
    }
  }
  $.ui.status(undefined)
  void pull('fast', 60_000)
  void pull('tickets', 180_000)
  void pull('slow', 300_000)
  void pull('orch', 180_000)
  refreshFast = () => pull('fast', 60_000, true)
  const fastEvery = await fastEveryFrom($) // #139: read once at start, before the fast pull is scheduled
  $.clock.every(fastEvery, () => void pull('fast', 60_000))
  $.clock.every(EVERY.tickets, () => void pull('tickets', 180_000))
  $.clock.every(EVERY.slow, () => void pull('slow', 300_000))
  $.clock.every(EVERY.orch, () => void pull('orch', 180_000))
  void $.ui.open({ id: PANE, title: TITLE, columns: 60 })
}

// The pane opens by itself only in a session of this repo (its worktrees included); in any other
// repo (Zorblax) only the command opens it (owner 05.10).
export function repoOwnsCwd(cwd: unknown, repo: string) {
  if (typeof cwd !== 'string') return false // #173: a session that reports no string cwd is not in this repo
  const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
  const c = norm(cwd)
  const r = norm(repo)
  return c === r || c.startsWith(r + '/') || c.startsWith(r + '-')
}

async function inRepo($: EngineInterface) {
  return repoOwnsCwd(await $.session.cwd(), REPO)
}

// #972: what the state record holds the pane's measure against: the budget's target (hooks/budget.ts: the host's 100,000 less a 5,000 margin)
const HOVER_ROWS_LIMIT = TARGET

// #151 draw-words (layout (b)): an ORCH row's tick text is the longest form that ends before the model column: «k/N тикетов», else «k/N тик.», else «k/N».
// `room` = px from the tick column's x to the model column's x (the text starts at the tick column's x); the picture fits a text to its column, never past it.
const orchTickText = (k: number, n: number, room: number) => {
  for (const form of [`${k}/${n} тикетов`, `${k}/${n} тик.`, `${k}/${n}`]) if (textWidth(form, PX) < room) return form
  return `${k}/${n}`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // The command itself is ~/.claude/commands/<command>.md: a file the desktop composer lists at startup, which a
    // command registered here never reaches; the command.run hook below answers it in a session of this repo.
    // A hot reload fires session.start again; a desktop chat is SDK-hosted (isInteractive false) and its
    // surface stays attached (no second session.attach), so the attached surfaces restart the pulls (#683).
    const drawn = (await $.session.surfaces()).some(s => s === 'desktop' || s === 'mobile')
    if ((e.isInteractive || drawn) && (await inRepo($))) await start($)
    return next(e)
  })

  on('session.attach', async ($, e, next) => {
    if ((e.surface === 'desktop' || e.surface === 'mobile') && (await inRepo($))) await start($)
    return next(e)
  })

  // #173: /monitor is answered only in a session of this repo; any other session passes the command on (no claim here).
  on('command.run', { command: COMMAND }, async ($, e, next) => {
    if (!(await inRepo($))) return next(e)
    // #173 row 2b: a hook that raises is skipped and the command file's body goes to the model (a turn the owner never asked
    // for); once the chat is this repo's, the hook always answers, and says so when the pane would not open.
    try {
      await start($)
      await $.ui.open({ id: PANE, title: TITLE, columns: 60 })
    } catch (err) {
      void logError($, 'render', err)
      return { text: `${TITLE} monitor pane did not open: ${String(err).slice(0, 120)}` }
    }
    return { text: OPENED_TEXT }
  })

  // #972 (the owner's eye, 13.10 22:15: a ring around one title, a fill on the next): a click would leave the host's focus ring on the pressed Button; the pane is a pointer
  // surface with no keyboard flow, so the ring never moves onto it: no `next` keeps it where it was (nowhere).
  on('ui.focus', { component: 'Pane', requestId: PANE }, () => ({}))
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    // The terminal's table may still resolve an Svg (drawn there as an empty box): its bars are text lines.
    const Svg = e.surface !== 'terminal' && 'Svg' in els ? (els as any).Svg : null
    const Link = (els as any).Link
    // #683 r5 (pt 6): every Svg gets its markup's own height up front. Without it the box is 0 tall until the new
    // picture decodes, so each redraw shrank the body for a moment and the pane snapped to the top (measured 10.10
    // 00:36: 6 jumps between the bottom and the top in 8 s after one wheel scroll).
    const svgH = (src: string) => Number(/height=["']([\d.]+)["']/.exec(src)?.[1] ?? 0) || undefined
    const cur = await read($, tab)
    const f = await read($, fast)
    // #886: an answer without `goals` (the collector answers `{}` when it has nothing) is no answer: the empty state.
    const tAnswer = await read($, tickets)
    const t = tAnswer && Array.isArray(tAnswer.goals) ? tAnswer : null
    const s = await read($, slow)
    const errs = await read($, errors)
    const collectorOff = await read($, collectorOffAtom)
    const open = new Set(await read($, expanded))
    const costMode = await read($, costModeAtom)
    const o = await read($, orchAtom)
    const selected = await read($, selectedAtom)
    const cpuHist = f?.hist ?? {}
    const bp = await read($, panelAtom)
    const selectedRd = await read($, readSelAtom)
    const bpRd = await read($, readPanelAtom)
    const runSel = await read($, runSelAtom)
    const runPick = await read($, runPickAtom)
    const histPage = await read($, histPageAtom)
    const gridPage = await read($, gridPageAtom)
    const tkPage = await read($, tkPageAtom)
    const pressed = await read($, pressedAtom)
    const costGroup = await read($, costGroupAtom)
    const listPage: Record<string, number> = await read($, listPageAtom)
    const held = heldIssues(f)
    // The add-on's own assets (the ascendancy faces its rows and panels draw), read before the draw.
    await ADDON.prepare?.({ f, runPick, bp, bpRd, readBytes: (p: string) => $.fs.read(p, { as: 'bytes' }), root: $.plugin.root })
    // The words straight from the shared cache file: the atom stayed empty in the live pane (debug 09.10 22:5x).
    const agentWords: Record<string, string> = Object.fromEntries(Object.entries((await read($, wordsAtom)) as Record<string, string>).filter(([, x]) => goodWord(x)))
    try {
      for (const [k, x] of Object.entries(JSON.parse(await $.fs.read(WORD_CACHE)) as Record<string, Word>)) if (x.text && goodWord(x.text)) agentWords[k] = x.text
    } catch { /* no cache yet */ }

    const setTab = (x: Tab) => () => void update($, tab, () => x)
    const toggle = (n: number) => () =>
      void update($, expanded, xs => (xs.includes(n) ? xs.filter(k => k !== n) : [...xs, n]))

    const Title = ({ children }: { children: any }) => <Text bold color={K.text}>{children}</Text>
    const Tile = ({ children }: { children: any }) => (
      <Box flexDirection="column" backgroundColor={K.tile} borderStyle="round" borderColor={K.tile}
        paddingX={1} marginBottom={1}>{children}</Box>
    )
    const Pic = ({ src, alt, interactive }: { src: string; alt: string; interactive?: boolean }) => {
      const w = Number(/width=["'](\d+)["']/.exec(src)?.[1] ?? 340)
      const hh = Number(/height=["'](\d+)["']/.exec(src)?.[1] ?? 100)
      // A full-width picture (the machine card) has no width: the desktop fits it to the card's content.
      const fit = w >= 300
      return interactive
        ? <Svg source={src} alt={alt} width={fit ? undefined : w} height={fit ? undefined : hh} isInteractive />
        : <Svg source={src} alt={alt} width={fit ? undefined : w} height={fit ? undefined : hh} />
    }
    const Gap = () => <Text> </Text>
    const Muted = ({ children }: { children: any }) => <Text color={K.nmuted}>{children}</Text>
    const Dot = ({ st }: { st: keyof typeof STATE_DOT }) => <Text color={STATE_DOT[st][1]}>{STATE_DOT[st][0]} </Text>
    // A ticket link with the card Claude Code's own transcript shows on hover: state pill, repo #n,
    // how long ago it was opened, the title, the author (from the tickets collector; no hook runs).
    const ago = (iso?: string) => {
      if (!iso) return ''
      const m = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60_000))
      return m < 60 ? `${m} мин назад` : m < 48 * 60 ? `${Math.round(m / 60)} ч назад` : `${Math.round(m / 1440)} дн назад`
    }
    const IssueLink = ({ n, k }: { n: number; k?: string }) => {
      const link = Link ? <Link href={`${GH}${n}`} label={`#${n}`} /> : <Text color={K.nmuted}>#{n}</Text>
      const i = t?.issues[String(n)]
      if (!i) return link
      const [pill, pillIcon, pillCol] = i.open ? ['открыт', 'issue-opened', K.active] : i.reason === 'NOT_PLANNED' ? ['не делаем', 'skip', K.nmuted] : ['закрыт', 'issue-closed', K.done]
      const mark = (name: string, color: string, word: string, own = false) => (Svg
        ? (src => <Svg source={src} alt={word} width={Number(/width=["'](\d+)["']/.exec(src)![1])} height={TROW} />)(markSvg(name, color, word, own))
        : <Text color={color}>{word}</Text>)
      return (
        <Box key={`il-${k ?? n}`} flexDirection="row">
          {link}
          {/* Above the link, right of the # column: an absolute Box paints over the elements drawn before
              it only, so a card hanging below was crossed by the links of the rows after it (owner, 05.10, 06.10). */}
          <Box position="absolute" bottom={1} left={`#${n}`.length + 1} display="none" hover={{ display: 'flex' }} flexDirection="column"
            width={58} borderStyle="round" borderColor={K.rule} backgroundColor={K.tile} paddingX={1}>
            <Box flexDirection="row" gap={1}>
              {mark(pillIcon, pillCol, pill, true)}
              <Text color={K.nmuted} wrap="truncate-end">{`${REPO_NAME} #`}{n}</Text>
              <Box flexGrow={1} />
              <Text color={K.nmuted}>{ago(i.created)}</Text>
            </Box>
            <Text bold color={K.text} wrap="wrap">{i.title}</Text>
            {i.labels.length > 0 && (
              <Box flexDirection="row" flexWrap="wrap" gap={1}>
                {i.labels.map(l => <Text key={l} color={K.text} backgroundColor={K.rule}> {LABEL_WORD[l] ?? l} </Text>)}
              </Box>
            )}
            {i.author && mark('person', K.nmuted, i.author)}
          </Box>
        </Box>
      )
    }

    // ---------------------------------------------------------- main tab
    const Cell = ({ w, children, grow }: { w?: number; grow?: boolean; children: any }) => (
      <Box width={w} flexGrow={grow ? 1 : 0} flexShrink={grow ? 1 : 0} paddingRight={1}>{children}</Box>
    )
    // A table row: zebra by `i`; given `k` (unique in the pane) it is a keyed Box, a hover scope
    // whose background lights while the pointer is over it.
    const Row = ({ children, i, k }: { children: any; i?: number; k?: string }) => (
      k
        ? <Box key={k} hover={{ backgroundColor: K.hover }} flexDirection="row" alignItems="flex-start"
            backgroundColor={i != null && i % 2 === 1 ? K.stripe : undefined}>{children}</Box>
        : <Box flexDirection="row" alignItems="flex-start" backgroundColor={i != null && i % 2 === 1 ? K.stripe : undefined}>
          {children}
        </Box>
    )
    const Head = ({ children }: { children: any }) => (
      <Box flexDirection="column">
        <Box flexDirection="row" alignItems="flex-start">{children}</Box>
        <Text color={K.rule} wrap="truncate-end">{'─'.repeat(200)}</Text>
      </Box>
    )

    // A table drawn column by column: one Box per column holding one Text per row. The desktop gets
    // the whole pane as one tree per redraw and stops redrawing once it grows past ~100k characters
    // (07.10: a card froze on a detail panel's 80 stat rows), and a row of Box cells costs ~800 of them.
    // Odd rows (`odd`) carry the stripe on each cell's Text; the columns have no padding, so the stripe
    // runs unbroken (a fixed column's own slack is the gap). Row hover is not drawn: a hover scope on
    // every cell (~50 characters each) put the pane with an open square at ~124k (07.10).
    type TCell = { t: string; c?: string; b?: boolean } | null
    type TRow = { cells: TCell[]; odd?: boolean }
    const ColTable = ({ cols, rows }: { cols: { w?: number; grow?: boolean }[]; rows: TRow[] }) => (
      <Box flexDirection="row">
        {cols.map((col, ci) => (
          <Box flexDirection="column" width={col.w} flexGrow={col.grow ? 1 : 0} flexShrink={col.grow ? 1 : 0}>
            {rows.map(r => {
              const x = r.cells[ci], bg = r.odd ? K.stripe : undefined
              return col.grow
                ? <Text color={x?.c} bold={x?.b} backgroundColor={bg} wrap="truncate-end">{x?.t || ' '}</Text>
                : <Text color={x?.c} bold={x?.b} backgroundColor={bg}>{x?.t || ' '}</Text>
            })}
          </Box>
        ))}
      </Box>
    )

    // ---------------------------------------------------------- #683 r4c / #972: rows of pictures and THE pointer element
    // A card's rows are ONE picture (kit `rowsSvg`): every word, number, icon and capsule is drawn there, in the picture's own
    // font and colour. Over it lies one absolute column with one Box per row, IN THE FLOW, each as high as its row in px: a
    // blank Svg gives the height (the host takes an Svg's px height as it is, while a Box `height` is whole 19-px cells), so a
    // row's Box stands on its picture row exactly, whatever the rows above it, in every state of the pane. A data row's Box is
    // keyed (a hover scope) and lights with the translucent veil from the pill's left edge to the card's right edge: the row
    // highlight, the same rectangle every table row has. The terminal (no Svg) draws the same facts as one text line a row.
    //
    // #972 (the owner, 2026-10-13: one hover look, «светлые фигуры/кнопки как у новее старее», no underline anywhere): ONE
    // pointer element in the whole pane, `pressEl`: a plain host Button in an absolute Box at the TOP of its row Box (a Button
    // stands at its Box's px top exactly: #945 grid.ts, the b11 probes) and at the cell column of its drawn face. Its label is
    // no-break spaces as wide as the face the picture draws (a title, a ticket number in blue, a seed's capsule, a chevron), or
    // the word itself where the picture draws none (the pager words, a control). The host lights the label box with its light
    // rectangle under the pointer and takes the press on it. No host Link and no Markdown link is drawn on the desktop: both
    // are laid on the host's 19-px text lines (a card row is 26 px) and show an underline (#972's audit: zones −8 … +14 px off).
    // A ticket number's press opens its URL (`openUrl`). There is ONE drawn form: no size tier picks another placement code.
    const cols = Number((e as any).props?.bodyColumns ?? 100)
    // The card's content width (bodyColumns cells of 7.8 px), 4 px over so the fit to the slot only ever shrinks.
    const CW = Math.max(300, Math.min(1400, Math.round(cols * PX_PER_COL) + 4))
    const W = CW
    // The grid is drawn for the 123-column pane (W 963); a narrower pane scales its middle columns.
    const sc = Math.min(1, W / 963)
    const ACT_DX = 80
    const COLS: RowCol[] = [
      { key: 'name', x: 38, w: Math.round(300 * sc) },
      { key: 'tick', x: Math.round(352 * sc), w: 66, hx: PX_PER_COL }, // #994 (defect 4): the head «тикет» stands over its numbers (drawn a pane column right of the column)
      { key: 'meta', x: Math.round(428 * sc), w: Math.round(120 * sc) },
      { key: 'num', x: Math.round(552 * sc), w: 66, align: 'end', num: true },
      { key: 'act', x: Math.round(552 * sc) + ACT_DX, w: Math.max(40, W - Math.round(552 * sc) - ACT_DX - 96) }, // #837 b9b B1: ACT_DX 80 (was 70): the status head stands 14 px (13.5 on the screen) after the end of the right-aligned «контекст» / «билды» head, was 4
      { key: 'time', x: W - 96, w: 84, align: 'end', num: true },
    ]
    // #837 unit 11 (autopilot comparison 00:08Z): the queue card draws on COLS too (the baseline 50ea5412 had one grid for every card); its own QCOLS is gone
    const tv = (v: CellV | undefined) => (v == null ? '' : typeof v === 'string' ? v : v.t ?? '')
    const TCOL: Record<string, string> = { tx: K.text, mu: K.nmuted, er: K.nfailed, wt: K.owner, ok: K.active, dn: K.done, ac: K.accent, nm: K.text }
    // #793 F20: a pressed look next to the hover: the button pressed this moment (its key in `pressedAtom`, cleared
    // 250 ms later) is drawn on the darker `press` fill; the selected one (F17) on `sel`, with its label in the accent.
    // #994 (defect 7, a press that took two tries): the 250-ms press fill is gone (a square slab), and with it the two redraws every press caused (the atom set, then cleared
    // 250 ms later): a redraw landing between the button's down and up is a press the host may drop. A press runs its own closure and nothing else.
    const press = (_k: string, go: (p: any) => void) => (p?: any) => { go(p) }
    /** a ticket's URL opened by the system's handler (the default browser); no console window: rundll32 is a GUI process */
    const openUrl = (url: string) => void $.process.run(['rundll32', 'url.dll,FileProtocolHandler', url], { timeoutMs: 10_000 }).catch(() => {})
    type PressSpec = { key: string; on: (p?: any) => void; label: string; dim?: boolean; left: number; lead?: number
      /** #994: the Box stands this many cells from its row's RIGHT edge instead (a face at a picture's right end: a fitted picture's scale error grows with x), `trail` no-break spaces after the Button */
      right?: number; trail?: number
      /** #995 send-back 2: the Button stays mounted in a 0 x 0 clipped Box (`display: none` loses the next press as an unmount does, measured): a pager's end that a press just reached keeps its Button, so the press after it is not lost */
      hidden?: boolean }
    /** THE pointer element of the pane (#972): a plain host Button in an absolute Box at the top of its row Box, `left` in cells; `lead` = no-break spaces
     *  of plain Text BEFORE the Button (the sub-cell part of its left, so the Button's light starts on the drawn face, not on the cell edge). */
    const pressEl = (p: PressSpec) => (p.right != null
      ? (
        <Box position="absolute" top={0} right={Math.max(0, p.right)} flexDirection="row" {...(p.hidden ? { width: 0, height: 0, overflow: 'hidden' as const } : {})}>
          <Button key={p.key} plain label={p.label} onPress={press(p.key, p.on)} />
          {p.trail ? <Text>{NB.repeat(p.trail)}</Text> : null}
        </Box>
      )
      : (
        <Box position="absolute" top={0} left={Math.max(0, p.left)} {...(p.hidden ? { width: 0, height: 0, overflow: 'hidden' as const } : {})}>
          {p.lead ? <Text>{NB.repeat(p.lead)}</Text> : null}
          {p.dim ? <Button key={p.key} plain label={p.label} dimColor onPress={press(p.key, p.on)} /> : <Button key={p.key} plain label={p.label} onPress={press(p.key, p.on)} />}
        </Box>
      ))
    // #837 b3: a picture fitted to a card's slot (the `Svg` has a `height` and no `width`: the host fits W px into SW = the pane's columns less CARD_CHROME_PX, 930 at 123 columns,
    // W 963), so markup x is on the screen at x * SW / W. `cardFit` and `cardCol` (a press's left in cells) are shared by every card.
    const cardSW = Math.max(300, Math.min(W, Math.round(cols * PX_PER_COL - CARD_CHROME_PX)))
    const cardFit = cardSW / W
    const cardCol = (hp: Hot, e = 0) => Math.max(0, Math.round(hp.x * cardFit / PX_PER_COL) - e)
    const NB = String.fromCharCode(160)
    /** #994 (defect 10): a blank label as wide as its face on the real pane: em spaces and no-break spaces to the face's width less the host's padding (kit LABEL_*) */
    const shortBlank = (need: number) => {
      const c = Math.max(LABEL_NB, need - LABEL_PAD)
      const em = Math.floor(c / LABEL_EM)
      const nb = Math.max(em ? 0 : 1, Math.round((c - em * LABEL_EM) / LABEL_NB))
      return BLANK_CH.repeat(em) + NB.repeat(nb)
    }
    /** #972 (the owner's eye, 13.10 22:15: the number's light «starts left of the #, wider than the number»): the blank label of a drawn face, EXACT: the Box's left is the
     *  cell edge at or before the face's first px, `lead` no-break spaces of plain Text carry the rest (2.77 px each), the label is em spaces (14.75 px) plus no-break spaces
     *  to the face's width within ~1.4 px; the light rectangle then stands on the face on both ends. `x`, `w` in markup px, `fit` = screen px a markup px, `e` = the row Box's cells. */
    const blankSpan = (x: number, w: number, fit: number, e: number): { L: number; lead: number; label: string } => {
      const sx = x * fit
      const L = Math.max(0, Math.floor(sx / PX_PER_COL) - e)
      const lead = Math.max(0, Math.round((sx - (L + e) * PX_PER_COL) / NBSP_PX))
      return { L, lead, label: shortBlank(w * fit) }
    }
    /** a row's pointer element from its `Hot` (kit rowBody): a link opens its URL, a press runs its own; `e` = the cells the row's Box stands right of the card's edge.
     *  The label: the word itself for a press the host draws (`visible`: the pager words), else no-break spaces over the drawn face (`drawn`, or the link's number in class lk, 14 px). */
    const pressAt = (hp: Hot, e: number, rowKey: string) => {
      if (hp.href) {
        const { L, lead, label } = blankSpan(hp.tx ?? hp.x, textWidth(hp.label, 14), cardFit, e)
        return pressEl({ key: `lk-${rowKey}-${hp.label}`, on: () => openUrl(hp.href!), label, lead, left: L })
      }
      if (!hp.press) return null
      if (hp.pg) { // #994: a pager chevron stands at the picture's right end: placed from the right edge (the distance there is a few px, so a fitted picture's scale error is under 1 px)
        // #994: the end of a pager's range is drawn dim and lights nothing. #995 send-back 2 (the press after a page turn was lost, 1 in 2 on the pane): a press that reaches the end
        // used to unmount its own Button, and the host then dropped the next press; the end's Button now stays mounted, not drawn (`hidden`), with no action
        const D = Math.max(0, (W - hp.x - hp.w) * cardFit), R = Math.floor(D / PX_PER_COL)
        if (hp.press.dim) return pressEl({ key: hp.press.key, on: () => {}, label: NB, left: 0, right: 0, hidden: true })
        return pressEl({ key: hp.press.key, on: hp.press.on, label: shortBlank(hp.w * cardFit), left: 0, right: R, trail: Math.round((D - R * PX_PER_COL) / NBSP_PX) })
      }
      if (hp.visible) return pressEl({ key: hp.press.key, on: hp.press.dim ? () => {} : hp.press.on, label: hp.label, dim: !!hp.press.dim, left: cardCol(hp, e) })
      const d = hp.drawn ?? { x: hp.x, w: hp.w }
      const { L, lead, label } = blankSpan(d.x, d.w, cardFit, e)
      return pressEl({ key: hp.press.key, on: hp.press.on, label, lead, left: L })
    }
    // The terminal line of a row: the rail's mark, the name, the ticket, meta, num, then the action and the time.
    const termRow = (k: string, r: RowSpec) => {
      if (r.kind === 'gap' || r.kind === 'head') return null
      if (r.kind === 'label') return (
        <Box key={k} flexDirection="row" gap={1}>
          <Text bold color={K.nmuted}>{r.text ?? ''}</Text>
          <Box flexGrow={1} />
          {r.right && <Text color={K.nmuted}>{r.right}</Text>}
          {(r.hot ?? []).map(hp => (hp.href ? (Link ? <Link href={hp.href} label={hp.label} /> : <Text color={K.accent}>{hp.label}</Text>) : hp.press ? <Button key={hp.press.key} plain label={hp.label} dimColor={!!hp.press.dim} onPress={hp.press.on} /> : null))}
        </Box>
      )
      const col = r.color ?? (r.state ? K[r.state] : K.idle)
      if (r.kind === 'track') {
        const n = Math.max(10, Math.min(40, cols - 34))
        const at = Math.round(Math.max(0, Math.min(1, r.frac ?? 0)) * n)
        return (
          <Box key={k} flexDirection="row" gap={1}>
            <Text><Text color={col}>{'━'.repeat(at)}</Text><Text color={K.rule}>{'─'.repeat(n - at)}</Text></Text>
            {r.knob && <Text bold color={col}>{r.knob}</Text>}
          </Box>
        )
      }
      const c = r.cells ?? {}
      // a nested row's name is indented two spaces a level
      const pad = '  '.repeat(Math.max(0, Math.min(2, r.lvl ?? 0)))
      const cell = (key: string, dflt: string) => {
        const v = c[key]
        if (v == null || v === '') return null
        const o = typeof v === 'object' ? v : { t: v }
        if (o.href) return Link ? <Link href={o.href} label={o.t ?? ''} /> : <Text color={K.accent}>{o.t}</Text>
        if (o.press) return <Button key={o.press.key} plain label={o.t ?? ''} dimColor={!!o.press.dim} onPress={o.press.on} />
        if (o.zones) { // the same bar in 12 characters: each group's part of them by its share
          const lit = Math.round(12 * o.zones.w.reduce((s, w, k) => s + w * (o.zones!.fill[k] ?? 0), 0))
          return <Text color={o.zones.color}>{'■'.repeat(lit) + '□'.repeat(12 - lit)}</Text>
        }
        if (o.stages && !o.stages.solid) return <Text color={o.stages.color}>{Array.from({ length: o.stages.n }, (_, i) => (i <= o.stages!.done ? '■' : '□')).join('')}</Text>
        return <Text color={o.color ?? TCOL[o.cls ?? dflt] ?? K.nmuted} bold={o.cls === 'nm'} wrap="truncate-end">{(key === 'name' ? pad : '') + (o.t ?? '')}</Text>
      }
      const glyph = r.rail?.num != null ? `${r.rail.num}` : r.rail?.ring ? '  ○' : r.rail?.dot || r.rail?.img ? ' •' : r.calm ? '○' : '●'
      return (
        <Box key={k} flexDirection="row" gap={1}>
          <Text color={r.rail?.color ?? col}>{glyph}</Text>
          {cell('name', r.bold ? 'nm' : r.calm ? 'mu' : 'tx')}
          {cell('tick', 'mu')}
          {cell('meta', 'mu')}
          {cell('num', 'mu')}
          <Box flexGrow={1} />
          {cell('act', 'tx')}
          {cell('time', 'mu')}
        </Box>
      )
    }
    // `grid` = the row's columns: the main tab's COLS, or a tab's own (an add-on tab)
    const rowAlt = (r: RowSpec) => r.alt ?? r.text ?? tv(r.cells?.name)
    // #837 b2 (read in the real pane 2026-10-12 18:38): the host gives a row Box its height only from an Svg with a NON-EMPTY alt; an empty alt
    // stands the Box on the card's first line. So every spacer is one empty Svg of the row's px, alt "row", no width prop (#972: the 1x1 rect of #837 b2 is not needed: 46 characters a row less). (#972: a Box `height` as a
    // percentage with decimals is refused by the host, «must be a number or a percentage»; a whole percentage cannot be a 26-px row; a number is whole 19-px cells.)
    const spacer = (px: number, key?: string) => <Svg key={key} source={`<svg xmlns='http://www.w3.org/2000/svg' width='${W}' height='${px}'/>`} alt="row" height={px} />
    /** The rows of one card: ONE picture, over it one absolute column of row Boxes in the flow (see the head of this section). `k` names the card; `i` = a row's index for its own look;
     *  `cols` = its grid. A data row (a strip) = a keyed Box with the hover veil, `marginLeft` = the pill's left in whole cells (its presses are that many cells less left); a row
     *  with a press but no veil (a label row with the pager words, a head) = a plain Box; an unbroken run of rows with neither = ONE spacer. */
    // #972 (the owner's eye, 13.10 22:15: «квадратный хайлайт, раньше был нормальный»): a card row has NO highlight of its own. The host's Box fill is a square slab over a
    // rounded pill (what he saw); a rounded one is a picture of the pill revealed under the pointer (the baseline's pill form), ~290 characters a row, which puts the heaviest
    // state (an open square with its stats) at 106,000 against the host's 100,000 and unmounts the pane (measured on the branch, 13.10 23:0x). So the one light in a card is the
    // element's: the host's light shape on the drawn face of a title, a ticket number, a pager word, a seed; a row with no element lights nothing. The row Box keeps its key
    // (the state record and the tests count rows by it) and starts at the card's edge (no marginLeft).
    // #972 (the brain, 13.10 23:1x): every row lights on hover as a SOFT rounded shape exactly on its own drawn bounds (a card row: its pill; a table row: its 19-px band, rx 5),
    // never the saturated square slab; the element under the pointer lights as the host's rounded lighter shape inside it. A rounded shape the host cannot draw: it is a picture
    // of the row's own shape revealed under the pointer (`display` none → flex inside the row's keyed Box). The cost is measured by the size tests (~250 characters a row).
    const rowsOne = (k: string, items: { r: RowSpec; i: number; cols?: RowCol[] }[], grid: RowCol[] = COLS) => {
      if (!Svg) return items.map(x => termRow(`${k}-${x.i}`, x.r))
      const m = rowsSvg(items, grid, W, k === 'run' ? 1 : 0, k === 'run')
      const pic = <Svg key={`${k}-pic`} source={m.svg} alt={items.map(x => clipLine(rowAlt(x.r), ALT_MAX)).filter(Boolean).join(String.fromCharCode(10))} height={svgH(m.svg)} />
      const isData = (n: number) => items[n]!.r.kind === 'strip' && !items[n]!.r.still // a strip: not a head, a label, a gap or a track; #994: a placeholder lights nothing
      if (!m.hot.some(h => h.length) && !items.some((_x, n) => isData(n))) return pic
      const rows: any[] = []
      let run = 0
      items.forEach((_x, n) => {
        const hot = m.hot[n]!, hh = m.hs[n]!, data = isData(n)
        if (!data && !hot.length) { run += hh; return }
        if (run) { rows.push(spacer(run, `s${k}${n}`)); run = 0 }
        const pill = data ? m.pills[n] : undefined
        // #995 send-back 2 row 3 (a hovered row showed two lights: the row's reveal and, over its title, the title Button's own host light): ONE light, the host's rectangle.
        // A row that presses (its title's press) gets that Button over its whole pill; the row draws no reveal of its own, so a row that does not press lights nothing.
        // The row's other presses (a ticket number, a seed) come after it and stand over it, each lighting its own face.
        const presses = hot.map(hp => pressAt(hp.title && pill ? { ...hp, drawn: { x: pill.x, w: pill.w } } : hp, 0, `${k}${n}`))
        rows.push(pill
          ? <Box key={`hv-${k}${n}`}>{spacer(hh)}{presses}</Box>
          : <Box>{spacer(hh)}{presses}</Box>)
      })
      if (run) rows.push(spacer(run, `s${k}end`))
      return (
        <Box key={k} flexDirection="column">
          {pic}
          <Box position="absolute" top={0} left={0} right={0} flexDirection="column">{rows}</Box>
        </Box>
      )
    }
    /** one row of its own (the readings tab: the open item's panel stands between rows) */
    const rowEl = (k: string, r: RowSpec, i: number, grid: RowCol[] = COLS) => rowsOne(k, [{ r, i }], grid)
    // The legend of the Tickets tab (baseline 50ea5412): one picture, a state dot and its word; a word with no room goes to the next line.
function legendSvg(items: { icon: [string, string]; t: string }[], W: number) {
  const parts: string[] = []
  let x = 10, ln = 0
  for (const it of items) {
    const w = TICON + 6 + textWidth(it.t)
    if (x > 10 && x + w > W - 12) { x = 10; ln++ }
    parts.push(icon(it.icon[0], n1r(x + TICON / 2), ln * TROW + 9.5, TICON, it.icon[1]), `<text x="${n1(x + TICON + 6)}" y="${ln * TROW + 14}">${besc(it.t)}</text>`)
    x += w + 20
  }
  return svgDoc(W, (ln + 1) * TROW, parts.join(''))
}

// ---- #683 r5 (0): the pane's ONE table and ONE detail head (desktop). A picture drawn at its markup's own width
    // (TW = the Svg's `width`) is 1:1, so the native Boxes laid over it in cells (19 px a line, 7.8 px a column) keep
    // their place over many rows; a picture fitted to its slot shrinks ~0.4 % and would drift.
    const TW = W - 4
    // A keyed absolute Box over each data row is its hover scope: an 8-digit colour lights the row, the text under it shows.
    // #683 r5 (E): `whole` = a table of tableFit's columns (no word cut, a wrapped row = a hover Box as many lines
    // high); a row's native cells (a ticket's Link, the tree's toggle) lie INSIDE its hover Box, each in an absolute
    // Box at its pane column: the pointer on one is still on the row, so the row stays lit. `keys` = the rows' own
    // hover keys (the keys these rows had before the table was a picture).
    // #972: a table row keeps the translucent veil Box (a 19-px band over the 19-px row: the zebra's rx-5 corners are the only difference). A rounded reveal picture a row,
    // as the cards have, is ~250 characters a row: the Cost tab (~100 table rows, 84,000 on the owner's pane) would pass the host's 100,000 and unmount.
    // #972: a table row's light is the same family as a card row's: a soft rounded band (rx 5, the zebra's shape) revealed under the pointer, 1:1 like the table picture.
    const tableEl = (k: string, tcols: TCol[], groups: TGroup[], alt: string, whole = false, keys?: string[] | false, uniq = false) => {
      const t = tableSvg(tcols, groups, TW, whole, uniq)
      // (`h` is the JSX factory in this module: a hot cell is `hp`, as in rowEl)
      // #837 b17: a press over a table's picture (the tree's toggle: the chevron is DRAWN in the picture, the Button's label is blank) is the bare Button inside the row's keyed Box, as at 50ea5412:
      // no width Box, no PressBox (the fill-less b6 wrapper showed the owner a square grey block under the pointer); the row's veil is the highlight.
      // #771: a ticket cell is a plain link; its hover card (~1.2k chars a row) took the Cost tab past the size guard.
      // #972: a table's pointer element is `pressEl` like every other: a ticket number (drawn in the picture, class ac) gets a blank Button of its width that
      // opens the ticket; a press (the tree's toggle: the chevron drawn, the label BLANK) its own. The picture is 1:1 (TW), so a column is `x / PX_PER_COL` cells.
      const native = (hp: THot, n: number) => {
        const left = Math.round(hp.x / PX_PER_COL) - 2
        if (hp.card != null || hp.href) {
          const { L, lead, label } = blankSpan(hp.x + 2, textWidth(hp.t || `#${hp.card}`), 1, 2)
          return pressEl({ key: `tl-${k}-${n}-${hp.card ?? hp.t}`, on: () => openUrl(hp.href ?? `${GH}${hp.card}`), label, lead, left: L })
        }
        return hp.press ? pressEl({ key: hp.press.key, on: hp.press.on, label: hp.t, dim: !!hp.press.dim, left }) : null
      }
      // #837 pass 2 (rule 2): each row has its OWN absolute box (top = its grid line, height = its span), as at 50ea5412: a row's
      // place never depends on the boxes before it (the one stacked column with marginTop gaps of 51895958 is gone).
      return (
        <Box flexDirection="column">
          <Svg source={t.svg} alt={alt} width={TW} height={svgH(t.svg)} />
          {t.rows.map((line, n) => {
            const hot = t.hot.filter(hp => hp.row === n)
            const key = (keys || undefined)?.[n] ?? `${k}-h${n}`
            return hot.length
              ? (
                <Box key={key} position="absolute" top={cellTop(line)} left={2} right={0} height={cellH(t.spans[n]!)}>
                  {hot.map(hp => native(hp, n))}
                </Box>
              )
              // #995 send-back 2 row 3: a table row presses nothing as a whole (its toggle and ticket number are presses of their own, each lighting its face): it draws no light;
              // its keyed Box stays, empty, so a row keeps its place and key
              : keys === false ? null // #771: a table with no keys (the Cost error card) draws no Box over a row with nothing native in it
              : <Box key={key} position="absolute" top={cellTop(line)} left={2} right={0} height={cellH(t.spans[n]!)} />
          })}
        </Box>
      )
    }
    /** A whole table of one group, fitted to its own words: `drop` = the columns that leave it, in order, when the pane is too narrow. */
    const tableOf = (k: string, spec: TFit[], rows: TCellV[][], drop: string[], alt: string, keys?: string[] | false, uniq = false) => {
      const f = tableFit(spec, [{ rows }], drop, TW)
      return tableEl(k, f.cols, f.groups, alt, true, keys, uniq)
    }
    const pctCell = (p: number | null, asIs = false): TCellV => (p == null ? '' : { t: `${asIs ? p : Math.round(p)} %`, ratio: p, cls: p >= 100 ? 'ok' : p >= 80 ? 'tx' : 'er' })
    // The head: the picture, and the native buttons over its right edge (a native Button is ~28 px wider than its label).
    const detailHead = (title: string, lines: string[], face?: string, btns: { key: string; label: string; primary?: boolean; on: (press: any) => void }[] = []) => {
      const shown = lines.filter(Boolean)
      const btnW = btns.length ? btns.reduce((s, b) => s + textWidth(b.label, 13) + 28 + PX_PER_COL, PX_PER_COL) : 0
      const src = detailSvg(face || undefined, title, shown.map(t => ({ t })), TW, btnW)
      return (
        <Box flexDirection="column">
          <Svg source={src} alt={[title, ...shown].join(' · ')} width={TW} height={svgH(src)} />
          {btns.length > 0 && (
            <Box position="absolute" top={1} right={1} flexDirection="row" gap={1}>
              {btns.map(b => <Ctl k={b.key} label={b.label} main={b.primary} go={b.on} />)}
            </Box>
          )}
        </Box>
      )
    }
    // #793 F01: ONE control look for a pressed action and a selected choice: a plain Button (no chrome, so no white fill the
    // desktop draws for `variant="primary"`); the selected one on the `sel` fill, the pressed one on the `press` fill for 250 ms, the others dim at rest.
    // #972: the Button itself is the pane's one pointer element (as `pressEl` draws it); the Box around it carries only the selected / pressed fill
    // (the host draws a Button's label in its own colour: a child Text's colour is not drawn, read on the real pane 2026-10-13).
    // #994 (the owner, 14.10: one hover look, «весь дешборд должен соблюдать один стиль»): the Box carries NO fill of its own (its square slab was the one hover that was not the
    // host's rounded light); a control's hover is the host Button's own light, a selected choice is a drawn capsule (`switchPic`)
    const PressBox = ({ k, children }: { k: string; sel?: boolean; children: any }) => <Box key={`hv-pb-${k}`}>{children}</Box>
    const Ctl = ({ k, label, go, dim, sel }: { k: string; label: string; go: (press: any) => void; main?: boolean; dim?: boolean; sel?: boolean }) => (
      <PressBox k={k} sel={sel}>
        <Button key={k} plain label={label} dimColor={dim} onPress={press(k, go)} />
      </PressBox>
    )
    // ---------------------------------------------------------- #972: the lanes of the shown tab (hooks/budget.ts)
    // A list that grows with the data asks `lane` what to draw of it (hooks/budget.ts; DESIGN-SYSTEM.md «The tabs and the size of a tab»). The tab is drawn WHOLE first; only when
    // that draw passes the target is it drawn with every lane empty (measured: the tab's fixed part), the room left shared between the lanes by `layout`, and the tab drawn again
    // with each lane's page. A lane that fits is drawn whole; one that does not gets the pager («новее» / «старее»: on its label row in a queue, in the card's corner elsewhere);
    // a row whose child rows do not fit keeps as many as fit and one «ещё N» row. No list has a page size of its own.
    // #983: three ways a lane answers. 'whole': everything (a design cap of its own its only cut): the tab is drawn so first, and under the target that draw is the pane's;
    // 'probe': nothing (the tab's fixed part is measured); 'plan': its page of the budget's layout.
    let mode: 'whole' | 'probe' | 'plan' = 'whole'
    let wholeEst = 0, wholeOver = false
    /** the Tickets tab's rows and what each is charged: made once a render, read by each of its passes */
    let tkPrep: any = null
    const lanes: Lane[] = []
    let plans: Record<string, LanePlan> = {}
    /** a tab that cuts its own pages from the lane's grant (Тикеты) says here what its drawn page weighs and how many pages it has */
    const ownCharge: Record<string, number> = {}
    const ownPages: Record<string, number> = {}
    const lane = (key: string, units: Unit[], opt: { extra?: number; max?: number; at?: number; pin?: number } = {}): LanePlan => {
      if (mode === 'plan') return plans[key] ?? noPlan(units.length)
      if (mode === 'whole') {
        // the whole draw is given up as soon as the lists' own charges pass WHOLE_GIVE_UP targets (hundreds of rows: no use drawing them to measure them, the budget pages them);
        // under that it is drawn and measured, since a charge may be up to twice what a row draws
        const pl = layout(Number.MAX_SAFE_INTEGER, [{ key, units, ...opt }], listPage).plans[key]!
        wholeEst += pl.charged
        if (wholeEst > TARGET * WHOLE_GIVE_UP) wholeOver = true
        return (plans[key] = wholeOver ? noPlan(units.length) : pl)
      }
      lanes.push({ key, units, ...opt })
      return noPlan(units.length)
    }
    /** a pager's step, decided at PRESS time from the atom's value and the page count of the NEWEST render (`PAGES`; the host may call the handler of an older render, drawn from other data); `n` = the pages of the render that drew the pager, used only before any render stored a count */
    const pagesNow = (key: string, n: number) => Math.max(1, PAGES[key] ?? n)
    const stepList = (key: string, d: number, n: number) => () => void update($, listPageAtom, (x: Record<string, number>) => { const m = pagesNow(key, n); return { ...x, [key]: Math.max(0, Math.min(m - 1, Math.min(x[key] ?? SHOWN[key] ?? 0, m - 1) + d)) } })
    /** #994: forget a lane's paged-to page (an opened row pins its own page again until the next step) */
    const unpage = (key: string) => update($, listPageAtom, (x: Record<string, number>) => Object.fromEntries(Object.entries(x).filter(([k]) => k !== key)))
    const counterOf = (p: LanePlan) => `${p.from}–${p.to} из ${p.total}`
    /** #995 send-back 1 (defect 1: a short last page of a paged section made the cards below it jump up 26 px): the rows a FULL page of a paged lane holds
     *  (its longest page); 0 when the lane is not paged. A queue section pads its page to it with blank rows, so every page keeps one height. */
    const pageFill = (p: LanePlan) => (p.paged ? Math.max(0, ...p.pages.map(x => x.length)) : 0)
    const PAGER_PAD = 2 * PG_W + PG_GAP + 12 // #994: the room a label row's pager takes right of its counter (two icon buttons of PG_W, PG_GAP apart, 12 px after the counter: the light's own padding)
    /** #994: a pager's icon button on a label row: its chevron drawn in the row's picture, a blank host Button over that face; the end of the range is drawn dim and gets no Button */
    const pg = (x: number, which: 'prev' | 'next', press: Hot['press']): Hot => ({ x, w: PG_W, label: which === 'prev' ? 'новее' : 'старее', pg: which, drawn: { x, w: PG_W }, press })
    /** the x of a label row's two icon buttons (markup px) */
    const PG_PREV_X = W - 12 - 2 * PG_W - PG_GAP, PG_NEXT_X = W - 12 - PG_W
    /** the pager of a label row (the queue's «Готово» form): the counter and the two words; nothing when the lane is not paged */
    const labelPager = (key: string, p: LanePlan, step: (d: number) => () => void = d => stepList(key, d, p.pages.length)): { right?: string; rightPad?: number; hot?: Hot[] } => (p.paged
      ? { right: counterOf(p), rightPad: PAGER_PAD, hot: [pg(PG_PREV_X, 'prev', { key: `${key}-prev`, on: step(-1), dim: p.page === 0 }), pg(PG_NEXT_X, 'next', { key: `${key}-next`, on: step(1), dim: p.page === p.pages.length - 1 })] }
      : {})
    /** the pager in a card's corner (the Tickets form): the counter and the two words, drawn only when the lane is paged; a direct child of the Card */
    const cardPager = (key: string, p: LanePlan) => p.paged && cornerPager(key, counterOf(p), { on: stepList(key, -1, p.pages.length), dim: p.page === 0 }, { on: stepList(key, 1, p.pages.length), dim: p.page === p.pages.length - 1 })
    /** #994: the pager in a card's corner: ONE picture (the muted counter, the two chevrons) drawn 1:1 in whole pane columns, a blank host Button over each live chevron; the end of the
     *  range is drawn dim and gets no Button (it lights nothing). The same two icon buttons as a label row's pager. */
    const cornerPager = (key: string, counter: string, prev: { on: () => void; dim: boolean }, next: { on: () => void; dim: boolean }) => {
      const wc = Math.ceil((textWidth(counter) + 12 + 2 * PG_W + PG_GAP + 4) / PX_PER_COL), pw = n1r(wc * PX_PER_COL)
      const xn = pw - 8 - PG_W, xp = xn - PG_GAP - PG_W
      const src = svgDoc(pw, TROW, `<text x="${n1(xp - 12)}" y="14" text-anchor="end" class="mu nu">${besc(counter)}</text>` + pagerIcon('prev', xp + PG_W / 2, PG_CY, prev.dim) + pagerIcon('next', xn + PG_W / 2, PG_CY, next.dim))
      const btn = (x: number, which: 'prev' | 'next', b: { on: () => void; dim: boolean }) => {
        // #995 send-back 2: the dim end keeps its Button mounted and not drawn (see pressAt), so the press after the one that reached it is not lost
        if (b.dim) return pressEl({ key: `${key}-${which}`, on: () => {}, label: NB, left: 0, hidden: true })
        const { L, lead, label } = blankSpan(x, PG_W, 1, 0)
        return pressEl({ key: `${key}-${which}`, on: b.on, label, lead, left: L })
      }
      // #994 (seen on the whole card, 14.10 06:3x): the Box's top 0 is the card's border edge, 10 px over its title band: a blank picture of PG_DROP px puts the pager's line
      // on the title's (the chevrons' Buttons stand at the top of their own row Box, under it)
      return (
        <Box key={`pg-${key}`} position="absolute" top={0} right={1} width={wc} flexDirection="column">
          <Svg source={`<svg xmlns='http://www.w3.org/2000/svg' width='${pw}' height='${PG_DROP}'/>`} alt="row" width={pw} height={PG_DROP} />
          <Box key={`pg-${key}-line`}>
            <Svg source={src} alt={counter} width={pw} height={TROW} />
            {btn(xp, 'prev', prev)}
            {btn(xn, 'next', next)}
          </Box>
        </Box>
      )
    }
    /** #994: a switch (Cost's period, its group) as ONE picture a line: each choice a capsule of the host light's own shape (19 px, rx 4), the chosen one in the selected look
     *  (the `sel` fill, an accent outline, its word in the accent): the hover (the host's rounded light over the same face) is its lighter step. A blank host Button over every
     *  choice; lines 4 px apart, so two switches never touch. */
    const SW_PAD = 8, SW_GAP = 4, SW_LINE = TROW + 4
    const PG_DROP = 10
    const switchPic = (lines: { key: string; label: string; sel: boolean; go: () => void }[][]) => (
      <Box flexDirection="column">
        {lines.map(opts => {
          let x = 0.5
          const faces = opts.map(o => { const w = Math.ceil(textWidth(o.label) + 2 * SW_PAD); const f = { ...o, x, w }; x += w + SW_GAP; return f })
          const lw = Math.ceil(x)
          const body = faces.map(f => (f.sel ? `<rect x="${f.x}" y=".5" width="${f.w}" height="${TROW - 1}" rx="4" fill="${K.sel}" stroke="${K.accent}" stroke-opacity=".55"/>` : '') +
            `<text x="${n1(f.x + SW_PAD)}" y="14" class="${f.sel ? 'ac' : 'mu'}">${besc(f.label)}</text>`).join('')
          return (
            <Box key={`sw-${opts[0]!.key}`} flexDirection="column">
              <Svg source={svgDoc(lw, SW_LINE, body)} alt={opts.map(o => (o.sel ? `[${o.label}]` : o.label)).join(' · ')} width={lw} height={SW_LINE} />
              {faces.map(f => { const { L, lead, label } = blankSpan(f.x, f.w, 1, 0); return pressEl({ key: f.key, on: f.go, label, lead, left: L }) })}
            </Box>
          )
        })}
      </Box>
    )
    // A card: the tile, its title band (icon, title, a muted sub, a state word at the right), then its rows.
    const Card = ({ k, iconName, name, sub, state, children }:
      { k: string; iconName?: string; name: string; sub?: string; state?: { t: string; cls: string }; children: any }) => (
      <Box key={`card-${k}`} flexDirection="column" backgroundColor={K.tile} borderStyle="round" borderColor={K.tile} paddingX={1} marginBottom={1}>
        {Svg
          ? (src => <Svg source={src} alt={[name, sub, state?.t].filter(Boolean).join(' · ')} height={svgH(src)} />)(headSvg([iconName ?? null, name], sub ?? '', W, state))
          : (
            <Box flexDirection="row" gap={1}>
              <Text bold color={K.text}>{name}</Text>
              {sub && <Text color={K.nmuted}>{sub}</Text>}
              {state && <Text color={TCOL[state.cls] ?? K.nmuted}>{`· ${state.t}`}</Text>}
            </Box>
          )}
        {children}
      </Box>
    )
    const tick = (n: number | null | undefined): CellV => (n == null ? '' : { t: `#${n}`, cls: 'ac', href: `${GH}${n}` })
    const titleOf = (n: number | null | undefined) => (n != null ? t?.issues[String(n)]?.title : undefined)
    const isClosed = (n: number) => t?.issues[String(n)]?.open === false
    // "Claude Opus 5.5" -> "Opus 5.5"; an ORCH session by its role, every other by its chat title.
    const shortModel = (m: string) => m.replace(/^Claude\s+/, '')
    // #837 b9b B5: an unknown cell of an Agents row (no model, a context of 0 or none, no duration) is the muted long dash of the machine card, never an empty cell, «0» or «-»
    const UNKNOWN: CellV = { t: UNKNOWN_DASH, cls: 'mu' }
    const modelCell = (m: string | null | undefined): CellV => { const s = shortModel(String(m ?? '')).trim(); return s && s !== '-' ? s : UNKNOWN }
    const ctxCell = (n: number | null | undefined): CellV => (n && n > 0 ? kctx(n) : UNKNOWN)
    const ageCell = (v: string | null | undefined): CellV => { const s = ru(v).trim(); return s && s !== '-' ? s : UNKNOWN }
    const isOrch = (a: Agent) => /^orch\b/i.test(a.name)
    // #151 draw-words: the px an ORCH tick text may take: its column's width, cut at the model column (the tick column's x to the model column's x)
    const tickCol = COLS.find(c => c.key === 'tick')!
    const tickRoom = Math.min(tickCol.w, COLS.find(c => c.key === 'meta')!.x - tickCol.x)
    // "#681 test-selection replay measurement" -> "test-selection replay measurement": the tick column carries the #.
    const subName = (s: Agent['subagents'][number]) => String(s.name || s.type).replace(/^#\d+\s*[:·-]?\s*/, '')
    // Haiku's word for an agent, else the same vocabulary from its last tool call, drawn muted.
    // #119: a row with no ticket of its own (a subagent) has no merge request: its raw word takes no merge clause
    const wordFor = (id: string | undefined, digest: string | undefined, merging = false) =>
      id && agentWords[id] ? { word: agentWords[id]!, muted: false } : { word: rawWord(digest, merging), muted: true }
    // #112: a session strip's word. A session waiting on GitHub's budget says «ждёт GitHub» whatever the model wrote; a word
    // that names a merge stays only while the merge line holds a request of this session (one of its `tickets` in
    // merge.running or merge.waiting), else the raw word from its last tool call.
    const sessionWord = (id: string | undefined, digest: string | undefined, tickets: number[]) => {
      if (waitsGithub(digest)) return { word: 'ждёт GitHub', muted: false }
      const model = id ? agentWords[id] : undefined
      const merging = tickets.some(n => f?.merge?.running?.issues?.includes(n) || f?.merge?.waiting?.some(w => w.issues?.includes(n)))
      if (model && (!MERGE_WORD.test(model) || merging)) return { word: model, muted: false }
      return { word: rawWord(digest, merging), muted: true }
    }
    const act = (w: { word: string; muted: boolean }): CellV => (w.word ? { t: w.word, cls: w.muted ? 'mu' : 'tx', icon: actIcon(w.word), inset: true } : '')
    const hm = (iso?: string) => {
      if (!iso) return ''
      if (/[+-]\d\d:\d\d$/.test(iso) || !/Z$/.test(iso)) return String(iso).slice(11, 16)
      const d = new Date(iso)
      return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    }
    // #837 b3: the footer's stamp: `HH:MM` as `hm`, and `dd.mm HH:MM` when the stamp's own date is not today's. Only an ISO string holds a date; a bare clock string stays `hm`'s.
    // The date is read in the frame `hm` reads the clock in: an explicit offset (the collector's +00:00) against today's UTC date, a `Z` or naive stamp against today's local date.
    const hmDay = (iso?: string) => {
      const t = hm(iso)
      if (!t || !iso || !/^\d{4}-\d\d-\d\dT/.test(iso)) return t
      const ymd = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
      const now = new Date()
      let day: string, today: string
      if (/[+-]\d\d:\d\d$/.test(iso)) { day = iso.slice(0, 10); today = now.toISOString().slice(0, 10) }
      else { const d = /Z$/.test(iso) ? new Date(iso) : null; day = d ? ymd(d) : iso.slice(0, 10); today = ymd(now) }
      return day === today ? t : `${day.slice(8, 10)}.${day.slice(5, 7)} ${t}`
    }

    // The rail of one parent's nested rows (lvl 1, or 2 = under the lvl-1 row before it): which lines pass through
    // each; `tail` = the level-0 line goes on under the last of them (a queue's running -> waiting line).
    const nestRails = (kids: RowSpec[], tail = false): RowSpec[] => {
      let had1 = false
      const lv = kids.map(k => { const l = (k.lvl ?? 1) >= 2 && had1 ? 2 : 1; if (l === 1) had1 = true; return l })
      return kids.map((k, i) => {
        const later1 = tail || lv.slice(i + 1).includes(1)
        const next2 = lv[i + 1] === 2
        return { ...k, lvl: lv[i], rail: { ...k.rail, up: true, ...(lv[i] === 1 ? { down: later1, sub: next2 } : { down: next2, outer: later1 }) } }
      })
    }

    // AGENTS: one strip per session (busy first): rail icon (hubot = ORCH, speech = a chat), name, ticket, model, ctx,
    // what it does, time; its rows nested under it, one INDENT right. A chat: its working subagents, of the finished
    // ones the SUBS_DONE latest, the rest one calm line. An ORCH: «N тикетов» on its own row,
    // then one row per ticket (in work, claimed, waiting, closed: the title, the #N Link, its state in plain words,
    // how many subagents finished on it) with its WORKING subagents one level deeper; a finished subagent is a count,
    // never a row (an ORCH keeps ~20 finished ones in a busy hour).
    // A ticket its ORCH holds but no longer claims (tools/agentops/dispatch.py `_ended_outcome`: an open issue the
    // session itself unassigned = parked) says what it waits for, read from the other cards' own data; one never
    // worked on is simply not taken yet.
    const parkedOn = (n: number): string | null => {
      const m = f?.merge
      if (m?.running?.issues?.includes(n) || m?.waiting?.some(w => w.issues?.includes(n))) return 'ждёт мерджа'
      const jobs = JOB_QUEUES(f).flatMap(r => [...(r?.running ?? []), ...(r?.queued ?? [])])
      if (jobs.some(j => j.issue === n)) return JOB_WAIT
      if (m?.landed?.some(l => l.rc === 0 && l.issues?.includes(n))) return 'смерджен, ждёт закрытия'
      if (t?.issues[String(n)]?.labels?.includes('needs-info')) return 'ждёт ответа'
      return null
    }
    const agentsBlock = () => {
      const tkHeld = held // the Tickets tab's own held set (the loop below has a `held` of its own: this ORCH's)
      // #995 (the owner, 14.10: «линия разделяющая Чаты (брейн и тд) и не чаты (автопилоты, орки, кимастеры ...)»): two sections of the one table, as a queue's: «Чаты»
      // (the interactive sessions) first, then «Фоновые сессии» (every background session: an autopilot, an ORCH, ...), each busy first
      // (Claude Code's own `kind` of the session: 'background' for a session started in the background; an ORCH and an autopilot by their names as well)
      const isChat = (a: Agent) => a.kind !== 'background' && !isOrch(a) && !/^autopilot\b/i.test(a.name)
      const list: Agent[] = [...(f?.agents ?? [])].sort((x, y) => Number(isChat(y)) - Number(isChat(x)) || Number(y.busy) - Number(x.busy))
      const rows: RowSpec[] = []
      type Sub = Agent['subagents'][number]
      // #972: a session is a unit of the lane: its strip and its child rows; a child row not drawn is counted into the unit's one «ещё N» row (`t` tickets, `w` working, `d` finished)
      type Kid = { r: RowSpec; cost: number; t?: number; w?: number; d?: number }
      const sess: { row: RowSpec; kids: Kid[]; chat: boolean }[] = []
      const subRow = (s: Sub, lvl: number, withTick = true): RowSpec => {
        const sw = s.busy ? wordFor(s.id, s.digest) : { word: '', muted: false }
        const deep = (s.depth ?? 1) > 1
        // #15: a `<role>: <title>` row draws its role as an icon in the name cell and the title without the prefix; the alt leads with the role's name.
        // A row with no role takes the same inset as a role row (#837 b11 R3): its title starts at the same x in the card.
        const role = roleOf(subName(s), String(s.type ?? ''))
        const nameCell: CellV = { t: role ? role.title : subName(s), ...(role ? { icon: role.icon } : { inset: true }), ...(s.busy ? {} : { cls: 'mu' }) }
        return {
          kind: 'strip', lvl: deep ? 2 : lvl, state: s.busy ? 'active' : 'done', calm: !s.busy, child: true,
          alt: role ? `${role.role} · ${role.title}: ${sw.word}` : `${subName(s)}: ${sw.word}`,
          rail: { [deep ? 'ring' : 'dot']: true, color: s.busy ? K.active : K.done },
          cells: { name: nameCell, tick: withTick ? tick(s.issue) : '', meta: modelCell(s.model), num: ctxCell(s.ctx), act: act(sw), time: ageCell(s.age) },
        }
      }
      const calmRow = (text: string, alt: string, age?: string): RowSpec => ({
        kind: 'strip', lvl: 1, state: 'done', calm: true, child: true, alt,
        rail: { dot: true, color: K.done }, cells: { name: { t: text, cls: 'mu' }, time: ru(age) },
      })
      for (const a of list) {
        const orch = isOrch(a)
        const busySubs = a.subagents.filter(s => s.busy)
        const done = a.subagents.filter(s => !s.busy).sort((x, y) => ageS(x.age) - ageS(y.age))
        const held = ((a.held ?? []) as any[]).map(h => (typeof h === 'number' ? { number: h } : h)) as { number: number; claimed?: boolean; units?: { done: number; total: number } | null }[]
        const tnums = orch ? [...new Set([...held.map(h => h.number), ...a.subagents.map(s => s.issue).filter((n): n is number => n != null)])] : []
        // #151 draw-words: a held ticket is done when it is closed or its merge landed (rc 0 in the merge line's `landed`)
        const landedNums = new Set((f?.merge?.landed ?? []).filter(l => l.rc === 0).flatMap(l => l.issues ?? []))
        // A ticket of this ORCH: 0 in work (a subagent works on it), 1 claimed, 2 held and waiting, 3 known only by
        // its finished subagents, 4 closed; drawn in that order, the closed ones by their latest subagent.
        const latest = (ss: Sub[]) => [...ss].sort((x, y) => ageS(x.age) - ageS(y.age))[0]
        const tks = tnums.map(n => {
          const h = held.find(x => x.number === n)
          const work = busySubs.filter(s => s.issue === n), fin = done.filter(s => s.issue === n)
          const st = work.length ? 0 : isClosed(n) ? 4 : h?.claimed ? 1 : h ? 2 : 3
          // #129: the held entry's unit progress (Python's `units`, null or absent when the ticket has none); #151: a ticket known only by its
          // subagents or closed has no held entry, so its units come from the session's `ticket_units` map (the payload contract)
          return { n, work, fin, st, last: latest([...work, ...fin]), units: h?.units ?? a.ticket_units?.[String(n)] ?? null }
        }).sort((x, y) => x.st - y.st || (x.st === 4 ? ageS(x.last?.age ?? '99 d') - ageS(y.last?.age ?? '99 d') : 0))
        const mark = (st: number) => (st === 4 ? 'done' : st <= 1 ? 'active' : 'idle')
        // #112: the session's own tickets (its held ones and its current issue) decide whether a merge word is its own
        const ownTickets = [...held.map(h => h.number), ...(a.issue != null ? [a.issue] : [])]
        const w = a.busy ? sessionWord(a.sid, a.digest, ownTickets) : { word: '', muted: false }
        const name = orch ? 'ORCH' : a.name
        const kids: Kid[] = []
        if (orch) {
          const CLOSED = 2
          let closed = 0
          for (const k of tks) {
            if (k.st === 4 && ++closed > CLOSED) continue
            const nW = k.work.length, nF = k.fin.length
            // #129 / #151: a ticket with units leads its text with «k/n» on every state (closed and finished-only included); #151 draw-words: no word before it
            const said = [
              k.units ? `${k.units.done}/${k.units.total}` : '',
              k.st === 0 ? `в работе: ${nW} ${ruForm(nW, 'воркер', 'воркера', 'воркеров')}`
                : k.st === 1 ? (nF ? 'у ORCH в работе' : 'взят, воркера пока нет')
                : k.st === 2 ? parkedOn(k.n) ?? (nF ? 'отложен ORCH' : 'ещё не взят')
                : k.st === 4 ? 'закрыт' : '',
              nF ? `${nF} ${ruForm(nF, 'отработал', 'отработали', 'отработали')}` : '',
            ].filter(Boolean).join(' · ')
            const title = titleOf(k.n)
            const iss = t?.issues[String(k.n)]
            // #837 pass 2 (rule 7): a ticket row is the ticket's state exactly as «Состояния» names it and «Цели» draws it
            // (the same iconState / STATE_ICON, the title tx while open, mu once closed); closed = purple, never the grey `idle`
            const tst: TicketState = k.st <= 1 ? 'work' : k.st === 4 ? (iss && !iss.open ? iconState(iss, tkHeld) : 'closed') : iss ? iconState(iss, tkHeld) : 'open'
            const stIcon = STATE_ICON[tst]
            const cls = k.st === 0 ? 'tx' : 'mu' // baseline 50ea5412 :1620
            kids.push({ cost: COST.ticket, t: 1, r: {
              kind: 'strip', lvl: 1, state: mark(k.st), calm: k.st !== 0, child: true, alt: `#${k.n} ${title ?? ''}: ${said}`,
              // F14 / F16: the ticket's state icon in front of the name; the number stands in the тикет column like a parent row's (#837 agents-child-tick); the state's
              // word carries the same icon and colour
              rail: { icon: stIcon[0], size: 13, color: K[mark(k.st)] },
              cells: { name: { t: title ?? 'тикет', cls }, tick: tick(k.n), act: said ? { t: said, cls, inset: true } : '', time: ru(k.last?.age) },
            } })
            for (const s of k.work) kids.push({ r: subRow(s, 2, false), cost: COST.sub, w: 1 })
          }
          if (closed > CLOSED) kids.push({ cost: COST.calm, t: closed - CLOSED, r: calmRow(`ещё ${closed - CLOSED} ${ruForm(closed - CLOSED, 'закрыт', 'закрыты', 'закрыты')}`, `${closed - CLOSED} more closed tickets`) })
          for (const s of busySubs) if (s.issue == null) kids.push({ r: subRow(s, 1), cost: COST.sub, w: 1 })
          const loose = done.filter(s => s.issue == null)
          if (loose.length) kids.push({ cost: COST.calm, d: loose.length, r: calmRow(`ещё ${loose.length} ${ruForm(loose.length, 'завершён', 'завершены', 'завершены')} без тикета`, `${loose.length} more finished subagents without a ticket`, loose[loose.length - 1]!.age) })
        } else {
          const doneShown = done.length > SUBS_DONE + 1 ? SUBS_DONE : done.length
          const more = done.length - doneShown
          for (const s of busySubs) kids.push({ r: subRow(s, 1), cost: COST.sub, w: 1 })
          for (const s of done.slice(0, doneShown)) kids.push({ r: subRow(s, 1), cost: COST.sub, d: 1 })
          if (more > 0) kids.push({ cost: COST.calm, d: more, r: calmRow(`ещё ${more} завершены`, `${more} more finished subagents`, done[done.length - 1]!.age) })
        }
        sess.push({ kids, chat: isChat(a), row: {
          kind: 'strip', state: a.busy ? 'active' : 'idle', calm: !a.busy, bold: true, alt: `${name}: ${w.word}`,
          rail: { icon: orch ? 'bot.hubot.octicons' : 'message.octicons', size: 15, color: a.busy ? K.active : K.idle, down: kids.length > 0 },
          cells: {
            name, meta: modelCell(a.model), num: ctxCell(a.ctx),
            // #151 draw-words: k = the held tickets closed or merge-landed, N = the held ones; the form is the one that fits the room (orchTickText)
            tick: !orch ? tick(a.issue) : tks.length ? { t: orchTickText(tks.filter(k => isClosed(k.n) || landedNums.has(k.n)).length, tks.length, tickRoom), cls: 'mu' } : '',
            act: act(w), time: ageCell(a.age),
          },
        } })
      }
      // #972: the sessions through the budget: the page's sessions, each with the child rows that fit; the rows not drawn of a session are ONE calm row that counts them
      const plan = lane('ag', sess.map((x): Unit => ({ own: COST.session, kids: x.kids.map(k => k.cost), more: COST.calm })), { extra: COST.agentsExtra })
      // a section's label row stands over its first session on the page; a section with no session at all draws its label and the «пусто» placeholder (a queue's empty section)
      const SECTIONS: [boolean, string][] = [[true, 'Чаты'], [false, 'Фоновые сессии']]
      const labelled = new Set<boolean>()
      const section = (chat: boolean) => {
        if (labelled.has(chat)) return
        labelled.add(chat)
        rows.push({ kind: 'label', text: SECTIONS.find(x => x[0] === chat)![1] })
      }
      const lastPage = !plan.paged || plan.page === plan.pages.length - 1
      if (!sess.some(x => x.chat) && plan.page === 0) { section(true); rows.push(emptyRow()) }
      for (const sh of plan.shown) {
        const x = sess[sh.i]!
        section(x.chat)
        const kids = x.kids.slice(0, sh.kids).map(k => k.r)
        const cut = x.kids.slice(sh.kids)
        if (cut.length) {
          const n = (k: 't' | 'w' | 'd') => cut.reduce((a, c) => a + (c[k] ?? 0), 0)
          const [nT, nW, nD] = [n('t'), n('w'), n('d')]
          const parts = [nT ? `${nT} ${ruForm(nT, 'тикет', 'тикета', 'тикетов')}` : '', nW ? `${nW} ${ruForm(nW, 'работает', 'работают', 'работают')}` : '', nD ? `${nD} ${ruForm(nD, 'завершён', 'завершены', 'завершены')}` : ''].filter(Boolean)
          kids.push(calmRow(`ещё ${parts.join(', ')}`, `${cut.length} more rows`))
        }
        rows.push(x.row)
        // no gap row between sessions: the baseline's 4-px gap picture was 0 cells high in the host (round(4/19)), its sessions stood 26 px apart
        rows.push(...nestRails(kids))
      }
      if (!sess.some(x => !x.chat) && lastPage) { section(false); rows.push(emptyRow()) }
      const err = errs.fast ?? f?.agents_error
      return { pager: cardPager('ag', plan), body: (
        <Box flexDirection="column">
          {rowsOne('ag', ([{ kind: 'head', cells: { name: 'сессия', tick: 'тикет', meta: 'модель', num: 'контекст', act: 'делает', time: 'длительность' } }, ...rows] as RowSpec[]).map((r, i) => ({ r, i })))}
          {err && <Text color={K.nfailed} wrap="truncate-end">{clipLine(err)}</Text>}
          {collectorOff && <Text color={K.nmuted} wrap="truncate-end">collector off: pulling directly</Text>}
        </Box>
      ) }
    }

    // A QUEUE (Merge and the add-on's runs card, one component): Идёт (a strip + its track + child rows) / В очереди (amber
    // numbered circles) / Готово (one row per entry); one rail line joins running -> waiting.
    // #972: `runLabel` / `waitLabel` = the pager of a paged section on its label row (`labelPager`); `waitFrom` = the places in the queue before the first waiting row drawn
    type LabelPager = { right?: string; rightPad?: number; hot?: Hot[] }
    const queueRows = ({ running, waiting, done, heads, runLabel, waitLabel, waitFrom = 0, waitFill = 0 }: { heads: Record<string, string>; running: QRun[]; waiting: { alt: string; cells: Record<string, CellV>; stale?: boolean; sel?: boolean }[];
      done: { rows: QDone[]; right?: string; pager?: Hot[]; fill?: number }; runLabel?: LabelPager; waitLabel?: LabelPager; waitFrom?: number; waitFill?: number }) => {
      const rows: RowSpec[] = [{ kind: 'head', cells: heads }]
      const flows = running.length > 0 && waiting.length > 0
      rows.push({ kind: 'label', text: 'Идёт', ...(runLabel ?? {}) })
      if (!running.length) rows.push({ kind: 'strip', state: 'idle', calm: true, still: true, rail: { dot: true, color: K.idle }, cells: { name: { t: 'ничего', cls: 'fa' } } })
      running.forEach((r, ri) => {
        const kids = r.kids ?? []
        const last = ri === running.length - 1
        const hasTrack = r.frac != null
        rows.push({ kind: 'strip', state: r.stale ? 'idle' : r.state ?? 'active', calm: r.stale, fade: r.stale, sel: r.sel, bold: true, alt: r.alt, rail: { icon: r.icon, color: r.stale ? K.idle : K[r.state ?? 'active'], down: hasTrack || kids.length > 0 || (last && flows), size: 15 }, cells: r.cells })
        if (hasTrack) rows.push({ kind: 'track', state: r.stale ? 'idle' : r.state ?? 'active', fade: r.stale, frac: r.frac, knob: r.knob, alt: `${r.alt}: ${r.knob ?? ''}`, rail: { up: true, down: kids.length > 0 || (last && flows) } })
        // its child rows (a run's items being computed) hang one level right of it
        rows.push(...nestRails(kids.map(k => ({ ...k, ...(r.stale ? { calm: true, fade: true } : {}) })), last && flows))
      })
      rows.push({ kind: 'label', text: 'В очереди', rail: { through: flows }, ...(waitLabel ?? {}) })
      if (!waiting.length) rows.push(emptyRow())
      waiting.forEach((w, i) => rows.push({ kind: 'strip', state: 'wait', calm: true, fade: w.stale, sel: w.sel, alt: w.alt, rail: { num: waitFrom + i + 1, color: K.wait, up: i > 0 || flows, down: i < waiting.length - 1 }, cells: w.cells }))
      // #995 send-back 1 (defect 1): a short page of a paged section keeps a full page's height (blank rows of a strip's height: they draw and light nothing)
      const pad = (n: number) => { for (let k = 0; k < n; k++) rows.push({ kind: 'gap', h: CARD_PITCH }) }
      if (waiting.length) pad(waitFill - waiting.length)
      rows.push({ kind: 'label', text: 'Готово', right: done.right, rightPad: done.pager?.length ? PAGER_PAD : 0, hot: done.pager ?? [] })
      done.rows.forEach(d => rows.push({ kind: 'strip', state: d.state, calm: !d.sel, sel: d.sel, alt: d.alt,
        rail: { icon: d.state === 'failed' ? 'failed.octicons' : d.state === 'idle' ? 'pause.octicons' : 'done.octicons', color: K[d.state] }, cells: d.cells }))
      if (done.rows.length) pad((done.fill ?? 0) - done.rows.length)
      return rows
    }

    // MERGE (#680): the entry under test (its title, ticket, tests and time left; a track whose knob counts the
    // tests a / b, the phase before and after the suite), the waiting ones, the last landings one row per branch
    // ("с 2-й попытки" when a branch landed after a failed try).
    // #995 (the owner, 14.10: «зачем тут столбец ветка? Он абсолютно ни о чем мне не говорит»): no «ветка» column (a row with no ticket title still names its branch
    // in the title cell); «Готово» is a lane with the label row's pager, as the runs card's «Готово»: MERGE_PAGE landings a page at most (the card's height of before)
    // #1093: the full run on main (merge-status `full_run`, its own lock and out folder) is merge work too; the collector passes it
    // through with raw seconds (only `running` gets its human durations), so the card reads them through `dur`
    type FullRun = { commit?: string | null; percent?: number | null; failed?: number | null; elapsed_s?: number | null; eta_s?: number | null }
    const fullRunOf = (m: Merge | undefined) => (m as (Merge & { full_run?: FullRun | null }) | undefined)?.full_run
    const PHASE: Record<string, string> = { queue: 'очередь', suite: 'тесты', 're-check': 'перепроверка', push: 'пуш' }
    // #203: the name of a core update's row (its home head's sha8, as the merge-status record gives it)
    // #207: no sha yet (the head is read under the lock, after the queue shows the row) → the bare name, no «?»
    const coreUpdateName = (sha: string | null | undefined) => (sha ? `обновление ядра ${sha}` : 'обновление ядра')
    const MERGE_PAGE = 3
    const mergeBlock = () => {
      const m: Merge | undefined = f?.merge
      const run = m?.running
      const running: QRun[] = []
      if (run) {
        // #37: a commit holding the line is one holder: its message is the task, its ticket `#N` (the message's first line) or
        // the word `commit`; a merge keeps its branch and ticket
        const isCommit = run.kind === 'commit'
        // #203: a core update holding the line is named by its sha8, with no ticket (its `issues` is empty)
        const isCore = run.kind === 'core-update'
        const coreName = coreUpdateName(run.sha)
        const commitTick = run.issues.length ? { t: run.issues.map(n => `#${n}`).join(' '), cls: 'ac', href: `${GH}${run.issues[0]}` } : { t: 'commit', cls: 'tx' }
        // #1089: one running row per ticket of the batch (same phase, knob and elapsed); a run with no ticket keeps one row
        const batch: (number | null)[] = run.issues.length && !isCommit ? run.issues : [null]
        const frac = run.tests_done != null && run.tests_total ? run.tests_done / run.tests_total : (run.percent ?? 0) / 100
        const pos = run.phase === 'suite' ? 1 + 10 * frac : run.phase === 're-check' ? 11.5 : run.phase === 'push' ? 12.5 : 0.5
        const tests = run.tests_done != null && run.tests_total ? `${num(run.tests_done)} / ${num(run.tests_total)}` : `${run.percent ?? 0} %`
        const phase = PHASE[run.phase] ?? run.phase
        for (const n of batch) running.push({
          icon: 'git-merge', alt: isCommit ? `commit ${run.message ?? ''}` : isCore ? coreName : `merge ${run.branches.join(', ')}`, frac: pos / 13, state: run.failed > 0 ? 'failed' : 'active',
          knob: run.phase === 'suite' ? tests : phase,
          cells: {
            name: isCommit ? (run.message || 'commit') : isCore ? coreName : (titleOf(n) ?? (run.branches.join(', ') || '?')), tick: isCommit ? commitTick : tick(n),
            act: run.failed > 0 ? { t: `упало ${run.failed}${run.eta_h ? `, ещё ~${ru(run.eta_h)}` : ''}`, cls: 'er', icon: 'failed.octicons' }
              : { t: `${phase}${run.eta_h ? `, ещё ~${ru(run.eta_h)}` : ''}`, cls: 'tx', icon: run.phase === 'suite' ? 'flask.octicons' : 'git-merge' },
            time: ru(run.elapsed_h),
          },
        })
      }
      // #1093: the full run on main is one row under «Идёт», after the merge's rows; its track is `percent` (merge_status's
      // tests_total is the suite's own summary count, not this run's tests), its act the phase or «упало N»
      const fr = fullRunOf(m)
      if (fr) {
        const fpct = fr.percent ?? 0
        const fail = (fr.failed ?? 0) > 0
        const eta = fr.eta_s != null ? dur(fr.eta_s) : ''
        running.push({
          icon: 'git-merge', alt: `full run on main ${fr.commit ?? ''}`, frac: (1 + 10 * fpct / 100) / 13, state: fail ? 'failed' : 'active',
          knob: `${fpct} %`,
          cells: {
            name: `full run on main ${fr.commit ?? ''}`, tick: tick(null),
            act: fail ? { t: `упало ${fr.failed}${eta ? `, ещё ~${eta}` : ''}`, cls: 'er', icon: 'failed.octicons' }
              : { t: `тесты${eta ? `, ещё ~${eta}` : ''}`, cls: 'tx', icon: 'flask.octicons' },
            time: fr.elapsed_s != null ? dur(fr.elapsed_s) : '',
          },
        })
      }
      const waitRows = (m?.waiting ?? []).map(w => {
        // #203: a waiting core update is named by its sha8, with no ticket chip and the git-merge icon
        const core = w.kind === 'core-update'
        const n = w.issues?.[0] ?? null
        const name = core ? coreUpdateName(w.sha) : (titleOf(n) ?? w.branch ?? '')
        return { alt: core ? `${name} waits` : `merge ${w.branch} waits`, cells: { name: { t: name, cls: 'tx' }, tick: core ? '' : tick(n),
          act: { t: (w.eta_start_h ? `старт через ~${ru(w.eta_start_h)}` : `${w.position}-й в очереди`) + (w.takes_h ? `, займёт ~${ru(w.takes_h)}` : ''), cls: 'wt', icon: core ? 'git-merge' : 'waiting.octicons' },
          time: w.waited_h ? `ждёт ${ru(w.waited_h)}` : '' } as Record<string, CellV> }
      })
      // #1089: a ticket whose parked merge is lost (its merge_lost, bin/collect.py) is a waiting row: «merge lost», no time
      const lostRows = Object.entries(t?.issues ?? {}).filter(([, i]) => i?.merge_lost === true).map(([k]) => {
        const n = Number(k)
        return { alt: `merge lost #${n}`, cells: { name: { t: titleOf(n) ?? '', cls: 'tx' }, tick: tick(n),
          act: { t: 'merge lost', cls: 'er', icon: 'failed.octicons' }, time: '' } as Record<string, CellV> }
      })
      const waitAll = [...waitRows, ...lostRows]
      // #972: the waiting entries through the budget: a page of them, the pager on the «В очереди» row
      const wp = lane('mgw', waitAll.map(() => COST.mergeWait), { extra: COST.mergeExtra })
      const waiting = wp.shown.map(s => waitAll[s.i]!)
      // One row per branch: a refused or retried landing of the same branch counts as an attempt, not a row.
      const byBranch: (MergeLanded & { attempts: number })[] = []
      for (const l of m?.landed ?? []) {
        const g = byBranch.find(x => x.branch === l.branch)
        if (g) g.attempts++
        else byBranch.push({ ...l, attempts: 1 })
      }
      const dp = lane('mgd', byBranch.map(() => COST.mergeDone), { extra: COST.mergeDoneExtra, max: MERGE_PAGE })
      const done: QDone[] = dp.shown.map(s => byBranch[s.i]!).map(l => {
        const n = l.issues[0] ?? null
        // #1089: a landed ticket still open says so (its merge is in, its issue is not closed)
        const open = n != null && t?.issues?.[String(n)]?.open === true
        // #85: another branch of this ticket still runs or waits in the merge line: this landing is not the ticket's end
        const r = m?.running
        const sibling = n != null && ((!!r?.issues?.includes(n) && (r?.branches ?? []).some(b => b !== l.branch))
          || (m?.waiting ?? []).some(w => w.branch !== l.branch && !!w.issues?.includes(n)))
        const tries = l.attempts > 1 ? `, с ${l.attempts}-й попытки` : ''
        // #116 B2: `не влит в HH:MM — <причина>` (HH:MM from the end); an unknown reason reads `не влит: <reason>`
        const notLanded = (): string => {
          const at = l.ended ? ` в ${hm(l.ended)}` : ''
          if (!l.reason) return `не влит${at}`
          const why = NOT_LANDED_WHY.get(l.reason)
          return why ? `не влит${at} — ${why(l.failed)}` : `не влит: ${l.reason}`
        }
        return { state: l.rc ? 'failed' : 'done', alt: `${l.branch} ${l.rc ? 'did not land' : 'landed'}`, cells: {
          name: { t: titleOf(n) ?? l.branch, cls: 'mu' }, tick: tick(n),
          act: l.rc ? { t: notLanded(), cls: 'er' }
            : sibling ? { t: `влита ветка, ждёт вторая${tries}`, cls: 'mu' }
            : { t: `влит${l.landed ? ` в ${hm(l.landed)}` : ''}${open ? ', тикет открыт' : ''}${tries}`, cls: 'mu' },
          time: ru(l.took_h) } }
      })
      return (
        <Box flexDirection="column">
          {rowsOne('mg', queueRows({ running, waiting, waitFrom: Math.max(0, wp.from - 1), waitLabel: labelPager('mgw', wp), waitFill: pageFill(wp),
            done: { rows: done, pager: labelPager('mgd', dp).hot ?? [], right: byBranch.length ? (dp.paged ? counterOf(dp) : String(byBranch.length)) : '', fill: pageFill(dp) },
            heads: { name: 'задача', tick: 'тикет', act: 'статус', time: 'длительность' } }).map((r, i) => ({ r, i })))}
          {f?.merge_error && <Text color={K.nfailed} wrap="truncate-end">{clipLine(f.merge_error)}</Text>}
        </Box>
      )
    }

    // The 5-s samples averaged into 15-s buckets, newest last (the strip draws them from its right edge).
    const buckets = (hist: CpuSample[]): Split6[] => {
      const per = Math.max(1, Math.ceil(HIST / HIST_DRAW))
      const out: Split6[] = []
      for (let end = hist.length; end > 0 && out.length < HIST_DRAW; end -= per) {
        const part = hist.slice(Math.max(0, end - per), end)
        const avg = (k: keyof CpuSample) => part.reduce((a, s) => a + (s[k] ?? 0), 0) / part.length
        out.unshift({ work: avg('work'), agents: avg('agents'), app: avg('app'), tests: avg('tests'), you: avg('you'), unmeasured: avg('unmeasured'), merge: avg('merge') })
      }
      return out
    }
    // The generic machine card (#796 u4b): the title picture and the load picture, an error or `loading...` line while nothing
    // arrived, then the caller's `below` slot (offline notes, the waiting line).
    type HostCard = {
      id: string; name: string; iconName: string; sub: string; m: Machine | undefined; loaded: boolean; err?: string
      state?: { t: string; cls: string }; headState?: { t: string; cls: string }; marks: any; cap: any; hist: any; slotsNote?: boolean; below?: any
    }
    const hostCard = (x: HostCard) => {
      const { id, name, iconName, sub, m, loaded, err, state, headState, marks, cap, hist } = x
      const body = loaded
        ? (Svg
          ? (src => <Svg source={src} alt={m ? `${name} load` : `${name} offline`} height={svgH(src)} />)(machineSvg(name, sub, iconName, m ?? null, marks, cap, m ? buckets(hist ?? []) : [], W, state))
          : (
            <Box flexDirection="column">
              <Box flexDirection="row" gap={1}>
                <Text bold color={K.text}>{name}</Text>
                {sub && <Muted>{sub}</Muted>}
                {state && <Text color={TCOL[state.cls]}>{`· ${state.t}`}</Text>}
              </Box>
              {m && <Text color={K.nmuted}>CPU {Math.round(m.cpu_total)} % · RAM {m.ram_free} GB free{x.slotsNote ? ` · slots ${cap}` : ''}</Text>}
            </Box>
          ))
        : null
      return (
        <Box key={`card-${id}`} flexDirection="column" backgroundColor={K.tile} borderStyle="round" borderColor={K.tile} paddingX={1} marginBottom={1}>
          {body ?? (Svg ? (src => <Svg source={src} alt={name} height={svgH(src)} />)(headSvg([iconName, name], sub, W, headState)) : <Text bold color={K.text}>{name}</Text>)}
          {!loaded && <Text color={err ? K.nfailed : K.nmuted}>{err ? clipLine(err) : 'loading...'}</Text>}
          {x.below}
        </Box>
      )
    }
    // The PC card; its slot squares and waiting line come from the add-on (none: the bare load picture).
    const machineCard = (id: 'pc') => {
      const m = f?.machine
      const err = f?.machine_error ?? errs.fast
      const cores = m?.cores
      const sub = [cores != null ? `${cores} ядер` : '', m ? `${Math.round(m.ram_total)} GB` : ''].filter(Boolean).join(', ')
      const x = A ? A.pcSlots(m) : { marks: [], cap: '—', below: [], slotsNote: false }
      return hostCard({ id, name: 'PC', iconName: 'monitor.octicons', sub, m, loaded: !!m, err, marks: x.marks, cap: x.cap, hist: cpuHist[id], slotsNote: x.slotsNote, below: x.below })
    }

    const A: any = ADDON.make?.({
      fastAtom: { get: () => read($, fast) }, runPickAtom: { set: (fn: any) => update($, runPickAtom, fn) }, runSelAtom: { set: (fn: any) => update($, runSelAtom, fn) }, histPageAtom: { set: (fn: any) => update($, histPageAtom, fn) }, gridPageAtom: { set: (fn: any) => update($, gridPageAtom, fn) }, gridPage,
      bp, runSel, runPick, histPage, Box, Text, Button, Svg, Link, f, t, errs, W, sc, run: (argv: string[], o: any) => $.process.run(argv, o), after: (ms: number, fn: () => void) => $.clock.after(ms, fn), Card, Ctl, Muted, Gap, rowEl, rowsOne, tableEl, tableOf, detailHead, tick, hm, hmDay, svgH,
      queueRows, nestRails, HIST, refreshFast: () => refreshFast?.(), logError: (kind: 'pull' | 'render', err: unknown) => logError($, kind, err), hostCard, cpuHist, Cell, Head, Row, selected,
      press, pctCell, titleOf, Tile, Title, ColTable, tv, fresh, lane, labelPager, cardPager, pg, PAGER_PAD, counterOf, pageFill, COLS, sizeOf, COST, pagesNow, clipLine, unpage, listPage,
      panelAtom: { get: () => read($, panelAtom), set: (fn: any) => update($, panelAtom, fn) }, selectedAtom: { set: (fn: any) => update($, selectedAtom, fn) },
      selectedRd, bpRd, readPanelAtom: { get: () => read($, readPanelAtom), set: (fn: any) => update($, readPanelAtom, fn) }, readSelAtom: { set: (fn: any) => update($, readSelAtom, fn) },
      readBytes: (p: string) => $.fs.read(p, { as: 'bytes' }), copy: (a: any) => $.ui.copy(a),
    })

    // ---------------------------------------------------------- tickets tab
    // #671: a row of the tree with the branch above it (a page that begins inside a branch repeats these rows first)
    type TkRow = { i: Issue; depth: number; anc: TkRow[] }
    const ticketRows = (i: Issue, depth: number, anc: TkRow[], out: TkRow[]) => {
      const row: TkRow = { i, depth, anc }
      out.push(row)
      if (open.has(i.n)) for (const k of i.kids) { const ki = t!.issues[String(k)]; if (ki) ticketRows(ki, depth + 1, [...anc, row], out) }
      return out
    }
    // #671: the chars a page's ticket rows may weigh (the pane around them, ~25k with the tab bar, the legend and the
    // card heads, stays under the desktop's 90,000 guard); a page ends where the next row would cross it, never at a row count.
    // #983: the room is the budget's own (hooks/budget.ts): the target less this tab drawn with no row (the measuring pass), never a literal; a row is charged its own drawn size
    const ticketLine = ({ i, depth }: TkRow, idx: number) => {
      const st = stateOf(i, held)
      const all = subtree(t!, i.n)
      return (
        <Row k={`hv-tk-${i.n}`} i={idx}>
          <Cell w={2 + depth * 2}>
            <Box flexDirection="row">
              <Text>{' '.repeat(depth * 2)}</Text>
              {i.kids.length > 0
                ? <Button key={`tg-${i.n}`} plain label={open.has(i.n) ? '▾' : '▸'} onPress={toggle(i.n)} />
                : <Text> </Text>}
            </Box>
          </Cell>
          <Cell w={2}><Dot st={st} /></Cell>
          {/* #671: a plain link here; the hover card (~1,200 chars a row, the title twice) pushed the tab
              past the desktop's redraw cap once an epic opened (74k -> 115k with #670's 16 children). */}
          <Cell w={6}>{Link ? <Link href={`${GH}${i.n}`} label={`#${i.n}`} /> : <Text color={K.nmuted}>#{i.n}</Text>}</Cell>
          <Cell grow><Text color={i.open ? K.text : K.nmuted} wrap="wrap">{i.title}</Text></Cell>
          <Cell w={8}>
            <Text color={K.nmuted}>{all.length > 0 ? `${all.filter(k => !k.open).length}/${all.length}` : ''}</Text>
          </Cell>
          <Cell w={3}>
            <Text color={K.done}>{held.has(i.n) && i.open ? '◆' : i.blockers.length ? '⧗' : ''}</Text>
          </Cell>
        </Row>
      )
    }
    const ticketHead = (
      <Head>
        <Cell w={4}><Text> </Text></Cell>
        <Cell w={6}><Muted>тикет</Muted></Cell>
        <Cell grow><Muted>название</Muted></Cell>
        <Cell w={8}><Muted>готово</Muted></Cell>
        <Cell w={3}><Text> </Text></Cell>
      </Head>
    )

    const ticketsTab = () => {
      if (!t) return <Muted>{errs.tickets ?? 'загружается'}</Muted>
      const legend: TicketState[] = ['work', 'owner', 'ready', 'blocked', 'open', 'triage', 'closed', 'dropped']
      const goalRows = t.goals.flatMap(n => ticketRows(t.issues[String(n)], 0, [], []))
      const looseRows = t.loose.flatMap(n => ticketRows(t.issues[String(n)], 0, [], []))
      // #683 r5 (E), desktop: each card is THE table of the pane, a tree: the toggle (a native press), the state dot,
      // the number (a native Link) and the title stand two pane columns right a level; the title is the row's content
      // and is never cut: it wraps onto further lines of the picture. In a pane too narrow the held / blocked mark
      // leaves first, then the done count. The legend is one picture of the same dots.
      if (Svg) {
        const IND = 2 * PX_PER_COL
        const SPEC: TFit[] = [
          { key: 'tg', label: '', x: 3 * PX_PER_COL, need: 14, gap: 2 },
          { key: 'dot', label: '', x: 5 * PX_PER_COL, need: 14, gap: 4 },
          { key: 'n', label: 'тикет', x: 8 * PX_PER_COL, gap: 10 },
          { key: 'title', label: 'название', x: 14 * PX_PER_COL, clip: true }, // #994: a title is one line, cut with an ellipsis
          { key: 'done', label: 'готово', x: 0, end: true, w: 40 },
          { key: 'mark', label: '', x: 0, end: true, w: 12 },
        ]
        const cells = ({ i, depth }: TkRow): TCellV[] => {
          const st = iconState(i, held)
          const all = subtree(t, i.n)
          const dx = depth * IND
          return [
            // the toggle: a chevron of the picture (right = closed, turned down = open) under a native press
            i.kids.length > 0 ? { t: BLANK, icon: ['chevron-right.octicons', K.muted], turn: open.has(i.n) ? 90 : 0, press: { key: `tg-${i.n}`, on: toggle(i.n) }, dx } : '',
            { t: '', icon: STATE_ICON[st], dx },
            { t: `#${i.n}`, href: `${GH}${i.n}`, dx },
            { t: i.title, cls: i.open ? 'tx' : 'mu', dx },
            all.length > 0 ? { t: `${all.filter(k => !k.open).length}/${all.length}`, cls: 'mu' } : '',
            // the state already says held / blocked (#1079: names as written): the one fact it cannot hold is a ticket in work, or
            // waiting for the owner, that ALSO waits for other tickets
            // #1075: a parked ticket (its label `parked`, open or closed) takes this mark (the pause icon) over the hourglass
            i.parked ? { t: '', icon: PARKED_ICON } : i.open && i.blockers.length && st !== 'blocked' ? { t: '', icon: STATE_ICON.blocked } : '',
          ]
        }
        // #671: every row of the tree, in order, goals then loose, and what each weighs in the drawn tree: its picture
        // (as the JSON of the tree holds it) and its hover Box with the native cells. A page takes rows while they fit
        // the budget; one that begins inside a branch first repeats the rows above it.
        tkPrep ??= (() => {
          const stream = [...goalRows.map(r => ({ r, loose: false })), ...looseRows.map(r => ({ r, loose: true }))].map((x, j) => ({ ...x, j }))
          const byRow = new Map(stream.map(x => [x.r, x] as const))
          const fitted = tableFit(SPEC, [{ rows: stream.map(x => cells(x.r)) }], ['mark', 'done'], TW)
          // #983: a row's charge = what it adds to a table that already draws rows like it, from ONE drawing of the row alone (a drawing a row, as before #972):
          //   its picture's part = the picture of the row alone less the empty picture, an icon's shape counted as the short reference a table of many rows draws (the shape
          //   itself is drawn once a table: the lane's `extra`, COST.ticketsExtra);
          //   its native part (the keyed Box with the light, the presses) = measured on the element once for every form a row takes (its presses, their columns, its lines);
          //   plus COST.ticketSlack (a later row's coordinates and keys are a few digits longer).
          const svgLen = (svg: string) => JSON.stringify(svg).length
          const base = svgLen(tableSvg(fitted.cols, [{ rows: [] }], TW, true, true).svg)
          const baseEl = sizeOf(tableEl('tkg', fitted.cols, [{ rows: [] }], '', true, [], true))
          const colsKey = JSON.stringify(fitted.cols)
          if (TK_COST.size > 4000) TK_COST.clear()
          if (TK_NATIVE.size > 400) TK_NATIVE.clear()
          const cost = stream.map((x, j) => {
            const row = fitted.groups[0]!.rows[j]!
            const ck = colsKey + JSON.stringify(row)
            let c = TK_COST.get(ck)
            if (!c) {
              const one = tableSvg(fitted.cols, [{ rows: [row] }], TW, true, true)
              const pic = svgLen(one.svg) - base
              let shapes = 0, refs = 0
              for (const m of one.svg.matchAll(/ d='([^']{80,})'/g)) { shapes += m[1]!.length; refs++ }
              const form = colsKey + JSON.stringify([one.hot.map(hp => [hp.press ? 1 : 0, hp.t.length, Math.round(hp.x)]), one.spans[0], String(x.r.i.n).length])
              let native = TK_NATIVE.get(form)
              if (native == null) {
                native = sizeOf(tableEl('tkg', fitted.cols, [{ rows: [row] }], '', true, [`hv-tk-${x.r.i.n}`], true)) - baseEl - pic
                TK_NATIVE.set(form, native)
              }
              c = [pic - shapes + refs * 12 + native + COST.ticketSlack, one.spans[0]!]
              TK_COST.set(ck, c)
            }
            return c
          })
          return { stream, byRow, cost }
        })()
        const { stream, byRow, cost }: { stream: { r: TkRow; loose: boolean; j: number }[]; byRow: Map<TkRow, { r: TkRow; loose: boolean; j: number }>; cost: [number, number][] } = tkPrep
        // the lane only asks the budget for the tab's room: the pages are this tab's own (a page that begins inside a branch repeats the rows above it; a picture's height bounds it too)
        const tkPlan = lane('tk', stream.map((_x, j) => cost[j]![0]), { extra: COST.ticketsExtra })
        const probing = mode === 'probe' || (mode === 'whole' && wholeOver) // a pass that draws no row
        const TK_BUDGET = mode === 'whole' ? Infinity : Math.max(0, tkPlan.grant - COST.pager - COST.ticketsExtra)
        // a table's picture is at most 4,096 px high (the engine refuses a taller one): its lines bound a page too
        const TK_LINES = Math.floor(4000 / TROW) - 1
        type Pg = { rows: { x: typeof stream[number]; again: boolean }[]; fresh: number; used: number; lines: [number, number] }
        const pages: Pg[] = []
        const fits = (pg: Pg, x: typeof stream[number]) => pg.used + cost[x.j]![0] <= TK_BUDGET && pg.lines[+x.loose] + cost[x.j]![1] <= TK_LINES
        const put = (pg: Pg, x: typeof stream[number], again: boolean) => { pg.rows.push({ x, again }); pg.used += cost[x.j]![0]; pg.lines[+x.loose] += cost[x.j]![1]; if (!again) pg.fresh++ }
        for (const x of stream) {
          let pg = pages[pages.length - 1]
          if (!pg || !fits(pg, x)) {
            pg = { rows: [], fresh: 0, used: 0, lines: [0, 0] }
            pages.push(pg)
            for (const a of x.r.anc) put(pg, byRow.get(a)!, true)
          }
          put(pg, x, false)
        }
        const page = Math.min(tkPage, Math.max(0, pages.length - 1))
        // the measuring pass draws no row (the tab's fixed part); the drawn page's own weight is what the tab is charged
        const shown = probing ? [] : pages[page]?.rows ?? []
        if (!probing) { ownPages.tk = pages.length; ownCharge.tk = shown.length ? COST.pager + COST.ticketsExtra + pages[page]!.used : 0 }
        const first = pages.slice(0, page).reduce((a, p) => a + p.fresh, 0)
        // #837 b9a: a press decides at PRESS time, from the atom's value (the host may call the handler of an older render): a step, not a page number computed in the render
        const setPage = (d: number) => () => void update($, tkPageAtom, x => { const m = pagesNow('tk', pages.length); return Math.max(0, Math.min(m - 1, Math.min(x, m - 1) + d)) })
        const pager = !probing && pages.length > 1 && cornerPager('tk', `${first + 1}–${first + pages[page]!.fresh} из ${stream.length}`, { on: setPage(-1), dim: page === 0 }, { on: setPage(1), dim: page === pages.length - 1 })
        const table = (k: string, name: string, rows: TkRow[]) =>
          tableOf(k, SPEC, rows.map(cells), ['mark', 'done'], name, rows.map(r => `hv-tk-${r.i.n}`), true)
        const lg = legendSvg(legend.map(st => ({ icon: STATE_ICON[st], t: STATE_WORD[st] })), W)
        const goalsHere = shown.filter(y => !y.x.loose).map(y => y.x.r)
        const looseHere = shown.filter(y => y.x.loose).map(y => y.x.r)
        return (
          <Box flexDirection="column">
            <Box flexDirection="column" marginBottom={1}><Svg source={lg} alt={legend.map(st => STATE_WORD[st]).join(' · ')} height={svgH(lg)} /></Box>
            {(goalsHere.length > 0 || page === 0 || probing) && (
              <Card k="tk-goals" name="Цели">
                {pager}
                {table('tkg', 'Цели', goalsHere)}
              </Card>
            )}
            <Card k="tk-loose" name="Вне целей">
              {goalsHere.length === 0 && page > 0 && pager}
              {table('tkl', 'Вне целей', looseHere)}
            </Card>
            <Muted>{fresh(t.at)}{errs.tickets ? ` · ${errs.tickets}` : ''}</Muted>
          </Box>
        )
      }
      return (
        <Box flexDirection="column">
          <Box flexDirection="row" flexWrap="wrap" marginBottom={1}>
            {legend.filter(st => st !== 'dropped').map(st => <Text color={STATE_DOT[st as keyof typeof STATE_DOT][1]}>{STATE_DOT[st as keyof typeof STATE_DOT][0]} <Text color={K.nmuted}>{STATE_WORD[st]}  </Text></Text>)}
          </Box>
          <Tile>
            <Title>Цели</Title>
            {ticketHead}
            {goalRows.map(ticketLine)}
          </Tile>
          <Tile>
            <Title>Вне целей</Title>
            {ticketHead}
            {looseRows.map(ticketLine)}
          </Tile>
          <Muted>{fresh(t.at)}{errs.tickets ? ` · ${errs.tickets}` : ''}</Muted>
        </Box>
      )
    }

    // #837 pass 2: the Cost cards' table is the pane's one table: every row its own hover box (tableOf), as at 50ea5412
    const costTable = (k: string, spec: TFit[], rows: TCellV[][], drop: string[], alt: string, keys: string[]) => tableOf(k, spec, rows, drop, alt, keys)
    // ---------------------------------------------------------- slow tabs
    // #683 r5 step 1b: ONE population (the collector's `cut` of the period) cut six ways, each cut a card that is THE
    // table of the pane: what the row is about first, then, in the same trailing columns in every card, the share bar,
    // `% лимита` (the cost as a share of the weekly limit, the plan price's API-equivalent a percent), `$` and `токенов`.
    const costTab = () => {
      const c = s?.[`cost_${costMode}` as 'cost_day' | 'cost_week' | 'cost_month']
      const cut: CostCut | undefined = c?.cut
      const MODES: [CostMode, string][] = [['day', 'День'], ['week', 'Неделя'], ['month', 'Месяц']]
      // #972 (the owner, 13.10: «ОКей 1»): the twelve tables in three groups behind a second switch under the period's, one group drawn at a time
      const GROUPS: [string, string][] = [['roles', 'Роли и модели'], ['spawn', 'Кто кого запускает'], ['days', 'Дни и тикеты']]
      const group = GROUPS.some(g => g[0] === costGroup) ? String(costGroup) : 'roles'
      // another period or group: every table starts from its first page
      const firstPages = () => void update($, listPageAtom, (x: Record<string, number>) => Object.fromEntries(Object.entries(x).filter(([k]) => !k.startsWith('cost-'))))
      // #994 (defect 1): the two switches are pictures of capsules, 4 px apart (their selected fills touched)
      const switcher = Svg ? switchPic([
        MODES.map(([m, label]) => ({ key: `cost-${m}`, label, sel: costMode === m, go: () => { void update($, costModeAtom, () => m); firstPages() } })),
        GROUPS.map(([g, label]) => ({ key: `costg-${g}`, label, sel: group === g, go: () => { void update($, costGroupAtom, () => g); firstPages() } })),
      ]) : (
        <Box flexDirection="column">
          <Box flexDirection="row" gap={1}>
            {MODES.map(([m, label]) => (
              <Ctl k={`cost-${m}`} label={label} main={costMode === m} sel={costMode === m} dim={costMode !== m} go={() => { void update($, costModeAtom, () => m); firstPages() }} />
            ))}
          </Box>
          <Box flexDirection="row" gap={1}>
            {GROUPS.map(([g, label]) => (
              <Ctl k={`costg-${g}`} label={label} sel={group === g} dim={group !== g} go={() => { void update($, costGroupAtom, () => g); firstPages() }} />
            ))}
          </Box>
        </Box>
      )
      if (!cut) {
        // D2 (#793): an error is a card (title + one-row table), never a bare repr
        const why = errs.slow ?? c?.cut_error
        return (
          <Box flexDirection="column">
            {switcher}
            <Gap />
            {why
              ? (
                <Card k="cost-error" iconName="failed.octicons" name="Расходы не посчитаны">
                  {Svg
                    ? tableOf('c-error', [{ key: 'why', label: 'причина', x: P0 }], [[{ t: fit(String(why), TW - 2 * P0, PX), cls: 'er' }]], [], 'Расходы не посчитаны', false)
                    : <Text color={TCOL.er}>{String(why)}</Text>}
                </Card>
              )
              : <Muted>загружается</Muted>}
          </Box>
        )
      }
      const total = cut.total.cost_usd
      const allTok = Object.values(cut.total.tokens).reduce((a: number, b: number) => a + b, 0)
      const p = cut.total.limit_pct
      const per = cut.total.limit_usd_per_pct // #1094: the plan price; null = no plan, so no money figure of the limit is drawn
      // #995: the share of the weekly limit always in its unit, the percent of the table's «% лимита» column under it (was «≈ 5.2 недельного лимита» from 150 % up: a number with no unit)
      // #1018 A: the month (limit_days 30) is against a month's limit; a day, a week, or a payload with no limit_days, against the weekly one
      const lim = p == null ? null : `≈ ${thousands(Math.round(p))} % ${(cut.total.limit_days ?? 7) > 7 ? 'месячного' : 'недельного'} лимита`
      const headLine = [usd(total), lim, `${tokH(allTok)} токенов`].filter(Boolean).join(' · ')
      const period = ddmm(cut.since) === ddmm(cut.until) ? ddmm(cut.since) : `${ddmm(cut.since)} – ${ddmm(cut.until)}`
      type R = { name: string; cost: number; tokens: number; note?: string; n?: number; extra?: TCellV[]; child?: boolean; noBar?: boolean }
      type Ex = { key: string; label: string }
      const sum = (xs: { cost_usd: number }[]) => xs.reduce((a, x) => a + x.cost_usd, 0)
      const subNote = (v: number) => (v >= 0.005 ? `субагенты ${usd(v)}` : '')
      const mu = (v: string): TCellV => (v ? { t: v, cls: 'mu' } : '')
      // «По чатам»: only the chats (a titled chat, the untitled ones); the roles and the mods are in «По ролям».
      const chatRows = cut.by_who.filter(x => x.kind === 'chat' || x.kind === 'untitled')
      const whoName = (x: CostCut['by_who'][number]) => (x.kind === 'untitled' ? 'чаты без названия' : x.key.split(/\s+/).filter(Boolean).join(' '))
      const top = chatRows.slice(0, 10)
      const others = chatRows.slice(10)
      const who: R[] = top.map(x => ({ name: whoName(x), cost: x.cost_usd, tokens: x.tokens, note: subNote(x.sub_usd) }))
      if (others.length) who.push({ name: `остальные (${others.length})`, cost: sum(others), tokens: others.reduce((a, x) => a + x.tokens, 0), note: subNote(others.reduce((a, x) => a + x.sub_usd, 0)) })
      const chatSum = sum(chatRows)
      // «По ролям»
      const ROLE_NAME: Record<string, string> = { orch: 'ORCH', other_sub: 'другие субагенты', other_chat: 'другие чаты', mod: 'моды' }
      const roleName = (k: string) => ROLE_NAME[k] ?? k
      const countWord = (x: { n: number; group: string }) =>
        `${thousands(x.n)} ${x.group === 'main' ? plural(x.n, 'сессия', 'сессии', 'сессий') : x.group === 'sub' ? plural(x.n, 'субагент', 'субагента', 'субагентов') : plural(x.n, 'вызов', 'вызова', 'вызовов')}`
      const roles: R[] = cut.by_role.map(x => ({ name: roleName(x.key), cost: x.cost_usd, tokens: x.tokens, note: countWord(x), extra: [mu(countWord(x))] }))
      // the three spawners: itself, then whom it starts
      const SPAWN_EX: Ex[] = [{ key: 'n', label: 'субагентов' }, { key: 'per', label: 'за сессию' }, ...(cut.days > 1 ? [{ key: 'day', label: 'в день' }] : [])]
      const spawn = (key: string) => {
        const sp = cut.by_spawner.find(x => x.key === key)
        if (!sp) return { sub: undefined as string | undefined, rows: [] as R[], total: 0 }
        const rows: R[] = [{ name: 'сам', cost: sp.own.cost_usd, tokens: sp.own.tokens, extra: ['', '', ''] },
          ...sp.roles.map((r): R => ({
            name: roleName(r.key), cost: r.cost_usd, tokens: r.tokens, note: thousands(r.agents),
            extra: [mu(thousands(r.agents)), mu(rate(r.agents / sp.sessions)), mu(rate(r.agents / cut.days))],
          }))]
        return { sub: `${thousands(sp.sessions)} ${plural(sp.sessions, 'сессия', 'сессии', 'сессий')} · всего ${usd(sp.cost_usd)}`, rows, total: sp.cost_usd }
      }
      const spOrch = spawn('orch'), spAuto = spawn('autopilot'), spBrain = spawn('brain')
      // «Моды»
      const MOD_EX: Ex[] = [{ key: 'model', label: 'модель' }, { key: 'calls', label: 'вызовов' }, ...(cut.days > 1 ? [{ key: 'day', label: 'в день' }] : [])]
      const modName = (k: string) => (k === 'monitor' ? 'монитор' : k)
      const modSum = sum(cut.by_mod)
      const mods: R[] = cut.by_mod.map(x => ({
        name: modName(x.key), cost: x.cost_usd, tokens: x.tokens, note: `${modelName(x.model)} · ${thousands(x.calls)}`,
        extra: [mu(modelName(x.model)), mu(thousands(x.calls)), mu(rate(x.calls / cut.days))],
      }))
      // role x model (collector's by_role_model): a row with no tokens and no cost is drawn nowhere
      const rmAll = cut.by_role_model ?? []
      const rmKept = rmAll.filter(x => x.tokens > 0 || x.cost_usd > 0)
      const rmName = (x: { role: string; group: string }) => (x.group === 'mod' ? modName(x.role) : roleName(x.role))
      // «По моделям: кто вызывает»: a model, then under it who calls it
      const models: R[][] = [] // a group = the model's row, then one row per role on it
      for (const x of cut.by_model.filter(m => m.tokens > 0 || m.cost_usd > 0)) {
        models.push([{ name: modelName(x.key), cost: x.cost_usd, tokens: x.tokens, note: x.unpriced_turns > 0 && x.cost_usd === 0 ? 'нет цены' : '' },
          ...rmKept.filter(r => r.model === x.key).map((r): R => ({ name: rmName(r), cost: r.cost_usd, tokens: r.tokens, note: countWord(r), extra: [mu(countWord(r))], child: true }))])
      }
      // «Роль на разных моделях»
      const ROLE_ORDER = ADDON.roleOrder ?? ['brain', 'orch', 'autopilot', 'worker', 'worker-strong', 'researcher-find', 'researcher-how', 'reviewer-standards', 'reviewer-spec', 'other_sub', 'other_chat', 'monitor']
      const roleRank = (r: string) => { const i = ROLE_ORDER.indexOf(r); return i < 0 ? ROLE_ORDER.length : i }
      const byFirst = (a: { first: string | null; model: string }, b: { first: string | null; model: string }) =>
        (a.first == null ? 1 : 0) - (b.first == null ? 1 : 0) || (a.first ?? '').localeCompare(b.first ?? '') || a.model.localeCompare(b.model)
      const rmByRole = [...rmKept].sort((a, b) => roleRank(a.role) - roleRank(b.role) || (roleRank(a.role) === ROLE_ORDER.length ? a.role.localeCompare(b.role) : 0) || byFirst(a, b))
      const span = (x: { first: string | null; last: string | null }) =>
        (x.first == null ? '' : x.first === x.last || x.last == null ? ddmm(x.first) : `${ddmm(x.first)} – ${ddmm(x.last)}`)
      const daysIn = (x: { first: string | null; last: string | null }) =>
        (x.first == null ? 1 : Math.max(1, Math.round((Date.parse(x.last ?? x.first) - Date.parse(x.first)) / 86400000) + 1))
      const perRun = (v: number) => (v <= 0 ? '$0' : v < 0.0005 ? '<$0.001' : v < 0.1 ? `$${v.toFixed(3)}` : usd(v)) // #837 b3: above 0 and under 0.0005 is `<$0.001`, never `$0.000`; 0 is `$0`
      // «ORCH на разных моделях: результат»
      const orchRm = rmKept.filter(x => x.role === 'orch' && x.led > 0).sort(byFirst)
      const orchCost = (x: (typeof orchRm)[number]) => x.cost_usd + x.spawned.cost_usd
      const KIND_WORD: Record<string, string> = { input: 'ввод', cache_write: 'запись в кэш', cache_read: 'чтение из кэша', output: 'вывод' }
      const kinds: R[] = [...cut.by_kind].sort((a, b) => b.cost_usd - a.cost_usd).map(x => ({ name: KIND_WORD[x.key] ?? x.key, cost: x.cost_usd, tokens: x.tokens }))
      const allDays: R[] = cut.by_day.map(x => ({ name: ddmm(x.key), cost: x.cost_usd, tokens: x.tokens }))
      // #995 (row 6): «По дням» draws every day of the period, newest first (zero days kept), in every period: the month's 30 rows fit its group (the old cut to the 7 dearest
      // and «остальные (N)» is gone); the budget pages the card when it must (`card` below, a lane)
      const days: R[] = [...allDays].map((d, i) => ({ d, i, k: cut.by_day[i]!.key })).sort((x, y) => (x.k < y.k ? 1 : x.k > y.k ? -1 : x.i - y.i)).map(x => x.d)
      const dayMax = Math.max(0, ...allDays.map(d => d.cost))
      const tickets: R[] = cut.by_issue.map(x => ({ name: rowIssue(t, x)?.title ?? '', cost: x.cost_usd, tokens: x.tokens, n: x.number }))
      let kk = 0 // never reset: the rows' hover keys stay unique across the ten tables
      const keys = (n: number) => Array.from({ length: n }, () => `hv-cost-${kk++}`)
      const limitLine = [per == null ? '' : `1 % недельного лимита ≈ $${per} по ценам API`, fresh(s!.at)].filter(Boolean).join(' · ') // #774: the tab's ONE muted line
      // #683 r5 (E), desktop: every card is THE table of the pane; the trailing columns are the same in all ten. In a
      // pane too narrow the extra count columns leave first (the last one first), then the note, the tokens, the share
      // of the limit and the bar.
      if (Svg) {
        const BAR: TFit = { key: 'bar', label: 'доля', x: 0, share: BAR_W }
        const PCT: TFit = { key: 'pct', label: '% лимита', x: 0, end: true, w: 64 }
        // #1094 carried finding: with no plan price the % column leaves every table (its head and its cells), it is not blanked
        const PCT_COL: TFit[] = per == null ? [] : [PCT]
        const USD: TFit = { key: 'usd', label: '$', x: 0, end: true, w: 60 }
        const TOK: TFit = { key: 'tok', label: 'токенов', x: 0, end: true, w: 60 }
        const nums = (r: R): TCellV[] => [...(per == null ? [] : [mu(limPct(r.cost, per))]), usd(r.cost), { t: tokH(r.tokens), cls: 'mu' }]
        // #972: every table's rows go through the budget (`lane`): the rows of its page; the pager stands in the card's corner only when the group does not fit whole
        // a row is charged from its own cells (hooks/budget.ts `tableCost`): the words it holds, the bar, a ticket's press
        const paged = <T,>(k: string, all: T[], cost: (r: T) => number) => {
          const pl = lane(`cost-${k}`, all.map(cost), { extra: COST.tableExtra })
          return { pl, rows: pl.shown.map(s => all[s.i]!) }
        }
        const card = (k: string, title: string, label: string, all: R[], den: number, opt: { sub?: string; wrap?: boolean; extras?: Ex[]; groups?: R[][] } = {}) => {
          const ex = opt.extras ?? []
          const hasNote = ex.length === 0 && all.some(r => r.note)
          const spec: TFit[] = [{ key: 'name', label, x: P0, ...(opt.wrap ? { clip: true } : {}) },
            ...ex.map((e): TFit => ({ key: e.key, label: e.label, x: 0, end: true })),
            ...(hasNote ? [{ key: 'note', label: '', x: 0, end: true } as TFit] : []), BAR, ...PCT_COL, USD, TOK]
          const cellsOf = (r: R): TCellV[] => [r.child ? { t: r.name, cls: 'mu', dx: 2 * PX_PER_COL } : r.name, ...ex.map((_, i) => r.extra?.[i] ?? ''), ...(hasNote ? [mu(r.note ?? '')] : []), r.noBar ? '' : { t: '', share: den > 0 ? r.cost / den : 0 }, ...nums(r)]
          const costOf = (r: R) => tableCost(cellsOf(r))
          // `groups`: a group's head never parts from the rows under it: a unit of the lane; the rows of a group that do not fit a page are ONE row that sums them
          const restOf = (cutK: R[]): R => ({ name: `остальные (${cutK.length})`, cost: cutK.reduce((a, r) => a + r.cost, 0), tokens: cutK.reduce((a, r) => a + r.tokens, 0), child: true, extra: [''] })
          const pl = opt.groups
            ? lane(`cost-${k}`, opt.groups.map((g): Unit => ({ own: costOf(g[0]!), kids: g.slice(1).map(costOf), more: costOf(restOf(g.slice(1))) })), { extra: COST.tableExtra })
            : paged(k, all, costOf).pl
          const rows = opt.groups
            ? pl.shown.flatMap(s => { const g = opt.groups![s.i]!, kids = g.slice(1), cutK = kids.slice(s.kids); return [g[0]!, ...kids.slice(0, s.kids), ...(cutK.length ? [restOf(cutK)] : [])] })
            : pl.shown.map(s => all[s.i]!)
          return (
            <Card k={`cost-${k}`} name={title} sub={opt.sub}>
              {cardPager(`cost-${k}`, pl)}
              {costTable(`c-${k}`, spec, rows.map(cellsOf),
                [...ex.map(e => e.key).reverse(), 'note', 'tok', 'pct', 'bar'], title, keys(rows.length))}
            </Card>
          )
        }
        const E = (key: string, label: string): TFit => ({ key, label, x: 0, end: true })
        const RM_SPEC: TFit[] = [{ key: 'role', label: 'роль', x: P0 }, { key: 'model', label: 'модель', x: 0 }, { key: 'days', label: 'дни', x: 0 },
          E('runs', 'запусков'), E('day', 'в день'), E('costper', '$ за запуск'), E('tokper', 'токенов за запуск'), E('usd', '$')]
        const OM_SPEC: TFit[] = [{ key: 'model', label: 'модель', x: P0 }, { key: 'days', label: 'дни', x: 0 },
          E('sess', 'сессий'), E('agents', 'субагентов за сессию'), E('costper', '$ за сессию'), E('closed', 'тикетов закрыто'), E('perticket', '$ за тикет')]
        // #972 (the owner, 13.10): the tickets card keeps every ticket the collector sends (its twenty dearest) in every period: the month's cut to ten is gone, the budget pages the card when it must
        const rest = cut.issue_rest, none = cut.no_issue
        const byN = new Map(cut.by_issue.map(x => [x.number, x]))
        const tAll: { cells: TCellV[]; cost: number }[] = tickets.map(r => {
          const i = rowIssue(t, byN.get(r.n!)!)
          const cells: TCellV[] = [i ? { t: '', icon: STATE_ICON[iconState(i, held)] } : '', { t: `#${r.n}`, card: r.n }, r.name, ...nums(r)]
          return { cost: tableCost(cells), cells }
        })
        const tail = (cells: TCellV[]) => tAll.push({ cost: tableCost(cells), cells })
        if (rest.n > 0) tail(['', '', `остальные тикеты (${rest.n})`, ...nums({ name: '', cost: rest.cost_usd, tokens: rest.tokens })])
        tail(['', '', 'без тикета', ...nums({ name: '', cost: none.cost_usd, tokens: none.tokens })])
        const TSPEC: TFit[] = [
          { key: 'dot', label: '', x: 3 * PX_PER_COL, need: 14, gap: 4 },
          { key: 'n', label: 'тикет', x: 5 * PX_PER_COL, gap: 10 },
          { key: 'title', label: 'название', x: 9 * PX_PER_COL, clip: true }, ...PCT_COL, USD, TOK, // #994: a title is one line, cut with an ellipsis
        ]
        return (
          <Box flexDirection="column">
            {switcher}
            <Gap />
            <Box flexDirection="row">
              <Muted>{period}</Muted>
              <Box flexGrow={1} />
              <Text bold color={K.text}>{headLine}</Text>
            </Box>
            <Gap />
            {group === 'roles' && card('role', 'По ролям', 'роль', roles, total, { extras: [{ key: 'cnt', label: 'сколько' }] })}
            {group === 'roles' && (() => {
              const rmCells = (x: (typeof rmByRole)[number], first: boolean): TCellV[] => {
                const d = daysIn(x)
                return [first ? rmName(x) : '', modelName(x.model), span(x),
                  thousands(x.n), x.n > 0 ? rate(x.n / d) : '', x.n > 0 ? perRun(x.cost_usd / x.n) : '', x.n > 0 ? tokH(Math.round(x.tokens / x.n)) : '', usd(x.cost_usd)]
              }
              const { pl, rows } = paged('rolemodel', rmByRole, x => tableCost(rmCells(x, true)))
              return (
                <Card k="cost-rolemodel" name="Роль на разных моделях">
                  {cardPager('cost-rolemodel', pl)}
                  {/* the role is named on the first row of its group and on the first row of a page */}
                  {costTable('c-rolemodel', RM_SPEC, rows.map((x, i) => rmCells(x, i === 0 || rows[i - 1]!.role !== x.role)), ['day', 'days', 'tokper'], 'Роль на разных моделях', keys(rows.length))}
                </Card>
              )
            })()}
            {group === 'spawn' && card('orch', 'ORCH: сам и кого запускает', 'кто', spOrch.rows, spOrch.total, { sub: spOrch.sub, extras: SPAWN_EX })}
            {group === 'spawn' && (() => {
              const omCells = (x: (typeof orchRm)[number]): TCellV[] => [modelName(x.model), span(x), thousands(x.led), rate(x.spawned.agents / x.led), usd(orchCost(x) / x.led),
                x.closed == null ? '' : thousands(x.closed), x.closed ? usd(orchCost(x) / x.closed) : '']
              const { pl, rows } = paged('orchmodel', orchRm, x => tableCost(omCells(x)))
              return (
                <Card k="cost-orchmodel" name="ORCH на разных моделях: результат">
                  {cardPager('cost-orchmodel', pl)}
                  {costTable('c-orchmodel', OM_SPEC, rows.map(omCells), ['days', 'agents'], 'ORCH на разных моделях: результат', keys(rows.length))}
                </Card>
              )
            })()}
            {group === 'spawn' && card('auto', 'autopilot: сам и кого запускает', 'кто', spAuto.rows, spAuto.total, { sub: spAuto.sub, extras: SPAWN_EX })}
            {group === 'spawn' && card('brain', 'brain: сам и кого запускает', 'кто', spBrain.rows, spBrain.total, { sub: spBrain.sub, extras: SPAWN_EX })}
            {group === 'spawn' && card('mod', 'Моды', 'мод', mods, modSum, { sub: `${usd(modSum)} из ${usd(total)}`, extras: MOD_EX })}
            {group === 'spawn' && card('who', 'По чатам', 'чат', who, chatSum, { sub: `${usd(chatSum)} из ${usd(total)}`, wrap: true })}
            {group === 'roles' && card('model', 'По моделям: кто вызывает', 'модель / роль', models.flat(), total, {
              extras: [{ key: 'cnt', label: 'сколько' }], groups: models, // a model never parts from the roles under it
            })}
            {group === 'roles' && card('kind', 'По виду токенов', 'вид', kinds, total)}
            {group === 'days' && card('day', 'По дням', 'день', days, dayMax)}
            {group === 'days' && (() => {
              const { pl, rows } = paged('issue', tAll, r => r.cost)
              return (
                <Card k="cost-issue" name="По тикетам">
                  {cardPager('cost-issue', pl)}
                  {costTable('c-issue', TSPEC, rows.map(r => r.cells), ['tok', 'pct'], 'По тикетам', keys(rows.length))}
                </Card>
              )
            })()}
            <Muted>{limitLine}</Muted>
          </Box>
        )
      }
      const plain = (title: string, label: string, rows: R[], sub?: string) => (
        <Tile>
          <Row>
            <Title>{title}</Title>
            {sub && <Muted>{` ${sub}`}</Muted>}
          </Row>
          <Head>
            <Cell w={9}><Muted>$</Muted></Cell>
            {per != null && <Cell w={11}><Muted>% лимита</Muted></Cell>}
            <Cell grow><Muted>{label}</Muted></Cell>
            <Cell w={9}><Muted>токенов</Muted></Cell>
          </Head>
          {rows.map((r, i) => (
            <Row i={i} k={`hv-cost-${kk++}`}>
              <Cell w={9}><Text color={K.text}>{usd(r.cost)}</Text></Cell>
              {per != null && <Cell w={11}><Text color={K.nmuted}>{limPct(r.cost, per)}</Text></Cell>}
              <Cell grow>
                {r.n != null
                  ? <Row><Cell w={6}><IssueLink n={r.n} /></Cell><Cell grow><Text color={K.text} wrap="wrap">{r.name}</Text></Cell></Row>
                  : <Text color={K.text} wrap="wrap">{r.name}{r.note ? ` · ${r.note}` : ''}</Text>}
              </Cell>
              <Cell w={9}><Text color={K.nmuted}>{tokH(r.tokens)}</Text></Cell>
            </Row>
          ))}
        </Tile>
      )
      const tail: R[] = []
      if (cut.issue_rest.n > 0) tail.push({ name: `остальные тикеты (${cut.issue_rest.n})`, cost: cut.issue_rest.cost_usd, tokens: cut.issue_rest.tokens })
      tail.push({ name: 'без тикета', cost: cut.no_issue.cost_usd, tokens: cut.no_issue.tokens })
      return (
        <Box flexDirection="column">
          {switcher}
          <Gap />
          <Row>
            <Cell grow><Muted>{period}</Muted></Cell>
            <Text bold color={K.text}>{headLine}</Text>
          </Row>
          <Gap />
          {plain('По ролям', 'роль', roles)}
          {plain('Роль на разных моделях', 'роль · модель', rmByRole.map((x): R => ({ name: `${rmName(x)} · ${modelName(x.model)}`, cost: x.cost_usd, tokens: x.tokens, note: `${countWord(x)}, ${span(x)}` })))}
          {plain('ORCH: сам и кого запускает', 'кто', spOrch.rows, spOrch.sub)}
          {plain('ORCH на разных моделях: результат', 'модель', orchRm.map((x): R => ({ name: modelName(x.model), cost: orchCost(x), tokens: x.tokens + x.spawned.tokens,
            note: `${thousands(x.led)} ${plural(x.led, 'сессия', 'сессии', 'сессий')}, закрыто ${x.closed == null ? 'нет данных' : thousands(x.closed)}` })))}
          {plain('autopilot: сам и кого запускает', 'кто', spAuto.rows, spAuto.sub)}
          {plain('brain: сам и кого запускает', 'кто', spBrain.rows, spBrain.sub)}
          {plain('Моды', 'мод', mods, `${usd(modSum)} из ${usd(total)}`)}
          {plain('По чатам', 'чат', who, `${usd(chatSum)} из ${usd(total)}`)}
          {plain('По моделям: кто вызывает', 'модель / роль', models.flat().map(r => (r.child ? { ...r, name: `  ${r.name}` } : r)))}
          {plain('По виду токенов', 'вид', kinds)}
          {plain('По дням', 'день', days)}
          {plain('По тикетам', 'тикет', [...tickets, ...tail])}
          <Muted>{limitLine}</Muted>
        </Box>
      )
    }

    // #6: «Пайплайн», above the two count tables: one line per ticket of the collector's `pipeline` (already sorted): the ticket, its strip of stages,
    // its stage's word with its icon, the time since its start and the time at its stage. The lines are a lane (`pipe`): a page of them when they do not
    // fit, the pager on the label row as the Merge card's. No rows (no key, or an empty list): no section.
    const pipeSection = () => {
      const rows = f?.pipeline ?? []
      if (!rows.length) return null
      const plan = lane('pipe', rows.map(() => COST.pipeLine), { extra: COST.pipeExtra })
      const now = Date.now()
      const meta = COLS.find(c => c.key === 'meta')!
      const lines: RowSpec[] = plan.shown.map(s => rows[s.i]!).map((r): RowSpec => {
        const w0 = pipeWord(r)
        // #151: a row with units (the payload's `units`, null or absent when the ticket has none) adds «k/n» after its stage word
        const w = r.units ? { ...w0, t: `${w0.t} · ${r.units.done}/${r.units.total}` } : w0
        return {
          kind: 'strip', state: r.failed ? 'failed' : r.stage === 'ready' ? 'wait' : 'active', calm: r.stage === 'ready', alt: `#${r.number} ${r.title}: ${w.t}`,
          rail: { dot: true, color: w.color },
          cells: {
            name: { t: r.title, cls: 'tx' }, tick: tick(r.number),
            meta: { t: '', kind: pipeStripKind(r.stage, w.color, r.failed, meta.x, meta.w) },
            num: pipeSince(r.started_at, now),
            act: { t: w.t, cls: r.failed ? 'er' : 'tx', icon: w.icon },
            // #171: a waiting row's right column is the time left to its date (past: «срок вышел»); no readable date, the time at its stage
            time: r.wait?.until && Number.isFinite(Date.parse(r.wait.until)) ? pipeLeft(r.wait.until, now) : pipeSince(r.stage_since, now),
          },
        }
      })
      const items: { r: RowSpec; i: number }[] = [
        { r: { kind: 'head', cells: { name: 'задача', tick: 'тикет', meta: 'этап', num: 'с начала', act: 'статус', time: 'на этапе' } }, i: 0 },
        { r: { kind: 'label', text: 'Тикеты', ...labelPager('pipe', plan) }, i: 1 },
        ...lines.map((r, k) => ({ r, i: k + 2 })),
      ]
      return (
        <Card k="prog-pipe" name="Пайплайн" state={{ t: String(rows.length), cls: 'nm' }}>
          {rowsOne('pipe', items)}
        </Card>
      )
    }

    const progressTab = () => {
      if (!t) return <Muted>{errs.tickets ?? 'загружается'}</Muted>
      const openIssues = Object.values(t.issues).filter(i => i.open)
      const rows = progressLines(t.issues) // #841: every open ticket on ONE line
      const pipe = pipeSection()

      const total = openIssues.length
      // #683 r5 (E), desktop: the card's total on its title line, the shares as THE table of the pane: the label, the
      // count, the share as the table's own bar. In a pane too narrow the bar leaves (the count says it).
      // (#983: the table's rows are `progressLines`' ten fixed lines, never a list that grows: no lane)
      // #995 (the owner, 14.10: «В табке прогресс расставить все более по порядку ... Должны быть что по тикетам и все остальное (папки и тд)»): two cards in this order,
      // «По тикетам» (every line that is a ticket's state) and «Остальное» (the folders: a map, an epic, a ticket with open sub-tickets), each a title, its count and a table
      // with a head. An empty card draws its head only.
      // #995 send-back 1 (defect 2): a row's «доля» is its share of ITS OWN card's total (the number on the card's title line), not of both cards' sum.
      // #995 send-back 1 (defect 4): a count row opens nothing, so it lights nothing: the table gets no row Boxes (`keys` false).
      const groups: [string, string, [string, number][]][] = [
        ['prog-tickets', 'По тикетам', rows.filter(([label]) => !PROGRESS_OTHER.includes(label))],
        ['prog-other', 'Остальное', rows.filter(([label]) => PROGRESS_OTHER.includes(label))],
      ]
      if (Svg) return (
        <Box flexDirection="column">
          {pipe}
          {groups.map(([k, name, gr]) => {
            const own = gr.reduce((a, [, n]) => a + n, 0)
            return (
              <Card k={k} name={name} state={{ t: String(own), cls: 'nm' }}>
                {tableOf(k === 'prog-tickets' ? 'pl' : 'po', [{ key: 'dot', label: '', x: P0, need: 14, gap: 4 }, { key: 'label', label: 'метка', x: P0 + 18 }, { key: 'n', label: 'тикетов', x: 0, end: true, w: 32 }, { key: 'share', label: 'доля', x: 0, share: 240 }],
                  gr.map(([label, n]): TCellV[] => [{ t: '', icon: LABEL_ICON[label]! }, LABEL_WORD[label] ?? label, String(n), { t: '', share: own ? n / own : 0 }]), ['share'], name, false)}
              </Card>
            )
          })}
          <Muted>{fresh(t.at)}</Muted>
        </Box>
      )
      const W = 24
      return (
        <Box flexDirection="column">
          {pipe}
          <Tile>
            <Row>
              <Cell grow><Title>Открытые тикеты по меткам</Title></Cell>
              <Text bold color={K.text}>{total}</Text>
            </Row>
            <Head>
              <Cell w={18}><Muted>метка</Muted></Cell>
              <Cell w={9}><Muted>тикетов</Muted></Cell>
              <Cell grow><Muted>доля</Muted></Cell>
            </Head>
            {rows.map(([label, n], i) => {
              const fill = total ? Math.round((n / total) * W) : 0
              return (
                <Row i={i} k={`hv-prog-${label}`}>
                  <Cell w={18}><Text color={K.text}>{LABEL_WORD[label] ?? label}</Text></Cell>
                  <Cell w={9}><Text color={K.text}>{n}</Text></Cell>
                  <Cell grow>
                    <Box flexDirection="row">
                      <Text color={K.accent}>{'█'.repeat(fill)}</Text>
                      <Text color={K.rule}>{'█'.repeat(W - fill)}</Text>
                    </Box>
                  </Cell>
                </Row>
              )
            })}
          </Tile>
          <Muted>{fresh(t.at)}</Muted>
        </Box>
      )
    }

    // ---------------------------------------------------------- tab bar (#683 r5 pt 8)
    const REG = makeRegistry(ADDON)
    const TABS: [Tab, string][] = [['main', 'Главная'], ...REG.tabs.map((e): [Tab, string] => [e.id, e.label])]
    const tabPic = tabsSvg(TABS.map(x => x[1]), Math.max(0, TABS.findIndex(x => x[0] === cur)), W)
    const bar = Svg
      ? (
        <Box flexDirection="column">
          <Svg source={tabPic.svg} alt={TABS.map(x => x[1]).join(' · ')} height={TABH} />
          {/* #972: a tab head is `pressEl` like every pointer element (its host rectangle is the hover; the selected one is bright, the others dim) */}
          {TABS.map(([id, label], i) => pressEl({ key: `tab-${id}`, on: setTab(id), label, dim: cur !== id, left: tabPic.cols[i]! }))}
        </Box>
      )
      : (
        <Box flexDirection="row" gap={1} flexWrap="wrap">
          {TABS.map(([id, label]) => (
            <Button key={`tab-${id}`} label={label} variant={cur === id ? 'primary' : undefined} dimColor={cur !== id} onPress={setTab(id)} />
          ))}
        </Box>
      )

    // ---------------------------------------------------------- main tab (#683 r4c)
    // The registry's cards in their approved `order`: a tile, its title band, its rows.
    const mainTab = () => {
      return (
        <Box flexDirection="column">
          {REG.cards.filter(e => !(f && e.key && (f as any)[e.key] === undefined && !(f as any)[`${e.key}_error`])).map(e => e.draw(ctx))}
          {/* #157: the host window title is the static TITLE and the body has no title row, so the version number stands beside the footer time; null / absent draws nothing */}
          <Muted>{fresh(f?.at) + (Number.isInteger(f?.version) ? ` · v${f!.version}` : '')}</Muted>
        </Box>
      )
    }
    const ctx: Ctx = {
      agentsCard: () => {
        const list: Agent[] = f?.agents ?? []
        const busyN = list.filter(a => a.busy).length
        const ag = agentsBlock()
        return <Card k="agents" iconName="bot.hubot.octicons" name="Agents" sub={`${busyN} ${plural(busyN, "работает", "работают", "работают")}, ${list.length - busyN} без дела`}>{ag.pager}{ag.body}</Card>
      },
      mergeCard: () => <Card k="merge" iconName="merge-queue.octicons" name="Merge" sub={`очередь: ${(f?.merge?.running ? 1 : 0) + (fullRunOf(f?.merge) ? 1 : 0)} идёт, ${f?.merge?.waiting?.length ?? 0} ждёт` + queueEnd(f?.merge?.queue_end_h, f?.merge?.queue_until)}>{mergeBlock()}</Card>,
      addon: A,
      machineCard, ticketsTab, costTab, progressTab,
    }

    // ---------------------------------------------------------- assemble
    const assemble = () => {
      const shown = REG.tabs.find(e => e.id === cur)
      return (
        <Box flexDirection="column">
          {bar}
          <Gap />
          {shown ? shown.draw(ctx) : mainTab()}
        </Box>
      )
    }
    // #983: the tab WHOLE first (every list in full): under the target that is the draw, and its charge is its own measure, exact
    let tree = assemble()
    let fixed = sizeOf(tree), charged = fixed, fits = true
    if (wholeOver || fixed > TARGET) {
      // it does not fit whole: the measuring pass (every lane empty), the budget, then the tab with each lane's page (see `lane`)
      mode = 'probe'
      fixed = sizeOf(assemble())
      const lay = layout(TARGET - fixed, lanes, listPage)
      plans = lay.plans; fits = lay.fits
      mode = 'plan'
      tree = assemble()
      // what the drawn page of every list is charged (a tab with its own pages says its own)
      charged = fixed + Object.entries(plans).reduce((a, [k, pl]) => a + (ownCharge[k] ?? pl.charged), 0)
    }
    // the page counts a pager's press reads at press time
    for (const k of Object.keys(PAGES)) delete PAGES[k]
    for (const [k, pl] of Object.entries(plans)) PAGES[k] = ownPages[k] ?? pl.pages.length
    for (const k of Object.keys(SHOWN)) delete SHOWN[k]
    for (const [k, pl] of Object.entries(plans)) SHOWN[k] = pl.page
    // #972: ONE drawn form. The host stops drawing a tree past ~100,000 serialized characters (#837 b3); the pane's measure of its own tree (+ GUARD_STAMPS for the
    // `press` handles the host stamps on every element with a handler, #837 b11 D6) goes into the state record (#944) against HOVER_ROWS_LIMIT, and the heaviest
    // states are measured by test/size tests; no second placement code answers the limit (the six forms of #837 b3-b15 placed the same row by different code,
    // and the owner's data sat on the threshold: #972's cause 2).
    // #972: the host stamps `,"press":{"plugin":"pob-slots","handle":N}` on every element with a handler (#837 b11 D6: ~30 characters each): counted per Button, not a constant
    try {
      const treeJson = JSON.stringify(tree)
      const measure = sizeOf(tree)
      const one: CardForm = 'one'
      void (async () => {
        const session = await $.session.id().catch(() => 'unknown')
        const entry = await stateEntry($)
        // #972: `view` = what the tab shows beyond its name (the period and the group of the cost tab, every pager off its first page): a state of its own;
        // `budget` = the tab's fixed part as measured, what the lanes were charged on top, and whether the least of every lane fitted (hooks/budget.ts)
        // #983: an open item whose panel arrived, and a picked row whose own rows arrived, are states of their own too (the first draw after the press is still reading them)
        const ownOpen = !!REG.tabs.find(x => x.id === cur)?.ownOpen
        const view = [cur === 'cost' ? `${costMode}/${['roles', 'spawn', 'days'].includes(String(costGroup)) ? costGroup : 'roles'}` : '',
          ownOpen ? (selectedRd && bpRd?.key === selectedRd && bpRd.data ? 'panel' : '') : selected && bp?.key === selected && bp.data ? 'panel' : '', runSel && runPick?.id === runSel && runPick.progress ? 'squares' : '',
          cur === 'tickets' && tkPage > 0 ? `tk:${Math.min(tkPage, (ownPages.tk ?? 1) - 1) + 1}` : '', cur === 'tickets' && open.size ? `open:${open.size}` : '',
          ...Object.entries(plans).filter(([, pl]) => pl.page > 0).map(([k, pl]) => `${k}:${pl.page + 1}`).sort()].filter(Boolean).join(' ')
        await stateRecorder($).record({
          session, tab: String(cur), rowForm: one, lightCards: [],
          picked: runSel ?? null, open: (ownOpen ? selectedRd : selected) ?? null,
          cards: { grid: one, ag: one, mg: one, run: one },
          measure, firstMeasure: measure, limit: HOVER_ROWS_LIMIT, forced: null, entry,
          view, budget: { fixed, charged, fits },
        }, treeJson)
      })().catch(() => {})
    } catch { /* a record, never a reason to fail a redraw */ }
    return tree
  })
}
