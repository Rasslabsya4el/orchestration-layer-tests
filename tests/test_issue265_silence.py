"""#265 unit 1: the pure decision "is an ORCH session silent" (``tools/agentops/orch_silence.py``).

Seams: ``silence_verdict`` (a pure function of the times and flags it is given) and ``silent_orch_limit_seconds``
(the settings key ``silent_orch_minutes``, default 30). No clock, no file, no process: every time is a fixed
local date-time built here, so the differences are exact."""
import dataclasses
from datetime import datetime

import pytest

from tools.agentops.orch_silence import SilenceVerdict, silence_verdict, silent_orch_limit_seconds

LIMIT = 30 * 60.0


def t(hour, minute, second=0):
    return datetime(2026, 10, 20, hour, minute, second).timestamp()


def test_a_not_finishing_silent_30_min_wakes_with_shells_carried():
    verdict = silence_verdict(
        now=t(2, 9, 3),
        own_last_record=t(1, 39, 3),
        subagent_last_records=(),
        finishing=False,
        woken_at=None,
        limit_seconds=LIMIT,
        shells=("b2", "b1"),
    )
    assert verdict.action == "wake"
    assert verdict.shells == ("b1", "b2")
    assert verdict.silent_seconds == 1800.0


def test_a_four_seconds_short_of_the_limit_is_none():
    verdict = silence_verdict(
        now=t(2, 8, 59),
        own_last_record=t(1, 39, 3),
        subagent_last_records=(),
        finishing=False,
        woken_at=None,
        limit_seconds=LIMIT,
        shells=("b1", "b2"),
    )
    assert verdict.action == "none"


def test_b_after_wake_without_answer_ends_only_when_a_full_limit_has_passed():
    common = dict(
        own_last_record=t(1, 39, 3),
        subagent_last_records=(),
        finishing=False,
        woken_at=t(2, 9, 3),
        limit_seconds=LIMIT,
    )
    assert silence_verdict(now=t(2, 38, 59), **common).action == "none"
    assert silence_verdict(now=t(2, 39, 3), **common).action == "end"


def test_b_after_wake_with_a_newer_record_is_none():
    verdict = silence_verdict(
        now=t(2, 39, 3),
        own_last_record=t(2, 20, 0),
        subagent_last_records=(),
        finishing=False,
        woken_at=t(2, 9, 3),
        limit_seconds=LIMIT,
    )
    assert verdict.action == "none"


def test_c_finishing_silent_30_min_ends():
    kwargs = dict(
        own_last_record=t(3, 24, 57),
        subagent_last_records=(),
        finishing=True,
        woken_at=None,
        limit_seconds=LIMIT,
    )
    assert silence_verdict(now=t(3, 54, 57), **kwargs).action == "end"
    assert silence_verdict(now=t(3, 54, 56), **kwargs).action == "none"


def test_d_running_subagent_with_a_fresh_record_keeps_the_session_none():
    for finishing in (False, True):
        for shells in ((), ("b1",)):
            verdict = silence_verdict(
                now=t(12, 0, 0),
                own_last_record=t(9, 0, 0),
                subagent_last_records=(t(11, 55, 0),),
                finishing=finishing,
                woken_at=None,
                limit_seconds=LIMIT,
                shells=shells,
            )
            assert verdict.action == "none"
            assert verdict.silent_seconds == 300.0


def test_e_silent_25_min_with_shells_is_none():
    verdict = silence_verdict(
        now=t(2, 25, 0),
        own_last_record=t(2, 0, 0),
        subagent_last_records=(),
        finishing=False,
        woken_at=None,
        limit_seconds=LIMIT,
        shells=("b1", "b2"),
    )
    assert verdict.action == "none"
    assert verdict.shells == ("b1", "b2")


def test_f_limit_seconds_comes_from_silent_orch_minutes():
    assert silent_orch_limit_seconds({}) == 1800.0
    assert silent_orch_limit_seconds({"silent_orch_minutes": 10}) == 600.0


def test_verdict_is_a_frozen_dataclass_with_the_three_fields():
    verdict = SilenceVerdict(action="none", silent_seconds=0.0, shells=())
    assert (verdict.action, verdict.silent_seconds, verdict.shells) == ("none", 0.0, ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.action = "end"


def test_a_session_that_answered_the_wake_and_went_silent_again_is_woken_again():
    verdict = silence_verdict(
        now=t(3, 30),
        own_last_record=t(2, 20),
        subagent_last_records=(),
        finishing=False,
        woken_at=t(2, 9, 3),
        limit_seconds=LIMIT,
    )
    assert verdict.action == "wake"
