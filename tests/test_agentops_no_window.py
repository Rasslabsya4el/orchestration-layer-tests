"""#216: a console window opened for every `gh`/`claude` call the dispatcher started (b690167:
`DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` -- `CREATE_NO_WINDOW` is ignored
when combined with `DETACHED_PROCESS`, per Microsoft's documentation of the process creation
flags, so every console child got a new visible console of its own).

Row 3 (superseded by #248): `launch_detached_dispatcher` no longer starts the dispatcher through
`CreateProcess` flags at all -- it starts it through WMI `Win32_Process.Create`, the same
mechanism `scripts/launch_hidden.ps1` uses (see `dispatch.py`'s own docstring and
`tests/test_agentops_job_breakaway.py` for #248's measurement). The two tests below now check the
windowless `powershell`/WMI command that mechanism builds instead of the retired
`STARTUPINFO`/`creationflags` contract.

Row 5 (#246, widened from the original 4 modules to every module `tools/agentops` ships, issue
#246 acceptance row 2; widened again by #269 to the whole core manifest plus `tools/hooks/*.py`):
every literal `subprocess.run` / `subprocess.Popen` / `subprocess.call` / `subprocess.check_call` /
`subprocess.check_output` call in a walked file names a `creationflags=` keyword -- found here by
walking the AST of each module (a call added later with no flag is caught the same way). This does
not cover `launch_detached_dispatcher`'s own spawn (`spawn(command, **kwargs)`, an injected/aliased
callable, never a literal `subprocess.X(...)` call): that one is row 3's own two tests above.
`_ALLOWLIST` is the explicit, reasoned exception list #246 asked for: `orphans.py`'s three calls
build a `kwargs` dict and set `kwargs["creationflags"]` only when `sys.platform == "win32"` (still
correct: `no_window_flags()` is itself a no-op off Windows), so the flag is never a literal keyword
of the call node itself.

#269: the walked set is no longer `tools/agentops/*.py` alone -- it is every `*.py` entry of the
core manifest (`tools/agentops/core_manifest.txt`, read through `core_check.read_manifest`, the
one hand-written list) plus every file of `tools/hooks/*.py`, de-duplicated. Four calls found
missing the flag by the wider walk (`tools/hooks/autopilot_checkpoint.py:83`,
`tools/hooks/autopilot_commit_guard.py:69`, `tools/hooks/brain_reset_nudge.py:307`,
`scripts/token_meter.py:697`, a manifest entry outside `tools/agentops`) were fixed in the same
change, not allowlisted.

#246's own cause (row 1): the task scheduler's `pythonw.exe` start is not itself the bug -- the
`gh` calls in `cloud_lane.py`, `monitor_issues.py`, `provisional.py` and `port.py` that this test
now also covers ran with no `creationflags` at all before this fix, so *any* caller (console or
not) left their consoles to `CreateProcess`'s own default: allocate one and show it, because none
of the three ways to suppress it (`CREATE_NO_WINDOW`, an inherited invisible console, a hidden
`STARTUPINFO`) was ever requested. See Microsoft's own docs on process creation flags:
https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags
(`CREATE_NO_WINDOW`: "the process is a console application that is being run without a console
window... the console handle for the application is not set.").
"""

from __future__ import annotations

import ast
import base64
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops.core_check import read_manifest
from tools.agentops.dispatch import launch_detached_dispatcher

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only creationflags")

ROOT = Path(__file__).resolve().parents[1]


def _walked_modules(root: Path = ROOT) -> list[Path]:
    """Every `*.py` entry of the core manifest (the one hand-written list, `core_manifest.txt`)
    plus every file of `tools/hooks/*.py`, de-duplicated (#269). Manifest entries are literal
    repo-relative file paths (`core_check.run_check` joins them straight onto `root`, no glob
    expansion) -- a directory or glob entry would need handling here too, but none exists today."""
    manifest_paths = (root / entry for entry in read_manifest()
                      if entry.endswith(".py") and not entry.startswith("tests/"))
    hook_paths = (root / "tools" / "hooks").glob("*.py")
    seen: dict[Path, None] = {}
    for path in (*manifest_paths, *hook_paths):
        seen.setdefault(path.resolve(), None)
    return sorted(seen)


MODULES = _walked_modules()

_SUBPROCESS_CALL_NAMES = {"run", "Popen", "call", "check_call", "check_output"}

#: (relative path, line number) -> why this call is allowed to have no literal `creationflags=`
#: keyword. Every entry names a reason; a call not on this list and missing the flag fails the test.
_ALLOWLIST: dict[tuple[str, int], str] = {
    ("tools/agentops/orphans.py", 456): (
        "creationflags is set on a `kwargs` dict only `if sys.platform == \"win32\"` two lines "
        "above, then passed as `**kwargs` -- never a literal keyword on the call node itself, "
        "but present at runtime on every platform that needs it (`real_process_table`; "
        "re-pointed from 365 by #950 after #947 added 37 lines above)"
    ),
    ("tools/agentops/orphans.py", 561): (
        "same `kwargs`-dict pattern (`real_msys_table`) -- line moved by #258's "
        "`MsysTableUnreadable` handling, same call; re-pointed from 470 by #950 after #947"
    ),
    ("tools/agentops/orphans.py", 618): (
        "same `kwargs`-dict pattern as above (`taskkill_killer`) -- line moved by #258; "
        "re-pointed from 527 by #950 after #947"
    ),
}


# -- row 3 (#248): launch_detached_dispatcher's own WMI command -------------------------------

