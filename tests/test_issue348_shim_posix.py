"""Issue #348 unit 7: the gh shim on POSIX (the GitHub Linux test machine).

On Windows the shim is ``gh.cmd`` and PATHEXT finds it. On Linux the shim is the file ``gh`` (a ``#!/bin/sh`` script), and
``shutil.which`` skips a file without the execute bit, so the run's PATH reached /usr/bin/gh and ``check_gh_shim`` refused
(#357). The first test runs the POSIX branch of ``_write_shims`` on any machine (``os.name`` patched to ``posix`` for that
one call, ``os.chmod`` recorded); the second runs the real check on Linux/macOS only.
"""

import os
from pathlib import Path

import pytest

from tools.replays import launcher

CASE = {"moment_utc": "2026-10-01T00:00:00Z"}


def _config(root: Path) -> launcher.Config:
    (root / "harness" / "config").mkdir(parents=True, exist_ok=True)  # the deny list reads it (#357 unit 5)
    (root / "harness" / "config" / "orchestration.json").write_text('{"targets": []}', encoding="utf-8")
    return launcher.Config(claude_bin=str(root / "fake.py"), role_repo=root / "role", draft_dir=root / "draft",
                           runs_dir=root / "runs", trees_dir=root / "trees", harness_root=root / "harness",
                           state_dir=root / "state")


def test_posix_shim_is_a_shebang_file_made_executable_and_first_on_path(tmp_path, monkeypatch):
    state = tmp_path / "state" / "rstate"
    state.mkdir(parents=True)
    cfg = _config(tmp_path)
    modes: dict[str, int] = {}
    real_chmod = os.chmod

    def record_chmod(path, mode, *args, **kwargs):
        modes[Path(path).name] = mode
        real_chmod(path, mode, *args, **kwargs)

    real_name = os.name
    monkeypatch.setattr(os, "chmod", record_chmod)
    monkeypatch.setattr(os, "name", "posix")
    try:
        launcher._write_shims(state, cfg)
    finally:
        os.name = real_name

    shim = state / "gh"
    assert shim.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    assert modes.get("gh", 0) & 0o111 == 0o111, "the POSIX shim has no execute bit: PATH skips it"
    env = launcher._environment(state, CASE, "a")
    assert env["PATH"].split(os.pathsep)[0] == str(state), "the shim dir is not first on the run's PATH"


@pytest.mark.skipif(os.name == "nt", reason="the POSIX shim is the file gh: Linux/macOS PATH lookup")
def test_check_gh_shim_passes_on_posix_with_the_shim_first(tmp_path):
    tree = tmp_path / "trees" / "rtree"
    tree.mkdir(parents=True)
    state = tmp_path / "state" / "rstate"
    state.mkdir(parents=True)
    cfg = _config(tmp_path)
    launcher._write_shims(state, cfg)
    env = launcher._environment(state, CASE, "a")
    launcher.check_gh_shim(env, tree, state)  # raises when the check fails
