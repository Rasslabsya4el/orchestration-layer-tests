"""Issue #349 unit 5: the launcher of the brain-role replays.

``run_one`` makes one replay: a run tree (``arms.make_run_tree``) moved to an opaque name, a per-run state dir with a
``gh`` shim that calls the recording stub (``gh_stub``), one headless ``claude -p`` process with the case's owner
message, a kill of the whole process tree at the timeout, the answer parsed out of the stream-json file, and the
leak, model and effort checks of the session transcript. #363: when the final text block of that first turn holds a
``?`` and the case has a ``turn2_answer``, a second process (``claude --resume <id> -p <answer>``, no ``--agent``)
runs in the same tree and environment; the record then carries ``turns`` 2, ``text2`` and both turns' costs, and the
leak scan covers the second turn's transcript too. ``run_all`` runs the slots (case x arm x
per_arm) in a thread pool, re-runs a void or timed-out run in a fresh ``n``, and stops at the budget. ``finish`` turns
the kept records into the blind case files. ``issue_counts`` reads the highest issue number of repos through the real
gh.

Blindness: every path the brain can see (its cwd, the shim dir on PATH, ``REPLAY_STATE_DIR``) is named by an opaque run
id (``r`` + 8 hex); only the record file and ``progress.json`` in ``runs_dir`` carry case and arm.

``python -m tools.replays.launcher counts|run|finish`` is the command line. Nothing here writes to GitHub, and no
tree or worktree is ever removed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tools.replays.arms import AGENT_ARMS, ARMS, make_run_tree
from tools.replays.blind import DEFAULT_CASES_PATH, build_case_files, check_blind
from tools.replays.leak_scan import ScanResult, is_void, scan_run

HARNESS_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
#: outside every repo: Claude Code reads the CLAUDE.md of every parent folder of its cwd.
DEFAULT_TREES_DIR = Path("C:/Coding/_replay_trees")
MODEL = "claude-opus-5-5"
EFFORT = "high"
HOME_SLUG = "Rasslabsya4el/orchestration-layer"
#: the repo each case's ``gh`` is stood in for (REPLAY_REPO): a slug, or a ``config:<suffix>`` ref (see resolve_repo).
CASE_REPOS = {"a": HOME_SLUG, "b": "config:cabinet-dashboard", "c": HOME_SLUG, "d": HOME_SLUG, "e": HOME_SLUG}


def resolve_repo(ref: str, harness_root: Path = HARNESS_ROOT) -> tuple[str, str]:
    """(folder, slug) of a repo ref. A ref not starting with ``config:`` is the folder itself, with the home's slug.
    ``config:<suffix>`` is the one ``targets`` entry of config/orchestration.json whose repo ends with ``<suffix>``
    (case-sensitive); zero or two entries raise ValueError."""
    if not ref.startswith("config:"):
        return ref, HOME_SLUG
    suffix = ref[len("config:"):]
    path = Path(harness_root) / "config" / "orchestration.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"config:{suffix} cannot be resolved: {path} is missing or unreadable ({exc})") from exc
    hits = [t for t in config.get("targets", []) if t["repo"].endswith(suffix)]
    if len(hits) != 1:
        raise ValueError(f"config:{suffix} matches {len(hits)} entries of config/orchestration.json, need exactly one")
    return hits[0]["folder"], hits[0]["repo"]
#: written into a model-less assistant record's place by Claude Code itself (an interrupt, an API error): no model.
SYNTHETIC_MODEL = "<synthetic>"
#: the run's environment loses these; the shim hands them to the stub only, through ``REPLAY_<name>``.
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
STAGE_DIR = "_stage"
KILL_WAIT_SECONDS = 30
RESULT_SLACK_SECONDS = 300
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_RECORD_FILE = re.compile(r"^[a-z]-[a-z]+-\d+\.json$")


@dataclass
class Config:
    """Where everything is. ``claude_bin`` may be a ``.py`` file (a stand-in run through this python).

    ``trees_dir`` holds only opaque-named trees; ``state_dir`` (default ``<runs_dir>-state`` beside ``runs_dir``) holds
    only opaque-named state dirs, so nothing the brain can list from its cwd or its shim dir names a case or an arm.
    ``records_dir`` (#357, default none): ``<records_dir>/<case>/`` is copied into every run tree of that case,
    both arms."""

    claude_bin: Path | str
    role_repo: Path
    draft_dir: Path
    runs_dir: Path
    trees_dir: Path = DEFAULT_TREES_DIR
    harness_root: Path = HARNESS_ROOT
    timeout_s: float = 1800
    model: str = MODEL
    effort: str = EFFORT
    projects_dir: Path = DEFAULT_PROJECTS_DIR
    cases_path: Path = DEFAULT_CASES_PATH
    state_dir: Path | None = None
    records_dir: Path | None = None

    def __post_init__(self) -> None:
        # every path absolute (#357): a relative one would make the run's PATH entry for its gh shim relative too,
        # and that entry resolves to nothing from the run's own tree
        for name in ("role_repo", "draft_dir", "runs_dir", "trees_dir", "harness_root", "projects_dir", "cases_path"):
            setattr(self, name, Path(getattr(self, name)).absolute())
        state = Path(self.state_dir) if self.state_dir else self.runs_dir.parent / f"{self.runs_dir.name}-state"
        self.state_dir = state.absolute()
        self.records_dir = Path(self.records_dir).absolute() if self.records_dir is not None else None


def _dump_json(path: Path, data) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_lines(path: Path):
    """The parsed JSON objects of a file, one per line; a line that is not JSON (a killed process's last) is skipped."""
    for raw in Path(path).read_bytes().decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


# ---- one run -----------------------------------------------------------------------------------------------------


def _load_case(case_key: str, cfg: Config) -> dict:
    cases = json.loads(cfg.cases_path.read_text(encoding="utf-8"))["cases"]
    if case_key not in cases:
        raise ValueError(f"unknown case {case_key!r} in {cfg.cases_path}")
    case = {**cases[case_key], "key": case_key}
    case["repo"] = resolve_repo(case["repo"])[0]
    return case


def build_command(cfg: Config, owner_message: str, arm: str = "full") -> list[str]:
    """The exact argv of a run: one prompt, the brain agent (the ``AGENT_ARMS`` only; ``empty`` and ``architect`` run
    with no ``--agent``, #363), no permission questions, stream-json out. Raises ValueError on an unknown arm."""
    if arm not in ARMS:
        raise ValueError(f"arm must be one of {ARMS}, got {arm!r}")
    bin_ = str(cfg.claude_bin)
    prefix = [sys.executable, bin_] if bin_.endswith(".py") else [bin_]
    agent = ["--agent", "brain"] if arm in AGENT_ARMS else []
    return [*prefix, "-p", owner_message, "--model", cfg.model, "--effort", cfg.effort, *agent,
            "--permission-mode", "bypassPermissions", "--output-format", "stream-json", "--verbose"]


def build_resume_command(cfg: Config, session_id: str, answer: str) -> list[str]:
    """The argv of the second turn (#363): ``--resume <id> -p <answer>`` with the first command's model, effort,
    permission mode and output flags. No ``--agent``: a resumed session keeps the agent it started with."""
    bin_ = str(cfg.claude_bin)
    prefix = [sys.executable, bin_] if bin_.endswith(".py") else [bin_]
    return [*prefix, "--resume", session_id, "-p", answer, "--model", cfg.model, "--effort", cfg.effort,
            "--permission-mode", "bypassPermissions", "--output-format", "stream-json", "--verbose"]


def _opaque_id(cfg: Config) -> str:
    while True:
        opaque = "r" + secrets.token_hex(4)
        if not (cfg.trees_dir / opaque).exists() and not (cfg.state_dir / opaque).exists():
            return opaque


def _move_tree(tree: Path, dest: Path) -> Path:
    """Move a run tree to ``dest``. A run tree is a sealed standalone clone (#357, its ``.git`` is a folder, so the move
    is all it needs). A git worktree of an old caller (``.git`` is a file) also gets its admin folder
    (``.git/worktrees/<name>``, named after the tree's first path) renamed and both pointers rewritten, so ``.git`` and
    ``git worktree list`` carry no case or arm word either. Returns ``dest``."""
    if dest.exists():
        raise FileExistsError(dest)
    git_file = tree / ".git"
    admin = None
    if git_file.is_file():
        text = git_file.read_text(encoding="utf-8").strip()
        admin = Path(text.split(":", 1)[1].strip())
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tree), str(dest))
    if admin is not None:
        new_admin = admin.with_name(dest.name)
        admin.rename(new_admin)
        for pointer, line in ((dest / ".git", f"gitdir: {new_admin.as_posix()}"),
                              (new_admin / "gitdir", (dest / ".git").as_posix())):
            pointer.unlink()  # git may mark the file hidden, which a plain overwrite cannot open on Windows
            pointer.write_text(line + "\n", encoding="utf-8")
    return dest


