"""#59: `research` without the bridge's settings file prints ONE fallback line and returns non-zero,
no traceback; with the file, no fallback is printed; any other bridge failure still raises.

Seams: `cli.main(["research", brief])` (the dispatch) and `ocserver.ROOT` (the repo root the
settings file is read from). No network, no opencode process: the server start is stubbed or
pointed at a missing binary.
"""

from __future__ import annotations

import pytest

from tools.agentops import cli, ocserver
from tools.agentops.util import AgentOpsError

BRIEF = (
    "# brief\n\n1. A question.\n\nReport: `docs/research/2026-10-16-fallback.md`\n"
)
VALID_OPENCODE = {"provider": {"codex-sale-oai": {"models": {"gpt-6-luna": {}}}}}


def _brief(tmp_path):
    path = tmp_path / "brief.md"
    path.write_text(BRIEF, encoding="utf-8")
    return path


def _repo(tmp_path, name, content=None):
    root = tmp_path / name
    root.mkdir()
    if content is not None:
        (root / "opencode.json").write_text(content, encoding="utf-8")
    return root


def test_missing_settings_prints_one_fallback_line_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, "norepo")  # no opencode.json
    monkeypatch.setattr(ocserver, "ROOT", root)

    rc = cli.main(["research", str(_brief(tmp_path))])

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.err == ""  # no traceback on stderr
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("research bridge cannot start: ")
    assert str(root / "opencode.json") in lines[0]
    assert "researcher-how" in lines[0]


def test_empty_settings_prints_the_same_fallback_line(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, "emptyrepo", content="  \n")  # present but empty
    monkeypatch.setattr(ocserver, "ROOT", root)

    rc = cli.main(["research", str(_brief(tmp_path))])

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.err == ""
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("research bridge cannot start: ")
    assert str(root / "opencode.json") in lines[0]


def test_valid_settings_do_not_print_the_fallback(tmp_path, monkeypatch, capsys):
    import json

    root = _repo(tmp_path, "okrepo", content=json.dumps(VALID_OPENCODE))
    monkeypatch.setattr(ocserver, "ROOT", root)

    def stub_ensure(**kwargs):  # the server is not started: the bridge got past its settings
        raise AgentOpsError("stub: no server in this test")

    monkeypatch.setattr(ocserver, "ensure", stub_ensure)

    with pytest.raises(AgentOpsError, match="stub: no server"):
        cli.main(["research", str(_brief(tmp_path))])
    assert "research bridge cannot start" not in capsys.readouterr().out


def test_other_bridge_failure_still_raises(tmp_path, monkeypatch, capsys):
    import json

    root = _repo(tmp_path, "nobinrepo", content=json.dumps(VALID_OPENCODE))
    monkeypatch.setattr(ocserver, "ROOT", root)
    # Keep the server state and log out of the real var/agentops.
    monkeypatch.setattr(ocserver, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(ocserver, "STATE_FILE", tmp_path / "home" / "server.json")

    def no_binary():
        raise AgentOpsError("opencode not found: install it (npm i -g opencode-ai) or set AGENTOPS_OPENCODE")

    monkeypatch.setattr(ocserver, "find_opencode", no_binary)

    with pytest.raises(AgentOpsError, match="opencode not found"):
        cli.main(["research", str(_brief(tmp_path))])
    assert "research bridge cannot start" not in capsys.readouterr().out


def test_a_settings_file_that_is_still_the_port_stub_prints_the_fallback(tmp_path, monkeypatch, capsys):
    import copy
    import json

    stub = copy.deepcopy(VALID_OPENCODE)
    for provider in stub["provider"].values():
        provider.setdefault("options", {})["baseURL"] = "https://example.invalid/v1"
    root = _repo(tmp_path, "stubrepo", content=json.dumps(stub))
    monkeypatch.setattr(ocserver, "ROOT", root)

    rc = cli.main(["research", str(_brief(tmp_path))])

    out = capsys.readouterr().out.strip().splitlines()
    assert rc != 0 and len(out) == 1
    assert out[0].startswith("research bridge cannot start: ") and "stub" in out[0] and "researcher-how" in out[0]
