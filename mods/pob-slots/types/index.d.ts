// Shapes written by bin/collect.py; loose where the panel only passes values through.

export type Cell = {
  ref: string
  name: string
  seed: number
  state: 'done' | 'failed' | 'running' | 'waiting' | 'queued'
  variant?: string
  took?: string
  stat?: string
  metrics?: [string, number | null, number | null][]
  // #683 r4c: the reference's class / ascendancy (fixture build); a running cell's start (epoch s)
  class?: string | null
  asc?: string | null
  started?: number
  // #711: where a running build is, from its log's own `step start:` lines: `stage` = the row's words (group and
  // step; in the loop the pass and the move), absent while no step has started; `after` = an older pipeline's log:
  // the last stage that ended; `bar` = the three groups' measured shares of build time, how far each is filled, and
  // the index of the group the build is in.
  stage?: string
  // #1004 u1: the status word of a started build (addon.py `_stamp` / `status_word`, the data's own Russian words); the row's act text
  status?: string
  group?: string | null
  step?: string
  move?: string
  after?: string
  bar?: { w: number[]; fill: number[]; at: number }
  // #683 r5: a replay build: its main stat before -> after from the run's log line; a running one's start rebuilt
  // from the pool order (epoch s) and the run's median build time
  delta?: string
  started_est?: number
  expect_s?: number
}

export type Job = {
  id: string
  tag: string
  name: string
  issue: number | null
  priority: string
  host: string
  workers: number
  state: string
  age: string
  took?: string
  // #587 table columns: local date / start time, run time (queued: the wait), a benchmark run's rows.
  date?: string
  start?: string
  dur?: string
  bench?: boolean
  rows_done?: number
  rows_total?: number
  gone?: boolean
  progress?: Progress | null
  // A running job: the slot locks it holds now (the daemon widens a lone job past `workers`, #570).
  locks?: number
  // #699: running = time still to go (`left_*`), queued = its whole run (`takes_*`) and its start from now
  // (`start_*`); `est` = how it was known (rate / against / history), null = nothing to go on.
  left_s?: number | null; left_h?: string; takes_s?: number | null; takes_h?: string
  start_s?: number; start_h?: string; est?: string | null; after?: string[]
  // #706: a queued job: why the daemon did not start it at its last step, and whether its host is fixed
  wait?: { kind: string; text: string }; host_fixed?: boolean
  // #1106: a queued job whose `--after` predecessor still runs or waits: that predecessor's tag (no job id)
  waits_for?: { tag: string }
  what?: string // #683 r5: what the run executes: benchmark / replay / tests / request / ''
  kind_name?: string // #985 b: the run's plain Russian name (addon.py kind_name), never empty from the collector
  // #683 r5: a remote-host row kept from its last good reading while the link is down, and that reading's HH:MM
  stale?: boolean; stale_since?: string
}

export type Progress = { done: number; total: number; total_open?: boolean; eta: string | null; dir?: string; cells: Cell[]; kind?: "replay" }

// The squares of a picked run that is not running, loaded by `collect.py cells <id>` on the click.
export type RunPick = { id: string; loading?: boolean; progress?: Progress; gone?: boolean; error?: string }

export type Runs = {
  period: string
  total: number
  others: number
  slots: { slot: number; role: string; pid: number | null; age: string }[]
  waiting: number
  running: Job[]
  queued: Job[]
  recent: Job[]
  history?: Job[]
  queue_until: string | null
  /** #699: the whole queue's end from now; `partial` = some job has no estimate, so it ends later. */
  queue_end_s?: number | null; queue_end_h?: string; queue_end_partial?: boolean
  daemon?: DaemonSlots | null
}

// #622: the runs daemon's status cut to the owner's reservation (#621); a field the daemon does not
// write yet is null. `age_s`: seconds since its last tick.
export type DaemonSlots = {
  host: string | null
  age_s: number
  pid?: number | null
  slots_total: number | null
  slots_in_use: number | null
  reserved: number | null
  reserved_until: string | null
}

// collect.py offline(): a remote host out of reach, as of its last good reading.
export type Offline = {
  lost?: string
  lost_ago?: string
  seen?: string
  seen_ago?: string
  running?: { tag: string; workers: number | null; done: number | null; total: number | null; eta: string | null }[]
  queued?: number
  verdict: string
  restore: string
  // The machine's shape at the last contact, for the empty card.
  cores?: number
  slots_total?: number
}

