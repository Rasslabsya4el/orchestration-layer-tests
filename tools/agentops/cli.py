"""Three entry points (requirement 2): `python -m tools.agentops ...` or `tools/agentops/cli.py`.

The core's own commands:

    python -m tools.agentops worker docs/specs/<spec>.md --model gpt-5.6-terra --minutes 180 [--issue 46]
    python -m tools.agentops research docs/research/briefs/<brief>.md --model terra [--issue 46]
    python -m tools.agentops dispatch [--once] [--max-hours 4] [--ensure]
    python -m tools.agentops keepalive install | remove | status
    python -m tools.agentops finishing [--session <id>]
    python -m tools.agentops outcome --issue 46 --route worker/sonnet-5 --class accepted --why "..." [--session ID]
    python -m tools.agentops routes [--since 2026-09-20]
    python -m tools.agentops nudge-report [--root PATH] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--days N]
    python -m tools.agentops rules [--issues N,M,...] [--root PATH]
    python -m tools.agentops recount --since DATE
    python -m tools.agentops core check [--root PATH]
    python -m tools.agentops port --target <path> --repo <owner/name> [--replace]
    python -m tools.agentops merge <branch> [--suite "<cmd>"]
    python -m tools.agentops commit -m <message> <path>...
    python -m tools.agentops merge-status [--json]
    python -m tools.agentops prequeue --cwd <issue worktree>
    python -m tools.agentops test-copy check [--root PATH]
    python -m tools.agentops guard install | check [--path FILE] [--repo DIR]
    python -m tools.agentops junctions [<worktree>]
    python -m tools.agentops worktree add <name> --base <branch-or-commit>
    python -m tools.agentops worktree gc [--apply] [--older-than-days 3] [--dirty]
    python -m tools.agentops stop <pid> [<pid> ...] [--dry-run]
    python -m tools.agentops machine play | normal | --current
    python -m tools.agentops pytest-runs [--days N] [--log <path>]
    python -m tools.agentops repo-order set <name> <name> ... | show

An installation's own modules (`local_modules` of `config/orchestration.json`, ADR 0109) add
commands and flags of their own; `--help` lists whatever this installation loaded, each after the
core command it belongs with.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.agentops.detach import run_detach_command
from tools.agentops.areas import add_subparser as add_areas_subparser, run_areas
from tools.agentops.sweep import add_subparser as add_sweep_subparser, run_sweep
from tools.agentops.rework import add_rework_subparser, run_rework_command
from tools.agentops.core_check import add_subparser as add_core_subparser, run_core_command
from tools.agentops.dispatch import (
    DEFAULT_MAX_HOURS, _attach_own_out, run_autopilot_stretch_command, run_dispatch, run_ensure,
    run_finishing_command,
)
from tools.agentops.brain_notice import run_brain_chat_command
from tools.agentops.extensions import load as load_local_modules, registry
from tools.agentops.githooks import add_subparser as add_githooks_subparser, run_githooks_command
from tools.agentops.guard import add_subparser as add_guard_subparser, run_guard_command
from tools.agentops.keepalive import add_subparser as add_keepalive_subparser, run_keepalive_command
from tools.agentops.merge import (
    add_commit_subparser,
    add_subparser as add_merge_subparser,
    run_commit_command,
    run_merge_command,
)
from tools.agentops.machine_mode import add_subparser as add_machine_subparser, run_machine_command
from tools.agentops.repo_order import add_subparser as add_repo_order_subparser, run_repo_order_command
from tools.agentops.loop_stop_cli import add_subparser as add_loop_stop_subparser, run_loop_stop_command
from tools.agentops.losses import add_subparser as add_losses_subparser, run_losses
from tools.agentops.pytest_runs_report import add_subparser as add_pytest_runs_subparser, run_pytest_runs
from tools.agentops.purposes import add_subparser as add_purposes_subparser, run_purposes
from tools.agentops.retro_cmd import add_retro_subparser, run_retro_sources
from tools.agentops.merge_status import add_subparser as add_merge_status_subparser, run_merge_status_command
from tools.agentops.prequeue import add_subparser as add_prequeue_subparser, run_prequeue_command
from tools.agentops.red_on_base import add_subparser as add_red_on_base_subparser, run_red_on_base_command
from tools.agentops.test_copy_cmd import add_subparser as add_test_copy_subparser, run_test_copy_command
from tools.agentops.outcomes import (
    OUTCOME_CHECKS,
    OUTCOME_CLASSES,
    UnknownOutcomeCheck,
    UnknownOutcomeClass,
    UnknownRoute,
    format_routes_table,
    parse_since,
    record_outcome,
    routes as compute_routes,
)
from tools.agentops.outcomes_report import add_subparser as add_outcomes_subparser, run_report
from tools.agentops.port import DEFAULT_USER_SETTINGS_PATH, add_subparser as add_port_subparser, run_port_command
from tools.agentops.target_add import add_target_subparser, run_target_command
from tools.agentops.research import run_research
from tools.agentops.ocserver import BridgeConfigMissing, bridge_unavailable_line
from tools.agentops.nudge_report import add_subparser as add_nudge_report_subparser, run_command as run_nudge_report
from tools.agentops.rules_count import add_subparser as add_rules_subparser, run_rules_command
from tools.agentops.recount import add_subparser as add_recount_subparser, run_recount_command
from tools.agentops.logic_audit import add_subparser as add_logic_audit_subparser, run_logic_audit_command
from tools.agentops.stop import add_stop_subparser, run_stop_command
from tools.agentops.monitor_snapshots import add_subparser as add_monitor_snapshots_subparser, run_command as run_monitor_snapshots_command
from tools.agentops.close_issue import add_subparser as add_close_subparser, run_close_command
from tools.agentops.worktree import (add_junctions_subparser, add_worktree_subparser, run_junctions_command,
                                     run_worktree_command)
from tools.agentops.worker import run_worker


def _parse_unit(raw: str) -> str:
    """`--unit k/n`: k, n positive ints, k <= n; anything else is an argparse error, so
    `main()` never sees a malformed unit."""

    parts = raw.split("/")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"--unit must be k/n (e.g. 2/3), got {raw!r}")
    k_raw, n_raw = parts
    if not k_raw.isdigit() or not n_raw.isdigit():
        raise argparse.ArgumentTypeError(f"--unit must be k/n with positive ints (e.g. 2/3), got {raw!r}")
    k, n = int(k_raw), int(n_raw)
    if k <= 0 or n <= 0 or k > n:
        raise argparse.ArgumentTypeError(f"--unit must have 0 < k <= n, got {raw!r}")
    return f"{k}/{n}"


def main(argv: list[str] | None = None) -> int:
    load_local_modules()
    ap = argparse.ArgumentParser(prog="agentops", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    #: the local commands already in this parser, so each is added once per parser built.
    added: set[str] = set()

    worker = sub.add_parser("worker", help="run one code task through the opencode bridge")
    worker.add_argument("spec")
    worker.add_argument("--model", default="gpt-5.6-terra")
    worker.add_argument("--minutes", type=float, default=180.0)
    worker.add_argument("--issue", type=int)
    worker.add_argument("--branch")
    registry.add_commands(sub, after="worker", added=added)

    research = sub.add_parser("research", help="run one research brief through the opencode bridge")
    research.add_argument("brief")
    research.add_argument("--model", default="terra", choices=["luna", "terra", "gpt-6-luna", "gpt-5.6-terra"])
    research.add_argument("--issue", type=int)
    research.add_argument("--no-reddit", action="store_true",
                          help="brief's sources are not forums: no Reddit instruction, no reddit.com URL required (#488)")
    registry.add_commands(sub, after="research", added=added)

    dispatch = sub.add_parser("dispatch", help="run the ORCH dispatcher loop (issue #50)")
    dispatch.add_argument("--once", action="store_true", help="run a single tick and exit")
    dispatch.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS)
    dispatch.add_argument("--ensure", action="store_true",
                          help="start one detached dispatcher if none is live, else do nothing (issue #201)")
    dispatch.add_argument("--own-out", default=None,
                          help="the file this dispatcher opens itself for its stdout and stderr (#280; the "
                               "detached start passes it, a hand run does not)")
    registry.add_dispatch_flags(dispatch)
    registry.add_commands(sub, after="dispatch", added=added)

    add_keepalive_subparser(sub)
    registry.add_commands(sub, after="keepalive", added=added)

    finishing = sub.add_parser("finishing", help="say this ORCH session takes no new work (D110, #191)")
    finishing.add_argument("--session", help="this session's dispatcher id, when it cannot find its own")
    registry.add_commands(sub, after="finishing", added=added)

    stretch = sub.add_parser("autopilot-stretch",
                             help="print the autopilot stretch live on an issue, or nothing (#1142)")
    stretch.add_argument("--issue", type=int, required=True)
    registry.add_commands(sub, after="autopilot-stretch", added=added)

    brain_chat = sub.add_parser("brain-chat",
                                help="print the brain chat title ORCH's report on an issue goes to (#24)")
    brain_chat.add_argument("--issue", type=int, required=True)
    registry.add_commands(sub, after="brain-chat", added=added)

    outcome = sub.add_parser("outcome", help="log one delegation's outcome (issue #66)")
    outcome.add_argument("--issue", type=int, required=True)
    outcome.add_argument("--route", required=True, help="e.g. worker/sonnet-5, or the opencode model")
    outcome.add_argument("--class", dest="cls", required=True, choices=OUTCOME_CLASSES)
    outcome.add_argument("--why", required=True)
    outcome.add_argument("--session", help="session/agent id, for the routes join")
    outcome.add_argument("--unit", type=_parse_unit,
                         help="this delegation's slice of a sliced issue, e.g. 2/3")
    outcome.add_argument("--check", choices=OUTCOME_CHECKS,
                         help="a second helper's verdict on the first answer (issue #131): agree or disagree")
    registry.add_commands(sub, after="outcome", added=added)

    routes_cmd = sub.add_parser("routes", help="the routing table recomputed from outcome lines (issue #66)")
    routes_cmd.add_argument("--since", help="a date (e.g. 2026-09-20) or ISO timestamp")
    registry.add_commands(sub, after="routes", added=added)

    add_outcomes_subparser(sub)
    registry.add_commands(sub, after="outcomes", added=added)

    add_nudge_report_subparser(sub)
    registry.add_commands(sub, after="nudge-report", added=added)

    add_rules_subparser(sub)
    registry.add_commands(sub, after="rules", added=added)

    add_recount_subparser(sub)
    registry.add_commands(sub, after="recount", added=added)

    add_logic_audit_subparser(sub)
    registry.add_commands(sub, after="logic-audit", added=added)

    add_core_subparser(sub)
    registry.add_commands(sub, after="core", added=added)

    add_githooks_subparser(sub)
    add_port_subparser(sub)
    registry.add_commands(sub, after="port", added=added)

    add_target_subparser(sub)

    add_merge_subparser(sub)
    registry.add_commands(sub, after="merge", added=added)

    add_commit_subparser(sub)
    for _name in ("merge", "commit"):
        sub.choices[_name].add_argument(
            "--detach", action="store_true",
            help="run this same command as a process no Claude Code shell holds; print the result "
                 "file path and pid, return at once (issue #399)")
    registry.add_commands(sub, after="commit", added=added)

    add_merge_status_subparser(sub)
    add_prequeue_subparser(sub)
    add_red_on_base_subparser(sub)
    add_test_copy_subparser(sub)
    add_loop_stop_subparser(sub)
    add_machine_subparser(sub)
    add_repo_order_subparser(sub)

    add_guard_subparser(sub)
    registry.add_commands(sub, after="guard", added=added)

    add_stop_subparser(sub)
    add_monitor_snapshots_subparser(sub)
    add_close_subparser(sub)
    add_junctions_subparser(sub)
    add_worktree_subparser(sub)
    registry.add_commands(sub, after="junctions", added=added)
    registry.add_commands(sub, after="stop", added=added)

    add_areas_subparser(sub)
    registry.add_commands(sub, after="areas", added=added)

    add_sweep_subparser(sub)

    add_rework_subparser(sub)
    registry.add_commands(sub, after="rework", added=added)

    add_losses_subparser(sub)
    registry.add_commands(sub, after="losses", added=added)

    add_pytest_runs_subparser(sub)

    add_purposes_subparser(sub)

    add_retro_subparser(sub)

    map_run = sub.add_parser("map-run", help="rebuild the whole test map at a commit, after a green landing (#41)")
    map_run.add_argument("--commit", required=True)
    map_run.add_argument("--detach", action="store_true",
                         help="the spawn's form: started by a caller that does not wait; the build runs here")
    map_run.add_argument("--cwd", type=Path, default=None, help="the main checkout (default: this installation's)")

    registry.add_remaining_commands(sub, added=added)

    args = ap.parse_args(argv)
    if args.command == "merge" and getattr(args, "detach", False):
        if not getattr(args, "issue", None):  # #145: a detached merge's result must reach the ticket that waits for it
            print(f"refused at queue: detached merge of {args.branch} has no --issue N; "
                  "the ticket that waits for its result needs one")
            return 1
        from .merge import worktree_refusal
        refusal = worktree_refusal(args.cwd)
        if refusal:  # #904: refuse at once, not as a result file later
            print(refusal)
            return 1
        from .merge import queue_target_refusals
        refused = queue_target_refusals(args.branch, cwd=args.cwd)  # #110: refuse at once, before the line
        if refused:
            print("\n".join(refused))
            return 1
        if refused is not None:  # [] = checked and clean; None = not checked, the runner checks as today
            argv = [*(sys.argv[1:] if argv is None else argv), "--queue-checked"]
    if args.command in ("merge", "commit") and getattr(args, "detach", False):
        return run_detach_command(args, list(sys.argv[1:] if argv is None else argv))

    if args.command == "worker":
        result = run_worker(Path(args.spec), model=args.model, minutes=args.minutes, issue=args.issue,
                            branch=args.branch)
        print(f"{result.slug}: {result.outcome}; report {'present' if result.report_present else 'MISSING'}; "
              f"branch {result.branch}")
        return 0 if result.report_present else 1

    if args.command == "research":
        try:
            result = run_research(Path(args.brief), model=args.model, issue=args.issue,
                                  require_reddit=not args.no_reddit)
        except BridgeConfigMissing as exc:  # #59: one line, rc 1, no traceback
            print(bridge_unavailable_line(exc))
            return 1
        partial = f"; partial {result.partial_path}" if result.partial_path else ""  # #333: a stalled run's text
        print(f"{result.slug}: {result.reason}; report {result.report_path or '(rejected)'}{partial}")
        print(result.status)  # #333: steps, minutes since the last step, sources, reddit reached
        return 0 if result.accepted else 1

    if args.command == "dispatch":
        if args.ensure:
            return run_ensure()
        _attach_own_out(args.own_out)  # #280: the detached dispatcher opens dispatch.out itself
        return run_dispatch(once=args.once, max_hours=args.max_hours, args=args)

    if args.command == "keepalive":
        return run_keepalive_command(args)

    if args.command == "junctions":
        return run_junctions_command(args)
    if args.command == "worktree":
        return run_worktree_command(args)

    if args.command == "finishing":
        run_finishing_command(session=args.session)
        return 0

    if args.command == "autopilot-stretch":
        rc, text = run_autopilot_stretch_command(args.issue)
        if rc == 0:
            if text:
                print(text)
        else:
            print(text, file=sys.stderr)
        return rc

    if args.command == "brain-chat":
        rc, text = run_brain_chat_command(args.issue)
        if rc == 0:
            print(text)
        else:
            print(text, file=sys.stderr)
        return rc

    if args.command == "outcome":
        try:
            record = record_outcome(issue=args.issue, route=args.route, cls=args.cls, why=args.why,
                                    session=args.session, unit=args.unit, check=args.check)
        except (UnknownOutcomeClass, UnknownOutcomeCheck, UnknownRoute) as exc:
            print(f"error: {exc}")
            return 2
        print(f"logged: #{record['issue']} {record['route']} {record['class']} -- {record['why']}")
        return 0

    if args.command == "routes":
        since = parse_since(args.since) if args.since else None
        rows = compute_routes(since=since)
        print(format_routes_table(rows))
        return 0

    if args.command == "outcomes":
        return run_report(args)

    if args.command == "nudge-report":
        return run_nudge_report(args)

    if args.command == "rules":
        return run_rules_command(args)

    if args.command == "recount":
        return run_recount_command(args)

    if args.command == "logic-audit":
        return run_logic_audit_command(args)

    if args.command == "core":
        return run_core_command(args, settings_path=DEFAULT_USER_SETTINGS_PATH)

    if args.command == "githooks":
        return run_githooks_command(args)

    if args.command == "port":
        return run_port_command(args, settings_path=DEFAULT_USER_SETTINGS_PATH)

    if args.command == "map-run":  # imported here: a target without map_run.py still starts (#41)
        from tools.agentops.map_run import run_map_run_command
        return run_map_run_command(args)

    if args.command == "target":
        return run_target_command(args, settings_path=DEFAULT_USER_SETTINGS_PATH)

    if args.command == "merge":
        return run_merge_command(args)

    if args.command == "commit":
        return run_commit_command(args)

    if args.command == "merge-status":
        return run_merge_status_command(args)

    if args.command == "prequeue":
        return run_prequeue_command(args)

    if args.command == "red-on-base":
        return run_red_on_base_command(args)

    if args.command == "test-copy":
        return run_test_copy_command(args)

    if args.command == "loop-stop":
        return run_loop_stop_command(args)

    if args.command == "machine":
        return run_machine_command(args)

    if args.command == "repo-order":
        return run_repo_order_command(args)

    if args.command == "guard":
        return run_guard_command(args)

    if args.command == "stop":
        return run_stop_command(args)

    if args.command == "monitor-snapshots":
        return run_monitor_snapshots_command(args)

    if args.command == "close":
        return run_close_command(args)

    if args.command == "areas":
        return run_areas(args)

    if args.command == "sweep":
        return run_sweep(args)

    if args.command == "rework":
        return run_rework_command(args)

    if args.command == "losses":
        return run_losses(args)

    if args.command == "pytest-runs":
        return run_pytest_runs(args)

    if args.command == "purposes":
        return run_purposes(args)

    if args.command == "retro-sources":
        return run_retro_sources(args)

    local = registry.run_command(args)
    if local is not None:
        return local

    return 2


if __name__ == "__main__":
    sys.exit(main())
