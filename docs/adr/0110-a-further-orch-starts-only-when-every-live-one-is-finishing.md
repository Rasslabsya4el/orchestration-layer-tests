# 2026-09-27 D110. A further ORCH starts only when every live one is finishing, and sessions meet at the merge (amends D104)

Status: Accepted

The owner's grill (`docs/research/2026-09-27-orch-near-line-grill.md`, round 1 Q1, round 2 Q1 and Q2; #177). On
2026-09-27 nine ready issues waited 22 min to 1 h 20 min for the one ORCH to end after it came near its context
line, against the wake's bar of 2 min; the owner had said that throughput matters (2026-09-26). D104's "exactly one
ORCH session exists at a time" and its "one merger and no merge lock" are replaced by:

1. More than one ORCH session may live, as few as possible. One reason starts a further ORCH: a ready issue exists
   and every live ORCH is finishing. Nothing else starts one, the number of issues a session holds included.
2. A finishing ORCH takes no new work, finishes or parks what it holds and ends by itself; it is not woken and not
   offered an issue. So at most one live ORCH takes new work at any moment, and an issue claimed at that moment is
   that session's.
3. Between sessions nothing is checked before the work: an issue is taken at once, and a collision is met at the
   merge (the rebase, the suite, the worker's hand edit on a conflict). Inside one session the code overlap stays
   ORCH's own reading, and a blocked-by edge written by the brain still orders issues known to collide.
4. `main` has several mergers, and they share one checkout (so do the brain's and the autopilot's commits): a merge
   (the sync with `origin/main`, the merge, the suite, the push) is done by one session at a time. A refused push
   is answered by a fetch, a rebase, the suite and a second push, never by a forced one.
5. The rule is the orchestration core's and holds in any repo. Its indicator is the count of conflicts: at 3 per 20
   closed issues (2026-09-27: 3 over about 49, that is 1.2 per 20), or at one conflict that ends without a green
   suite, the brain is told and the rule is put to the owner again with the numbers.

D104 keeps the rest: every ready issue at once through subagents, no cap on the issues a session holds, the owner
talks only to the brain, the threshold T under the context line.

Rejected: strictly one ORCH with only the empty wakes removed (the wait stays); a further ORCH also when a session
holds more than N issues (one ORCH worked ten issues normally, and every further session adds merges between
sessions); waiting on any suspicion of a code overlap (the issue's text as the estimate catches 8 of the 16 pairs
that changed the same file and flags 44-55 % of the pairs that shared nothing, so about half of the issues would
wait for nothing); waiting on an exact signal from the other session's worktrees (the brain's recommendation; the
owner chose to take at once: a shared file came to a conflict 3 times over about 49 closed issues, each resolved in
1-8 min).
