"""`agentops worker <spec.md> --model <id> --minutes N [--issue N]` (requirement 2).

Own worktree branch, own `.out` file per run under `artifacts/runs/`, a WIP commit on every exit
path, a heartbeat line every 10 minutes, and a final report that starts with `=== FINAL REPORT ===`
-- the GPT-era harness's behaviour (now removed, #65), kept, but driven over the `opencode serve`
HTTP bridge (`ocserver.py`) instead of one `opencode run` subprocess per session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import ocserver
from .metering import record_run
from .models import model_name
from .util import human_duration
from .worktree import ROOT, branch_commit_count, commit_wip, head_commit, make_worktree, slugify

RUNS = ROOT / "artifacts" / "runs"
HEARTBEAT_SECONDS = 10 * 60
POLL_SECONDS = 5.0
FINAL_REPORT_MARKER = "=== FINAL REPORT ==="
FINAL_REPORT_MINIMUM = 500
CONTINUATION_MINUTES = 30.0

PREAMBLE = """You are a software engineer working in this repository, in your own git worktree.
Rules:
1. Work only inside this worktree; commit here, never touch other checkouts, never merge or push.
2. Finish with one commit whose message states what changed and the measured numbers.
3. Your final message is the report. Begin it with a line that says exactly `{marker}`.
4. Durations you report: never a bare number of seconds above ~90; write minutes or hours and minutes.

The task:

