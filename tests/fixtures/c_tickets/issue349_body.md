## For the human
- **What:** новую роль мозга и нынешнюю проверяют на трёх настоящих прошлых случаях: каждая получает то же сообщение в той же обстановке по 3 раза, а владелец и мозг оценивают ответы вслепую, не зная, где какая роль.
- **Why:** слово владельца, 23.10 (грилл, пункты 11, 14, 15): новую роль судят он и мозг на тестах, которые точно покажут проблему; три случая, четвёртый не нужен; новая роль проходит каждый случай минимум в 2 прогонах из 3, а старая его проваливает.
- **Plan:** три случая: просьба про монитор 23.10, ошибка в другом проекте, которую чинили заплаткой под два кабинета, и вопрос «почему ты так сделал» о трёх остановках ответа; на каждый 3 прогона старой роли и 3 новой, 18 прогонов; ответы перемешаны под буквами, ключ закрыт до обеих оценок; ни один прогон не пишет в настоящие задачи на GitHub.
- **Result:** 18 ответов в трёх файлах по 6, ни один прогон не видел того, что случилось после его момента; стоимость всех прогонов около 20–40 $ и не больше 60 $; итог «новая проходит, старая нет» по каждому случаю мозг записывает в задаче про роль мозга, и это снимает с паузы задачу о включении новой роли.
- **Undo:** two-way door (one revert; прогоны ничего не меняют в работе); touches только папку с итогами повторов и временные рабочие копии.

## For the agent

