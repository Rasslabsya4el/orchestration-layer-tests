import { test, mock } from 'claude-code/testing'
import { COMMAND } from '../hooks/register'
import { PANE, REPO, OPENED_TEXT } from '../hooks/project'

// #173 row 2b: in a chat of this repo whose pane opened by itself and was then closed by the person, /monitor reopens the
// pane and answers from the hook: its text comes back, `next` is not called (the stub beneath the plugin, which stands for
// the command file's body going to the model, is never reached). A hook that raised would hand the command to the model.
const BODY_TO_MODEL = 'the command file body went to the model'

// a chat of this repo: the pane opened (the first /monitor stands for the self-opening start), the person closed it,
// /monitor again. `openFails` makes the second ui.open reject (a refusal beneath, a surface that cannot seat the pane).
const reopen = async ($: any, on: any, openFails: boolean) => {
  const clock = mock.clock(on)
  const opened: string[] = []
  const closed: string[] = []
  let refuse = false
  on('session.cwd', async () => ({ value: REPO }) as any)
  on('ui.open', async (_$: any, e: any) => {
    if (refuse) throw new Error('ui.open refused')
    opened.push(e.id)
    return { value: { isPlaced: true } } as any
  })
  on('command.run', async () => ({ text: BODY_TO_MODEL }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('process.run', async () => ({ value: { exitCode: 0, stdout: '{}', stderr: '', isStdoutTruncated: false } }) as any)
  const run = async () => {
    let answer: any
    let error: any
    void $.command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).then((r: any) => { answer = r }, (e: any) => { error = e })
    await clock.settle()
    return { answer, error }
  }
  const first = await run()
  const openedBefore = opened.length // the first start opens the pane by itself, then the command opens it again
  closed.push(PANE) // the person's close mark: the mod holds no ui.close hook and no pane state, so it is only the pane going away
  refuse = openFails
  const second = await run()
  return { first, second, opened, closed, openedBefore }
}

test('/monitor after the pane was closed reopens it and answers from the hook', async ($, on) => {
  const { first, second, opened, closed, openedBefore } = await reopen($, on, false)
  if (first.error || first.answer?.text !== OPENED_TEXT) throw new Error(`first /monitor: ${JSON.stringify(first)}`)
  if (!closed.includes(PANE)) throw new Error(`the pane was not closed: ${JSON.stringify(closed)}`)
  if (opened.length !== openedBefore + 1 || opened.some(id => id !== PANE)) throw new Error(`the reopen opened ${JSON.stringify(opened.slice(openedBefore))}, want exactly [${PANE}]`)
  if (second.error) throw new Error(`/monitor after the close threw: ${second.error}`)
  if (second.answer?.text === BODY_TO_MODEL) throw new Error('/monitor after the close went on to the model (next was called)')
  if (second.answer?.text !== OPENED_TEXT) throw new Error(`/monitor after the close answered ${JSON.stringify(second.answer)}, want ${OPENED_TEXT}`)
})

test('/monitor answers from the hook even when the reopen is refused: no model turn', async ($, on) => {
  const { second } = await reopen($, on, true)
  if (second.error) throw new Error(`a refused reopen raised out of the hook (the command would go to the model): ${second.error}`)
  if (!second.answer?.text) throw new Error(`a refused reopen gave no answer text: ${JSON.stringify(second.answer)}`)
  if (second.answer.text === BODY_TO_MODEL) throw new Error('a refused reopen went on to the model (next was called)')
})
