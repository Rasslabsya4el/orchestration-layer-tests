// #176 row 13, the probe's logic apart from the hook: what one AssistantMessage render appends to the log, and how.
// The file calls come in as `io` (the hook passes its $.fs; a test passes an in-memory store), so a test imports this
// module and reads back what was written. No `$` in here: the hook reads the session id and passes it on.

export type LogIo = {
  exists: (path: string) => Promise<boolean>
  read: (path: string) => Promise<string>
  write: (path: string, text: string) => Promise<void>
}

// one render, as the hook saw it
export type Render = {
  surface: string
  text: string
  // the raw props: `isSummary` true or absent, `onScreen` an OnScreen or absent
  isSummary?: unknown
  onScreen?: unknown
  // the tree next(e) resolved to
  drawn: unknown
  // the plugin names of the links that ran beneath the hook, nearest first (next.trace)
  trace: readonly string[]
  at: Date
  session: () => Promise<string | null>
}

// ISO local time with the offset, from a JS Date
export const localIso = (d: Date): string => {
  const p = (n: number, w = 2) => String(n).padStart(w, '0')
  const off = -d.getTimezoneOffset()
  const a = Math.abs(off)
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:` +
    `${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}${off >= 0 ? '+' : '-'}${p(Math.floor(a / 60))}:${p(a % 60)}`
}

// a drawn Markdown element (the form ticket-links draws)
const isMarkdown = (node: any): boolean => node?.type === 'Markdown' || node?.type?.name === 'Markdown'

// one probe: its own last-logged key and its own queue of appends. The appends run one after another, so two renders
// never read the same log and drop a line. Only a desktop render is logged; a repeat of the last (text, isSummary) is not.
export const createProbe = () => {
  let lastKey: string | undefined
  let queue: Promise<unknown> = Promise.resolve()
  return async (io: LogIo, path: string, r: Render): Promise<void> => {
    if (r.surface !== 'desktop') return
    const isSummary = r.isSummary === undefined ? null : Boolean(r.isSummary)
    const key = JSON.stringify([r.text, isSummary])
    if (key === lastKey) return
    lastKey = key
    // drawn_by_mod: true when the tree next(e) resolved to is a Markdown; false when ticket-links ran beneath and drew
    // nothing; null when the chain order does not show it (ticket-links not beneath this hook)
    const drawnByMod = isMarkdown(r.drawn) ? true : r.trace.includes('ticket-links') ? false : null
    const job = queue.then(async () => {
      const row = {
        local_time: localIso(r.at),
        session: await r.session(),
        isSummary,
        onScreen: r.onScreen ?? null,
        drawn_by_mod: drawnByMod,
        text200: Array.from(r.text).slice(0, 200).join(''),
      }
      // there is no append: the file is read whole and written back whole; a log over the read limit rejects and
      // nothing is written, so the file is never cut down to one line
      const old: string = (await io.exists(path)) ? await io.read(path) : ''
      await io.write(path, (old === '' || old.endsWith('\n') ? old : `${old}\n`) + JSON.stringify(row) + '\n')
    }).catch(() => undefined)
    queue = job
    await job
  }
}
