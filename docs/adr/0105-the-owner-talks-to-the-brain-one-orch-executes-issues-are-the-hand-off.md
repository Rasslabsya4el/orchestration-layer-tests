# 2026-09-26 D105. The owner talks only to the brain; one ORCH executes; a GitHub issue is the hand-off

Status: Accepted; amended by D105 amd 1

Amended by D105 amd 1, which supersedes this decision's sentence "it never executes" (kept here as history).

The owner's grill (`docs/research/2026-09-26-orchestration-v2-grill.md`, Q9, Q14 T*, Q15): the brain (Fable 5.1 high) grills the owner, captures decisions and writes
issues with the numbers a test must reproduce; it does not run the work ORCH runs (D105 amd 1) and never waits for ORCH. ORCH (Opus 5.5 high) is the
only executor and the only merger; it judges each issue by its own "Done when" and closes it. The hand-off is the
issue, never a retold chat. A dispatcher script (#50), not a chat, starts ORCH sessions, so neither the brain nor the
owner ever wakes ORCH. Rejected: ORCH as the brain's subagent (it dies with a brain reset, Q14 S) and a self-clearing
standing ORCH chat (a cleared chat cannot wake itself, #50).

## D105 amd 1 (2026-10-24, #348): the brain does not run the work ORCH runs; it reads itself the source of a fact that decides a verdict

The brain does not run the work ORCH runs: no code, no merges, no test runs. Where a fact decides a verdict, the
brain reads its source itself (the code, the file, the log line), not a retold report. The rest of D105 stands: the
issue is the hand-off, ORCH is the only executor and merger, and the dispatcher, not the brain, starts ORCH.
