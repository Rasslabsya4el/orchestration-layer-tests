from pathlib import Path

ROOT = Path(__file__).parents[1]
A_FILE = ROOT / "scratch" / "434-s1" / "a.txt"


def test_part_a_file_exists_and_holds_a():
    assert A_FILE.exists(), f"missing {A_FILE}"
    assert A_FILE.read_text(encoding="utf-8").strip() == "A"
