"""The one `git` helper of the merge tests that build real repos in a tmp folder (#223).

Windows can fail to start a process under load (exit 0xC0000142, DLL init failed): that is not an answer of git, so
the helper starts the same command again (3 retries). Any other failure raises as it did."""
from __future__ import annotations

import subprocess

OS_COULD_NOT_START = 0xC0000142  # STATUS_DLL_INIT_FAILED


def git(cwd, *args) -> str:
    argv = ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args]
    for _attempt in range(3):
        try:
            return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
        except subprocess.CalledProcessError as exc:
            if exc.returncode != OS_COULD_NOT_START:
                raise
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
