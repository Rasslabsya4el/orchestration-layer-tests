"""#367 unit 3: the live-state guard in tests/conftest.py.

A test that is not marked `live` (or `slow`) fails when it reads this PC's live state: a file under the real repo's
`var/`, a file under the real `~/.claude`, or a `git`/`gh` run in the real repo root. The planted tests live in
`tests/_planted367_<case>.py` (not collected by the default name pattern); each run is one subprocess
`python -m pytest` from the repo root, so the repo's own conftest and its guard are in force. The file is deleted
after the run. `AGENTOPS_NO_RUNLOG=1` keeps the subprocess run out of the real run log.

A guard failure is an assertion in the autouse fixture's teardown, so a planted test that reads live state passes its
call and errors at teardown: the report section of each test is found by its name.
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
WORDS = "mark the test `live` or give it its own state"
HOME_SETTINGS = Path.home() / ".claude" / "settings.json"
TEARDOWN_HEADER = re.compile(r"_+ ERROR at teardown of (\w+) _+")


@contextlib.contextmanager
def _var_probe():
    """A file of this test's own under ROOT/var/agentops: written here (the guard allows writes), read by a planted
    test, deleted after. It exists on every machine, whatever var/ holds (GitHub's Linux machines have none)."""
    folder = ROOT / "var" / "agentops"
    created = not folder.is_dir()
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / "_planted367.txt"
    probe.write_text("probe\n", encoding="utf-8")
    try:
        yield probe
    finally:
        probe.unlink(missing_ok=True)
        if created:
            with contextlib.suppress(OSError):
                folder.rmdir()


def _home_settings() -> Path:
    """The real ~/.claude/settings.json. Not on every machine: where it is absent the case skips."""
    if not HOME_SETTINGS.is_file():
        pytest.skip(f"{HOME_SETTINGS} is not on this machine")
    return HOME_SETTINGS


def _run_planted(case: str, body: str) -> tuple[int, str]:
    """Write the planted tests, run them with the repo's conftest in force, delete the file; (exit code, output)."""
    path = TESTS / f"_planted367_{case}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "-v", "-p", "no:faulthandler", "-p", "no:cacheprovider"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
            env={**os.environ, "AGENTOPS_NO_RUNLOG": "1"},
        )
    finally:
        path.unlink(missing_ok=True)
    return proc.returncode, proc.stdout + proc.stderr


def _teardown_errors(out: str) -> dict[str, str]:
    """The report section of each test whose teardown errored, by test name (the guard's message is in it)."""
    parts = TEARDOWN_HEADER.split(out)  # [text, name, section, name, section, ...]
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_unmarked_var_read_and_git_at_the_root_fail_at_teardown():
    with _var_probe() as probe:
        rc, out = _run_planted("unmarked", f"""
            import subprocess

            def test_reads_var_file():
                with open({str(probe)!r}, "rb") as f:
                    f.read(1)

            def test_git_log_in_repo_root():
                subprocess.run(["git", "log", "-1"], cwd={str(ROOT)!r}, capture_output=True)

            def test_git_c_into_repo_root_from_tmp(tmp_path):
                subprocess.run(["git", "-C", {str(ROOT)!r}, "log", "-1"], cwd=tmp_path, capture_output=True)
        """)
    errors = _teardown_errors(out)
    assert rc == 1, out
    assert "3 passed, 3 errors" in out, out
    assert str(probe) in errors["test_reads_var_file"] and WORDS in errors["test_reads_var_file"], out
    assert "git log -1" in errors["test_git_log_in_repo_root"] and WORDS in errors["test_git_log_in_repo_root"], out
    assert f"git -C {ROOT} log -1" in errors["test_git_c_into_repo_root_from_tmp"], out


def test_planted_read_of_home_claude_settings_fails():
    target = _home_settings()  # skips where the file is absent
    rc, out = _run_planted("home", """
        from pathlib import Path

        def test_reads_home_settings():
            with open(Path.home() / ".claude" / "settings.json", "rb") as f:
                f.read(1)
    """)
    errors = _teardown_errors(out)
    assert rc == 1, out
    assert str(target) in errors["test_reads_home_settings"] and WORDS in errors["test_reads_home_settings"], out