// #97 u2: one machine's load by source (bin/collect.py machine()): `cpu` in % of the machine, `ram` in GB. The keys: the add-on's work key
// (`work`, WORK_KEY), `agents`, `app` (the Claude desktop app), `tests` (pytest processes), `you` (the owner's other programs),
// `unmeasured` (what no process holds). A key an older collector does not send reads as 0.
export type Split = { agents: number; you: number; app?: number; tests?: number; unmeasured?: number; [key: string]: number | undefined }

export type Machine = {
  cores: number
  cpu: Split
  cpu_total: number
  // Seconds between the two scans the CPU shares are averaged over (the pull interval).
  interval_s?: number
  ram: Split
  ram_total: number
  ram_free: number
  // #1018: the merge's share of the used CPU (%) and RAM (MB), already taken out of `cpu` / `ram` (bin/collect.py); 0 with no merge, absent in older payloads.
  merge?: { cpu: number; cpu_raw?: number; ram_mb: number }
}

export type Agent = {
  /** The Claude Code session id (the join key of #678's opencode rows); '' for «Другое». */
  sid: string
  name: string
  kind: string
  busy: boolean
  issue: number | null
  held: number[]
  ticket_units?: Record<string, { done: number; total: number }> | null
  model: string
  ctx: number
  age: string
  last: string
  /** #683 r2: the last transcript events of a busy session and their hash (the Haiku word's key). */
  digest?: string
  dk?: string
  /** #678: an opencode session is a subagent row too (`type` 'opencode'); `depth` 2 = its own sub-session. */
  subagents: { id?: string; digest?: string; dk?: string; name: string; type: string; issue: number | null; model: string; ctx: number; age: string; busy: boolean; depth?: number; last?: string }[]
}

/** #6: one ticket's line of the Progress tab's «Пайплайн» (the collector's `pipeline` on the fast pull, already sorted). `stage` is the furthest stage
 *  the ticket is at; `workers` counts the live workers (`strong` of them worker-strong); `merge_position` / `merge_percent` are null outside the merge queue. */
export type PipelineRow = {
  number: number; title: string; stage: 'ready' | 'orch' | 'work' | 'merge' | 'accept'; failed: boolean
  workers: number; strong: number; researchers: number
  merge_position: number | null; merge_percent: number | null
  started_at: string | null; stage_since: string | null
  /** #151: the ticket's unit progress («k/n»), null or absent when the ticket has none */
  units?: { done: number; total: number } | null
  /** #171: the ticket waits after its merge: its `Parked:` reason and the date it waits to (`until`, a local ISO time, or null); absent or null when it does not wait */
  wait?: { reason: string; until: string | null } | null
  /** #171: the brain sent the ticket back to work after its last merge; absent or false otherwise */
  returned?: boolean
}

export interface Fast {
  at: string
  /** #157: the orchestration layer's build number (commits on the home main up to the taken sha); null / absent when unknown */
  version?: number | null
  status?: string
  runs?: Runs
  machine?: Machine
  agents?: Agent[]
  /** #6: the Progress tab's pipeline rows (one per ticket at a stage), sorted by the collector */
  pipeline?: PipelineRow[]
  runs_error?: string
  machine_error?: string
  agents_error?: string
  readings?: Readings
  /** #680: the merge line from `agentops merge-status --json`; `missing` while that command is not on main. */
  merge?: Merge
  merge_error?: string
  /** Added by the hook, not the collector: CPU samples per host of the last 10 min (one per 5-s pull). */
  hist?: Record<string, CpuSample[]>
}

/** #591: one build the brain queued for the owner to read (artifacts/readings/queue.json). */
export interface Reading {
  run_dir: string
  run_name: string
  ref: string
  seed: number
  variant: string
  ratio_pct: number | null
  metric?: string
  ours?: number | null
  rank?: number | null
  audit?: string | null
  floors?: string | null
  life?: number | null
  stop?: string
  /** The collector's own words for the row (collect.py _reading_facts): the build's name as a reader says it (the
   *  main skill, else the reference's words), what a variant lacks («без Blood Magic»), the row's face, the main
   *  number under its UI label, the build's check (null = not known) and its sentences ([is it bad, text]). */
  name?: string; var?: string; class?: string | null; asc?: string | null
  metric_label?: string; ours_h?: string
  errors?: number | null; warnings?: number | null; floors_bad?: boolean | null; said?: [boolean, string][]
  note?: string
  added_at?: string
}
export type Readings = { updated: string | null; rows: Reading[]; error?: string }

