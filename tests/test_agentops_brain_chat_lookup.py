"""#69 unit 1: the brain chat lookup (exact, alias, folder), the home alert hold and the fallback log line. Fakes only."""

from __future__ import annotations

import subprocess

from tools.agentops.brain_chat_lookup import (
    HOME_ALERT_HOLD_SECONDS,
    BrainChatAway,
    FallbackLogger,
    home_alert_text,
    read_home_brain_title,
    resolve_brain_chat,
)

FOLDER_WIN = "C:\\Coding\\Hermes finances agent"
FOLDER = "C:/Coding/Hermes finances agent"
TITLE = "привет"
DEAD = ("stopped", "dead")


def interactive(name, cwd=FOLDER_WIN):
    return {"name": name, "kind": "interactive", "cwd": cwd, "sessionId": "s-" + name}


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_one_chat_in_the_folder_is_the_brain_chat_by_folder():
    got = resolve_brain_chat([interactive("Brain: Hermes")], TITLE, [], FOLDER, DEAD)
    assert (got.name, got.how, got.count) == ("Brain: Hermes", "folder", 1)


def test_two_interactive_chats_in_the_folder_are_ambiguous():
    rows = [interactive("Brain: Hermes"), interactive("Other chat")]
    got = resolve_brain_chat(rows, TITLE, [], FOLDER, DEAD)
    assert (got.name, got.how, got.count) == (None, "ambiguous", 2)


def test_exact_title_row_is_exact_and_the_logger_stays_silent():
    rows = [interactive(TITLE, cwd="C:\\Coding\\elsewhere"), interactive("Brain: Hermes")]
    got = resolve_brain_chat(rows, TITLE, [], FOLDER, DEAD)
    assert (got.name, got.how) == (TITLE, "exact")
    assert FallbackLogger().line(TITLE, got) is None


def test_interactive_row_in_another_repo_folder_never_counts():
    rows = [interactive("Brain: PoB", cwd="C:\\Coding\\PoB")]
    got = resolve_brain_chat(rows, TITLE, [], FOLDER, DEAD)
    assert (got.name, got.how, got.count) == (None, "none", 0)


def test_dead_state_row_in_the_folder_is_not_counted():
    row = dict(interactive("Brain: Hermes"), state="stopped")
    got = resolve_brain_chat([row], TITLE, [], FOLDER, DEAD)
    assert (got.name, got.how, got.count) == (None, "none", 0)


def test_non_interactive_row_in_the_folder_is_not_counted():
    row = {"name": "Brain: Hermes", "kind": "background", "cwd": FOLDER_WIN, "state": "running"}
    got = resolve_brain_chat([row], TITLE, [], FOLDER, DEAD)
    assert (got.name, got.how, got.count) == (None, "none", 0)


def test_subfolder_or_worktree_cwd_does_not_equal_the_folder():
    rows = [interactive("Brain: Hermes", cwd=FOLDER_WIN + "\\.claude\\worktrees\\wt-x")]
    got = resolve_brain_chat(rows, TITLE, [], FOLDER, DEAD)
    assert (got.name, got.how, got.count) == (None, "none", 0)


def test_folder_match_ignores_case_and_trailing_slash():
    rows = [interactive("Brain: Hermes", cwd="c:\\coding\\hermes finances agent\\")]
    got = resolve_brain_chat(rows, TITLE, [], FOLDER, DEAD)
    assert (got.name, got.how, got.count) == ("Brain: Hermes", "folder", 1)


def test_alias_beats_folder():
    rows = [interactive("Brain: Hermes"), interactive("Brain: Alias", cwd="C:\\Coding\\elsewhere")]
    got = resolve_brain_chat(rows, TITLE, ["Brain: Alias"], FOLDER, DEAD)
    assert (got.name, got.how) == ("Brain: Alias", "alias")


def test_exact_beats_alias():
    rows = [interactive(TITLE, cwd="C:\\Coding\\elsewhere"), interactive("Brain: Alias")]
    got = resolve_brain_chat(rows, TITLE, ["Brain: Alias"], FOLDER, DEAD)
    assert (got.name, got.how) == (TITLE, "exact")


