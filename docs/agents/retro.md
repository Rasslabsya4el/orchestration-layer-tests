# Weekly retro: records and link failures

Reference for the weekly retro. It says where each role's records lie, which hand-offs count as link failures, and the
one command that prints both. The Pocock `retro` skill is not copied, wrapped or edited here: it only gets this source
to read. Its own steps stay its own.

A finding about the orchestration core (an agent's behaviour under the core's rules, a core rule, the dispatcher, the merge queue, the monitor's general part) is filed as a core issue with `python -m tools.agentops core issue` from whichever repo the retro runs in; it lands at the home. A finding unsure to be the core's is that repo's own `needs-triage` issue; a repo's own finding is a ticket of that repo.

## Roles, records, failure kinds

The roles are the table in `docs/agents/core-block.md` ("Roles (orchestration v2)"). The merge queue and the core push
are steps that every closed ticket passes; they count as two more rows.

| role or step | where its records lie |
|---|---|
| brain | the hand-off lines (`Updated ...`) in `docs/STATE.md` |
| ORCH | the `orch_start` events in `artifacts/runs/events.jsonl`; its checkpoint, closing and parking comments on issues |
| dispatcher | the leading-stamped lines of `artifacts/runs/dispatch.out` |
| researcher-find / researcher-how | subagent transcripts of the ORCH sessions (the `agentType` in each `.meta.json`) |
| external researcher | `docs/research/*.md` (counted by each file's last write) |
| worker / worker-strong | subagent transcripts (`agentType` worker, worker-strong) |
| reviewer-standards / reviewer-spec | subagent transcripts (`agentType` reviewer-standards, reviewer-spec) |
| merge queue | `var/agentops/results/*-merge-*.json` and `var/agentops/merge.log` |
| core push | the `core_targets` list of each merge result (only when the settings name `targets`) |
| logic-audit (weekly prophylaxis) | its own printed table: `python -m tools.agentops logic-audit`; it reads the guard, merge and dispatch logs and the repo's own files, writes nothing |

A role whose location is not there is a `no records` row. That is a finding for the brain, not an error.

Link failure kinds, each counted in the window:

| kind | a hand-off that did not reach the next role or step |
|---|---|
| a | a merge did not land for a reason outside the branch's own code: `target-check`, `conflict`, `rebase-conflict`, `lock-timeout`, `red-on-base`; one row per reason |
| b | a merge did not land and needed a code change to land: `red-suite`, `red-on-branch` |
| c | a core push to a target failed (`core_targets` lines with `: failed:`); one row per cause |
| d | an ORCH session ended holding a ticket with no closing or parking checkpoint (the dispatcher's alarm comment) |
| e | a second ORCH took a ticket over (`taking over #N from ...`) |
| f | a merge result comment with no closer for more than a day |
| g | a `parked` ticket whose named event has passed and that still carries `parked` |
| h | the GitHub budget reached 0 (sampler rows, `rate limit exceeded` lines), with the callers the records name |

A kind with no record to read is printed as `no source`, never dropped. The command counts; judging what a failure
means is the retro's and the brain's.

## The command

```
python -m tools.agentops retro-sources --since <date> --until <date>T<HH:MM>
```

- `--since` (required) and `--until` (default: now) take `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`, local time; the window is
  `[since, until)`.
- It prints table 1 (role or step, location, exists, entry count) and table 2 (kind, reason or cause, count, time lost,
  up to three pointers, note). Time lost is written as a human duration.
- Issue comments are read through the GitHub search, one search per kind; the last line gives the number of GitHub
  calls the run made.
- It writes no file and changes no record: a read-only command.
