# ORCH: the executing chat

What this repo adds -- every run of its heavy tool, the run queue, the machine budget and the numbers measured here --
is in `docs/agents/local/orch.md`, read it when it exists.

Orchestration v2, settled in the owner's grill. ORCH is Sonnet 5.5 at effort high, rollback Opus 5.5 high (one background session started by
the dispatcher; see "Start and end of a session"). Role file: `.claude/agents/orch.md`.
It executes `ready-for-agent` issues end to end, **every ready issue at once, in parallel through its own subagents**,
and closes each with its numbers; it messages the brain (`docs/agents/brain.md`) only for `needs-info`.
Cost is ~97-99% cache reads, so cost is about turns x context: short contexts and few turns beat cheaper models.

## One ORCH, every ready issue at once

As few live ORCH sessions as possible: a further one starts only when at least one ready issue exists and every
live session is finishing (below). Each session holds as many issues as are ready -- each cut into units that run
side by side (below) -- and merges each itself; when more than one session lives, `main`'s several mergers still take turns through one
critical section (below). No cap on the number of issues a session holds (the owner, 20:20: "the more the better",
it does not change the tokens per issue); only these bound it:

- **Code overlap.** An issue whose code area clearly overlaps one ORCH already holds is not taken; it waits for the
  overlapping one to merge.
- **The machine.** A subagent adds nothing measurable to ORCH's own process (`claude.exe` 355 MB before and
  while a subagent ran); its load is what its tools start. So: a subagent whose spec starts heavy local work runs
  only after the machine budget's own check says GO (the check, and the queue a long or wide job goes through, are
  the local file's); any other subagent starts while the PC has >= 2 GB free RAM
  (`Get-CimInstance Win32_OperatingSystem`, `FreePhysicalMemory`); below that the issue waits.
- **The API rate limit.** On a rate-limit error ORCH backs off (1, 2, 4, 8 min) and retries the same subagent; it
  never drops or parks the issue for it.

**The offer cap** `orch_offer_max` (`config/orchestration.json`, default 4) is the most tickets the dispatcher offers one session in its first prompt, oldest first; it is not the N of T = B + N x X below (N there counts the issues this context has taken).

**The context threshold T = B + N x X** (grill Q22). B = ORCH's context right after its start, X = the context
one issue adds, N = the issues this context has taken (held now plus finished in it). B and X are measured, never
argued: `python scripts/token_meter.py --orch-threshold` over the finished one-issue ORCH contexts prints them, T for
each N, and the ceiling T never passes -- the context line of ORCH's own model, over which it claims nothing new and
finishes what it holds, as over T. Today's B, X and ceiling are the local
file's. X was measured while ORCH still did each issue's code itself; with the code in subagents and ORCH holding
only their reports (<= 30-60 lines) X should fall -- the brain reruns the meter weekly (`--since` a week ago) and
rewrites these numbers. The subagent share of a session set: `python scripts/token_meter.py --subagent-share --sessions <ids>` or `--orch-title <t>`. The same command prints the live ORCH's own context against its T. Over T: ORCH claims
nothing new, finishes what it holds (parking for context only past the hard line, below), rewrites `docs/STATE.md` (its own entry written under the merge
lock through `python -m tools.agentops commit -m <message> --state-entry "<text>" --state-as <writer>
docs/STATE.md`, never a hand edit of the shared `Updated` line). A commit message holds no close/fix/resolve form before a ticket reference (`closed #N`, `fixes #N`, `resolves #N`): GitHub closes the ticket when the commit reaches main, and the layer's commands now refuse such a message; write `Refs <URL>` or `Ticket: <URL>`, or "finished #N". The block keeps the entries of 6 writers; past that the entry with the oldest own time leaves, never the one being written., and ends its turn; the dispatcher then starts
a fresh ORCH. An issue that grows large is otherwise left to the default compaction (~90%); its checkpoint comment
is what it recovers from.

**Say you are finishing the moment you decide to take nothing more** (over T, at the context line, or what you
still hold will bring you to it) -- `python -m tools.agentops finishing [--session <id>]`, run once, the same turn.
This is what lets the dispatcher start a further session at once instead of waiting for your context to end on its
own; finishing is final, so say it only once you mean it. A wake naming an issue you decide not to take prints the
exact command with your own session id filled in. `finishing` itself posts a "still working" note (not a park) on
every open issue you still hold whose last checkpoint does not park; the end path reads that note as your own
checkpoint. The note stands in for your checkpoint on each open issue you still hold (a progress checkpoint may follow, its first line never a park); a closing checkpoint still closes; a park line names only a wait outside the session or context past the hard line (below), steps in "Working an issue", "Long runs".

**Over the context line you take nothing new and finish what you hold.** You park a ticket for context only past
the hard line (`context_hard_line` in the settings file); before it, a ticket is parked only for a real wait (a
merge, a run, a blocker). A parking checkpoint says why: `context` with the number, or the wait it names.

## On wake

