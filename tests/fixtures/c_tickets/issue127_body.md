## For the human
- **What:** один тест ядра падает в каждом полном прогоне тестов слияния и проходит при повторе в одиночку; он должен проходить с первого раза.
- **Why:** замечено brain 17.10 18:5x при чтении пачки слияний: тот же тест упал во всех 18 прогонах слияний с 16.10 23:50, каждый раз повтор занимал 21-58 с; на итог слияний не повлиял, но каждое слияние ждёт лишний повтор и в отчёте висит «1 упавший тест».
- **Plan:** найти, что из остального набора тестов ломает этот тест (общее состояние или порядок), и убрать зависимость.
- **Result:** 0 упавших в полном прогоне тестов слияния; показывает прогон всего набора и следующие 3 слияния без повтора этого теста.
- **Undo:** two-way door (one revert); touches один тест ядра и его копии во всех репо с ядром.

## For the agent

A Flow fix (GLOSSARY): it slows every merge of the home. Call `diagnosing-bugs` first; the cause, read in code or measured, is the report's first line.

### Facts (brain, 17.10 18:5x, from the merge results)

- `tests/test_issue91_metering_without_conftest.py::test_metering_test_passes_in_a_copy_without_a_conftest_and_writes_no_real_log` is `FAILED` in the full merge suite of all 18 merges that ran a suite since `var/agentops/results/20261016T235001-merge-i73b-int.json` (19 ran a suite of 64 merge results in that window), and passes on the rerun of failed tests alone (`1 passed in 21-58 s`), so each result says `failed=0`.
- Latest: `var/agentops/results/20261017T180210-merge-i120-int.json`: `1 failed, 3101 passed, 1 deselected in 22 min 50 s`, then `1 passed in 32.46s`.
- The test came with "A repo that takes the core gets core tests that pass without the home's conftest" (#91, closed).

### Acceptance rows

| row | command | expected |
|---|---|---|
| 1 | the cause | named with file:line (which other test or shared state breaks it in the full run) |
| 2 | the full suite as the merge runs it, on the branch | 0 failed, the test passes in the first pass |
| 3 | the test alone, and together with the tests row 1 names | passes |
| 4 | `python -m tools.agentops core check` | rc 0 |

Live row (read by the brain): the next 3 home merges that run a suite show no `FAILED` line for this test.

### Not in this ticket

- The rerun-of-failed-tests step itself (it works as meant).

