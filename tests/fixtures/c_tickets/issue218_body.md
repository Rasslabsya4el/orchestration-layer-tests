## For the human
- **What:** в словарь проекта добавляются пять слов, которые принесли две недавние задачи: Loop stop, Send-back, unblocker, brain-answer, mods journal.
- **Why:** находка исполнителя, 19.10: в отчётах по задаче о разборе застрявших тикетов и задаче об установке модов эти слова употребляются, а в словаре их нет; по правилу каждое такое слово — находка для мозга.
- **Plan:** исполнитель пишет по одной статье на слово, по смыслу, как оно работает сейчас в правилах и коде.
- **Result:** 5 из 5 слов есть в словаре; проверка — поиск каждого слова в словаре и проверка ядра без ошибок.
- **Undo:** two-way door (one revert); touches словарь проекта во всех репозиториях с ядром.

## For the agent

Flow fix: no (vocabulary only, no rule or code changes behaviour)

Read `GLOSSARY.md` first; call the `domain-modeling` skill before the first edit.

### Facts
- #209 (landed ea44580, closed 19.10) added the unblocker role, the `<!-- brain-answer -->` marker, the failed-attempt count and the `Loop stop`; its checkpoint 4 and ORCH's closing report name "Loop stop", "Send-back", "unblocker", "brain-answer" as missing from `GLOSSARY.md`.
- #210 (landed ea44580, closed 19.10) added the mods journal of the merge guard; its checkpoint 3 names "mods journal" as missing.
- The rule texts using them: `docs/agents/brain.md` ("Three failed attempts in a row go to an unblocker"), `docs/agents/orch.md`, `.claude/agents/unblocker.md`; the code: `tools/agentops/loop_stop.py`, `tools/agentops/unblocker_start.py` and the #210 guard journal (find its module from `tests/test_issue210_guard_journal.py`).

### The rule
One entry per word in `GLOSSARY.md`, in its existing format: what the thing is in the domain, no implementation detail (no file, function or setting names). Where a word has an `_Avoid_` alias already used in the docs (e.g. "send back" vs "Send-back"), name it. "Loop stop" and "brain-answer" are related: one entry may point to the other.

### Acceptance rows
1. `GLOSSARY.md` has an entry headed by each of: Loop stop, Send-back, Unblocker, Brain-answer, Mods journal (5 of 5).
2. Each entry reads with no file path, function name or setting key in it.
3. `python -m tools.agentops core check` rc 0.

### Not in this ticket
- #209's open points (play mode holding the unblocker, limbo with no retry, one Fable session per already-stopped ticket at deploy): the brain's review.
- Any change to the rules or code these words name.

## Recount
- Incident: two closing reports of 19.10 (#209, #210) used five words absent from `GLOSSARY.md`.
- Counted: closing reports and review findings over the 7 days after landing (to ~2026-10-26) that flag any of the five words as missing from the glossary.
- Window: 7 days after the merge.
- Count: before 2 reports; target 0.

