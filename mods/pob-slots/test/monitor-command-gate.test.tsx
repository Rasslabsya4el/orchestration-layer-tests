import { test, mock } from 'claude-code/testing'
import { COMMAND, repoOwnsCwd } from '../hooks/register'
import { PANE, REPO, OPENED_TEXT } from '../hooks/project'

// #173 rule 1 and row 2: the command is one name in every repo (pinned in the core hook code), and a monitor answers it
// only for a session whose cwd is its own repo (worktrees included). Another repo's chat gets no pane from this monitor.
const REPO_B = 'C:/Coding/repo-b'
// the answer a test puts beneath the plugins: a command the monitor passed on reads this text
const PASSED_ON = 'passed on'

// /monitor typed in a session whose session.cwd answers `cwd`: the pane ids it opened, the command's answer, and an
// error the command raised (a throw is recorded, never swallowed).
const runMonitor = async ($: any, on: any, cwd: any) => {
  const clock = mock.clock(on)
  const opened: string[] = []
  let answer: any = undefined
  let error: any = undefined
  let settled = false
  on('session.cwd', async () => ({ value: cwd }) as any)
  on('ui.open', async (_$: any, e: any) => {
    opened.push(e.id)
    return { value: { isOpen: true } } as any
  })
  on('command.run', async () => ({ text: PASSED_ON }) as any)
  on('ui.status', async () => ({ value: undefined }) as any)
  on('process.run', async () => ({ value: { exitCode: 0, stdout: '{}', stderr: '', isStdoutTruncated: false } }) as any)
  void ($ as any).command.run({ command: COMMAND, args: '', origin: 'user', presentation: 'text' }).then(
    (r: any) => { answer = r; settled = true },
    (e: any) => { error = e; settled = true },
  )
  await clock.settle()
  return { opened, answer, error, settled }
}

test('the command is monitor in every repo', async () => {
  if (COMMAND !== 'monitor') throw new Error(`COMMAND is ${COMMAND}, not monitor`)
})

test('a session is owned by this repo only (worktrees included)', async () => {
  // #173 send-back: a session with no string cwd is not in this repo (no throw)
  const cases: [unknown, string, boolean][] = [
    [undefined, REPO, false],
    [null, REPO, false],
    [42, REPO, false],
    [REPO, REPO, true],
    [`${REPO}/`, REPO, true],
    [`${REPO}/.claude/worktrees/wt-x`, REPO, true],
    ['C:\\Coding\\orchestration-layer\\mods\\pob-slots', REPO, true],
    [REPO_B, REPO, false],
    [`${REPO_B}/src`, REPO, false],
    ['D:/elsewhere', REPO, false],
    ['C:/Coding', REPO, false],
  ]
  for (const [cwd, repo, want] of cases) {
    const got = repoOwnsCwd(cwd, repo)
    if (got !== want) throw new Error(`repoOwnsCwd(${cwd}, ${repo}) = ${got}, want ${want}`)
  }
})

test('/monitor in a session of this repo opens its pane', async ($, on) => {
  const { opened, error } = await runMonitor($, on, REPO)
  if (error) throw new Error(`/monitor in the repo threw: ${error}`)
  if (!opened.includes(PANE)) throw new Error(`/monitor in the repo opened ${JSON.stringify(opened)}, not ${PANE}`)
})

test('/monitor in a worktree of this repo opens its pane', async ($, on) => {
  const { opened, error } = await runMonitor($, on, `${REPO}/.claude/worktrees/wt-x`)
  if (error) throw new Error(`/monitor in a worktree threw: ${error}`)
  if (!opened.includes(PANE)) throw new Error(`/monitor in a worktree opened ${JSON.stringify(opened)}, not ${PANE}`)
})

test('/monitor in another repo opens no pane of this monitor', async ($, on) => {
  const { opened, error, answer } = await runMonitor($, on, REPO_B)
  if (error) throw new Error(`/monitor in ${REPO_B} threw: ${error}`)
  if (opened.length) throw new Error(`/monitor in ${REPO_B} opened ${JSON.stringify(opened)}`)
  if (answer?.text !== PASSED_ON) throw new Error(`/monitor in ${REPO_B} was not passed on: answer ${JSON.stringify(answer)}`)
})

test('/monitor outside every repo opens no pane', async ($, on) => {
  const { opened, error } = await runMonitor($, on, 'D:/elsewhere')
  if (error) throw new Error(`/monitor outside every repo threw: ${error}`)
  if (opened.length) throw new Error(`/monitor outside every repo opened ${JSON.stringify(opened)}`)
})

// #173 send-back: a session with no usable cwd (undefined, null, a non-string) is not in this repo: the hook does
// nothing (no pane, no throw) and passes the command on.
for (const [name, cwd] of [['undefined', undefined], ['null', null], ['a number', 42]] as const) {
  test(`/monitor with a session cwd of ${name}: no pane, no throw, the command is passed on`, async ($, on) => {
    const { opened, answer, error, settled } = await runMonitor($, on, cwd)
    if (error) throw new Error(`/monitor with cwd ${name} threw: ${error}`)
    if (opened.length) throw new Error(`/monitor with cwd ${name} opened ${JSON.stringify(opened)}`)
    if (answer?.text === OPENED_TEXT) throw new Error(`/monitor with cwd ${name} answered the monitor's opened line`)
    if (!settled) throw new Error(`/monitor with cwd ${name} did not settle`)
    if (answer?.text !== PASSED_ON) throw new Error(`/monitor with cwd ${name} was not passed on: answer ${JSON.stringify(answer)}`)
  })
}
