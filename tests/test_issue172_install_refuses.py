"""Issue #172 unit 3: `mods install` refuses (rc 1, nothing written) when the source's hooks import a name from
'./project' that the source's `hooks/project.ts` does not export; the one line names each missing name.

Seam: `mods.run_install` with a temp mods root, a temp repo and a temp install folder (env AGENTOPS_MODS_DIR /
AGENTOPS_MODS_REPO, HOME and USERPROFILE redirected, settings file and command folder passed in). The real
~/.claude is never touched.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

# a local module is loaded through importlib, as the loader loads it (core_check pattern I)
mods = importlib.import_module("tools.agentops.mods")

IMPORTED = "import { ALPHA_HOOK, BETA_HOOK } from './project';\nexport const hook = [ALPHA_HOOK, BETA_HOOK];\n"
ALL_EXPORTED = "export const ALPHA_HOOK = 1;\nexport const BETA_HOOK = 2;\n"
ONLY_ALPHA = "export const ALPHA_HOOK = 1;\n"
SETTINGS = b'{\n  "theme": "dark"\n}\n'


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,
                   capture_output=True)


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every file and folder under root as {relative path: bytes, or None for a folder}; {} when root is absent."""
    if not root.exists():
        return {}
    out: dict[str, bytes | None] = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        out[rel] = p.read_bytes() if p.is_file() else None
    return out


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    home = tmp_path / "home"
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    settings = tmp_path / "settings.json"
    _write(settings, SETTINGS)
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(mods_dir))
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    inst = mods_dir / mods.install_name("demo")
    return SimpleNamespace(mods=mods_dir, repo=repo, settings=settings, commands=commands, inst=inst,
                           src=repo / "mods" / "demo")


def _old(*paths: Path) -> None:
    """Back-date install files so the newer-in-install refusal (#921) does not fire before the check under test."""
    for p in paths:
        os.utime(p, (1_000_000_000, 1_000_000_000))


def _commit(r) -> None:
    _git(r.repo, "add", "-A")
    _git(r.repo, "commit", "-qm", "source")


def _install(r, force: bool = False) -> int:
    return mods.run_install("demo", force=force, settings_path=r.settings, commands_dir=r.commands)


def test_refuses_when_hooks_import_a_name_project_does_not_export(roots, capsys):
    _write(roots.src / "hooks" / "register.tsx", IMPORTED)
    _write(roots.src / "hooks" / "project.ts", ONLY_ALPHA)
    _commit(roots)
    # an earlier install is in place: its files and the settings file must stay byte-identical after the refusal
    _write(roots.inst / "hooks" / "register.tsx", "old register\n")
    _write(roots.inst / "hooks" / "project.ts", "old project\n")
    _old(roots.inst / "hooks" / "register.tsx", roots.inst / "hooks" / "project.ts")
    before_inst = _tree(roots.inst)
    before_settings = roots.settings.read_bytes()

    assert _install(roots) == 1
    out = capsys.readouterr().out
    assert "BETA_HOOK" in out
    assert "ALPHA_HOOK" not in out
    assert _tree(roots.inst) == before_inst
    assert roots.settings.read_bytes() == before_settings
    assert not (roots.inst / ".cache").exists()


def test_force_does_not_bypass_the_missing_export_refusal(roots, capsys):
    _write(roots.src / "hooks" / "register.tsx", IMPORTED)
    _write(roots.src / "hooks" / "project.ts", ONLY_ALPHA)
    _commit(roots)
    _write(roots.inst / "hooks" / "project.ts", "old project\n")
    _old(roots.inst / "hooks" / "project.ts")
    before_inst = _tree(roots.inst)
    before_settings = roots.settings.read_bytes()

    assert _install(roots, force=True) == 1
    assert "BETA_HOOK" in capsys.readouterr().out
    assert _tree(roots.inst) == before_inst
    assert roots.settings.read_bytes() == before_settings


def test_installs_when_every_imported_name_is_exported(roots):
    _write(roots.src / "hooks" / "register.tsx", IMPORTED)
    _write(roots.src / "hooks" / "project.ts", ALL_EXPORTED)
    _commit(roots)

    assert _install(roots) == 0
    assert (roots.inst / "hooks" / "register.tsx").read_bytes() == (roots.src / "hooks" / "register.tsx").read_bytes()
    assert (roots.inst / "hooks" / "project.ts").read_bytes() == ALL_EXPORTED.encode("utf-8")


def test_mod_without_hooks_folder_is_not_checked(roots):
    _write(roots.src / "DESIGN.md", "design")
    _commit(roots)

    assert _install(roots) == 0
    assert (roots.inst / "DESIGN.md").read_bytes() == b"design"
