"""#113: the collector's cache folder is the `MONITOR_CACHE_DIR` folder when that variable is set, else the default
`mods/pob-slots/bin/.cache` (unchanged).

The collector is loaded in a child process (its `CACHE_DIR` is fixed at import). The override
is tested with a populated temp folder, so the real default `.cache` next to the collector is never written here.

    python -m pytest -q -p no:faulthandler tests/test_issue113_cache_env.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECT = next((ROOT / "mods").glob("*/bin/collect.py"))

PROGRAM = r'''
import importlib.util, json, sys

spec = importlib.util.spec_from_file_location("collect", sys.argv[1])
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)
sys.stdout.write(json.dumps({"cache": str(collect.CACHE_DIR), "facts": collect._pipeline_facts_read()}, default=str))
'''


def _load_collector(tmp_path: Path, cache_env: Path | None) -> dict:
    rig = tmp_path / "rig"
    (rig / "config").mkdir(parents=True, exist_ok=True)
    (rig / "config" / "orchestration.json").write_text(json.dumps({"repo": "owner/rig"}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "MONITOR_CACHE_DIR"}
    env["MONITOR_ROOT"] = str(rig)
    if cache_env is not None:
        env["MONITOR_CACHE_DIR"] = str(cache_env)
    r = subprocess.run([sys.executable, "-c", PROGRAM, str(COLLECT)], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout)


def test_override_folder_is_the_cache_and_its_pipeline_facts_are_read(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    rows = {"113": {"open": True, "labels": ["ready-for-agent"], "label_at": "2026-10-17T10:00:00Z",
                    "title": "override probe"}}
    (cache / "pipeline-tickets.json").write_text(json.dumps(rows), encoding="utf-8")

    data = _load_collector(tmp_path, cache)

    assert data["cache"] == str(cache)
    assert data["facts"] == rows


def test_unset_variable_keeps_the_default_cache_folder(tmp_path):
    data = _load_collector(tmp_path, None)

    assert data["cache"] == str(COLLECT.parent / ".cache")
