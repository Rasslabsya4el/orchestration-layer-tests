"""#354 U3: no launch line of this repo asks for the auto permission mode."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_permission_mode_auto():
    from tools.agentops.guard import launch_hits

    hits = launch_hits(ROOT)
    assert not hits, "permission-mode auto still at:\n" + "\n".join(hits)
