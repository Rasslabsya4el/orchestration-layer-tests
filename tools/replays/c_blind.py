"""The blind judge of the C-arm comparison (issue #376 unit 5).

build_pair writes one judge prompt for one ticket: the ticket text (exactly as given), the two diffs as X and Y,
and a rubric. Which side is X and which is Y comes from ``random.Random(seed)``; the mapping goes to a key file
that is appended to and never written into the prompt.

A diff's lines are of three kinds. A body line (added, removed, context, hunk, "no newline") is never checked.
A path line (``diff --git``, ``--- a/<path>`` or ``--- /dev/null``, the ``+++`` twin, rename, copy, mode and
similarity lines) is checked for this ticket's branch names only: both sides touch the same paths. Any other
header or message line is checked for the side words, branch names of any number, commit shas and trailers.
A commit block (a ``commit <sha>`` line through the first ``diff --git`` after it, as git log -p and git show
print it) is dropped whole. This ticket's own branch names are replaced by ``<branch>`` in the kept header lines.

``leaks`` checks a scrubbed diff for what is left of those markers, and for any line that starts ``S:`` or ``C:``.
The ticket text is not scanned at all. parse_verdict reads the judge's last ``VERDICT:`` line; reveal maps the six
verdicts to S, C or tie and refuses until all six are in. The judge runs through ``judge_command``, a headless
``claude -p`` with ``--tools ""`` (no tools); the caller passes the prompt file as stdin. Reuses ``_fence_for``
from blind.py; blind.py is not changed.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from tools.replays.blind import _fence_for

JUDGE_MODEL = "claude-opus-5-5"
JUDGE_EFFORT = "xhigh"
SIX_NUMBERS = frozenset({349, 219, 135, 218, 127, 238})

#: path and mode lines of a diff. ``--- `` and ``+++ `` count only with an ``a/``, ``b/`` path or /dev/null.
_PATH_LINE = re.compile(
    r"^(?:diff --git |--- a/|--- /dev/null$|\+\+\+ b/|\+\+\+ /dev/null$|rename (?:from|to) |copy (?:from|to) "
    r"|(?:new|deleted) file mode |(?:old|new) mode |similarity index |dissimilarity index |Binary files )"
)
#: first line of a commit block as git log -p and git show print it; the block runs to the next ``diff --git``.
_COMMIT = re.compile(r"^commit [0-9a-f]{7,40}\b")
#: other header lines dropped outright when they stand outside a commit block.
_DROP = re.compile(
    r"^(?:index [0-9a-f]+\.\.[0-9a-f]+|Merge:|From [0-9a-f]{40}|From:|Date:|Subject:|Author:"
    r"|Merge (?:branch|pull request|remote)\b)",
    re.IGNORECASE,
)
#: trailers that name a co-author or a signer; dropped on any line.
_TRAILER = re.compile(r"^\s*(?:Co-Authored-By|Signed-off-by):", re.IGNORECASE)
#: a line that names a side at its start, e.g. "S: merged" or "C:".
_SIDE_LABEL = re.compile(r"^[SC]:(?=\s|$)")
#: this ticket's own branch names in a line, with word boundaries (a path segment never matches).
_BRANCH_NAME = r"(?<![\w/.\-])(?:c-{n}|i{n})(?![\w/\-])"
#: any side's branch name, for a path line.
_BRANCH_LEAK = re.compile(r"(?<![\w/.\-])(?:c-\d+|i\d+)(?![\w/\-])", re.IGNORECASE)
#: side words, trailers and shas, for an other header or message line.
_HEADER_LEAK = re.compile(
    r"\b(?:layer|ORCH|worker|baseline|Co-Authored-By|Signed-off-by)\b"
    r"|(?<![\w/.\-])(?:c-\d+|i\d+)(?![\w/\-])"
    r"|\b[0-9a-f]{40}\b",
    re.IGNORECASE,
)
_VERDICT = re.compile(r"VERDICT:\s*(X|Y|tie)(?![A-Za-z0-9_])")

RUBRIC = """## Rubric

Rate X and Y each from 1 to 5 on four points: fit to the goal, tests, scope, simplicity.
Name each defect with file:line.
Say in two sentences which one is better and why.
End with one line, exactly one of: VERDICT: X or VERDICT: Y or VERDICT: tie
"""


class KeyLocked(Exception):
    """reveal refused: the verdicts do not yet hold every expected ticket."""


def _kind(line: str) -> str:
    """"path" for a diff path or mode line, "body" for a diff body line or an empty line, else "other"."""
    if _PATH_LINE.match(line):
        return "path"
    if line == "" or line.startswith(("+", "-", " ", "@@", "\\")):
        return "body"
    return "other"


def _own_branch(number: int) -> re.Pattern:
    """This ticket's own branch names only: ``c-<number>`` and ``i<number>``."""
    return re.compile(_BRANCH_NAME.format(n=number), re.IGNORECASE)