def test_marked_live_reads_and_unmarked_tmp_cases_pass():
    with _var_probe() as probe:
        # the live home read needs the real settings file: it is left out where that file is absent
        home = HOME_SETTINGS.is_file()
        home_live = """
            @pytest.mark.live
            def test_live_reads_home():
                with open(Path.home() / ".claude" / "settings.json", "rb") as f:
                    f.read(1)
        """ if home else ""
        expected = 4 + (1 if home else 0)
        rc, out = _run_planted("pass", f"""
            import subprocess
            import pytest
            from pathlib import Path

            @pytest.mark.live
            def test_live_reads_var():
                with open({str(probe)!r}, "rb") as f:
                    f.read(1)

            @pytest.mark.live
            def test_live_git_log_in_repo_root():
                subprocess.run(["git", "log", "-1"], cwd={str(ROOT)!r}, capture_output=True)
        {home_live}
            def test_tmp_read_and_git_version(tmp_path):
                f = tmp_path / "a.txt"
                f.write_text("x", encoding="utf-8")
                assert f.read_text(encoding="utf-8") == "x"
                subprocess.run(["git", "--version"], cwd=tmp_path, capture_output=True, check=True)

            def test_git_c_into_tmp_from_repo_root(tmp_path):
                subprocess.run(["git", "-C", str(tmp_path), "log", "-1"], cwd={str(ROOT)!r}, capture_output=True)
        """)
    assert rc == 0, out
    assert f"{expected} passed" in out, out


def test_git_subcommands_that_read_no_live_state_pass_at_the_root():
    """#367 cause B: a temp origin made with `git init --bare` or `git clone` while the cwd is the repo root reads no
    state of the checkout, and so do the exempt subcommands, with the root as cwd or as the `-C` of a tmp cwd."""
    rc, out = _run_planted("exempt", f"""
        import subprocess

        import pytest

        ROOT = {str(ROOT)!r}
        EXEMPT = [
            (["git", "init", "--bare", "-b", "main", "@TMP@/origin.git"], "root"),
            (["git", "-c", "init.defaultBranch=main", "init", "--bare", "@TMP@/c.git"], "root"),
            (["git", "cat-file", "-t", "HEAD"], "root"),
            (["git", "ls-files"], "root"),
            (["git", "check-ignore", "-q", "nothing.txt"], "root"),
            (["git", "merge-base", "HEAD", "HEAD"], "root"),
            (["git", "rev-parse", "--is-shallow-repository"], "root"),
            (["git", "show", "e2ac3d8:docs/agents/brain.md"], "root"),
            (["git", "-C", ROOT, "show", "e2ac3d8:docs/agents/brain.md"], "tmp"),
            (["git", "diff", "--name-only", "--diff-filter=A", "e2ac3d8..HEAD"], "root"),
            (["git", "-C", ROOT, "ls-files"], "tmp"),
        ]

        @pytest.mark.parametrize("argv, where", EXEMPT)
        def test_exempt_subcommand_reads_no_live_state(argv, where, tmp_path):
            argv = [word.replace("@TMP@", str(tmp_path)) for word in argv]
            # the origin made by `init` must exist: a failed init is a failed test, not a silent pass
            subprocess.run(argv, cwd=ROOT if where == "root" else tmp_path, capture_output=True, timeout=60,
                           check="init" in argv)

        def test_clone_from_tmp_origin_with_root_as_cwd(tmp_path):
            origin = tmp_path / "origin.git"
            subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                           cwd=tmp_path, capture_output=True, check=True, timeout=60)
            subprocess.run(["git", "clone", str(origin), str(tmp_path / "clone")],
                           cwd=ROOT, capture_output=True, check=True, timeout=60)
    """)
    assert rc == 0, out
    assert "12 passed" in out, out


