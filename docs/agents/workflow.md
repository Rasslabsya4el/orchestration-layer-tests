# Workflow

What this repo adds is in `docs/agents/local/workflow.md`, read it when it exists.

Moved verbatim from CLAUDE.md; it binds every agent as if it were still written there.
The section "How to work in this repo" below.

Orchestration v2 changed who does what: the brain talks to the owner and writes issues (`docs/agents/brain.md`);
ORCH executes, reviews and merges (`docs/agents/orch.md`). Where a sentence below names the lead, it means the
brain or ORCH by that split.

## How to work in this repo

- Start from `docs/STATE.md` (what runs, what constrains merging), then the
  issues: `gh issue list --label ready-for-agent` for agent work,
  `ready-for-human` for the owner's. A
  change goes through the mattpocock-skills flow, scaled to its size: settle
  it (`grilling`, or the owner's `/grill-with-docs`), write it down as an
  issue with a goal and a result checked on the whole class of cases, one of them not named in the ticket
  (skill `write-ticket`; `/to-spec`, `/to-tickets` when it has several steps), implement it in a worktree
  (core block, "Skills are hard steps": `tdd`, `diagnosing-bugs`, `codebase-design`), review every unit diff
  (`mattpocock-skills:code-review` against the issue), then the benchmark numbers
  before the merge. An issue is closed with its numbers in the closing comment.
  Rewrite `docs/STATE.md` at every commit that changes runs or constraints, its own entry written under the merge
  lock through `python -m tools.agentops commit -m <message> --state-entry "<text>" --state-as <writer>
  docs/STATE.md` (never a plain `git commit`/`git push` on `main` in the main checkout, and never a hand edit of
  the shared `Updated` line; a commit whose paths are not all Markdown under `docs/` runs the merge suite and a red suite refuses it), and keep the machine's work queue non-empty so it never idles.
  When the brain asks the owner for a new chat, it hands him the copy block
  (`docs/agents/sessions.md`): one fenced code block holding one start line
  (`/wayfinder <N>`, the number not a link, no `/clear`). The results reach the owner
  through the brain's review (`docs/agents/brain-reference.md` "The results review").
  Who does what follows CLAUDE.md "Roles". Tokens first, but the machine never idles: as few ORCH sessions as
  possible, each holding every ready issue and working them in parallel through its subagents, a further session
  only when every live one is finishing (`docs/agents/orch.md`); ORCH parks an issue on a long run; the brain
  writes the issues.
- Record a decision as an ADR in `docs/adr/` only when it meets the bar in
  `docs/agents/domain.md` (hard to reverse, surprising, a real trade-off); the
  rest lives in the commit message with its numbers.
- A behaviour row that makes made-up issues, PRs, branches or repos runs in a repo made by
  `python -m tools.agentops trial create`, never in a work repo of the installation; its closing comment names the
  trial repo; deleting trial repos is the owner's (`trial list` shows them).
- Long runs (benchmarks, searches) are launched as detached processes, never
  from the session shell: a session end kills its children. The launch forms
  are the launch rule in the repo's machine doc. Put a monitor on the log; while it runs, read logs
  and prepare the next fix.
- Commit each step with the before/after numbers in the message.
- The `mattpocock-skills:code-review` Standards axis reads `CODING_STANDARDS.md` at the repo root and names the rule a finding breaks;
  a rule missing there is a ticket, never a line added in passing.
- **A red suite is never merged around.** A merge whose suite is red is not retried by hand (`git merge`, a
  different `--suite`, a skipped test): the agent fixes the code, or the data the failing tests read, until the
  suite is green, then merges through the merge command. A test that judges built data judges data built from the
  code being merged, never stale data left in a checkout. The one exception is a test already red on main before the
  ticket: it is marked, never fixed inside the ticket, per "Red before the ticket" in `docs/agents/orch.md`.
- **Every session runs in `bypassPermissions` behind the permission guard** (`tools/hooks/permission_guard.py`; the
  red list is in the ADR "every session runs in bypass permissions"). A guard refusal is final: the agent writes the exact call and the red line's number
  into the issue (a checkpoint), never retries it in another form, and goes on; nothing is asked of the owner. A
  red line that is wrong or missing is a `needs-triage` ticket for the brain, never a permission entry and never a
  hand edit of the settings (they hot-reload into every session and change only through a merged ticket and
  `python -m tools.agentops guard install`). On a new machine the owner first runs
  `claude --dangerously-skip-permissions` once in a terminal and accepts the disclaimer (without it `claude --bg`
  in `bypassPermissions` refuses); `guard check`'s 5th line shows it.
  The mods install root (`~/.claude/mods`) and the owner's loaded-mods line (`env.CLAUDE_CODE_PLUGIN_DIRS`) are kept
  by `python -m tools.agentops mods install <name>` / `mods remove <name>`, never by a shell command.

Weekly, in the brain's prophylaxis: `python -m tools.agentops skills --since <a week ago>` prints skill calls per role
and the share of subagent transcripts that opened `GLOSSARY.md`. A skill whose count stays below its trigger's work
(core block, "Skills are hard steps") becomes a ticket; a finding about the orchestration core is a core issue at the home instead, see `docs/agents/retro.md`.
The same prophylaxis also runs `python -m tools.agentops logic-audit --since <date of the last prophylaxis>`: a read-only `researcher-how` (part 1 of the command) reads the older code against each baseline row and keeps only findings that cite a rule line and a code line it has read; the brain merges the four parts into one table `kind | finding | place A | place B | count | proposed verdict` for the owner; the step fixes nothing, and a confirmed finding becomes its own ticket.

## Issue categories and relations

Beyond the five triage states (`docs/agents/triage-labels.md`):

- **Category**: every issue carries exactly one of `bug` / `enhancement` next to its state label.

| Category | Meaning |
| -------- | ------- |
| `bug` | Something is broken: code, a run or a tool does the wrong thing (a defect, a leak, a wrong number, a race). |
| `enhancement` | Everything else: a feature, a refactor, a measurement, a decision, research, a chore. |

- **A parent** (an issue with open native sub-issues) carries a category and no state; its
  sub-issues carry the states. It closes when its sub-issues are done.
- **A parked issue** (ORCH waits on a job it submitted instead of sitting on it, `docs/agents/orch.md`) has its
  `ready-for-agent` removed and no state until the job's result re-adds `ready-for-agent`.
- **A blocked issue** keeps its real state: an open native blocker (`gh issue edit N --add-blocked-by M`) is what
  holds it back. The dispatcher never hands out an issue with an open blocker, so there is no "blocked" state
  and no label is removed to hold an issue.

`gh` conventions for these relations (`docs/agents/issue-tracker.md` holds the rest of the `gh` conventions):

- **Category**: apply with `gh issue edit <number> --add-label bug` / `--add-label enhancement`
  (`docs/agents/triage-labels.md`).
- **Blocking** (GitHub's native issue dependencies, gh >= 2.101): `gh issue edit N --add-blocked-by M` (N waits for
  M) / `--remove-blocked-by M`. Read with `gh issue list --json number,blockedBy`: `blockedBy.nodes` lists CLOSED
  blockers too, so filter by `state`. Keep a `Blocked by #M` line in the body as the text fallback.
- **Parent and sub-issues**: `gh issue edit P --add-sub-issue C` or `gh issue edit C --parent P` (`--remove-sub-issue`
  to undo). Read with `--json parent,subIssues,subIssuesSummary`. The native link is the only one: never write a `Part of #P` line in the child's body.
- **Create with relations**: `gh issue create --title ... --body ... --label enhancement --label needs-triage
  --blocked-by M --parent P`.
- **A finding about the orchestration core** goes through `python -m tools.agentops core issue`, not a plain
  `needs-triage` issue here; unsure whether a finding is the core's own, file it as this repo's own `needs-triage`
  issue instead. An agent of the home reads, in a target's folder, the dispatcher's logs and the transcripts of
  that repo's ORCH sessions only, read-only, to diagnose a core issue.
- **The monitor's general part** (the monitor's files listed in `tools/agentops/core_manifest.txt`) is core like any manifest file: a bug in it is a core issue, filed at the home with `python -m tools.agentops core issue`; the repo's add-on (`hooks/addon.tsx`, `project.ts`) is the repo's own.

