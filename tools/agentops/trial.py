"""#455 unit 1: the live-trial repo command.

    python -m tools.agentops trial create <slug> --from <owner/repo> [--at <commit>] [--public --require-check <name>...]
    python -m tools.agentops trial list

`create_trial` makes `<owner>/<trial_repo_prefix><slug>` (the installation's owner, the setting `trial_repo_prefix`;
private unless `--public`), pushes the source's tree at the commit to the new repo's default branch as ONE fresh
parentless commit (no source history), copies the source's labels, sets the required status checks when asked, clones
the new repo to `var/trials/<name>` and appends one JSON line {name, from, at, visibility, created} to
`var/trials.jsonl`. A failure midway prints what exists so far and keeps the record line; nothing is ever deleted.

`list_trials` prints each record line and whether the repo still exists (`gh repo view`).

Every gh and git call goes through one runner (`run_command`, an argv list); the tests pass a fake. No console window:
`no_window_flags()` as in `closing_keywords.py`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from tools.agentops import core_settings
from tools.agentops.util import no_window_flags

#: a machine bound on one gh or git call (clone, push and label calls included), seconds.
COMMAND_SECONDS = 300
RECORD_PARTS = ("var", "trials.jsonl")
TRIALS_PARTS = ("var", "trials")
#: every git call: no autocrlf rewrite of the tree, no commit signing prompt.
GIT_CONFIG = ("-c", "core.autocrlf=false", "-c", "commit.gpgsign=false")
COMMIT_AUTHOR = ("-c", "user.name=trial", "-c", "user.email=trial@localhost")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}")
_OWNER_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMIT = re.compile(r"[0-9a-fA-F]{7,40}")
REFUSAL_NEEDS_PUBLIC = ("trial create: --require-check needs --public: free private repos answer 403 on branch "
                        "protection, so a protected base must be a public repo")


class TrialError(Exception):
    """A gh or git call failed; `output` is its full stderr (or stdout), for a reader that matches a message."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def run_command(argv: list[str], input_text: str | None = None) -> str:
    """The one runner: a gh or git call from an argv list, stdout returned; a nonzero exit or a call that does not run
    raises TrialError. Prompts are off, so a call that wants a login fails instead of waiting."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    try:
        done = subprocess.run(argv, input=input_text, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=COMMAND_SECONDS, env=env, creationflags=no_window_flags())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrialError(f"{argv[0]} did not run: {exc}") from exc
    if done.returncode != 0:
        output = (done.stderr or done.stdout or "").strip()
        last = output.splitlines()[-1] if output else f"exit {done.returncode}"
        raise TrialError(f"{' '.join(argv[:2])} failed: {last}", output=output)
    return done.stdout


def _installation_owner(path: Path | None = None) -> str:
    """The owner part of the installation's own `repo` setting: the owner of every trial repo."""
    return core_settings.load(path)["repo"].split("/", 1)[0]


def _record_path(root: Path) -> Path:
    return Path(root).joinpath(*RECORD_PARTS)


def _append_record(root: Path, record: dict) -> None:
    path = _record_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _label_argv(label: dict, repo: str) -> list[str]:
    argv = ["gh", "label", "create", label["name"], "-R", repo]
    if label.get("color"):
        argv += ["--color", label["color"]]
    if label.get("description"):
        argv += ["--description", label["description"]]
    return argv + ["--force"]


