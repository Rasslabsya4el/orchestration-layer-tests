"""tools/agentops/metering.py: one JSON line per run into artifacts/runs/agentops.jsonl
(requirement 2), with the model's credit multiplier from the owner's codex.sale table."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.agentops.metering import record_run
from tools.agentops.models import CREDIT_MULTIPLIERS


def test_record_run_appends_one_jsonl_line(tmp_path: Path):
    log = tmp_path / "agentops.jsonl"
    record_run(kind="worker", model="gpt-5.6-terra", outcome="finished", wall_s=723.4,
              tokens_in=1000, tokens_out=400, issue=46, log_path=log)
    record_run(kind="research", model="deepseek-flash", outcome="accepted", wall_s=90.0,
              tokens_in=200, tokens_out=80, issue=46, log_path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "worker"
    assert first["model"] == "gpt-5.6-terra"
    assert first["issue"] == 46
    assert first["tokens_in"] == 1000
    assert first["tokens_out"] == 400
    assert first["outcome"] == "finished"
    assert first["credits_multiplier"] == CREDIT_MULTIPLIERS["gpt-5.6-terra"]
    assert "ts" in first and isinstance(first["ts"], int)

    second = json.loads(lines[1])
    assert second["credits_multiplier"] == CREDIT_MULTIPLIERS["deepseek-flash"]


def test_credit_multipliers_match_the_owners_table_2026_09_26():
    assert CREDIT_MULTIPLIERS == {"deepseek-flash": 0.3, "gpt-6-luna": 0.9, "gpt-5.6-terra": 1.5}


@pytest.fixture
def run_log_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Issue #91: this test's own redirect of the default run log (no tests/conftest.py needed)."""
    log = tmp_path / "agentops.jsonl"
    monkeypatch.setattr("tools.agentops.metering.DEFAULT_LOG", log)
    return log


def test_no_test_writes_the_real_run_log(run_log_in_tmp: Path):
    """Issue #256: a `record_run` with no `log_path` inside the suite lands in a temporary log,
    never in the real `artifacts/runs/agentops.jsonl`. Issue #91: the redirect is this test's own
    fixture, so the test passes in a copy of the core that has no tests/conftest.py."""
    from tools.agentops import metering

    real = Path(__file__).resolve().parents[1] / "artifacts" / "runs" / "agentops.jsonl"
    assert metering.DEFAULT_LOG.resolve() != real.resolve()
    before = real.stat().st_size if real.exists() else None

    record_run(kind="research", model="gpt-6-luna", outcome="no reddit.com URL", wall_s=0.0,
               tokens_in=500, tokens_out=300, session="sess1")

    after = real.stat().st_size if real.exists() else None
    assert after == before
    assert json.loads(metering.DEFAULT_LOG.read_text(encoding="utf-8").splitlines()[-1])["session"] == "sess1"