The dispatcher (`tools/agentops/dispatch.py`) starts every ORCH session with the ready queue already in the prompt:
`Take every ready issue (ready now, in dispatch order: #N, #M, ...). Follow docs/agents/orch.md.` This is the whole
first turn of a fresh context. ORCH never relies on an earlier conversation: each issue's last checkpoint comment and
`docs/STATE.md` are its whole memory.
A dispatcher message "#N is now held by <session>: post nothing on it, ..." ends this session's work on #N the same turn: no comment, no merge, no subagent, monitors and runs on it stopped.

1. Read `docs/STATE.md`. The dispatcher lists only open, `ready-for-agent`, unassigned issues with no OPEN native
   blocker (`gh issue edit N --add-blocked-by M`); a blocked issue keeps its label and is simply not listed.
   **An issue a local lane has taken is not yours**: the lane's label is the answer: take only issues the lane's label does not mark, even when
   re-reading the queue by hand (`gh issue list --label ready-for-agent --search no:assignee` shows it). Which label
   that is, and what the lane does, is the local file's. **Between sessions nothing is checked before the claim**:
   another live session may hold issues of its own; an issue it already claimed simply never shows up unassigned,
   and a collision, if the two turn out to touch the same code, is met at the merge.
2. For each listed issue, in order: read its body and last checkpoint comment; skip it if its code area overlaps
   one already taken or the machine cannot take it yet (above); else claim it
   (`gh issue edit N --add-assignee @me`; one GitHub account for all agents, so "assigned" means "someone is on it" --
   an issue already assigned is someone else's, skip it; a session that takes over an issue another session held
   writes a checkpoint naming itself before any other work on it, so the dispatcher's stale-assignee rule sees it)
   and start its subagent in the background (the routing
   table below), then move on to the next without waiting. A dispatcher restart keeps every ticket a live session holds or was offered, because wakes and offers are persisted in the state file; a ticket whose holders are on record is freed only when every one of them is gone (its recorded pid is dead; a holder with no pid on record is not judged gone).
3. While the subagents run, ORCH may end its turn: each subagent's completion wakes it (the dispatcher never ends a
   session whose subagents still run). ORCH keeps a subagent running for every issue it holds, or parks or finishes the issue with a checkpoint before
   ending its turn; an issue held with nothing running is read by the dispatcher as "ended without a checkpoint". On each worker's report, first stop what that worker left running (`python -m tools.agentops stop --worker-worktree <wt-name>`, which stops only processes whose command line holds that worktree's path) and start no wait on that worker's files; then judge it against the issue's
   "Done when", merge, close with the numbers, and write its outcome line (`python -m tools.agentops outcome
   --issue N --route <agent type>/<model> --class accepted|redone|wrong-fact|abandoned --why "..." [--session ID]`);
   then re-read the ready queue
   (`gh issue list --label ready-for-agent --search no:assignee`, skipping natively blocked ones) and claim what it
   can take into the freed place, unless the context is over T. A dispatcher wake (a message naming a ready
   issue this session does not hold, sent while it sleeps between reports) is handled the same way, as if a
   subagent had just reported: re-read the queue and claim what fits. An issue offered in a session's start
   prompt gets no wake in the first 3 min after that start; an issue that was not offered is woken as before.

## Working an issue

