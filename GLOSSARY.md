# GLOSSARY.md

This repo's own file; the port only ensures it exists.

## Language

### Working roles

**Owner**:
The person the project belongs to: reads the results of the heavy tool, decides game questions, approves facts.
_Avoid_: the user

**Claude usage**:
The share of the plan's usage limit that Claude models spend, each at the weight the plan gives it; the unit of cost
per closed issue, with the brain's share shown apart. The $ API-equivalent is shown beside it, never instead of it.
_Avoid_: tokens (a raw count ignores the model's weight), cost (reads as $)

**Base context**:
What a fresh session holds before its first message: tool schemas, memory, skills, the system prompt. Every turn pays
it again, so it is the first thing cut. ORCH's B is its base context.
_Avoid_: floor (a build's feasibility minimum), fixed floor

**Context line**:
The context size past which a long-lived session hands off instead of taking new work; set per model from measured
degradation, the same for every role on that model. ORCH's cost threshold T is apart and never exceeds it.
_Avoid_: context limit, threshold (T is ORCH's)

**Context ceiling**:
The context size, above the context line, past which a long-lived session does nothing but its hand-off, whatever
is in work. Between the line and the ceiling it finishes the step in work and takes no new one.
_Avoid_: hard limit, second line

**Area**:
A sub-project of a repo, the one thing an issue is told apart by in the tracker. An issue has exactly one area; a
sub-issue has the area of its parent; a map lives inside an area and never opens one by being a map. Areas are few
and named by the owner's yes; this repo's are orchestration, builds, runs and crafting.
_Avoid_: category (Pocock's `bug` / `enhancement`), direction, component, one area per map

**Stream**:
A parent issue between a map and its tasks: a map of 30 tickets or more is cut into 5 to 10 streams, and each
stream holds its tasks. A stream is open while it holds an open ticket: it is closed with its last one and reopened
when a new one lands. It carries no label of its own.
_Avoid_: volume, second map, sub-map

**Orientation context**:
What a worker reads into its context before its first edit to learn where the change goes. Every later turn pays for
it again, so it costs more than the turns that gathered it.
_Avoid_: exploration (the turns before the first edit, not what they leave behind), warm-up

**Context hand-back**:
A worker ending at the context line before its issue is done: its step committed, and a note (done, next, files) from
which a fresh worker goes on. Always with the prefix: a job hand-back is the runs daemon posting a job's result to
its issue, and every subagent's final report is a hand-back to the harness.
_Avoid_: hand-off (a long-lived session's, to its own next context), respawn, restart

**Wake**:
The dispatcher's message to a sleeping ORCH that names a ready issue the session does not hold. ORCH handles it like
a subagent's report.
_Avoid_: ping, nudge (the reminder that tells the brain to hand off)

**Finishing ORCH**:
An ORCH session that takes no new work: it is at its context line or over its threshold T, finishes or parks what it
holds and ends by itself. It is never woken and never offered an issue. At most one live ORCH is not finishing.
_Avoid_: full ORCH (no number of issues fills one), old ORCH, draining

**Pending core mark**:
The file a home push leaves in a target's git-ignored `artifacts/runs/` when the target refused the core update: the core
version it did not take and the time. The dispatcher retries from it and clears it when the update lands.
_Avoid_: retry flag, update queue

**Further ORCH**:
An ORCH session started while another is live. One reason starts it: a ready issue exists and every live ORCH is
finishing.
_Avoid_: second ORCH (there may be a third), parallel ORCH, extra session

**Code overlap**:
Two issues expected to change the same code, judged before the work is handed out. Inside one ORCH session it is
ORCH's own reading; between sessions it is not judged.
_Avoid_: collision, conflict (both exist only after the work)

**Collision**:
Two branches that changed the same file, met at the merge. Git rebases most by itself; one it cannot is a conflict,
resolved by hand, and conflicts are what is counted.
_Avoid_: overlap (the estimate before the work), clash

**Cloud session credits**:
A dollar pool on the account that pays a cloud session's Claude usage before the plan's limit is touched; once spent or
expired, a cloud session draws the plan like a local one. A cloud session runs on a Linux VM, so it never runs the heavy tool.
_Avoid_: cloud limit, usage credits (the account's separate paid pool)

**Cloud lane**:
The route by which cloud-ready issues are worked in a standing cloud session on the cloud session credits instead of by
the local ORCH. It opens only when the owner tells the brain, never by a threshold, and closes at the week's reset,
at the owner's word, or before the credits run low.
_Avoid_: cloud ORCH, routine lane (a routine bills the plan)

**Standing cloud session**:
A cloud session the owner opened by hand (two clicks on a link the brain gives) that is fed messages for as long as it
lives. Opening one is the owner's word that the cloud lane is open.
_Avoid_: routine run session (billed to the plan)

**Heavy tool**:
A repo's own local program whose runs give the numbers a test or an acceptance row needs, and which cannot run in a
cloud session (in Poe-Junkroan: PoB). A repo may have none.
_Avoid_: the repo's heavy numeric tool (the long form), numeric tool, engine

**Cloud-ready issue**:
An issue whose acceptance needs no run of the heavy tool, so it can be worked in a cloud session.
_Avoid_: PoB-free issue (Poe-Junkroan's older name), light issue, small issue

**Worker**:
A coding agent that implements one issue or spec in its own worktree. A process of the heavy tool is never called a
worker without its own prefix (in Poe-Junkroan: PoB worker).

**Checkpoint**:
The issue comment a worker or ORCH writes at each milestone: done, next, numbers and branch (plus the job id and the
acceptance rule when parked). Whoever picks the issue up next resumes from its last checkpoint. The tracker stamps the
time; a time written by hand is not part of it.

**Closer**:
The ORCH session that takes a ticket back after its merge result is posted, reads the result's rows and closes the
ticket with its numbers, or sends it on. It is seldom the session that queued the merge.
_Avoid_: finisher, merger

**Holder**:
The merge that holds the merge lock: it runs the suite for itself and the batch it collected and lands them on main,
while every other merge waits behind it. Other holders are always named with their thing: a ticket's holder (the
session working it), a finding's holder (the open ticket that already carries it).
_Avoid_: lock owner, runner

**Fail-fast**:
The merge suite's stop at a confirmed failure: a test that fails is run again alone at once; red again, the suite
stops and the merge ends red naming that test; green alone, the suite goes on.
_Avoid_: early abort, first-failure stop

**Withdrawn merge**:
A queued or running merge ended on purpose because it is known to be doomed. It counts as no failed attempt.
_Avoid_: killed merge, cancelled merge

**Superseded merge**:
A queued or running merge of a branch ended because a merge of the same branch at a newer head was queued, which
takes its place in the line. It counts as no failed attempt.
_Avoid_: replaced merge, stale merge

**Send-back**:
A comment ORCH writes on a ticket when it returns a unit or a result to its worker with a reason: the first line is
the send-back marker, then the reason. Each send-back counts as one failed attempt.

**Loop stop**:
The written stop on a ticket at the third failed attempt in a row: a send-back or a merge result that did not land,
counted since the newest owner's words or Brain-answer. It states what was asked, in the asker's own words, one row
per attempt with its reason, which reason repeats, and what the agent cannot see, measure or reach alone. A landed
merge or an accepted result restarts the count. A ticket with three failed attempts in a row is the unblocker's: ORCH builds nothing on it.

**Unblocker**:
The background role on Fable 5.1 at high effort that the dispatcher starts, not ORCH, on one ticket that has a Loop
stop. It solves nothing itself: it writes the Loop stop and the way for ORCH as a Brain-answer, then hands the ticket
back to ORCH. Only a choice that is the owner's own goes to him as a question; that ticket then waits for him, asked
through the brain.

**Brain-answer**:
The comment the unblocker writes on a ticket to carry the way on: the first line is the brain-answer
marker, the first section is the Loop stop, then the cause and the way for ORCH as concrete next steps. It restarts
the count of failed attempts. It is never the owner's own words, which stay apart from it.

**Link failure**:
A hand-off between two roles, or a role and a step every ticket passes (the merge queue, the core push), where the
work did not reach the next one, or reached it only after a loss its records show. A kind of link failure nothing
records yet is counted as `no source`, never dropped.
_Avoid_: incident (that is one counted case of any kind), handoff bug, glitch

**Monitor**:
The owner's one local page: what waits on the owner, what the PC, the laptop and the agents are doing, and the
feed. It changes nothing except a host's mode.
_Avoid_: dashboard, `runs watch`

**Feed**:
The monitor's list of events (an issue closed, a run failed, ORCH took issues). Each event stays unread until the owner
marks it read on the monitor or the brain has discussed it with the owner; opening or refreshing the page reads
nothing.
_Avoid_: log, events tab, review mark

**Pipeline**:
The monitor's Progress-tab section that draws one line per open ticket that is `ready-for-agent` (not `parked`, no open
blocker), held by ORCH, or in the merge queue. Each line sits at its ticket's stage; the lines run furthest stage first.
_Avoid_: queue (the merge queue is one stage of it), board, backlog

**Flow fix**:
A ticket that removes a slowdown, a failure or a hang of the way tickets go from `ready-for-agent` to landed on main,
in any repo running the core: the offer, the work, the tests, the merge line. The brain marks it when it writes the
ticket; the owner adds or removes the mark by one word.
_Avoid_: pipeline fix (Pipeline is the monitor's Progress-tab section), bottleneck ticket

**Priority ticket**:
A ticket that heads every queue it stands in, in every repo on the shared PC: a Flow fix by its mark, or any ticket the
owner pushes first by his word to the brain. By default it waits for a running batch to finish and heads the next one.
_Avoid_: urgent ticket, hotfix

**Repo order**:
The owner's ranking of the repos on the shared PC, changed by his word about weekly; after Priority tickets it decides
whose test run takes the next free place, on the PC or on GitHub. A run that has waited past a fixed bound goes next
whatever its repo.
_Avoid_: repo priority (reads as the Priority ticket's mark)

**Berth**:
The place one heavy run holds in the one line every repo on the shared PC uses while it runs: the PC's own place, or
a count of GitHub machines a merge holds. Runs wait for a free place in turn, after Priority tickets and in the owner's
Repo order.
_Avoid_: seat (a place at a table, not a run's turn in a line)

**Test copy**:
A separate public repo that receives only a working repo's code and tests, cleaned of other repos' names and personal
data, so its merge tests can run on GitHub's free machines while the working repo stays private.
_Avoid_: public mirror, fork (both suggest the whole repo with its history)

**Part**:
One of the pieces a merge suite is cut into to run on GitHub, each piece on its own GitHub machine at the same time.
Together the pieces hold the whole suite, each test in exactly one of them, and the same commit is always cut the same way.
_Avoid_: shard, matrix job (the names of the same thing in the run's own tooling)

**PC-only tests**:
The test files that cannot run on GitHub's Linux machines (they exercise Windows-only code). A merge sent to GitHub
leaves them out of its Parts, and the merge runs them on the PC beside the GitHub run; a failure among them makes the
merge red.
_Avoid_: Windows tests (the word is about where they cannot run, not about what they test)

**GitHub fallback**:
A merge suite meant for GitHub that runs on the PC instead: GitHub could not take it in time, a Part was lost for a
reason that is not a test, the Test copy was refused, or the free GitHub minutes are spent. The brain hears of it in one
line, and the next merge tries GitHub again.
_Avoid_: GitHub failure (a fallback is not a red result; a test that fails on GitHub is one)

**Stage**:
The state a ticket is in at the moment, one of six in order: `ready` (waits for ORCH), `orch` (held by ORCH, no subagent
live on it), `work` (a subagent is live on it), `merge` (in the merge queue or merging), `accept` (its merge landed, the ticket is still open while its rows are read; a landed ticket that carries `parked`
stays at `ready` drawn as waiting, with the reason and the time left from its body's `Parked:` line, and one whose newest
`<!-- send-back -->` comment is newer than its newest landed merge row is back at `ready` (`orch` or `work` if held), not here), `done` (closed: the ticket then leaves the pipeline). A Stage is the
ticket's state; a Station is only its drawn mark. In Russian text: «ждёт» (waiting on a `Parked:` line), «возвращён» (sent back).
_Avoid_: station (the mark it is drawn as), status, phase

**Station**:
One of the five marks drawn on a ticket's strip in the Progress tab, `ready` · `orch` · `work` · `merge` · `accept`, joined
by a line: passed marks filled, the current one lit, later ones hollow; a `done` ticket has left the strip. A Station is
only the drawn mark; a Stage is the ticket's state, and a stage lights the station of the same name.
_Avoid_: stage (the state it shows), step (a skill step), dot

**Accept**:
To judge a result as holding against what was asked: a report against its question, a unit against its spec, a closed
ticket against its acceptance rows. A result accepted after a send-back is `redone`, which is still accepted. In Russian
text: «принять», the stage «приёмка».
_Avoid_: approve (the owner's word on a provisional decision), pass (a test's), merge (a landing is not an acceptance)

**Per closed ticket**:
A money or usage figure of a window divided by the tickets closed in that same window: the unit every cost is compared
in, so more work done is never read as a loss. In Russian text: «на закрытую задачу».
_Avoid_: weekly total, per issue, per session

**Side thread**:
A chat the app opens off another chat, whose transcript starts with records copied from its parent chat; the app keeps
it alive after the owner closes it, so the monitor tells it apart by those copied records. In Russian text: «боковая
ветка».
_Avoid_: побочный тред, fork, sidechain (a subagent's transcript), subagent

**Autopilot**:
The brain working a goal the owner gave on leaving (one task, a set of issues, or a wayfinder map) alone, across as
many of its own context resets as it needs, until it is stuck on every front; it takes provisional decisions for the
owner, only hard stops wait, and the owner gets one review on return. It starts only after a leaving grill, keeps the
owner's autopilot terms, tells the chat brain nothing while it runs, and ends when the owner takes the goal back in
the chat brain; the chat brain and the autopilot never work one goal at once.
_Avoid_: autonomous run (a run is a run of the heavy tool), AFK (a wayfinder ticket type), night mode

**Leaving grill**:
The chat brain's grill of the owner on a goal just before an autopilot stretch: a couple of rounds of the questions
likely to come up while he is away, conditional ones on research still open, the likely ones only; the owner ends it
when he likes, and whatever stays unasked is decided as any question born during the stretch.
_Avoid_: pre-sleep grill, full grill, night grill

**Autopilot terms**:
The conditions the owner sets for one autopilot stretch on leaving, in free words, echoed back in one line and agreed
before the owner goes: most often a scope (a ticket and everything under it), a deadline, a usage percentage; custom ones and any combination too. A term may lift a hard stop only by naming it and
its limit. The owner can change them during the stretch through the chat brain.
_Avoid_: conditions, settings, mode

**Hard stop**:
A decision the autopilot never takes, even provisionally, because it cannot be undone: money or usage (the credits,
the usage ceiling, a Reset), an outward or irreversible action, a reading of the heavy tool, a one-way-door question born during
the stretch that the leaving grill did not answer. It waits for the owner, unless
the autopilot terms lift it by name.
_Avoid_: blocker, escalation, owner-only decision (retired: most of those are provisional on autopilot)

**Provisional decision**:
A decision the autopilot takes for the owner (a goal's scope, a game verdict the corpora and the heavy tool cannot settle, the
owner's taste, a reversible question born during the stretch) and builds on until the owner approves or vetoes it on return;
a veto reverts everything built on it. The brain's own decisions (code, architecture, process) are not provisional.
_Avoid_: draft decision, assumption

**Sweep**:
The brain's pass, on the owner's word, over every open ticket outside `ready-for-agent` (no state label, `needs-triage`,
`parked`, `ready-for-human`, parents and maps included). Each ticket leaves it with one of four outcomes: ready, one
plain question to the owner carrying the ticket's context, closed with a verdict, or parked on a named event; "left as
is" is not one. The owner agrees the outcomes in one table before they apply.
_Avoid_: triage (the evaluation of one issue), grooming, backlog review

**Self-improvement change**:
A change to the orchestration, its roles or their rules that the system proposes itself (a prophylaxis finding, an
audit, the brain's own finding in passing), not the owner's word and not a bug fix that a reproducing test judges. It
exists only to remove counted incidents from the logs: no counted incident, no change. A change to how the brain
talks to the owner or to his own workflow waits for his yes; any other change passes on its number alone and stays
only if its recount moves. One prophylaxis adds no more lines of agent rules than it removes (#1104).
_Avoid_: retro ticket, improvement, process tweak

**Recount**:
The second count of a self-improvement change's incidents, taken by the next prophylaxis (by the one after it when the
incident is rarer than weekly) against the count the change was opened with. A count that did not move reverts the
change; a written rule whose incident recurred is deleted or replaced by an automatic check, never kept (#1104).
_Avoid_: re-measure, follow-up check, verification

### Retired

**Lead**:
Retired by #45: the brain writes issues and talks to the owner, ORCH executes and merges (CLAUDE.md "Roles").

**Foreman**:
Retired by #45: the worker runs its tests and reports its spec's numbers, and ORCH reads the diff.

**TEST RUNS session**:
Retired by #73 and ADR 0106: the keymaster is an on-demand session that starts only on an anomaly, not a standing chat.

### Porting the orchestration

**Orchestration core**:
The part of the orchestration that turns an issue into merged work in any repo: the brain, ORCH, the dispatcher,
workers and researchers, their role files and protocols, the token meter and the outcome log. It holds nothing of the
repo it runs in, and it is the only thing ported.
_Avoid_: skeleton (a skeleton is a build's), костяк, orchestration layer, the bridge (that is the opencode part alone)

**Local extension**:
A repo's own rules and code joined to the orchestration core without changing a core file (in Poe-Junkroan: the
runs of its heavy tool, the keymaster, the machine budget, its game principles, readings and research).
_Avoid_: custom update, override, fork

**Leak words**:
A repo's own list of words (its folder, its name on GitHub, its domain words) that must never appear in a core file it
ships or takes. A repo without a list is not checked, and the check says so instead of passing; a port and a core
update give a repo that has none a starter list of its folder and its names, which stays the repo's own.
_Avoid_: forbidden words, avoid words, blocklist

**Home**:
The one repo where the orchestration core is changed; every other repo receives the change as a copy. The home is this
repo until the owner moves it into a repo of its own.
_Avoid_: upstream, master copy, source repo

**Target**:
A repo that received a copy of the orchestration core. The first target is the test repo made for the port, the
owner's working repo follows on the owner's word.
_Avoid_: client repo, downstream

**Core issue**:
An issue about the orchestration core, whichever repo met it: what broke, what looked off to an agent, a change, an
idea. It is written at the home and says which repo it came from; one written by a target holds facts of the
orchestration only, nothing of that repo's own code, data or tasks.
_Avoid_: bug report, upstream issue, defect entry

**Core epic**:
The one parent issue at the home that holds the core issues of every repo. It has no destination and no end, so it
is not a map.
_Avoid_: defect map, карта, backlog

**Core update**:
The one way a target's core files change: the home's newest core brought into the target as one commit of the
target. Every home merge runs it in every target at once, and a target's next ORCH start runs it again; nothing else
edits a core file in a target. Its outcome is one of taken, refused or failed.
_Avoid_: sync, pull, patch, port (the first copy into a fresh repo), upgrade

**Core push**:
The core updates one home merge runs in every target at once; its outcome is one line per target. A step every closed
core ticket passes, not a role.
_Avoid_: sync, broadcast, deploy

**Taken**:
The outcome of a core update after which the target holds the home's head: the update's one commit is in. A target
already at that head is taken too, with nothing written (#48); a target whose last update was taken is current.
_Avoid_: applied, updated, synced, succeeded

**Refused**:
The outcome of a core update, a merge or a commit that a check turned down before anything was written: the repo stays
exactly as it was, and the one line names the cause and the way out (an ORCH live in the target: its next ORCH start
takes the update; an uncommitted core path; a core file edited in a target outside a core update; a red suite). A
refusal is no defect: it waits for its condition.
_Avoid_: rejected, blocked, declined, failed (something broke)

**Failed**:
The outcome of a core update that broke on the way (a bad read of the home, a step that raised, a commit that could not
be made) rather than one a check turned down. It never changes the home merge's own result; its reason lands once per
cause in the target's dispatcher log and once in the home's brain chat (#42).
_Avoid_: refused (a check's deliberate no), error, broken, crashed

**Permission guard**:
The one check on an agent's tool calls once every session runs in bypassPermissions: a PreToolUse hook that denies the red list and passes everything else with
no question. Its refusal is final and lands in the issue, never on the owner. It reads the command text only (D112).
Its file is in ADR D112.
_Avoid_: classifier (the auto mode's judge of intent, retired by D112), allow list

**Red list**:
The short list of the stupid an agent may never do: bulk delete outside the throwaway areas, touching Windows or the
owner's folders, rewriting shared git history, destroying on GitHub, ending processes outside `tools.agentops stop`,
hand-writing the harness settings, reading secrets. The brain's, kept short; the owner picks no line (D112).
_Avoid_: deny list (that is one layer of it, the settings' `permissions.deny`), blocklist

**Machine mode**:
The one switch per user, `play` or `normal`, kept in one file outside every repo and naming none. In `play` every
installation of the core starts nothing and holds (freezes, never kills) what it runs; `normal` resumes it. Absent
file = `normal`. Stood time (the seconds spent in `play`) is subtracted by every watchdog (D117). Its path and shape
are in ADR D117.
_Avoid_: pause (the runs queue's own `pause`), lowered priority

**Mods journal**:
A record kept beside the owner's mods folder, not inside it, of each real install and removal of a mod: the mod's
folder name, the action, the local time. The test guard that watches that folder reads it: a folder that came or went
during a test run and has an entry from that run is a note in the run, not an error. A folder a test made by itself,
with no entry, still fails the run and is named in it.
