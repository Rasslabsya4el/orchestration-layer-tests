"""#1037 protocol 1: the merge lock -> checkout lock order and the seat guard each have ONE definition in
`tools/agentops/merge.py` (`acquire_merge_line`, `merge_then_checkout_locked`, `seat_guard`). (a) a grep over
merge.py, core_update.py and line_check.py finds none of the old inline forms; (b) the function's own behaviour
is held on one example per former site, the order of acquire/release recorded with fake locks. No heavy tool."""
from __future__ import annotations

import re
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools.agentops import core_update, line_check, merge

AGENTOPS = Path(merge.__file__).parent


def _code_lines(name: str) -> list[str]:
    return [l for l in (AGENTOPS / name).read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")]


# -- (a) the old inline forms are gone -----------------------------------------------------------------

def test_a_no_private_guard_call_outside_merge_py():
    for name in ("core_update.py", "line_check.py"):
        assert [l for l in _code_lines(name) if re.search(r"\._guard\(", l)] == [], name


def test_a_no_hand_written_lock_pair_outside_the_one_function():
    for name in ("core_update.py", "line_check.py"):
        offenders = [l for l in _code_lines(name) if re.search(r"(?<![A-Za-z_])(MergeLock|CheckoutLock)\(", l)]
        assert offenders == [], name
    text = "\n".join(_code_lines("merge.py"))
    assert len(re.findall(r"=\s*CheckoutLock\(", text)) == 2         # _checkout_locked + merge_then_checkout_locked
    assert len(re.findall(r"=\s*MergeLock\(", text)) == 2            # acquire_merge_line + merge_then_checkout_locked
    assert len(re.findall(r"return MergeLock\(", text)) == 1         # seat_guard
    assert len(re.findall(r"checkout_lock\.release\(\)", text)) == 1  # one release order, in the one function


# -- (b) behaviour, recorded with fake locks -----------------------------------------------------------

@pytest.fixture
def events(monkeypatch):
    ev: list = []

    class FakeMerge:
        fail_on_acquire = False

        def __init__(self, path):
            self.path = Path(path)
            ev.append(("new", type(self).__name__, self.path.name))

        def acquire(self, max_age=None, *, ticket_info=None, waiter_hook=None):
            ev.append(("acquire", type(self).__name__, max_age, ticket_info, waiter_hook))
            if type(self).fail_on_acquire:
                raise RuntimeError("cannot take")
            return None

        def release(self):
            ev.append(("release", type(self).__name__))

        @contextmanager
        def _guard(self, deadline):
            ev.append(("guard-in", self.path.name, deadline))
            try:
                yield
            finally:
                ev.append(("guard-out", self.path.name))

    class FakeCheckout(FakeMerge):
        fail_on_acquire = False

    monkeypatch.setattr(merge, "MergeLock", FakeMerge)
    monkeypatch.setattr(merge, "CheckoutLock", FakeCheckout)
    return types.SimpleNamespace(log=ev, merge_cls=FakeMerge, checkout_cls=FakeCheckout)


def _kinds(env):
    return [e for e in env.log if e[0] in ("acquire", "release")]


def _stop_inside_the_lock(cwd):
    raise StopIteration("inside the lock")


def test_b_function_order_timeouts_and_release_reverse(events):
    ticket = {"kind": "commit", "message": "m", "issues": [], "needs_suite": True}
    with merge.merge_then_checkout_locked(Path("x/merge.lock"), 7.5, merge_ticket_info=ticket) as (lock, cwd_lock):
        assert type(lock).__name__ == "FakeMerge" and type(cwd_lock).__name__ == "FakeCheckout"
    assert [e for e in events.log if e[0] == "new"] == [("new", "FakeMerge", "merge.lock"),
                                                        ("new", "FakeCheckout", "merge.lock.cwd")]
    assert _kinds(events) == [("acquire", "FakeMerge", 7.5, ticket, None),
                              ("acquire", "FakeCheckout", 7.5, None, None),
                              ("release", "FakeCheckout"), ("release", "FakeMerge")]


def test_b_function_checkout_failure_releases_the_merge_lock_and_raises(events):
    events.checkout_cls.fail_on_acquire = True
    with pytest.raises(RuntimeError):
        with merge.merge_then_checkout_locked(Path("x/merge.lock"), None):
            raise AssertionError("body must not run")
    assert _kinds(events) == [("acquire", "FakeMerge", None, None, None),
                              ("acquire", "FakeCheckout", None, None, None), ("release", "FakeMerge")]


def test_b_function_body_error_still_releases_in_reverse(events):
    with pytest.raises(KeyError):
        with merge.merge_then_checkout_locked(Path("x/merge.lock"), 1.0):
            raise KeyError("body")
    assert [e[:2] for e in _kinds(events)][-2:] == [("release", "FakeCheckout"), ("release", "FakeMerge")]


def test_b_site_run_commit_suite_free_takes_only_the_checkout_lock(events, tmp_path, monkeypatch):
    monkeypatch.setattr(merge, "_core_commit_refusal", lambda cwd, paths: "")  # tmp_path is no git repo (#1116)
    monkeypatch.setattr(merge, "_head", _stop_inside_the_lock)
    with pytest.raises(StopIteration):
        merge.run_commit(["docs/a.md"], "m", cwd=tmp_path, lock_path=tmp_path / "merge.lock", lock_timeout=3.0)
    assert _kinds(events) == [("acquire", "FakeCheckout", 3.0, {"kind": "commit", "message": "m", "issues": [], "needs_suite": False}, None),
                              ("release", "FakeCheckout"), ("release", "FakeMerge")]


def test_b_site_run_commit_with_suite_holds_merge_then_checkout(events, tmp_path, monkeypatch):
    monkeypatch.setattr(merge, "resolve_suite_cmd", lambda c: "suite")
    monkeypatch.setattr(merge, "_prune_suite_outputs", lambda **k: None)
    monkeypatch.setattr(merge, "_core_commit_refusal", lambda cwd, paths: "")  # tmp_path is no git repo (#1116)
    monkeypatch.setattr(merge, "_head", _stop_inside_the_lock)
    with pytest.raises(StopIteration):
        merge.run_commit(["src/a.py"], "m", cwd=tmp_path, lock_path=tmp_path / "merge.lock", lock_timeout=3.0,
                         suite_env={}, suite_shared=(), suite_output_dir=tmp_path, merge_log_path=tmp_path / "log")
    assert _kinds(events) == [("acquire", "FakeMerge", 3.0, {"kind": "commit", "message": "m", "issues": [], "needs_suite": True}, None),
                              ("acquire", "FakeCheckout", 3.0, None, None),
                              ("release", "FakeCheckout"), ("release", "FakeMerge")]


def test_b_site_merge_line_passes_the_hook_and_closes_it(events):
    closed = []
    hook = types.SimpleNamespace(close=lambda: closed.append(1))
    ticket = {"kind": "merge"}
    lock, posted = merge.acquire_merge_line(Path("x/merge.lock"), 9.0, ticket, hook)
    assert posted is None and type(lock).__name__ == "FakeMerge" and closed == [1]
    assert _kinds(events) == [("acquire", "FakeMerge", 9.0, ticket, hook)]


def test_b_site_merge_line_without_hook_passes_none_and_closes_on_error(events):
    merge.acquire_merge_line(Path("x/merge.lock"), None, {"kind": "merge"})
    assert _kinds(events) == [("acquire", "FakeMerge", None, {"kind": "merge"}, None)]
    events.merge_cls.fail_on_acquire = True
    closed = []
    with pytest.raises(RuntimeError):
        merge.acquire_merge_line(Path("x/merge.lock"), None, {}, types.SimpleNamespace(close=lambda: closed.append(1)))
    assert closed == [1]


def test_b_site_run_merge_locked_goes_through_acquire_merge_line(monkeypatch, tmp_path):
    seen = {}

    def fake(lock_path, lock_timeout, ticket_info, hook):
        seen.update(lock_path=lock_path, timeout=lock_timeout, kind=ticket_info["kind"], hook=hook)
        raise StopIteration("taken")

    monkeypatch.setattr(merge, "acquire_merge_line", fake)
    monkeypatch.setattr(merge, "_make_line_check_hook", lambda *a, **k: "HOOK")
    with pytest.raises(StopIteration):
        merge._run_merge_locked("b", "suite", cwd=tmp_path, lock_path=tmp_path / "l", remote="origin", main="main",
                                lock_timeout=4.0, max_rebase_attempts=1, suite_env=None, suite_output_dir=tmp_path,
                                merge_log_path=tmp_path / "log")
    assert seen == {"lock_path": tmp_path / "l", "timeout": 4.0, "kind": "merge", "hook": "HOOK"}


def test_b_site_line_check_seat_goes_through_seat_guard(events, tmp_path):
    seat = tmp_path / "seat"
    obj = types.SimpleNamespace(_seat=seat, _dir=tmp_path, ticket="t", state="", _thread=None,
                                _front_clear=lambda: False, _seat_free=lambda: True, _write_status=lambda: None,
                                _prepare=lambda: None)
    line_check.LineCheck._try_take_seat(obj)
    assert obj.state == "waiting"
    line_check.LineCheck._free_seat(types.SimpleNamespace(_seat=seat, ticket="t", _thread=None))
    assert [g[:2] for g in events.log if g[0].startswith("guard")] == [
        ("guard-in", "seat"), ("guard-out", "seat"), ("guard-in", "seat"), ("guard-out", "seat")]