## Tickets and labels

The ticket and label rules of the core; Pocock's own `docs/agents/issue-tracker.md` and `docs/agents/triage-labels.md`
hold only what Pocock's setup writes.

### Issue text

- English, like every doc in the repo. Domain terms verbatim as the tools of the domain write them.
- An issue that touches what the repo measures names the metric and the benchmark that prove it, and a goal with a result checked on the whole class of cases, one of them not named in the ticket (skill `write-ticket`). Done means that result, not a schema check.
- A decision the issue settles goes into an ADR only when it meets the bar in `docs/agents/domain.md`; the issue links the ADR.

### How a ticket is built

- **Exactly one area**, named at creation (`gh issue create --label <area label>`). An area is a label that says
  what part of the project the ticket belongs to (`GLOSSARY.md`: Area).
- **The parent is named at creation** when the ticket is a subtask by substance: a native sub-issue
  (`gh issue create --parent P`, or `gh issue edit C --parent P` later), never a line `Part of #N` in the body.
  A ticket that is nobody's subtask carries its area alone.
- **A map is cut into streams**: 5 to 10 streams from 30 tickets. A stream is a parent issue whose title starts
  with `Stream: `; it has no label of its own and is open while it holds an open ticket (`GLOSSARY.md`: Stream).
- **Filters**: by area, `gh issue list --label "<area label>"` (web UI: `is:issue label:"<area label>"`);
  by parent, the parent's own page (GitHub lists its sub-issues there) or, from a script,
  `gh api repos/<owner>/<repo>/issues/<P>/sub_issues`. The issue search takes no `parent-issue:` qualifier here
  (every form of the value finds 0; GitHub documents it for Projects views only).

