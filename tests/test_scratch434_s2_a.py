from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_scratch_434_s2_a_file_says_a():
    target = ROOT / "scratch" / "434-s2" / "a.txt"
    assert target.exists(), f"missing {target}"
    assert target.read_text(encoding="utf-8").strip() == "A"
