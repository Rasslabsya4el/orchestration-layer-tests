# 2026-09-26 D106. PoB runs are owned by a script; a keymaster session starts only on anomalies

Status: Accepted

The owner's grill (`docs/research/2026-09-26-orchestration-v2-grill.md`, Q5, Q10, Q16, Q18, Q19, Q23) and the owner's call at 17:08 the same day: the runs store and
daemon (`scripts/runs.py`, #40, #48) own the PC and laptop queues, priorities P0/P1/P2 with graceful preemption (kill
with `--resume` only on "срочно"), the owner's modes (play / browser / normal), reservations, and posting a finished
job's result into its issue with `ready-for-agent` re-added. No agent starts long PoB work except through
`runs submit`. The keymaster is a role, not a standing chat: the daemon starts a keymaster session only on an
anomaly (#57). Rejected: an LLM keymaster chat that polls or posts hourly (tokens on every tick).
