"""Issue #247 unit 4: a run is green only when the ids sent were all collected.

The shard plugin records every collected node id in a ``tryfirst`` hook (before a ``-m "not slow"`` deselection) and, in
the ``trylast`` hook, raises ``pytest.UsageError`` (pytest exit 4, which the workflow's exit-5 pass cannot hide) when an id
the job was sent is not among them. Fake items in the pattern of tests/test_issue247_shard_ids.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import test_shard_plugin as shard

SHARD = "TEST_SHARD"
IDS = "TEST_IDS_FILE"
PC_ONLY = "TEST_PC_ONLY"
PLAIN = "tests/test_x.py"
WIN = "tests/test_win.py"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"


class FakeConfig:
    def __init__(self, rootpath: Path) -> None:
        self.rootpath = rootpath
        self.deselected: list = []
        self.hook = SimpleNamespace(pytest_deselected=lambda items: self.deselected.extend(items))


def plain(count: int) -> list:
    return [SimpleNamespace(nodeid=f"{PLAIN}::test_{k:03d}") for k in range(count)]


def win() -> list:
    return [SimpleNamespace(nodeid=f"{WIN}::test_{name}") for name in ("a", "b")]


def ids(items: list) -> list[str]:
    return [item.nodeid for item in items]


def setup(tmp_path: Path, listed: list[str]) -> Path:
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "pc_only.txt").write_text(f"{WIN}\n", encoding="utf-8")
    (tmp_path / "ids.txt").write_text("\n".join(listed) + "\n", encoding="utf-8")
    return tmp_path


def run(monkeypatch, tmp_path, env: dict, items: list, listed: list[str], slow: tuple[str, ...] = ()):
    """Both hooks in pytest's order: tryfirst records, a ``-m`` deselection removes ``slow``, trylast checks and filters."""
    root = setup(tmp_path, listed)
    for name in (SHARD, IDS, PC_ONLY):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(IDS, str(root / "ids.txt"))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config = FakeConfig(root)
    shard.pytest_record_collected_ids(config, items)
    items[:] = [item for item in items if item.nodeid not in slow]
    shard.pytest_collection_modifyitems(config, items)
    return items, config


def test_179_ids_none_collected_is_a_usage_error(tmp_path, monkeypatch):
    sent = [f"tests/test_renamed.py::test_{k:03d}" for k in range(179)]
    with pytest.raises(pytest.UsageError, match=r"179 of 179"):
        run(monkeypatch, tmp_path, {SHARD: "1/4"}, plain(40), sent)


def test_the_message_names_the_first_ten_missing_ids(tmp_path, monkeypatch):
    sent = [f"tests/test_renamed.py::test_{k:03d}" for k in range(179)]
    with pytest.raises(pytest.UsageError) as caught:
        run(monkeypatch, tmp_path, {SHARD: "1/4"}, plain(40), sent)
    message = str(caught.value)
    assert "ids sent but not collected" in message
    assert "tests/test_renamed.py::test_000" in message and "tests/test_renamed.py::test_009" in message
    assert "tests/test_renamed.py::test_010" not in message


def test_all_ids_collected_filters_normally(tmp_path, monkeypatch):
    items = plain(30)
    sent = ids(items)[:12]
    kept, config = run(monkeypatch, tmp_path, {SHARD: "1/1"}, items, sent)
    assert sorted(ids(kept)) == sorted(sent)
    assert len(config.deselected) == 18


def test_one_missing_id_among_many_is_a_usage_error(tmp_path, monkeypatch):
    items = plain(30)
    sent = ids(items)[:20] + [f"{PLAIN}::test_gone"]
    with pytest.raises(pytest.UsageError, match=r"1 of 21") as caught:
        run(monkeypatch, tmp_path, {SHARD: "1/1"}, items, sent)
    assert f"{PLAIN}::test_gone" in str(caught.value)


def test_an_id_deselected_as_slow_is_not_missing(tmp_path, monkeypatch):
    items = plain(10)
    sent = ids(items)[:5]
    kept, _ = run(monkeypatch, tmp_path, {SHARD: "1/1"}, items, sent, slow=(sent[2],))
    assert sorted(ids(kept)) == sorted(sent[:2] + sent[3:])


def test_pc_only_ids_are_not_required_on_a_linux_part(tmp_path, monkeypatch):
    items = plain(10)  # the PC-only file is not collected there: ignored before import
    sent = ids(items)[:3] + ids(win())
    kept, _ = run(monkeypatch, tmp_path, {SHARD: "1/1"}, items, sent)
    assert sorted(ids(kept)) == sorted(ids(items)[:3])


def test_pc_only_mode_requires_the_pc_only_ids(tmp_path, monkeypatch):
    items = plain(10) + win()
    sent = [f"{PLAIN}::test_000", f"{WIN}::test_a", f"{WIN}::test_b"]
    kept, _ = run(monkeypatch, tmp_path, {PC_ONLY: "1"}, items, sent)
    assert ids(kept) == ids(win())
    # a PC-only id the Windows checkout does not collect fails; a plain id missing there is not asked for
    with pytest.raises(pytest.UsageError, match=r"1 of 2"):
        run(monkeypatch, tmp_path, {PC_ONLY: "1"}, plain(10) + win()[:1], [f"{WIN}::test_a", f"{WIN}::test_b", f"{PLAIN}::test_nope"])


def test_pc_only_mode_does_not_ask_for_plain_ids(tmp_path, monkeypatch):
    items = win()
    kept, _ = run(monkeypatch, tmp_path, {PC_ONLY: "1"}, items, ids(win()) + [f"{PLAIN}::test_elsewhere"])
    assert ids(kept) == ids(win())


def test_an_empty_slice_with_ids_matched_globally_is_no_error(tmp_path, monkeypatch):
    items = plain(10)
    sent = ids(items)[:2]
    # three parts over two ids: part 3/3 holds nothing, which pytest reports as exit 5 and the workflow passes
    kept, _ = run(monkeypatch, tmp_path, {SHARD: "3/3"}, items, sent)
    assert kept == []


def test_no_ids_file_changes_nothing(tmp_path, monkeypatch):
    root = setup(tmp_path, [f"{PLAIN}::test_nope"])
    for name in (SHARD, IDS, PC_ONLY):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(SHARD, "1/1")
    items = plain(5)
    config = FakeConfig(root)
    shard.pytest_record_collected_ids(config, items)
    shard.pytest_collection_modifyitems(config, items)
    assert len(items) == 5


def test_the_exit_5_pass_in_the_workflow_stays_behind_the_ids_file():
    text = WORKFLOW.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if re.search(r"-eq 5\b", line)]
    assert len(lines) == 2  # the two pytest steps
    for line in lines:
        assert '[ -n "${TEST_IDS_FILE:-}" ]' in line


def test_an_ids_file_with_no_ids_is_an_error(tmp_path):
    blank = tmp_path / "ids.txt"
    blank.write_text("\n  \n", encoding="utf-8")
    with pytest.raises(pytest.UsageError, match="no ids"):
        shard.load_ids(str(blank))
