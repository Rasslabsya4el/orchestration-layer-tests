"""Read-only report (a target ticket, #645): which of ORCH's and the brain's OWN turns are reads a Haiku researcher
could do.

Over the main chats of a window (not subagents) each assistant turn (one per message.id) that carries
a Bash / PowerShell tool_use is classed as ``TABLE_TEXT`` says -- reads / plumbing / runs / other, "other"
never folded into reads: by its first command, except a turn whose every command only reads. A Read / Grep /
Glob tool_use turn is a read of its own column. Every turn is priced with token_meter's ``turn_cost``.
The same reads are then priced as researcher-find spawns on claude-haiku-5-5:
median $ of a find spawn x read turns / median tool calls per find spawn.

    python scripts/own_turns.py --since 2026-10-01 --until 2026-10-08 [--role orch|brain|both] [--top-other N]

``--top-other N`` lists the N commonest first commands of the "other" class (#78).
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import token_meter as tm  # noqa: E402

HAIKU = "claude-haiku-5-5"
SHELL_TOOLS = ("Bash", "PowerShell")
READ_TOOLS = ("Read", "Grep", "Glob")
KINDS = ("reads", "tool reads", "plumbing", "runs", "other", "no command")

# The classification table, printed with the report. A command word is read by its basename, lowercased.
READ_WORDS = {
    "cat", "head", "tail", "grep", "rg", "ls", "find", "wc", "get-content", "gc", "select-string",
    "get-childitem", "dir", "type", "tree", "sed", "awk", "sort", "uniq", "cut", "stat", "file", "diff",
    "test", "[", "test-path", "select-object", "where-object", "sort-object", "measure-object",
    "format-table", "format-list", "group-object",
}
PLUMBING_WORDS = {
    "gh", "git", "pytest", "mkdir", "echo", "date", "cd", "pwd", "printf", "sleep", "true", "touch",
    "mv", "cp", "rm",
}
RUN_SCRIPT_RE = re.compile(r"scripts[\\/]\w*(?:runs?|load|benchmark|replay|probe)\w*\.py")
PYTHON_WORDS = {"python", "python3", "py", "python.exe"}
# the stdlib modules a python -c / python - run may import and still count as a read
PY_READ_MODULES = {
    "json", "re", "sys", "os", "pathlib", "csv", "glob", "datetime", "collections", "statistics", "textwrap",
    "math", "itertools", "functools", "typing", "dataclasses", "hashlib", "base64", "io", "codecs",
    "operator", "string", "shlex", "struct", "binascii", "__future__",
}
PY_IMPORT_RE = re.compile(r"\b(?:from\s+([A-Za-z_][\w.]*)\s+)?import\s+([^\n;\"')]+)")
PRINT_ONLY_BAD = re.compile(
    r"\.write|\bopen\([^\n]*?,\s*(?:mode\s*=\s*)?[\"'][^\"']*[wax+]|\.open\(\s*(?:mode\s*=\s*)?[\"'][^\"']*[wax+]|"
    r"\bos\.(?!(?:path|environ|getcwd|getenv|listdir|scandir|walk|stat|sep|linesep|name|getpid)\b)\w+|"
    r"unlink|rmtree|subprocess|shutil|\.rename|\.mkdir|\.touch|\.rmdir|\.chmod|requests\.|urllib|socket|"
    r"exec\(|eval\(|__import__|importlib|runpy|sleep\(",
)
# a PowerShell Verb-Noun cmdlet name in a command text
PS_CMDLET_RE = re.compile(r"\b[A-Za-z]+-[A-Za-z]\w*")
# any of these anywhere in the whole turn: it writes, runs, kills or reaches another machine
WRITE_GUARD = re.compile(
    r"sed\s+(-\w*i|--in-place)|\b(tee|mv|cp|rm|mkdir|touch|rmdir|ln|chmod|dd|truncate|mklink|unlink|nohup|"
    r"setsid|kill|taskkill|start|ssh|scp|Remove-Item|Move-Item|Copy-Item|Set-Content|Add-Content|Out-File|"
    r"New-Item|Clear-Content|Stop-Process|Start-Process)\b|-delete\b|-exec\b|--detach|tools\.agentops|"
    r"(?<![/\w.])agentops\b(?!/)|scripts[\\/]\w*(?:runs?|load)\w*\.py|"
    r"pytest|\bdelete\b|\bcall\b",
)
# a redirect outside quotes: `>f`, `>>f`, `2>f`, `&>f`; not `2>&1`, `>&2`, `>/dev/null`, `>$null`, `->`, `>=`
REDIRECT_RE = re.compile(r"(?<!-)>>?(?![=&]|\s*(?:/dev/null|\$null)\b)")
# a heredoc: its body becomes one quoted word; the rest of the `<<` line (group 3) stays in the command
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n\s*\2\b", re.S)
ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
KEYWORDS = {"if", "while", "until", "then", "do", "else", "elif", "time", "!", "{", "}", "$", "done", "fi"}
HEADERS = {"for", "case", "esac"}
HARMLESS_WORDS = {"cd", ":", "exit", "break", "continue"}


def _table_text() -> str:
    reads = " ".join(sorted(READ_WORDS))
    modules = " ".join(sorted(PY_READ_MODULES))
    return f"""Classification (a Bash / PowerShell tool_use): a turn counts by its first command (the first command of
the turn that is a command of its own: assignments W=x, loop headers, do, cd, : and timeout N are skipped), except
a turn whose every command only reads counts as reads. A turn that may write (a redirect >, >>, 2>f, &>f, or a
command that is not a read and carries a write word: tee, rm, mv, Out-File, Set-Content, a run script, pytest ...) is
never reads: its reads become other.
  reads     {reads}
            sed, sort, tail, find without -i, -o, -f, -delete, -exec; python -c '...' or python - that prints,
            writes nothing and imports
            only {modules}; powershell -Command whose every cmdlet is a reads word
  tool reads  a Read / Grep / Glob tool_use (no shell command in the turn)
  plumbing  gh git pytest python -m pytest python -m tools.agentops mkdir echo date cd pwd printf sleep touch mv cp rm
  runs      scripts/*runs*|*load*|*benchmark*|*replay*|*probe*.py
  other     everything else (never folded into reads): wait loops, probes, writes
  no command  a turn with no Bash / PowerShell / Read / Grep / Glob tool_use (text, Edit, Agent ...)"""


TABLE_TEXT = _table_text()


def _flatten(command: str) -> str:
    """``command`` with each heredoc body folded into one quoted word where the ``<<`` was; the rest of the
    ``<<`` line (a redirect, a pipe, ``&& rm``) stays in the command."""

    return HEREDOC_RE.sub(lambda m: ' "' + m.group(4).replace('"', "'") + '" ' + m.group(3), command)


def _segments(text: str) -> list[str]:
    """The simple commands of ``text``: cut at ``&&`` ``||`` ``;`` ``|`` newline ``(`` ``)`` and backticks,
    never inside a quoted string (a multi-line ``python -c "..."`` stays one command)."""

    out: list[str] = []
    cur: list[str] = []
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif text.startswith(("&&", "||"), i):
            out.append("".join(cur))
            cur = []
            i += 2
            continue
        elif ch in ";|\n()`":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [seg.strip() for seg in out if seg.strip()]


def _unquoted(text: str) -> str:
    """``text`` with every quoted string blanked out (shell redirects live outside quotes)."""

    out: list[str] = []
    quote = None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append(" ")
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _base(word: str) -> str:
    """A command word's name: quotes, any directory and case dropped."""

    return word.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()


def _words(seg: str) -> list[str]:
    """The words of a simple command after its leading ``NAME=value`` assignments, shell keywords and a
    ``timeout N`` wrapper."""

    words = seg.split()
    while True:
        while words and (ASSIGN_RE.match(words[0]) or words[0] in KEYWORDS):
            words = words[1:]
        if not words or _base(words[0]) != "timeout":
            return words
        rest = words[1:]
        if rest and re.match(r"^\d+[smh]?$", rest[0]):
            rest = rest[1:]
        words = rest


def _is_command(words: list[str]) -> bool:
    """False for a segment with no command of its own: nothing left, a loop or case header, cd, :, exit."""

    return bool(words) and _base(words[0]) not in HEADERS | HARMLESS_WORDS


def head_segment(command: str) -> str:
    """The turn's first simple command that is a command of its own (see ``_is_command``); "" when none."""

    for seg in _segments(_flatten(command)):
        if _is_command(_words(seg)):
            return seg
    return ""


def _edits_or_follows(first: str, args: list[str]) -> bool:
    """A read command in its writing or waiting form: ``sed -i`` in any position, ``sort -o``, ``tail -f``,
    ``find -delete`` / ``-exec`` / ``-fprint``."""

    if first == "sed":
        return any(re.match(r"-\w*i|--in-place", a) for a in args)
    if first == "sort":
        return any(re.match(r"-\w*o|--output", a) for a in args)
    if first == "tail":
        return any(re.match(r"-\w*[fF]|--follow", a) for a in args)
    if first == "find":
        return any(re.match(r"-(delete|exec|execdir|ok|okdir|fls|fprint\w*)$", a) for a in args)
    return False


def _py_modules(seg: str) -> list[str]:
    """The top-level modules a python snippet imports (``from X import`` gives X only)."""

    mods: list[str] = []
    for m in PY_IMPORT_RE.finditer(seg):
        if m.group(1):
            mods.append(m.group(1))
        else:
            mods.extend(part.split()[0] for part in m.group(2).split(",") if part.split())
    return [mod.split(".")[0] for mod in mods]


def _python_reads(seg: str) -> bool:
    """A python -c / python - run that prints, writes nothing, and imports only stdlib read modules."""

    if "print" not in seg or PRINT_ONLY_BAD.search(seg):
        return False
    return all(mod in PY_READ_MODULES for mod in _py_modules(seg))


def _ps_reads(seg: str) -> bool:
    """A powershell -Command body whose every Verb-Noun cmdlet is a reads word (an allowlist)."""

    names = [m.group(0).lower() for m in PS_CMDLET_RE.finditer(seg)]
    return bool(names) and all(name in READ_WORDS for name in names)


def _segment_kind(seg: str) -> str:
    """-> "read" (a file lookup, or a python / PowerShell query that only reads), "harmless" (no command of
    its own), or "no" for any other simple command."""

    words = _words(seg)
    if not _is_command(words):
        return "harmless"
    first = _base(words[0])
    args = words[1:]
    if first in READ_WORDS:
        return "no" if _edits_or_follows(first, args) else "read"
    if first in PYTHON_WORDS and args[:1] in (["-c"], ["-"]):
        return "read" if _python_reads(seg) else "no"
    if first in ("powershell", "pwsh"):
        return "read" if _ps_reads(seg) else "no"
    return "no"


def _redirects(command: str) -> bool:
    """True when a redirect sits outside quotes (a heredoc body is quoted, its ``<<`` line is not)."""

    return bool(REDIRECT_RE.search(_unquoted(_flatten(command))))


def _may_write(command: str) -> bool:
    """True when the turn may write: a redirect, or a command that is not a read and carries a write word.
    A read command cannot write except through its own flags (``_edits_or_follows``), so a word in its
    arguments (``grep pytest``) is a name, not a write."""

    text = _flatten(command)
    return _redirects(command) or any(
        WRITE_GUARD.search(seg) for seg in _segments(text) if _segment_kind(seg) == "no")


def is_read_turn(command: str) -> bool:
    """True when the whole turn only reads: no redirect, and every simple command is a read (at least one)."""

    if _redirects(command):
        return False
    kinds = [_segment_kind(seg) for seg in _segments(_flatten(command))]
    return "read" in kinds and "no" not in kinds


def classify_command(command: str) -> str:
    """-> "reads", "plumbing", "runs" or "other". A turn whose every command only reads is "reads"; any other
    turn counts by its first command, and a turn that may write is never "reads" (its reads become "other")."""

    if is_read_turn(command):
        return "reads"
    kind = _first_word_class(command)
    if kind == "reads" and _may_write(command):
        return "other"
    return kind


def _first_word_class(command: str) -> str:
    seg = head_segment(command)
    words = _words(seg)
    if not words:
        return "other"
    first = _base(words[0])
    args = words[1:]
    if first in PYTHON_WORDS:
        if args[:1] == ["-m"] and len(args) > 1:
            return "plumbing" if args[1] == "pytest" or args[1].startswith("tools.agentops") else "other"
        if args[:1] in (["-c"], ["-"]):
            return "reads" if _python_reads(seg) else "other"
        return "runs" if RUN_SCRIPT_RE.search(_unquoted(seg)) else "other"
    if first in READ_WORDS:
        return "other" if _edits_or_follows(first, args) else "reads"
    if first in PLUMBING_WORDS:
        return "plumbing"
    return "other"


def command_head(command: str) -> str:
    """The key a turn's first command is listed under by ``--top-other``: its command word, and for a
    Python run the module (``-m``) or the flag it runs with (``-c``)."""

    words = _words(head_segment(command))
    if not words:
        return "(empty)"
    first = _base(words[0])
    if first in PYTHON_WORDS and len(words) > 1:
        return f"{first} {words[1]}"
    return first


@dataclass
class TurnRow:
    kind: str
    cost: float | None


@dataclass
class RoleStats:
    counts: dict[str, int] = field(default_factory=lambda: {k: 0 for k in KINDS})
    money: dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in KINDS})
    unpriced: int = 0
    other_heads: dict[str, int] = field(default_factory=dict)
    unpriced_models: dict[str, int] = field(default_factory=dict)
    excluded_turns: int = 0
    excluded_sessions: int = 0
    sessions: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def read_turns(self) -> int:
        return self.counts["reads"] + self.counts["tool reads"]

    @property
    def read_money(self) -> float:
        return self.money["reads"] + self.money["tool reads"]


def turn_commands(path: Path) -> dict[str, tuple[str, str | None]]:
    """message.id -> (tool kind, first shell command or None) for a main transcript: the first
    tool_use of the turn decides (a Bash / PowerShell one gives its command)."""

    out: dict[str, tuple[str, str | None]] = {}
    for obj in tm._read_jsonl(path):
        if obj.get("type") != "assistant":
            continue
        message = obj.get("message") or {}
        mid = message.get("id")
        content = message.get("content")
        if not mid or not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            if mid in out:
                break
            name = block.get("name") or ""
            inp = block.get("input") or {}
            if name in SHELL_TOOLS:
                out[mid] = ("shell", str(inp.get("command") or ""))
            elif name in READ_TOOLS:
                out[mid] = ("tool", None)
            else:
                out[mid] = ("other-tool", None)
            break
    return out


def count_tool_calls(path: Path) -> int:
    seen: set[str] = set()
    for obj in tm._read_jsonl(path):
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    seen.add(block.get("id") or f"anon-{len(seen)}")
    return len(seen)


def role_of(title: str | None, brain_pattern: str | None, first_turn: tm.Turn | None) -> str | None:
    if title and tm.ORCH_NAME_RE.match(title):
        return "orch"
    if first_turn is not None and tm.is_brain_turn(first_turn):
        return "brain"
    if brain_pattern and tm.classify_chat(title, brain_pattern=brain_pattern, orch_pattern=r"$^") == "brain":
        return "brain"
    return None


def is_real(t: tm.Turn) -> bool:
    """False for a ``<synthetic>`` / model-less turn or one with 0 tokens in every kind (a failed start)."""

    if t.model in (None, "<synthetic>"):
        return False
    return (t.input_tokens + t.cache_creation_input_tokens + t.cache_read_input_tokens + t.output_tokens) > 0


def money(x: float) -> str:
    return f"${x:,.2f}"


def collect(projects_dir: Path, prices: dict, since, until, roles: set[str], brain_pattern: str | None):
    """-> ({role: RoleStats}, {role: zero-researcher session count}, haiku info)."""

    stats = {r: RoleStats() for r in roles}
    since_ts = since.timestamp()
    for path in tm.discover_main_transcripts(projects_dir):
        try:
            if path.stat().st_mtime < since_ts:
                continue
        except OSError:
            continue
        parsed = tm.parse_main_transcript(path)
        if not parsed.turns:
            continue
        role = role_of(parsed.title, brain_pattern, parsed.turns[0])
        if role not in roles:
            continue
        in_win = [t for t in parsed.turns if tm.in_window(t, since, until)]
        if not in_win:
            continue
        turns = [t for t in in_win if is_real(t)]
        stats[role].excluded_turns += len(in_win) - len(turns)
        if not turns:
            stats[role].excluded_sessions += 1
            continue
        cmds = turn_commands(path)
        st = stats[role]
        st.sessions.add(path.stem)
        for t in turns:
            tool, command = cmds.get(t.message_id, ("none", None))
            if tool == "shell":
                kind = classify_command(command or "")
            elif tool == "tool":
                kind = "tool reads"
            else:
                kind = "no command"
            cost = tm.turn_cost(t, prices)
            st.counts[kind] += 1
            if kind == "other":
                head = command_head(command or "")
                st.other_heads[head] = st.other_heads.get(head, 0) + 1
            if cost is None:
                st.unpriced += 1
                st.unpriced_models[t.model or "None"] = st.unpriced_models.get(t.model or "None", 0) + 1
            else:
                st.money[kind] += cost
    # researcher spawns
    spawn_sessions: set[str] = set()
    find_costs: list[float] = []
    find_own: list[float] = []
    find_calls: list[int] = []
    find_tools: list[int] = []
    for jsonl_path, meta in tm.discover_subagent_transcripts(projects_dir):
        agent = meta.get("agentType")
        if agent not in ("researcher-find", "researcher-how"):
            continue
        session = jsonl_path.parent.parent.name
        spawn_sessions.add(session)
        if agent != "researcher-find":
            continue
        sub = tm.parse_subagent_transcript(jsonl_path, meta)
        if not sub or not tm.in_window(sub[0], since, until):
            continue
        total = 0.0
        own = 0.0
        for t in sub:
            c = tm.turn_cost(dataclasses.replace(t, model=HAIKU), prices)
            total += c or 0.0
            own += tm.turn_cost(t, prices) or 0.0
        find_costs.append(total)
        find_own.append(own)
        find_calls.append(len(sub))  # assistant turns = API calls, one per message.id
        find_tools.append(count_tool_calls(jsonl_path))
    zero = {r: len(stats[r].sessions - spawn_sessions) for r in roles}
    return stats, zero, (find_costs, find_calls, find_own, find_tools)


def render(stats, zero, haiku, roles_order, classes: bool = True, top_other: int = 0) -> str:
    find_costs, find_calls, find_own, find_tools = haiku
    lines: list[str] = []
    med_cost = statistics.median(find_costs) if find_costs else None
    med_calls = statistics.median(find_calls) if find_calls else None
    if med_cost is not None:
        lines.append(
            f"median find spawn at Haiku 5.5: ${med_cost:.4f}; same spawns at their own recorded model: "
            f"${statistics.median(find_own):.4f}"
        )
    for role in roles_order:
        st = stats[role]
        lines.append(f"== {role.upper()} own turns ==")
        lines.append(f"turns total: {st.total}   sessions: {len(st.sessions)}   unpriced turns: {st.unpriced}")
        lines.append(f"excluded (<synthetic>/no model/0 tokens): {st.excluded_turns} turns, {st.excluded_sessions} sessions")
        if st.unpriced_models:
            lines.append("unpriced models: " + ", ".join(f"{m} x{n}" for m, n in sorted(st.unpriced_models.items())))
        lines.append(f"{'class':<12}{'turns':>8}{'$':>14}")
        for k in KINDS:
            lines.append(f"{k:<12}{st.counts[k]:>8}{money(st.money[k]):>14}")
        lines.append(f"$ of reads (reads + tool reads): {st.read_turns} turns, {money(st.read_money)}")
        if med_cost is not None and med_calls:
            hv = med_cost * st.read_turns / med_calls
            lines.append(
                f"$ of the same reads as Haiku 5.5 spawns: {money(hv)} = median find spawn {money(med_cost)}"
                f" x {st.read_turns} read turns / {med_calls:g} API calls (assistant turns) per spawn ({len(find_costs)} find spawns;"
                f" median tool_use per spawn {statistics.median(find_tools):g}, reference only)"
            )
        else:
            lines.append("$ of the same reads as Haiku 5.5 spawns: n/a (no researcher-find spawn in the window)")
        lines.append(f"sessions with 0 researcher spawns: {zero[role]} of {len(st.sessions)}")
        if top_other > 0:
            lines.append(f"top {top_other} first commands of the other class ({role.upper()}):")
            ranked = sorted(st.other_heads.items(), key=lambda kv: (-kv[1], kv[0]))[:top_other]
            for head, n in ranked:
                lines.append(f"  {n:>6}  {head}")
        lines.append("")
    if classes:
        lines.append(TABLE_TEXT)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--role", action="append", choices=["orch", "brain", "both"], default=None)
    ap.add_argument("--projects-dir", type=Path, default=tm.DEFAULT_PROJECTS_DIR)
    ap.add_argument("--prices", type=Path, default=tm.DEFAULT_PRICES_PATH)
    ap.add_argument("--brain-title", default=None, help="extra regex for the brain's chat title")
    ap.add_argument("--top-other", type=int, default=0, metavar="N",
                    help="also list the N commonest first commands of the other class")
    args = ap.parse_args(argv)
    since0, until0 = tm.default_window()
    since = tm.parse_date_arg(args.since) if args.since else since0
    until = tm.parse_date_arg(args.until) if args.until else until0
    chosen = args.role or ["both"]
    roles = {"orch", "brain"} if "both" in chosen else set(chosen)
    order = [r for r in ("orch", "brain") if r in roles]
    prices = tm.load_prices(args.prices)
    stats, zero, haiku = collect(args.projects_dir, prices, since, until, roles, args.brain_title)
    print(f"window {since:%Y-%m-%d %H:%M} .. {until:%Y-%m-%d %H:%M}")
    print(render(stats, zero, haiku, order, top_other=args.top_other))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
