"""Issue 109 unit 3: `python -m tools.agentops retro-sources` over a tmp fixture folder with planted records and an
injected fake gh. No network, no file written by the command.

Seams: `run_retro_sources(args, root=, runner=, out=) -> rc` and `moment(raw) -> datetime` (the date argument).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from tools.agentops import retro_sources
from tools.agentops.retro_cmd import moment, run_retro_sources

REPO = "acme/orchestration-layer"
TAKEOVER_URL = "https://github.com/acme/orchestration-layer/issues/7#issuecomment-1"

CORE_BLOCK = """## Roles (orchestration v2)

| role | where | model / effort | job | protocol |
|---|---|---|---|---|
| brain | the brain chat | Fable 5.1 high | talks to the owner | `docs/agents/brain.md` |
| ORCH | background sessions | Sonnet 5.5 high | takes ready issues | `docs/agents/orch.md` |
| dispatcher | `python -m tools.agentops dispatch` | none | starts ORCH | `docs/agents/orch.md` |
| researcher-find / researcher-how | subagent | Haiku / Sonnet 5.5 | find / how | `.claude/agents/` |
| external researcher | GPT via opencode | Luna 5.6 | web research | `docs/agents/research.md` |
| worker / worker-strong | subagent in its own worktree | Haiku / Sonnet 5.5 | code + tests | `.claude/agents/` |
| reviewer-standards | subagent, read-only | Haiku 5.5 high | Standards axis | `.claude/agents/` |
| reviewer-spec | subagent, read-only | Sonnet 5.5 high | Spec axis | `.claude/agents/` |

The brain row's model binds the owner's desktop chat.
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _merge(root: Path, name: str, *, reason: str, started: str, in_line: int, suite: int,
           core_targets: list[str] | None = None) -> None:
    body = {"reason": reason, "started": started, "ended": started, "in_line_s": in_line, "suite_s": suite,
            "failed": True, "core_targets": core_targets or [], "issues": []}
    _write(root / "var" / "agentops" / "results" / name, json.dumps(body))


def _fixture(root: Path, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", tmp_path / "no-projects")
    _write(root / "config" / "orchestration.json",
           json.dumps({"repo": REPO, "claude_projects_dir": "C--fixture"}))
    _write(root / retro_sources.CORE_BLOCK, CORE_BLOCK)
    # the two target-check refusals of the real window: in line 14000 + 14380 s (7 h 53 min), suite 5760 + 5760 s (3 h 12 min)
    _merge(root, "20261016T100500-merge-a1.json", reason="target-check", started="2026-10-16T10:00:00",
           in_line=14000, suite=5760)
    _merge(root, "20261016T110500-merge-a2.json", reason="target-check", started="2026-10-16T11:00:00",
           in_line=14380, suite=5760)
    _merge(root, "20261016T130500-merge-c1.json", reason="pushed", started="2026-10-16T13:00:00", in_line=60,
           suite=300, core_targets=["acme/target-repo: failed: GitHub rate limit exceeded (retry later)"])


class FakeGh:
    """Answers each GraphQL search by its text: one takeover comment, every other search empty."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        query = next(a for a in args if a.startswith("q="))
        nodes: list[dict] = []
        if "taking over" in query:
            nodes = [{"number": 7, "url": TAKEOVER_URL, "state": "OPEN", "closedAt": None,
                      "comments": {"nodes": [{"body": "taking over #6 from orch-abc", "createdAt": "2026-10-16T12:00:00Z",
                                              "url": TAKEOVER_URL, "author": {"login": "orch"}}]}}]
        return json.dumps({"data": {"search": {"issueCount": len(nodes), "nodes": nodes}}})


def _tree(root: Path) -> list[str]:
    return sorted(os.path.relpath(os.path.join(dp, f), root) for dp, _, files in os.walk(root) for f in files)


def _run(root: Path, runner: FakeGh) -> tuple[int, list[str]]:
    out: list[str] = []
    rc = run_retro_sources(argparse.Namespace(since="2026-10-16", until="2026-10-17T15:00"),
                           root=root, runner=runner, out=out.append)
    return rc, out


def test_prints_both_tables_and_the_call_count(tmp_path, monkeypatch):
    root = tmp_path / "home"
    _fixture(root, monkeypatch, tmp_path)
    fake = FakeGh()
    rc, out = _run(root, fake)
    text = "\n".join(out)
    assert rc == 0
    assert "Table 1" in text and "Table 2" in text
    assert "7 h 53 min" in text and "3 h 12 min" in text
    assert "rate limit" in text
    assert TAKEOVER_URL in text
    assert any(f"repo:{REPO}" in a for a in fake.calls[0])
    assert text.splitlines()[-1] == f"GitHub calls this run: {len(fake.calls)}"
    assert len(fake.calls) == 4


def test_a_missing_location_is_a_no_records_row(tmp_path, monkeypatch):
    root = tmp_path / "home"
    _fixture(root, monkeypatch, tmp_path)
    _, out = _run(root, FakeGh())
    brain = [line for line in "\n".join(out).splitlines() if line.startswith("brain")]
    assert brain and "no records" in brain[0]


def test_the_command_writes_no_file(tmp_path, monkeypatch):
    root = tmp_path / "home"
    _fixture(root, monkeypatch, tmp_path)
    before = _tree(root)
    _run(root, FakeGh())
    assert _tree(root) == before


@pytest.mark.parametrize("raw, expected", [
    ("2026-10-16", datetime(2026, 10, 16, 0, 0)),
    ("2026-10-17T15:00", datetime(2026, 10, 17, 15, 0)),
])
def test_dates_accept_a_day_and_a_local_minute(raw, expected):
    assert moment(raw) == expected


def test_a_date_in_another_form_is_refused():
    with pytest.raises(ValueError):
        moment("17.10.2026")
