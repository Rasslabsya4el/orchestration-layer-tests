"""#55 unit 2: the `told` line names the issue numbers of the batch it sent.

Through `Dispatcher._send_to_brain` with the fake `claude` of `test_agentops_dispatch_brain_notice.py`
(its `wake` seam); the message lines are the real `format_line` of `brain_notice.py`.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_agentops_dispatch_brain_notice import BRAIN, NoticeClaude, NoticeGh, _dispatcher
from tools.agentops.brain_notice import format_line


def _told(d) -> list[str]:
    return [line for line in d.log_lines if line.startswith("told ")]


def test_batch_of_33_and_54_told_line_ends_with_their_numbers(tmp_path: Path):
    claude = NoticeClaude()
    d = _dispatcher(tmp_path, claude, NoticeGh())
    message = "\n".join([
        format_line(33, "The unslop mod is removed", "closed", "Closed: merged"),
        format_line(54, "Another ticket", "parked", "Parked: waits on a run"),
    ])

    d._send_to_brain(BRAIN, message)

    assert claude.notices() == [message]
    assert _told(d) == [f"told {BRAIN}: 2 issue(s) parked or closed (#33 #54)"]


def test_single_issue_batch_told_line_ends_with_its_number(tmp_path: Path):
    claude = NoticeClaude()
    d = _dispatcher(tmp_path, claude, NoticeGh())
    message = format_line(33, "The unslop mod is removed", "closed", "Closed: merged")

    d._send_to_brain(BRAIN, message)

    assert _told(d) == [f"told {BRAIN}: 1 issue(s) parked or closed (#33)"]


def test_numbers_are_named_in_ascending_order_whatever_the_line_order(tmp_path: Path):
    claude = NoticeClaude()
    d = _dispatcher(tmp_path, claude, NoticeGh())
    message = "\n".join([
        format_line(54, "Another ticket", "parked", "Parked: waits on a run"),
        format_line(33, "The unslop mod is removed", "closed", "Closed: merged"),
    ])

    d._send_to_brain(BRAIN, message)

    assert _told(d) == [f"told {BRAIN}: 2 issue(s) parked or closed (#33 #54)"]
