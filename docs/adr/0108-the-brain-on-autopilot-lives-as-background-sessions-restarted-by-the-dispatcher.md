# 2026-09-27 D108. The brain on autopilot lives as background sessions restarted by the dispatcher

Status: Accepted; amended by D108 amd 1

The owner's grill (`docs/research/2026-09-27-autopilot-grill.md`, Q9 and Q12) and the reset research (#109): a goal
the owner gives on leaving is worked by background `claude --bg` brain sessions, one live session per goal, no cap
on goals. Each session writes a checkpoint on the goal issue and ends its turn at the context line; the dispatcher
stops it and starts a fresh one, and a SessionStart hook injects the checkpoint. The owner's chat brain stays the only
chat with Remote Control: it hands the goal over and relays both ways through `SendMessage`, and it is the one that
pushes to the phone, since a `--bg` session cannot (#110).

This reverses "the brain never clears itself" (#92) for autopilot only: the chat brain still never clears itself.
Rejected: a reset inside the owner's chat (no documented way to clear and re-prompt a desktop chat from a script);
compaction or a Stop hook in one long session (#109: 4-7x the usage per reset, a summary the model writes instead of
our checkpoint, and Claude Code's "Autocompact is thrashing" guard parks the session where only an outside loop can
restart it). ORCH stays one session (D104); the autopilot slot sits beside it.

## Amendment 1 (2026-10-15, the owner, #1103 under the map #1076)

The chat brain leaves the loop of a running stretch; it no longer works beside it. Counts (session transcripts):
the stretch on #780 ran 3 days 18 h in 279 sessions, sent the chat 28 messages, and the chat made about as many model
calls over the same hours (about 4,900 each), both working the same tickets by day; the night on #1045 (8 h 30 min,
33 sessions) sent the chat one message. The double cost was two brains on one goal, not the messages.

1. A stretch starts only after the chat brain grilled the owner on the goal (a map or a task): a couple of rounds
   of the questions likely to come up during the stretch, conditional ones on research still open, without
   fanaticism; the owner ends the grill when he likes.
2. During the stretch the autopilot sends the chat nothing: no question, no stop message, no push; everything goes
   to the review. The dispatcher wakes the chat for nothing either. The chat may ask the autopilot on the owner's word.
3. A question born during the stretch: the autopilot decides a reversible one provisionally; an irreversible one
   (a one-way door) waits for the owner, like a hard stop, while the rest of the goal goes on.
4. The stretch ends at the goal's end, stuck on every front, the usage ceiling, the owner's "стоп", or when the owner
   starts working the goal in the chat: the chat stops the autopilot first (a checkpoint written) and only then works
   with him. A message about the autopilot or about another topic stops nothing. No clock deadline by default.
5. Several goals may run at once, one session each (unchanged).

This withdraws the relay in both directions and the one push of the first paragraph. Rejected: the owner's first
message to the chat ends the stretch (the owner, 2026-10-15: a question about the night or another topic must not
stop it); a clock cap past the morning (his rule: no deadline he did not name).
