# Sessions: chats, handovers, cost

What this repo adds is in `docs/agents/local/sessions.md`, read it when it exists.

Who does what is CLAUDE.md "Roles" and the protocols `docs/agents/brain.md`, `docs/agents/orch.md`.
Which chat resets how: the brain by the owner's /clear, when the brain proposes it (this
section's follow-up rule); one ORCH works every ready issue in parallel through its subagents (`docs/agents/orch.md`);
a background session never clears itself, the owner clears it when it grows. "The lead" below is the brain.

## A new chat

- A new chat opens with one fixed line: continue this repo; read CLAUDE.md, GLOSSARY.md, the open `wayfinder:map`
  issues and `docs/STATE.md`; check `git log -5` and `gh issue list`; continue. The line itself, in the owner's
  language, is the local file's.
- The lead asks the owner to open a new chat only when all of these hold (the owner's rule, restated
  after a switch was proposed with tasks still running): the app's background-task panel is EMPTY -- the
  session's Monitors and background Bash tasks stopped, every subagent finished or stopped and its leftover wait loops
  killed; the detached runs, the queue runner and GPT workers launched through WMI are not session tasks and keep
  going; `docs/STATE.md` is fresh (a commit just landed, no analysis half-done, no question pending); and the owner is
  at the PC, inside the hours the local file names (never at night). About once a day, preferably right after a
  run's results are recorded and before the next long stretch of waiting. It says so in one line with the opening
  message above and then takes no new steps until the new chat starts. A new chat re-arms the watchers from
  the queue's log and the current run's `.out`.
- The hand-off (the owner's rule): when the lead asks for a new chat it ends with a copy block: one
  fenced code block holding one start line, `/wayfinder <N>` (the number, not a link; never `/clear`). A pending owner decision rides in that line as `ask the owner: <decision>`, never as a blank for him to fill in. What the next
  session needs is in `docs/STATE.md` and the tickets, never in the pasted text, so the lead composes no context block.
- Live external runs (the owner's rule, after a hand-off left two live research runs unnamed and they failed
  unseen): every hand-off's machines line lists EVERY live external run -- research runs, jobs of the repo's heavy
  tool, detached drivers, ORCH sessions and what they hold -- each with its end time or cap, its log or output
  path, and who reads the result. The first reply of the new chat repeats that list after reading it, before any
  other work, and says what it checked. The lead checks every listed run without being asked and reports a failed
  one the moment it sees it, never on the owner's reminder (`docs/agents/brain-reference.md` "The clear and the hand-off", its "Live work" paragraph).

## Limit handover protocol

When the owner says the lead's limit is about to run out, the lead does this once, then stops; a cheaper session keeps
the machine busy until the reset without doing any logic. The protocol's steps, the window, the plan and the handover
file are this repo's and stand in the local file.

## Cost

- Waiting costs nothing: the session does not poll; Monitors and task notifications wake it. Tool output stays small
  (`tail`, `grep`, `cut`), never a whole log.
- Context is the main cost: a fresh chat is cheaper per turn than a long one, so "about once a day" above is also a
  cost rule.
- Which reset policy is cheapest is measured, never argued: replay the finished chats through
  `python scripts/token_meter.py` and compare the cache reads, which are ~97-99% of the tokens. The numbers this
  repo measured, and the policy they settled, are the local file's.
