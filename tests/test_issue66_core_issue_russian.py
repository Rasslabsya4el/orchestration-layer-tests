"""#66 row 5: a core issue written by ``core_issue`` carries Russian text in each of the five fields of
``## For the human`` (the field names stay English) and still passes ``ticket_shape.check``. The seam is
the issue the module sends to ``gh`` (title and body), read back through the fake from
``test_agentops_dispatch_core_issue``; no network."""
from __future__ import annotations

import re
from pathlib import Path

from tools.agentops import core_issue
from tools.agentops.dispatch import ORCH_DOC
from tools.agentops.ticket_shape import check
from tests.test_agentops_dispatch_core_issue import SETTINGS, FakeCoreGh, _manifest_path

CYR = re.compile(r"[а-яА-ЯёЁ]")
FIELDS = ["What", "Why", "Plan", "Result", "Undo"]
# the caller's own `what` is Russian here: the What field carries the caller's text as given
RU_WHAT = f"{ORCH_DOC}: работа с задачей, сессия завершилась без чекпоинта"


def _write(tmp_path: Path) -> FakeCoreGh:
    gh = FakeCoreGh()
    core_issue.write_core_issue(
        role="dispatcher", kind="broke", what=RU_WHAT, error="ORCH session S1 ended holding #9",
        log="", did="parked", session="S1", caught_by="suite",
        gh_runner=gh, settings=SETTINGS, cwd=tmp_path, manifest_path=_manifest_path(tmp_path),
    )
    return gh


def _issue_with_label(gh: FakeCoreGh, label: str) -> dict:
    (issue,) = [i for i in gh.issues.values() if label in i["labels"]]
    return issue


def _human_fields(body: str) -> dict[str, str]:
    block = body.split("## For the human", 1)[1].split("## For the agent", 1)[0]
    return {m.group(1): m.group(2) for m in re.finditer(r"^- \*\*(\w+):\*\*(.*)$", block, re.M)}


def test_core_issue_human_block_has_russian_text_in_all_five_fields(tmp_path):
    gh = _write(tmp_path)
    issue = _issue_with_label(gh, "core-issue")

    fields = _human_fields(issue["body"])

    assert list(fields) == FIELDS
    for name in FIELDS:
        assert CYR.search(fields[name]), f"{name} line has no Cyrillic: {fields[name]!r}"


def test_core_issue_still_passes_ticket_shape(tmp_path):
    gh = _write(tmp_path)
    issue = _issue_with_label(gh, "core-issue")

    assert check(issue["title"], issue["body"]) == []


def test_core_epic_parent_human_block_is_russian_too(tmp_path):
    gh = _write(tmp_path)
    parent = _issue_with_label(gh, "core-epic")

    fields = _human_fields(parent["body"])

    assert list(fields) == FIELDS
    assert all(CYR.search(fields[name]) for name in FIELDS)
    assert check(parent["title"], parent["body"]) == []
