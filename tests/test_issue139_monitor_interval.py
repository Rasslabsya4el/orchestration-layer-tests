"""#139 unit 1: the monitor's refresh is a setting, and the expensive agents list is shared for longer than one pull.

`main()` gives the `fast` shared file the freshness `max(0.5, refresh_s - 1)` (default 4, as before); `agents()`
shares the raw live-session list through `shared_result("agents_raw", agents_every_s, ...)`, so two pulls inside
`agents_every_s` run `claude agents --json --all` once. A cache failure falls back to the direct call.

No PoB, no network: `live_sessions_raw` is a counting fake, the config and the cache live in tmp_path.

    python -m pytest -q -p no:faulthandler tests/test_issue139_monitor_interval.py
"""
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mods" / "pob-slots" / "bin"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BIN))

import collect  # noqa: E402
from tools.agentops import monitor_agents as ma  # noqa: E402


def _use_root(monkeypatch, tmp_path, monitor=None):
    """A project root in tmp_path: `config/orchestration.json` with the given `monitor` block (or none)."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    body = {"repo": "owner/name"}
    if monitor is not None:
        body["monitor"] = monitor
    (cfg / "orchestration.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(collect, "REPO", tmp_path)
    monkeypatch.setattr(collect, "CACHE_DIR", tmp_path / ".cache")


def _fast_freshness(monkeypatch, tmp_path, monitor=None):
    """The `fresh_for` that `main()` passes to `shared_result` for the `fast` mode."""
    _use_root(monkeypatch, tmp_path, monitor)
    seen = {}

    def fake_shared(mode, fresh_for, compute):
        seen[mode] = fresh_for
        return "{}"

    monkeypatch.setattr(collect, "shared_result", fake_shared)
    monkeypatch.setattr(sys, "argv", ["collect.py", "fast"])
    with pytest.raises(SystemExit):
        collect.main()
    return seen["fast"]


# ---- (a) refresh_s -> the fast freshness


def test_refresh_2_gives_fast_freshness_1(tmp_path, monkeypatch):
    assert _fast_freshness(monkeypatch, tmp_path, {"refresh_s": 2}) == 1.0


def test_no_refresh_key_gives_fast_freshness_4(tmp_path, monkeypatch):
    assert _fast_freshness(monkeypatch, tmp_path) == 4
    assert _fast_freshness(monkeypatch, tmp_path, {"source": "pob-slots"}) == 4


@pytest.mark.parametrize("bad", ["x", 0, -3])
def test_bad_refresh_value_falls_back_to_fast_freshness_4(tmp_path, monkeypatch, bad):
    assert _fast_freshness(monkeypatch, tmp_path, {"refresh_s": bad}) == 4


# ---- (b) agents(): the raw live-session list is listed once per agents_every_s window


def _stub_agents(monkeypatch, calls):
    """Everything `agents()` reads beside `live_sessions_raw` is a stub; `live_sessions_raw` counts its calls."""
    monkeypatch.setattr(ma, "_token_meter", lambda: SimpleNamespace(load_prices=lambda p: {}))
    monkeypatch.setattr(ma, "panel_agents", lambda *a, **k: {"sessions": []})
    monkeypatch.setattr(ma, "load_orch_state", lambda *a, **k: None)

    def live():
        calls.append(1)
        return [{"session_id": "abc", "kind": "interactive", "state": "busy", "cwd": str(ROOT)}]

    monkeypatch.setattr(ma, "live_sessions_raw", live)


def test_two_agents_calls_inside_the_window_list_sessions_once(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path)  # no agents_every_s: the default 20 s window
    calls: list = []
    _stub_agents(monkeypatch, calls)
    collect.agents()
    collect.agents()
    assert len(calls) == 1, f"two calls inside one window listed sessions {len(calls)} times"


def test_an_agents_call_after_the_window_lists_again(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path, {"agents_every_s": 0.05})  # a tiny window, read again at each call
    calls: list = []
    _stub_agents(monkeypatch, calls)
    collect.agents()
    assert len(calls) == 1
    time.sleep(0.2)
    collect.agents()
    assert len(calls) == 2, f"a call after the window listed sessions {len(calls)} times in all"


def test_a_failing_cache_path_falls_back_to_the_direct_call(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path)
    calls: list = []
    _stub_agents(monkeypatch, calls)

    def broken(*a, **k):
        raise OSError("cache broken")

    monkeypatch.setattr(collect, "shared_result", broken)
    out = collect.agents()
    assert isinstance(out, list)
    assert len(calls) == 1, "the fallback did not make the direct call"


def test_an_empty_list_is_read_again_directly_not_held_for_the_window(tmp_path, monkeypatch):
    """`live_sessions_raw` returns `[]` when `claude agents` fails: that must not blank the pane for 20 s."""
    _use_root(monkeypatch, tmp_path)
    calls: list = []
    _stub_agents(monkeypatch, calls)
    monkeypatch.setattr(ma, "live_sessions_raw", lambda: calls.append(1) or [])
    collect.agents()
    assert len(calls) == 2, f"an empty list was listed {len(calls)} times (cached read + one direct read expected)"


# ---- (c) four callers in one window compute once


def test_four_threads_inside_one_window_compute_once(tmp_path, monkeypatch):
    _use_root(monkeypatch, tmp_path)
    count: list = []
    lock = threading.Lock()

    def compute():
        with lock:
            count.append(1)
        time.sleep(0.3)  # long enough for the other three to arrive and wait
        return {"n": 1}

    texts: list = []
    out_lock = threading.Lock()

    def caller():
        text = collect.shared_result("fast", 4, compute)
        with out_lock:
            texts.append(text)

    threads = [threading.Thread(target=caller, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not any(t.is_alive() for t in threads), "a caller never returned"
    assert len(count) == 1, f"four callers inside one window computed {len(count)} times"
    assert len(texts) == 4 and len(set(texts)) == 1