def test_git_that_reads_live_state_at_the_root_still_fails():
    """#367 cause B: only the exempt subcommands pass. `status`, `rev-parse HEAD`, a `-C` into the root from a tmp
    cwd, a value option that hides the subcommand (`--git-dir X status`, `--namespace clone status`) and a clone whose
    source is the root (`.`, the absolute root path, after a value option) are all flagged."""
    rc, out = _run_planted("flagged", f"""
        import subprocess

        ROOT = {str(ROOT)!r}

        def test_git_show_head_at_root():
            subprocess.run(["git", "show", "HEAD:README.md"], cwd=ROOT, capture_output=True)

        def test_git_show_branch_file_at_root():
            subprocess.run(["git", "show", "origin/main:docs/STATE.md"], cwd=ROOT, capture_output=True)

        def test_git_diff_working_tree_at_root():
            subprocess.run(["git", "diff", "--name-only"], cwd=ROOT, capture_output=True)

        def test_git_status_at_root():
            subprocess.run(["git", "status", "--short"], cwd=ROOT, capture_output=True)

        def test_git_rev_parse_head_at_root():
            subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True)

        def test_git_rev_parse_shallow_then_head_at_root():
            subprocess.run(["git", "rev-parse", "--is-shallow-repository", "HEAD"], cwd=ROOT, capture_output=True)

        def test_git_c_root_status_from_tmp(tmp_path):
            subprocess.run(["git", "-C", ROOT, "status", "--short"], cwd=tmp_path, capture_output=True)

        def test_git_git_dir_value_then_status_at_root():
            subprocess.run(["git", "--git-dir", "x.git", "status"], cwd=ROOT, capture_output=True)

        def test_git_namespace_value_named_clone_then_status_at_root():
            subprocess.run(["git", "--namespace", "clone", "status"], cwd=ROOT, capture_output=True)

        def test_clone_dot_source_at_root(tmp_path):
            subprocess.run(["git", "clone", ".", str(tmp_path / "c")], cwd=ROOT, capture_output=True)

        def test_clone_absolute_root_source_from_tmp(tmp_path):
            subprocess.run(["git", "clone", ROOT, str(tmp_path / "c")], cwd=tmp_path, capture_output=True)

        def test_clone_root_source_after_branch_option_at_root(tmp_path):
            subprocess.run(["git", "clone", "-b", "main", ".", str(tmp_path / "c")], cwd=ROOT, capture_output=True)
    """)
    errors = _teardown_errors(out)
    shown = {
        "test_git_show_head_at_root": "git show HEAD:README.md",
        "test_git_show_branch_file_at_root": "git show origin/main:docs/STATE.md",
        "test_git_diff_working_tree_at_root": "git diff --name-only",
        "test_git_status_at_root": "git status --short",
        "test_git_rev_parse_head_at_root": "git rev-parse HEAD",
        "test_git_rev_parse_shallow_then_head_at_root": "git rev-parse --is-shallow-repository HEAD",
        "test_git_c_root_status_from_tmp": f"git -C {ROOT} status --short",
        "test_git_git_dir_value_then_status_at_root": "git --git-dir x.git status",
        "test_git_namespace_value_named_clone_then_status_at_root": "git --namespace clone status",
        "test_clone_dot_source_at_root": "git clone . ",
        "test_clone_absolute_root_source_from_tmp": f"git clone {ROOT} ",
        "test_clone_root_source_after_branch_option_at_root": "git clone -b main . ",
    }
    assert rc == 1, out
    assert f"{len(shown)} passed, {len(shown)} errors" in out, out
    for name, command in shown.items():
        assert command in errors[name] and WORDS in errors[name], out


def test_live_and_slow_tests_get_the_real_half_integration_and_unmarked_get_the_wrapper():
    """#367 cause A: the fixture in tests/conftest.py wraps `merge.half_integration` for an unmarked test only; a test
    marked `live` or `slow` gets the real function (its name is the real one, the wrapper's is another)."""
    rc, out = _run_planted("real", """
        import pytest
        from tools.agentops import merge

        @pytest.mark.live
        def test_live_gets_the_real_function():
            assert merge.half_integration.__name__ == "half_integration"

        @pytest.mark.slow
        def test_slow_gets_the_real_function():
            assert merge.half_integration.__name__ == "half_integration"

        def test_unmarked_gets_the_wrapper():
            assert merge.half_integration.__name__ == "half_integration_unless_real_root"
    """)
    assert rc == 0, out
    assert "3 passed" in out, out


def test_half_integration_at_the_real_root_runs_no_git(monkeypatch):
    """#367 cause A: the dispatcher's end path calls `half_integration(<the real root>)`; the autouse fixture in
    tests/conftest.py answers None there without git. A recorder on `merge._git` shows that no git ran."""
    from tools.agentops import merge

    calls: list[tuple] = []

    def recording_git(cwd, *args, **kwargs):
        calls.append((cwd, args))
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(merge, "_git", recording_git)
    assert merge.half_integration(ROOT) is None
    assert calls == []


def test_half_integration_in_a_tmp_repo_still_reads_git(tmp_path):
    """#367 cause A: any other cwd calls the real `half_integration`: a fresh repo has no half state, and a
    `rebase-merge` dir in its git dir is read as a half rebase."""
    from tools.agentops import merge

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True, timeout=60)
    assert merge.half_integration(tmp_path) is None
    (tmp_path / ".git" / "rebase-merge").mkdir()
    assert merge.half_integration(tmp_path) == "rebase"
