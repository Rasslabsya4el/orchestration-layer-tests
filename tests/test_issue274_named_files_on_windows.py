"""#274: the four test_issue219_* files the ticket names (their re-runs are ended through ``taskkill``, which Linux
lacks) are in tests/pc_only.txt, so the Windows job runs them."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMED_FILES = (
    "tests/test_issue219_failfast.py",
    "tests/test_issue219_failfast_callers.py",
    "tests/test_issue219_supersede.py",
    "tests/test_issue219_withdraw.py",
)


def _listed() -> list[str]:
    lines = [ln.strip() for ln in (ROOT / "tests" / "pc_only.txt").read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def test_the_four_named_files_are_listed_and_exist():
    listed = _listed()
    for path in NAMED_FILES:
        assert path in listed, path
        assert (ROOT / path).is_file(), path


def test_the_list_stays_sorted():
    listed = _listed()
    assert listed == sorted(listed)