export type Issue = {
  n: number
  title: string
  open: boolean
  reason: string | null
  labels: string[]
  parent: number | null
  blockers: number[]
  kids: number[]
  parked_on_run?: boolean
  // #1075: the ticket carries the label `parked` (bin/collect.py tickets(): its row's `parked`); drawn by the parked mark, never by a word in its title
  parked?: boolean
  // #1089: the newest merge marker is a parked-on-merge comment whose result file is missing after 30 min (bin/collect.py tickets())
  merge_lost?: boolean
  state: 'closed' | 'owner' | 'blocked' | 'ready' | 'triage' | 'open'
  updated?: string
  created?: string
  author?: string | null
}

export type MergeRunning = {
  /** #37: what holds the line: a commit (its `message`, first line cut to 60 characters) or a merge (its branch); null for an old record */
  kind?: 'commit' | 'merge' | 'core-update' | null; message?: string | null
  /** #203: a core update holds the line with its home head's sha8 (`sha`), no branch and no ticket */
  sha?: string | null
  branches: string[]; issues: number[]; phase: 'suite' | 're-check' | 'push' | string; started: string
  percent: number | null; tests_done: number | null; tests_total: number | null; failed: number
  eta_s: number | null; workers?: number; suite_out?: string
  /** Human durations added by the collector. */
  eta_h?: string; elapsed_h?: string
}
export type MergeWaiting = {
  /** #203: a waiting core update has `kind` "core-update" and its `sha`, no branch and no ticket */
  kind?: 'merge' | 'core-update' | null; sha?: string | null
  branch?: string; issues?: number[]; position: number; queued: string; waited_s: number | null; eta_start_s: number | null
  waited_h?: string; eta_start_h?: string
  /** #699: one landing's usual time. */ takes_h?: string
}
/** #116 B1: a did-not-land merge (rc != 0) carries its `reason` (merge_status), its end `ended` and its red suite's `failed` count. */
export type MergeLanded = { branch: string; issues: number[]; queued?: string; landed?: string; queued_to_land_s: number | null; rc: number; took_h?: string
  reason?: string | null; ended?: string; failed?: number | null }
export type Merge = { missing?: boolean; at?: string; queue_end_h?: string; queue_until?: string; running?: MergeRunning | null; waiting?: MergeWaiting[]; landed?: MergeLanded[] }

export type Tickets = { at: string; goals: number[]; loose: number[]; issues: Record<string, Issue> }

export type Slow = { at: string; progress?: any; results?: any; cost_day?: any; cost_week?: any; cost_month?: any }

export type CostMode = 'day' | 'week' | 'month'

