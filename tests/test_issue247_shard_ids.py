"""Issue #247 unit 2: the shard plugin honours the ids file (TEST_IDS_FILE) and the PC-only mode (TEST_PC_ONLY).

Fake items in the pattern of tests/test_issue232_shard.py. ``TEST_IDS_FILE`` keeps only the items whose nodeid is a
non-empty line of the file, before the shard split; ``TEST_PC_ONLY=1`` keeps only the items of the files listed in
``tests/pc_only.txt`` and applies no sharding. Neither env set changes nothing; a set path that does not exist is a usage error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from types import SimpleNamespace

from tools.agentops import test_shard_plugin as shard

SHARD = "TEST_SHARD"
IDS = "TEST_IDS_FILE"
PC_ONLY = "TEST_PC_ONLY"
PLAIN = "tests/test_x.py"
WIN = "tests/test_win.py"


def fake_items(count: int = 10) -> list:
    """Ten plain items in a shuffled order, so the sort by nodeid is what the split relies on."""
    order = [7, 2, 9, 0, 5, 1, 8, 3, 6, 4][:count]
    return [SimpleNamespace(nodeid=f"{PLAIN}::test_{k:02d}") for k in order]


def pc_items() -> list:
    return [SimpleNamespace(nodeid=f"{WIN}::test_{name}") for name in ("a", "b", "c")]


def mixed_items() -> list:
    """The plain items first, then the PC-only ones: the incoming order the plugin must keep."""
    return fake_items() + pc_items()


def ids(items: list) -> list[str]:
    return [item.nodeid for item in items]


class FakeConfig:
    """A config with a rootpath (where tests/pc_only.txt is read) and a recording ``hook.pytest_deselected``."""

    def __init__(self, rootpath: Path) -> None:
        self.rootpath = rootpath
        self.deselected: list = []
        self.hook = SimpleNamespace(pytest_deselected=lambda items: self.deselected.extend(items))


def write_pc_only(root: Path) -> Path:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "pc_only.txt").write_text(f"{WIN}\n", encoding="utf-8")
    return root


def write_ids(root: Path, lines: list[str]) -> str:
    path = root / "ids.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def run(monkeypatch, env: dict, items: list, root: Path) -> tuple[list, FakeConfig]:
    for name in (SHARD, IDS, PC_ONLY):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config = FakeConfig(root)
    shard.pytest_collection_modifyitems(config, items)
    return items, config


def test_ids_file_keeps_only_the_listed_ids(tmp_path, monkeypatch):
    root = write_pc_only(tmp_path)
    listed = write_ids(root, [f"{PLAIN}::test_02", "", f"{PLAIN}::test_05", "   ", f"{PLAIN}::test_07\r"])
    items, config = run(monkeypatch, {IDS: listed}, fake_items(), root)
    # kept in the incoming order (7, 2, 9, 0, 5, ... -> 7, 2, 5); the other seven are reported as deselected
    assert ids(items) == [f"{PLAIN}::test_07", f"{PLAIN}::test_02", f"{PLAIN}::test_05"]
    assert sorted(ids(config.deselected)) == sorted(
        f"{PLAIN}::test_{k:02d}" for k in (0, 1, 3, 4, 6, 8, 9)
    )


def test_ids_file_then_shard_splits_only_the_listed_ids(tmp_path, monkeypatch):
    root = write_pc_only(tmp_path)
    listed = write_ids(root, [f"{PLAIN}::test_{k:02d}" for k in (1, 3, 6, 8)])
    # the four listed ids sorted are 01, 03, 06, 08: part 1/2 keeps positions 0 and 2 -> 01, 06
    # (a split over all ten would keep 06 and 08 instead, so this pins the order of the two steps)
    part1, config1 = run(monkeypatch, {SHARD: "1/2", IDS: listed}, fake_items(), root)
    assert sorted(ids(part1)) == sorted([f"{PLAIN}::test_01", f"{PLAIN}::test_06"])
    part2, config2 = run(monkeypatch, {SHARD: "2/2", IDS: listed}, fake_items(), root)
    assert sorted(ids(part2)) == sorted([f"{PLAIN}::test_03", f"{PLAIN}::test_08"])
    # the two parts are disjoint and together hold exactly the listed ids
    assert sorted(ids(part1) + ids(part2)) == sorted(f"{PLAIN}::test_{k:02d}" for k in (1, 3, 6, 8))


def test_ids_file_drops_a_listed_pc_only_id_under_a_shard(tmp_path, monkeypatch):
    # on GitHub's Linux parts the PC-only files stay out, even when the ids file names them
    root = write_pc_only(tmp_path)
    listed = write_ids(root, [f"{PLAIN}::test_00", f"{WIN}::test_a"])
    items, _ = run(monkeypatch, {SHARD: "1/1", IDS: listed}, mixed_items(), root)
    assert ids(items) == [f"{PLAIN}::test_00"]


def test_pc_only_keeps_only_the_pc_only_files_items(tmp_path, monkeypatch):
    root = write_pc_only(tmp_path)
    items, config = run(monkeypatch, {PC_ONLY: "1"}, mixed_items(), root)
    assert ids(items) == ids(pc_items())
    assert ids(config.deselected) == ids(fake_items())


def test_pc_only_applies_no_sharding(tmp_path, monkeypatch):
    # TEST_SHARD is set too, but in the PC-only mode both PC-only items stay in every part
    root = write_pc_only(tmp_path)
    for part in ("1/2", "2/2"):
        items, _ = run(monkeypatch, {PC_ONLY: "1", SHARD: part}, mixed_items(), root)
        assert ids(items) == ids(pc_items())


def test_pc_only_with_ids_file_keeps_the_listed_pc_only_items(tmp_path, monkeypatch):
    # the windows job: only the PC-only ids among the ids the merge sent
    root = write_pc_only(tmp_path)
    listed = write_ids(root, [f"{WIN}::test_b", f"{PLAIN}::test_00"])
    items, config = run(monkeypatch, {PC_ONLY: "1", IDS: listed}, mixed_items(), root)
    assert ids(items) == [f"{WIN}::test_b"]
    assert len(config.deselected) == len(mixed_items()) - 1


def test_pc_only_mode_does_not_ignore_the_pc_only_file_at_collection(tmp_path, monkeypatch):
    root = write_pc_only(tmp_path)
    path = root / "tests" / "test_win.py"
    config = SimpleNamespace(rootpath=root)
    monkeypatch.setenv(SHARD, "1/2")
    monkeypatch.setenv(PC_ONLY, "1")
    assert shard.pytest_ignore_collect(collection_path=path, config=config) is None
    monkeypatch.delenv(PC_ONLY)
    assert shard.pytest_ignore_collect(collection_path=path, config=config) is True


def test_neither_env_set_keeps_everything(tmp_path, monkeypatch):
    root = write_pc_only(tmp_path)
    before = mixed_items()
    items, config = run(monkeypatch, {}, mixed_items(), root)
    assert ids(items) == ids(before)  # the PC-only items too: no mode asks for them to go
    assert config.deselected == []


def test_missing_ids_file_path_is_an_error(tmp_path, monkeypatch):
    root = write_pc_only(tmp_path)
    missing = str(root / "no-such-ids.txt")
    with pytest.raises(pytest.UsageError, match="does not exist"):
        run(monkeypatch, {IDS: missing}, mixed_items(), root)
