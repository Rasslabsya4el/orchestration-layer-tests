"""The one place a `gh` command is run from this package: one timeout, one transient-failure check.

``run_gh(args, ...)`` runs ``gh <args>`` with output captured and returns a ``GhResult`` (a
``subprocess.CompletedProcess`` with one more attribute, ``transient``). It follows these rules:

* One timeout, ``GH_TIMEOUT_SECONDS`` (the same bound the runs daemon puts on each of its own ``gh`` calls).
  A call that does not answer in time has its whole process tree killed and raises
  ``subprocess.TimeoutExpired``; the call never waits for ever on a child that keeps the pipes open.
* A nonzero return code is not an exception (unless ``check=True``): the caller reads ``returncode`` as it
  does for ``subprocess.run``. What the adapter adds is ``result.transient``: True when that failure's
  text shows a flaky ``gh`` / GitHub call (a gateway error, a dropped connection, a rate limit) that is worth
  one more try, False for every other failure and for a success.
* ``check=True`` raises ``GhCalledProcessError`` (a ``subprocess.CalledProcessError`` that also carries
  ``transient``), the one place the adapter raises on a return code.
* ``cwd``, ``input`` (stdin; ``str`` in text mode, ``bytes`` when ``text=False``) and ``text`` (``True``:
  utf-8 with ``errors="replace"``; ``False``: bytes) cover every calling pattern of the package.
* The primary rate limit (#1113, rule 3): a call that fails with "API rate limit exceeded" reads the reset time once
  (``gh api rate_limit``, which does not spend the GraphQL budget; 60 s when that read fails) and every later call
  of the same resource (``graphql`` or ``core``) in this process returns at once, without starting ``gh``, a
  transient failure "waiting until HH:MM:SS"; after that time calls go through again. No retry storm.
  #1134: the block is skipped only when that read shows ``remaining > 0`` (the window already rolled); a read
  with ``remaining == 0`` or without an int ``remaining`` blocks until the reset, as before.
* No console window is ever opened (``util.no_window_flags``).
* #148 rule 4: every attempted call (a failed one and a call the rate-limit block answers, marked
  ``"blocked": true``) appends one JSON line to ``GH_CALLS_LOG``: ``at``, ``module`` (the calling module),
  ``kind`` (``rest`` / ``graphql``) and ``endpoint``. The write never raises and never delays the call.

Standard library plus ``.util`` only, so the module stays copyable with the rest of the package.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

from .util import no_window_flags

#: One ``gh`` call waits at most this long.
GH_TIMEOUT_SECONDS = 45.0
#: The append-only log of every attempted ``run_gh`` call (#148 rule 4).
GH_CALLS_LOG = Path(__file__).resolve().parents[2] / "var" / "agentops" / "gh_calls.jsonl"
#: After a timeout the pipes are drained for at most this long before the call gives up on them.
_DRAIN_SECONDS = 3.0
_KILL_SECONDS = 10.0

#: A gh / network flake in a failure's text: both a transient marker and the word gh/github must be there,
#: so a plain failure ("not found", "bad credentials") never matches.
_TRANSIENT_MARKER_RE = re.compile(
    r"error connecting to|HTTP (?:502|503|504)\b|Bad Gateway|Service Unavailable|Gateway Time-?out|"
    r"TLS handshake timeout|connection reset by peer|i/o timeout|could not resolve host|"
    r"secondary rate limit|API rate limit (?:already )?exceeded|dial tcp", re.I)
_GH_WORD_RE = re.compile(r"\bgh\b|github", re.I)


def is_transient_gh_failure(tail: Iterable[str]) -> bool:
    """True when the text shows a flaky `gh` / GitHub call (the one transient failure a text can prove)."""

    text = "\n".join(tail)
    return bool(_TRANSIENT_MARKER_RE.search(text) and _GH_WORD_RE.search(text))


class GhResult(subprocess.CompletedProcess):
    """A ``CompletedProcess`` that also says whether a failure was transient."""

    transient: bool = False


class GhCalledProcessError(subprocess.CalledProcessError):
    """``check=True`` on a nonzero return code; ``transient`` as on ``GhResult``."""

    transient: bool = False


#: The primary rate limit in a failure's text (the secondary one, left to the callers' own retry, says "secondary").
_PRIMARY_LIMIT_RE = re.compile(r"rate limit (?:already )?exceeded|RATE_LIMIT", re.I)
#: Waits this long when the reset time cannot be read.
_RESET_FALLBACK_SECONDS = 60.0
#: `gh` commands that talk REST (`core`); every other command, and `gh api graphql`, spends the GraphQL budget.
_REST_COMMANDS = frozenset({"run", "workflow", "release", "gist", "cache", "secret", "variable"})

_now = time.time
#: resource ("graphql" / "core") -> epoch second before which a call is answered without starting `gh`.
_blocked_until: dict[str, float] = {}
_blocked_lock = threading.Lock()


def _is_rate_limit_read(args: list[str]) -> bool:
    return args[:2] == ["api", "rate_limit"]


def _resource_of(args: list[str]) -> str:
    if args[:1] == ["api"]:
        return "graphql" if "graphql" in args else "core"
    return "core" if args[:1] and args[0] in _REST_COMMANDS else "graphql"


#: `gh api` flags that take their value in the next argument (that value is not the endpoint).
_API_VALUE_FLAGS = frozenset({"-X", "--method", "-f", "-F", "--raw-field", "--field", "-H", "--header",
                              "--input", "-q", "--jq", "-t", "--template", "--cache", "--hostname", "-p",
                              "--preview"})


def _endpoint_of(args: list[str]) -> str:
    """``gh api <path>``: the first positional after ``api``, without its query string; any other command:
    its first two args (``issue view``)."""

    if args[:1] == ["api"]:
        rest = args[1:]
        i = 0
        while i < len(rest):
            token = rest[i]
            if token in _API_VALUE_FLAGS:
                i += 2
            elif token.startswith("-"):
                i += 1
            else:
                return token.split("?", 1)[0]
        return "api"
    return " ".join(args[:2]).split("?", 1)[0]


def _calling_module() -> str:
    """The last dotted part of the first module on the stack that is not this one; for a script or ``python -m``
    (``__main__``) the stem of its file; ``unknown`` if neither exists."""

    frame = sys._getframe(1)
    while frame is not None:
        glob = frame.f_globals
        name = glob.get("__name__", "")
        if name != __name__:
            if name == "__main__":
                file = glob.get("__file__")
                return Path(str(file)).stem if file else "unknown"
            return name.rsplit(".", 1)[-1] or "unknown"
        frame = frame.f_back
    return "unknown"


def _log_gh_call(args: list[str], *, blocked: bool) -> None:
    """Append one JSON line for an attempted ``run_gh`` call (#148 rule 4). Never raises and never breaks the call:
    the whole body is guarded, a non-str arg is read as its str, and the single ``write`` keeps the line whole."""

    try:
        text_args = [str(arg) for arg in args]
        entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "module": _calling_module(),
                 "kind": "graphql" if _resource_of(text_args) == "graphql" else "rest",
                 "endpoint": _endpoint_of(text_args)}
        if blocked:
            entry["blocked"] = True
        GH_CALLS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(GH_CALLS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 - the log never breaks a gh call (#148)
        pass


def _read_reset(resource: str) -> float | None:
    """The epoch second to block the resource until (``gh api rate_limit``), or None when the read shows budget
    left (``remaining > 0``: the window already rolled, so no block). now + 60 s when the read fails."""

    fallback = _now() + _RESET_FALLBACK_SECONDS
    try:
        read = _spawn_gh(["api", "rate_limit"], cwd=None, input=None, text=True, check=False, timeout=GH_TIMEOUT_SECONDS)
        if read.returncode:
            return fallback
        entry = json.loads(read.stdout)["resources"][resource]
        reset = float(entry["reset"])
        remaining = entry.get("remaining")
    except Exception:  # noqa: BLE001 - any failure of the read is the 60 s fallback
        return fallback
    if isinstance(remaining, int) and remaining > 0:
        return None
    return max(reset, _now() + 1.0)


def rate_limit_reset(resource: str = "core") -> float | None:
    """#148: the epoch second the resource's rate limit resets (``gh api rate_limit``), None when budget is left,
    now + 60 s when the read fails. A thin public wrapper of ``_read_reset``."""

    return _read_reset(resource)


def _blocked_result(args: list[str], until: float, check: bool) -> GhResult:
    stamp = time.strftime("%H:%M:%S", time.localtime(until))
    err = f"gh: API rate limit exceeded, waiting until {stamp}"
    argv = ["gh", *args]
    if check:
        error = GhCalledProcessError(1, argv, output="", stderr=err)
        error.transient = True
        raise error
    result = GhResult(argv, 1, "", err)
    result.transient = True
    return result


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=_KILL_SECONDS, creationflags=no_window_flags())
    except Exception:  # noqa: BLE001 - the direct kill below still runs
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def run_gh(args: list[str], *, cwd: str | Path | None = None, input: str | bytes | None = None,
           text: bool = True, check: bool = False, timeout: float = GH_TIMEOUT_SECONDS) -> GhResult:
    """Run ``gh <args>`` bounded by ``timeout``; see the module docstring for the contract."""

    if _is_rate_limit_read(args):
        _log_gh_call(args, blocked=False)
        return _spawn_gh(args, cwd=cwd, input=input, text=text, check=check, timeout=timeout)
    resource = _resource_of(args)
    with _blocked_lock:
        until = _blocked_until.get(resource, 0.0)
    blocked = _now() < until
    _log_gh_call(args, blocked=blocked)
    if blocked:
        return _blocked_result(args, until, check)
    try:
        result = _spawn_gh(args, cwd=cwd, input=input, text=text, check=check, timeout=timeout)
    except GhCalledProcessError as exc:
        _note_rate_limit(resource, exc.returncode, exc.stderr, exc.output)
        raise
    _note_rate_limit(resource, result.returncode, result.stderr, result.stdout)
    return result


def _note_rate_limit(resource: str, rc: int, err: object, out: object) -> None:
    """After a failed call: when its text is the primary rate limit, block the resource until its reset."""

    if not rc:
        return
    text = " ".join((_as_text(err), _as_text(out)))
    if "secondary" in text.lower() or not _PRIMARY_LIMIT_RE.search(text):
        return
    until = _read_reset(resource)
    if until is None:
        return
    with _blocked_lock:
        _blocked_until[resource] = until


def _spawn_gh(args: list[str], *, cwd: str | Path | None, input: str | bytes | None, text: bool, check: bool,
              timeout: float) -> GhResult:
    """One ``gh`` process, bounded by ``timeout``; no rate-limit memory."""

    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                    "stdin": subprocess.PIPE if input is not None else None,
                    "cwd": str(cwd) if cwd is not None else None}
    if text:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    proc = subprocess.Popen(["gh", *args], creationflags=no_window_flags(), **kwargs)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=_DRAIN_SECONDS)
        except Exception:  # noqa: BLE001 - a pipe held by a stray child is abandoned
            out, err = exc.stdout, exc.stderr
            for pipe in (proc.stdout, proc.stderr, proc.stdin):
                try:
                    if pipe:
                        pipe.close()
                except Exception:  # noqa: BLE001
                    pass
        raise subprocess.TimeoutExpired(proc.args, timeout, output=out, stderr=err) from None
    except BaseException:
        _kill_tree(proc)
        raise
    rc = proc.returncode
    transient = bool(rc) and is_transient_gh_failure(["gh", _as_text(err), _as_text(out)])
    if check and rc:
        error = GhCalledProcessError(rc, proc.args, output=out, stderr=err)
        error.transient = transient
        raise error
    result = GhResult(proc.args, rc, out, err)
    result.transient = transient
    return result
