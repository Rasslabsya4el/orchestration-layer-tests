"""#180: the owner's GPT research (C:\\Coding\\gpt-research) reads three top-level literals of tools/agentops/research.py
by name, with ast and no import: MODEL_ALIASES, TAIL and TAIL_NO_REDDIT. This test fails when one of them stops being
a top-level literal assignment, or when the model aliases change, so the outside reader is never broken silently.
The check takes source text; the mutated copies are written to a tmp dir and read back, nothing in the repo changes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

RESEARCH_PY = Path(__file__).resolve().parents[1] / "tools" / "agentops" / "research.py"
READER = "the owner's C:\\Coding\\gpt-research reads this name by ast (load_model_aliases / load_prompt_tails)"
NAMES = ("MODEL_ALIASES", "TAIL", "TAIL_NO_REDDIT")
EXPECTED_ALIASES = {"terra": "gpt-5.6-terra", "luna": "gpt-6-luna"}


def reader_contract_problems(source: str) -> list[str]:
    """What a reader that does ast.literal_eval on each top-level assignment of these names would not get."""
    values: dict[str, list[ast.expr]] = {name: [] for name in NAMES}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            targets = [node.target.id]
        else:
            continue
        for name in targets:
            if name in values:
                values[name].append(node.value)
    problems: list[str] = []
    literals: dict[str, object] = {}
    for name in NAMES:
        nodes = values[name]
        if len(nodes) != 1:
            problems.append(f"{name}: {len(nodes)} top-level assignments, want exactly 1")
            continue
        try:
            literals[name] = ast.literal_eval(nodes[0])
        except (ValueError, TypeError, SyntaxError) as exc:
            problems.append(f"{name}: value is not a literal ({exc})")
    if "MODEL_ALIASES" in literals and literals["MODEL_ALIASES"] != EXPECTED_ALIASES:
        problems.append(f"MODEL_ALIASES is {literals['MODEL_ALIASES']!r}, want {EXPECTED_ALIASES!r}")
    for name in ("TAIL", "TAIL_NO_REDDIT"):
        if name in literals and not isinstance(literals[name], str):
            problems.append(f"{name} is not a string")
    return problems


def check_reader_contract(source: str) -> None:
    problems = reader_contract_problems(source)
    assert not problems, f"{READER}: {'; '.join(problems)}"


def test_research_py_keeps_the_reader_contract():
    check_reader_contract(RESEARCH_PY.read_text(encoding="utf-8"))


MAIN_ALIASES = '{"terra": "gpt-5.6-terra", "luna": "gpt-6-luna"}'


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        ("MODEL_ALIASES = " + MAIN_ALIASES, "MODELS = " + MAIN_ALIASES, "renamed to MODELS"),
        ("MODEL_ALIASES = " + MAIN_ALIASES,
         'MODEL_ALIASES = dict(terra="gpt-5.6-terra", luna="gpt-6-luna")', "assigned from a call"),
        ("MODEL_ALIASES = " + MAIN_ALIASES, 'MODEL_ALIASES = {"terra": "gpt-5.6-terra", "luna": "gpt-5.6-luna"}',
         "a model renamed"),
    ],
)
def test_a_renamed_or_called_model_aliases_fails_the_contract(tmp_path: Path, old: str, new: str, why: str):
    source = RESEARCH_PY.read_text(encoding="utf-8")
    assert source.count(old) == 1, why
    copy = tmp_path / "research.py"
    copy.write_text(source.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(AssertionError) as excinfo:
        check_reader_contract(copy.read_text(encoding="utf-8"))
    assert "gpt-research reads this name by ast" in str(excinfo.value), why
    assert "MODEL_ALIASES" in str(excinfo.value), why
