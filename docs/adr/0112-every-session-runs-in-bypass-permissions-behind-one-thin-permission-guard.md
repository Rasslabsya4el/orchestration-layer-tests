# 2026-09-30 D112. Every session runs in bypassPermissions behind one thin permission guard; the auto-mode classifier is retired

Status: Accepted

The owner's order of 2026-09-30 14:38 and the grill (`docs/research/2026-09-30-permission-guard-grill.md`, Q1 and
A1; #354). Until now every session ran in the auto mode, whose classifier judged each command by intent. It cannot
be tuned from a project file, a background session cannot answer its prompt, and the owner is often away from the
PC, so a refusal of ordinary work (`gh issue create`, `git pull`, `runs.py mode`) stopped the work until his own
message naming the action; the same day it refused the record of this decision ("Instruction Poisoning").

1. Every session (brain, ORCH, keymaster, workers, autopilot, the chats of every target) runs in
   `bypassPermissions`: `permissions.defaultMode` in the user settings, `--permission-mode bypassPermissions` on
   every launch. No classifier judges intent; nothing asks the owner to run a command.
2. One guard, `tools/hooks/permission_guard.py`, a PreToolUse hook that runs in every mode, denies a short red list
   against the stupid and lets everything else through with no question: bulk delete outside the throwaway areas,
   writes to Windows / Program Files / the registry / the owner's folders and the machine's commands, rewriting
   shared git history, destroying on GitHub, ending processes outside `tools.agentops stop`, hand-writing the
   harness settings, reading secrets. The fixed-text lines are doubled as `permissions.deny` rules. The list is the
   brain's and stays short; the owner picks no line ("список не выбираю ... страховка от тупого твоя, короткая").
3. The guard reads the command text only: `bash -c`, an encoded command, a delete from inside a script pass by
   construction. A missing line lets an agent do one irreversible thing once; the owner takes that over the
   questions.
4. A guard refusal is final and lands in the issue, never on the owner; a wrong or missing line is a ticket for the
   brain, never a permission entry or a hand edit of the settings (they hot-reload into every session and change
   only through a merged ticket and `guard install`).
5. The guard, its deny rules and the launch flags are core: they travel with `core update` and the port (D109);
   the user settings file is one per machine, written by `python -m tools.agentops guard install`.

Supersedes: the morning rule of 2026-09-30 (#315: no standing allow entries, the classifier stays the judge, a
refused one-off cleared only by the owner's own message) as far as the two contradict.

Rejected: the auto mode kept, with `autoMode` rules in the user settings describing our environment (still a
judgement of intent that a background session cannot argue with, and it refused this decision's own record);
narrow `permissions.allow` prefix rules per refused command (withdrawn by the owner the same morning: a blanket
allow of merges; and they match text, not programs); a long red list (every surplus line is a question back on the
owner, who asked for short); asking the owner per refusal (the state before).
