"""#279: a memory cap set by a process that has since ended still reads back from another process. ``cap_process_memory``
duplicates its job handle into the capped process, so the job's name lives as long as that process does. The sleeper
processes here never allocate; they only hold the cap. Windows only (ctypes against kernel32); listed in
tests/pc_only.txt."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import jobcap

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")

LIMIT = 2 * 1024 ** 3  # the collector's cap: 2147483648 bytes
PROCESS_DUP_HANDLE = 0x0040
SLEEP_CMD = "import time; time.sleep(60)"  # ends by itself if the test dies before it kills the sleeper
REPO = str(Path(__file__).resolve().parents[1])

# The capping child: starts a sleeper, caps it at LIMIT, prints "<sleeper pid> <ok or the cap's message>" and exits
# without killing the sleeper.
CAPPER = r'''
import subprocess, sys
sys.path.insert(0, sys.argv[1])
from tools.agentops import jobcap
sleeper = subprocess.Popen([sys.executable, "-c", sys.argv[2]], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
msg = jobcap.cap_process_memory(sleeper.pid, int(sys.argv[3]))
print(sleeper.pid, "ok" if msg is None else msg, flush=True)
'''


@pytest.fixture
def sleeper():
    p = subprocess.Popen([sys.executable, "-c", SLEEP_CMD], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    yield p
    if p.poll() is None:
        p.kill()
    p.wait()


def test_limit_reads_back_after_the_process_that_set_it_has_ended():
    capper = subprocess.run([sys.executable, "-c", CAPPER, REPO, SLEEP_CMD, str(LIMIT)], capture_output=True,
                            text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    pid_text, _, outcome = capper.stdout.strip().partition(" ")
    assert capper.returncode == 0, capper.stderr
    sleeper_pid = int(pid_text)
    try:
        assert outcome == "ok", capper.stdout
        assert jobcap.process_memory_limit(sleeper_pid) == 2147483648
    finally:
        os.kill(sleeper_pid, signal.SIGTERM)  # on Windows: TerminateProcess


def test_a_refused_duplicate_does_not_fail_the_cap(monkeypatch, sleeper):
    real_open = jobcap._open_process

    def refuse_dup_access(pid, access):
        if access & PROCESS_DUP_HANDLE:
            raise OSError("stubbed: PROCESS_DUP_HANDLE refused")
        return real_open(pid, access)

    monkeypatch.setattr(jobcap, "_open_process", refuse_dup_access)
    try:
        assert jobcap.cap_process_memory(sleeper.pid, LIMIT) is None
        assert jobcap.process_memory_limit(sleeper.pid) == LIMIT  # still limited, read through this process
    finally:
        handle = jobcap._HELD.pop(sleeper.pid, None)
        if handle:
            jobcap._kernel32().CloseHandle(handle)
