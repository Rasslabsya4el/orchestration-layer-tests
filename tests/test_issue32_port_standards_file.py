"""Issue #32 unit 2: the port writes ``CODING_STANDARDS.md`` into a target that lacks it.

The file is rendered from ``config/core_templates/CODING_STANDARDS.md.stub``: its ``{core_rules}`` line becomes the
``- `` rule lines of the core file ``docs/agents/core-standards.md``, and part 2 holds one plain sentence (no rules yet).
A target that already has one keeps it byte for byte. ``--dry-run`` lists the file among the own files it would write.

The checks on the written file are the ones of ``tests/test_coding_standards.py``, loaded from that file and run
against the target's copy, so the two cannot drift apart.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import port

REPO = Path(__file__).resolve().parents[1]
REPO_SLUG = "X/My Repo.v2"
UPDATE_RULE = "pull-request"
HOME_REPO = "owner/orchestration-layer"
CORE_VERSION = "0" * 40
PLUGIN_NAME = "mattpocock-skills@claude-plugins-official"
CORE_FILE = REPO / "docs" / "agents" / "core-standards.md"


def _rule_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


def _target(root: Path, name: str) -> Path:
    target = root / name
    target.mkdir(parents=True)
    return target


def _write_own(target: Path) -> None:
    port.write_own_files(target, REPO_SLUG, None, UPDATE_RULE, HOME_REPO, CORE_VERSION, plugin_name=PLUGIN_NAME)


def _checks_module():
    """``tests/test_coding_standards.py`` loaded under another name, so its checks can be run on any file."""
    spec = importlib.util.spec_from_file_location("_coding_standards_checks", REPO / "tests" / "test_coding_standards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_port_writes_coding_standards_file_when_absent(tmp_path, monkeypatch):
    target = _target(tmp_path / "targets", "no-standards-yet")
    _write_own(target)

    written = target / "CODING_STANDARDS.md"
    assert written.is_file(), "the port did not write CODING_STANDARDS.md"
    text = written.read_text(encoding="utf-8")

    core_rules = _rule_lines(CORE_FILE.read_text(encoding="utf-8"))
    assert core_rules, "the core file holds no rule lines"
    part_one, part_two = text.split("## 2.", 1)
    assert _rule_lines(part_one) == core_rules, "part 1 is not the core file's rule lines"
    assert _rule_lines(part_two) == [], "part 2 (the repo's own rules) must start empty"

    lines_after_heading = [line for line in part_two.splitlines()[1:] if line.strip()]
    assert len(lines_after_heading) == 1, lines_after_heading
    assert not lines_after_heading[0].startswith(("- ", "#")), lines_after_heading[0]

    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert len(headings) == 2, headings
    assert "{core_rules}" not in text, "the placeholder was not replaced"

    checks = _checks_module()
    monkeypatch.setattr(checks, "STANDARDS", written)
    checks.test_file_exists_and_is_under_120_lines()
    checks.test_every_rule_line_ends_with_its_source()
    checks.test_every_named_source_file_exists()
    checks.test_core_part_is_a_verbatim_copy_of_the_core_file()


def test_existing_coding_standards_file_is_kept_byte_for_byte(tmp_path):
    target = _target(tmp_path / "targets", "has-standards")
    original = "# Ours\r\n\r\n- our own rule (docs/x.md)\r\n".encode("utf-8")
    (target / "CODING_STANDARDS.md").write_bytes(original)

    _write_own(target)

    assert (target / "CODING_STANDARDS.md").read_bytes() == original


@pytest.fixture
def dry_run(monkeypatch):
    """`plan_port` with every check that needs a real git remote, the plugin or the network replaced."""
    for name in ("check_home_clean", "check_no_agents_md_only", "check_target_ready"):
        monkeypatch.setattr(port, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(port, "check_machine_ready", lambda *args, **kwargs: PLUGIN_NAME)

    def gh(args):
        return SimpleNamespace(returncode=0, stdout="[]")

    return gh


def test_dry_run_lists_coding_standards_file(tmp_path, dry_run):
    target = _target(tmp_path / "targets", "dry-no-standards")

    files, _labels = port.plan_port(target, REPO_SLUG, gh_runner=dry_run, settings_path=tmp_path / "settings.json")
    assert "CODING_STANDARDS.md" in files

    out = port.run_dry_run(target, REPO_SLUG, gh_runner=dry_run, settings_path=tmp_path / "settings.json")
    assert "file CODING_STANDARDS.md" in out.splitlines()


def test_dry_run_does_not_list_coding_standards_file_when_present(tmp_path, dry_run):
    target = _target(tmp_path / "targets", "dry-has-standards")
    (target / "CODING_STANDARDS.md").write_text("# Ours\n", encoding="utf-8")

    files, _labels = port.plan_port(target, REPO_SLUG, gh_runner=dry_run, settings_path=tmp_path / "settings.json")
    assert "CODING_STANDARDS.md" not in files
