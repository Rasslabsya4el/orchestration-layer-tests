"""#166 unit 2: the collector writes `collector-fast.json` where the installed pane reads it.

The pane reads `<install root>/<monitor.plugin_id>/bin/.cache/collector-fast.json` (`mods/pob-slots/hooks/project.ts`
MOD_DIR + `/bin/.cache/collector-fast.json`, read at `hooks/register.tsx`). `collect.fast_path()` is the collector's
side of that one path:

- `MONITOR_CACHE_DIR` (or a CACHE_DIR moved off its default) wins: the file stays in CACHE_DIR;
- else the install folder `<install root>/<plugin id>` when it exists: the file goes to its `bin/.cache`;
- else (no install folder) CACHE_DIR, and no install folder is ever created.

No PoB, no network, no process started: the install root is a tmp dir (AGENTOPS_MODS_DIR) except in test (b), which
only reads the real project.ts and computes a path.

    python -m pytest -q -p no:faulthandler tests/test_issue166_fast_path.py
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mods" / "pob-slots" / "bin"
PROJECT_TS = ROOT / "mods" / "pob-slots" / "hooks" / "project.ts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BIN))

import collect  # noqa: E402

OUT = "collector-fast.json"
PANE_TAIL = "/bin/.cache/collector-fast.json"


def _plugin_id() -> str:
    from tools.agentops import install_folder

    return install_folder.plugin_id(ROOT)


def _pane_read_path(install_root: Path) -> Path:
    """The pane's read path: project.ts's MOD_DIR with the tmp install root substituted for its folder, plus the
    template tail of COLLECTOR_FAST. Parsed from the file, never typed in here."""
    text = PROJECT_TS.read_text(encoding="utf-8")
    mod_dir = re.search(r"^export const MOD_DIR = '([^']+)'", text, re.M).group(1)
    tail = re.search(r"^export const COLLECTOR_FAST = `\$\{MOD_DIR\}([^`]+)`", text, re.M).group(1)
    assert tail == PANE_TAIL, f"project.ts COLLECTOR_FAST tail changed: {tail!r}"
    return install_root / Path(mod_dir).name / tail.lstrip("/")


def _install(install_root: Path) -> Path:
    folder = install_root / _plugin_id()
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def default_cache(monkeypatch):
    """No cache override: MONITOR_CACHE_DIR unset and CACHE_DIR at its default folder."""
    monkeypatch.delenv("MONITOR_CACHE_DIR", raising=False)
    monkeypatch.setattr(collect, "CACHE_DIR", collect._DEFAULT_CACHE_DIR)
    return collect._DEFAULT_CACHE_DIR


def test_a_collector_write_path_is_the_pane_read_path(tmp_path, monkeypatch, default_cache):
    install_root = tmp_path / "mods"
    _install(install_root)
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(install_root))

    assert collect.fast_path() == _pane_read_path(install_root)


def test_b_real_project_ts_names_the_installed_monitor_folder(monkeypatch, default_cache):
    monkeypatch.delenv("AGENTOPS_MODS_DIR", raising=False)
    real_root = Path.home() / ".claude" / "mods"
    if not (real_root / _plugin_id()).is_dir():
        pytest.skip("the monitor is not installed on this machine")

    path = collect.fast_path()

    assert path.as_posix().endswith("/orch-monitor/bin/.cache/collector-fast.json")
    assert path == real_root / _plugin_id() / "bin" / ".cache" / OUT
    assert path == _pane_read_path(real_root)


def test_c_monitor_cache_dir_wins_over_the_install_folder(tmp_path, monkeypatch):
    install_root = tmp_path / "mods"
    _install(install_root)
    cache = tmp_path / "cache"
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(install_root))
    monkeypatch.setenv("MONITOR_CACHE_DIR", str(cache))
    monkeypatch.setattr(collect, "CACHE_DIR", cache)

    assert collect.fast_path() == cache / OUT


def test_d_missing_install_folder_falls_back_to_cache_dir(tmp_path, monkeypatch, default_cache):
    install_root = tmp_path / "mods"
    install_root.mkdir()
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(install_root))

    assert collect.fast_path() == collect.CACHE_DIR / OUT
    assert not (install_root / _plugin_id()).exists(), "the install folder must never be created"


def test_e_write_creates_bin_cache_under_an_existing_install_folder(tmp_path, monkeypatch, default_cache):
    install_root = tmp_path / "mods"
    folder = _install(install_root)
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(install_root))

    collect._write_collector_fast({"at_ms": 1, "refresh_s": 2, "data": {"version": 7}})

    written = folder / "bin" / ".cache" / OUT
    assert json.loads(written.read_text(encoding="utf-8")) == {"at_ms": 1, "refresh_s": 2, "data": {"version": 7}}
    assert not list(written.parent.glob("*.tmp")), "the tmp file must be swapped in, not left behind"
