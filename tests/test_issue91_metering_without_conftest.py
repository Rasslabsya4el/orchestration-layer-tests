"""Issue #91: the core's metering test passes in a target that has no `tests/conftest.py`.

The test is run in a copy of the core's own files (the metering test and the `tools/` files it
imports, all manifest lines) that has no `tests/conftest.py`, in a subprocess. The copy writes its
own minimal pytest settings, so no `pytest.ini` of the home is read. The copy is made inside this
repo's folder (not a system temp folder), and removed when the test ends.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.agentops.util import BUILD_ENV_VARS

HOME = Path(__file__).resolve().parents[1]
METERING_TEST = HOME / "tests" / "test_agentops_metering.py"


# The core files the metering test imports (each one a line of tools/agentops/core_manifest.txt).
CORE_FILES_OF_THE_METERING_TEST = (
    "tools/agentops/__init__.py",
    "tools/agentops/core_format.py",
    "tools/agentops/metering.py",
    "tools/agentops/models.py",
    "tools/agentops/util.py",
)
# The env switches the merge suite sets for its own run (a plugin list, the plugins' files); none reaches the copy.
SUITE_ENV_SWITCHES = ("SUITE_PLAN_FILE", "SUITE_DURATIONS_FILE", "SUITE_EVENTS_FILE", *BUILD_ENV_VARS)
# The copy's own pytest settings; having an ini file here also stops pytest's search at the copy.
PYTEST_SETTINGS = "[pytest]\ntestpaths = tests\n"


def _copy_core_files_without_conftest(dest: Path) -> None:
    for rel in CORE_FILES_OF_THE_METERING_TEST:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HOME / rel, target)
    (dest / "tests").mkdir()
    shutil.copy2(METERING_TEST, dest / "tests" / METERING_TEST.name)
    (dest / "pytest.ini").write_text(PYTEST_SETTINGS, encoding="utf-8")


def test_metering_test_passes_in_a_copy_without_a_conftest_and_writes_no_real_log():
    with tempfile.TemporaryDirectory(prefix="issue91-copy-", dir=HOME) as tmp:
        dest = Path(tmp)
        _copy_core_files_without_conftest(dest)
        assert not (dest / "tests" / "conftest.py").exists()

        # The copy's own run log, seeded so that "unchanged size" is a real check, not "absent".
        log = dest / "artifacts" / "runs" / "agentops.jsonl"
        log.parent.mkdir(parents=True)
        log.write_bytes(b'{"seed": 1}\n')
        before = log.stat().st_size

        # The merge suite runs this test with `PYTEST_ADDOPTS=-p tools.agentops.suite_plan_plugin -p ...suite_events_plugin`
        # (merge.py:1939-1951 `_run_merge_suite`); the copy holds neither plugin, so the child starts clean of the suite's switches.
        env = {k: v for k, v in os.environ.items() if k not in SUITE_ENV_SWITCHES}
        env.update(PYTHONPATH=str(dest), PYTHONDONTWRITEBYTECODE="1")
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_agentops_metering.py", "-q",
             "-p", "no:faulthandler", "-p", "no:cacheprovider"],
            cwd=dest, env=env, capture_output=True, text=True, timeout=120,
            creationflags=creation_flags,
        )
        after = log.stat().st_size

    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    assert after == before
