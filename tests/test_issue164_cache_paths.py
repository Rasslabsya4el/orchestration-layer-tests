"""#164: the collector's two remaining cache files (`opencode_launch.json` and `orch.json`) live in the cache folder
`CACHE_DIR`, so the `MONITOR_CACHE_DIR` override moves them with the rest. Without the variable both paths are the
default `mods/pob-slots/bin/.cache/<name>`, as before.

The collector is loaded in a child process (its `CACHE_DIR` and the cache paths are fixed at import). The override
run calls `opencode()` and `orch()` with fakes for their inputs, so the real default `.cache` next to the collector
is never written by this test: its listing and mtimes are taken before and after and must not change.

    python -m pytest -q -p no:faulthandler tests/test_issue164_cache_paths.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECT = next((ROOT / "mods").glob("*/bin/collect.py"))
DEFAULT_CACHE = COLLECT.parent / ".cache"

PROGRAM = r'''
import importlib.util, json, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("collect", sys.argv[1])
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)
scratch = Path(sys.argv[2])
out = {"cache_dir": str(collect.CACHE_DIR), "opencode_cache": str(collect._oc_launch_cache()),
       "orch_cache": str(collect._orch_cache())}

if sys.argv[3] == "run":
    class FakeClient:  # the opencode server: no sessions, nothing busy
        def call(self, method, path, directory=None, timeout=None):
            return [] if path == "/session" else {}

    # one registry entry with a Claude session: opencode() has something to remember, so it writes its file
    registry = {"ses_164": {"claude_session": "claude-164", "directory": str(scratch)}}
    out["rows"] = collect.opencode(client=FakeClient(), registry=registry, now_ms=1_700_000_000_000)

    # orch(): no transcripts and no run events, so only its own cache file is written
    sys.path.insert(0, sys.argv[4])  # the project's scripts/ (REPO is the rig here, not the project)
    import token_meter as tm
    tm.discover_main_transcripts = lambda root: []
    collect._closed_by_session = lambda: ({}, set())
    out["orch_keys"] = sorted(collect.orch().keys())

sys.stdout.write(json.dumps(out, default=str))
'''


def _snapshot(folder: Path):
    """The folder's listing with sizes and mtimes, or None when the folder is absent."""
    if not folder.exists():
        return None
    return sorted((str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in folder.rglob("*"))


def _run_collector(tmp_path: Path, cache_env: Path | None, mode: str) -> dict:
    rig = tmp_path / "rig"
    (rig / "config").mkdir(parents=True, exist_ok=True)
    (rig / "config" / "orchestration.json").write_text(json.dumps({"repo": "owner/rig"}), encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "MONITOR_CACHE_DIR"}
    env["MONITOR_ROOT"] = str(rig)
    if cache_env is not None:
        env["MONITOR_CACHE_DIR"] = str(cache_env)
    r = subprocess.run([sys.executable, "-c", PROGRAM, str(COLLECT), str(scratch), mode, str(ROOT / "scripts")],
                       cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout)


def test_override_folder_receives_opencode_and_orch_cache_files(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    before = _snapshot(DEFAULT_CACHE)

    data = _run_collector(tmp_path, cache, "run")

    assert data["opencode_cache"] == str(cache / "opencode_launch.json")
    assert data["orch_cache"] == str(cache / "orch.json")
    assert (cache / "opencode_launch.json").is_file()
    assert (cache / "orch.json").is_file()
    assert _snapshot(DEFAULT_CACHE) == before


def test_unset_variable_keeps_the_default_paths(tmp_path):
    data = _run_collector(tmp_path, None, "paths")

    assert data["opencode_cache"] == str(DEFAULT_CACHE / "opencode_launch.json")
    assert data["orch_cache"] == str(DEFAULT_CACHE / "orch.json")