"""


def _system_prompt(model: str) -> str:
    return PREAMBLE.format(marker=FINAL_REPORT_MARKER)


def _report_is_present(text: str) -> bool:
    index = text.rfind(FINAL_REPORT_MARKER)
    if index < 0:
        return False
    return len(text[index + len(FINAL_REPORT_MARKER):].strip()) >= FINAL_REPORT_MINIMUM


def _reply_text(client: ocserver.Client, directory: str, session_id: str) -> str:
    data = client.messages(directory, session_id)
    for item in reversed(data):
        if (item.get("info") or {}).get("role") == "assistant":
            return "\n".join(p.get("text", "") for p in item.get("parts", []) if p.get("type") == "text").strip()
    return ""


@dataclass
class WorkerResult:
    slug: str
    branch: str
    worktree: str
    outcome: str
    report_present: bool
    wip_commits: int
    elapsed_s: float
    tokens_in: int = 0
    tokens_out: int = 0
    report_text: str = ""
    log_path: str = ""


def _log(out_path: Path, text: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M} {text}"
    print(line, flush=True)
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _wait_for_session(client: ocserver.Client, directory: str, session_id: str, *, minutes: float,
                      slug: str, out_path: Path) -> str:
    """Poll until the session goes idle or the time limit is reached; a heartbeat every 10
    minutes (requirement 2). Returns "finished" or "time limit".

    Does not accept "idle" as "finished" until the session was seen busy at least once, or a
    short grace window has passed (`ocserver.MIN_GRACE_SECONDS`) -- the same race the research
    smoke run hit (2026-09-26 follow-up, issue #46): `prompt_async` returns before opencode's own
    async job shows up as busy, so an immediate status check reads a session that never started
    as "already finished"."""
    started = time.monotonic()
    deadline = started + minutes * 60
    last_heartbeat = started
    seen_busy = False
    while True:
        now = time.monotonic()
        if now >= deadline:
            return "time limit"
        busy = session_id in client.statuses([directory])
        seen_busy = seen_busy or busy
        if not busy and (seen_busy or now - started >= ocserver.MIN_GRACE_SECONDS):
            return "finished"
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            _log(out_path, f"{slug}: alive {human_duration(now - started)}; commits on branch: "
                          f"{branch_commit_count(Path(directory))}")
            last_heartbeat = now
        time.sleep(min(POLL_SECONDS, max(0.1, deadline - now)))


def run_worker(spec_path: Path, *, model: str = "gpt-5.6-terra", minutes: float = 180.0,
              issue: int | None = None, branch: str | None = None, runs_dir: Path | None = None,
              config_path: Path | None = None) -> WorkerResult:
    spec_path = Path(spec_path)
    if not spec_path.is_file():
        raise SystemExit(f"no spec file {spec_path}")
    slug = slugify(spec_path.stem)
    branch = branch or f"agentops-{slug}"
    runs = runs_dir or RUNS
    runs.mkdir(parents=True, exist_ok=True)
    out_path = runs / f"agentops-worker-{slug}.out"
    report_path = runs / f"agentops-worker-{slug}.md"
    started = time.monotonic()

    wt = make_worktree(branch)
    _log(out_path, f"{slug}: worktree {wt} on branch {branch}; running {model_name(model)} through the opencode bridge")

    table = ocserver.read_opencode_config(config_path)
    providers = table.get("provider", {})
    provider_id = next(iter(providers), None)
    if provider_id is None or model not in providers.get(provider_id, {}).get("models", {}):
        raise SystemExit(f"model {model} is not in {config_path or (ROOT / 'opencode.json')}'s provider table")

    client = ocserver.ensure(config_path=config_path)
    session = client.create_session(str(wt), title=slug)
    session_id = session["id"]
    ocserver.register(session_id, directory=str(wt), model=model, task=slug)

    spec_text = spec_path.read_text(encoding="utf-8")
    system = _system_prompt(model)
    client.prompt_async(str(wt), session_id, agent="agentops", provider_id=provider_id, model=model,
                        system=system, text=f"{system}The task is in the attached spec {spec_path.name}:\n\n{spec_text}")

    outcome = _wait_for_session(client, str(wt), session_id, minutes=minutes, slug=slug, out_path=out_path)
    body = _reply_text(client, str(wt), session_id)

    if outcome != "finished" or not _report_is_present(body):
        _log(out_path, f"{slug}: {outcome}; report present: {_report_is_present(body)}; sending one continuation")
        continuation = (system + "You are continuing your own interrupted work on this spec. `git log --oneline -5` "
                       "and `git status` show what exists (the harness committed your uncommitted tree as WIP). "
                       "Do not start over. Finish what is missing, commit, and write the final report, beginning "
                       f"with a line that says exactly `{FINAL_REPORT_MARKER}`.")
        wip, wip_detail = commit_wip(wt, slug, outcome, time.monotonic() - started)
        _log(out_path, f"{slug}: wip commit: {'yes' if wip else 'no'} ({wip_detail})")
        client.steer(str(wt), session_id, agent="agentops", provider_id=provider_id, model=model,
                    system=continuation, text=continuation)
        outcome = _wait_for_session(client, str(wt), session_id, minutes=min(CONTINUATION_MINUTES, minutes),
                                    slug=slug, out_path=out_path)
        body = _reply_text(client, str(wt), session_id) or body

    wip, wip_detail = commit_wip(wt, slug, outcome, time.monotonic() - started)
    ocserver.unregister(session_id)
    report_present = _report_is_present(body)
    usage = client.usage(str(wt), session_id) or {}
    elapsed_s = time.monotonic() - started

    head = head_commit(wt)
    report_path.write_text(
        f"# agentops worker {slug}\n\nbranch: {branch}\nworktree: {wt}\nlast commit: {head}\n"
        f"elapsed: {human_duration(elapsed_s)}\noutcome: {outcome}\n\n{body or '(no final message)'}\n",
        encoding="utf-8",
    )
    _log(out_path, f"{slug}: done: {outcome}; wip commit: {'yes' if wip else 'no'}; "
                  f"report: {'present' if report_present else 'MISSING'}")

    record_run(kind="worker", model=model, outcome=outcome, wall_s=elapsed_s,
              tokens_in=usage.get("input", 0), tokens_out=usage.get("output", 0), issue=issue,
              session=session_id)

    return WorkerResult(slug=slug, branch=branch, worktree=str(wt), outcome=outcome,
                        report_present=report_present, wip_commits=1 if wip else 0, elapsed_s=elapsed_s,
                        tokens_in=usage.get("input", 0), tokens_out=usage.get("output", 0),
                        report_text=body, log_path=str(out_path))
