// #944: the pane writes its own state to a file whenever the state changes (`state.jsonl` plus one tree file per change).
// Pure: the caller passes the fs and the directory; nothing here reads the plugin's constants (project.ts stays as it is).
//
// Tree files are a ring of TREE_RING fixed names: the SDK's `$.fs` has read, write, list, exists, stat and ancestors,
// and no delete (.claude-plugin/types/claude-code/index.d.ts:3242-3342; its only `delete` is `$.store`, a key-value store).
// So the files cannot be pruned by age; the ring overwrites the oldest one instead.

export type CardForm = 'pill' | 'mask' | 'light' | 'one' // 'one' since #972: the one drawn form (the three tiers are history)

export interface StateInput {
  session: string
  tab: string
  rowForm: CardForm
  lightCards: string[]            // names of cards forced light, any order (the module sorts)
  picked: string | null           // the picked run id
  open: string | null             // the open build id
  cards: { grid: CardForm; ag: CardForm; mg: CardForm; run: CardForm }
  measure: number                 // JSON.stringify(tree).length + GUARD_STAMPS of the FINAL tree
  firstMeasure: number            // the same measure before any card went light
  limit: number                   // 90000
  forced: string | null           // FORCE_HOVER_TIER or null
  entry: { register: number; kit: number; version: string }
  /** #972: what the tab shows beyond its name (the cost tab's period and group, every pager off its first page); a state of its own. Absent = '' */
  view?: string
  /** #972: the budget of this draw (hooks/budget.ts): the tab drawn with every growing list empty, as measured; that plus what the lists were charged; whether every list's least fitted */
  budget?: { fixed: number; charged: number; fits: boolean }
}

export interface StateFs {
  write(path: string, text: string): Promise<unknown> | unknown
  read(path: string): Promise<unknown> | unknown
  exists(path: string): Promise<boolean> | boolean
}

/** Equals REDRAW_CAP in register.tsx (1 MB): past it state.jsonl keeps its newest half, cut at a newline. */
export const STATE_CAP = 1_000_000
/** The tree files' ring: a line names its tree `tree-<k>.json`, k = the newest line's k + 1 mod 20 (shared by every session, #994). */
export const TREE_RING = 20

/** What makes a state line: tab, row form, the sorted light cards, the picked run and the open build; since #972 the tab's view too, when it has one. Nothing else. */
export function stateSignature(i: StateInput): string {
  return JSON.stringify([i.tab, i.rowForm, [...i.lightCards].sort(), i.picked, i.open, ...(i.view ? [i.view] : [])])
}

export interface Recorder {
  /** true = a line was written; false = the signature is the last one written, or a write failed (never throws). */
  record(i: StateInput, treeJson: string): Promise<boolean>
}

const p2 = (n: number) => String(n).padStart(2, '0')
function localStamp(d: Date): string {
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}T${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`
}

export function createRecorder(fs: StateFs, dir: string, now: () => Date = () => new Date()): Recorder {
  const stateFile = `${dir}/state.jsonl`
  let last: string | null = null
  let written = 0
  // Calls run one after another: state.jsonl is read, then written whole, so two overlapping calls would lose a line.
  let queue: Promise<unknown> = Promise.resolve()

  async function once(i: StateInput, treeJson: string): Promise<boolean> {
    try {
      const sig = stateSignature(i)
      if (sig === last) return false
      let before = (await fs.exists(stateFile)) ? String(await fs.read(stateFile)) : ''
      // #994: every session that mounts the pane writes into the same ring; each counting from its own 0 overwrote the owner's newest tree within seconds (a new session's first
      // state is tree-00), and nothing could be laid on his pane. The ring is shared: the next file is the one after the newest line's, whoever wrote it.
      const prev = /"tree":"tree-(\d+)\.json"[^\n]*\n?$/.exec(before)
      const tree = `tree-${p2(prev ? (Number(prev[1]) + 1) % TREE_RING : written % TREE_RING)}.json`
      await fs.write(`${dir}/state/${tree}`, treeJson)
      if (before.length > STATE_CAP) {
        before = before.slice(Math.floor(before.length / 2))
        const nl = before.indexOf('\n')
        before = nl < 0 ? '' : before.slice(nl + 1)
      }
      const line = JSON.stringify({
        t: localStamp(now()),
        session: i.session,
        entry: i.entry,
        tab: i.tab,
        picked: i.picked,
        open: i.open,
        cards: i.cards,
        guard: { measure: i.measure, limit: i.limit, forced: i.forced, first_measure: i.firstMeasure },
        tree,
        ...(i.view ? { view: i.view } : {}),
        ...(i.budget ? { budget: i.budget } : {}),
      })
      await fs.write(stateFile, `${before}${line}\n`)
      last = sig
      written++
      return true
    } catch {
      return false
    }
  }

  return {
    record(i, treeJson) {
      const run = queue.then(() => once(i, treeJson))
      queue = run.catch(() => {})
      return run
    },
  }
}
