"""`python -m tools.agentops skills --since YYYY-MM-DD [--until YYYY-MM-DD] [--issue N]`: Skill calls per role,
and GLOSSARY.md reads by subagents, over the project's own transcripts (#1066). A local module (`local_modules`).

Reads only `Path.home()/.claude/projects/<entry>` where the entry's name starts with the project's folder prefix
(one level), its `*.jsonl` main sessions and `<session>/subagents/*.jsonl` (+ `.meta.json`). A record counts when its
own `timestamp` is in [since, until); `--issue N` keeps the transcripts whose text mentions `#N`.
"""

from __future__ import annotations

import itertools
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tools.agentops import outcomes
from tools.agentops.core_settings import load as _load_settings
from tools.agentops.outcomes_transcripts import _agent_type

#: the Claude projects folder of this repo, from config/orchestration.json (no repo name is written here).
PROJECT_PREFIX = _load_settings()["claude_projects_dir"]
GLOSSARY = "GLOSSARY.md"
SESSION_ROLE = "session"
#: the repo root is two parents above tools/agentops/.
REPO_ROOT = Path(__file__).resolve().parents[2]
#: the merge log whose conflict lines the table reports (var/ is linked into each worktree).
MERGE_LOG = REPO_ROOT / "var" / "agentops" / "merge.log"
#: skills the rule names by hard trigger: shown with 0 when no call happened, so a missing call is visible.
WATCHED_SKILLS = (
    "write-ticket",
    "mattpocock-skills:tdd",
    "mattpocock-skills:diagnosing-bugs",
    "mattpocock-skills:code-review",
    "mattpocock-skills:writing-for-agents",
    "mattpocock-skills:domain-modeling",
    "mattpocock-skills:codebase-design",
)
#: roles the rules of #1067 name: shown as a column with 0 when they made no call, so a missing call is visible.
#: The target's own extra roles come from the settings' `skill_watched_roles` (#72), see `watched_roles`.
BASE_WATCHED_ROLES = ("researcher-find", "researcher-how")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)


@dataclass
class Tally:
    #: (skill, role) -> Skill tool_use blocks in the window.
    calls: Counter = field(default_factory=Counter)
    #: the project folders read (names under ~/.claude/projects).
    folders: list[str] = field(default_factory=list)
    #: subagent transcripts with >= 1 tool call in the window.
    subagents_with_calls: int = 0
    #: of those, the ones with a tool call whose input names GLOSSARY.md.
    subagents_naming_glossary: int = 0
    #: merge.log lines containing "conflict" (any case) whose timestamp is in the window.
    conflict_lines: int = 0
    #: the extra watched roles this tally was read with (settings `skill_watched_roles`; each is an agent-setting name).
    watched: tuple[str, ...] = ()


def watched_roles(settings: dict | None = None) -> tuple[str, ...]:
    """The settings' `skill_watched_roles` (#72): a list of agent-setting names, each also the role name a main session
    gets when its first records name it. Absent or not a list = none."""
    data = _load_settings() if settings is None else settings
    raw = data.get("skill_watched_roles")
    if not isinstance(raw, list):
        return ()
    return tuple(name for name in raw if isinstance(name, str))


def _window_edge(raw: str | None, default: datetime) -> datetime:
    if raw is None:
        return default
    return outcomes._token_meter().parse_date_arg(raw)


def project_folders(projects_root: Path) -> list[Path]:
    if not projects_root.is_dir():
        return []
    return sorted(p for p in projects_root.iterdir() if p.is_dir() and p.name.startswith(PROJECT_PREFIX))


def _session_role(path: Path, watched: tuple[str, ...]) -> str:
    """The agent-setting's name when one of a main session's first 30 lines is that record and the name is one of
    the `watched` roles, else `session` (an agent-setting not listed, e.g. orch, stays `session`)."""

    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in itertools.islice(fh, 30):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "agent-setting":
                    setting = obj.get("agentSetting")
                    return setting if isinstance(setting, str) and setting in watched else SESSION_ROLE
    except OSError:
        pass
    return SESSION_ROLE


def _transcripts(folder: Path, since: datetime, watched: tuple[str, ...]):
    """(path, role) of each transcript modified at or after `since` (a transcript last written before
    the window holds no record inside it). Main sessions first, then subagents."""

    lo = since.timestamp()
    for path in sorted(folder.glob("*.jsonl")):
        if path.stat().st_mtime >= lo:
            yield path, _session_role(path, watched), False
    for path in sorted(folder.glob("*/subagents/*.jsonl")):
        if path.stat().st_mtime < lo:
            continue
        agent_type = _agent_type(path.parent / (path.stem + ".meta.json"))
        yield path, (f"subagent ({agent_type})" if agent_type else "subagent"), True


def _lines(path: Path):
    """The transcript's lines one at a time: the pieces `read_text().splitlines()` gives (universal newlines, then
    str.splitlines on each physical line), without holding the whole file in memory."""

    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            yield from raw.splitlines()