def _write_shims(state: Path, cfg: Config) -> None:
    """``gh.cmd`` (cmd, PowerShell) and ``gh`` (git bash) call the stub of this harness. PYTHONPATH lives in the shim
    only, so the hooks of the run tree keep importing the tree's own ``tools``; ``-P`` keeps the tree (the cwd) off
    the stub's import path. The shim gives the stub the real tokens (``REPLAY_<name>``) the run's env had scrubbed."""
    python = sys.executable
    root = str(cfg.harness_root)
    cmd_tokens = "".join(f'if defined REPLAY_{n} set "{n}=%REPLAY_{n}%"\r\n' for n in TOKEN_VARS)
    (state / "gh.cmd").write_text(
        f'@echo off\r\nset "PYTHONPATH={root}"\r\n{cmd_tokens}"{python}" -P -m tools.replays.gh_stub %*\r\n',
        encoding="utf-8", newline="")
    posix = lambda p: str(p).replace("\\", "/")  # noqa: E731
    sh_tokens = "".join(f'[ -n "$REPLAY_{n}" ] && export {n}="$REPLAY_{n}"\n' for n in TOKEN_VARS)
    (state / "gh").write_text(
        f'#!/bin/sh\n{sh_tokens}PYTHONPATH="{posix(root)}" exec "{posix(python)}" -P -m tools.replays.gh_stub "$@"\n',
        encoding="utf-8", newline="\n")
    if os.name != "nt":  # #348: PATH lookup skips a file without the execute bit, so the run would reach the real gh
        os.chmod(state / "gh", os.stat(state / "gh").st_mode | 0o111)


