"""Issue #363 unit 3a: the launcher's second turn.

No network and no real claude: ``cfg.claude_bin`` is a python file that prints stream-json like the real one, takes
both the first form (``-p <message> --agent ...``) and the resume form (``--resume <id> -p <answer>``), and appends one
line per call (its argv) to ``FAKE_CALLS``. The turn texts come from ``FAKE_TEXTS1`` / ``FAKE_TEXTS2`` (JSON lists of
the main thread's text blocks). The cases file is the test's own (a ``turn2_answer`` key, or none).
"""

import json
import re
import textwrap
from pathlib import Path

import pytest

from tools.replays import launcher
from tools.replays.launcher import Config, build_resume_command, finish, run_all, run_one

FAKE_CLAUDE = textwrap.dedent(
    r'''
    import json, os, re, sys, time
    from pathlib import Path

    argv = sys.argv[1:]
    cwd = os.getcwd()
    resume = "--resume" in argv
    with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")
    if resume:
        sid = os.environ.get("FAKE_NEW_SID") or argv[argv.index("--resume") + 1]
        texts = json.loads(os.environ.get("FAKE_TEXTS2", '["second answer"]'))
        leak = os.environ.get("FAKE_LEAK2") == "1"
        cost = float(os.environ.get("FAKE_COST2", "0.5"))
        sleep = float(os.environ.get("FAKE_SLEEP2", "0"))
    else:
        sid = "sess-" + Path(cwd).name
        texts = json.loads(os.environ.get("FAKE_TEXTS1", '["first answer"]'))
        leak = os.environ.get("FAKE_LEAK1") == "1"
        cost = float(os.environ.get("FAKE_COST1", "1.5"))
        sleep = 0.0
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "effort": "high"}), flush=True)
    state = os.environ.get("REPLAY_STATE_DIR")
    if state and os.environ.get("FAKE_WRITE"):
        with open(os.path.join(state, "writes.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": ["issue", "create"], "body": "turn 2" if resume else "turn 1"}) + "\n")
    folder = Path(os.environ["FAKE_PROJECTS"]) / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    folder.mkdir(parents=True, exist_ok=True)
    recs = [{"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "see the parity page" if leak else "nothing"}]}},
        {"type": "assistant", "message": {"id": "m" + sid + str(resume), "model": "claude-opus-5-5",
                                          "content": [{"type": "text", "text": "x"}]}}]
    with open(folder / (sid + ".jsonl"), "a", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in recs) + "\n")
    content = []
    for text in texts:
        content += [{"type": "text", "text": text}, {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]
    print(json.dumps({"type": "assistant", "message": {"content": content}}), flush=True)
    time.sleep(sleep)
    print(json.dumps({"type": "result", "subtype": "success", "session_id": sid, "total_cost_usd": cost}), flush=True)
    '''
)

ANSWER = "Да, делай как предложил, без вопросов."


