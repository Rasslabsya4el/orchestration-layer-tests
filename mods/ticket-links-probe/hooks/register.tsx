import type { Register } from 'claude-code'
import { createProbe, type LogIo } from './probe'

// #176 row 13, the probe (throwaway, separate from ticket-links): every desktop AssistantMessage render appends one JSON
// line to this log. The chain answers the drawing unchanged: `drawn` is what next(e) resolved to, returned by reference.
// A failure of the log is swallowed, so the chat is never affected. It reads nothing but the log file it appends to.
const LOG_PATH = 'C:/Coding/orchestration-layer/var/agentops/ticket_links_probe.jsonl'

const probe = createProbe()

// the hook's file calls, as the probe takes them
const filesOf = ($: any): LogIo => ({
  exists: p => $.fs.exists(p),
  read: p => $.fs.read(p),
  write: (p, t) => $.fs.write(p, t),
})

// the session id, or null where the session does not expose it
const sessionOf = async ($: any): Promise<string | null> => {
  try {
    return await $.session.id()
  } catch {
    return null
  }
}

export const register: Register = on => {
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    const drawn = await next(e)
    try {
      await probe(filesOf($), LOG_PATH, {
        surface: e.surface,
        text: e.props.text,
        isSummary: e.props.isSummary,
        onScreen: 'onScreen' in e.props ? e.props.onScreen : undefined,
        drawn,
        trace: (next.trace ?? []).map(t => t.plugin),
        at: new Date(),
        session: () => sessionOf($),
      })
    } catch {
      // the probe never changes the chat
    }
    return drawn
  })
}
