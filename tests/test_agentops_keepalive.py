"""tools/agentops/keepalive.py (issue #201): `keepalive install | remove | status`. No test calls
a real `schtasks`: a small stateful fake stands in for the host's task scheduler, keyed by task
name, so `install` then `status` then `remove` on the SAME fake round-trip like a real one would."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops.keepalive import (
    SCHEDULE_DESCRIPTION,
    SCHEDULE_DESCRIPTION_CLOCK_ONLY,
    KeepaliveError,
    _create,
    _looks_glued,
    _task_xml,
    _validate_split_action,
    install,
    remove,
    run_keepalive_command,
    status,
    task_name,
)


class FakeSchtasks:
    """Mimics enough of `schtasks` to prove install/status/remove round-trip: `/Create` registers
    the `/TN` name (and remembers its XML, issue #238: `/Query .../XML` hands it back so `status`
    can read the real trigger out of it), `/Query` succeeds only for a registered name, `/Delete`
    unregisters it."""

    def __init__(self) -> None:
        self.registered: set[str] = set()
        self.xml_by_name: dict[str, str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        verb = args[1]
        name = args[args.index("/TN") + 1]
        if verb == "/Create":
            self.registered.add(name)
            xml_path = Path(args[args.index("/XML") + 1])
            self.xml_by_name[name] = xml_path.read_text(encoding="utf-16")
            return subprocess.CompletedProcess(args, 0, stdout="SUCCESS", stderr="")
        if verb == "/Query":
            if name not in self.registered:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="ERROR: not found")
            stdout = self.xml_by_name[name] if "/XML" in args else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if verb == "/Delete":
            self.registered.discard(name)
            self.xml_by_name.pop(name, None)
            return subprocess.CompletedProcess(args, 0, stdout="SUCCESS", stderr="")
        raise AssertionError(f"unexpected schtasks verb: {verb}")


class FakeSchtasksRefusingLogon(FakeSchtasks):
    """Issue #230: a non-administrator's `schtasks` -- `/Create` answers "Access is denied" for a
    task whose XML holds a `LogonTrigger` (measured with two real, non-elevated probe calls, #230),
    and succeeds for one that does not (the clock trigger alone)."""

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        if args[1] == "/Create":
            xml_path = Path(args[args.index("/XML") + 1])
            xml = xml_path.read_text(encoding="utf-16")
            self.calls.append(args)
            if "<LogonTrigger>" in xml:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="ERROR: Access is denied.")
            name = args[args.index("/TN") + 1]
            self.registered.add(name)
            self.xml_by_name[name] = xml
            return subprocess.CompletedProcess(args, 0, stdout="SUCCESS", stderr="")
        return super().__call__(args)


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    """These are behaviour tests of the module's own logic, run on the CI/dev machine's own
    platform; force the Windows branch so the fake `schtasks` path is exercised even off Windows,
    and undo it after."""
    monkeypatch.setattr(sys, "platform", "win32")
    yield


def test_install_then_status_reports_one_task_every_5_min_and_at_logon(tmp_path: Path):
    fake = FakeSchtasks()
    root = tmp_path / "target-repo-v2"
    root.mkdir()

    ok, message = install(root=root, runner=fake)
    assert ok, message

    count, status_message = status(root=root, runner=fake)
    assert count == 1
    assert SCHEDULE_DESCRIPTION in status_message
    assert "every 5 min" in status_message
    assert "at logon" in status_message
    assert task_name(root) in fake.registered


def test_remove_leaves_zero(tmp_path: Path):
    fake = FakeSchtasks()
    root = tmp_path / "target-repo-v2"
    root.mkdir()
    install(root=root, runner=fake)
    assert status(root=root, runner=fake)[0] == 1

    ok, message = remove(root=root, runner=fake)
    assert ok, message

    count, _ = status(root=root, runner=fake)
    assert count == 0


def test_status_with_nothing_installed_is_zero(tmp_path: Path):
    fake = FakeSchtasks()
    root = tmp_path / "target-repo-v2"
    root.mkdir()
    count, message = status(root=root, runner=fake)
    assert count == 0
    assert message == "0 tasks"


def test_task_name_is_derived_from_the_repo_directory(tmp_path: Path):
    a = tmp_path / "target-repo-v2"
    b = tmp_path / "target-repo-v2-copy"
    a.mkdir()
    b.mkdir()
    assert task_name(a) != task_name(b)
    assert "target-repo-v2" in task_name(a)


def test_install_installs_the_xml_with_both_triggers(tmp_path: Path):
    fake = FakeSchtasks()
    root = tmp_path / "target-repo-v2"
    root.mkdir()
    install(root=root, runner=fake)

    create_call = next(c for c in fake.calls if c[1] == "/Create")
    xml_path = Path(create_call[create_call.index("/XML") + 1])
    # the module deletes its temp XML file after the call; read it before that by re-running with
    # a runner that captures the path before it is removed
    captured: dict[str, str] = {}

    def capturing_runner(args: list[str]) -> subprocess.CompletedProcess:
        if args[1] == "/Create":
            path = Path(args[args.index("/XML") + 1])
            captured["xml"] = path.read_text(encoding="utf-16")
        return fake(args)

    install(root=root, runner=capturing_runner)
    xml = captured["xml"]
    assert "<LogonTrigger>" in xml
    assert "PT5M" in xml


def test_install_falls_back_to_the_clock_trigger_when_logon_is_denied(tmp_path: Path):
    """Issue #230, acceptance rows 2, 3, 4, 5: a non-administrator's `schtasks` refuses the
    `LogonTrigger` with "Access is denied"; `install` still exits ok, names what it left out and
    why, `status` then reports the one task, and `remove` leaves zero."""
    fake = FakeSchtasksRefusingLogon()
    root = tmp_path / "target-repo-v2"
    root.mkdir()

    ok, message = install(root=root, runner=fake)
    assert ok, message
    assert SCHEDULE_DESCRIPTION_CLOCK_ONLY in message
    assert "left out" in message
    assert "logon" in message
    assert "Access is denied" in message
    # two /Create calls: the refused both-triggers attempt, then the accepted clock-only retry
    creates = [c for c in fake.calls if c[1] == "/Create"]
    assert len(creates) == 2

    count, status_message = status(root=root, runner=fake)
    assert count == 1, status_message
    # issue #238, row 4: a task installed without the logon trigger is named for the clock alone,
    # never the fixed "every 5 min and at logon" message a task that never got a LogonTrigger
    # never actually has.
    assert SCHEDULE_DESCRIPTION_CLOCK_ONLY in status_message
    assert "at logon" not in status_message

    ok, _ = remove(root=root, runner=fake)
    assert ok
    assert status(root=root, runner=fake)[0] == 0


def _task_xml_211d8ec(command: str, working_dir: str, *, include_logon: bool = True) -> str:
    """`_task_xml` as it stood at 211d8ec: one `command` string glued into a single `<Command>`
    element, no `<Arguments>` element at all -- the shape row 1 (#238) measured as never launching
    (`schtasks /Run` leaves the task `Running` forever, no process, next run refused)."""
    logon_trigger = """
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>""" if include_logon else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>{logon_trigger}
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Settings>
    <Hidden>true</Hidden>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def _assert_command_and_arguments_are_split(xml: str) -> None:
    """Issue #238, row 5: the executable alone in `<Command>`, its arguments in a separate
    `<Arguments>` element -- never one string glued together in `<Command>` (row 1's measured
    cause: that shape never launches on this host)."""
    assert "<Arguments>" in xml, "no <Arguments> element: command and arguments are glued together"
    command_start = xml.index("<Command>") + len("<Command>")
    command_end = xml.index("</Command>")
    command_text = xml[command_start:command_end]
    assert "-m" not in command_text and "dispatch" not in command_text, (
        f"<Command> holds more than the executable: {command_text!r}"
    )


def test_task_xml_splits_command_and_arguments(tmp_path: Path):
    """Issue #238, row 5. Passes against the fixed `_task_xml`."""
    xml = _task_xml('"C:\\pythonw.exe"', "-m tools.agentops dispatch --ensure", str(tmp_path))
    _assert_command_and_arguments_are_split(xml)


def test_task_xml_of_211d8ec_fails_the_split_command_assertion():
    """Issue #238, row 5: the same assertion, run against 211d8ec's `_task_xml` (main, before this
    fix) -- it fails, because that shape glues the executable and its arguments into one
    `<Command>` string with no `<Arguments>` element at all."""
    xml = _task_xml_211d8ec(
        '"C:\\pythonw.exe" -m tools.agentops dispatch --ensure', "C:\\repo",
    )
    with pytest.raises(AssertionError):
        _assert_command_and_arguments_are_split(xml)


def test_non_windows_refuses_with_a_message_and_nonzero(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    fake = FakeSchtasks()
    root = tmp_path / "target-repo-v2"
    root.mkdir()

    ok, message = install(root=root, runner=fake)
    assert not ok
    assert "Windows only" in message
    assert fake.calls == []  # schtasks is never called off Windows


def test_run_keepalive_command_install_status_remove(monkeypatch, tmp_path: Path):
    fake = FakeSchtasks()

    class Args:
        keepalive_command = "install"

    # patch the default runner so the CLI path (which uses the module default) hits the fake too
    monkeypatch.setattr("tools.agentops.keepalive._default_runner", fake)

    rc = run_keepalive_command(Args())
    assert rc == 0

    class StatusArgs:
        keepalive_command = "status"

    rc = run_keepalive_command(StatusArgs())
    assert rc == 0

    class RemoveArgs:
        keepalive_command = "remove"

    rc = run_keepalive_command(RemoveArgs())
    assert rc == 0


# -- #246 acceptance row 6: refuse a glued Command/Arguments split before schtasks is called ------

class _CountingSchtasks(FakeSchtasks):
    """Same as `FakeSchtasks`, but exposes a bare call count for the "0 calls" assertion."""

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        return super().__call__(args)


def test_looks_glued_true_for_a_command_holding_arguments_with_none_of_its_own():
    """Issue #238's dead shape: `Command` holds the executable and its arguments in one string,
    `Arguments` is empty."""
    assert _looks_glued('"C:\\Python\\pythonw.exe" -m tools.agentops dispatch --ensure', "")
    assert _looks_glued("C:\\Python\\pythonw.exe -m tools.agentops dispatch --ensure", "")


def test_looks_glued_false_for_a_properly_split_action():
    assert not _looks_glued('"C:\\Python\\pythonw.exe"', "-m tools.agentops dispatch --ensure")
    assert not _looks_glued("C:\\Python\\pythonw.exe", "-m tools.agentops dispatch --ensure")


def test_looks_glued_false_for_a_quoted_path_with_no_spaces_and_no_arguments():
    """A single executable path alone, no arguments at all, is not glued -- nothing to split."""
    assert not _looks_glued('"C:\\Python\\pythonw.exe"', "")


def test_validate_split_action_raises_before_any_schtasks_call():
    with pytest.raises(KeepaliveError, match="glued"):
        _validate_split_action("C:\\Python\\pythonw.exe -m tools.agentops dispatch --ensure", "")


def test_create_refuses_a_glued_action_with_zero_schtasks_calls(tmp_path: Path):
    """The bar: a fake `schtasks` runner sees 0 calls when the action is not split."""
    fake = _CountingSchtasks()
    with pytest.raises(KeepaliveError):
        _create(
            "agentops-dispatch-test",
            "C:\\Python\\pythonw.exe -m tools.agentops dispatch --ensure",
            "",
            str(tmp_path),
            fake,
            include_logon=True,
        )
    assert fake.calls == []


def test_install_never_hands_create_a_glued_action(tmp_path: Path):
    """`install`'s own real `exe`/`arguments` split (a quoted `pythonw.exe` path, arguments as a
    separate string) must pass `_validate_split_action` -- regression cover for `install` itself,
    on top of the direct `_create`/`_validate_split_action` tests above."""
    fake = FakeSchtasks()
    root = tmp_path / "target-repo-v2"
    root.mkdir()

    ok, message = install(root=root, runner=fake)

    assert ok, message
    assert any(call[1] == "/Create" for call in fake.calls)
