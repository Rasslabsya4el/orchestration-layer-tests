"""#137: a Stop-hook block asks for the correction only, not a second full reply."""

from tools.hooks.state_claim_check import REASON


def test_reason_asks_one_line_and_no_repeat():
    assert "one line" in REASON
    assert "do not repeat the reply" in REASON
