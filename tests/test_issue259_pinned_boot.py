"""#259: no test reads the machine's real boot time (a freshly booted GitHub runner booted after the rigs' fake session starts)."""
import pytest

from tools.agentops.dispatch import machine_boot_time


def test_the_boot_time_a_test_reads_is_older_than_any_fake_session_start():
    pytest.importorskip("psutil")
    assert machine_boot_time() == 0.0
