## For the human
- **What:** проверка слияния останавливается на первом подтверждённом упавшем тесте, новый коммит той же ветки заменяет свою проверку, и появляется команда снять слияние с очереди.
- **Why:** слово владельца, 19.10: «задача уходит в мердж, падает с ошибкой, мы сразу делаем фикс и он ждет… постоянно хуевый кью»; 19.10 мозг снимал слияние руками, потому что команды нет; за 17-19.10 упавшие проверки держали очередь 314 минут, из них около 77 минут шли уже после первого упавшего теста.
- **Plan:** три правила в инструменте слияния: остановка при подтверждённом падении, замена старой проверки новой, команда снятия; снимать может исполнитель своё слияние и мозг любое, каждое снятие — одна строка в чате мозга.
- **Result:** упавшая проверка заканчивается не позже 2 минут после повторного прогона упавшего теста (было до 36 минут); 0 проверок старого коммита, доживших до конца при новом коммите в очереди; снятое слияние уходит из очереди за 1 минуту; показывают тесты на подставном прогоне.
- **Undo:** two-way door (one revert); touches очередь слияний всех репозиториев с ядром.

## For the agent

Flow fix: yes (the merge line of every repo with the core: doomed runs hold it and nothing takes a merge out)

Decided in the grill of #215 (record `docs/research/2026-10-19-merge-queue-grill.md`, rounds 2-3). Read `GLOSSARY.md` first.

### Facts (19.10)
- No command takes a queued or running merge out: `tools/agentops/merge.py:4198` has `merge` and `commit` only. The brain stopped #195's run with `python -m tools.agentops stop <pid>` and parked it by hand.
- 17.10 00:00 - 19.10 10:12: 74 runs held the line 2,237 min; 10 ended red (314 min). A failing test is confirmed only at the end of the suite: in 4 red runs the first failing test ran 9, 12, 20 and 36 min before the end (about 77 min).
- Existing levers: the failed-ids re-run (`merge.py:2566`), the silent-holder takeover (`merge.py:1092`), `merge_priority`, `--queue-checked`.
- #209 (landed 19.10) counts failed attempts for the unblocker; outside-event results (lock-timeout, setup-error, silent-holder) count for nothing.

### The rules
1. **Fail fast.** When a test fails during the suite, it is re-run alone at once; if the re-run fails too, the suite is stopped, the run ends red, and its result names the test and both failures. A re-run that passes lets the suite go on (as today's end-of-suite re-run would have).
2. **A new commit replaces its run.** When a merge is queued for a branch whose older head is queued or running, the older entry is ended (`superseded`, naming the new head) and only the new head is run. A superseded result counts as no attempt in #209's counter.
3. **Withdraw.** `python -m tools.agentops merge withdraw <issue|branch> --why "<one line>"`: a queued entry leaves the queue; a running one has its suite processes stopped (their own process tree only) and the lock freed. The ticket gets a comment `<!-- merge withdrawn -->` with who, why and the minutes the run held the line; the brain chat gets one line through the existing brain notice. A withdrawn result counts as no attempt.
4. **Who withdraws.** Rule text, `docs/agents/orch.md` and `docs/agents/brain.md`: the executor withdraws only its own ticket's merge; the brain withdraws any merge it knows is doomed. The brain's text replaces the `agentops stop <pid>` route for merges. Call `writing-for-agents` before these edits; net added lines over the two files at most +6.

### Acceptance rows
1. A fake suite where test T fails, and fails again alone: the run ends red within 2 min of the re-run's end; the result names T; the remaining tests are not run.
2. A fake suite where T fails once and passes alone: the suite goes on to the end; the run's result is green if nothing else fails.
3. A running entry for branch B at head h1; a merge queued for B at h2: h1's run ends `superseded` within 1 min, h2 is in the queue in h1's place; #209's counter for that issue is unchanged.
4. `merge withdraw` on a queued entry: `merge-status` no longer lists it, 1 comment and 1 brain line written.
5. `merge withdraw` on the running entry: its suite processes are gone and the next entry starts within 1 min; no process outside the run's tree is touched.
6. `python -m tools.agentops core check` rc 0; `python -m tools.agentops rules --issues <this>` net at most +6.

### Not in this ticket
- The queue notice to the brain and the weekly count of queue cases: the sibling ticket "The brain hears of a doomed merge and the week counts what queue cases cost".
- A model that reads the queue: not now (the owner, #215 round 2); it comes back only past the "dear" line.
- Reordering the queue by judgment.

## Recount
- Incident: doomed merge runs held the line after their failure was known, and a merge could only be stopped by a process kill (#195, 19.10).
- Counted: over the merge log, minutes from the first confirmed failing test to the run's end in red runs; runs of an old head that finished while a newer head of the same branch was queued; merges stopped by `agentops stop` instead of `merge withdraw`.
- Window: 7 days after landing.
- Count: before about 77 min over 4 red runs, and 1 kill by pid; target at most 2 min per red run, 0 old-head runs, 0 kills by pid.

