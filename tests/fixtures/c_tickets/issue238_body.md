## For the human
- **What:** один тест очереди тяжёлых проверок иногда падает сам по себе: он читает файл вывода раньше, чем проверка успела его записать.
- **Why:** замечено при слиянии #233, 19.10: полный прогон показал один красный тест, повторный прогон и прогон на main прошли.
- **Plan:** тест ждёт появления файла вывода (с ограничением по времени), а не только появления строки в очереди.
- **Result:** тест проходит 20 запусков подряд из 20; сейчас иногда падает (число не измерено).
- **Undo:** two-way door (one revert); touches только этот тест, продукт не меняется.

## For the agent

Found while closing #233 (merge result `var/agentops/results/20261019T162913-merge-i233.json`, suite output).

### Facts
- `tests/test_issue224_wiring.py::test_a_heavy_line_check_waits_for_the_berth_and_frees_it_at_its_end`: line 205 reads `check.out` right after `wait_until(lambda: len(ms.waiting_rows()) == 1)`; the waiter row exists before `_take_berth` has written the first status line to `out`, so `FileNotFoundError` is possible (seen once in a 1871-test run; passed on rerun and alone).

### The rule
Wait (bounded, e.g. 10 s) until `out` exists and has text before asserting its start.

### Acceptance rows
1. `python -m pytest tests/test_issue224_wiring.py -q` repeated 20 times in a row: 20 passed.

### Not in this ticket
- Any change to `machine_berths.py` or the line's behaviour.

