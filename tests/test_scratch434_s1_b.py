"""#435 (scratch run S1, part B): waits until part A is on origin/main, then checks b.txt."""
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = int(os.environ.get("S1_POLL_SECONDS", "20"))
MAX_SECONDS = int(os.environ.get("S1_MAX_SECONDS", "1500"))
A_PATH = "scratch/434-s1/a.txt"
B_PATH = ROOT / "scratch/434-s1/b.txt"


def _human(seconds):
    return f"{seconds} s" if seconds < 90 else f"{seconds / 60:.0f} min"


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _wait_for_part_a():
    deadline = time.monotonic() + MAX_SECONDS
    while True:
        _git("fetch", "origin")
        if _git("show", f"origin/main:{A_PATH}").returncode == 0:
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"part A ({A_PATH}) is not on origin/main after {_human(MAX_SECONDS)}; "
                "part B waits for it and does not pass without it"
            )
        time.sleep(POLL_SECONDS)


@pytest.mark.live  # it runs git fetch and git show in the real repo root: live state, as tests/_live_guard.py asks
def test_part_b_file_says_B_once_part_a_is_on_origin_main():
    _wait_for_part_a()
    assert B_PATH.exists(), f"{B_PATH} is missing"
    assert B_PATH.read_text(encoding="utf-8").strip() == "B"