def _write_cases(path: Path, with_answer: bool = True) -> None:
    case = {"title": "t", "repo": "C:/Coding/orchestration-layer", "commit": "0" * 40,
            "moment_utc": "2026-10-23T15:15:00Z", "owner_message": "first message", "pass_mark": "p",
            "leak_markers": ["parity"]}
    if with_answer:
        case["turn2_answer"] = ANSWER
    path.write_text(json.dumps({"cases": {"a": case}}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_PROJECTS", str(projects))
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    for name in launcher.TOKEN_VARS:
        monkeypatch.delenv(name, raising=False)

    def fake_tree(case, arm, n, base_dir, role_repo, draft_dir):
        tree = Path(base_dir) / f"{case['key']}-{arm}-{n}"
        tree.mkdir(parents=True)
        return tree

    monkeypatch.setattr(launcher, "make_run_tree", fake_tree)
    (tmp_path / "harness" / "config").mkdir(parents=True)
    (tmp_path / "harness" / "config" / "orchestration.json").write_text('{"targets": []}', encoding="utf-8")
    _write_cases(tmp_path / "cases.json")
    return Config(
        claude_bin=str(fake), role_repo=tmp_path / "role", draft_dir=tmp_path / "draft", runs_dir=tmp_path / "runs",
        trees_dir=tmp_path / "trees", harness_root=tmp_path / "harness", projects_dir=projects,
        cases_path=tmp_path / "cases.json", state_dir=tmp_path / "state", timeout_s=60)


def _calls(cfg) -> list[list[str]]:
    path = cfg.runs_dir.parent / "calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_question_in_final_block_sends_the_answer_on_resume(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["thinking", "Should I open the issue?"]))
    record = run_one("a", "old", 1, cfg)
    calls = _calls(cfg)
    assert len(calls) == 2
    first, second = calls
    assert "--resume" not in first and _arg(first, "-p") == "first message"
    assert _arg(second, "--resume") == record["session_id"] == "sess-" + Path(record["tree"]).name
    assert _arg(second, "-p") == ANSWER
    assert "--agent" not in second
    for flag in ("--model", "--effort", "--permission-mode", "--output-format"):
        assert _arg(second, flag) == _arg(first, flag)
    assert "--verbose" in second
    assert record["turns"] == 2
    assert record["text"] == "thinking\n\nShould I open the issue?"  # turn 1's text stays as it was
    assert record["text2"] == "second answer"
    assert record["cost_usd"] == pytest.approx(2.0) and record["cost2"] == pytest.approx(0.5)
    assert record["cost1"] == pytest.approx(1.5)
    assert record["cost_known"] is True and not record["void"] and "turn2_error" not in record
    state = Path(record["state_dir"])
    assert (state / "stream2.jsonl").is_file() and (state / "stderr2.txt").is_file()
    assert json.loads((cfg.runs_dir / "a-old-1.json").read_text("utf-8"))["text2"] == "second answer"


def test_no_question_mark_means_one_call(cfg):
    record = run_one("a", "old", 1, cfg)
    assert len(_calls(cfg)) == 1
    assert record["turns"] == 1 and "text2" not in record and "cost2" not in record
    assert record["cost_usd"] == pytest.approx(1.5)


def test_question_only_in_an_early_block_means_one_call(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["Which one?", "I will go with the first."]))
    record = run_one("a", "old", 1, cfg)
    assert len(_calls(cfg)) == 1
    assert record["turns"] == 1 and "text2" not in record


def test_case_without_turn2_answer_gets_no_second_turn(cfg, monkeypatch):
    _write_cases(cfg.cases_path, with_answer=False)
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["Should I open the issue?"]))
    record = run_one("a", "old", 1, cfg)
    assert len(_calls(cfg)) == 1
    assert record["turns"] == 1 and "text2" not in record


def test_leak_marker_only_in_the_second_turn_voids_the_run(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["Should I open the issue?"]))
    monkeypatch.setenv("FAKE_LEAK2", "1")
    record = run_one("a", "old", 1, cfg)
    assert len(_calls(cfg)) == 2
    assert record["void"] is True and record["void_reason"] == "leak marker"
    assert [h["marker"] for h in record["hits"]] == ["parity"]


def test_leak_marker_in_a_new_session_file_of_the_second_turn_voids_the_run(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["Should I open the issue?"]))
    monkeypatch.setenv("FAKE_LEAK2", "1")
    monkeypatch.setenv("FAKE_NEW_SID", "sess-resumed")
    record = run_one("a", "old", 1, cfg)
    assert record["void"] is True and record["void_reason"] == "leak marker"
    assert record["transcript2"].endswith("sess-resumed.jsonl")
    assert record["session_id2"] == "sess-resumed" and record["turns"] == 2


def test_clean_second_turn_in_a_new_session_file_is_kept(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["Should I open the issue?"]))
    monkeypatch.setenv("FAKE_NEW_SID", "sess-resumed")
    record = run_one("a", "old", 1, cfg)
    assert record["void"] is False and record["turns"] == 2 and record["text2"] == "second answer"


def test_second_turn_timing_out_is_recorded_and_the_first_turn_stands(cfg, monkeypatch):
    cfg.timeout_s = 3
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["Should I open the issue?"]))
    monkeypatch.setenv("FAKE_SLEEP2", "30")
    record = run_one("a", "old", 1, cfg)
    assert len(_calls(cfg)) == 2
    assert record["turn2_error"] == "timed out"
    assert record["turns"] == 1 and "text2" not in record
    assert record["void"] is False and record["timed_out"] is False
    assert record["text"] == "Should I open the issue?"
    assert record["cost_known"] is False  # the killed turn printed no result record
    assert record["cost_usd"] == pytest.approx(1.5)


def test_second_turn_that_does_not_start_is_recorded(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["Should I open the issue?"]))

    def refuse(cmd, *args, **kwargs):  # the second launch fails as a missing binary does
        if "--resume" in cmd:
            raise FileNotFoundError("no claude")
        return real_launch(cmd, *args, **kwargs)

    real_launch = launcher._launch
    monkeypatch.setattr(launcher, "_launch", refuse)
    record = run_one("a", "old", 1, cfg)
    assert "no claude" in record["turn2_error"]
    assert record["turns"] == 1 and record["void"] is False


