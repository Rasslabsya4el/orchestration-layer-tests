This is the orchestration core's own block, imported by `CLAUDE.md` with one `@` line. It carries what holds in
any repo running this core; a target copies it unedited. Principles keep the numbers they hold in a repo that
imports them; a home repo may also keep its own, more specific, version of a mixed principle in its own part.

5. **No process ceremony in the product; one light process around it.** The code carries no ledgers, proof
   surfaces, waivers, blocker taxonomies or publication gates: a function either returns a result or raises, and
   validation is a test that measures the outcome. Work around the code follows mattpocock-skills and nothing
   else (section "Core conventions"): GitHub issues with the five triage labels, `GLOSSARY.md` for the vocabulary, an
   ADR in `docs/adr/` only for a decision that meets the `domain-modeling` bar (hard to reverse, surprising, a
   real trade-off). No parallel registry of tasks or decisions next to these; a skill step that does not fit a
   repo is skipped, not worked around.
7. **TIME IS WRITTEN FOR A HUMAN: NEVER A BIG NUMBER OF SECONDS.** Human-facing text (messages, reports, State
   entries, handovers) writes durations in a unit a human reads at a glance: seconds only under ~90 s, then
   minutes, then hours and minutes past ~90 min; a bare count of seconds in the hundreds or thousands is
   forbidden, and so is a rate quoted per 100 s instead of per minute. Machine files (JSON, logs) keep raw
   seconds; whoever quotes them converts. This binds every spec and every agent prompt.

## Big efforts: Pocock's wayfinder

An effort bigger than one session lives in one `wayfinder:map` issue (mattpocock-skills `wayfinder`), loaded
first by a session working on it; only the owner runs its user-invoked commands (`/wayfinder`,
`/grill-with-docs`, `/to-spec`, `/to-tickets`, `/triage`, `/handoff`), never copied or overridden. A grill the
brain runs on its own calls `grilling` together with `domain-modeling`, landing decisions in ADRs and
`GLOSSARY.md` as it goes.

## Roles (orchestration v2)

The owner talks only to the brain. The brain writes issues, ORCH executes them; the hand-off is a GitHub issue,
never a retold chat. Goal: fewer tokens per closed issue than one strong agent working alone.

| role | where | model / effort | job | protocol |
|---|---|---|---|---|
| brain | the chat named by `brain_chat_title`, or by `brain_chats` for an issue's area (`config/orchestration.json`), the only one with Remote Control | Fable 5.1 high (fallback Opus 5.5 xhigh) | talks to the owner, grills, writes issues with a goal and a result checked on the whole class of cases, one of them not named in the ticket (skill `write-ticket`), labels `ready-for-agent`; does not run the work ORCH runs (code, merges, test suites), reads itself the source of a fact that decides a verdict | `docs/agents/brain.md` |
| ORCH | as few Claude Code background sessions as possible (a further one starts only when every live one is finishing), started by `tools/agentops/dispatch.py`, no Remote Control | Sonnet 5.5 high (rollback Opus 5.5 high) | takes every ready issue at once and works them in parallel through its subagents, merges and closes each with its numbers | `docs/agents/orch.md` |
| dispatcher | `python -m tools.agentops dispatch`, a detached Python loop | none (a script, no tokens) | starts the one ORCH session when none is live and an issue is ready, offering it every ready issue; tracks the issues it holds and checks each ended with a closing or parking checkpoint (the outer "Ralph" loop) | `docs/agents/orch.md` |
| unblocker | background session started by the dispatcher | Fable 5.1 high (fallback Opus 5.5 xhigh) | writes the way for a ticket with 3 failed attempts in a row | `docs/agents/orch.md` |
| researcher-find / researcher-how | subagent | Haiku / Sonnet 5.5 | find, list, grep, tables / how and why; file:line on every fact | `.claude/agents/` |
| external researcher | GPT via opencode (`tools/agentops/`) | Luna 5.6 simple, Terra 5.6 complex | web / forum research | `docs/agents/research.md` |
| worker / worker-strong | subagent in its own worktree | worker Haiku 5.5 high / worker-strong Sonnet 5.5 high | code + tests from a numeric spec; worker-strong for subtle core logic | `.claude/agents/` |
| reviewer-standards | subagent, read-only | Haiku 5.5 high | the Standards axis of every unit review, against CODING_STANDARDS.md | `.claude/agents/` |
| reviewer-spec | subagent, read-only | Sonnet 5.5 high | the Spec axis of every unit review, against the issue | `.claude/agents/` |

