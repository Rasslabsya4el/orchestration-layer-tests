import json
from datetime import datetime, timedelta, timezone

import pytest

from tools.agentops import machine_mode as mm
from tools.agentops.cli import main


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "m"))
    monkeypatch.setattr(mm, "run_first_pass", lambda mode: None)  # a test never freezes real processes
    return tmp_path / "m"


def test_two_roots_share_one_file(tmp_path, monkeypatch, capsys, mdir):
    for root in ("repoA", "repoB"):
        (tmp_path / root).mkdir()
        monkeypatch.chdir(tmp_path / root)
        assert main(["machine", "play"]) == 0
        assert main(["machine", "--current"]) == 0
    assert capsys.readouterr().out.split() == ["play"] * 4
    text = (mdir / "machine_mode.json").read_text()
    assert set(json.loads(text)) == {"mode", "since"}
    assert "repo" not in text and str(tmp_path) not in text


def test_absent_is_normal_unparsable_exits_nonzero(mdir, capsys):
    assert main(["machine", "--current"]) == 0
    assert capsys.readouterr().out.strip() == "normal"
    mdir.mkdir(parents=True)
    (mdir / "machine_mode.json").write_text("{not json")
    assert main(["machine", "--current"]) != 0
    with pytest.raises(mm.MachineModeUnreadable):
        mm.current_mode()


def test_stood_seconds_two_spans():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def at(m):
        return t0 + timedelta(minutes=m)

    mm.set_mode("play", at(10))
    mm.set_mode("normal", at(20))
    mm.set_mode("play", at(40))
    mm.set_mode("normal", at(45))
    assert mm.stood_seconds(at(0), at(60)) == 15 * 60
    assert mm.stood_seconds(at(15), at(42)) == 5 * 60 + 2 * 60
    assert mm.stood_seconds(at(20), at(40)) == 0
    assert mm.stood_seconds(at(0), at(5)) == 0
    assert mm.stood_seconds(at(50), at(60)) == 0
    mm.set_mode("play", at(70))
    assert mm.stood_seconds(at(60), at(100), now=at(80)) == 10 * 60
