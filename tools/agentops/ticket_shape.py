"""Shape check for a ticket: a plain summary block for the human, then the spec for the agent."""
from __future__ import annotations

import argparse
import re
import sys

HUMAN = "## For the human"
AGENT = "## For the agent"
FIELDS = ("What", "Why", "Plan", "Result", "Undo")
_FIELD_RE = re.compile(r"^- \*\*(\w+):\*\*[ \t]*(.*)$")
_PATH_RE = re.compile(r"[\w./-]+\.(?:py|md)\b")
_LINE_REF_RE = re.compile(r"(?<![\d\s]):\d+")


def _body_problems(body: str) -> list[str]:
    lines = body.lstrip().splitlines()
    # #1074: `Parked: <event>` lines and blank lines may stand above the human block; nothing else may.
    while lines and (not lines[0].strip() or lines[0].startswith("Parked:")):
        lines = lines[1:]
    if not lines or lines[0].strip() != HUMAN:
        return [f"body must start with the line `{HUMAN}` (only `Parked: <event>` lines may come before it)"]
    problems: list[str] = []
    end = next((i for i, l in enumerate(lines) if i and l.strip() == AGENT), None)
    if end is None:
        problems.append(f"missing the line `{AGENT}` after the human block")
        end = len(lines)
    block = lines[1:end]

    found: dict[str, tuple[int, list[str]]] = {}
    current = None
    for i, line in enumerate(block):
        m = _FIELD_RE.match(line)
        if m and m.group(1) in FIELDS:
            current = m.group(1)
            if current not in found:
                found[current] = (i, [m.group(2)])
            continue
        if line.startswith("#"):
            current = None
        elif current in found:
            found[current][1].append(line)
    last = -1
    for name in FIELDS:
        if name not in found:
            problems.append(f"missing field `- **{name}:**`")
            continue
        pos, parts = found[name]
        if not "".join(parts).strip():
            problems.append(f"empty field `- **{name}:**`")
        if pos < last:
            problems.append(f"field `- **{name}:**` is out of order")
        last = max(last, pos)

    human = "\n".join(block)
    for tok in dict.fromkeys(m.group(0) for m in _PATH_RE.finditer(human)):
        problems.append(f"file path in the human block: `{tok}`")
    for m in _LINE_REF_RE.finditer(human):
        start = human.rfind(" ", 0, m.start()) + 1
        problems.append(f"line reference in the human block: `{human[start:m.end()].strip()}`")
    return problems


_FLOW_FIX_LINE_RE = re.compile(r"^Flow fix: (yes( \(.+\))?|no \(.+\))\s*$", re.MULTILINE)
_FLOW_FIX_YES_RE = re.compile(r"^Flow fix: yes( \(.+\))?\s*$", re.MULTILINE)
FLOW_FIX_LABEL = "flow-fix"


def flow_fix_problems(body: str, labels: list[str]) -> list[str]:
    """#199: a core issue's body carries one line `Flow fix: yes | no (<why>)`; `yes` needs the label `flow-fix`."""
    if not _FLOW_FIX_LINE_RE.search(body):
        return ["add one line `Flow fix: yes | no (<why>)` to the agent block (yes -> also label `flow-fix`)"]
    if _FLOW_FIX_YES_RE.search(body) and FLOW_FIX_LABEL not in labels:
        return ["Flow fix: yes needs the label `flow-fix` in the same create call"]
    return []


def check(title: str | None, body: str | None) -> list[str]:
    problems: list[str] = []
    if title is not None:
        if not title.strip():
            problems.append("title is empty")
        if len(title) > 80:
            problems.append(f"title is {len(title)} characters, at most 80")
        for tok in re.findall(r"#\d+", title):
            problems.append(f"issue number in the title: `{tok}`")
    if body is not None:
        problems.extend(_body_problems(body))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ticket_shape")
    ap.add_argument("--title")
    ap.add_argument("--body-file", help="path, or - for stdin")
    args = ap.parse_args(argv)
    body = None
    if args.body_file is not None:
        body = sys.stdin.read() if args.body_file == "-" else open(args.body_file, encoding="utf-8").read()
    problems = check(args.title, body)
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