def _checkout_folders(case_repo: str, harness_root: Path) -> list[str]:
    """The real checkout folders a run must not reach: the case's repo folder and each ``targets`` folder of
    config/orchestration.json (read now). A missing or unreadable config raises: no run starts without its deny list."""
    path = Path(harness_root) / "config" / "orchestration.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"the run's deny list cannot be built: {path} is missing or unreadable ({exc})") from exc
    return [case_repo, *(target["folder"] for target in config.get("targets", []))]


def _deny_rules(folder: str) -> list[str]:
    """The deny rules of one checkout folder (#357): a ``Bash`` pattern for each spelling the folder can be typed in
    (``C:/x/y``, ``C:\\x\\y``, git-bash ``/c/x/y``), and the five file-tool path rules, which Claude Code matches on
    the ``C:/...`` spelling (probed: ``//C:/...`` does not deny a Read)."""
    forward = folder.replace("\\", "/").rstrip("/")
    backslash = forward.replace("/", "\\")
    gitbash = f"/{forward[0].lower()}{forward[2:]}" if forward[1:2] == ":" else forward
    rules = [f"Bash(*{spelling}*)" for spelling in (forward, backslash, gitbash)]
    return rules + [f"{tool}({forward}/**)" for tool in ("Read", "Edit", "Write", "Glob", "Grep")]


def _disable_memory(tree: Path, case_repo: str, harness_root: Path) -> None:
    """The tree's ``settings.local.json``: ``autoMemoryEnabled`` false, and ``permissions.deny`` rules for every real
    checkout (see ``_checkout_folders``), merged with the deny entries and keys already there. Raises ValueError,
    before anything is written, when the config is missing."""
    deny_folders = _checkout_folders(case_repo, harness_root)
    path = tree / ".claude" / "settings.local.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    permissions = settings.setdefault("permissions", {})
    rules = [rule for folder in deny_folders for rule in _deny_rules(folder)]
    permissions["deny"] = list(dict.fromkeys([*permissions.get("deny", []), *rules]))
    settings["autoMemoryEnabled"] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump_json(path, settings)