The brain row's model binds the owner's desktop chat, which picks its own model; the `model:` pin in `.claude/agents/brain.md` binds only `--agent brain` launches (the autopilot).

A subagent never decides what a domain fact means or which rule to write; every delegation prompt names its
issue (`#N`). The owner's playbook: `docs/agents/owner.md`.

## Where the rules live

- How to work in a repo (issue flow, judging a change, audits, commits): `docs/agents/workflow.md`
- Outside-world research: `docs/agents/research.md`
- Chats, handovers, cost: `docs/agents/sessions.md`
- The weekly retro's records per role and its link failures: `docs/agents/retro.md`

Every agent, always: code only in its own git worktree; tests in the foreground; every wait bounded, own
background shells stopped before the report; never delete a worktree; a draft of a comment, a table or a report goes to the session's scratchpad directory, never into the repo; a process an agent starts never opens a console window of its own: it inherits the session's hidden console or goes through the repo's hidden launcher (the forms are in the repo's machine doc); a ticket is opened or rewritten (title or body) only after calling the Skill tool with `write-ticket`; a git merge or rebase conflict is worked by the agent itself, without a skill (the plugin ships none for it); a permission guard refusal is final (`docs/agents/workflow.md`, "Every session runs in `bypassPermissions`").

## Skills are hard steps

A skill is called with the Skill tool, at the step that names it, before that step's first edit; a skill left to a
hint goes unused. The first read of
every worker, worker-strong, researcher-how and reviewer-spec is `GLOSSARY.md`; a term in a report that is not in it is
a finding for the brain. A report carries each line its role's step owes:

| skill | called when | report line |
|---|---|---|
| `tdd` | before the first source edit of a unit with a test that needs no run of the heavy tool; the test fails before the fix is written; a unit whose only test is the heavy tool writes `tdd skipped: no test without the heavy tool` | `tdd: called / skipped (why)` |
| `diagnosing-bugs` | before any work on a defect (a wrong number, a crash, a regression, a hang); the cause, read in code or measured, is the first line of the report | `diagnosis: <cause>` |
| `mattpocock-skills:code-review` | on every unit diff against its issue, before the unit joins the issue branch | `mattpocock-skills:code-review: N findings, M fixed (reviewer-standards / reviewer-spec)`, written by the one who runs it |
| `writing-for-agents` | before any edit of `.claude/agents/`, `.claude/skills/`, `docs/agents/`, `CLAUDE.md` or a core block | `writing-for-agents: called` |
| `domain-modeling` | before any edit of `GLOSSARY.md` or `docs/adr/` | `domain-modeling: called` |
| `codebase-design` | before the first edit of a unit that adds a module or moves a seam | `codebase-design: called` |

## Core conventions

### Issue tracker

GitHub Issues of the project, through the `gh` CLI, written in English except the five lines of a ticket's "For the human" block, which are Russian. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default roles, label = role name (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`,
`wontfix`), plus a sixth, `parked`: waiting on a named event, not worked until it comes. It is orthogonal to the
roles (a parked ticket keeps its role label); the dispatcher never offers a `parked` issue; whoever posts the
event removes the label. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the root; `D<n>` anywhere in the repo is ADR `<nnnn>`. A new ADR
takes the highest number in `docs/adr/` plus one. Amendments (`D<n> amd <k>`) live inside their ADR, and a
superseded decision says so in its `Status` line. Where the number space continues an older decision log, every
existing `D<n>` reference stays valid one to one. See `docs/agents/domain.md`.
