"""Issue #59 unit 2: the port writes the bridge's settings file, `opencode.json`, as a STUB.

The stub comes from `config/core_templates/opencode.json.stub`: the shape `ocserver.read_opencode_config` reads
(`provider.<id>.options.baseURL`, an env-reference `apiKey`, the models table) with a fake baseURL and no secret. A
target that already holds its own `opencode.json` keeps it byte for byte; the port summary names the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import ocserver, port

REPO = Path(__file__).resolve().parents[1]
REPO_SLUG = "X/My Repo.v2"
HOME_REPO = "owner/orchestration-layer"
PLUGIN_NAME = "mattpocock-skills@claude-plugins-official"
TEMPLATE = REPO / "config" / "core_templates" / "opencode.json.stub"


def _target(root: Path, name: str) -> Path:
    target = root / name
    target.mkdir(parents=True)
    return target


def _write_own(target: Path) -> list[str]:
    return port.write_own_files(target, REPO_SLUG, None, "word", HOME_REPO, "0" * 40, plugin_name=PLUGIN_NAME)


def test_target_without_opencode_json_gets_the_stub(tmp_path):
    target = _target(tmp_path, "no-bridge-yet")
    written = _write_own(target)

    path = target / "opencode.json"
    assert "opencode.json" in written
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.read_bytes() == TEMPLATE.read_bytes()
    with pytest.raises(ocserver.BridgeConfigMissing):  # unit 1: the stub is read as "not filled in yet"
        ocserver.read_opencode_config(path)
    providers = data["provider"]
    assert providers, "the stub holds no provider"
    for provider in providers.values():
        assert provider["options"]["baseURL"].startswith("https://example.invalid"), "the stub must hold a fake baseURL"
        assert provider["options"]["apiKey"] == "{env:CODEX_LB_API_KEY}", "the key must be an env reference"
        assert {"gpt-5.6-terra", "gpt-6-luna"} <= set(provider["models"])
    assert "baseURL" in data["_comment"] and "CODEX_LB_API_KEY" in data["_comment"]
    for provider in providers.values():  # filled in by the owner, the bridge accepts the same shape
        provider["options"]["baseURL"] = "https://real.example/v1"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert ocserver.build_config(config_path=path)["provider"] == providers


def test_target_with_its_own_opencode_json_keeps_it_byte_for_byte(tmp_path):
    target = _target(tmp_path, "has-bridge")
    original = b'{"provider": {"mine": {"options": {"baseURL": "https://real.example/v1"}}}}\r\n'
    (target / "opencode.json").write_bytes(original)

    written = _write_own(target)

    assert (target / "opencode.json").read_bytes() == original
    assert "opencode.json" not in written


def test_second_write_leaves_the_stub_alone(tmp_path):
    target = _target(tmp_path, "twice")
    _write_own(target)
    (target / "opencode.json").write_text('{"edited": true}\n', encoding="utf-8")

    assert "opencode.json" not in _write_own(target)
    assert (target / "opencode.json").read_text(encoding="utf-8") == '{"edited": true}\n'


def test_port_summary_names_the_file():
    report = port.PortReport(own_files_written=["CLAUDE.md", "opencode.json"], next_step="x")
    assert "opencode.json" in report.render()
    assert "opencode.json" not in port.PortReport(own_files_written=["CLAUDE.md"], next_step="x").render()


@pytest.fixture
def dry_run(monkeypatch):
    for name in ("check_home_clean", "check_no_agents_md_only", "check_target_ready"):
        monkeypatch.setattr(port, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(port, "check_machine_ready", lambda *args, **kwargs: PLUGIN_NAME)
    return lambda args: SimpleNamespace(returncode=0, stdout="[]")


def test_dry_run_lists_the_stub_only_when_absent(tmp_path, dry_run):
    settings = tmp_path / "settings.json"
    bare = _target(tmp_path, "dry-bare")
    own = _target(tmp_path, "dry-own")
    (own / "opencode.json").write_text("{}\n", encoding="utf-8")

    bare_files, _ = port.plan_port(bare, REPO_SLUG, gh_runner=dry_run, settings_path=settings)
    own_files, _ = port.plan_port(own, REPO_SLUG, gh_runner=dry_run, settings_path=settings)
    assert "opencode.json" in bare_files
    assert "opencode.json" not in own_files


def test_template_is_a_manifest_file():
    assert "config/core_templates/opencode.json.stub" in port.read_manifest(port.MANIFEST_PATH)
