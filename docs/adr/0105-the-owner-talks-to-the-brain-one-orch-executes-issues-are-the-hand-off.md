# 2026-09-26 D105. The owner talks only to the brain; one ORCH executes; a GitHub issue is the hand-off

Status: Accepted

The owner's grill (`docs/research/2026-09-26-orchestration-v2-grill.md`, Q9, Q14 T*, Q15): the brain (Fable 5.1 high) grills the owner, captures decisions and writes
issues with the numbers a test must reproduce; it never executes and never waits for ORCH. ORCH (Opus 5.5 high) is the
only executor and the only merger; it judges each issue by its own "Done when" and closes it. The hand-off is the
issue, never a retold chat. A dispatcher script (#50), not a chat, starts ORCH sessions, so neither the brain nor the
owner ever wakes ORCH. Rejected: ORCH as the brain's subagent (it dies with a brain reset, Q14 S) and a self-clearing
standing ORCH chat (a cleared chat cannot wake itself, #50).
