"""Issue #232 unit 1 (D119): the test shard plugin and the GitHub workflow that runs the suite in parts.

The plugin keeps, of the collected items, the ones at positions ``p`` of the list sorted by nodeid with
``p % n == i - 1`` when env ``TEST_SHARD`` is ``i/n``. The workflow file is checked as text (and as YAML when PyYAML
is installed), and its text is scanned by the test copy's patterns: 0 hits.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import test_copy_check
from tools.agentops import test_shard_plugin as shard

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
ENV = "TEST_SHARD"


def fake_items(count: int = 10) -> list:
    """Items in a deliberately shuffled order, so the sort by nodeid is what the test relies on."""
    order = [7, 2, 9, 0, 5, 1, 8, 3, 6, 4][:count]
    return [SimpleNamespace(nodeid=f"tests/test_x.py::test_{k:02d}") for k in order]


class FakeConfig:
    """A config whose ``hook.pytest_deselected`` records the items it is handed."""

    def __init__(self) -> None:
        self.deselected: list = []
        self.hook = SimpleNamespace(pytest_deselected=lambda items: self.deselected.extend(items))


def run_shard(monkeypatch, value: str | None, items: list) -> tuple[list, FakeConfig]:
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    config = FakeConfig()
    shard.pytest_collection_modifyitems(config, items)
    return items, config


def ids(items: list) -> list[str]:
    return [item.nodeid for item in items]


def test_four_shards_partition_the_ten_ids(monkeypatch):
    everything = ids(fake_items())
    kept_by_part = []
    for i in range(1, 5):
        items, config = run_shard(monkeypatch, f"{i}/4", fake_items())
        kept = ids(items)
        dropped = [item.nodeid for item in config.deselected]
        assert sorted(kept + dropped) == sorted(everything)  # every item is either kept or reported
        kept_by_part.append(kept)

    union = [nodeid for part in kept_by_part for nodeid in part]
    assert sorted(union) == sorted(everything)  # the union is the whole list
    assert len(union) == len(set(union))  # pairwise disjoint
    sizes = [len(part) for part in kept_by_part]
    assert max(sizes) - min(sizes) <= 1
    assert sizes == [3, 3, 2, 2]


def test_shards_take_positions_of_the_sorted_list(monkeypatch):
    # sorted by nodeid the ten ids are test_00 .. test_09; part i keeps the positions p with p % 4 == i - 1
    expected = {
        1: ["tests/test_x.py::test_00", "tests/test_x.py::test_04", "tests/test_x.py::test_08"],
        2: ["tests/test_x.py::test_01", "tests/test_x.py::test_05", "tests/test_x.py::test_09"],
        3: ["tests/test_x.py::test_02", "tests/test_x.py::test_06"],
        4: ["tests/test_x.py::test_03", "tests/test_x.py::test_07"],
    }
    for i, want in expected.items():
        items, _ = run_shard(monkeypatch, f"{i}/4", fake_items())
        assert sorted(ids(items)) == sorted(want)


@pytest.mark.parametrize("value", [None, "1/1"])
def test_unset_or_one_of_one_keeps_everything(monkeypatch, value):
    before = fake_items()
    items, config = run_shard(monkeypatch, value, fake_items())
    assert ids(items) == ids(before)
    assert config.deselected == []


@pytest.mark.parametrize("value", ["", "0/5", "6/5", "2", "a/b", "1/0", "-1/5", "2/-3", "1/2/3", "1.5/5", "1/"])
def test_malformed_value_raises(monkeypatch, value):
    with pytest.raises(pytest.UsageError):
        run_shard(monkeypatch, value, fake_items())


def test_workflow_is_text_checked_for_its_keys():
    text = WORKFLOW.read_text(encoding="utf-8")
    for key in ("workflow_dispatch", "matrix", "ubuntu-latest", "TEST_SHARD"):
        assert key in text, key
    assert "python -m pytest tests -q -n auto -p tools.agentops.test_shard_plugin" in text
    assert "fromJSON(needs.plan.outputs.matrix)" in text


def test_workflow_parses_as_yaml_when_pyyaml_is_installed():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert data["name"] == "tests"
    on = data.get("on", data.get(True))  # PyYAML reads the bare key `on` as True
    assert set(on) == {"workflow_dispatch"}
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["parts"]["default"] == "5" and inputs["parts"]["type"] == "string"
    assert "ref" in inputs
    jobs = data["jobs"]
    assert set(jobs) == {"plan", "test"}
    assert jobs["test"]["needs"] == "plan"
    assert jobs["test"]["runs-on"] == "ubuntu-latest"
    assert jobs["test"]["timeout-minutes"] == 90
    strategy = jobs["test"]["strategy"]
    assert strategy["fail-fast"] is False
    assert strategy["matrix"]["part"] == "${{ fromJSON(needs.plan.outputs.matrix) }}"
    steps = jobs["test"]["steps"]
    assert steps[0]["uses"] == "actions/checkout@v4"
    assert steps[0]["with"]["ref"] == "${{ inputs.ref }}"
    assert steps[1]["uses"] == "actions/setup-python@v5"
    assert steps[1]["with"]["python-version"] == "3.12"
    assert steps[3]["env"]["TEST_SHARD"] == "${{ matrix.part }}/${{ inputs.parts }}"
    assert "-m \"not slow\"" in steps[3]["run"]


def test_workflow_text_has_no_test_copy_hits():
    hits = test_copy_check.scan(REPO, test_copy_check.load_patterns(REPO), [WORKFLOW.relative_to(REPO).as_posix()])
    assert hits == []
