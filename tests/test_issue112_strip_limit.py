"""#112: a session's digest names its last tool result that says GitHub's API rate limit (`result: ...`), so the monitor's
strip can show «ждёт GitHub». Fixture transcripts, 0 PoB, no subprocess and no network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECT = ROOT / "mods" / "pob-slots" / "bin" / "collect.py"


def _collect():
    spec = importlib.util.spec_from_file_location("collect_112", COLLECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _transcript(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "session.jsonl"
    # digest() reads from the middle of the tail on, so its first line is cut off: a summary line stands for it
    lines = [json.dumps({"type": "summary"})] + [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _call(tool_id: str, name: str, inp: dict) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}}


def _result(tool_id: str, content, is_error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                     "content": content, "is_error": is_error}]}}


def _events(collect, path: Path) -> list[str]:
    return collect.digest(path).get("digest", "").splitlines()


def test_a_rate_limited_background_command_is_the_last_event(tmp_path):
    collect = _collect()
    path = _transcript(tmp_path, [
        _call("t1", "Bash", {"command": "gh issue view 4"}),
        _result("t1", "gh: API rate limit exceeded", is_error=True),
    ])
    assert _events(collect, path)[-1] == "result: gh: API rate limit exceeded"


def test_text_content_of_a_result_is_read(tmp_path):
    collect = _collect()
    path = _transcript(tmp_path, [
        _call("t1", "Bash", {"command": "gh pr list"}),
        _result("t1", [{"type": "text", "text": "gh: API rate limit exceeded"}]),
    ])
    assert _events(collect, path)[-1] == "result: gh: API rate limit exceeded"


def test_a_read_of_a_file_that_names_the_limit_is_not_an_event(tmp_path):
    # a Read of this very collector names «API rate limit» in its source: that is a file's text, not the limit
    collect = _collect()
    path = _transcript(tmp_path, [
        _call("t1", "Read", {"file_path": "collect.py"}),
        _result("t1", "# the GitHub API rate limit is read here"),
    ])
    assert not any(line.startswith("result:") for line in _events(collect, path))


def test_an_ordinary_result_is_left_out(tmp_path):
    collect = _collect()
    path = _transcript(tmp_path, [
        _call("t1", "Bash", {"command": "gh issue view 4"}),
        _result("t1", "title: a ticket"),
    ])
    assert not any(line.startswith("result:") for line in _events(collect, path))


def test_a_later_command_makes_the_limit_not_last(tmp_path):
    collect = _collect()
    path = _transcript(tmp_path, [
        _call("t1", "Bash", {"command": "gh issue view 4"}),
        _result("t1", "gh: API rate limit exceeded", is_error=True),
        _call("t2", "Bash", {"command": "gh pr list"}),
        _result("t2", "[]"),
    ])
    assert _events(collect, path)[-1].startswith("Bash")
