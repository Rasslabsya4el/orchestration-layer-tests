"""Issue #376 unit 4: one C session, the re-run of a closed ticket in its sealed clone.

``run_c`` checks the clone's seal first (``c_seal.check_sealed`` on ``env_from_state``): a refused clone raises
``SealError`` and no process starts. Then it writes the trimmed system prompt (``trim_claude_md``) into the scratch of
``out_dir``, stubs the clone's CLAUDE.md (``neutralise_claude_md``) and starts one fresh headless ``claude -p`` through the launcher's hidden start (``launcher._launch``, a
whole-tree kill at the timeout), in the clone as cwd, with ``sealed_env``'s environment. After it ends it reads the
result json (``parse_result``), the peak context of its transcripts (``peak_context``) and the branch ``c-<N>``
(diff, commits), and writes ``out_dir/c<N>.json`` and ``out_dir/c<N>.diff``. Nothing here writes to GitHub or pushes.

    python -m tools.replays.c_run run --number N --clone DIR --state DIR --base SHA --forbid SHA ... --ticket FILE
        --cap-usd X --timeout-min M --out DIR [--claude-bin PATH]

The claude CLI flags used (claude 2.1.308, ``claude --help``): ``--system-prompt-file`` (the file's text replaces the
default system prompt), ``--max-budget-usd`` (the dollar cap, honoured with ``--print``), ``--output-format json``
(one result object printed at the end; a session killed at the timeout prints none, so its cost is unknown).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from tools.replays.arms import _git_bytes
from tools.replays.c_seal import DEFAULT_ISSUES, SealError, check_sealed, env_from_state, sealed_env
from tools.replays.launcher import EFFORT, HARNESS_ROOT, MODEL, Config, _json_lines, _launch, find_transcript

#: a CLAUDE.md line that imports a file of the repo (one level: the imported text is not expanded again).
IMPORT_LINE = re.compile(r"^@(docs/\S+)[ \t]*$", re.MULTILINE)
#: what the clone's own CLAUDE.md becomes before the launch: Claude Code reads it from the cwd, so the untrimmed
#: text must not reach the session (ORCH ruling on #376 unit 4); the trimmed text is the system prompt instead.
CLAUDE_STUB = "The instructions of this session are in its system prompt.\n"
#: the shim settings file ``sealed_env`` writes into the clone; kept out of every commit the session makes.
SETTINGS_EXCLUDE = ".claude/settings.local.json"
NO_COST_ON_TIMEOUT = "no cost on timeout; read the transcript"
#: the sections the C session does not get (named by their ``## `` heading).
REMOVED_SECTIONS = ("Roles (orchestration v2)", "Big efforts: Pocock's wayfinder", "Skills are hard steps")
APPENDED_LINE = ("Delegate the simple to light models, the hard to strong ones. Do not delegate the small. Work in this "
                 "clone; commit on a branch named `c-<N>`; run the tests you need; end with a short report.")
USAGE_INPUT_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
BUDGET_SUBTYPE = "error_max_budget_usd"


def trim_claude_md(repo: Path, number: int) -> str:
    """The repo's CLAUDE.md text with each ``@docs/...`` import line replaced by that file's content (one level), the
    sections REMOVED_SECTIONS cut (a section runs from its ``## `` heading to the next heading of the same or a higher
    level), and APPENDED_LINE appended with ``<N>`` = ``number``. Raises ValueError when a named section is missing or
    an import names a file that is not there."""
    repo = Path(repo)
    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    text = IMPORT_LINE.sub(lambda match: _imported(repo, match.group(1)), text)
    for name in REMOVED_SECTIONS:
        text = _without_section(text, name)
    return text.rstrip("\n") + "\n\n" + APPENDED_LINE.replace("<N>", str(number)) + "\n"


def neutralise_claude_md(clone: Path) -> None:
    """Stubs the clone's CLAUDE.md with CLAUDE_STUB (the session takes its instructions from the system prompt), marks
    the stub skip-worktree so no commit carries it, and appends SETTINGS_EXCLUDE to the clone's ``.git/info/exclude``
    so a later ``git add -A`` never commits the shim settings file."""
    clone = Path(clone)
    (clone / "CLAUDE.md").write_text(CLAUDE_STUB, encoding="utf-8")
    _git_bytes(clone, "update-index", "--skip-worktree", "CLAUDE.md")
    exclude = clone / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    exclude.write_text(text + SETTINGS_EXCLUDE + "\n", encoding="utf-8")


def _imported(repo: Path, rel: str) -> str:
    path = repo / rel
    if not path.is_file():
        raise ValueError(f"CLAUDE.md imports {rel}, which is not in {repo}")
    return path.read_text(encoding="utf-8").rstrip("\n")


def _heading(line: str) -> tuple[int, str] | None:
    """(level, text) of a markdown ATX heading line, None for any other line."""
    level = len(line) - len(line.lstrip("#"))
    if 1 <= level <= 6 and line[level:level + 1] == " ":
        return level, line[level + 1:].strip()
    return None


def _without_section(text: str, name: str) -> str:
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if _heading(line) == (2, name)), None)
    if start is None:
        raise ValueError(f"CLAUDE.md has no section {name!r}")
    end = next((j for j in range(start + 1, len(lines))
                if (_heading(lines[j]) or (99, ""))[0] <= 2), len(lines))
    del lines[start:end]
    return "\n".join(lines)


def build_command(claude_bin, system_prompt_file: Path, ticket_text: str, cap_usd: float) -> list[str]:
    """The argv of one fresh headless session: the ticket text as the one user message, the system prompt from a file,
    the model and effort of the C arm, the dollar cap, bypassPermissions, json out. No ``--agent``. A ``.py`` binary is
    a stand-in run through this python (the launcher's convention)."""
    bin_ = str(claude_bin)
    prefix = [sys.executable, bin_] if bin_.endswith(".py") else [bin_]
    return [*prefix, "-p", ticket_text, "--model", MODEL, "--effort", EFFORT, "--output-format", "json",
            "--permission-mode", "bypassPermissions", "--system-prompt-file", str(system_prompt_file),
            "--max-budget-usd", f"{cap_usd:g}"]


def parse_result(stdout: str) -> dict:
    """The fields of one session's json result (``--output-format json``). Raises ValueError when it is not json."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the session's stdout is not json ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError("the session's json is not an object")
    model_usage = data.get("modelUsage") or {}
    return {
        "session_id": data.get("session_id"),
        "cost_usd": float(data.get("total_cost_usd") or 0.0),
        "per_model": {model: float(usage.get("costUSD") or 0.0) for model, usage in model_usage.items()},
        "turns": data.get("num_turns"),
        "duration_min": float(data.get("duration_ms") or 0) / 60000,
        "is_error": bool(data.get("is_error")),
        "subtype": data.get("subtype"),
        "result": data.get("result") or "",
    }


def peak_context(transcript_paths: list[Path]) -> int:
    """The largest input + cache-creation + cache-read token count of one assistant message over the transcripts."""
    peak = 0
    for path in transcript_paths:
        for rec in _json_lines(path):
            usage = (rec.get("message") or {}).get("usage") if rec.get("type") == "assistant" else None
            if isinstance(usage, dict):
                peak = max(peak, sum(int(usage.get(field) or 0) for field in USAGE_INPUT_FIELDS))
    return peak


def _limit_usd_per_pct() -> float:
    config = json.loads((HARNESS_ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    return float(config["limit_usd_per_pct"])


def _transcripts(claude_bin, clone: Path, session_id: str | None, out_dir: Path) -> list[Path]:
    """The session's transcript and its subagents' (``<session>/subagents/agent-*.jsonl``), none when the session id is
    unknown or its file is not there."""
    if not session_id:
        return []
    cfg = Config(claude_bin=claude_bin, role_repo=clone, draft_dir=out_dir, runs_dir=out_dir)
    main = find_transcript(cfg, clone, session_id)
    if main is None:
        return []
    return [main, *sorted((main.parent / session_id / "subagents").glob("agent-*.jsonl"))]


def _git_text(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode("utf-8", "replace").strip()


def _branch_exists(clone: Path, branch: str) -> bool:
    try:
        _git_bytes(clone, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except RuntimeError:
        return False
    return True


def run_c(number: int, clone: Path, state: Path, base: str, forbid: list[str], ticket_text: str, cap_usd: float,
          timeout_min: float, out_dir: Path, claude_bin: Path | None = None,
          issues: tuple[int, ...] = DEFAULT_ISSUES) -> dict:
    """One C session on ticket ``number`` in its sealed ``clone``; returns the record that ``out_dir/c<N>.json`` holds.
    Raises SealError before any process starts when the clone is not sealed."""
    clone = Path(clone).absolute()
    state = Path(state).absolute()
    out_dir = Path(out_dir).absolute()
    check_sealed(clone, base, list(forbid), env_from_state(clone, state), tuple(issues))
    if claude_bin is None:
        from tools.agentops.dispatch import find_claude_exe
        claude_bin = find_claude_exe()
    env = sealed_env(clone, state)
    scratch = out_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    system_prompt = scratch / f"c{number}-system.md"
    system_prompt.write_text(trim_claude_md(clone, number), encoding="utf-8")
    neutralise_claude_md(clone)
    stdout_path = scratch / f"c{number}.stdout.json"
    stderr_path = scratch / f"c{number}.stderr.txt"
    cmd = build_command(claude_bin, system_prompt, ticket_text, cap_usd)
    started = time.time()
    rc, timed_out = _launch(cmd, clone, env, stdout_path, stderr_path, timeout_min * 60)
    ended = time.time()

    try:
        parsed = parse_result(stdout_path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        parsed = None
    session_id = parsed["session_id"] if parsed else None
    peak = peak_context(_transcripts(claude_bin, clone, session_id, out_dir))

    branch = f"c-{number}"
    has_branch = _branch_exists(clone, branch)
    commits = int(_git_text(clone, "rev-list", "--count", f"{base}..{branch}")) if has_branch else 0
    diff = _git_bytes(clone, "diff", f"{base}..{branch}") if has_branch else b""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"c{number}.diff").write_bytes(diff)

    breaks: list[str] = []
    if rc not in (None, 0):
        breaks.append(f"crash rc {rc}")
    if timed_out:
        breaks.append("timeout")
        breaks.append(NO_COST_ON_TIMEOUT)
    if parsed is None:
        breaks.append("no json result")
    elif parsed["is_error"]:
        breaks.append(f"is_error: {parsed['subtype']}")
    if not has_branch:
        breaks.append(f"no branch {branch}")
    if not (parsed and parsed["result"].strip()):
        breaks.append("no final report")
    cost = parsed["cost_usd"] if parsed and not timed_out else None
    record = {
        "number": number,
        "cost_usd": cost,
        "per_model": parsed["per_model"] if parsed else {},
        "cost_pct_of_week": cost / _limit_usd_per_pct() if cost is not None else None,
        "hours": (ended - started) / 3600,
        "turns": parsed["turns"] if parsed else None,
        "peak_context": peak,
        "session_id": session_id,
        "rc": rc,
        "stopped": timed_out or (parsed is not None and parsed["subtype"] == BUDGET_SUBTYPE),
        "breaks": breaks,
        "branch": has_branch,
        "commits": commits,
    }
    (out_dir / f"c{number}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.replays.c_run", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("--number", required=True, type=int)
    run_cmd.add_argument("--clone", required=True, type=Path)
    run_cmd.add_argument("--state", required=True, type=Path)
    run_cmd.add_argument("--base", required=True)
    run_cmd.add_argument("--forbid", action="append", default=[])
    run_cmd.add_argument("--ticket", required=True, type=Path, help="a file with the ticket's text (the user message)")
    run_cmd.add_argument("--cap-usd", required=True, type=float)
    run_cmd.add_argument("--timeout-min", required=True, type=float)
    run_cmd.add_argument("--out", required=True, type=Path)
    run_cmd.add_argument("--claude-bin", type=Path, default=None)
    args = parser.parse_args(argv)
    issues = tuple(dict.fromkeys((*DEFAULT_ISSUES, args.number)))
    try:
        record = run_c(args.number, args.clone, args.state, args.base, args.forbid,
                       args.ticket.read_text(encoding="utf-8"), args.cap_usd, args.timeout_min, args.out,
                       args.claude_bin, issues)
    except SealError as exc:
        print(f"refused: {exc}")
        return 1
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    print(f"c{args.number}: breaks {record['breaks'] or 'none'}, stopped {record['stopped']}, "
          f"{record['hours'] * 60:.1f} min, record in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
