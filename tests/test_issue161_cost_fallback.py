"""#161: the monitor's Cost tab computes `cut` in every repo, not only where settings name a `monitor_dashboard` module.

Without the module, `collect.slow()` takes the repo's own `scripts/token_meter.py` (through `monitor_agents._token_meter()`)
and fills `cost_day|cost_week|cost_month` with `cut` alone; a failure to load it sets `cost_<w>_error`, a failure to build
`cut` sets `cut_error`. With the module (Poe), the panel keys stay as before (`tests/test_issue33_cost_rows.py`).

No PoB, no network: the transcripts are written under tmp_path in the Claude projects layout and read by the real
`token_meter.scan_projects_dir`; the issue reads and the usage meter are stubbed; the prices are the repo's own
`config/model_prices.json`. The plugin half (the drawn Cost tab) is `mods/pob-slots/test/issue161-cost-error.test.tsx`.

    python -m pytest -q -p no:faulthandler tests/test_issue161_cost_fallback.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mods" / "pob-slots" / "bin"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(ROOT / "scripts"))

import collect  # noqa: E402
import token_meter  # noqa: E402
from tools.agentops import monitor_agents as ma  # noqa: E402

MODEL = "claude-sonnet-5-5"  # config/model_prices.json: input 2.0, cache_write 2.5, cache_read 0.1, output 10.0 per M
WINDOWS = ("cost_day", "cost_week", "cost_month")


def _midpoint_of_today() -> datetime:
    """A moment inside the last day's window (local midnight up to now), whatever the hour of the run."""
    since, until = collect.period_window(1, datetime.now().astimezone())
    return since + (until - since) / 2


def _assistant_line(ts: datetime, message_id: str, *, input_tokens: int = 0, cache_write: int = 0,
                    cache_read: int = 0, output: int = 0) -> str:
    """One assistant line of a main transcript, as Claude Code writes it (no `cache_creation` detail: the write is 5-minute)."""
    return json.dumps({
        "type": "assistant", "timestamp": ts.isoformat(),
        "message": {"id": message_id, "model": MODEL, "role": "assistant", "content": [],
                    "usage": {"input_tokens": input_tokens, "cache_creation_input_tokens": cache_write,
                              "cache_read_input_tokens": cache_read, "output_tokens": output}},
    })


def _write_fixture_projects(tmp_path: Path) -> Path:
    """tmp_path/projects/-fixture-project/<session>.jsonl: the project folder (DEFAULT_PROJECTS_DIR), two main transcripts,
    two priced assistant turns each, inside the last day.

    Expected spend, by hand from the Sonnet prices (per million tokens: input 2.0, cache write 2.5, cache read 0.1, output 10.0):
      session-a  turn 1: 1000 in + 200 out            = 1000*2.0/1e6 + 200*10.0/1e6   = 0.002 + 0.002 = 0.004
      session-a  turn 2: 10000 cache read + 500 out   = 10000*0.1/1e6 + 500*10.0/1e6  = 0.001 + 0.005 = 0.006
      session-b  turn 1: 2000 in + 100 out            = 2000*2.0/1e6 + 100*10.0/1e6   = 0.004 + 0.001 = 0.005
      session-b  turn 2: 4000 cache write + 300 out   = 4000*2.5/1e6 + 300*10.0/1e6   = 0.010 + 0.003 = 0.013
      total: 0.028 USD over 4 turns
    """
    ts = _midpoint_of_today()
    project = tmp_path / "projects" / "-fixture-project"
    project.mkdir(parents=True)
    (project / "session-a.jsonl").write_text("\n".join([
        _assistant_line(ts, "msg-a1", input_tokens=1000, output=200),
        _assistant_line(ts, "msg-a2", cache_read=10000, output=500),
    ]) + "\n", encoding="utf-8")
    (project / "session-b.jsonl").write_text("\n".join([
        _assistant_line(ts, "msg-b1", input_tokens=2000, output=100),
        _assistant_line(ts, "msg-b2", cache_write=4000, output=300),
    ]) + "\n", encoding="utf-8")
    return project


def _no_monitor(monkeypatch, tmp_path: Path, projects_dir: Path) -> None:
    """No `monitor_dashboard` in the settings; the core's token_meter reads `projects_dir` (the project folder)."""
    monkeypatch.setattr(collect, "_settings_module", lambda key: None)
    monkeypatch.setattr(collect, "_ISSUE_INFO_ONCE", lambda: {})
    monkeypatch.setattr(collect, "_closed_by_session", lambda: ({}, set()))
    monkeypatch.setattr(collect, "MONITOR_USAGE", tmp_path / "activity-usage.jsonl")
    monkeypatch.setattr(token_meter, "DEFAULT_PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(ma, "_token_meter", lambda: token_meter)


# row 1: settings without monitor_dashboard -> cut in every window, no panel part

def test_row1_cost_windows_carry_cut_without_monitor_dashboard(monkeypatch, tmp_path):
    _no_monitor(monkeypatch, tmp_path, _write_fixture_projects(tmp_path))
    out = collect.slow()
    for window in WINDOWS:
        assert "cut" in out[window], window
        assert "cut_error" not in out[window], window
    assert out["cost_day"]["cut"]["total"]["cost_usd"] == pytest.approx(0.028)
    assert out["cost_day"]["cut"]["total"]["turns"] == 4
    assert out["cost_week"]["cut"]["total"]["cost_usd"] == pytest.approx(0.028)
    assert out["cost_week"]["cut"]["total"]["turns"] == 4
    assert out["cost_month"]["cut"]["total"]["cost_usd"] == pytest.approx(0.028)
    assert out["cost_month"]["cut"]["total"]["turns"] == 4


def test_row1_panel_parts_stay_out_without_monitor_dashboard(monkeypatch, tmp_path):
    _no_monitor(monkeypatch, tmp_path, _write_fixture_projects(tmp_path))
    out = collect.slow()
    for window in WINDOWS:
        assert not {"by_issue", "by_day", "by_model"} & set(out[window]), window


# row 3: a failure to build cut, or to load token_meter, is an error key, never a silent empty tab

def test_row3_cut_failure_sets_cut_error(monkeypatch, tmp_path):
    _no_monitor(monkeypatch, tmp_path, tmp_path / "projects")

    def boom(_dir):
        raise RuntimeError("boom in scan")

    monkeypatch.setattr(token_meter, "scan_projects_dir", boom)
    out = collect.slow()
    for window in WINDOWS:
        assert "cut" not in out[window], window
        assert "boom in scan" in out[window]["cut_error"], window


def test_row3_token_meter_load_failure_sets_window_errors(monkeypatch, tmp_path):
    _no_monitor(monkeypatch, tmp_path, tmp_path / "projects")

    def no_meter():
        raise ImportError("no scripts/token_meter.py here")

    monkeypatch.setattr(ma, "_token_meter", no_meter)
    out = collect.slow()
    for window in WINDOWS:
        assert window not in out, window
        assert "no scripts/token_meter.py here" in out[f"{window}_error"], window
