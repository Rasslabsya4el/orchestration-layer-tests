# 2026-10-11 D117. Machine mode is one file per user, outside every repo

Status: Accepted; to be withdrawn (the owner, 2026-10-21, on issue #292: machine mode does not help and has no place
in the orchestration layer; its removal is a drafted ticket of the plan of map #289)

Why: when the owner switches the PC to play, only the PoB workers of one repo were lowered; the agent sessions, test
suites and jobs of every other repo kept running, and on 2026-10-10 he had to restart the PC.

Decision:

1. One file per user, `~/.claude/agentops/machine_mode.json` = `{"mode": "play" | "normal", "since": <iso>}`
   (folder overridable by `AGENTOPS_MACHINE_DIR`). Absent = `normal`. Written atomically (tmp + `os.replace`). A file
   that does not parse raises; a loop whose read raised starts nothing on that tick.
2. No registry and no repo name or path anywhere: every installation reads the same file and holds its own work,
   telling itself apart by its own root, locks, records and worktrees.
3. In `play` loops start nothing and the work in flight is frozen (suspended), never killed; on `normal` it is resumed.
   A kind of work that does not survive a freeze is stopped and offered again.
4. An append-only span log beside the file gives `stood_seconds(start, end)`; every watchdog subtracts it.

Trade-off: one shared file means any repo's switch holds all of them and a corrupt file stops all loops (chosen: the
owner wants one button; a stood machine is safer than a running one). A registry of repos would let a switch skip
some, but it needs upkeep and a name per repo, which the owner forbade. Freeze keeps a job's state but its clocks keep
running, hence the stood-time subtraction; kill would lose the work.
