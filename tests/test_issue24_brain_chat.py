"""#24: ORCH's own report goes to the chat the dispatcher's notices go to.

The seam is `python -m tools.agentops brain-chat --issue N`, which prints the chat title
`brain_target(settings, <the issue's label names>)` returns. Settings and the label read are injected
(or monkeypatched at the module attribute the command reads), so no network and no real `gh` runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.agentops.brain_notice as brain_notice
import tools.agentops.cli as cli
import tools.agentops.core_settings as core_settings
from tools.agentops.brain_notice import run_brain_chat_command

REPO = "Rasslabsya4el/orchestration-layer"
DEFAULT_TITLE = "Оркестровый слой"
POE_TITLE = "Poe chat"
SETTINGS = {
    "repo": REPO,
    "brain_chat_title": DEFAULT_TITLE,
    "brain_chats": {"area: poe": POE_TITLE},
}


def _labels(*names: str):
    """A read_labels seam: the label names of the issue, whatever the number."""
    return lambda issue, repo: tuple(names)


def _failing_labels(issue, repo):
    raise RuntimeError(f"gh issue view {issue} failed: HTTP 504")


def test_1_area_label_mapped_in_brain_chats_gives_that_title():
    rc, text = run_brain_chat_command(5, settings=SETTINGS, read_labels=_labels("bug", "area: poe"))

    assert rc == 0
    assert text == POE_TITLE


def test_2_no_mapped_label_gives_the_default_title():
    rc, text = run_brain_chat_command(5, settings=SETTINGS, read_labels=_labels("bug", "ready-for-agent"))

    assert rc == 0
    assert text == DEFAULT_TITLE


@pytest.mark.parametrize("table", [{}, None])
def test_3_empty_brain_chats_gives_the_title(table):
    settings = {"repo": REPO, "brain_chat_title": DEFAULT_TITLE, "brain_chats": table}

    rc, text = run_brain_chat_command(5, settings=settings, read_labels=_labels("area: poe"))

    assert rc == 0
    assert text == DEFAULT_TITLE


def test_3_missing_brain_chats_key_gives_the_title():
    settings = {"repo": REPO, "brain_chat_title": DEFAULT_TITLE}

    rc, text = run_brain_chat_command(5, settings=settings, read_labels=_labels("area: poe"))

    assert rc == 0
    assert text == DEFAULT_TITLE


def test_4_failed_gh_read_is_rc_1_with_a_reason_and_no_guessed_title():
    rc, text = run_brain_chat_command(5, settings=SETTINGS, read_labels=_failing_labels)

    assert rc == 1
    assert "#5" in text and "not read" in text
    assert DEFAULT_TITLE not in text and POE_TITLE not in text


def test_4_cli_failed_read_prints_nothing_on_stdout(monkeypatch, capsys):
    monkeypatch.setattr(core_settings, "load", lambda path=None: SETTINGS)
    monkeypatch.setattr(brain_notice, "_gh_read_labels", _failing_labels)

    assert cli.main(["brain-chat", "--issue", "5"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "not read" in out.err


def test_5_cli_prints_exactly_the_title_and_a_newline(monkeypatch, capsys):
    monkeypatch.setattr(core_settings, "load", lambda path=None: SETTINGS)
    monkeypatch.setattr(brain_notice, "_gh_read_labels", _labels("area: poe"))

    assert cli.main(["brain-chat", "--issue", "5"]) == 0
    out = capsys.readouterr()
    assert out.out == POE_TITLE + "\n"
    assert out.err == ""


def test_5_the_label_read_asks_gh_for_the_labels_of_the_settings_repo(monkeypatch):
    """The real reader: `gh issue view N -R <repo> --json labels`, labels taken from its JSON."""
    import tools.agentops.gh_call as gh_call

    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = json.dumps({"labels": [{"name": "area: poe"}, {"name": "bug"}]})
        stderr = ""

    def fake_run_gh(args, **kwargs):
        calls.append(list(args))
        return Done()

    monkeypatch.setattr(gh_call, "run_gh", fake_run_gh)

    labels = brain_notice._gh_read_labels(5, REPO)

    assert calls == [["issue", "view", "5", "-R", REPO, "--json", "labels"]]
    assert labels == ("area: poe", "bug")
