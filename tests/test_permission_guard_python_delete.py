"""tools/hooks/permission_guard.py (#835): a recursive delete refused in shell form stays refused through a
Python call (`python -c`, heredoc script); a Python call that deletes nothing recursive stays allowed.
No heavy tool, no network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.hooks import permission_guard as pg  # noqa: E402

MAIN = r"D:\Work\target-repo-v2"
WT = MAIN + r"\.claude\worktrees\wt-835"


def _d(cmd, cwd=MAIN, tool="Bash"):
    return pg.decide({"tool_name": tool, "tool_input": {"command": cmd}, "cwd": cwd})


REFUSED = [
    # the transcript's form (#797)
    """python -c "import shutil;shutil.rmtree('.claude/rig797-1164')\"""",
    """python3 -c "import shutil; shutil.rmtree('src/pkg')\"""",
    """py -c "import shutil; shutil.rmtree(r'D:\\Work\\target-repo-v2\\var')\"""",
    # variable path, os.walk + chmod + rmtree (the second transcript form)
    """python -c "import os,shutil,stat
for r,d,f in os.walk('.claude/rig797-1'):
    os.chmod(r, stat.S_IWRITE)
shutil.rmtree(r)\"""",
    """python -c "import shutil,sys; shutil.rmtree(sys.argv[1])" scratch7""",
    """python -c "import os; os.rmdir('artifacts/x')\"""",
    """python -c "import os; os.removedirs('artifacts/x/y')\"""",
    """python -c "import pathlib; pathlib.Path('var/x').rmdir()\"""",
    """python -c "import pathlib
for p in pathlib.Path('src').rglob('*'): p.unlink()\"""",
    """python -c "import os
for r,d,f in os.walk('src'):
    for n in f: os.remove(os.path.join(r,n))\"""",
    "python - <<'EOF'\nimport shutil\nshutil.rmtree('.claude/rig797-17936')\nEOF",
    "python - <<'EOF'\nimport os\nfor r,d,f in os.walk('artifacts'):\n    [os.unlink(os.path.join(r,x)) for x in f]\nEOF",
    "cd D:/Work/target-repo-v2 && python -c \"import shutil; shutil.rmtree('vendor')\"",
    # #1131: a file delete in a loop through a path object / os / a bare imported name still refuses
    "python - <<'EOF'\nfrom pathlib import Path\nfor f in Path('src').rglob('*'): f.unlink()\nEOF",
    'python -c "from os import remove\nfor n in [\'a\']: remove(n)"',
    'python -c "import os\nfor n in [\'a\']: os . remove(n)"',
]

ALLOWED = [
    'python -c "print(1)"',
    "python scripts/benchmark.py --workers 2",
    "python -m pytest tests/test_permission_guard.py -q",
    'python -c "import os; os.remove(\'tmp_scratch.txt\')"',
    'python -c "import pathlib; pathlib.Path(\'notes.txt\').unlink()"',
    "python - <<'EOF'\nprint('hello')\nEOF",
    # the same exempt paths the shell form lets through
    'python -c "import shutil; shutil.rmtree(\'tmp_rig\')"',
    'python -c "import shutil; shutil.rmtree(\'src/__pycache__\')"',
    # #1131: `remove(` called on a Lua table / a list / a set inside a loop deletes no file
    "python - <<'PYEOF'\nlua = 'for j = 1, #drop do if mod_equal(d, m) then table.remove(drop, j) skip = true break end end'\nPYEOF",
    'python -c "xs = [1, 2]\nfor x in list(xs): xs.remove(x)"',
    'python -c "s = {1}\nfor x in range(3): s.remove(1)"',
    # `rm -rf` of a heredoc body for a non-python command is still not a command
    "cat <<'EOF'\nshutil.rmtree('src')\nEOF",
]


@pytest.mark.parametrize("cmd", REFUSED)
def test_python_delete_refused(cmd):
    r = _d(cmd)
    assert r and r.startswith("permission guard, red line 1:"), r


@pytest.mark.parametrize("cmd", ALLOWED)
def test_python_nondestructive_allowed(cmd):
    assert _d(cmd) is None


WRITE_THEN_RUN = (
    "cat > scripts/pane_dump.py <<'EOF'\nimport shutil\nprint(1)\nshutil.rmtree('.claude/tmp843')\nEOF\n"
    "mkdir -p .claude/tmp843 && time python scripts/pane_dump.py --ref HEAD --out .claude/tmp843/head.json; "
    "ls -la .claude/tmp843"
)


def test_heredoc_that_only_writes_a_script_does_not_refuse_a_later_python_run():
    assert _d(WRITE_THEN_RUN) is None
    assert _d(WRITE_THEN_RUN.replace("time python scripts/pane_dump.py", "python -m pytest -q")) is None


def test_python_stdin_heredoc_after_a_writing_heredoc_stays_refused():
    cmd = "cat > f.py <<'EOF'\nx = 1\nEOF\npython - <<'EOF'\nimport shutil\nshutil.rmtree('.claude/tmp843')\nEOF"
    assert _d(cmd).startswith("permission guard, red line 1:")


def test_reason_names_the_matched_rmtree_text():
    r = _d("python - <<'EOF'\nimport shutil\nshutil.rmtree('.claude/tmp843')\nEOF")
    assert "[matched: shutil.rmtree('.claude/tmp843')]" in r, r
    r = _d("""python -c "import shutil; shutil.rmtree('src/pkg')\"""")
    assert "rmtree('src/pkg')" in r.split("[matched:")[1], r


def test_row4_forms_for_pane_dump():
    assert _d("python scripts/pane_dump.py --out tmp_843/head.json", cwd=WT) is None
    assert _d("python -c \"import shutil; shutil.rmtree('tmp_843')\"") is None
    assert _d("python -c \"import shutil; shutil.rmtree('.claude/tmp843')\"") is not None


def test_own_worktree_path_same_as_shell_form():
    sh = _d("rm -rf src/pkg", cwd=WT)
    py = _d("python -c \"import shutil; shutil.rmtree('src/pkg')\"", cwd=WT)
    assert (sh is None) == (py is None)


def test_powershell_remove_item_recurse_refused_today():
    assert _d(r"Remove-Item -Recurse -Force .claude\rig797-1", tool="PowerShell").startswith(
        "permission guard, red line 1")
