"""#79: a core issue the dispatcher writes opens with a Russian What line for the human. The
English `what` stays the title and the duplicate key (`find_open_core_issue`). Seams: the writer
seam the dispatcher calls (`core_issue_writer` kwargs, `_default_core_issue_writer`), and
`core_issue.write_core_issue` / `find_open_core_issue` on a fake `gh` (no network)."""

from __future__ import annotations

import re
from pathlib import Path

from tools.agentops import dispatch as dispatch_mod
from tools.agentops.core_issue import find_open_core_issue, write_core_issue
from tools.agentops.dispatch import FAILED_TICKS_ALERT_SECONDS, ORCH_DOC, Dispatcher
from tests.test_agentops_dispatch import FakeClaude, FakeGh
from tests.test_agentops_dispatch_core_issue import (
    SETTINGS, FakeCoreGh, _dispatcher, _end_one_no_checkpoint, _manifest_path,
)
from tests._area_rule import _no_area_rule  # noqa: F401

CYRILLIC = re.compile(r"[Ѐ-ӿ]")
WHAT_CHECKPOINT = f"{ORCH_DOC}: Working an issue -- Checkpoint"
WHAT_TICKS = f"{ORCH_DOC}: every dispatcher tick fails"
RU_CHECKPOINT = "сессия исполнителя закончилась, оставив задачу без итоговой или отложенной отметки о ходе работы"
RU_TICKS = "диспетчер подряд не может выполнять свою работу: каждая попытка падает"


def _what_line(body: str) -> str:
    match = re.search(r"^- \*\*What:\*\* (.*)$", body, re.MULTILINE)
    assert match, body[:300]
    return match.group(1)


def _write_kwargs(tmp_path: Path, core_gh: FakeCoreGh, **extra) -> dict:
    kwargs = dict(role="dispatcher", kind="broke", what=WHAT_CHECKPOINT, error="ORCH session s1 ended",
                  log="", did="parked", session="s1", caught_by="later-run", gh_runner=core_gh,
                  settings=SETTINGS, cwd=tmp_path, manifest_path=_manifest_path(tmp_path))
    kwargs.update(extra)
    return kwargs


def test_write_core_issue_what_line_shows_the_russian_what_human_and_title_stays_english(tmp_path):
    core_gh = FakeCoreGh()
    write_core_issue(**_write_kwargs(tmp_path, core_gh, what_human=RU_CHECKPOINT))

    issue = core_gh.issues[501]
    assert _what_line(issue["body"]) == RU_CHECKPOINT
    assert "Working an issue -- Checkpoint" in issue["title"]
    assert not CYRILLIC.search(issue["title"])


def test_write_core_issue_without_what_human_keeps_the_english_what_line(tmp_path):
    core_gh = FakeCoreGh()
    write_core_issue(**_write_kwargs(tmp_path, core_gh))

    # today's line: `_plain` shortens the manifest path to its bare name
    assert _what_line(core_gh.issues[501]["body"]) == "orch: Working an issue -- Checkpoint"


def test_same_error_twice_with_an_open_issue_takes_the_same_path_and_opens_no_second(tmp_path):
    core_gh = FakeCoreGh()
    first = write_core_issue(**_write_kwargs(tmp_path, core_gh, what_human=RU_CHECKPOINT))
    same = find_open_core_issue(kind="broke", what=WHAT_CHECKPOINT, gh_runner=core_gh,
                                settings=SETTINGS, cwd=tmp_path)
    # the lookup key is the English `what`: the Russian line would miss the open issue
    assert find_open_core_issue(kind="broke", what=RU_CHECKPOINT, gh_runner=core_gh,
                                settings=SETTINGS, cwd=tmp_path) is None

    second = write_core_issue(**_write_kwargs(tmp_path, core_gh, what_human=RU_CHECKPOINT, same=same))

    assert same == first.created_issue == 501
    assert second.comment_added is True and second.created_issue is None
    creates = [c for c in core_gh.calls if c[:2] == ["issue", "create"] and "core-issue" in c]
    assert len(creates) == 1
    assert [n for n, _ in core_gh.comments] == [501]


def test_default_writer_passes_what_human_through_and_looks_up_the_english_what(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_find(**kwargs):
        seen["find"] = kwargs
        return 7

    def fake_write(**kwargs):
        seen["write"] = kwargs

    monkeypatch.setattr(dispatch_mod, "find_open_core_issue", fake_find)
    monkeypatch.setattr(dispatch_mod, "write_core_issue", fake_write)

    dispatch_mod._default_core_issue_writer(role="dispatcher", kind="broke", what=WHAT_TICKS,
                                            error="e", log="", did="kept retrying every tick",
                                            session="-", caught_by="later-run",
                                            what_human=RU_TICKS)

    assert seen["find"]["what"] == WHAT_TICKS
    assert seen["write"]["what"] == WHAT_TICKS
    assert seen["write"]["what_human"] == RU_TICKS
    assert seen["write"]["same"] == 7


def test_checkpoint_caller_passes_a_russian_what_human_to_the_writer_seam(tmp_path):
    calls: list[dict] = []
    d = Dispatcher(claude=FakeClaude(), gh=FakeGh(), events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   core_issue_writer=lambda **kw: calls.append(kw))
    d._write_no_checkpoint_core_issue(9, "s1")

    assert len(calls) == 1
    assert calls[0]["what"] == WHAT_CHECKPOINT
    assert CYRILLIC.search(calls[0]["what_human"])


def test_checkpoint_caller_end_to_end_issue_body_what_line_is_russian(tmp_path):
    claude, gh, core_gh = FakeClaude(), FakeGh(), FakeCoreGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, core_gh=core_gh)

    _end_one_no_checkpoint(tmp_path, claude, gh, d, 9)
    _end_one_no_checkpoint(tmp_path, claude, gh, d, 9)  # the same defect again: a comment

    creates = [c for c in core_gh.calls if c[:2] == ["issue", "create"] and "core-issue" in c]
    assert len(creates) == 1
    body = core_gh.issues[501]["body"]
    assert CYRILLIC.search(_what_line(body))
    assert len(core_gh.comments) == 1


def test_failed_ticks_caller_passes_a_russian_what_human_once(tmp_path):
    now = [1000.0]
    calls: list[dict] = []
    d = Dispatcher(claude=FakeClaude(), gh=FakeGh(), events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   core_issue_writer=lambda **kw: calls.append(kw), clock=lambda: now[0])

    d._note_failed_tick(RuntimeError("boom"))
    now[0] += FAILED_TICKS_ALERT_SECONDS
    d._note_failed_tick(RuntimeError("boom"))
    d._note_failed_tick(RuntimeError("boom"))

    assert len(calls) == 1
    assert calls[0]["what"] == WHAT_TICKS
    assert CYRILLIC.search(calls[0]["what_human"])
