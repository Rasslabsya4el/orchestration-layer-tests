import { test, mock } from 'claude-code/testing'
import { PANE, REPO } from '../hooks/project'
import { COMMAND } from '../hooks/register'
// #161 (plugin half): the Cost tab draws what the collector's `slow` pull answered. A window whose `cut` failed
// (`cut_error`) draws the error card «Расходы не посчитаны» with its reason; a window with `cut` draws its total.
// Once the pull has answered, the loading word «загружается» is never drawn. The seam is the drawn pane.

const PROPS: any = { title: 'Monitor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} }
const AT = '2026-10-18T10:00:00+02:00'

// every string in the drawn tree, depth first (text children and string props alike)
const allStrings = (x: any, out: string[] = []): string[] => {
  if (typeof x === 'string') out.push(x)
  else if (Array.isArray(x)) for (const v of x) allStrings(v, out)
  else if (x && typeof x === 'object') for (const v of Object.values(x)) allStrings(v, out)
  return out
}

// a cut the costTab draws whole: one day, four priced turns, 0.028 USD (the same figures as the python fixture, tests/test_issue161_cost_fallback.py)
const CUT = {
  since: '2026-10-17T00:00:00+02:00', until: AT, days: 1,
  total: { cost_usd: 0.028, turns: 4, unpriced_turns: 0, tokens: { input: 3000, cache_write: 4000, cache_read: 10000, output: 1100 },
    limit_pct: null, limit_usd_per_pct: null, limit_days: 1 },
  by_kind: [], by_who: [], by_role: [], by_spawner: [], by_mod: [], by_role_model: [], by_model: [], by_day: [], by_issue: [],
  issue_rest: { n: 0, tokens: 0, cost_usd: 0 }, no_issue: { turns: 0, tokens: 0, cost_usd: 0 },
}

// the drawn text of the Cost tab's day window, after one `slow` pull answered `cost` for every window
const drawCostDay = async ($: any, on: any, surface: 'terminal' | 'desktop', cost: any): Promise<string> => {
  const clock = mock.clock(on)
  const slow = JSON.stringify({ at: AT, cost_day: cost, cost_week: cost, cost_month: cost })
  on('process.run', async (_$: any, e: any) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'slow' ? slow : '{}', stderr: '', isStdoutTruncated: false } }) as any)
  on('ui.open', async () => ({ value: { isOpen: true } }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('session.cwd', async () => ({ value: REPO }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).catch(() => {})
  await clock.settle()
  const ui = await $.ui.mount({ plugin: 'pob-slots', surface, component: 'Pane', requestId: PANE, props: PROPS })
  await clock.settle()
  await ui.press({ key: 'tab-cost' })
  await clock.settle()
  await ui.press({ key: 'cost-day' })
  await clock.settle()
  const text = allStrings(await ui.drawn()).join('\n')
  await ui.unmount()
  return text
}

for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: cut_error draws the error card with its reason, never the loading word`, async ($, on) => {
    const text = await drawCostDay($, on, surface, { cut_error: 'boom in scan' })
    if (!text.includes('Расходы не посчитаны')) throw new Error(`${surface}: no error card drawn for cut_error: ${text.slice(0, 300)}`)
    if (!text.includes('boom in scan')) throw new Error(`${surface}: the error card does not draw the reason`)
    if (text.includes('загружается')) throw new Error(`${surface}: the loading word is drawn after the pull answered with cut_error`)
  })

  test(`${surface}: cut present draws the total, not the error card`, async ($, on) => {
    const text = await drawCostDay($, on, surface, { cut: CUT })
    if (!text.includes('$0.03')) throw new Error(`${surface}: the total of 0.028 USD is not drawn: ${text.slice(0, 300)}`)
    if (text.includes('Расходы не посчитаны')) throw new Error(`${surface}: the error card is drawn for a cut that is there`)
    if (text.includes('загружается')) throw new Error(`${surface}: the loading word is drawn after the pull answered with cut`)
  })
}
