"""#454 unit 5 (rule 4): every call under tools/ that writes a commit is routed through a closing-keyword check.

The scan reads the AST of every Python file under tools/ (test directories and __pycache__ excluded) and looks at
Call nodes only. A call writes a commit when the string "commit" or "commit-tree" appears among its positional
arguments (inside list/tuple literals too, at any position) and the callee is a git runner (`subprocess.*`, or a
name containing `git`: `git`, `_git`, `_git_call`, `_run_git`, `m._git`), or the argv list also holds "git" or
"tools.agentops" / "agentops".

A call with agentops in its argv (`subprocess.run([sys.executable, "-m", "tools.agentops", "commit", ...])`) is the
agentops commit CLI, which checks its own message (rule 1 path): routed.

A runner call (`subprocess.run/Popen/call/check_output/check_call`, or `git`, `_git`, `_git_call`, `_run_git`,
`run_git`) whose argv cannot be read (a bare name, `*args`, an expression) is reported as
`file:line (argv not readable)` when its function, or the module if it has none, holds a list literal with "git" and
a commit word: the argv was built there as a literal and passed on through a variable (`cmd = [...]; run(cmd)`).
Any other runner call with an unreadable argv carries no commit word the AST can see and is not reported.

Any other commit call is routed when its enclosing function calls `require_clean` or `require_clean_built`
(any attribute path) on a line before the call's line. There is no allowlist: a finding the scan cannot route is
fixed in the code under tools/.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"

COMMIT_WORDS = {"commit", "commit-tree"}
AGENTOPS_WORDS = {"tools.agentops", "agentops"}
GIT_NAME = re.compile(r"(^|_)git(_|$)")
CHECK_NAMES = {"require_clean", "require_clean_built"}
UNREADABLE = " (argv not readable)"


def _literal_strings(node: ast.AST) -> set[str]:
    """Every string constant of `node`, descending into list and tuple literals only."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.List, ast.Tuple)):
        out: set[str] = set()
        for element in node.elts:
            out |= _literal_strings(element)
        return out
    return set()


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_subprocess_call(call: ast.Call) -> bool:
    func = call.func
    return isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "subprocess"


def _is_git_callee(call: ast.Call) -> bool:
    return _is_subprocess_call(call) or bool(GIT_NAME.search(_call_name(call)))


def _positional_strings(call: ast.Call) -> set[str]:
    out: set[str] = set()
    for arg in call.args:
        out |= _literal_strings(arg)
    return out


def _all_strings(call: ast.Call) -> set[str]:
    out = _positional_strings(call)
    for keyword in call.keywords:
        out |= _literal_strings(keyword.value)
    return out


def _commit_kind(call: ast.Call) -> str | None:
    """'cli' for the agentops commit CLI, 'commit' for a git runner that writes a commit, None for any other call."""
    if not _positional_strings(call) & COMMIT_WORDS:
        return None
    argv = _all_strings(call)
    if argv & AGENTOPS_WORDS:
        return "cli"
    if _is_git_callee(call) or "git" in argv:
        return "commit"
    return None


RUNNER_NAMES = {"run", "Popen", "call", "check_output", "check_call"}
GIT_HELPERS = {"git", "_git", "_git_call", "_run_git", "run_git"}


def _is_runner_call(call: ast.Call) -> bool:
    """A call that starts a process: subprocess's runners, or one of the git helpers that wrap them."""
    if _is_subprocess_call(call):
        return call.func.attr in RUNNER_NAMES
    return _call_name(call) in GIT_HELPERS


def _argv_unreadable(call: ast.Call) -> bool:
    """A runner call whose argv the AST cannot read: a bare name, `*args`, an expression."""
    if _is_subprocess_call(call):
        argv = call.args[0] if call.args else next((kw.value for kw in call.keywords if kw.arg == "args"), None)
        return not isinstance(argv, (ast.List, ast.Tuple))
    return not any(_literal_strings(arg) for arg in call.args)


def _git_list_with_commit(scope: ast.AST) -> bool:
    """True when `scope` holds a list or tuple literal with "git" and a commit word: an argv built there."""
    for node in ast.walk(scope):
        if isinstance(node, (ast.List, ast.Tuple)):
            words = _literal_strings(node)
            if "git" in words and words & COMMIT_WORDS:
                return True
    return False


def _first_check_before(func: ast.AST, line: int) -> bool:
    """True when `func` calls require_clean / require_clean_built on a line before `line`."""
    return any(isinstance(node, ast.Call) and _call_name(node) in CHECK_NAMES and node.lineno < line
               for node in ast.walk(func))


def _python_files(tools_dir: Path) -> list[Path]:
    files = []
    for path in sorted(tools_dir.rglob("*.py")):
        rel_parts = path.relative_to(tools_dir).parts
        if "__pycache__" in rel_parts or "tests" in rel_parts[:-1]:
            continue
        files.append(path)
    return files