// The collector's `cost_*.cut` (collect.py slow): one population of the period cut six ways; every cut sums to total.cost_usd.
export type CutTokens = { input: number; cache_write: number; cache_read: number; output: number }
export type CostCut = {
  since: string
  until: string
  total: { cost_usd: number; turns: number; unpriced_turns: number; tokens: CutTokens; limit_pct: number | null; limit_usd_per_pct: number | null; /** #1018: 7 for a day / week, 30 for a month; absent in an older payload = the week */ limit_days?: number }
  by_kind: { key: string; tokens: number; cost_usd: number }[]
  by_who: { key: string; kind: string; sessions: number; turns: number; tokens: number; cost_usd: number; sub_usd: number }[]
  /** local calendar days of the period (1 / 7 / 30) */
  days: number
  /** ranked by $, sums to total; main: n = sessions, sub: n = subagents, mod: n = calls; `types` only for other_sub */
  by_role: { key: string; group: 'main' | 'sub' | 'mod'; n: number; turns: number; tokens: number; cost_usd: number; types: { key: string; agents: number; cost_usd: number }[] }[]
  /** a spawner's own turns and the subagents it started; own + sub = cost_usd, roles sum to sub; absent with no turn */
  by_spawner: { key: string; sessions: number; chats: string[]; turns: number; tokens: number; cost_usd: number
    own: { turns: number; tokens: number; cost_usd: number }; sub: { turns: number; tokens: number; cost_usd: number; agents: number }
    roles: { key: string; agents: number; turns: number; tokens: number; cost_usd: number; types: { key: string; agents: number; cost_usd: number }[] }[] }[]
  /** one row per role x model, ranked by $, sums to total; main n = sessions (led = led by this model), sub n = subagents, mod n = calls; ORCH rows: ended / closed from the dispatcher */
  by_role_model: { role: string; group: 'main' | 'sub' | 'mod'; model: string; n: number; led: number; turns: number; unpriced_turns: number; tokens: number; cost_usd: number
    first: string | null; last: string | null; spawned: { turns: number; tokens: number; cost_usd: number; agents: number }; ended: number | null; closed: number | null }[]
  by_mod: { key: 'monitor'; model: string; calls: number; unpriced_calls: number; turns: number; tokens: number; cost_usd: number }[]
  by_model: { key: string; turns: number; unpriced_turns: number; tokens: number; cost_usd: number }[]
  by_day: { key: string; turns: number; tokens: number; cost_usd: number }[]
  /** title / open / reason / state: what a row draws by itself when Tickets holds no entry for it (a closed ticket under no open goal); absent when the collector's list lacked it */
  by_issue: { number: number; turns: number; tokens: number; cost_usd: number; title?: string; open?: boolean; reason?: string | null; state?: Issue['state'] }[]
  issue_rest: { n: number; tokens: number; cost_usd: number }
  no_issue: { turns: number; tokens: number; cost_usd: number }
}

export type KindStats = {
  sessions: number
  ended: number
  closed: number
  subagents: number
  turns: number
  ctx: number
  new: number
  cost: number
  sub_cost: number
  unpriced: number
  by_type: Record<string, number>
  by_model: Record<string, number>
}

export type Orch = {
  at: string
  took: string
  parsed: number
  periods: Record<CostMode, { since: string; until: string; kinds: Record<string, KindStats> }>
}

/** One finished cell's build, decoded by `collect.py build`: its stats, grouped. */
export interface BuildData {
  name: string
  seed: number
  class: string | null
  level: string | null
  skill: string | null
  author_skill: string | null
  author: boolean
  groups: { title: string; rows: [string, string, string, number | null][] }[]
  code: string
  /** #683 r5: a replay run's square: the replay's own before -> after of its main stat */
  note?: string | null
}

export type BuildPanel = { key: string; loading?: boolean; error?: string; data?: BuildData; opened?: string }

/** One CPU sample per fast pull: each source's percent of the whole machine (#114: the sources the CPU bar draws; #119: `merge`, the merge's percent, absent in older samples = 0). */
export type CpuSample = { work: number; agents: number; app: number; tests: number; you: number; unmeasured: number; merge?: number }

export type Tab = string

declare module 'claude-code' {
  interface PluginState {
    'pob-slots': {
      fast: Fast | null
      tickets: Tickets | null
      slow: Slow | null
      errors: Record<string, string>
      /** #142 u2: the fast pull runs `collect.py fast` itself because the collector's file is stale or missing */
      collectorOff: boolean
      tab: Tab
      expanded: number[]
      costMode: CostMode
      /** #972: the group of tables the cost tab shows ('roles' | 'spawn' | 'days') */
      costGroup: string
      /** #972: the page of every paged list, by its lane key (hooks/budget.ts) */
      listPage: Record<string, number>
      orch: Orch | null
      selected: string | null
      panel: BuildPanel | null
      /** #994 (defect 6): the open item and its panel of a tab that keeps its own (TabEntry `ownOpen`) */
      readSel: string | null
      readPanel: BuildPanel | null
      // #587: the run whose squares the runs card shows (job id; null = the first running one),
      // the loaded squares of a run that is not running, and the History page.
      runSel: string | null
      runPick: RunPick | null
      histPage: number
      // #837 b12: the page of the shown run's grid (the run it belongs to: another run starts at page 0)
      gridPage: { run: string | null; page: number }
      tkPage: number
      pressed: string | null
      // #622: the reservation row's inputs per host (N slots, M minutes, as typed) and the last
      // command's error per host.
      reserveForm: Record<string, { n?: string; m?: string }>
      reserveMsg: Record<string, string>
      /** #683 r2: Haiku's words per agent id (session sid / subagent id), from bin/.cache/activity.json. */
      words: Record<string, string>
    }
  }
}
