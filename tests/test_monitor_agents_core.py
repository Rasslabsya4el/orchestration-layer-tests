"""#885 E2b (E6): the agents part of the collector works where the project's own package, `cloud_lane` and
`token_meter` are absent.

The blocked names are the folders under `src/` (the project's packages; none in a project without `src/`), plus the two
core modules named here. A child Python blocks them (and their submodules) on `sys.meta_path` before it imports
`tools.agentops.monitor_agents` and the collector. Its project is a temp folder with a config and NO add-on
(`monitor.addon` absent), so the add-on's runs part is absent. The outside calls (`claude agents`, the opencode server,
the merge line, the machine counters) are stubbed in the child: the test starts no process and writes no cache.
Every part the child calls must return without an `*_error` key; the agents part is an empty list and the add-on's
keys are absent.

    python -m pytest -q tests/test_monitor_agents_core.py
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
from pathlib import Path

BLOCKED = tuple(json.loads(sys.argv[2]))


class Blocked:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError(f"blocked for the test: {fullname}")
        return None


sys.meta_path.insert(0, Blocked())
from tools.agentops import monitor_agents as ma  # must import with the blocked names absent

spec = importlib.util.spec_from_file_location("collect", sys.argv[1])
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

ma.live_sessions_raw = lambda: []  # no `claude agents` call
collect.opencode = lambda: []  # no opencode server call
collect.merge_status = lambda: {"missing": True}  # no merge-status subprocess
collect.machine = lambda *a, **k: {}  # no process table read, no cache file

out = {
    "agents": collect.agents(),
    "fast": collect.fast(),
    "slow": collect.slow(),
    "orch": collect.orch(),
    "blocked_loaded": sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED),
}
sys.stdout.write(json.dumps(out, default=str))
'''


def _project_packages() -> list[str]:
    src = ROOT / "src"
    if not src.is_dir():
        return []
    return sorted(p.name for p in src.iterdir() if (p / "__init__.py").is_file())


def test_agents_part_works_without_the_project_package_cloud_lane_or_token_meter(tmp_path):
    blocked = [*_project_packages(), "cloud_lane", "token_meter"]
    rig = tmp_path / "rig"
    (rig / "config").mkdir(parents=True)
    (rig / "config" / "orchestration.json").write_text(json.dumps({"repo": "owner/rig"}), encoding="utf-8")
    cache = tmp_path / "cache"  # #113: an empty cache folder, so a live run beside the collector cannot change the result
    cache.mkdir()
    env = {**os.environ, "MONITOR_ROOT": str(rig), "MONITOR_CACHE_DIR": str(cache)}
    r = subprocess.run([sys.executable, "-c", PROGRAM, str(COLLECT), json.dumps(blocked)], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0, r.stderr[-800:]
    data = json.loads(r.stdout)

    assert data["blocked_loaded"] == []  # nothing of the blocked names was imported
    assert data["agents"] == []  # the agents part: returns, empty
    assert data["fast"]["agents"] == []
    assert set(data["fast"]) == {"at", "agents", "machine", "merge", "pipeline", "version"}  # no add-on section: no runs part
    assert data["fast"]["version"] is None  # #157: not the home repo and no core_version_count in this rig
    assert data["fast"]["pipeline"] == []  # #6: no tickets facts yet, no agents: no rows
    # #161: no token_meter = the cost windows say why (an error key each), no `cut`; the cost panels stay absent
    assert set(data["slow"]) == {"at", "cost_day_error", "cost_week_error", "cost_month_error"}
    assert set(data["orch"]) == {"at"}  # the ORCH part is absent
    errors = [k for part in ("fast", "orch") for k in data[part] if k.endswith("_error")]
    assert errors == []
