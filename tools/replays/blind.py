"""Blind case files for the brain-role replays (issue #349 unit 6, issue #363 unit 3b).

build_case_files writes one case file: a heading, the case's owner message and
pass mark verbatim (from cases.json), then six or nine outputs shuffled under the
letters A-F or A-I. Each section holds the output's text and every recorded write.
A record with ``turns`` renders ``### Turn 1`` (its text and ``writes``) and, when
turns is 2, ``### Turn 2`` (its text2 and ``writes2``); a record without ``turns``
renders as before, and a record without ``writes2`` renders no turn-2 writes. A case with ``turn2_answer`` prints it after the pass mark, under the
heading ``**Owner answer for turn 2:**``. Nothing in the file names an arm, a run,
a session or a cost; that mapping goes to the key file, which is never committed.

The key file is a JSON object: ``entries`` (one per letter per case) and
``total_cost_usd`` (the sum of all entries' cost_usd, recomputed on every call).
Other top-level keys are kept on read-modify-write.

check_blind judges a written case file against the key: a missing or empty
letter section, an arm word in a heading, a session or run id from the key,
and a cost value from the key written out as text.
"""

from __future__ import annotations

import json
import random
import re
import shlex
from pathlib import Path

DEFAULT_CASES_PATH = Path(__file__).resolve().parent / "cases.json"
LETTERS = "ABCDEF"
LETTERS_NINE = "ABCDEFGHI"
OUTPUT_COUNTS = (len(LETTERS), len(LETTERS_NINE))
TURN_TWO_HEADING = "**Owner answer for turn 2:**"
KEY_FIELDS = ("case", "letter", "arm", "run_id", "session_id", "cost_usd", "model", "effort")
REQUIRED_OUTPUT_FIELDS = ("run_id", "arm", "session_id", "cost_usd", "model", "effort", "text")
_FENCE_RE = re.compile(r"^(`{3,})")


def _load_case(case_key: str, cases_path: Path) -> dict:
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))["cases"]
    if case_key not in cases:
        raise ValueError(f"unknown case {case_key!r} in {cases_path}")
    return cases[case_key]


def _fence_for(*parts: str) -> str:
    """A backtick fence longer than any backtick run inside the parts."""
    longest = 0
    for part in parts:
        for run in re.findall(r"`+", part):
            longest = max(longest, len(run))
    return "`" * max(3, longest + 1)


def _render_write(number: int, write: dict) -> list[str]:
    """One write: its argv (and body, when it has one) in a fenced block, then its fields.

    A write with no body (missing or None) renders only its argv. ``fields``
    (dict or list), when present, is rendered in full as a json fenced block.
    """
    argv_line = shlex.join(str(a) for a in write["argv"])
    body = write.get("body")
    body_text = "" if body is None else str(body).rstrip("\n")
    first_block = argv_line if not body_text else f"{argv_line}\n\n{body_text}"
    fence = _fence_for(first_block)
    lines = [f"### Write {number}", "", f"{fence}text", first_block, fence, ""]
    fields = write.get("fields")
    if fields is not None:
        fields_text = json.dumps(fields, ensure_ascii=False, indent=2)
        fence = _fence_for(fields_text)
        lines += [f"{fence}json", fields_text, fence, ""]
    return lines


def _render_output(letter: str, out: dict) -> str:
    lines = [f"## {letter}", ""]
    if out.get("turns") is not None:
        lines += ["### Turn 1", "", out["text"].rstrip(), ""]
    else:
        lines += [out["text"].rstrip(), ""]
    for number, write in enumerate(out.get("writes") or [], start=1):
        lines += _render_write(number, write)
    if out.get("turns") == 2:
        lines += ["### Turn 2", "", out["text2"].rstrip(), ""]
        for number, write in enumerate(out.get("writes2") or [], start=1):
            lines += _render_write(number, write)
    return "\n".join(lines)


def _render_case(
    case_key: str,
    case: dict,
    outputs: list[dict],
    order: list[int],
    letters: str,
    mark_key: str,
) -> str:
    pass_text = case[mark_key] if mark_key in case else case["pass_mark"]
    parts = [
        f"# Case {case_key}: {case['title']}",
        "",
        "## Owner message",
        "",
        case["owner_message"],
        "",
        "## Pass mark",
        "",
        pass_text,
        "",
    ]
    if "turn2_answer" in case:
        parts += [f"## {TURN_TWO_HEADING}", "", case["turn2_answer"], ""]
    for position, letter in enumerate(letters):
        parts.append(_render_output(letter, outputs[order[position]]))
    return "\n".join(parts).rstrip("\n") + "\n"


def _read_key(key_path: Path) -> dict:
    """The key object; an absent file is an empty key. Other top-level keys are kept."""
    if not key_path.exists():
        return {"entries": [], "total_cost_usd": 0.0}
    data = json.loads(key_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"key file {key_path} is not a JSON object with an entries list")
    return data