def _environment(state: Path, case: dict, case_key: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in TOKEN_VARS:
        value = env.pop(name, None)
        if value:
            env[f"REPLAY_{name}"] = value
    env.update(
        REPLAY_STATE_DIR=str(state),
        REPLAY_MOMENT=case["moment_utc"],
        REPLAY_REPO=resolve_repo(CASE_REPOS[case_key])[1],
        REPLAY_REAL_GH=shutil.which("gh") or "gh",
        CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
        PATH=str(state) + os.pathsep + env.get("PATH", ""),
    )
    return env


def check_gh_shim(env: dict[str, str], tree: Path, state: Path) -> None:
    """#357: before the model starts, ``gh`` must resolve first on the run's own PATH, in its own cwd, to a file inside
    the run's state dir (the shim). Otherwise the run would reach the real ``gh`` on GitHub. Raises RuntimeError."""
    try:
        proc = subprocess.run([sys.executable, "-c", "import shutil; print(shutil.which('gh'))"], cwd=tree, env=env,
                              capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh shim does not resolve first on the run's PATH: the check timed out ({exc})") from exc
    lines = proc.stdout.strip().splitlines()
    got = lines[-1].strip() if lines and proc.returncode == 0 else "None"
    if got != "None" and Path(got).is_absolute():  # a relative answer resolves against the wrong cwd: refused
        found = Path(got).resolve()
        if found.is_file():
            try:
                found.relative_to(Path(state).resolve())
                return
            except ValueError:
                pass
    raise RuntimeError(f"gh shim does not resolve first on the run's PATH: got {got}")


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=KILL_WAIT_SECONDS, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _launch(cmd: list[str], tree: Path, env: dict, stdout_path: Path, stderr_path: Path,
            timeout_s: float) -> tuple[int | None, bool]:
    """Run ``cmd`` in ``tree`` with stdin closed and no console window; kill its whole tree at the timeout.
    Returns (exit code or None, timed_out)."""
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        proc = subprocess.Popen(cmd, cwd=tree, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                creationflags=_NO_WINDOW)
        try:
            return proc.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.wait(timeout=KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            return None, True


def _parse_stream(path: Path) -> dict:
    """session_id and effort of the init record, the owner-visible text (every text block of the main thread's
    assistant records, in order, blank-line joined; a helper's records carry ``parent_tool_use_id`` and are left
    out), ``last_text`` (the last of those blocks alone), the final result record's ``total_cost_usd``."""
    session_id = init_effort = cost = None
    texts: list[str] = []
    for rec in _json_lines(path):
        kind = rec.get("type")
        if kind == "system" and rec.get("subtype") == "init":
            session_id = rec.get("session_id") or session_id
            init_effort = rec.get("effort") or init_effort
        elif kind == "assistant" and not rec.get("parent_tool_use_id"):
            content = (rec.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
        elif kind == "result":
            session_id = session_id or rec.get("session_id")
            if rec.get("total_cost_usd") is not None:
                cost = float(rec["total_cost_usd"])
    return {"session_id": session_id, "init_effort": init_effort, "text": "\n\n".join(texts),
            "last_text": texts[-1] if texts else "", "cost_usd": cost}


def projects_folder_name(path: Path) -> str:
    """Claude Code's folder for a project path: every non-alphanumeric character turned into '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def find_transcript(cfg: Config, tree: Path, session_id: str) -> Path | None:
    """``<projects>/<tree path munged>/<session>.jsonl``; failing that, the one match under any project folder
    (a session id is unique)."""
    for candidate in (tree, tree.resolve()):
        path = cfg.projects_dir / projects_folder_name(candidate) / f"{session_id}.jsonl"
        if path.is_file():
            return path
    found = sorted(cfg.projects_dir.glob(f"*/{session_id}.jsonl"))
    return found[0] if found else None


def transcript_models(path: Path) -> list[str]:
    """The distinct ``message.model`` of the session file's assistant records, sorted (``<synthetic>`` excluded)."""
    models = set()
    for rec in _json_lines(path):
        if rec.get("type") == "assistant":
            model = (rec.get("message") or {}).get("model")
            if isinstance(model, str) and model and model != SYNTHETIC_MODEL:
                models.add(model)
    return sorted(models)


def transcript_usage(path: Path) -> dict[str, int]:
    """Token counts of the session file, summed over its assistant messages (one message written in several records
    counts once, by ``message.id``). Tokens, not dollars: the file carries no price."""
    by_message: dict[str, dict] = {}
    for index, rec in enumerate(_json_lines(path)):
        if rec.get("type") != "assistant":
            continue
        message = rec.get("message") or {}
        if isinstance(message.get("usage"), dict):
            by_message[message.get("id") or f"#{index}"] = message["usage"]
    return {f: sum(int(u.get(f) or 0) for u in by_message.values()) for f in USAGE_FIELDS}


def _read_writes(state: Path) -> list[dict]:
    path = state / "writes.jsonl"
    return list(_json_lines(path)) if path.exists() else []


def _void_reason(cfg: Config, parsed: dict, scan, models: list[str], wrong_effort: bool) -> str | None:
    if is_void(scan):
        return "leak marker"
    if scan.skipped_lines:
        return "unreadable transcript lines"
    if not models:
        return "no assistant record in transcript"
    if models != [cfg.model]:
        return "wrong model"
    if wrong_effort:
        return "wrong effort"
    if not parsed["text"].strip():
        return "empty answer"
    return None


def _judge(cfg: Config, tree: Path, case: dict, parsed: dict, turn2: dict | None = None) -> dict:
    """Leak scan, model check and effort check; the fields they add to the record. ``turn2`` (#363, a parsed second
    turn plus ``usable``) adds its transcript when it is a different file than turn 1's (a resumed session may write a
    new session id), so a leak marker in either turn is a hit. A second turn that is not ``usable`` is scanned when its
    transcript exists and never voids by its absence."""
    efforts = [e for e in (parsed["init_effort"], turn2 and turn2["init_effort"]) if e is not None]
    out = {"hits": [], "skipped_lines": [], "void": False, "void_reason": None, "wrong_model": False,
           "wrong_effort": any(e != cfg.effort for e in efforts),
           "model": None, "transcript": None, "usage": None}
    session_id = parsed["session_id"]
    transcript = find_transcript(cfg, tree, session_id) if session_id else None
    if transcript is None:
        out.update(void=True, void_reason="transcript not found")
        return out
    files = [transcript]
    scan = scan_run(transcript, case["leak_markers"])
    if turn2 and turn2["session_id"] and turn2["session_id"] != session_id:
        transcript2 = find_transcript(cfg, tree, turn2["session_id"])
        if transcript2 is not None:
            files.append(transcript2)
            more = scan_run(transcript2, case["leak_markers"])
            scan = ScanResult(scan.hits + more.hits, scan.skipped_lines + more.skipped_lines)
            out.update(transcript2=str(transcript2), usage2=transcript_usage(transcript2))
        elif turn2["usable"]:
            out.update(void=True, void_reason="transcript not found")
            return out
    models = sorted({m for file in files for m in transcript_models(file)})
    reason = _void_reason(cfg, parsed, scan, models, out["wrong_effort"])
    out.update(
        transcript=str(transcript), usage=transcript_usage(transcript),
        hits=[{**hit, "file": str(hit["file"])} for hit in scan.hits],
        skipped_lines=[[str(file), line] for file, line in scan.skipped_lines],
        model=models[0] if len(models) == 1 else (", ".join(models) if models else None),
        wrong_model=bool(models) and models != [cfg.model],
        void=reason is not None, void_reason=reason)
    return out


def _second_turn(cfg: Config, tree: Path, env: dict, state: Path, session_id: str, answer: str) -> dict:
    """Run the second turn; its parsed stream plus ``usable`` (it ended, was not killed and gave text) and ``error``
    (None, ``timed out``, ``exit code N``, ``empty answer`` or the launch exception)."""
    empty = {"session_id": None, "init_effort": None, "text": "", "last_text": "", "cost_usd": None}
    try:
        exit_code, timed_out = _launch(build_resume_command(cfg, session_id, answer), tree, env,
                                       state / "stream2.jsonl", state / "stderr2.txt", cfg.timeout_s)
    except OSError as exc:
        return {**empty, "usable": False, "error": f"did not start: {exc}"}
    parsed = _parse_stream(state / "stream2.jsonl")
    error = "timed out" if timed_out else f"exit code {exit_code}" if exit_code else (
        None if parsed["text"].strip() else "empty answer")
    return {**parsed, "usable": error is None, "error": error}


def run_one(case_key: str, arm: str, n: int, cfg: Config) -> dict:
    """One replay; returns its record and writes it to ``<runs_dir>/<case>-<arm>-<n>.json``. The record's
    ``opaque_id`` is the only name the brain's tree, state dir and shim dir carry."""
    case = _load_case(case_key, cfg)
    opaque = _opaque_id(cfg)
    # the records argument goes only when a records dir is set (#357): a make_run_tree stand-in without it still runs
    records = {} if cfg.records_dir is None else {"records_dir": cfg.records_dir}
    staged = Path(make_run_tree(case, arm, n, cfg.trees_dir / STAGE_DIR, cfg.role_repo, cfg.draft_dir, **records))
    tree = _move_tree(staged, cfg.trees_dir / opaque)
    state = cfg.state_dir / opaque
    state.mkdir(parents=True)
    _write_shims(state, cfg)
    _disable_memory(tree, case["repo"], cfg.harness_root)
    env = _environment(state, case, case_key)
    check_gh_shim(env, tree, state)  # #357: raises before any claude -p starts; no record is written then

    started = time.monotonic()
    exit_code, timed_out = _launch(build_command(cfg, case["owner_message"], arm), tree, env, state / "stream.jsonl",
                                   state / "stderr.txt", cfg.timeout_s)
    parsed = _parse_stream(state / "stream.jsonl")
    judged = _judge(cfg, tree, case, parsed)
    writes = _read_writes(state)  # turn 1's; the stub's file is cumulative, turn 2's are what comes after
    turn2 = None
    if (not timed_out and not judged["void"] and case.get("turn2_answer") and parsed["session_id"]
            and "?" in parsed["last_text"]):
        turn2 = _second_turn(cfg, tree, env, state, parsed["session_id"], case["turn2_answer"])
        judged = _judge(cfg, tree, case, parsed, turn2)
    if timed_out:
        judged.update(void=True, void_reason="timed out")
    cost1 = parsed["cost_usd"] if parsed["cost_usd"] is not None else 0.0
    cost2 = turn2["cost_usd"] if turn2 and turn2["cost_usd"] is not None else 0.0
    record = {
        "run_id": f"{case_key}-{arm}-{n}", "case": case_key, "arm": arm, "n": n, "opaque_id": opaque,
        "session_id": parsed["session_id"],
        "model": judged.pop("model"), "effort": cfg.effort, "init_effort": parsed["init_effort"],
        "cost_usd": round(cost1 + cost2, 6), "cost1": cost1,
        "cost_known": parsed["cost_usd"] is not None and (turn2 is None or turn2["cost_usd"] is not None),
        "turns": 2 if turn2 and turn2["usable"] else 1,
        "text": parsed["text"], "writes": writes,
        "timed_out": timed_out, "exit_code": exit_code, "seconds": round(time.monotonic() - started, 1),
        "tree": str(tree), "state_dir": str(state), **judged,
    }
    if turn2 is not None:
        record["cost2"] = cost2
        record["writes2"] = _read_writes(state)[len(writes):]
        if turn2["usable"]:
            record["text2"] = turn2["text"]
            record["session_id2"] = turn2["session_id"]
        else:
            record["turn2_error"] = turn2["error"]
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _dump_json(cfg.runs_dir / f"{record['run_id']}.json", record)
    return record


# ---- all runs ----------------------------------------------------------------------------------------------------


class _Progress:
    """The counters of ``run_all`` and ``<runs_dir>/progress.json``, rewritten at every start and end. All methods are
    called with ``lock`` held."""

    def __init__(self, path: Path, slots: int) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.state = {"slots": slots, "done": 0, "kept": 0, "running": [], "total_cost_usd": 0.0, "voids": 0,
                      "void_reasons": {}, "unknown_cost_runs": 0}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        _dump_json(tmp, self.state)
        for _ in range(50):  # a reader holding the file open makes the replace fail on Windows
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.01)
        _dump_json(self.path, self.state)

    def over_budget(self, budget_usd: float) -> bool:
        """True when the cost so far plus one more run (and each run in flight) at the average finished run's cost
        would pass the budget. With no run finished the average is 0."""
        done = self.state["done"]
        average = self.state["total_cost_usd"] / done if done else 0.0
        return self.state["total_cost_usd"] + (len(self.state["running"]) + 1) * average > budget_usd

    def stop_for_budget(self) -> None:
        self.state["stopped"] = "budget"
        self._write()

    def start(self, run_id: str) -> None:
        self.state["running"].append(run_id)
        self._write()

    def abort(self, run_id: str) -> None:
        """A run that raised: it is no longer running."""
        self.state["running"].remove(run_id)
        self._write()

    def end(self, record: dict, kept: bool) -> None:
        state = self.state
        state["running"].remove(record["run_id"])
        state["done"] += 1
        state["kept"] += int(kept)
        state["total_cost_usd"] = round(state["total_cost_usd"] + record["cost_usd"], 6)
        state["unknown_cost_runs"] += int(not record["cost_known"])
        if not kept:
            reason = record["void_reason"] or "empty answer"
            state["voids"] += 1
            state["void_reasons"][reason] = state["void_reasons"].get(reason, 0) + 1
        self._write()


def _is_kept(record: dict) -> bool:
    return not (record["void"] or record["timed_out"] or not record["text"].strip())


def run_all(cfg: Config, cases=("a", "b", "c"), arms=("old", "new"), per_arm: int = 3, concurrency: int = 3,
            budget_usd: float = 60.0, max_reruns: int = 4) -> list[dict]:
    """Run every slot (case, arm, 1..per_arm), at most ``concurrency`` at once. A slot whose run is void, timed out
    or empty runs again under a fresh ``n``, up to ``max_reruns`` times. Before each start, if the cost so far plus
    the average cost of the finished runs for this one and each in flight would pass ``budget_usd``, nothing more
    starts. Returns every record, kept and void, in end order."""
    slots = [(case, arm) for case in cases for arm in arms for _ in range(per_arm)]
    progress = _Progress(cfg.runs_dir / "progress.json", len(slots))
    next_n: dict[tuple[str, str], int] = defaultdict(lambda: 1)
    records: list[dict] = []

    def work(slot: tuple[str, str]) -> None:
        case, arm = slot
        for _ in range(1 + max_reruns):
            with progress.lock:
                if progress.over_budget(budget_usd):
                    progress.stop_for_budget()
                    return
                n = next_n[slot]
                next_n[slot] += 1
                progress.start(f"{case}-{arm}-{n}")
            try:
                record = run_one(case, arm, n, cfg)
            except BaseException:
                with progress.lock:
                    progress.abort(f"{case}-{arm}-{n}")
                raise
            kept = _is_kept(record)
            with progress.lock:
                records.append(record)
                progress.end(record, kept)
            if kept:
                return

    slot_bound = (1 + max_reruns) * (2 * cfg.timeout_s + RESULT_SLACK_SECONDS)  # two turns a run (#363)
    pool = ThreadPoolExecutor(max_workers=concurrency)
    try:
        for future in [pool.submit(work, slot) for slot in slots]:
            future.result(timeout=slot_bound)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return records


# ---- after the runs ----------------------------------------------------------------------------------------------


def finish(cfg: Config, records: list[dict], out_dir: Path, key_path: Path, seed: int | None = None,
           per_arm: int = 3, mark_key: str = "pass_mark") -> list[str]:
    """Per case, the ``per_arm`` kept records of each arm the records hold (lowest ``n`` first; #363: three arms, so
    nine outputs) become the blind case file and the key entries. ``mark_key`` is the case key the file's pass mark is
    read from (round 4: ``pass_mark_r4``); it goes to ``build_case_files`` only when it is not the default, so the
    default call is the one blind.py has always taken. Raises ValueError, before anything is written, naming every
    case and arm with fewer kept records. Returns the problems ``check_blind`` finds."""
    kept = [r for r in records if _is_kept(r)]
    cases = sorted({r["case"] for r in records})
    arms_of = {c: sorted({r["arm"] for r in records if r["case"] == c}) for c in cases}
    short = [f"case {c} arm {arm}: {have} kept runs, need {per_arm}"
             for c in cases for arm in arms_of[c]
             if (have := sum(1 for r in kept if r["case"] == c and r["arm"] == arm)) < per_arm]
    if short:
        raise ValueError("too few kept runs: " + "; ".join(short))
    problems: list[str] = []
    for index, case_key in enumerate(cases):
        outputs = []
        for arm in arms_of[case_key]:
            mine = sorted((r for r in kept if r["case"] == case_key and r["arm"] == arm), key=lambda r: r["n"])
            outputs += mine[:per_arm]
        extra = {} if mark_key == "pass_mark" else {"mark_key": mark_key}
        case_file = build_case_files(case_key, outputs, out_dir, key_path,
                                     seed=None if seed is None else seed + index, cases_path=cfg.cases_path, **extra)
        problems += [f"case {case_key}: {p}" for p in check_blind(case_file, key_path)]
    return problems


def issue_counts(repos, gh_cmd: list[str] | None = None) -> dict[str, int]:
    """The highest issue number of each repo (the newest issue, open or closed) through the real gh."""
    gh = gh_cmd or [shutil.which("gh") or "gh"]
    counts: dict[str, int] = {}
    for repo in repos:
        done = subprocess.run([*gh, "issue", "list", "-R", repo, "--state", "all", "--limit", "1", "--json", "number"],
                              capture_output=True, text=True, encoding="utf-8", timeout=120,
                              creationflags=_NO_WINDOW)
        if done.returncode != 0:
            raise RuntimeError(f"gh issue list -R {repo}: {done.stderr.strip()}")
        counts[repo] = max((row["number"] for row in json.loads(done.stdout)), default=0)
    return counts


# ---- command line ------------------------------------------------------------------------------------------------


def _default_runs_dir() -> Path:
    return HARNESS_ROOT / "var" / "agentops" / "replays"


def _load_records(runs_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(runs_dir.glob("*.json"))
            if _RECORD_FILE.match(p.name)]


def _cfg_from(args) -> Config:
    claude_bin = args.claude_bin
    if claude_bin is None:
        from tools.agentops.dispatch import find_claude_exe
        claude_bin = find_claude_exe()
    return Config(claude_bin=claude_bin, role_repo=args.role_repo, draft_dir=args.draft_dir, runs_dir=args.runs_dir,
                  trees_dir=args.trees_dir, harness_root=args.harness_root, timeout_s=args.timeout,
                  records_dir=args.records_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.replays.launcher")
    sub = parser.add_subparsers(dest="command", required=True)

    counts = sub.add_parser("counts", help="highest issue number per repo, through the real gh")
    counts.add_argument("repos", nargs="*", default=None,
                        help="default: the repo of every case (resolved when the command runs, not when the parser is built)")

    run = sub.add_parser("run", help="run the replays (writes nothing to GitHub)")
    run.add_argument("--trees-dir", type=Path, default=DEFAULT_TREES_DIR,
                     help="outside every repo: Claude Code reads the CLAUDE.md of every parent folder")
    run.add_argument("--claude-bin", default=None)
    run.add_argument("--role-repo", type=Path, default=HARNESS_ROOT)
    run.add_argument("--draft-dir", type=Path,
                     default=HARNESS_ROOT / "docs" / "research" / "2026-10-23-brain-role-draft")
    run.add_argument("--runs-dir", type=Path, default=_default_runs_dir())
    run.add_argument("--harness-root", type=Path, default=HARNESS_ROOT)
    run.add_argument("--timeout", type=float, default=1800)
    run.add_argument("--records-dir", type=Path, default=None,
                     help="read-only records copied into each run tree: <records-dir>/<case>/ (#357)")
    run.add_argument("--cases", nargs="+", default=["a", "b", "c"])
    run.add_argument("--arms", nargs="+", default=["old", "new"])
    run.add_argument("--per-arm", type=int, default=3)
    run.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--budget", type=float, default=60.0)
    run.add_argument("--max-reruns", type=int, default=4)

    fin = sub.add_parser("finish", help="build the blind case files and the key from the kept runs")
    fin.add_argument("--runs-dir", type=Path, default=_default_runs_dir())
    fin.add_argument("--out-dir", type=Path,
                     default=HARNESS_ROOT / "docs" / "research" / f"{date.today():%Y-%m-%d}-brain-replays")
    fin.add_argument("--key", type=Path, default=HARNESS_ROOT / "var" / "agentops" / "replays-key.json")
    fin.add_argument("--seed", type=int, default=None)
    fin.add_argument("--mark-key", default="pass_mark",
                     help="the case key the blind file's pass mark is read from (round 4: pass_mark_r4)")

    args = parser.parse_args(argv)
    if args.command == "counts":
        repos = args.repos or sorted({resolve_repo(ref)[1] for ref in CASE_REPOS.values()})
        print(json.dumps(issue_counts(repos), indent=2))
        return 0
    if args.command == "run":
        records = run_all(_cfg_from(args), cases=tuple(args.cases), arms=tuple(args.arms), per_arm=args.per_arm,
                          concurrency=args.concurrency, budget_usd=args.budget, max_reruns=args.max_reruns)
        progress = json.loads((args.runs_dir / "progress.json").read_text(encoding="utf-8"))
        print(json.dumps({**progress, "records": len(records)}, indent=2))
        return 2 if progress.get("stopped") else 0
    cfg = Config(claude_bin="", role_repo=HARNESS_ROOT, draft_dir=HARNESS_ROOT, runs_dir=args.runs_dir)
    problems = finish(cfg, _load_records(args.runs_dir), args.out_dir, args.key, args.seed,
                      mark_key=args.mark_key)
    print("\n".join(problems) if problems else "blind check: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