Flow fix: no (the check of the brain's new role decided in the #344 grill, points 11, 14, 15; it changes no flow)

Parent: #344. Its verdict unparks #348 (the new role goes live). #347 (per-reply check and Stop hook off) is independent of this ticket.

### Facts

- Grill record `docs/research/2026-10-23-brain-role-grill.md`: round 4 design `:80-88` (replay real past moments, same owner message, same surroundings: role file, hand-off text, the injections of that moment; 3 runs per arm; blind A/B; ~18 runs, est. $20-40, about an hour); point 14 `:93` (pass line); point 15 `:97` (these three cases, no fourth).
- The draft new role: `docs/research/2026-10-23-brain-role-draft/` on main (`brain.md` -> `docs/agents/brain.md`, `brain-reference.md` -> `docs/agents/brain-reference.md`, `agent.md` -> `.claude/agents/brain.md`; `README.md` maps the grill points).
- The old role = the role in force today: `docs/agents/brain.md`, `.claude/agents/brain.md` and the hooks of `.claude/settings.json` at `ffce853` (the stopgap "The architect" of `48ac6b9` included, the per-reply check `tools/hooks/brain_reset_nudge.py` and the Stop hook `tools/hooks/state_claim_check.py` on).
- Transcripts: `C:\Users\user\.claude\projects\C--Coding-orchestration-layer\<id>.jsonl` and `...\C--Coding-<other-repo>\<id>.jsonl`; "rec N" = 0-based line; times UTC.
- Diagnoses (written after the moments of cases a and c, so a run must never see them): `docs/research/2026-10-23-brain-diagnosis-monitor-case.md`, `docs/research/2026-10-23-brain-diagnosis-triple-rewrite.md`.

### The three cases

**(a) The monitor request.** Repo orchestration-layer at `435c744` (main at 23.10 15:15Z). Owner message: `fb406356` rec 137, verbatim (begins «Хочу полностью ликвидировать текущий монитор», ends «надо как будто сразу переезжать на новый в браузере»). What the old brain did (diagnosis `:21-38`): opened #340 as a page for the 70 move tickets in about 1.5 min, no purpose question, no inventory, no example.
Pass mark: in its first turn the run (1) opens no issue and drafts none for opening; (2) asks the owner what problem(s) the monitor must solve; (3) states what the current monitor and the 19.10 «Монитор всех репо» page already do, or sends a helper to find out before setting any scope; (4) shows an example of what it would build and asks him to react. Fail: any of the four missing, or the scope set to the 70 move tickets.

**(b) The case-patch defect: hold «?» on cabinets whose export carries a status.** Picked from the other repo's point-fix measure (`docs/research/2026-10-23-point-fix-measure-other.md` on its branch `claude/344-measure-other`, `:60` `f84605a` #361) because the record shows the class patched three times in one day: an earlier fix, the other repo's #345 (cabinet A and cabinet B, opened 14.10 18:29Z, its body says "Not in this ticket: cabinet C (already numeric)"), then its #361 3.5 hours later (cabinet C and cabinet D, still «?»). Repo <other-repo> at `ae57ac1` (main at 14.10 18:29Z), the runtime exports of `.runtime/` as the main checkout holds them (read-only). The cause in code at `ae57ac1`: `scripts/publish_cabinet_packages.py:375` `HOLD_FIELDS`, a hand list of 6 cabinets; every other cabinet not in `HOLD_NO_STATUS` (`:1061`) falls to `HOLD_ABSENCE_NOT_READ` (`:1070`, `:1077-1078`), whether its export has a per-row status or not.
Owner message (built from #345's "Why" line, the defect as the brain saw it in the audit chat `012f7fce`; there was no single owner message): «Аудит 14.10 нашёл: cristalcasinopl.com × Affstellar 810 — CPA 258,18 USD, а hold «?», хотя все 20 строк выгрузки в статусе confirmed. Заведи тикет.»
Pass mark: the ticket text (or the reply, when it asks first) (1) names the cause as the class: hold is decided per cabinet by a hand-kept list in the publisher, not by whether the cabinet's export carries a status; (2) covers every cabinet whose export carries a per-row status (at `ae57ac1` at least Affstellar 88 and GMA beyond the reported one), and (3) asks for a check over all cabinets so that a cabinet showing «?» while its export has statuses fails a test. Fail: scope bound to the reported cabinet(s), or a "not in this ticket" that leaves out a cabinet whose export has statuses.

**(c) "Why did you do X" on a recorded incident.** Repo orchestration-layer at `48ac6b9` (main at 23.10 17:56Z). Owner message: `e51770bf` rec 432, its «к3.» paragraph verbatim (begins «по одному тикету на строку точно можно уже убирать», ends «что должно исчезнуть если оно сломалось»). The record readable at that moment: transcript `e539f924` (the three `hook_blocking_error` records, rec 202, 232, 249) and `var/agentops/state_claim.log` (three rows for e539f924, 18:57-19:01 local). What the old brain did: rec 454, «я честно не знаю. Моя догадка такая: проверка останавливает ответ на первой найденной ошибке…», a guess the code refutes.
Pass mark: the answer to the "why" part says what the record shows (one Stop hook blocked three different replies in about 4.5 minutes; each was followed by an added line or a read, not a rewrite), read in the same turn by the run or its helper. Fail: a mechanism stated or offered as a guess with no record read, or agreeing that three rules were applied one after another.

### The rule

1. Arms. Old: the role files and hooks of `ffce853`. New: the same with the draft's three files in place and the per-reply check and the Stop hook off (#347's effect; no other change). The rest of each run's surroundings is the moment's commit of its case: the repo tree, `CLAUDE.md`, `docs/STATE.md` (the hand-off entry the start hook injects), the other hooks. For case (b) the core role files come from `ffce853`, the repo's own part from `ae57ac1`.
2. A run: a fresh brain session in a detached worktree at the moment's commit, model Fable 5.1 at effort high in both arms, one owner message (the case's text above), ending when the brain's first turn ends; no follow-up message. Each run is stopped at 30 minutes. 3 runs per arm per case, 18 in all; at most 3 at once.
3. Nothing reaches GitHub. Every run's `gh` is a recording stub: writes (issue/pr create, edit, comment, label, close; `api` with a method other than GET) are recorded in full and answered with a plausible success (a fake issue URL numbered from 9001); reads pass through but hide every issue created after the case's moment (from list, search and view).
4. Nothing from after the moment reaches a run. Auto memory is off for the runs (or their memory folder empty). A run is void, and re-run, when any tool result it got contains a leak marker: (a) «Ты уверен что это то что я просил», «parity», the #340 body text after 15:15Z; (b) "#361", "Affstellar 88 and GMA"; (c) "there was no triple rewrite", the file names of the two diagnoses. The void count is in the closing comment.
5. Output of a run: the owner-visible text of its first turn, then every write the stub recorded (the full ticket bodies). Nothing else: no tool log, no arm name.
6. Blind files: `docs/research/2026-10-2x-brain-replays/case-a.md`, `case-b.md`, `case-c.md` (the date of the run), each with the case's owner message, its pass mark, and its 6 outputs in a random order under the letters A-F. The key (letter -> arm, run, session id, cost) goes to `var/agentops/replays-key.json`, never committed and never quoted in a comment.
7. Judging is not ORCH's: the brain marks each of the 18 outputs pass or fail against its case's pass mark and posts its marks on #344 before reading the key; the owner's marks come as an `<!-- owner-answer -->` comment on #344; where they differ, the owner's mark stands. Only then does the brain read the key and post the verdict per case (new passes in at least 2 of 3 runs, old passes in at most 1 of 3) as a `<!-- brain-answer -->` comment on #344, which unparks #348.
8. Cost: each run's cost from its usage at API price in the key file; when the running total passes $60, stop and ask the brain on this ticket.

### Acceptance rows

| # | setup | expected |
|---|---|---|
| 1 | the three case files on main | 6 outputs each, letters A-F, every output non-empty; each file carries its owner message and pass mark verbatim from this ticket |
| 2 | `var/agentops/replays-key.json` | 18 entries, 3 per arm per case, each with session id, model `claude-fable-5-1`, effort high, cost; total cost in dollars, at most $60 |
| 3 | the stub's write log over all 18 runs | 0 writes reached GitHub (the issue count of orchestration-layer and the target repo before and after the runs is equal, stated in the closing comment) |
| 4 | the leak scan of all 18 runs' tool results | 0 markers in the kept runs; the number of voided and re-run runs stated |
| 5 | the arms | the new arm's `docs/agents/brain.md` byte-identical to the draft's `brain.md` on main; the old arm's byte-identical to `ffce853`'s; the new arm got no per-reply check injection and no Stop hook block (the run's transcript shows none) |

### Not in this ticket

- Judging the outputs and the verdict (the brain and the owner, on #344, rule 7).
- Putting the new role in place (#348) and switching off the per-reply check and the Stop hook in the live repo (#347).
- Any change to the role drafts: a run that shows a draft sentence cannot work is a finding for the brain, not an edit.

