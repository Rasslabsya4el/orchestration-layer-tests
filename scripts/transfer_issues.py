"""Move issues between repos of the account, and relabel them, paced and resumable (#12).

    python scripts/transfer_issues.py transfer --from-repo <owner/repo> --to-repo <owner/repo>
        --list <file> --log <jsonl> [--gap 10] [--last N]
    python scripts/transfer_issues.py relabel --repo <owner/repo> --map <file>
        --remove area:orchestration --log <jsonl> [--gap 3]
    python scripts/transfer_issues.py count --repo <owner/repo> --label <label>

Every ``gh`` call goes through one runner, ``runner(args) -> (returncode, stdout, stderr)``, and every
pause through ``sleep(seconds)``; both are injectable, so the tests run with a fake runner and no pause.
The real runner starts ``gh`` with no shell, on Windows with CREATE_NO_WINDOW, and with a 120 s timeout.

The log is one JSON object per line. A transfer is done for (from-repo, old) when its line carries
``new`` (moved, ``new`` null when no link came back) or ``skipped`` (a per-issue not found / already
moved); a relabel is done for (repo, n) when its line carries ``label``. A line with ``error`` is not
done, so a rerun retries it. A row without a repo key counts for the repo given on the command line.

A rate limit backs off 60, 120, 240, 480 s (4 retries), then the run stops with exit 1. A timeout is
retried once after 60 s. A repo or auth error (404 on a repo, 401, 403, "Resource not accessible") stops
the run at once. Any other error is logged and the run goes on; 3 of them in a row stop it. A run that
ends with a listed number lacking a done row exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

Runner = Callable[[list[str]], "tuple[int, str, str]"]
Sleep = Callable[[float], None]

BACKOFF = (60, 120, 240, 480)
MAX_OTHER_ERRORS_IN_A_ROW = 3
GH_TIMEOUT = 120
TIMEOUT_RC = 124
CREATE_NO_WINDOW = 0x08000000
DEFAULT_TRANSFER_GAP = 10.0
DEFAULT_RELABEL_GAP = 3.0
COUNT_LIMIT = 5000

# Per-issue outcomes: the issue is gone or already moved; the run goes on.
SKIP_MARKERS = ("could not resolve to an issue", "issue not found", "was transferred", "already")
# Repo or account outcomes: every later call would fail the same way; the run stops.
FATAL_MARKERS = ("could not resolve to a repository", "resource not accessible", "bad credentials")
FATAL_CODES = re.compile(r"\b(401|403|404)\b")


def real_runner(args: list[str]) -> tuple[int, str, str]:
    """Run ``gh`` with the given arguments: no shell, no console window on Windows, 120 s limit."""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=GH_TIMEOUT,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return TIMEOUT_RC, "", "timeout"
    except OSError as exc:  # gh not installed or not on PATH
        return 127, "", f"cannot start gh: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


def _detail(err: str, out: str, rc: int) -> str:
    return _first_line(err) or _first_line(out) or f"gh exit {rc}"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_log(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:  # a line cut short by a kill; its number is retried
            continue
    return rows


def append_log(path: str | Path, row: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if p.exists() and p.stat().st_size > 0:
        with p.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":  # a log cut short by a kill: start a fresh line
                prefix = "\n"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(prefix + json.dumps(row, ensure_ascii=False) + "\n")


def parse_numbers(text: str) -> list[int]:
    """Issue numbers separated by commas or whitespace; ``#12`` is 12; whole ``# `` / ``//`` lines are comments."""
    tokens: list[int] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped == "#" or stripped.startswith("# ") or stripped.startswith("//"):
            continue
        for token in re.split(r"[,\s]+", stripped):
            if not token:
                continue
            if not re.fullmatch(r"#?\d+", token):
                raise ValueError(f"line {lineno}: not an issue number: {token!r}")
            tokens.append(int(token.lstrip("#")))
    return list(dict.fromkeys(tokens))


def order_numbers(numbers: list[int], last: int | None) -> list[int]:
    if last is None or last not in numbers:
        return list(numbers)
    return [n for n in numbers if n != last] + [last]


def is_rate_limit(text: str) -> bool:
    low = text.lower()
    if "rate limit" in low or re.search(r"\b429\b", low):
        return True
    return bool(re.search(r"\b403\b", low)) and "secondary rate" in low


