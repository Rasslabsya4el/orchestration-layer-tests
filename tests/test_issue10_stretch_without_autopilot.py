"""#10: a repo whose `local_modules` omit `.autopilot` has no autopilot stretch, so the core answers
"none" instead of calling the autopilot-only methods `.autopilot` registers on `Dispatcher`.

The methods are removed from the class for one test (monkeypatch restores them), which is what a
dispatcher built without the module looks like. Fake `gh` and fake `claude`, no network.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude
from tests.test_issue1142_orch_silent import GOAL, TICKET, _run, _tree
from tools.agentops.autopilot import DISPATCHER_METHODS
from tools.agentops.dispatch import Dispatcher


def _dispatcher_without_autopilot(monkeypatch, tmp_path: Path, claude: FakeClaude, gh) -> Dispatcher:
    # Building the dispatcher loads the registry once (the `.autopilot` methods land on the class);
    # only then are they taken away, so this test is the same process state as a repo without the module.
    dispatcher = Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                            state_file=None, log=lambda line: None)
    for name in DISPATCHER_METHODS:
        monkeypatch.delattr(Dispatcher, name, raising=False)
    return dispatcher


def test_stretch_open_is_false_without_the_autopilot_module(monkeypatch, tmp_path: Path):
    gh = _tree()
    dispatcher = _dispatcher_without_autopilot(monkeypatch, tmp_path, FakeClaude(), gh)

    assert dispatcher._autopilot_stretch_open(GOAL, gh.issues[GOAL]) is False


def test_live_for_is_none_without_the_autopilot_module(monkeypatch, tmp_path: Path):
    gh = _tree()
    claude = FakeClaude()
    dispatcher = _dispatcher_without_autopilot(monkeypatch, tmp_path, claude, gh)

    assert dispatcher._autopilot_live_for(TICKET) is None


def test_live_for_is_none_even_with_a_live_session_of_the_stretch_name(monkeypatch, tmp_path: Path):
    """A leftover `autopilot #GOAL` session cannot make a stretch exist without the module."""
    gh = _tree()
    claude = FakeClaude()
    claude.add_session("s1", name=f"autopilot #{GOAL}", state="working", status="busy")
    dispatcher = _dispatcher_without_autopilot(monkeypatch, tmp_path, claude, gh)

    assert dispatcher._autopilot_live_for(TICKET) is None


NOTE = "autopilot module not loaded\n"


def test_stretch_command_returns_rc_0_and_no_text_without_the_autopilot_module(monkeypatch, tmp_path: Path):
    gh = _tree()
    _dispatcher_without_autopilot(monkeypatch, tmp_path, FakeClaude(), gh)

    rc, text = _run(gh, tmp_path)

    assert rc == 0
    assert text == ""


def test_stretch_command_writes_the_note_to_stderr_and_nothing_to_stdout_without_the_module(
        monkeypatch, tmp_path: Path, capsys):
    """ORCH reads a printed name on stdout as 'do not report', so the note goes to stderr only."""
    gh = _tree()
    _dispatcher_without_autopilot(monkeypatch, tmp_path, FakeClaude(), gh)

    rc, _ = _run(gh, tmp_path)
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == ""
    assert captured.err == NOTE


def test_cli_stretch_command_is_silent_on_stdout_and_notes_stderr_without_the_module(
        monkeypatch, tmp_path: Path, capsys):
    """The CLI path end to end: `cli.main(["autopilot-stretch", ...])` with fake gh and claude."""
    from tools.agentops import cli, dispatch

    gh = _tree()
    _dispatcher_without_autopilot(monkeypatch, tmp_path, FakeClaude(), gh)
    monkeypatch.setattr(dispatch, "GhCli", lambda: gh)
    monkeypatch.setattr(dispatch, "ClaudeCli", lambda: FakeClaude())

    rc = cli.main(["autopilot-stretch", "--issue", str(TICKET)])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == ""
    assert captured.err == NOTE


def test_loaded_module_stretch_command_prints_the_name_and_no_note(tmp_path: Path, capsys):
    """With `.autopilot` loaded the answer is unchanged: the name, rc 0, and no note on stderr."""
    gh = _tree()

    rc, text = _run(gh, tmp_path)
    captured = capsys.readouterr()

    assert rc == 0
    assert text == f"autopilot #{GOAL}"
    assert captured.err == ""
