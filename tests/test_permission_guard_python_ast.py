"""tools/hooks/permission_guard.py (#35): red line 1 through Python judges what the script CALLS, not what it
SAYS. A delete named inside a string literal, a comment or a docstring is text; a script that does not parse
is judged by the regex path of #835. No heavy tool, no network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.hooks import permission_guard as pg  # noqa: E402

MAIN = r"D:\Work\target-repo-v2"


def _d(cmd, cwd=MAIN, tool="Bash"):
    return pg.decide({"tool_name": tool, "tool_input": {"command": cmd}, "cwd": cwd})


def _py(body):
    return "python - <<'EOF'\n" + body + "\nEOF"


# the #18 call: a script that rewrites a source file; the new text (a string literal) holds two unlinks
ISSUE_18 = _py(
    'from pathlib import Path\n'
    'NEW = """\n'
    'def collect(tmp, attempt):\n'
    '    for n in range(3):\n'
    '        tmp.unlink(missing_ok=True)\n'
    '        attempt.unlink(missing_ok=True)\n'
    '"""\n'
    'for name in ["mods/heavy-tiles/bin/collect.py"]:\n'
    '    Path(name).write_text(NEW, encoding="utf-8")'
)

PASSED = [
    ISSUE_18,
    # a comment, a docstring, a log message
    _py("# os.remove(x)\nfor i in range(3):\n    print(i)"),
    _py('"""Never call shutil.rmtree( here."""\nprint(1)'),
    _py('for i in range(2):\n    print("cleanup: tmp.unlink() skipped")'),
    # a tree delete named in a string
    _py('msg = "shutil.rmtree(path) is forbidden"\nprint(msg)'),
]


@pytest.mark.parametrize("cmd", PASSED)
def test_delete_named_in_text_passes(cmd):
    assert _d(cmd) is None


def test_issue_18_replay_is_not_refused():
    r = _d(ISSUE_18)
    assert not r, r


def test_real_unlink_in_a_loop_is_refused_and_the_note_names_that_line():
    r = _d(_py('from pathlib import Path\nfor p in Path(d).iterdir():\n    p.unlink()'))
    assert r and r.startswith("permission guard, red line 1:"), r
    assert "[matched: p.unlink()]" in r, r


def test_literal_rmtree_outside_the_exempt_areas_is_refused():
    r = _d(_py('import shutil\nshutil.rmtree("C:/x")'))
    assert r and r.startswith("permission guard, red line 1:"), r
    assert "[matched: shutil.rmtree(\"C:/x\")]" in r, r


def test_a_real_delete_beside_a_docstring_is_still_refused():
    r = _d(_py('"""notes: rmtree( is text"""\nimport shutil\nshutil.rmtree(sys_dir)'))
    assert r and "[matched: shutil.rmtree(sys_dir)]" in r, r


def test_syntax_error_script_falls_back_to_the_regex_path():
    r = _d(_py("this is not python (\nimport shutil\nshutil.rmtree('src')"))
    assert r and r.startswith("permission guard, red line 1:"), r
    r = _d(_py("this is not python (\nprint('hello')"))
    assert r is None


def test_exec_of_a_string_stays_on_the_regex_path():
    r = _d(_py("exec(\"import shutil; shutil.rmtree('src')\")"))
    assert r and r.startswith("permission guard, red line 1:"), r


X = "import shutil; shutil.rmtree('C:/Users/user/x')"

# a delete the old guard refused that must stay refused: the code is built from a string
CODE_FROM_TEXT = [
    _py(f'import builtins\nbuiltins.exec("{X}")'),
    _py(f'import builtins\ngetattr(builtins, "exec")("{X}")'),
    _py(f'__builtins__.exec("{X}")'),
    _py(f'__builtins__["exec"]("{X}")'),
    _py(f'import builtins\nbuiltins.eval(builtins.compile("{X}", "x", "exec"))'),
    # a child process that runs the delete
    _py(f'import subprocess\nsubprocess.run(["python", "-c", "{X}"])'),
    _py(f'import os\nos.system(\'python -c "{X}"\')'),
    _py(f'import os\nos.popen(\'python -c "{X}"\')'),
    _py(f'import os\nos.execv("python", ["python", "-c", "{X}"])'),
    _py(f'import os\nos.spawnl(os.P_WAIT, "python", "python", "-c", "{X}")'),
    # a file delete in a loop through a deeper attribute chain
    _py('import shutil\nfor p in ["a"]:\n    shutil.os.remove(p)'),
    _py('import shutil\nfor p in ["a"]:\n    shutil.os.unlink(p)'),
]


@pytest.mark.parametrize("cmd", CODE_FROM_TEXT)
def test_delete_the_old_guard_refused_stays_refused(cmd):
    r = _d(cmd)
    assert r and r.startswith("permission guard, red line 1:"), r


def test_re_compile_and_a_plain_subprocess_with_a_delete_only_in_a_comment_pass():
    assert _d(_py('import re, subprocess\nr = re.compile("a+")\nsubprocess.run(["git", "status"])\n'
                  '# tmp.unlink() is not called\nfor i in range(2):\n    print(r)')) is None


def test_a_writing_heredoc_next_to_a_python_stdin_heredoc_is_text():
    cmd = ("cat > f.py <<'EOF'\nimport shutil\nshutil.rmtree('src')\nEOF\n"
           "python - <<'EOF'\nprint(1)\nEOF")
    assert _d(cmd) is None