def skip_reason(text: str) -> str | None:
    low = text.lower()
    if any(marker in low for marker in SKIP_MARKERS):
        return _first_line(text) or "skipped"
    return None


def is_fatal(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in FATAL_MARKERS) or bool(FATAL_CODES.search(low))


def parse_new_number(stdout: str) -> int | None:
    found = re.findall(r"/issues/(\d+)", stdout)
    return int(found[-1]) if found else None


def gh_with_backoff(args: list[str], runner: Runner, sleep: Sleep) -> tuple[int, str, str] | None:
    """One gh call. A rate limit backs off 60, 120, 240, 480 s and retries the same call; a timeout is
    retried once after 60 s. Returns the last (returncode, stdout, stderr), or None when the 4 retries are spent.
    """
    attempt = 0
    timeouts = 0
    while True:
        rc, out, err = runner(args)
        if rc == 0:
            return rc, out, err
        if rc == TIMEOUT_RC:
            if timeouts == 0:
                timeouts += 1
                sleep(BACKOFF[0])
                continue
            return rc, out, err
        if not is_rate_limit(out + "\n" + err):
            return rc, out, err
        if attempt == len(BACKOFF):
            return None
        sleep(BACKOFF[attempt])
        attempt += 1


def transfer(
    from_repo: str,
    to_repo: str,
    numbers: list[int],
    log: str | Path,
    *,
    gap: float = DEFAULT_TRANSFER_GAP,
    last: int | None = None,
    runner: Runner,
    sleep: Sleep,
) -> int:
    done = {
        (r.get("from", from_repo), r["old"])
        for r in read_log(log)
        if "old" in r and ("new" in r or "skipped" in r)
    }
    todo = [n for n in order_numbers(numbers, last) if (from_repo, n) not in done]
    other_errors = 0
    for n in todo:
        result = gh_with_backoff(["issue", "transfer", str(n), to_repo, "-R", from_repo], runner, sleep)
        if result is None:
            append_log(log, {"old": n, "error": "rate limit: 4 back-offs spent, run stopped"})
            return 1
        rc, out, err = result
        if rc == 0:
            new = parse_new_number(out)
            if new is None:
                # The transfer went through; only its link did not come back. Log it done, then stop.
                append_log(log, {"old": n, "new": None, "raw": out, "from": from_repo, "to": to_repo, "at": _now_iso()})
                return 1
            append_log(log, {"old": n, "new": new, "from": from_repo, "to": to_repo, "at": _now_iso()})
            done.add((from_repo, n))
            other_errors = 0
            sleep(gap)
            continue
        text = out + "\n" + err
        reason = skip_reason(text)
        if reason is not None:
            append_log(log, {"old": n, "skipped": reason})
            done.add((from_repo, n))
            other_errors = 0
            continue
        append_log(log, {"old": n, "error": _detail(err, out, rc)})
        if is_fatal(text):
            return 1
        other_errors += 1
        if other_errors >= MAX_OTHER_ERRORS_IN_A_ROW:
            return 1
    missing = [n for n in numbers if (from_repo, n) not in done]
    if missing:
        print(f"transfer: {len(missing)} listed number(s) have no done row: {missing}", file=sys.stderr)
        return 1
    return 0


def _map_number(value: object, lineno: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"map line {lineno}: n is not an issue number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"map line {lineno}: n is not an issue number: {value!r}")


def parse_map(text: str) -> list[tuple[int, str]]:
    """``N=label`` or ``#N=label`` lines, or JSON lines ``{"n": N, "label": "..."}``.

    Blank lines and ``#`` comments are skipped; a ``#`` followed by a non-digit starts a comment.
    A malformed line raises ValueError naming its line number.
    """
    pairs: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or (line.startswith("#") and not re.match(r"#\d", line)):
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"map line {lineno} is not valid JSON: {exc.msg}") from None
            if not isinstance(obj, dict) or "n" not in obj or "label" not in obj:
                raise ValueError(f'map line {lineno} is not {{"n": N, "label": "..."}}')
            n, label = _map_number(obj["n"], lineno), obj["label"]
        else:
            body = line[1:] if line.startswith("#") else line
            left, sep, label = body.partition("=")
            if not sep:
                raise ValueError(f"map line {lineno} is not N=label: {line!r}")
            n = _map_number(left.strip(), lineno)
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"map line {lineno} has no label")
        pairs.append((n, label.strip()))
    return pairs


