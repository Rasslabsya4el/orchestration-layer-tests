"""`areas propose` (issue #321, unit 3). Fake issues, no network."""
from __future__ import annotations

from types import SimpleNamespace

from tools.agentops import areas as A
from tools.agentops import areas_propose as P
from tools.agentops import core_settings


def _i(n, labels=(), parent=None, state="open"):
    return A.Issue(n, 1000 + n, state, tuple(labels), parent, f"t{n}")


def _ten():
    return {
        1: _i(1, ["bug"]), 2: _i(2, ["bug"], 1), 3: _i(3, [], 1), 4: _i(4, ["bug"], 3, "closed"),
        5: _i(5), 6: _i(6, ["question"], 5), 7: _i(7, [], 5), 8: _i(8, [], 7, "closed"),
        9: _i(9), 10: _i(10, ["bug"]),
    }


def test_propose_text():
    out = P.propose_text(_ten(), P.core_label_names())
    lines = out.splitlines()
    assert lines[0] == "roots:"
    assert lines[1] == "#1 t1: 3 in the tree (2 open, 1 closed)"
    assert lines[2] == "#5 t5: 3 in the tree (2 open, 1 closed)"
    assert "loners: 2 (2 open, 0 closed)" in lines
    assert "bug x4" in lines and "question x1" in lines
    tail = lines[lines.index("outside the lists:") + 1:]
    assert tail == ["question"]
    print(out)


def test_no_areas_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"repo": "o/r", "areas": []})
    gh = SimpleNamespace(load_issues=_ten)
    assert A.run_areas(SimpleNamespace(areas_command="propose"), gh) == 0
    assert "loners: 2" in capsys.readouterr().out


def test_gh_failure_exits_one(monkeypatch):
    monkeypatch.setattr(core_settings, "load", lambda *a, **k: {"repo": "o/r"})

    def boom():
        raise A.AreasError("gh api failed")

    assert A.run_areas(SimpleNamespace(areas_command="propose"), SimpleNamespace(load_issues=boom)) == 1