def _calls_with_function(tree: ast.AST) -> list[tuple[ast.Call, ast.AST | None]]:
    """Every Call node with its innermost enclosing function (None at module level)."""
    out: list[tuple[ast.Call, ast.AST | None]] = []

    def visit(node: ast.AST, func: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.Call):
                out.append((child, func))
            visit(child, inner)

    visit(tree, None)
    return out


def _scan(tools_dir: Path) -> list[tuple[str, int, str, bool]]:
    """(file relative to tools_dir, line, 'cli' | 'commit' | 'unreadable', routed) for every finding."""
    found = []
    for path in _python_files(tools_dir):
        rel = path.relative_to(tools_dir).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call, func in _calls_with_function(tree):
            kind = _commit_kind(call)
            scope = func if func is not None else tree
            if kind is None and _is_runner_call(call) and _argv_unreadable(call) and _git_list_with_commit(scope):
                kind = "unreadable"
            if kind is None:
                continue
            routed = kind == "cli" or (func is not None and _first_check_before(func, call.lineno))
            found.append((rel, call.lineno, kind, routed))
    return sorted(found)


def found_commit_calls(tools_dir: Path) -> list[str]:
    """`file:line` of every commit-writing call under `tools_dir`, routed or not (the agentops CLI form included)."""
    return [f"{rel}:{line}" for rel, line, kind, _ in _scan(tools_dir) if kind in ("cli", "commit")]


def unrouted_commit_calls(tools_dir: Path) -> list[str]:
    """`file:line` of every call under `tools_dir` that writes a commit, or may, and is not routed."""
    hits = []
    for rel, line, kind, routed in _scan(tools_dir):
        if routed:
            continue
        hits.append(f"{rel}:{line}" + (UNREADABLE if kind == "unreadable" else ""))
    return hits


def _copy_tools(tmp_path: Path) -> Path:
    def ignore(directory: str, names: list[str]) -> list[str]:
        return [n for n in names if n == "__pycache__" or (os.path.isfile(os.path.join(directory, n))
                                                            and not n.endswith(".py"))]
    dst = tmp_path / "tools"
    shutil.copytree(TOOLS, dst, ignore=ignore)
    return dst


def test_every_commit_call_under_tools_is_routed():
    assert "agentops/target_add.py:137" in found_commit_calls(TOOLS), "the scan must see the agentops commit CLI call"
    assert unrouted_commit_calls(TOOLS) == []


PLANTED_CASES = [
    # (source, the line that holds the call, the suffix the report carries)
    (
        "import subprocess\n"
        "\n"
        "\n"
        "def planted_commit(msg):\n"
        "    subprocess.run([\"git\", \"commit\", \"-m\", msg])\n",
        "subprocess.run([\"git\", \"commit\", \"-m\", msg])",
        "",
    ),
    (
        "def planted_git(cwd, msg):\n"
        "    _git(cwd, \"-c\", \"k=v\", \"commit\", \"-m\", msg)\n",
        "_git(cwd, \"-c\", \"k=v\", \"commit\", \"-m\", msg)",
        "",
    ),
    (
        "import subprocess\n"
        "\n"
        "\n"
        "def planted_cmd(m):\n"
        "    cmd = [\"git\", \"commit\", \"-m\", m]\n"
        "    subprocess.run(cmd)\n",
        "subprocess.run(cmd)",
        UNREADABLE,
    ),
]


def test_planted_unrouted_commit_call_is_reported(tmp_path):
    for index, (source, needle, suffix) in enumerate(PLANTED_CASES):
        tools = _copy_tools(tmp_path / f"case{index}")
        name = f"planted_commit_454_{index}.py"
        (tools / "agentops" / name).write_text(source, encoding="utf-8")
        lines = [number for number, text in enumerate(source.splitlines(), start=1) if needle in text]
        assert len(lines) == 1, f"case {index}: the planted line must be unique"
        assert unrouted_commit_calls(tools) == [f"agentops/{name}:{lines[0]}{suffix}"], f"case {index}"


def test_check_moved_after_commit_in_commit_wip_is_reported(tmp_path):
    tools = _copy_tools(tmp_path)
    worktree = tools / "agentops" / "worktree.py"
    text = worktree.read_text(encoding="utf-8")
    check_block = (
        "        try:\n"
        "            require_clean_built(message, where=\"the WIP commit\")\n"
        "        except ClosingKeywordError as error:\n"
        "            return False, str(error)\n"
    )
    commit_line = '        committed = git("commit", "-m", message, cwd=worktree)\n'
    assert check_block in text and commit_line in text, "the real worktree.py changed: update this test"
    moved = text.replace(check_block, "", 1).replace(commit_line, commit_line + check_block, 1)
    worktree.write_text(moved, encoding="utf-8")
    line = moved.splitlines().index(commit_line.rstrip("\n")) + 1
    assert unrouted_commit_calls(tools) == [f"agentops/worktree.py:{line}"]