- **Checkpoint.** Per issue: at every milestone write an issue comment: `done / next / numbers / branch` (plus job id and
  acceptance rule when parked; the orch-it clone is named by the setting `orch_it_clone`, never by a path, and a session finds it with
  `python -c "from tools.agentops import core_settings; print(core_settings.orch_it_clone())"`). Whoever picks the issue up next resumes from that comment. A checkpoint carries no
  clock time written by hand: the tracker stamps it. A time written anywhere else (the state file, a report to the
  brain, a parked job's note) is read in the same turn from the clock (`date "+%Y-%m-%d %H:%M:%S"`) or from a tool's
  own output, never reckoned. A defect of the core met while working the issue, and worked around rather than fixed
  here, is written the moment it is worked around, through `python -m tools.agentops core issue`,
  and the checkpoint links it. A draft of a comment, a table or a report is written in the session's scratchpad
  directory (the harness names it in the session's environment block as 'Scratchpad directory'), never in the repo: the repo holds only what is committed.
- **An issue's code is cut into units before any of it is delegated**, so that all held issues move at once and no
  unit outgrows a single spawn. A unit is one outcome with a test command of its own and its own places to read,
  small enough that a `worker` ends it under the worker's context line with no hand-back; an issue that is already
  one such unit stays whole, and the cut says so in one line with the reason. The cut is written down before the
  first worker starts: the issue's first checkpoint holds a table, one row per unit -- `unit` (k of n and a short
  name), `outcome`, `files it edits`, `its test`, `needs` (the units whose result it needs, or `none`), `part` (the ticket part the unit belongs to, a number).
  **Ticket parts.** The cut also groups the units into ticket parts: a ticket part is the units whose outcome works on `main`
  without the other parts (its own test green; nothing on `main` after it reads a half-built thing, such as a setting no
  code reads yet or a call into code not landed). An issue whose units cannot stand apart is one part, said in one line
  with why; it keeps today's flow. Every unit
  whose `needs` is `none` starts in the same turn, each worker in its own worktree, cut from the same pushed head
  (ORCH makes it itself, `python -m tools.agentops worktree add <name> --base <the issue branch>`, which puts it in `.claude/worktrees/wt-<name>` inside the project, never through the
  Agent tool's `isolation`, which loses its working directory on this harness build);
  a unit with a `needs` starts the moment the units it names are merged into the issue's branch, and waits for no
  other unit. Two units running at once share no file; where the cut cannot avoid it, one names the other in
  `needs`. ORCH merges the units' branches into one branch of the issue, runs the issue's own tests there and the prequeue step (Merging below), and each ticket part goes into `main` by one merge, the suite run once per part,
  through the merge command. The moment a ticket part's last unit is merged into the issue branch and prequeue passes, ORCH
  queues that ticket part's merge (below) and goes on with the other parts, never waiting for them. The issue stays open
  and assigned to the same ORCH between ticket parts: the merge result re-adds `ready-for-agent`, and the assignment keeps the
  dispatcher from offering the issue to a second session. A session that ends between ticket parts writes the parking
  checkpoint as today. A ticket part whose checks fail goes back to its own units; the other ticket parts go on landing. A lookup (a read, grep, list or find of a file
  not in ORCH's context, a log turned into a table) goes to `researcher-find`, or `researcher-how` for how and why; ORCH's own
  shell is plumbing (gh, git, pytest, `agentops`), files in its context and a piece it works itself in at most **10 own turns**, else it delegates (prices: the local file's). A piece ORCH works itself is named in the issue's checkpoint before it starts: the
  piece and the own turns it expects; the closing comment gives the turns it took. **The places first**: before a `worker` or `worker-strong` spawn ORCH runs one
  `researcher-find` on the issue -- which files and line ranges the change touches and what stands there, one line
  each -- unless the issue already names the ranges; the worker's prompt carries them verbatim under the heading
  `Places to read`. The routing below is `python -m tools.agentops routes`'s starting
  point, not its ceiling: it is recomputed from the outcome log, so it drifts as `routes` shows a route's
  measured accepted/redone/wrong-fact rates and its median cost and tokens, not by feel; every delegation prompt
  names exactly one `#N`, and every code-worker prompt also carries `Unit: <k> of <n> (<name>)`, the unit's test file and the report lines its unit owes (core block, "Skills are hard steps"); every worker, worker-strong and researcher-how prompt names `GLOSSARY.md` as the first read; a researcher-how that must write its one research file is given a worktree ORCH made with `worktree add`, its prompt carries that `Worktree: <path>` line, and the file is written at `Worktree: <path>`/docs/research/<file> and reaches main with the merge of the ticket part whose branch holds it (a prompt that needs no research file carries no Worktree line: the researcher answers in its report only); every delegation prompt also carries this line verbatim: `A permission-guard refusal (its reason starts "permission guard, red line") is final: never retry it in another form or as separate calls; write the exact call and the red line into the issue (a subagent names them in its report). A worker git or process guard refusal names the allowed form itself: follow it.`; and every delegation prompt carries this one too: `Run only your change's own test files and the tests this prompt names, never the whole suite (the merge runs it once); before anything that starts the repo's heavy tool, even a short call, run the machine-budget check and wait for GO.`; and every code-worker prompt carries its worktree's absolute path on a line of its own (`Worktree: <path>`) and a third line: `Every command runs with that path: git -C <path>, cd <path> &&, PYTHONPATH=<path>/src; never the main checkout. var/, vendor/, artifacts/ are already linked there; never type mklink.` and a fourth line, verbatim: `Commit in your worktree with the numbers in the message; never push: ORCH merges the local branch.`; the outcome
  command takes `--unit <k>/<n>` and writes it into the outcome row (the token meter and the monitor attribute
  cost and rows by it):

  | case | to | report |
  |---|---|---|
  | find, list, grep, a log into a table | `researcher-find` (Haiku) | <= 40 lines |
  | explain a large code area, what a run did (a research file: in a worktree ORCH made, see the delegation rule) | `researcher-how` (Sonnet 5.5) | <= 60 lines |
  | a unit's code + tests from a precise numeric spec | `worker` (Haiku 5.5 high), in a worktree ORCH made | <= 30 lines |
  | an uncuttable unit of subtle code at the heart of what the repo measures | `worker-strong` (Sonnet 5.5 high), in a worktree ORCH made | <= 30 lines |
  | the check of a verdict-changing claim, read-only, in a fresh context | `worker-strong` (Sonnet 5.5 high), no worktree | <= 30 lines |
  | Reddit and forums | external researcher, `docs/agents/research.md` | the brief's report |

  In `routes` the roles read `worker/haiku-5.5` (bare `worker` rows before the pin read `worker/sonnet-5.5`), `worker-strong/sonnet-5.5` and
  `researcher-how/sonnet-5.5`; the rows `<role>/sonnet-5` are history, from before the roles were pinned to Sonnet
  5.5, and route nothing now.

  A unit goes to `worker`; `worker-strong` takes only a unit of subtle core logic that cannot be cut further. An
  issue of two or more units is never handed whole to any model, whatever the model.

  **Long investigations are split, never dropped.** An investigation expected past the measured line is cut
  into independent questions before delegating, one short `researcher-how` each, run in parallel; one that cannot be cut is set aside as
  `needs-info` ("Outside the spec" below) with what is known and one message to the brain, never sent to Opus. Its measurement and arithmetic: `docs/research/*-orch-history.md`, section `orch.md:134-139`.

  **Review is the skill, on every unit.** Every unit diff goes through `mattpocock-skills:code-review` (the skill, against the issue)
  before it joins the issue branch. The skill's two sub-agents are the roles `reviewer-standards` (Haiku 5.5 high, `.claude/agents/reviewer-standards.md`)
  and `reviewer-spec` (Sonnet 5.5 high, `.claude/agents/reviewer-spec.md`); ORCH passes no model or effort by hand, and
  never Opus. The two roles have no shell: ORCH writes the unit's `git diff <fixed-point>...<branch>` to a file in the
  scratchpad and each reviewer prompt names that file and the worktree path, the issue's text for `reviewer-spec`.
  ORCH records `mattpocock-skills:code-review: N findings, M fixed (reviewer-standards / reviewer-spec)`
  and runs no review rounds beyond it. A verdict-changing claim on a number of the heavy tool goes to `worker-strong` (Sonnet 5.5) in a fresh
  context (the table's claim-check row), never judged by ORCH's own reading.
  The Standards source of that review is `CODING_STANDARDS.md` at the repo root, and the `mattpocock-skills:code-review:` line cites a rule from it. The harness's built-in `/code-review` is not this step and writes no report line.

  DeepSeek is paused until the owner re-enables it. No foreman: the worker runs its tests and reports the spec's
  numbers and the report lines of core block "Skills are hard steps"; ORCH reads the diff stat and the hunks that
  change the measured numbers, and a missing line sends the report back like a missing number. A failing issue is
  ORCH's own work, after `diagnosing-bugs`; Pocock's skills are used as shipped; the catalogue costs 5.7k of its base context, so B's bar is <= 41k. A verdict-changing claim of a subagent is checked against the repo's own tool before use. A context ends
  only when none of its subagents is still running. **Judging a report includes writing its outcome line**:
  `python -m tools.agentops outcome --issue N --route <agent type>/<model> --class <accepted|redone|wrong-fact|
  abandoned> --why "one line" [--session <id>]` -- this is what makes the table above recomputable, not decoration.
- **Red before the ticket.** Before a unit's first edit, ORCH runs
  `python -m tools.agentops red-on-base --cwd <base worktree> --tests "<the unit's selection>" [--ticket N]`
  on the unit's base commit, once per selection, never one prequeue run per test; every test red there is
  "red before the ticket". Each goes into `tests/red_on_main.txt` as `skip <nodeid or path> # Tests red on main before #N`,
  all naming one ticket: exit 3 means no ticket is known yet, so ORCH opens one with the list and runs the command
  again with `--ticket N`. A second unit on the same base leaves the list unchanged unless it finds new reds; only
  those are appended to that same ticket. A family of reds with one cause is listed in one pass. The unit never
  edits the file or fixes such a test; a ticket whose own spec is those tests is exempt. The plugin that skips the
  listed ids (`tools/agentops/red_on_main_plugin.py`) is a core file; the list is the repo's own, no core update touches it.
- **Send-backs and the loop stop.** When ORCH sends a result back it writes a comment whose first line is
  `<!-- send-back -->`, then the reason; when something of the ticket is accepted it writes `<!-- accepted -->`. At the
  third failed attempt in a row (a send-back or a merge result that did not land, except a withdrawn or superseded one; a landed merge or `<!-- accepted -->`
  restarts the count) since the newest of `<!-- owner-answer -->` (the owner's words, verbatim) and
  `<!-- brain-answer -->` starts a background `unblocker` on
  Fable 5.1 high (`.claude/agents/unblocker.md`), started by the dispatcher, not by ORCH. Its one comment, first line
  `<!-- brain-answer -->`: first a `Loop stop` section (what was asked in the asker's words; one row per attempt with
  its reason; which reason repeats; what the agent cannot see, measure or reach alone), then why the attempts fail and
  the way as concrete next steps for ORCH; it solves nothing itself, and hands the ticket back (`ready-for-agent`,
  unassigned); only when a choice is the owner's own, it writes `for the owner:` and the ticket waits as
  `ready-for-human`, asked through the brain. A ticket with three failed attempts in a row is the unblocker's: ORCH
  builds nothing on it.
- The alias `sonnet` is never passed as a model of a spawn: in Claude Code 2.1.280 it runs `claude-sonnet-5`; a Sonnet spawn is a role whose file pins `claude-sonnet-5-5`.
- **Context hand-back.** A report that starts with `Context hand-back` is not judged against the issue's "Done
  when": ORCH spawns a fresh worker of the same type on the same issue, from that branch, with the report's note
  as its `Places to read`; no researcher is run again. A hand-back that brought no new commit is not continued:
  ORCH writes a checkpoint on the issue instead.
- **A stopped worker is re-spawned in the same worktree.** The worktree is ORCH's, made with `worktree add`, so it
  outlives the spawn: a worker that stopped is spawned again with the same prompt and the same `Worktree:` line, and
  goes on from what that worktree holds.
- **Short calls to the repo's heavy tool** (within the machine budget's smallest share, a few minutes at most) run
  directly in the `others` share after that budget's own check, and ORCH waits for them. For a planned burst of them,
  capacity is reserved at planning time. The check, the share and the reserve command are the local file's.
- **Long runs are not waited for.** Anything longer than ~5 min or wider than the budget's smallest share is
  submitted to the run queue the local file names, with a priority: P0 blocks a
  merge or a decision ORCH waits on, P1 is an open issue's benchmark, P2 campaigns and exploration. A run that needs
  another run's output is submitted at once with `--after <job id>` (repeatable): the daemon holds it until every
  named job has ended rc 0, and nobody has to sit and watch the predecessor. An unrelated job (no `--after`) still
  starts while a chain waits; a dependent one keeps its place in submit order. If a predecessor ends rc != 0 the
  follower stays queued for ever and the daemon raises a `held_by_failed_predecessor` anomaly for whoever owns the
  queue --
  requeue the predecessor or cancel the follower. Then write the
  checkpoint with the job id and the acceptance rule ("merge if ..."), remove `ready-for-agent` and your assignment:
  the issue is parked and its place is free for the next ready issue. A parked issue's job carries `--issue N` (the
  queue refuses a submit with neither `--issue` nor `--no-issue`); without it the hand-back posts nowhere and the
  issue never returns. When the job ends the daemon posts the result into the issue
  and re-adds `ready-for-agent`; the issue is then an ordinary task: judge the run by the rule, merge or send back.
  A finishing session never resumes an issue it parked: the hand-back of a parked issue goes to the pool, and the
  next session judges it by its last checkpoint. A session that is not finishing may resume its own parked issue
  when the hand-back comes (it is the one session taking new work); its re-claim makes the issue held by it again.
  The dispatcher does not end an idle session that is not finishing, holds no claimed issue and has an issue parked on a live merge (`parked on merge <result file>`, younger than the merge lock's ceiling plus 5 min); it logs `idle, waiting on merge of #N` and wakes the session when the merge result makes the issue a candidate again (a dispatcher restart forgets what a session parked earlier, so it ends as before).
  An issue parked on a wait that is not a daemon job comes back through its label: whoever posts the result of
  that wait (the run's owner, the brain, ORCH itself on a live run) re-adds `ready-for-agent` in the same step as its
  comment. A wait with a machine `Parked:` form (`workflow.md`, "The sixth label") needs no one: the dispatcher flips it. Parking on a machine-checkable event
  writes that `Parked:` line at the top of the body with `gh issue edit N --body-file <draft in the scratchpad>`
  (after `write-ticket`, as every body rewrite).
  The parking checkpoint's FIRST line is `<!-- parked-on-run: <what it waits for> -->` (a run name, a date or a live row
  in plain words); the rest of the comment is free. A ticket that goes back to `ready-for-agent` leaves the line by
  its label alone: no "unparked" marker exists.
- **Provisional decisions.** ORCH runs `python -m tools.agentops veto N` / `python -m tools.agentops approve N`
  when an issue asks it to. Every commit ORCH or its worker makes for an issue whose body holds `Provisional: #N`
  carries the trailer `Provisional: #N` of the home repo. A veto runs the whole test suite, so run it after the machine-budget check.
- **Merging.** A worker never merges into `main` and never pushes; ORCH starts every merge from the main checkout (a start from a worktree is refused). Before the merge is queued, ORCH runs `python -m tools.agentops prequeue --cwd <issue worktree> --issue <N>` (one `--issue` per issue the branch closes; without it the number comes from the branch name) on the issue branch and queues the merge only on exit 0; any other exit queues nothing. Exit 1 prints the failing test ids: that red goes back to a unit of the issue, its prompt carrying those ids, and no merge is queued. prequeue runs again after every change to the issue branch (a unit re-run and merged in). `prequeue: skipped, full suite median <N> min` with exit 0 is a pass. Right before `tools.agentops merge`, ORCH reads the issue's comments newer than its own last
  checkpoint (`gh issue view N --json comments`); one that withdraws, stops or changes the spec stops the merge, and
  the issue gets a checkpoint naming that comment (closed as not needed, or re-specced) -- a spec withdrawn
  mid-work once reached `main` this way. The merge tool itself drops a merge of a ticket closed as not planned: it reads the ticket's state when the merge takes the lock (and for each waiting merge its holder batches), pushes nothing and names it in its result line (a failed read merges as today). `main` may have several mergers at once (more than one live session, and the brain and the autopilot
  commit in the same checkout too), so the whole sequence -- sync with `origin/main`, merge, the suite, push -- is
  one critical section: one session runs it at a time, holding a lock a dead holder never blocks. A conflict or a
  red suite leaves local `main` at the head it had before the attempt and pushes nothing, and names which of the
  two it was; a red suite's line also names the failed tests (up to 5, then `and <k> more`) and the path of the
  suite's own kept output, both logged too, one line per attempt. A test that fails in the suite and fails again when re-run alone ends the suite at once (fail-fast), so a red line may name it before the suite's end. A merge superseded by a newer head of its branch queued is ended at once. A merge that would touch a file with uncommitted
  changes is refused up front, before anything is touched, and a rollback never discards an uncommitted change the
  merge did not touch. A push refused because
  `origin/main` moved is answered with a fetch, a fast-forward onto the new `origin/main` and a re-merge, the suite
  again, a second push -- never a forced one; a conflict in that re-merge goes to the worker of the later branch. A conflict `agentops merge` reports (or the sync with the base branch) is worked by the one who resolves it (ORCH, or the worker it is sent to) directly, without a skill; a delegation prompt for a conflict carries that sentence, and the result line is `conflict: resolved, N files`.
  A merge whose suite would need the heavy tool sends that suite through the run queue instead
  of running it inside the merge command; the merge command's own suite and its environment otherwise come from
  the settings file (`merge_suite`, `merge_suite_env`), and `--suite` is passed only when the issue's own spec
  names a different one. The suite runs in one standing suite worktree of the repo, brought to the merge commit
  with the shared directories linked in (`merge_suite_shared_dirs`), so no uncommitted file of the main checkout
  changes its result; that worktree is never deleted. After every merge into `main` and its tests, `git push origin main` before the next
  worker's branch is cut: a worker prompt always branches from the pushed head, never from a local `main` that
  outruns `origin/main`. A
  change to what the repo measures merges with its benchmark numbers (this step's rule); how such a change is judged is
  the local file's. The issue is closed as "Closing" below says; rewrite `docs/STATE.md` when runs or
  constraints change, its own entry written under the same lock through `python -m tools.agentops commit -m
  <message> --state-entry "<text>" --state-as <writer> docs/STATE.md` (never a plain `git commit`/`git push` on
  `main` in the main checkout, and never a hand edit of the shared `Updated` line: the command brings the checkout
  up to `origin/main` itself and writes the entry under the lock). A change to any line of `docs/STATE.md` other than the writer's own `Updated` line is saved by a second, plain `commit` call of that path; the entry flag never carries it. An ORCH's `--state-as` is `ORCH <8-char session id>`
  (the shape of every other ORCH line: only `ORCH ...` lines are cut first when the block is full, so the brain's hand-off
  line survives), the entry's text holds a full `YYYY-MM-DD HH:MM` (the command refuses one without, exit 2) and does not
  repeat its own `Updated (...):` heading. The wait behind another holder can now run
  up to the lock's own age ceiling (a setting,
  tens of minutes by default) -- far longer than a plain foreground tool call is ever left running, and a
  session's own background shell can be stopped by the harness while it idles -- so the merge and the commit
  command are always started with `--detach`: the command runs as a process no session's shell holds, returns at
  once with the path of its result file (rc, the pushed sha or the reason such as `red-suite`, the suite's output
  file) and adds one `merge.log` line when it ends. **ORCH never waits for its merge to land**: a merge is
  always queued as `python -m tools.agentops merge <branch> --issue N [--issue M ...] --detach`, one `--issue` per
  issue the branch closes; right after, ORCH writes a parking checkpoint on each such issue (`parked on merge
  <result file>`, the numbers to re-read on `main` and the command that reads them), removes `ready-for-agent` and
  its assignment as for a long run (above), and never polls the result file. When the merge ends, the detached
  runner posts one comment on each issue still open (first line `<!-- merge result -->`: landed on `main` with its
  sha, or did not land with the reason, the time in line, the suite's time and failed count, the result file and the
  suite output) and re-adds `ready-for-agent`; a `nothing-to-merge` or `already-queued` outcome posts nothing. A merge of a ticket part that is not the issue's last differs in two lines: the checkpoint after queuing it names the ticket part (`part k of n`) and its result file and parks nothing, and the issue keeps its assignment, because units of the other parts still run; when its `<!-- merge result -->` lands ORCH writes a checkpoint with the sha and goes on. Only the last ticket part's merge parks the issue as above, its parking checkpoint naming `part n of n`. ORCH withdraws only its own ticket's merge, with `python -m tools.agentops merge withdraw <issue|branch> --why "<one line>"`; never another ticket's (the brain withdraws any merge it knows is doomed).
  A State commit (`commit --detach`) carries no issue and posts nothing. A holder still not done past the lock's
  ceiling is named, not waited on forever: the merge command exits with a code set aside for exactly this
  (`lock-timeout`, one line naming the holder's pid and age), and that outcome comes back through the same comment.
  On it, the session that claims the issue never deletes the lock file itself (only its holder, or whoever
  diagnoses it, may): it leaves a checkpoint naming the holder and sends one message to the brain, the same as any
  other stall it cannot resolve alone.
- **Closing.** The closing comment answers the issue's acceptance table again, one line per row: the number, the
  exact command that printed it, and the commit it was read on. The numbers are read on `main` after the last ticket part
  lands (a row one ticket part makes true may be read after that part); an issue of several ticket parts is closed once, and the
  closing comment names every ticket part's merge commit; a
  number that can only be read on the branch says so and names the branch's commit. A row no command measured is
  written "not measured" with the reason; "tests pass" without a command and a count answers no row. A piece ORCH
  worked itself is named there with the turns it expected and the turns it took. An issue parked on its merge is
  closed by the session that claims it (`--add-assignee @me` first, like any wake: the claim is the dispatcher's only record of who holds an issue) after the merge's `<!-- merge result -->` comment: it reads the result file
  that comment names; on a landing it reads `main`, answers the acceptance table and closes; on a red suite or a
  conflict it sends the names back to the branch's worker and queues the merge again (never a second merge of a
  branch whose merge is still in line). A finishing session never resumes it ("Long runs" above). The session that queued a merge is kept idle for it (by the dispatcher) and closes the issue itself when it is not finishing.
  ORCH closes only through `python -m tools.agentops close <N> --comment-file <file>` (the file is a draft: the session's scratchpad). The command lists every comment that is neither a machine marker nor a checkpoint and was written after the issue text's last edit, and refuses to close until the closing comment holds, for each, one line `comment <id>: done in row N`, `comment <id>: moved to #M` or `comment <id>: not a task`.
  A monitor ticket's live row is read from the installed pane (or `mods status <monitor.source>` -> 0 differ first), never from the repo's collector alone.
- **Outside the spec.** A decision the issue does not settle: label `needs-info` and take `ready-for-agent` off (the
  dispatcher offers an issue carrying `needs-info` to nobody; the brain puts `ready-for-agent` back with the
  answer), a checkpoint with the question,
  one message to the brain (unless the exception under "Report to the brain" applies), remove your assignment: the issue is parked and its place is free. The same when the
  permission guard refuses a step the spec needs: the refusal is handled as `docs/agents/workflow.md` says ("Every
  session runs in `bypassPermissions`").
- **Standing permission.** A step the run needs or that makes it faster, undone whole, is not outside the spec: ORCH
  takes it itself (what counts and what stays the owner's: `docs/agents/brain-reference.md`, "Standing permission"),
  writes in the ticket what it created and how to remove it, and names it in its next report.
- **Found outside the issue** (a bug, a gap, a missing piece of another issue): first search the open issues for the
  finding's file base name or function name, never its line number (lines move): `gh issue list --state open
  --search "<file or function>" --json number,title`; a hit is the finding's holder and gets a comment with the new
  facts only, never a second issue. No hit: open it yourself as an issue labelled
  `needs-triage`, naming its area and its parent (when it is a subtask by substance), what was found with file:line, linked from your checkpoint. No message to the brain: it triages
  from GitHub. A finding about the orchestration core itself goes a different way: write it through `python -m
  tools.agentops core issue` at the moment the defect is worked around, linked from the checkpoint, not as a
  `needs-triage` issue here. A worker or researcher only names such a finding in its report; ORCH writes the core
  issue. An agent unsure whether a finding is the core's own writes it as this repo's own `needs-triage` issue and
  lets this repo's brain sort it. A core file changes only at its home: write the core issue with `python -m
  tools.agentops core issue`, never patch the core file inside a target.
  A core update refuses while any ORCH is listed in the repo, so ORCH never waits inside its session for a core update to reach its repo: a ticket that needs a newer core is parked with its hand-off ("when main holds core >= <sha>: ...") and the session ends when it holds nothing else.
- **Sense of the domain.** Meeting the spec's numbers is not enough: before closing an issue that changes what the
  repo produces, look at the result for what is obviously wrong in the real world it models (the local file gives
  this repo's own examples). Found: `needs-info` with the case and the line. ORCH may judge that logic, it is not
  kept away from it; the brain reviews results with the owner.
- **Report to the brain.** Each closed or parked issue gets one message to the brain, at most 5 lines: the issue,
  what was done, the key numbers, anything that looks off (several issues ending in one turn share one message). The chat is the one `python -m tools.agentops brain-chat --issue <N>` prints for the issue, matched exactly against the names `ListAgents` shows, never a prefix or a similar title; if no live session has that exact name, the message is not sent to another chat and the closing comment or parking checkpoint carries it.
  No other status messages, no answers the brain did not ask for. Exception (autopilot stretch): before that message, on a ticket
  under an open autopilot stretch, run `python -m tools.agentops autopilot-stretch --issue <N>`. A printed name means
  no message: the closing comment or parking checkpoint carries it, and a question waits there for the autopilot.
  Nothing printed (rc 0) means the message as above; any other rc (the read failed) means the message as above plus one line
  in the checkpoint.
- **Memory belongs to the brain.** ORCH never writes, edits or deletes the auto-memory (`MEMORY.md` and its files).
  A lesson or preference worth keeping goes into the checkpoint
  or closing comment as one line `for the brain: ...`; the brain decides whether it becomes memory.

## Start and end of a session

Current mechanism (as few sessions as possible, each holding many issues). ORCH is never a standing chat: it is a
Claude Code background session, started and watched by `tools/agentops/dispatch.py` -- a long-running loop,
launched detached, that keeps every live ORCH session tracked and starts a further one only when every live session
is finishing.

- **Start.** Whenever at least one issue is ready and every live ORCH session is finishing (vacuously true with
  none live), the dispatcher launches `claude.exe --bg --name "orch <the issue numbers>" --agent orch --effort high
  --permission-mode bypassPermissions "Take every ready issue (ready now, in dispatch order: ...). Follow docs/agents/orch.md."`
  from the repo root.
- **Which issues a session holds** is read from GitHub every tick (30 s): the open, `ready-for-agent` issues
  assigned to the account, minus those already assigned before the session started and minus what a finishing
  session already holds (its held set is frozen the tick it becomes finishing) -- so a claim belongs to the one
  session still taking new work; the one exception is an issue a session was woken about, whose claim stays that
  session's even once it is finishing, and no stale check takes it. The dispatcher writes every live session and its held issues to
  `artifacts/runs/orch_session.json`.
- **Finishing.** A session becomes finishing, for good, the moment it says so
  (`python -m tools.agentops finishing [--session <id>]`, "The context threshold" above) or its context, read from
  its own transcript, is at or over the context line. A finishing session is never woken and never offered an
  issue; it is polled to its own end like any other. A finishing session never resumes an issue it parked; an issue a
  live session still holds (let go or not) is resolved only at that session's own end, never at another's.
  A finishing session never resumes an issue, not even one it parked itself; the parking checkpoint is the
  hand-off, and the session that later claims the issue reads that checkpoint first and continues `orch-<N>`, never a
  second branch. The dispatcher's offer of such an issue adds `parked by <session> at <checkpoint date>, continue
  branch orch-<N>` (remembered in `orch_session.json`). A finishing session with a live watch (a `Monitor` it
  armed, no ended task-notification or `TaskStop` in its transcript) is kept until the watch ends or its last
  transcript record is older than the optional setting `watch_hold_cap_minutes` (default 60, `tools/agentops/core_settings.py`).
- **End.** ORCH ends by finishing its turn after its last issue is closed or parked (or when over T) -- nothing to
  arrange. The dispatcher sees `state: "done"` (or an idle session whose issues are all closed, parked or
  `needs-info`; or any dead state such as `stopped`), checks that none of its subagents still runs, stops the
  session (`claude stop <id>`), and resolves EVERY issue the session held on its own (`_resolve_outcome`: closed,
  parked, `needs-info`, or -- still assigned -- the no-checkpoint path: a comment, unassigned,
  and `needs-info` on the third such ending in a row). An open issue with no assignee was let go by the session
  and is read as parked whatever its labels; only an issue still assigned at the end takes the no-checkpoint path.
  A restarted dispatcher adopts every live `orch ...` session it
  finds, each with its own held set and finishing mark, instead of starting another.
- **A session waiting on the owner** (idle while still holding a claimed issue, e.g. a permission prompt) is left
  running and counts as NOT finishing: the dispatcher never starts a further ORCH while a live session is not
  finishing, whatever state it is in.
- **Over `--max-hours`**, counted per session, the dispatcher stops that session, comments on and unassigns each
  held issue still claimed, and moves on. `--max-hours` bounds one ORCH session, never the dispatcher's own
  loop: the loop itself has no time limit and keeps running for as long as its own process lives. What keeps
  that process alive across a reboot, a crash or a logoff is outside the loop: `dispatch --ensure` starts one
  when none is live and does nothing when one already is, and a scheduled host task calling it on a short
  interval and at logon is how it comes back on its own. One dispatcher at a time is enforced by a lock file
  holding its pid; a second one started beside a live one refuses at once, starting and adopting nothing.

The mechanisms this one superseded, kept only so an old handover file still makes sense, are the local file's.

## Models and effort

The routing is the table above and CLAUDE.md "Roles". History of the tier route: `docs/research/*-orch-history.md`, section `orch.md:337-348`.

Also, always: "Every agent, always" in the core block binds ORCH and every spawn, and the machine budget check runs
before any heavy run. The lead may make a change of a few lines itself when a spawn
would cost more than the change. GPT workers through `tools/agentops` stay available as an option (their budget
is the vendor's, not Claude's), no longer the default route for code. A junction's target is always a path of the
repo's MAIN checkout (its shared runtime, run-output and vendor directories, its snapshots), never a path under the
worktree that holds the link, and never a path under the link itself. The case behind it: `docs/research/*-orch-history.md`, section `orch.md:365-366`. The junction makers refuse such a target.
