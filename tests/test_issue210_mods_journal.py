"""#210 unit 1: every real install or removal of a top-level folder of the mods install root appends one line to a journal
beside the root (`<install root's parent>/mods-journal.log`), so the session guard can tell a real install by another
ticket from a test's stray folder.

Seams: `mods.run_install`, `mods.run_remove`, `port.install_monitor` and the journal functions of
`install_folder`. The install root is redirected by env AGENTOPS_MODS_DIR to a temp folder; the real journal
(`~/.claude/mods-journal.log`) is only compared, never written.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import install_folder, mods, port


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _repo_mod(repo: Path, name: str) -> None:
    """A repository copy of mod `name` (mods/<name>/) for `mods install` to copy."""
    _write(repo / "mods" / name / "hooks" / "index.ts", "export const x = 1\n")
    _write(repo / "mods" / name / ".claude-plugin" / "plugin.json", '{"name": "%s"}\n' % name)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    inst = tmp_path / "mods"
    inst.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(tmp_path / "settings.json", "{}\n")
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(inst))
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))
    return SimpleNamespace(tmp=tmp_path, mods=inst, repo=repo, journal=tmp_path / "mods-journal.log",
                           settings=tmp_path / "settings.json", commands=tmp_path / "commands")


def _lines(journal: Path) -> list[str]:
    return journal.read_text(encoding="utf-8").splitlines() if journal.is_file() else []


# -- row 4: run_install writes exactly one journal line beside the temp install root --------------------------------

def test_run_install_appends_one_line_beside_the_root(root, capsys):
    assert install_folder.journal_path() == root.journal
    _repo_mod(root.repo, "pob-slots")

    rc = mods.run_install("pob-slots", settings_path=root.settings, commands_dir=root.commands)

    assert rc == 0, capsys.readouterr().out
    lines = _lines(root.journal)
    assert len(lines) == 1, lines
    _when, name, action, pid = lines[0].split("\t")
    assert (name, action, pid) == ("pob-slots", "install", str(os.getpid()))
    assert [(n, a) for _, n, a in install_folder.journal_entries()] == [("pob-slots", "install")]


# -- row 4: run_remove writes exactly one journal line (after the rmtree) ------------------------------------------

def test_run_remove_appends_one_line_beside_the_root(root, capsys):
    _write(root.mods / "unslop" / "hooks" / "index.ts", "x\n")

    rc = mods.run_remove("unslop", settings_path=root.settings)

    assert rc == 0, capsys.readouterr().out
    assert not (root.mods / "unslop").exists()
    assert [(n, a) for _, n, a in install_folder.journal_entries()] == [("unslop", "remove")]
    assert len(_lines(root.journal)) == 1


# -- row 4: a remove of a folder that is not installed writes nothing -----------------------------------------------

def test_run_remove_of_a_missing_folder_writes_nothing(root, capsys):
    rc = mods.run_remove("unslop", settings_path=root.settings)

    assert rc == 0, capsys.readouterr().out
    assert not root.journal.exists()


# -- row 4: a failing install writes no line ------------------------------------------------------------------------

def test_failing_install_writes_no_line(root, capsys):
    # no repository copy of the mod: run_install refuses before any copy
    rc = mods.run_install("ghost", settings_path=root.settings, commands_dir=root.commands)

    assert rc == 1, capsys.readouterr().out
    assert not root.journal.exists()
    assert not (root.mods / "ghost").exists()


# -- row 4: port.install_monitor writes one line, and only one ------------------------------------------------------

def test_install_monitor_appends_one_line(root, capsys):
    target = root.tmp / "target"
    source = target / port.MONITOR_DIR
    _write(source / "hooks" / "project.ts", "export const COMMAND = 'x'\n")
    _write(source / "hooks" / "register.tsx", "export default {}\n")
    repo = "owner/Target-Repo"
    planned = port._plan_monitor_settings(target, repo, root.settings)

    result = port.install_monitor(target, repo, root.settings, planned, commands_dir=root.commands)

    assert result.folder == root.mods / "target-repo-monitor"
    entries = install_folder.journal_entries()
    assert [(n, a) for _, n, a in entries] == [("target-repo-monitor", "install")]
    assert len(_lines(root.journal)) == 1


# -- the journal's own functions ------------------------------------------------------------------------------------

def test_journal_entries_skips_malformed_lines_and_a_missing_file_is_empty(root):
    assert install_folder.journal_entries() == []
    _write(root.journal, "\n".join([
        "2026-10-19T08:41:12\tpob-slots\tinstall\t4321",
        "not a time\tpob-slots\tinstall\t4321",
        "2026-10-19T08:42:00\tunslop\tremove",
        "2026-10-19T08:43:00\tunslop\tmove\t4321",
        "2026-10-19T08:44:00\tunslop\tremove\t4322",
        "",
    ]) + "\n")

    entries = install_folder.journal_entries()

    assert [(n, a) for _, n, a in entries] == [("pob-slots", "install"), ("unslop", "remove")]
    assert entries[0][0].isoformat() == "2026-10-19T08:41:12"


def test_journal_append_swallows_a_write_failure(root, monkeypatch):
    # the journal's folder is a regular file, so the append cannot open it: the install must not break
    blocker = root.tmp / "blocker"
    _write(blocker, "x")
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(blocker / "mods"))

    install_folder.journal_append("pob-slots", "install")  # no exception

    assert install_folder.journal_entries() == []


# -- the real journal is never written by these tests ---------------------------------------------------------------

def test_the_journal_follows_the_temp_root_not_the_real_one(root, capsys):
    real = Path.home() / ".claude" / "mods-journal.log"
    assert install_folder.journal_path() != real  # the temp root's journal, never the owner's real one
    assert install_folder.journal_path().parent == install_folder.install_root().parent
    _repo_mod(root.repo, "pob-slots")

    mods.run_install("pob-slots", settings_path=root.settings, commands_dir=root.commands)
    mods.run_remove("pob-slots", settings_path=root.settings)

    assert len(install_folder.journal_entries()) == 2  # both lines went to the temp journal