class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _decoded_script(command: list) -> str:
    encoded = command[command.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


def test_launch_detached_dispatcher_builds_a_windowless_wmi_command(tmp_path: Path):
    """#248: the dispatcher is started through WMI `Win32_Process.Create`
    (`Win32_ProcessStartup.ShowWindow = 0`), the same mechanism `scripts/launch_hidden.ps1`
    already uses -- never `CreateProcess`/`STARTUPINFO` flags (#216/#236, superseded). #257: the
    script goes as `-EncodedCommand`, no temp `.ps1` file, no `-File`."""
    calls: list[tuple[list, dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _FakeCompletedProcess("rc=0 pid=4242")

    out_path = tmp_path / "out.log"
    pid = launch_detached_dispatcher(cwd=tmp_path, out_path=out_path, popen=fake_run, env={})

    assert pid == 4242
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == "powershell"
    assert "-NonInteractive" in command
    assert "-EncodedCommand" in command
    assert "-File" not in command
    assert kwargs.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW, (
        "the transient powershell call itself must show no console window"
    )
    assert kwargs.get("timeout") == 60
    ps1_text = _decoded_script(command)
    assert "ShowWindow = [uint16]0" in ps1_text, "the WMI-started dispatcher must be windowless"
    assert "Win32_Process" in ps1_text and "Create" in ps1_text
    assert "cmd /c" in ps1_text, "WMI Create cannot redirect handles: needs a cmd /c wrapper"
    assert str(out_path) in ps1_text, "stdout/stderr must still be appended to out_path"
    assert "-m tools.agentops dispatch" in ps1_text


def test_launch_detached_dispatcher_raises_when_wmi_create_fails(tmp_path: Path):
    """A non-zero `Win32_Process.Create` `ReturnValue` (or no pid parsed at all) must raise, never
    silently return a bogus pid."""
    def fake_run(command, **kwargs):
        return _FakeCompletedProcess("rc=2 pid=0")

    with pytest.raises(OSError):
        launch_detached_dispatcher(cwd=tmp_path, out_path=tmp_path / "out.log", popen=fake_run,
                                   env={})


# -- row 5: every console-program subprocess call names a no-window flag ------------------------

def _iter_subprocess_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess_call = (
            isinstance(func, ast.Attribute)
            and func.attr in _SUBPROCESS_CALL_NAMES
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        if is_subprocess_call:
            yield node


def _missing_no_window_flags(
    modules: list[Path], root: Path = ROOT, allowlist: dict[tuple[str, int], str] = _ALLOWLIST,
) -> list[str]:
    """`file:line` of every `subprocess.*` call in `modules` with no literal `creationflags=`
    keyword, `allowlist` entries set aside. Refactored out of the test itself (#269) so a planted
    call in a throwaway copy can be checked the same way."""
    missing: list[str] = []
    for path in modules:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = str(path)
        for call in _iter_subprocess_calls(tree):
            has_flag = any(kw.arg == "creationflags" for kw in call.keywords)
            if has_flag:
                continue
            if (rel, call.lineno) in allowlist:
                continue
            missing.append(f"{rel}:{call.lineno}")
    return missing


def test_every_console_program_call_names_a_no_window_flag():
    """Row 5's bar (#246 acceptance row 2, widened by #269 to the core manifest's `*.py` entries
    plus `tools/hooks/*.py`): every `subprocess.*` call across the walked set lists 0 missing a
    no-window flag, once the reasoned `_ALLOWLIST` exceptions are set aside."""
    walked = [str(p.relative_to(ROOT)).replace("\\", "/") for p in MODULES]
    missing = _missing_no_window_flags(MODULES)
    assert missing == [], (
        f"subprocess calls with no no-window flag: {missing}\n"
        f"walked {len(walked)} files: {walked}"
    )


def test_every_allowlist_entry_still_exists_and_has_a_reason():
    """The allowlist rots silently otherwise: a line renumbered or removed by a later edit would
    just stop being checked. Every entry must still point at a real, flag-less subprocess call,
    and carry a non-empty reason."""
    calls_by_module: dict[str, set[int]] = {}
    for path in MODULES:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls_by_module[rel] = {
            call.lineno for call in _iter_subprocess_calls(tree)
            if not any(kw.arg == "creationflags" for kw in call.keywords)
        }
    for (rel, lineno), reason in _ALLOWLIST.items():
        assert reason.strip(), f"{rel}:{lineno} has an empty reason"
        assert lineno in calls_by_module.get(rel, set()), (
            f"{rel}:{lineno} is allowlisted but is no longer a flag-less subprocess call "
            f"(moved, fixed, or removed) -- drop the stale entry"
        )


def test_missing_no_window_flags_catches_a_planted_flagless_call(tmp_path: Path):
    """#269 acceptance row 3: a planted `subprocess.run` call with no `creationflags=` keyword, in
    a throwaway copy of a real hook file, must be caught by `_missing_no_window_flags` and named
    by `file:line` -- proving the checking function (not just today's fixed set of files) does the
    job, without touching the real hook file."""
    src = (ROOT / "tools" / "hooks" / "autopilot_checkpoint.py").read_text(encoding="utf-8")
    planted_line = 'subprocess.run(["gh", "issue", "view", "1"], capture_output=True)\n'
    src = src + "\n\n" + planted_line
    planted_lineno = len(src.splitlines())

    copy_dir = tmp_path / "tools" / "hooks"
    copy_dir.mkdir(parents=True)
    copy_path = copy_dir / "autopilot_checkpoint.py"
    copy_path.write_text(src, encoding="utf-8")

    missing = _missing_no_window_flags([copy_path], root=tmp_path)

    assert missing == [f"tools/hooks/autopilot_checkpoint.py:{planted_lineno}"]