def relabel(
    repo: str,
    mapping: list[tuple[int, str]],
    remove: str,
    log: str | Path,
    *,
    gap: float = DEFAULT_RELABEL_GAP,
    runner: Runner,
    sleep: Sleep,
) -> int:
    done = {(r.get("repo", repo), r["n"]) for r in read_log(log) if "n" in r and "label" in r}
    other_errors = 0
    for n, label in mapping:
        if (repo, n) in done:
            continue
        args = ["issue", "edit", str(n), "-R", repo, "--remove-label", remove, "--add-label", label]
        result = gh_with_backoff(args, runner, sleep)
        if result is None:
            append_log(log, {"n": n, "error": "rate limit: 4 back-offs spent, run stopped"})
            return 1
        rc, out, err = result
        if rc == 0:
            append_log(log, {"n": n, "label": label, "repo": repo, "at": _now_iso()})
            done.add((repo, n))
            other_errors = 0
            sleep(gap)
            continue
        append_log(log, {"n": n, "error": _detail(err, out, rc)})
        if is_fatal(out + "\n" + err):
            return 1
        other_errors += 1
        if other_errors >= MAX_OTHER_ERRORS_IN_A_ROW:
            return 1
    missing = [n for n, _ in mapping if (repo, n) not in done]
    if missing:
        print(f"relabel: {len(missing)} mapping(s) have no done row: {missing}", file=sys.stderr)
        return 1
    return 0


def count(repo: str, label: str, *, runner: Runner) -> int:
    rc, out, err = runner(
        ["issue", "list", "-R", repo, "--label", label, "--state", "all", "--limit", str(COUNT_LIMIT), "--json", "number"]
    )
    if rc != 0:
        raise RuntimeError(_detail(err, out, rc))
    items = json.loads(out)  # ValueError on bad JSON
    if not isinstance(items, list):
        raise ValueError("gh did not return a JSON list")
    if len(items) >= COUNT_LIMIT:
        raise RuntimeError(f"{len(items)} issues: the --limit {COUNT_LIMIT} may have cut the list short")
    return len(items)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="transfer_issues", description="Move, relabel and count GitHub issues (#12).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("transfer", help="move issues with gh issue transfer")
    t.add_argument("--from-repo", required=True)
    t.add_argument("--to-repo", required=True)
    t.add_argument("--list", required=True, help="file with issue numbers, comma/space/newline separated")
    t.add_argument("--log", required=True, help="jsonl log; numbers already in it are not moved again")
    t.add_argument("--gap", type=float, default=DEFAULT_TRANSFER_GAP, help="seconds between moves (default 10)")
    t.add_argument("--last", type=int, default=None, help="this number is moved after all others")

    r = sub.add_parser("relabel", help="gh issue edit: swap one label for another per issue")
    r.add_argument("--repo", required=True)
    r.add_argument("--map", required=True, help="N=label lines, or JSON lines {\"n\": N, \"label\": \"...\"}")
    r.add_argument("--remove", required=True, help="label to remove, e.g. area:orchestration")
    r.add_argument("--log", required=True)
    r.add_argument("--gap", type=float, default=DEFAULT_RELABEL_GAP, help="seconds between edits (default 3)")

    c = sub.add_parser("count", help="print how many issues (state all) carry a label")
    c.add_argument("--repo", required=True)
    c.add_argument("--label", required=True)
    return ap


def main(argv: list[str] | None = None, runner: Runner | None = None, sleep: Sleep | None = None) -> int:
    args = build_parser().parse_args(argv)
    run = runner or real_runner
    pause = sleep or time.sleep
    try:
        if args.cmd == "transfer":
            numbers = parse_numbers(Path(args.list).read_text(encoding="utf-8"))
            return transfer(
                args.from_repo,
                args.to_repo,
                numbers,
                args.log,
                gap=args.gap,
                last=args.last,
                runner=run,
                sleep=pause,
            )
        if args.cmd == "relabel":
            mapping = parse_map(Path(args.map).read_text(encoding="utf-8"))
            return relabel(args.repo, mapping, args.remove, args.log, gap=args.gap, runner=run, sleep=pause)
        print(count(args.repo, args.label, runner=run))
        return 0
    except ValueError as exc:
        print(f"{args.cmd} failed: {exc}", file=sys.stderr)
        return 2 if args.cmd != "count" else 1
    except RuntimeError as exc:
        print(f"count failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"{args.cmd} failed: cannot read or write a file: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