def _mentions(path: Path, issue_re) -> bool:
    """True when a line of the transcript matches `issue_re`. The pattern holds no line break, so a line-wise search
    finds exactly what a search of the whole text finds."""

    return any(issue_re.search(line) for line in _lines(path))


def _scan(path: Path, *, tm, since: datetime, until: datetime, issue_re) -> tuple[list[str], int, bool] | None:
    """(Skill names, tool calls, names GLOSSARY.md) of one transcript's records inside the window;
    None when the transcript does not mention the issue."""

    if issue_re is not None and not _mentions(path, issue_re):
        return None
    skills: list[str] = []
    tool_calls = 0
    glossary = False
    for line in _lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = tm.parse_timestamp(obj.get("timestamp") if isinstance(obj, dict) else None)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.astimezone()
        if not (since <= ts < until):
            continue
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_calls += 1
            tool_input = block.get("input") or {}
            if block.get("name") == "Skill":
                skills.append(str(tool_input.get("skill") or "-"))
            if GLOSSARY in json.dumps(tool_input, ensure_ascii=False):
                glossary = True
    return skills, tool_calls, glossary


def _count_conflicts(path: Path, *, tm, since: datetime, until: datetime) -> int:
    """merge.log lines containing "conflict" (any case) whose leading `YYYY-MM-DD HH:MM:SS` is in [since, until);
    a missing file counts 0."""

    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "conflict" not in line.lower():
            continue
        ts = tm.parse_timestamp(line[:19])
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.astimezone()
        if since <= ts < until:
            count += 1
    return count


def tally(projects_root: Path, *, since: str, until: str | None = None, issue: int | None = None,
          merge_log: Path | None = None) -> Tally:
    """Skill calls per (skill, role), the GLOSSARY.md count and the merge-log conflict lines, over the project
    folders under `projects_root` (`merge_log` defaults to MERGE_LOG)."""

    tm = outcomes._token_meter()
    lo = _window_edge(since, _EPOCH)
    hi = _window_edge(until, _FAR_FUTURE)
    issue_re = re.compile(rf"#{issue}(?!\d)") if issue is not None else None
    watched = watched_roles()
    result = Tally(watched=watched)
    for folder in project_folders(projects_root):
        result.folders.append(folder.name)
        for path, role, is_subagent in _transcripts(folder, lo, watched):
            scanned = _scan(path, tm=tm, since=lo, until=hi, issue_re=issue_re)
            if scanned is None:
                continue
            skills, tool_calls, glossary = scanned
            for skill in skills:
                result.calls[(skill, role)] += 1
            if is_subagent and tool_calls:
                result.subagents_with_calls += 1
                if glossary:
                    result.subagents_naming_glossary += 1
    result.conflict_lines = _count_conflicts(MERGE_LOG if merge_log is None else merge_log,
                                             tm=tm, since=lo, until=hi)
    return result


def render(t: Tally, *, since: str, until: str | None, issue: int | None) -> str:
    window = f"{since} .. {until or 'now'}" + (" (until exclusive)" if until else "")
    head = f"Skill calls, {window}; folders read: {len(t.folders)}"
    if issue is not None:
        head += f"; transcripts mentioning #{issue} only"
    skills = {skill for skill, _ in t.calls} | set(WATCHED_SKILLS)
    roles = sorted({role for _, role in t.calls} | set(BASE_WATCHED_ROLES) | set(t.watched),
                   key=lambda r: (r != SESSION_ROLE, r))
    totals = {s: sum(t.calls[(s, r)] for r in roles) for s in skills}
    lines = [head, "", "| skill | total | " + " | ".join(roles) + " |",
             "|---|---:|" + "---:|" * len(roles)]
    for skill in sorted(skills, key=lambda s: (-totals[s], s)):
        cells = [str(t.calls[(skill, r)]) for r in roles]
        lines.append(f"| {skill} | {totals[skill]} | " + " | ".join(cells) + " |")
    lines += ["", f"merge conflicts in window (var/agentops/merge.log lines containing \"conflict\", "
                  f"case-insensitive): {t.conflict_lines}"]
    lines += ["", f"subagent transcripts with >= 1 tool call: {t.subagents_with_calls}, "
                  f"of them naming GLOSSARY.md in a tool call: {t.subagents_naming_glossary}"]
    return "\n".join(lines)


def _add_skills_parser(sub) -> None:
    parser = sub.add_parser("skills", help="Skill calls per role and GLOSSARY.md reads over the project's transcripts (#1066)")
    parser.add_argument("--since", required=True, help="a date (YYYY-MM-DD), the window's start, inclusive")
    parser.add_argument("--until", help="a date (YYYY-MM-DD), the window's end, exclusive (default: no end)")
    parser.add_argument("--issue", type=int, help="only transcripts whose text mentions #N")


def _run_skills(args) -> int:
    projects_root = Path.home() / ".claude" / "projects"
    t = tally(projects_root, since=args.since, until=args.until, issue=args.issue, merge_log=MERGE_LOG)
    print(render(t, since=args.since, until=args.until, issue=args.issue))
    return 0


def register(core) -> None:
    core.command("skills", _add_skills_parser, _run_skills, after="outcomes")
