"""#354 U4: `guard install` and `guard check` on a tmp home and a tmp repo."""

import json

from tools.agentops import guard
from tools.agentops.cli import main

TODAY = ('{"enabledPlugins": {"mattpocock-skills@mattpocock": true}, "extraKnownMarketplaces": '
         '{"claude-plugins-official": {"source": {"source": "github", "repo": "anthropics/claude-plugins-official"}}}, '
         '"skipWorkflowUsageWarning": true, "theme": "dark", "switchModelsOnFlag": false, '
         '"agentPushNotifEnabled": true}')


def _user(tmp_path, text=TODAY):
    p = tmp_path / "home" / "settings.json"
    p.parent.mkdir()
    p.write_text(text + "\n", encoding="utf-8")
    return p


def test_install_adds_one_key_and_is_idempotent(tmp_path, capsys):
    p = _user(tmp_path)
    before = json.loads(TODAY)
    assert main(["guard", "install", "--path", str(p)]) == 0
    after = json.loads(p.read_text(encoding="utf-8"))
    assert len(before) == 6 and len(after) == 7
    assert list(after)[:6] == list(before) and all(after[k] == before[k] for k in before)
    assert after["permissions"]["defaultMode"] == "bypassPermissions"
    first = p.read_bytes()
    capsys.readouterr()
    assert main(["guard", "install", "--path", str(p)]) == 0
    assert capsys.readouterr().out.strip() == "0 changes"
    assert p.read_bytes() == first


def test_install_keeps_other_permissions(tmp_path):
    p = _user(tmp_path, '{"permissions": {"allow": ["Bash(ls)"], "defaultMode": "default"}, "theme": "dark"}')
    guard.install(p)
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["permissions"] == {"allow": ["Bash(ls)"], "defaultMode": "bypassPermissions"}
    assert list(d) == ["permissions", "theme"]


def _repo(root, good):
    (root / ".claude").mkdir(parents=True)
    s = {}
    if good:
        s = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "python tools/hooks/permission_guard.py"}]}]},
             "permissions": {"deny": list(guard.DENY_LINES)}}
    (root / ".claude" / "settings.json").write_text(json.dumps(s), encoding="utf-8")
    return root


def _cj(tmp_path, accepted):
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({"bypassPermissionsModeAccepted": True} if accepted else {"theme": "dark"}), encoding="utf-8")
    return p


def test_check_exit_codes(tmp_path):
    u = _user(tmp_path)
    cj = str(_cj(tmp_path, True))
    bad = _repo(tmp_path / "bad", False)
    good = _repo(tmp_path / "good", True)
    assert main(["guard", "check", "--path", str(u), "--repo", str(bad), "--claude-json", cj]) == 1
    guard.install(u)
    assert main(["guard", "check", "--path", str(u), "--repo", str(bad), "--claude-json", cj]) == 1
    assert main(["guard", "check", "--path", str(u), "--repo", str(good), "--claude-json", cj]) == 0


def test_check_disclaimer_fact(tmp_path, capsys):
    u = _user(tmp_path)
    guard.install(u)
    good = _repo(tmp_path / "good", True)
    no = str(_cj(tmp_path, False))
    assert main(["guard", "check", "--path", str(u), "--repo", str(good), "--claude-json", no]) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 5 and lines[4].startswith("FAIL") and "claude --dangerously-skip-permissions" in lines[4]
    yes = str(_cj(tmp_path, True))
    assert main(["guard", "check", "--path", str(u), "--repo", str(good), "--claude-json", yes]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 5 and lines[4].startswith("OK")


def test_install_prints_disclaimer_command_when_absent(tmp_path, capsys):
    p = _user(tmp_path)
    assert main(["guard", "install", "--path", str(p), "--claude-json", str(_cj(tmp_path, False))]) == 0
    assert "claude --dangerously-skip-permissions" in capsys.readouterr().out
    assert main(["guard", "install", "--path", str(p), "--claude-json", str(_cj(tmp_path, True))]) == 0
    assert capsys.readouterr().out.strip() == "0 changes"
