"""#317 unit 2: tools/agentops/areas_apply.py on a recording fake writer (no gh, no network)."""
from dataclasses import dataclass, replace

import pytest

from tools.agentops.areas_apply import apply_plan

AREAS = ["area:builds", "area:orchestration", "area:runs", "area:crafting"]
USED = set()


@dataclass(frozen=True)
class Iss:
    number: int
    id: int
    state: str = "open"
    labels: tuple = ()
    parent: int = None


class W:
    def __init__(self):
        self.calls = []

    def _rec(self, *a):
        self.calls.append(a)
        USED.add(a[0])

    def add_label(self, n, label):
        self._rec("add_label", n, label)

    def remove_label(self, n, label):
        self._rec("remove_label", n, label)

    def attach(self, parent, child_id):
        self._rec("attach", parent, child_id)


def mk(parents=None, labels=None, n=10):
    parents, labels = parents or {}, labels or {}
    return {i: Iss(i, 1000 + i, labels=tuple(labels.get(i, ())), parent=parents.get(i)) for i in range(1, n + 1)}


TREE = {3: 1, 4: 1, 5: 3, 6: 2, 7: 2, 8: 6}
PLAN = {"roots": {"1": "area:builds", "2": "area:runs"}, "loners": {"9": "area:crafting"}, "links": []}


def test_full_run_second_run_and_dry_run():
    issues = mk(TREE)
    w = W()
    out = apply_plan(issues, PLAN, AREAS, w)
    assert len(w.calls) == 9 and all(c[0] == "add_label" for c in w.calls)
    assert out[-3:] == ["not in the plan (1)", "#10", "links 0, labelled 9, unchanged 0, not in the plan 1"]
    d = W()
    assert apply_plan(issues, PLAN, AREAS, d, dry_run=True) == out and d.calls == []
    added = {c[1]: c[2] for c in w.calls}
    done = {n: replace(i, labels=(added[n],)) if n in added else i for n, i in issues.items()}
    w2 = W()
    out2 = apply_plan(done, PLAN, AREAS, w2)
    assert w2.calls == [] and out2[-1] == "links 0, labelled 0, unchanged 9, not in the plan 1"


def test_wrong_area_replaced_other_labels_untouched():
    issues = mk({2: 1}, {2: ("area:runs", "bug")})
    w = W()
    out = apply_plan(issues, {"roots": {"1": "area:builds"}}, AREAS, w)
    assert ("remove_label", 2, "area:runs") in w.calls and ("add_label", 2, "area:builds") in w.calls
    assert all(c[2] != "bug" for c in w.calls)
    assert "#2: +area:builds -area:runs" in out


def test_link_moves_loner_under_root():
    issues = mk()
    w = W()
    out = apply_plan(issues, {"roots": {"1": "area:builds"}, "links": [[9, 1]]}, AREAS, w)
    assert ("attach", 1, 1009) in w.calls and ("add_label", 9, "area:builds") in w.calls
    assert out[0] == "link #9 -> #1"
    w2 = W()
    apply_plan(mk({9: 1}), {"roots": {"1": "area:builds"}, "links": [[9, 1]]}, AREAS, w2)
    assert not [c for c in w2.calls if c[0] == "attach"]


def test_nested_roots():
    issues = mk({2: 1, 3: 2})
    w = W()
    apply_plan(issues, {"roots": {"1": "area:orchestration", "2": "area:builds"}}, AREAS, w)
    assert ("add_label", 3, "area:builds") in w.calls and ("add_label", 1, "area:orchestration") in w.calls


@pytest.mark.parametrize("plan", [
    {"roots": {"1": "area:nope"}},
    {"roots": {"99": "area:runs"}},
    {"loners": {"1": "area:runs"}, "roots": {"1": "area:runs"}},
    {"roots": {"1": "area:runs"}, "links": [[99, 1]]},
    {"roots": {"1": "area:runs"}, "links": [[2, 1], [2, 3]]},
    {"roots": {"1": "area:runs"}, "links": [[1, 2], [2, 1]]},
    {"roots": {"1": "area:runs"}, "links": [[1, 1]]},
])
def test_invalid_plans_raise_without_writes(plan):
    w = W()
    with pytest.raises(ValueError):
        apply_plan(mk(), plan, AREAS, w)
    assert w.calls == []


def test_writer_methods_only():
    assert USED <= {"add_label", "remove_label", "attach"}


def test_estimate_line_pins_human_duration_for_227():
    from tools.agentops.areas_apply import estimate_line

    # human_duration renders 227 s as "3.8 min" (not "3 min 47 s"); the core helper is not changed.
    assert estimate_line(227, 1.0) == "227 writes, about 3.8 min at 1 s each"


def test_227_pending_writes_give_11_progress_lines():
    from tools.agentops.areas_apply import ProgressWriter, count_writes

    n = 227
    issues = {i: Iss(i, 1000 + i) for i in range(1, n + 1)}
    plan = {"roots": {}, "loners": {str(i): "area:runs" for i in range(1, n + 1)}, "links": []}
    assert count_writes(issues, plan, AREAS) == n
    lines, t = [], iter(range(0, 10000))
    w = ProgressWriter(W(), n, emit=lines.append, clock=lambda: next(t))
    apply_plan(issues, plan, AREAS, w)
    assert len(lines) == 11
    assert lines[0].startswith("progress: 20 done, 207 left,")
    done = {i: replace(x, labels=("area:runs",)) for i, x in issues.items()}
    assert count_writes(done, plan, AREAS) == 0