def test_resume_command_drops_agent_and_keeps_the_other_flags(cfg):
    argv = build_resume_command(cfg, "sess-1", "the answer")
    assert argv.count("--resume") == 1 and "--agent" not in argv
    assert _arg(argv, "--resume") == "sess-1" and _arg(argv, "-p") == "the answer"
    assert _arg(argv, "--model") == cfg.model and _arg(argv, "--effort") == cfg.effort
    assert _arg(argv, "--permission-mode") == "bypassPermissions"
    assert _arg(argv, "--output-format") == "stream-json" and "--verbose" in argv


def test_run_all_counts_both_turns_in_the_cost(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["Should I open the issue?"]))
    records = run_all(cfg, cases=("a",), arms=("old",), per_arm=1, concurrency=1, budget_usd=60.0)
    assert len(records) == 1 and records[0]["turns"] == 2
    progress = json.loads((cfg.runs_dir / "progress.json").read_text("utf-8"))
    assert progress["total_cost_usd"] == pytest.approx(2.0)


def test_finish_groups_by_the_arms_the_records_hold(cfg, tmp_path, monkeypatch):
    seen = {}

    def fake_build(case_key, outputs, out_dir, key_path, seed=None, cases_path=None):
        seen[case_key] = [(r["arm"], r["n"]) for r in outputs]
        return tmp_path / "case.json"

    monkeypatch.setattr(launcher, "build_case_files", fake_build)
    monkeypatch.setattr(launcher, "check_blind", lambda case_file, key_path: [])
    arms = ("architect", "empty", "full")
    records = [{"case": "a", "arm": arm, "n": n, "void": False, "timed_out": False, "text": "t", "turns": 1}
               for arm in arms for n in (1, 2, 3, 4)]
    assert finish(cfg, records, tmp_path / "out", tmp_path / "key.json") == []
    assert seen["a"] == [(arm, n) for arm in arms for n in (1, 2, 3)]  # 9 outputs, lowest n of each arm
    with pytest.raises(ValueError, match="case a arm full: 2 kept runs, need 3"):
        finish(cfg, [r for r in records if not (r["arm"] == "full" and r["n"] > 2)], tmp_path / "out",
               tmp_path / "key.json")


def test_load_records_reads_every_arm_name(tmp_path):
    for name in ("a-empty-1.json", "b-architect-2.json", "c-full-3.json", "a-old-1.json", "progress.json"):
        (tmp_path / name).write_text(json.dumps({"run_id": name}), encoding="utf-8")
    assert sorted(r["run_id"] for r in launcher._load_records(tmp_path)) == [
        "a-empty-1.json", "a-old-1.json", "b-architect-2.json", "c-full-3.json"]


def test_finish_passes_the_mark_key_only_when_it_is_not_the_default(cfg, tmp_path, monkeypatch):
    calls = []

    def fake_build(case_key, outputs, out_dir, key_path, seed=None, cases_path=None, **extra):
        calls.append(extra)
        return tmp_path / "case.json"

    monkeypatch.setattr(launcher, "build_case_files", fake_build)
    monkeypatch.setattr(launcher, "check_blind", lambda case_file, key_path: [])
    records = [{"case": "a", "arm": arm, "n": n, "void": False, "timed_out": False, "text": "t", "turns": 1}
               for arm in ("architect", "empty", "full") for n in (1, 2, 3)]
    finish(cfg, records, tmp_path / "out", tmp_path / "key.json")
    finish(cfg, records, tmp_path / "out", tmp_path / "key.json", mark_key="pass_mark_r4")
    assert calls == [{}, {"mark_key": "pass_mark_r4"}]


def test_finish_command_line_has_a_mark_key_option(tmp_path, monkeypatch):
    seen = {}

    def fake_finish(cfg, records, out_dir, key_path, seed=None, per_arm=3, mark_key="pass_mark"):
        seen["mark_key"] = mark_key
        return []

    monkeypatch.setattr(launcher, "finish", fake_finish)
    assert launcher.main(["finish", "--runs-dir", str(tmp_path), "--mark-key", "pass_mark_r4"]) == 0
    assert seen["mark_key"] == "pass_mark_r4"
    assert launcher.main(["finish", "--runs-dir", str(tmp_path)]) == 0
    assert seen["mark_key"] == "pass_mark"


def test_writes_of_the_second_turn_are_kept_apart_from_the_first(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_TEXTS1", json.dumps(["Should I open the issue?"]))
    # the fake appends one stub write per call into the run's writes file: turn 1 one, turn 2 one more
    monkeypatch.setenv("FAKE_WRITE", "1")
    record = run_one("a", "old", 1, cfg)
    assert len(record["writes"]) == 1 and len(record["writes2"]) == 1
    assert record["writes"][0]["body"] == "turn 1" and record["writes2"][0]["body"] == "turn 2"


def test_no_second_turn_means_no_writes2(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_WRITE", "1")
    record = run_one("a", "old", 1, cfg)
    assert len(record["writes"]) == 1 and "writes2" not in record
