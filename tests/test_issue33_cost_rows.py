"""#33 unit 1: the monitor's Cost builder returns rows from its own meter only.

The monitor's own calls (its activity meter beside collect.py) are the one mod row the builder may return. A usage
file another mod keeps in the user's home is never read: with it present, the row keys stay the same as without it.

No PoB, no network: the monitor's settings module is a stand-in with an empty transcript scan and a canned panel;
the issue reads are stubbed; every file lives in tmp_path, and the user's home is pointed at tmp_path.

    python -m pytest -q -p no:faulthandler tests/test_issue33_cost_rows.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mods" / "pob-slots" / "bin"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BIN))

import collect  # noqa: E402

OWN_KEY = "monitor"  # the monitor's own meter's row key (mod_calls(MONITOR_USAGE, "monitor"))
SECOND_MOD_DIR = ("mods", "second-mod")  # a second mod's folder under the user's home


def _usage_row(model: str = "claude-haiku-5-5") -> str:
    return json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "model": model, "input_tokens": 1000,
                       "output_tokens": 200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})


def _stand_in_monitor(tmp_path: Path) -> SimpleNamespace:
    """A `monitor_dashboard` stand-in: empty transcripts, empty prices, a canned panel per window."""
    token_meter = SimpleNamespace(
        DEFAULT_PRICES_PATH=tmp_path / "prices.json",
        DEFAULT_PROJECTS_DIR=tmp_path / "projects",
        scan_projects_dir=lambda _dir: SimpleNamespace(turns=[]),
        fetch_closed_issues=lambda *a, **k: [],
        load_prices=lambda _path: {},
        discover_main_transcripts=lambda _dir: [],
    )
    panel = {"by_model": [], "by_day": [], "by_issue": [], "by_session": []}
    return SimpleNamespace(token_meter=token_meter, panel_cost=lambda days: json.loads(json.dumps(panel)))


def _cost_rows(monkeypatch, tmp_path: Path, monitor_rows: list[str], second_mod_rows: list[str]) -> dict:
    """The row keys of the Cost builder's three windows: panel by_model and by_day, and the cut's by_mod."""
    home = tmp_path / "home"
    second = home.joinpath(".claude", *SECOND_MOD_DIR)
    second.mkdir(parents=True)
    (second / "usage.jsonl").write_text("".join(line + "\n" for line in second_mod_rows), encoding="utf-8")
    monitor_meter = tmp_path / "activity-usage.jsonl"
    monitor_meter.write_text("".join(line + "\n" for line in monitor_rows), encoding="utf-8")

    md = _stand_in_monitor(tmp_path)
    monkeypatch.setattr(collect.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(collect, "MONITOR_USAGE", monitor_meter)
    monkeypatch.setattr(collect, "_settings_module", lambda key: md if key == "monitor_dashboard" else None)
    monkeypatch.setattr(collect, "_ISSUE_INFO_ONCE", lambda: {})
    monkeypatch.setattr(collect, "_closed_by_session", lambda: ({}, set()))
    out = collect.slow()

    keys: dict[str, set] = {"by_model": set(), "by_day": set(), "by_mod": set()}
    for window in ("cost_day", "cost_week", "cost_month"):
        cost = out[window]
        keys["by_model"] |= {row["key"] for row in cost["by_model"]}
        keys["by_day"] |= {row["key"] for row in cost["by_day"]}
        keys["by_mod"] |= {row["key"] for row in cost["cut"]["by_mod"]}
    return keys


def test_second_mod_usage_file_adds_no_rows(monkeypatch, tmp_path):
    keys = _cost_rows(monkeypatch, tmp_path, monitor_rows=[], second_mod_rows=[_usage_row()])
    assert keys == {"by_model": set(), "by_day": set(), "by_mod": set()}


def test_no_usage_files_adds_no_rows(monkeypatch, tmp_path):
    keys = _cost_rows(monkeypatch, tmp_path, monitor_rows=[], second_mod_rows=[])
    assert keys == {"by_model": set(), "by_day": set(), "by_mod": set()}


def test_monitor_meter_is_the_only_mod_row(monkeypatch, tmp_path):
    keys = _cost_rows(monkeypatch, tmp_path, monitor_rows=[_usage_row()], second_mod_rows=[_usage_row()])
    assert keys["by_mod"] == {OWN_KEY}
    assert keys["by_model"] == set()
    assert keys["by_day"] == set()