### The rule of labels

A label of the tracker is one of three kinds:

- one of the labels listed in `config/core_labels.json`: Pocock's labels (the triage roles of
  `docs/agents/triage-labels.md`, `bug`, `enhancement`, the `wayfinder:*` labels) or the core's own switch labels;
- one of the repo's areas (`docs/agents/workflow.md`, "How a ticket is built");
- a label a program reads as a switch, named with its reader in `docs/agents/local/issue-tracker.md`.

Nothing else is a label. A label is created by the brain alone, with one line to the owner. An agent that wants
a label writes the wish into its ticket and does not create it.

### The sixth label: `parked`

`parked` means: waiting on a named event; not worked until it comes. It is orthogonal to the five roles: a parked
ticket keeps its role label. The dispatcher never offers a `parked` issue. The ticket carries a `Parked: <event>`
line at the top of its body, and whoever posts the event removes the label.

When the event is machine-checkable, the `Parked:` line takes one of three forms. At each pass the dispatcher checks
the open `parked` issues of its repo; when the event holds, it removes `parked`, re-adds `ready-for-agent` and posts
one comment whose first line is `<!-- parked-return -->`:
- `Parked: until <YYYY-MM-DD HH:MM>` (local time);
- `Parked: <hours> h after core <sha> reaches <owner/repo>` (the hours count from the moment the target's core marker holds the sha or a descendant);
- `Parked: until #N closes` or `Parked: until owner/repo#N closes` (also wired as a native blocker when the repo allows).

Any other text after `Parked:` is a human wait (an answer, a letter, an owner's step): it comes back as before, when
whoever posts the event removes the label, or in the next sweep the owner asks for. A ticket with `needs-info`, `ready-for-human` or
`wontfix` is never flipped.