def create_trial(slug: str, source: str, *, at: str | None = None, public: bool = False,
                 require_checks=(), owner: str | None = None, root: Path = core_settings.ROOT,
                 runner=run_command, out=print) -> int:
    """Make the trial repo of `source` named `slug`; return 0 on success, 2 when refused before any call, 1 when a call
    failed midway (what exists so far is printed; nothing is deleted)."""
    require = list(require_checks)
    if require and not public:
        out(REFUSAL_NEEDS_PUBLIC)
        return 2
    if not _SLUG.fullmatch(slug):
        out(f"trial create: slug {slug!r} must be letters, digits, '.', '_' or '-' (not starting with a dot)")
        return 2
    if not _OWNER_NAME.fullmatch(source):
        out(f"trial create: --from must be owner/repo, got {source!r}")
        return 2
    if at is not None and not _COMMIT.fullmatch(at):
        out(f"trial create: --at must be a commit hash (7 to 40 hex digits), got {at!r}")
        return 2
    try:
        prefix = core_settings.trial_repo_prefix()
        owner = owner or _installation_owner()
    except (RuntimeError, ValueError) as exc:
        out(f"trial create: {exc}")
        return 2
    short = f"{prefix}{slug}"
    name = f"{owner}/{short}"
    visibility = "public" if public else "private"
    trials_dir = Path(root).joinpath(*TRIALS_PARTS)
    clone = trials_dir / short
    export = trials_dir / f"{short}.export"
    if clone.exists() or export.exists():
        out(f"trial create: {clone} or {export} already exists here; nothing was created")
        return 2
    new_url = f"https://github.com/{name}.git"
    src_url = f"https://github.com/{source}.git"
    made: list[str] = []
    step = "read the source"
    try:
        default = runner(["gh", "api", f"repos/{source}", "--jq", ".default_branch"]).strip()
        if not default:
            raise TrialError(f"{source} gives no default branch")
        sha = runner(["gh", "api", f"repos/{source}/commits/{at or default}", "--jq", ".sha"]).strip()
        if not sha:
            raise TrialError(f"{source} has no commit {at or default}")

        step = "create the repo"
        runner(["gh", "repo", "create", name, f"--{visibility}"])
        made.append(f"repo {name} ({visibility})")
        record = {"name": name, "from": source, "at": sha, "visibility": visibility, "created": _now()}
        _append_record(root, record)
        made.append(f"record line in {_record_path(root)}")

        step = "push the tree as one commit"
        git = ["git", "-C", str(export), *GIT_CONFIG]
        runner(["git", "init", "--quiet", "-b", default, str(export)])
        runner([*git, "fetch", "--quiet", "--depth", "1", src_url, sha])
        runner([*git, "checkout", "--quiet", "--detach", "FETCH_HEAD"])
        runner([*git, "checkout", "--quiet", "--orphan", "trial-export"])
        runner([*git, *COMMIT_AUTHOR, "commit", "--quiet", "-m", f"trial {short}: tree of {source}@{sha[:7]}"])
        runner([*git, "push", "--quiet", new_url, f"HEAD:refs/heads/{default}"])
        made.append(f"one commit on {name} {default}")

        step = "copy the labels"
        listed = runner(["gh", "label", "list", "-R", source, "--json", "name,color,description", "--limit", "1000"])
        for label in json.loads(listed or "[]"):
            runner(_label_argv(label, name))
        made.append(f"labels of {source}")

        if require:
            step = "set the required checks"
            body = {"required_status_checks": {"strict": False, "contexts": require},
                    "enforce_admins": None, "required_pull_request_reviews": None, "restrictions": None}
            runner(["gh", "api", "-X", "PUT", f"repos/{name}/branches/{default}/protection", "--input", "-"],
                   input_text=json.dumps(body))
            made.append(f"required checks {', '.join(require)} on {default}")

        step = "clone the repo"
        runner(["git", "clone", "--quiet", new_url, str(clone)])
        made.append(f"clone {clone}")
    except (TrialError, OSError) as exc:
        out(f"trial create: {step} failed: {exc}")
        out("exists so far: " + ("; ".join(made) if made else "nothing"))
        out("nothing was deleted; deleting a trial repo is the owner's")
        return 1
    out(f"trial create: {name} made ({visibility}), tree of {source}@{sha[:7]} on {default}")
    out(f"clone: {clone}")
    return 0


def _still_exists(name: str, runner) -> str:
    try:
        runner(["gh", "repo", "view", name, "--json", "name"])
    except TrialError as exc:
        text = exc.output.lower()
        if "could not resolve" in text or "not found" in text or "404" in text:
            return "no"
        return f"unknown ({exc})"
    return "yes"


def list_trials(*, root: Path = core_settings.ROOT, runner=run_command, out=print) -> int:
    """Print each record line: name, from, at, visibility, created, and whether the repo still exists."""
    path = _record_path(root)
    if not path.is_file():
        out(f"trial list: no trials recorded ({path})")
        return 0
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            out(f"trial list: line {number} of {path} is not JSON; skipped")
            continue
        name = record.get("name")
        out(f"{name}  from {record.get('from')}  at {record.get('at')}  {record.get('visibility')}  "
            f"created {record.get('created')}  still exists {_still_exists(name, runner)}")
    return 0


def add_subparser(sub) -> None:
    parent = sub.add_parser("trial", help="a live trial's own repo: create one, list the made ones (#455)")
    trial_sub = parent.add_subparsers(dest="trial_command", required=True)
    create = trial_sub.add_parser("create", help="make <owner>/<trial_repo_prefix><slug> from a repo's tree (#455)")
    create.add_argument("slug", help="the trial's name; the repo is <owner>/<trial_repo_prefix><slug>")
    create.add_argument("--from", dest="source", required=True, metavar="owner/repo", help="the source repo")
    create.add_argument("--at", default=None, metavar="commit",
                        help="the source commit (default: the head of its default branch)")
    create.add_argument("--public", action="store_true", help="a public repo (default: private)")
    create.add_argument("--require-check", dest="require_check", nargs="+", action="extend", default=[],
                        metavar="NAME", help="required status checks on the default branch (needs --public)")
    trial_sub.add_parser("list", help="the trial repos this installation made, and whether each still exists")


def run_trial_command(args) -> int:
    if args.trial_command == "create":
        return create_trial(args.slug, args.source, at=args.at, public=args.public, require_checks=args.require_check)
    return list_trials()
