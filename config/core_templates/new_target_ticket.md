## For the human
- **What:** в репо из папки `{folder}` переносится ядро оркестрации, оно становится целью дома (каждое слияние дома доходит до него само), и запускается его диспетчер; чат «{title}», который владелец открыл там, становится его брейном. То, что может сделать только владелец, возвращается одним коротким списком.
- **Goal:** репо из папки `{folder}` с чатом «{title}» становится целью дома одной командой по фиксированному протоколу, и владельцу ничего не нужно искать руками.
- **Plan:** `target add` дома проверяет условия, делает пробный прогон переноса, затем сам перенос, добавляет цель в список дома через `agentops commit`, запускает статус цели и её диспетчер; в конце печатается список для владельца.
- **Result:** команда печатает 7 строк шагов и список владельца с кодом 0; дом перечисляет новую цель; диспетчер цели жив; набор тестов дома — 0 упавших.
- **Undo:** two-way door (перенесённые файлы удаляются, запись цели убирается из списка дома); touches файл правил и настройки нового репо (строка импорта и добавленные ключи, больше ничего) и список целей дома.

## For the agent
Class: none (a fixed protocol: the seven steps of one command, no rule text)

Never rename any chat. The owner's chat in that folder is "{title}": take that title as the brain chat title. The
GitHub `owner/name` is read by the command from the folder's `origin`; no origin means a stop at step 1, nothing written.

### Command
    python -m tools.agentops target add --folder "{folder}" --brain-title "{title}"

Run at the home, not detached. The 7 steps, one line each, in order (`docs/agents/PORTING.md`, "A new target in one command"):
1. target repo: the folder is a git repo whose origin is a GitHub repo; an uncommitted tree is reported, never committed.
2. machine ready: the plugin enabled, `gh` 2.101 or newer.
3. port dry run: the clash check; a clash stops here and the ticket goes `needs-info` to the brain, no `--replace`.
4. port: the port for real.
5. target-entry: the entry with the target's repo and folder added to the home's targets through `agentops commit`.
6. core status: the target reports its core changes waiting.
7. dispatch: `dispatch --ensure` in the target prints `live pid <pid> commit <sha8>`.

A stop at any step: that step's line names the route; the ticket stays open with the stop line as its checkpoint.

### Acceptance
| row | check | expected |
|---|---|---|
| 1 | `target add` step 1: the target repo | one line, `step 1 target repo:` naming the GitHub repo |
| 2 | step 2: machine ready | one line, `step 2 machine ready:` with the plugin enabled and gh ok |
| 3 | step 3: port dry run | one line, `step 3 port dry run:` with the files and labels it would write; nothing written |
| 4 | step 4: port | one line, `step 4 port:` with the files copied and labels created |
| 5 | step 5: target-entry | one line, `step 5 target-entry:` the entry added to the home's targets, or "already listed" |
| 6 | step 6: core status | one line, `step 6 core status:` with the count of core changes waiting |
| 7 | step 7: dispatch | one line, `step 7 dispatch:` with `live pid <pid> commit <sha8>` (or `started pid <pid>` on a fresh start) |
| owner | the owner's list, printed after step 7 | 3 items: run `/setup-matt-pocock-skills` once in the chat "{title}" there; set that chat to the brain's model of the roles table; answer the brain's areas table there |

### Owner's list
1. Run `/setup-matt-pocock-skills` once in the chat "{title}" there.
2. Set that chat to the brain's model of the roles table (Fable 5.1 high, fallback Opus 5.5 xhigh).
3. Answer the brain's areas table there.

Closing note for the owner: if the target's tree was uncommitted when the port ran, the home's `core update` in that
target stays refused until the ported files (`.claude/settings.json`, `config/orchestration.json`, the port's
files) are committed there, by the owner or the target's brain; the system never commits in the owner's repo.
