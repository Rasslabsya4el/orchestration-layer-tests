"""#148 rule 4: every attempted `run_gh` call appends one JSON line to `var/agentops/gh_calls.jsonl`.

Fakes only (a counting `subprocess.Popen`, an injected clock, a tmp log path), no network, no `gh`."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tools.agentops import gh_call  # noqa: E402
from tools.agentops.gh_call import run_gh  # noqa: E402

from tests._gh_guard import REAL_SPAWN, _no_real_gh  # noqa: E402,F401

T0 = 1_000_000.0
MODULE = __name__.rsplit(".", 1)[-1]
AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class Rig:
    """Counts spawned processes; every call answers with `answer`."""

    def __init__(self) -> None:
        self.spawned: list[list[str]] = []
        self.answer: tuple[int, str, str] = (0, "ok", "")

    def popen(self, argv, **kwargs):
        self.spawned.append(list(argv))
        rig = self

        class _P:
            args = list(argv)
            pid = 1
            stdout = stderr = stdin = None

            def __init__(self) -> None:
                self.returncode, self._out, self._err = rig.answer

            def communicate(self, input=None, timeout=None):
                return self._out, self._err

            def kill(self):
                pass

        return _P()


@pytest.fixture
def rig(monkeypatch, tmp_path):
    r = Rig()
    monkeypatch.setattr(gh_call.subprocess, "Popen", r.popen)
    monkeypatch.setattr(gh_call, "_spawn_gh", REAL_SPAWN)  # the real spawn over the fake Popen above
    monkeypatch.setattr(gh_call, "_now", lambda: T0)
    monkeypatch.setattr(gh_call, "_blocked_until", {})
    r.log = tmp_path / "gh_calls.jsonl"
    monkeypatch.setattr(gh_call, "GH_CALLS_LOG", r.log)
    return r


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_rest_and_graphql_calls_write_one_line_each(rig):
    run_gh(["api", "repos/o/r/contents/x?ref=abc"])
    run_gh(["api", "graphql", "-f", "query=query { viewer { login } }"])

    lines = read_lines(rig.log)
    assert len(lines) == 2
    assert [entry["kind"] for entry in lines] == ["rest", "graphql"]
    assert [entry["endpoint"] for entry in lines] == ["repos/o/r/contents/x", "graphql"]
    for entry in lines:
        assert AT_RE.match(entry["at"]), entry["at"]
        assert entry["module"] == MODULE
        assert "blocked" not in entry


def test_other_command_endpoint_is_first_two_args(rig):
    run_gh(["issue", "view", "5"])

    (entry,) = read_lines(rig.log)
    assert entry["endpoint"] == "issue view"
    assert entry["kind"] == "graphql"


def test_rate_limit_read_is_logged_as_rest(rig):
    run_gh(["api", "rate_limit"])

    (entry,) = read_lines(rig.log)
    assert entry["kind"] == "rest"
    assert entry["endpoint"] == "rate_limit"


def test_failed_call_is_logged(rig):
    rig.answer = (1, "", "HTTP 404 Not Found")

    result = run_gh(["api", "repos/o/r"])

    assert result.returncode == 1
    (entry,) = read_lines(rig.log)
    assert entry["endpoint"] == "repos/o/r"


def test_unwritable_log_path_does_not_raise(rig, monkeypatch, tmp_path):
    blocker = tmp_path / "as_dir"
    blocker.mkdir()
    monkeypatch.setattr(gh_call, "GH_CALLS_LOG", blocker)

    result = run_gh(["api", "repos/o/r"])

    assert result.returncode == 0
    assert len(rig.spawned) == 1


def test_missing_log_directory_is_created(rig, monkeypatch, tmp_path):
    nested = tmp_path / "var" / "agentops" / "gh_calls.jsonl"
    monkeypatch.setattr(gh_call, "GH_CALLS_LOG", nested)

    run_gh(["api", "repos/o/r"])

    assert len(read_lines(nested)) == 1


def test_non_str_arg_never_breaks_the_call(rig):
    result = run_gh(["api", Path("repos/o/r")])

    assert result.returncode == 0
    assert len(rig.spawned) == 1
    (entry,) = read_lines(rig.log)
    assert entry["endpoint"] == str(Path("repos/o/r"))


def test_calling_module_of_a_script_is_its_file_stem(rig, tmp_path):
    script = tmp_path / "foo.py"
    glob = {"__name__": "__main__", "__file__": str(script), "run_gh": run_gh}
    exec(compile("run_gh(['api', 'repos/o/r'])", str(script), "exec"), glob)

    (entry,) = read_lines(rig.log)
    assert entry["module"] == "foo"


def test_calling_module_of_a_main_without_file_is_unknown(rig):
    glob = {"__name__": "__main__", "run_gh": run_gh}
    exec(compile("run_gh(['api', 'repos/o/r'])", "<interactive>", "exec"), glob)

    (entry,) = read_lines(rig.log)
    assert entry["module"] == "unknown"


def test_blocked_call_is_logged_with_blocked_true(rig):
    gh_call._blocked_until["core"] = T0 + 100.0

    result = run_gh(["api", "repos/o/r/contents/x"])

    assert result.returncode == 1
    assert result.transient is True
    assert rig.spawned == []
    (entry,) = read_lines(rig.log)
    assert entry["blocked"] is True
    assert entry["kind"] == "rest"
    assert entry["endpoint"] == "repos/o/r/contents/x"
