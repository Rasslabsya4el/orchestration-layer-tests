"""#201: a map build whose suite run passes its bound is stopped with its whole process tree, its build file and lock
removed, and one ``map failed | <sha12> | over the <bound> bound`` line in the log; a fast green suite still writes the
map; the bound comes from the settings key ``map_build_bound_minutes`` (default 120 min, an invalid value falls back).

Seams: ``run_map_run`` (the map-run subcommand's function) on the small repo of ``tests/test_select_build_map.py``, and
``resolve_map_build_bound`` on a settings file of the test's own (never the repo's config file)."""
import json
import sys
import time

import pytest

from tests.test_select_build_map import SUITE, _repo
from tools.agentops import core_settings, map_run
from tools.agentops.map_run import MAP_RUN_LOCK, resolve_map_build_bound, run_map_run
from tools.agentops.test_select import bound_text, map_dir, read_meta
from tools.agentops.util import pid_alive


def _patch_run(monkeypatch, repo, *, bound=None):
    monkeypatch.setattr(map_run, "machine_holds", lambda: False)
    monkeypatch.setattr(map_run, "ensure_suite_worktree", lambda *a, **k: repo)
    monkeypatch.setattr(map_run, "sha_on_main", lambda root, sha: True)  # #252: the main here is no git repo
    if bound is not None:
        monkeypatch.setattr(map_run, "resolve_map_build_bound", lambda: bound)


def _settings_file(tmp_path, monkeypatch, extra: dict):
    """A settings file of the test's own with the required keys only, plus ``extra``; ``load`` reads it."""
    path = tmp_path / "orchestration.json"
    data = {key: "x" for key in core_settings.REQUIRED_KEYS}
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", path)
    return path


def test_over_the_bound_stops_the_tree_and_logs_one_line(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    main = tmp_path / "main"
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "fake_suite.py"  # a suite that writes its datafile and its pid, then sleeps past the bound
    script.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path(r'{pid_file}').write_text(str(os.getpid()))\n"
        "open(os.environ['TESTMON_DATAFILE'], 'w').write('x')\n"
        "time.sleep(60)\n", encoding="utf-8")
    _patch_run(monkeypatch, repo, bound=3.0)
    log = tmp_path / "merge.log"

    start = time.monotonic()
    rc = run_map_run(sha, root=main, suite_cmd=f'"{sys.executable}" "{script}"', shared=(), log_path=log)
    elapsed = time.monotonic() - start

    assert rc == 1
    assert elapsed < 3.0 + 10.0
    assert not pid_alive(int(pid_file.read_text(encoding="utf-8")))  # the child is gone with its shell
    mdir = map_dir(main)
    assert not (mdir / MAP_RUN_LOCK).exists()
    assert list(mdir.glob("testmondata.build-*")) == []
    text = log.read_text(encoding="utf-8")
    assert text.count("map failed") == 1
    assert f"map failed | {sha[:12]} | over the {bound_text(3.0)} bound" in text


def test_a_fast_green_suite_still_writes_the_map(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    main = tmp_path / "main"
    _patch_run(monkeypatch, repo)  # the real bound: the repo's settings file, which names the key
    log = tmp_path / "merge.log"

    rc = run_map_run(sha, root=main, suite_cmd=SUITE, shared=(), log_path=log)

    mdir = map_dir(main)
    assert rc == 0
    assert (mdir / "testmondata").is_file()
    assert read_meta(mdir)["map_commit"] == sha
    assert not (mdir / MAP_RUN_LOCK).exists()
    assert "map built" in log.read_text(encoding="utf-8")


def test_bound_text_reads_whole_hours_as_hours():
    assert bound_text(2 * 3600.0) == "2 h"
    assert bound_text(90 * 60.0) == "1 h 30 min"


def test_absent_key_gives_the_default_120_minutes(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, {})
    assert resolve_map_build_bound() == 120 * 60.0


def test_a_configured_key_is_read_in_minutes(tmp_path, monkeypatch):
    _settings_file(tmp_path, monkeypatch, {"map_build_bound_minutes": 30})
    assert resolve_map_build_bound() == 30 * 60.0


@pytest.mark.parametrize("value", [0, -5, "x"])
def test_an_invalid_value_falls_back_to_the_default(tmp_path, monkeypatch, value):
    _settings_file(tmp_path, monkeypatch, {"map_build_bound_minutes": value})
    assert resolve_map_build_bound() == 120 * 60.0


def test_an_unreadable_settings_file_is_a_map_failed_line_not_a_silent_default(tmp_path, monkeypatch):
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", tmp_path / "missing.json")
    repo = tmp_path / "repo"
    sha = _repo(repo)
    log = tmp_path / "merge.log"
    _patch_run(monkeypatch, repo)
    rc = run_map_run(sha, root=tmp_path / "main", suite_cmd=SUITE, shared=(), log_path=log)
    assert rc == 1
    text = log.read_text(encoding="utf-8")
    assert text.count("map failed") == 1
    assert f"map failed | {sha[:12]} | missing settings file" in text
