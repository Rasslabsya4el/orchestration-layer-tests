## For the human
- **What:** мозг помечает, ради чего он работает (проверка закрытой задачи, расспрос владельца, написание задачи, разбор уведомления), и раз в неделю видно, сколько денег ушло на каждую цель.
- **Why:** слово владельца, 17.10 вечер: «Да, можно вести такие логи если это не сильно дорого будет»; без такой метки нельзя посчитать, стоит ли заводить отдельного проверяющего закрытых задач (решение отложено до этого счёта, 17.10).
- **Plan:** метка — короткая первая строка-комментарий в команде, которую мозг и так запускает: ни лишнего шага, ни лишнего вызова; счётчик читает уже существующие логи сессий и складывает стоимость по меткам.
- **Result:** за первую полную неделю не меньше 80 % денег мозга помечено; таблица «цель — деньги — доля» за неделю; показывают тесты счётчика и его недельный прогон.
- **Undo:** two-way door (one revert); touches только мозг (чат и автопилот) и его недельный отчёт; исполнители не меняются.

## For the agent

### Facts

- The weekly cost table prices every transcript turn through `scripts/token_meter.py` (`scan_projects_dir` :597, `turn_cost` :752, `is_brain_turn` :185), used by `tools/agentops/losses_cost.py:26-31` (`losses --week`).
- Brain actions are counted by tool, not by purpose: #126's reading closes with "brain actions are by tool, not by purpose (no grill/closure tag)", and its row 8 "a closure reviewer: not measurable (no purpose tag)"; the owner postponed the closure reviewer until it is counted (map #22, Not yet specified).
- A transcript already holds every tool call's full input, so a tag written into a command needs no hook and no new log file: the counter reads it where it already is.
- Cost of a tag: about 10 output tokens on a tool call the brain makes anyway; no extra turn, so no extra cache read of the brain's context.

### The rule

1. **Tag.** When the brain starts a stretch of work with one purpose, the first Bash or PowerShell call of that stretch starts with the line `# purpose: <word>`. Words (closed list, in `docs/agents/brain.md`): `closure-review` (reading a closed or closing ticket against its acceptance rows), `grill`, `ticket` (writing or rewriting a ticket), `notice` (a dispatcher or ORCH message), `owner` (answering the owner), `research` (a reading the brain runs or reads). A tag holds until the next tag or the next owner message, whichever is first; a turn with no tag in force is `untagged`.
2. **Subagents.** A subagent's turns count to the purpose in force at the parent's `Agent` call that started it.
3. **Count.** `python -m tools.agentops purposes --week [YYYY-Www]` (and `--since/--until`) prints one row per purpose: brain turns, $ at API price, share of brain $; then `untagged` and a total equal to the brain $ of `losses --week` for the same window (same pricing, same brain-turn test). Reads transcripts only; writes nothing.
4. **Rule text.** `docs/agents/brain.md` "What the brain does" gains one bullet with the tag form and the word list; `.claude/agents/brain.md` points to it. Net rule lines: at most +6.

### Acceptance

| row | check | number |
|---|---|---|
| 1 | test on a fixture transcript: two tags, an owner message between them, one subagent launched under the second tag | each turn and the subagent's turns land in the right purpose; `untagged` holds only the turns before the first tag and after the owner message |
| 2 | test: the `purposes` total equals the brain $ that `losses` computes on the same fixture | equal to the cent |
| 3 | test: an unknown word after `# purpose:` | counted as `untagged` and listed once by name under the table |
| 4 | the existing token-meter and losses tests | all pass |
| 5 | `python -m tools.agentops core check` (the counter and its test are core files, in the manifest) | rc 0 |
| 6 | live, read by the brain 7 days after the merge: `purposes --week` over the home's brain chat | `untagged` under 20 % of brain $; the `closure-review` row's $ written into the map's Not yet specified line |

### Not in this ticket

- The closure reviewer itself: decided only after row 6 gives its number.
- Tags for ORCH or workers: their cost already splits by ticket.

