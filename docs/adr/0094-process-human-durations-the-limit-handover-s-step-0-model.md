# 2026-09-20 D94. Process: human durations, the limit handover's step 0, model tiers (written 2026-09-22, owed)

Status: Accepted

Not architecture, recorded because every report depends on them. (1) CLAUDE.md principle 7 (the owner, twice): every
human-facing duration goes through `junkroan.report_format.human_duration` (merged d5226f9: ledger and PoB-call
tables, `summary()`, report.md and `--rereport`, the replay bench, `run_progress`, `describe_build`, the GPT harness);
JSON keeps seconds. (2) Limit handover step 0 (682f746): ask how much limit is left, then place the handover where no
code in flight would make queued runs stale -- baseline first, campaigns on the final code, a canary after a fresh
merge, a hard ceiling with a notification. (3) 2026-09-22, the owner: Opus 5.5 is the lead and tier 1, Opus 5 tier 2,
Sonnet 5 tier 3, Haiku 4.5 tier 4, Fable 5.1 retired (maybe a tier-1 second opinion later); effort lives in agent
definitions `.claude/agents/effort-*` / `opus5-*`. The session "TEST RUNS" owns the queue, the Backlog, the hourly
posts and the laptop; the lead chat orchestrates, reads and writes DECISIONS.

