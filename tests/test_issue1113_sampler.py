"""#1113 GitHub GraphQL budget sampler: fake `run_gh`, no real gh, no PoB, no console window."""

import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import gh_budget_sampler as sampler  # noqa: E402

PAYLOAD = {"data": {"rateLimit": {"limit": 5000, "remaining": 4700, "used": 300,
                                  "resetAt": "2026-10-15T13:00:00Z"}}}


def fake_ok(args, **kwargs):
    return SimpleNamespace(returncode=0, stdout=json.dumps(PAYLOAD), stderr="", args=args)


def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def test_once_appends_one_line_with_fields(tmp_path, monkeypatch):
    calls = []

    def fake(args, **kwargs):
        calls.append(args)
        return fake_ok(args, **kwargs)

    monkeypatch.setattr(sampler, "run_gh", fake)
    out = tmp_path / "budget.jsonl"
    assert sampler.main(["--out", str(out), "--once"]) == 0

    rows = read_lines(out)
    assert len(rows) == 1
    assert set(rows[0]) == {"t", "used", "remaining", "limit", "reset_at"}
    assert (rows[0]["used"], rows[0]["remaining"], rows[0]["limit"]) == (300, 4700, 5000)
    assert rows[0]["reset_at"] == "2026-10-15T13:00:00Z"
    # the local time carries seconds and an offset
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", rows[0]["t"])
    assert calls[0][:2] == ["api", "graphql"]
    assert "rateLimit { limit remaining used resetAt }" in calls[0][-1]


def test_failed_read_appends_error_line_and_does_not_raise(tmp_path, monkeypatch):
    def failing(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="",
                               stderr="HTTP 403: API rate limit exceeded\nsecond line\n", args=args)

    monkeypatch.setattr(sampler, "run_gh", failing)
    out = tmp_path / "budget.jsonl"
    assert sampler.main(["--out", str(out), "--once"]) == 0
    rows = read_lines(out)
    assert len(rows) == 1
    assert rows[0]["error"] == "HTTP 403: API rate limit exceeded"
    assert "t" in rows[0] and "used" not in rows[0]

    def raising(args, **kwargs):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(sampler, "run_gh", raising)
    assert sampler.main(["--out", str(out), "--once"]) == 0
    assert read_lines(out)[-1]["error"] == "gh not found"


SEVEN = [
    {"t": "2026-10-15T10:05:00+03:00", "used": 100, "reset_at": "R1"},
    {"t": "2026-10-15T10:15:00+03:00", "used": 150, "reset_at": "R1"},
    {"t": "2026-10-15T10:25:00+03:00", "used": 220, "reset_at": "R1"},
    {"t": "2026-10-15T10:55:00+03:00", "used": 300, "reset_at": "R1"},
    {"t": "2026-10-15T11:05:00+03:00", "used": 10, "reset_at": "R2"},
    {"t": "2026-10-15T11:20:00+03:00", "used": 40, "reset_at": "R2"},
    {"t": "2026-10-15T11:50:00+03:00", "used": 90, "reset_at": "R2"},
]


def test_hourly_points_two_windows_exact_numbers():
    # window R1: 300 - 100 = 200; window R2: 90 - 10 = 80
    assert sampler.hourly_points(SEVEN) == [
        ("2026-10-15T10:05:00+03:00", 200),
        ("2026-10-15T11:05:00+03:00", 80),
    ]


def test_summary_prints_windows_with_human_durations(tmp_path, capsys):
    path = tmp_path / "budget.jsonl"
    rows = SEVEN[:3] + [{"t": "2026-10-15T10:40:00+03:00", "error": "HTTP 502"}] + SEVEN[3:]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    assert sampler.main(["--summary", str(path)]) == 0
    out = capsys.readouterr().out
    assert "200" in out and "80" in out
    assert "50 min" in out  # R1: 10:05 to 10:55
    assert "45 min" in out  # R2: 11:05 to 11:50
    assert "errors: 1" in out  # the error line is counted, not summed into a window
    for match in re.finditer(r"(?<![\d.:+-])(\d+)\s*s\b", out):
        assert int(match.group(1)) <= 90, out


def test_loop_is_bounded_by_hours(tmp_path, monkeypatch):
    calls = []

    def fake(args, **kwargs):
        calls.append(args)
        return fake_ok(args, **kwargs)

    monkeypatch.setattr(sampler, "run_gh", fake)
    out = tmp_path / "budget.jsonl"
    started = time.monotonic()
    assert sampler.main(["--out", str(out), "--every-minutes", "0.01", "--hours", "0.0005"]) == 0
    elapsed = time.monotonic() - started
    assert 1 <= len(read_lines(out)) == len(calls)
    assert elapsed < 10