def _write_key(key_path: Path, data: dict, case_key: str, new_entries: list[dict]) -> None:
    entries = [e for e in data["entries"] if e.get("case") != case_key] + new_entries
    data = dict(data)
    data["entries"] = entries
    data["total_cost_usd"] = float(sum(float(e["cost_usd"]) for e in entries))
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_case_files(
    case_key: str,
    outputs: list[dict],
    out_dir: Path,
    key_path: Path,
    seed: int | None = None,
    cases_path: Path = DEFAULT_CASES_PATH,
    mark_key: str = "pass_mark",
) -> Path:
    """Write case-<case_key>.md into out_dir and its letter key into key_path.

    Six outputs go under A-F, nine (three arms x three runs) under A-I. The
    pass mark printed is ``cases.json``'s ``mark_key`` when the case has that key,
    else its ``pass_mark``. Returns the case file path. Raises ValueError, before
    anything is written, when the count is neither six nor nine, one has empty
    text, one lacks a required field (model and effort among them), or one has
    turns not 1 or 2 (turns 2 without a text2).
    """
    if len(outputs) not in OUTPUT_COUNTS:
        raise ValueError(
            f"need {len(LETTERS)} or {len(LETTERS_NINE)} outputs, got {len(outputs)}"
        )
    letters = LETTERS_NINE[: len(outputs)]
    for index, out in enumerate(outputs):
        missing = [f for f in REQUIRED_OUTPUT_FIELDS if f not in out]
        if missing:
            raise ValueError(f"output {index} lacks {missing}")
        if not isinstance(out["text"], str) or not out["text"].strip():
            raise ValueError(f"output {index} has empty text")
        turns = out.get("turns")
        if turns not in (None, 1, 2):
            raise ValueError(f"output {index} has turns {turns!r}, want 1 or 2")
        if turns == 2 and (not isinstance(out.get("text2"), str) or not out["text2"].strip()):
            raise ValueError(f"output {index} has turns 2 and no text2")
    case = _load_case(case_key, Path(cases_path))
    key_path = Path(key_path)
    key_data = _read_key(key_path)

    order = list(range(len(outputs)))
    random.Random(seed).shuffle(order)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    case_file = out_dir / f"case-{case_key}.md"
    case_file.write_text(
        _render_case(case_key, case, outputs, order, letters, mark_key), encoding="utf-8"
    )

    new_entries = []
    for position, letter in enumerate(letters):
        out = outputs[order[position]]
        values = {
            "case": case_key,
            "letter": letter,
            "arm": out["arm"],
            "run_id": out["run_id"],
            "session_id": out["session_id"],
            "cost_usd": out["cost_usd"],
            "model": out["model"],
            "effort": out["effort"],
        }
        new_entries.append({field: values[field] for field in KEY_FIELDS})
    _write_key(key_path, key_data, case_key, new_entries)
    return case_file


def _outside_fence(text: str) -> list[tuple[str, bool]]:
    """Each line with True when it lies outside a code fence (a fence line counts as inside)."""
    marked: list[tuple[str, bool]] = []
    fence: str | None = None
    for line in text.splitlines():
        if fence is None:
            opener = _FENCE_RE.match(line)
            if opener:
                fence = opener.group(1)
                marked.append((line, False))
            else:
                marked.append((line, True))
        else:
            if line.strip() == fence:
                fence = None
            marked.append((line, False))
    return marked


def _sections(text: str) -> dict[str, list[str]]:
    """Map each level-2 heading outside a code fence to the lines under it."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line, outside in _outside_fence(text):
        if outside and line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _headings(text: str) -> list[str]:
    return [line for line, outside in _outside_fence(text) if outside and line.startswith("#")]


def _arm_word_re(entries: list[dict], case_key: str | None) -> re.Pattern:
    """Whole words that name an arm: old and new, plus every arm the key holds for the case."""
    words = {"old", "new"}
    for entry in entries:
        arm = entry.get("arm")
        if isinstance(arm, str) and arm.strip() and (case_key is None or entry.get("case") == case_key):
            words.add(arm.strip())
    alternation = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"\b({alternation})\b", re.IGNORECASE)


def _expected_letters(case_key: str | None, entries: list[dict], sections: dict) -> str:
    """The letters the file must hold: those the key lists for its case, else A-F or A-I by sections."""
    own = {e.get("letter") for e in entries if case_key is not None and e.get("case") == case_key}
    if own:
        return "".join(letter for letter in LETTERS_NINE if letter in own)
    nine = any(letter in sections for letter in LETTERS_NINE[len(LETTERS):])
    return LETTERS_NINE if nine else LETTERS


def check_blind(case_file: Path, key_path: Path) -> list[str]:
    """Return the problems found in a case file; an empty list means it is blind."""
    problems: list[str] = []
    text = Path(case_file).read_text(encoding="utf-8")
    sections = _sections(text)
    key_path = Path(key_path)
    header = re.match(r"# Case ([^:\n]+):", text)
    case_key = header.group(1).strip() if header else None
    entries = _read_key(key_path)["entries"] if key_path.exists() else []
    expected = _expected_letters(case_key, entries, sections)
    for letter in expected:
        if letter not in sections:
            problems.append(f"section {letter} missing")
        elif not "\n".join(sections[letter]).strip():
            problems.append(f"section {letter} empty")
    for letter in LETTERS_NINE:
        if letter in sections and letter not in expected:
            problems.append(f"section {letter} has no output in the key")
    arm_re = _arm_word_re(entries, case_key)
    for heading in _headings(text):
        match = arm_re.search(heading)
        if match:
            problems.append(f"arm word {match.group(0)!r} in heading {heading!r}")

    if not key_path.exists():
        problems.append(f"key file missing, cannot check ids: {key_path}")
        return problems
    ids: set[str] = set()
    costs: set[str] = set()
    for entry in entries:
        for field in ("session_id", "run_id"):
            value = entry.get(field)
            if isinstance(value, str) and value:
                ids.add(value)
        if entry.get("cost_usd") is not None:
            costs.add(str(entry["cost_usd"]))
    for ident in sorted(ids):
        if ident in text:
            problems.append(f"key id {ident} found in case file")
    for cost in sorted(costs):
        if re.search(rf"\b{re.escape(cost)}\b", text):
            problems.append(f"key cost {cost} found in case file")
    return problems