def test_aliases_are_tried_in_listed_order():
    rows = [interactive("Second"), interactive("First")]
    got = resolve_brain_chat(rows, TITLE, ["First", "Second"], FOLDER, DEAD)
    assert (got.name, got.how) == ("First", "alias")


def test_away_is_held_from_first_not_found_and_alerts_once_per_batch():
    clock = Clock(0.0)
    away = BrainChatAway(clock)
    away.note(TITLE, False)
    assert away.held_since(TITLE) == 0.0
    clock.t = HOME_ALERT_HOLD_SECONDS - 1
    assert away.home_alert_due(TITLE) is False
    clock.t = HOME_ALERT_HOLD_SECONDS
    assert away.home_alert_due(TITLE) is True
    assert away.home_alert_due(TITLE) is False


def test_repeated_not_resolved_notes_keep_the_first_seen_time():
    clock = Clock(0.0)
    away = BrainChatAway(clock)
    away.note(TITLE, False)
    clock.t = 600.0
    away.note(TITLE, False)
    assert away.held_since(TITLE) == 0.0


def test_a_resolved_note_clears_the_batch_and_a_new_hold_alerts_again():
    clock = Clock(0.0)
    away = BrainChatAway(clock)
    away.note(TITLE, False)
    clock.t = HOME_ALERT_HOLD_SECONDS
    assert away.home_alert_due(TITLE) is True
    clock.t = HOME_ALERT_HOLD_SECONDS + 1
    away.note(TITLE, True)
    assert away.held_since(TITLE) is None
    clock.t = HOME_ALERT_HOLD_SECONDS + 2
    away.note(TITLE, False)
    clock.t = 2 * HOME_ALERT_HOLD_SECONDS + 1
    assert away.home_alert_due(TITLE) is False
    clock.t = 2 * HOME_ALERT_HOLD_SECONDS + 2
    assert away.home_alert_due(TITLE) is True


def test_fallback_line_is_logged_once_per_title_to_name_pair():
    logger = FallbackLogger()
    first = resolve_brain_chat([interactive("Brain: Hermes")], TITLE, [], FOLDER, DEAD)
    assert logger.line(TITLE, first) == (
        "brain chat: no chat named привет; using Brain: Hermes (the one chat in the repo folder)")
    assert logger.line(TITLE, first) is None
    renamed = resolve_brain_chat([interactive("Brain: Hermes 2")], TITLE, [], FOLDER, DEAD)
    assert logger.line(TITLE, renamed) == (
        "brain chat: no chat named привет; using Brain: Hermes 2 (the one chat in the repo folder)")


def test_fallback_line_is_logged_again_after_the_chat_was_found_by_name_in_between():
    logger = FallbackLogger()
    folder = resolve_brain_chat([interactive("Brain: Hermes")], TITLE, [], FOLDER, DEAD)
    exact = resolve_brain_chat([interactive(TITLE)], TITLE, [], FOLDER, DEAD)
    assert logger.line(TITLE, folder) is not None
    assert logger.line(TITLE, exact) is None
    assert logger.line(TITLE, folder) is not None


def test_home_alert_text_names_the_repo_title_count_held_and_since():
    assert home_alert_text("Hermes", TITLE, 2, 3, "18:07") == (
        "Hermes: brain chat привет not found (2 chats in the folder); 3 news held since 18:07")


def _proc(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=stdout, stderr=stderr)


def test_home_title_is_read_from_the_raw_config_through_gh():
    calls = []

    def runner(args):
        calls.append(args)
        return _proc(0, '{"brain_chat_title": "Brain: orch layer", "x": 1}')

    assert read_home_brain_title("Rasslabsya4el/orchestration-layer", runner) == "Brain: orch layer"
    assert calls == [["api", "repos/Rasslabsya4el/orchestration-layer/contents/config/orchestration.json",
                      "-H", "Accept: application/vnd.github.raw"]]


def test_home_title_is_none_when_gh_fails():
    assert read_home_brain_title("o/r", lambda args: _proc(1, "", "boom")) is None


def test_home_title_is_none_on_bad_json():
    assert read_home_brain_title("o/r", lambda args: _proc(0, "not json")) is None


def test_home_title_is_none_when_the_runner_raises():
    def runner(args):
        raise OSError("no gh")

    assert read_home_brain_title("o/r", runner) is None