def _scrub_diff(diff: str, number: int) -> str:
    own = _own_branch(number)
    kept: list[str] = []
    in_commit = False
    for line in diff.splitlines():
        if _COMMIT.match(line):
            in_commit = True
            continue
        if line.startswith("diff --git "):
            in_commit = False
        if in_commit or _TRAILER.match(line):
            continue
        kind = _kind(line)
        if kind == "body":
            kept.append(line)
        elif kind == "other" and _DROP.match(line):
            continue
        else:
            kept.append(own.sub("<branch>", line))
    return "\n".join(kept)


def leaks(text: str) -> list[str]:
    """What remains of the side markers in a scrubbed diff. A path line is checked for branch names of any number;
    an other header or message line for side words, branch names, commit shas and trailers; any line for a
    ``S:`` or ``C:`` start. Body lines are never checked."""
    found: list[str] = []
    for line in text.splitlines():
        label = _SIDE_LABEL.match(line)
        if label:
            found.append(label.group(0))
        kind = _kind(line)
        if kind == "path":
            found.extend(m.group(0) for m in _BRANCH_LEAK.finditer(line))
        elif kind == "other":
            found.extend(m.group(0) for m in _HEADER_LEAK.finditer(line))
    return list(dict.fromkeys(found))


def _load_key(key_path: Path) -> dict:
    if not key_path.exists():
        return {}
    data = json.loads(key_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"key file {key_path} is not a JSON object")
    return data


def _render(ticket: str, diff_x: str, diff_y: str) -> str:
    dx, dy = diff_x.rstrip("\n"), diff_y.rstrip("\n")
    fence_x, fence_y = _fence_for(dx), _fence_for(dy)
    return "\n".join([
        "# Judge: two diffs for one ticket",
        "",
        "## Ticket",
        "",
        ticket,
        "",
        "## Diff X",
        "",
        f"{fence_x}diff",
        dx,
        fence_x,
        "",
        "## Diff Y",
        "",
        f"{fence_y}diff",
        dy,
        fence_y,
        "",
        RUBRIC,
    ])


def build_pair(
    number: int,
    ticket_text: str,
    diff_s: str,
    diff_c: str,
    out_dir: Path,
    key_path: Path,
    seed: int,
) -> Path:
    """Write out_dir/judge-<number>.md and append the key {number: {"X": side, "Y": side}} to key_path.

    The ticket text goes in exactly as given. Raises ValueError, before anything is written, when the number is
    already in the key or a scrubbed diff still carries a side marker (its header lines are the brain's to fix,
    not rewritten here).
    """
    key_path = Path(key_path)
    key = _load_key(key_path)
    if str(number) in key:
        raise ValueError(f"key already holds #{number}; a second pair would overwrite its sides")
    diffs = {"S": _scrub_diff(diff_s, number), "C": _scrub_diff(diff_c, number)}
    for side, text in diffs.items():
        stray = leaks(text)
        if stray:
            raise ValueError(f"the {side} diff of #{number} still names a side: {stray}")
    order = ["S", "C"]
    random.Random(seed).shuffle(order)
    sides = {"X": order[0], "Y": order[1]}
    prompt = _render(ticket_text, diffs[sides["X"]], diffs[sides["Y"]])

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"judge-{number}.md"
    path.write_text(prompt, encoding="utf-8")
    key[str(number)] = dict(sides)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def judge_command(prompt_path: Path) -> list[str]:
    """The argv of a fresh headless judge: claude -p, Opus 5.5 at xhigh, JSON out, no tools, no agent.

    ``--tools ""`` disables every built-in tool (checked against the installed claude.exe 2.1.308 help; the
    ``--allowedTools`` flag is an allow-list and would not disable them). The caller opens prompt_path and passes
    it as stdin. Raises FileNotFoundError when it is not a file.
    """
    if not Path(prompt_path).is_file():
        raise FileNotFoundError(f"judge prompt not found: {prompt_path}")
    return [
        "claude", "-p",
        "--model", JUDGE_MODEL,
        "--effort", JUDGE_EFFORT,
        "--output-format", "json",
        "--tools", "",
    ]


def parse_verdict(result_text: str) -> str:
    """"X", "Y" or "tie" from the last VERDICT line of a judge's text; ValueError when there is none."""
    found = _VERDICT.findall(result_text)
    if not found:
        raise ValueError("no VERDICT line in the judge's output")
    return found[-1]


def reveal(
    key_path: Path,
    verdicts: dict[int, str],
    expected: frozenset[int] = SIX_NUMBERS,
) -> dict[int, str]:
    """Map each verdict letter to S, C or tie. Raises KeyLocked unless verdicts holds every expected number."""
    missing = sorted(set(expected) - set(verdicts))
    if missing:
        raise KeyLocked(f"verdicts missing for {missing}; the key stays sealed")
    key = _load_key(Path(key_path))
    revealed: dict[int, str] = {}
    for number in sorted(expected):
        verdict = verdicts[number]
        if verdict == "tie":
            revealed[number] = "tie"
        elif verdict in ("X", "Y"):
            if str(number) not in key:
                raise ValueError(f"key has no entry for #{number}")
            revealed[number] = key[str(number)][verdict]
        else:
            raise ValueError(f"verdict for #{number} is {verdict!r}, want X, Y or tie")
    return revealed
