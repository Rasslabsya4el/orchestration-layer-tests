# 2026-09-28 D111. A core issue is written at the home under one parent issue, with facts of the orchestration only

Status: Accepted

The owner's idea of 2026-09-28 about 09:25 and the grill (`docs/research/2026-09-28-core-defect-intake-grill.md`,
Q1-Q5; #251). Until now nothing travelled from a target to the home: a target read the home (`core update`), and a
defect of the core met in a target was worked around there or found by a brain by hand.

1. Whatever is about the orchestration core (what broke, what looked off to an agent, a change, an idea) is a core
   issue. The repo that met it writes it at the home (D109's one home; the split grill, Q3), and it says which repo
   it came from and the core's version that repo holds.
2. Every core issue is a sub-issue of one parent issue at the home, the core epic (the owner: "один эпик/перент
   ишью"). It is not a wayfinder map: it has no destination and no end. GitHub holds 100 sub-issues under a parent,
   closed ones counted, so the oldest closed ones leave the parent and stay findable by their label.
3. A core issue written by a target holds facts of the orchestration only: the core file, the error's text, the
   dispatcher's log lines, the core's version. No code, data, file names or task text of the target.
4. An agent of the home that diagnoses it reads, in the target's folder, the traces of the orchestration only (the
   dispatcher's logs, the transcripts of that repo's ORCH sessions), read-only, never the target's code; what it
   writes at home follows 3.
5. The fix returns as D109 set it: the test repo by itself, the owner's working repo on his word. A defect that
   stopped the work there waits for that word even when the home has fixed it.

Rejected: a list under a label with no parent (the brain's recommendation, no cap to manage; the owner wants one
issue to open); a wayfinder map (it ends, and `/wayfinder` works decisions, not a standing intake); issues kept in
each target and collected by the home (the finder writes to the home, settled on 2026-09-27); writing whatever helps
the diagnosis (faster, but pieces of the working repo's code and tasks would stay in another repo's tracker for
good); the home reading everything in a target.
