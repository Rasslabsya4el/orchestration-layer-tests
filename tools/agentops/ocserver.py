"""One local `opencode serve` per machine, and a small HTTP client for its API.

Ported from `orchlib/ocserver.py` (the owner's separate plugin repo, its own location one of
the paths in `config/orchestration.json`), trimmed to this repo's needs (requirement 1 of
issue #46): the server listens on 127.0.0.1 with a random password, every request names the
working directory with `?directory=`, so one server serves every worker worktree of this
project. State (pid, port, password) lives under `AGENTOPS_HOME`, which defaults to
`<repo>/var/agentops` -- `var/` is already a directory junction shared by every worktree
(`docs/STATE.md`), so this state file is naturally "one per machine" the same way the
installation's own resource-budget lock file (`config/orchestration.json`'s `bridge_lock`)
is for its own shared budget.

Provider and models come ONLY from this repo's `opencode.json` (requirement 1): `build_config`
reads it and adds nothing to `provider`. What the bridge adds on top: the bash deny-list
(requirement 3, `BASH_DENY`, ported verbatim from the plugin's `orchlib/ocserver.py`), the
question tool and Exa web search toggles used by the two entry points, and `share: disabled`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from fnmatch import fnmatchcase
from pathlib import Path

from .util import AgentOpsError, file_lock, no_window_flags, now, pid_alive, read_json, write_json

ROOT = Path(__file__).resolve().parents[2]
HOME_DIR = Path(os.environ.get("AGENTOPS_HOME") or (ROOT / "var" / "agentops"))
STATE_FILE = HOME_DIR / "server.json"
SESSIONS_FILE = HOME_DIR / "sessions.json"  # every live bridge session on this machine

# Shell commands a worker must not run (ported verbatim from `orchlib/ocserver.py` BASH_DENY,
# requirement 3). opencode's own permission engine applies "last matching rule wins", so "*"
# comes first; `is_denied` below re-implements that rule for tests that have no real opencode.
BASH_DENY = [
    "*taskkill*", "*stop-process*", "kill *", "*pkill*", "*killall*", "*wmic*", "rm *", "*remove-item*",
    "git stash*", "git clean*", "git push*", "git reset --hard*", "git checkout .*", "git checkout -- .*",
    "git restore .*", "git branch -D*", "*--force*",
]


def is_denied(command: str) -> bool:
    """Return whether `command` is blocked by `BASH_DENY`, the same "last match wins" rule
    opencode applies to its own `permission.bash` table (default allow, deny patterns after).

    Matched case-insensitively on every OS: `fnmatch.fnmatch` only lower-cases via
    `os.path.normcase`, which is a no-op outside Windows, so on Linux/macOS it would let
    differently-cased commands (e.g. `Remove-Item -Recurse -Force var`) through the deny-list.
    Lowering both sides and using `fnmatchcase` gives identical behaviour on every OS,
    including today's Windows behaviour."""
    command_lower = command.lower()
    decision = "allow"
    for pattern in BASH_DENY:
        if fnmatchcase(command_lower, pattern.lower()):
            decision = "deny"
    return decision == "deny"


def find_opencode() -> str:
    """The real opencode binary. The npm .cmd/.ps1 shim starts a child, so a stop by PID would
    miss it (ported from `orchlib/ocserver.py`)."""
    configured = os.environ.get("AGENTOPS_OPENCODE") or os.environ.get("ORCH_OPENCODE")
    if configured:
        return configured
    found = shutil.which("opencode.cmd") or shutil.which("opencode")
    if found and os.name == "nt":
        exe = Path(found).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if exe.is_file():
            return str(exe)
    if found:
        return found
    raise AgentOpsError("opencode not found: install it (npm i -g opencode-ai) or set AGENTOPS_OPENCODE")


class BridgeConfigMissing(AgentOpsError):
    """This repo's `opencode.json` is absent or empty: the bridge cannot start (#59). Only this
    condition is caught by the `research` dispatch; every other bridge failure still raises. A file that still
    holds the port's stub (a provider baseURL on the reserved `.invalid` host) counts as missing: the owner has
    not filled it in."""

    def __init__(self, path: Path, why: str = "is missing or empty", *, also: Path | None = None):
        # #229: `also` is the main working tree's copy when the bridge runs from a linked worktree; both are named.
        self.path = Path(path)
        self.also = Path(also) if also is not None else None
        self.why = why
        self.where = str(self.path) if self.also is None else f"{self.path} and {self.also}"
        super().__init__(f"{self.where} {why}: no provider/model table to build the server config from")


def bridge_unavailable_line(exc: BridgeConfigMissing) -> str:
    """The one line `research` prints when the bridge cannot start (#59)."""
    return (f"research bridge cannot start: {exc.where} {exc.why}; use researcher-how (internal) "
            f"for this research, or fill the file in (docs/agents/research.md)")


def _main_working_tree(root: Path) -> Path | None:
    """The main working tree of the repo `root` is a linked worktree of; None when `root` is not a linked
    worktree (the main tree itself, a plain repo, no git) (#229). Read-only git call, no console window."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-dir", "--git-common-dir"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                              creationflags=no_window_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) != 2:
        return None
    git_dir, common_dir = ((root / line.strip()).resolve() for line in lines)
    if git_dir == common_dir:
        return None
    return common_dir.parent


def _checked_config(path: Path) -> dict:
    """The provider/model table of the `opencode.json` at `path`; raises BridgeConfigMissing when absent,
    empty or still the port's stub."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    data = read_json(path, default=None) if text.strip() else None
    if not data:
        raise BridgeConfigMissing(path)
    for provider in (data.get("provider") or {}).values():
        host = urllib.parse.urlparse(str(((provider or {}).get("options") or {}).get("baseURL", ""))).hostname or ""
        if host.endswith(".invalid"):
            raise BridgeConfigMissing(path, "still holds the port's stub (baseURL on a .invalid host): fill it in")
    return data


def read_opencode_config(config_path: Path | None = None) -> dict:
    """This repo's `opencode.json`: provider and models only (requirement 1). #229: an explicit `config_path`
    wins and is read alone. Otherwise `ROOT / "opencode.json"`; when that is absent and `ROOT` is a linked
    worktree, the main working tree's `opencode.json` of the same repo is read in place (never copied, never
    printed). Absent in both places, the error names both paths."""
    if config_path is not None:
        return _checked_config(Path(config_path))
    own = ROOT / "opencode.json"
    if own.is_file():
        return _checked_config(own)
    main_tree = _main_working_tree(ROOT)
    if main_tree is None:
        return _checked_config(own)
    shared = main_tree / "opencode.json"
    if not (shared.is_file() and shared.read_text(encoding="utf-8").strip()):
        raise BridgeConfigMissing(own, "are both missing or empty", also=shared)
    return _checked_config(shared)


def build_config(base: dict | None = None, config_path: Path | None = None) -> dict:
    """The `OPENCODE_CONFIG_CONTENT` handed to `opencode serve`: this repo's provider/model
    table (unchanged) plus the deny-list and the toggles the two entry points need."""
    source = base if base is not None else read_opencode_config(config_path)
    providers = source.get("provider", {})
    permission = dict(source.get("permission", {}))
    bash = {"*": "allow", **{pattern: "deny" for pattern in BASH_DENY}}
    permission["bash"] = bash
    permission.setdefault("external_directory", "deny")
    permission.setdefault("question", "allow")
    # #174: no sub-sessions (opencode's `task` tool starts an @general child) whatever the repo says; placed
    # last in key order, so it beats a repo's `"*": "allow"` (opencode: the last matching rule wins)
    permission.pop("task", None)
    permission["task"] = "deny"
    return {
        "$schema": source.get("$schema", "https://opencode.ai/config.json"),
        "autoupdate": False,
        "share": "disabled",
        "provider": providers,
        "permission": permission,
        "agent": {
            # a worker has no web access by default (ported from orchlib's "orch" agent)
            "agentops": {"mode": "primary", "description": "agentops worker",
                        "permission": {"webfetch": "deny", "task": "deny"}},
            # a research run needs it (requirement 2, `research`; ported from orchlib's "orch-web");
            # no sub-sessions either (#174)
            "agentops-research": {"mode": "primary", "description": "agentops research (web search on)",
                                  "permission": {"webfetch": "allow", "task": "deny"}},
        },
    }


def config_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


def _signature(cfg: dict, enable_exa: bool) -> str:
    """`config_hash(cfg)` alone does not change with `enable_exa` (it is an env var, not part of
    `OPENCODE_CONFIG_CONTENT`), so a server started once without it was silently reused for a
    later `research` call that needed it -- the smoke run of 2026-09-26 (issue #46 follow-up):
    2.9 s, 0 tokens, "no reddit.com URL", because the already-running process had no websearch
    tool registered at all. `ensure`/`start` compare THIS signature, not the bare config hash."""
    return f"{config_hash(cfg)}:exa={int(bool(enable_exa))}"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Client:
    """HTTP client for one running `opencode serve`, base URL overridable for tests (a fake
    HTTP server stands in for `opencode serve`; no real network call, no real opencode, per
    issue #46)."""

    def __init__(self, state: dict, base_url: str | None = None):
        self.port = state.get("port")
        password = state.get("password", "")
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self.auth = f"Basic {token}"
        self.base_url = base_url or f"http://127.0.0.1:{self.port}"

    def url(self, path: str, directory: str | None = None) -> str:
        url = f"{self.base_url}{path}"
        if directory:
            url += ("&" if "?" in url else "?") + "directory=" + urllib.parse.quote(str(directory))
        return url

    def call(self, method: str, path: str, directory: str | None = None, body=None, timeout: float = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url(path, directory), data=data, method=method)
        req.add_header("Authorization", self.auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AgentOpsError(f"opencode {method} {path}: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentOpsError(f"opencode {method} {path}: {exc}") from exc
        return json.loads(raw) if raw.strip() else None

    # -- session lifecycle (requirement 1) --------------------------------------------------

    def create_session(self, directory: str, title: str) -> dict:
        return self.call("POST", "/session", directory, {"title": title})

    def prompt_async(self, directory: str, session_id: str, *, agent: str, provider_id: str, model: str,
                      system: str, text: str, effort: str | None = None) -> None:
        body = {"agent": agent, "model": {"providerID": provider_id, "modelID": model}, "system": system,
                "parts": [{"type": "text", "text": text}]}
        if effort:
            body["variant"] = effort
        self.call("POST", f"/session/{session_id}/prompt_async", directory, body)

    def steer(self, directory: str, session_id: str, *, agent: str, provider_id: str, model: str,
              system: str, text: str, effort: str | None = None) -> None:
        """Send another message into an already-running session (same call as `prompt_async`;
        named separately because a lead steering a live worker is a distinct action, `orchlib`'s
        `steer`)."""
        self.prompt_async(directory, session_id, agent=agent, provider_id=provider_id, model=model,
                          system=system, text=text, effort=effort)

    def abort(self, directory: str, session_id: str) -> None:
        self.call("POST", f"/session/{session_id}/abort", directory)

    def open_questions(self, directory: str, session_id: str) -> list:
        pending = self.call("GET", "/question", directory) or []
        return [q for q in pending if q.get("sessionID") == session_id]

    def answer_question(self, directory: str, request_id: str, answers: list[str]) -> None:
        self.call("POST", f"/question/{request_id}/reply", directory, {"answers": [[t] for t in answers]})

    def messages(self, directory: str, session_id: str, timeout: float = 60) -> list:
        return self.call("GET", f"/session/{session_id}/message", directory, timeout=timeout) or []

    def usage(self, directory: str, session_id: str) -> dict | None:
        """Token totals of a session (assistant messages carry `info.tokens`); None when there
        is nothing to total yet (ported from `orchlib/runner.py` `session_usage`)."""
        try:
            data = self.messages(directory, session_id)
        except AgentOpsError:
            return None
        total = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "messages": 0}
        for item in data:
            info = item.get("info") or {}
            tok = info.get("tokens") if info.get("role") == "assistant" else None
            if not isinstance(tok, dict):
                continue
            total["messages"] += 1
            total["input"] += int(tok.get("input") or 0)
            total["output"] += int(tok.get("output") or 0)
            total["reasoning"] += int(tok.get("reasoning") or 0)
            cache = tok.get("cache") or {}
            total["cache_read"] += int(cache.get("read") or 0)
            total["cache_write"] += int(cache.get("write") or 0)
        return total if total["messages"] else None

    def statuses(self, directories) -> dict:
        """Session id -> status type (busy, retry) of the sessions that are not idle."""
        out = {}
        for directory in set(map(str, directories)):
            if not Path(directory).exists():
                continue
            data = self.call("GET", "/session/status", directory, timeout=10) or {}
            out.update({sid: (st or {}).get("type", "?") for sid, st in data.items()
                        if (st or {}).get("type") != "idle"})
        return out

    def last_error(self, directory: str, session_id: str) -> str | None:
        """The error of the last message that carries one (opencode reports a failed model call
        as `info.error` on a message, not as an HTTP error), or None."""
        try:
            data = self.messages(directory, session_id)
        except AgentOpsError as exc:
            return str(exc)
        for item in reversed(data):
            error = (item.get("info") or {}).get("error")
            if error:
                return json.dumps(error, ensure_ascii=False)[:300] if isinstance(error, dict) else str(error)
        return None


# The very first `/session/status` check after `prompt_async` returns races opencode's own async
# job start: `prompt_async` acknowledges receipt before generation begins, and the status can
# still read idle for a moment after that. Accepting "idle" on that very first check reads as
# "the session already finished" when in fact it never started -- the smoke run of 2026-09-26
# (issue #46 follow-up): a research call returned after 2.9 s with 0 tokens because the poll
# loop's first (and only) check exited immediately. `wait_for_idle` refuses to call it finished
# on "idle" alone until it has EITHER seen the session busy at least once, OR that first grace
# window has fully elapsed (a call that fails before ever going busy is still real, e.g. a bad
# model id rejected synchronously -- the grace window bounds how long that takes to surface,
# it does not wait forever).
MIN_GRACE_SECONDS = 10.0
POLL_SECONDS = 2.0

# #333: a session with no new step for this long (by the wall clock, from the step times of its
# messages) is stalled: the run stops it and keeps the text parts it has.
STALL_MINUTES = 10.0
STALL_REASON = f"stalled: no new step for {STALL_MINUTES:g} min"
# #333: the status poll stays at POLL_SECONDS; the full message read and the progress reader run
# at most this often (a session's message list grows with every step).
PROGRESS_EVERY_S = 15.0


class Progress:
    """What a session has done so far, read from its messages (#333)."""

    def __init__(self, *, steps: int, last_step_s: float | None, sources: tuple, reddit: bool):
        self.steps = steps
        self.last_step_s = last_step_s  # epoch seconds of the newest step, None when there is none
        self.sources = sources          # distinct URLs of completed webfetch calls, in order
        self.reddit = reddit            # a completed webfetch of reddit.com (or a subdomain)


def session_progress(messages: list) -> Progress:
    """Steps, sources and Reddit reach of a session, from its `/session/{id}/message` list (#333).

    A step is a `step-start` part (one model call). Its time is the creation time of the assistant
    message that holds it (`info.time.created`, epoch ms). A source is the URL of a completed
    `webfetch` call; a web search holds only its query, so its results are not counted. Reddit is
    reached when a completed webfetch URL's host is reddit.com or ends in .reddit.com."""
    steps = 0
    last_ms = None
    sources: list[str] = []
    reddit = False
    for item in messages:
        created = ((item.get("info") or {}).get("time") or {}).get("created")
        for part in item.get("parts") or []:
            kind = part.get("type")
            if kind == "step-start":
                steps += 1
                if isinstance(created, (int, float)):
                    last_ms = created if last_ms is None else max(last_ms, created)
            elif kind == "tool" and part.get("tool") == "webfetch":
                state = part.get("state") or {}
                url = (state.get("input") or {}).get("url") or ""
                if state.get("status") == "completed" and url:
                    if url not in sources:
                        sources.append(url)
                    host = urllib.parse.urlsplit(url).hostname or ""
                    if host == "reddit.com" or host.endswith(".reddit.com"):
                        reddit = True
    return Progress(steps=steps, last_step_s=None if last_ms is None else last_ms / 1000,
                    sources=tuple(sources), reddit=reddit)


def _age(seconds: float) -> str:
    """A duration a human reads at a glance: seconds under 90 s, minutes under 90 min, then hours."""
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} s"
    if s < 90 * 60:
        return f"{s // 60} min"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def progress_line(progress: Progress, *, now: float) -> str:
    """The one status line of a research run (#333): steps, minutes since the last step, sources,
    and whether reddit.com was reached. Built only here, so a caller polling it gets the same text."""
    last = "none" if progress.last_step_s is None else f"{_age(now - progress.last_step_s)} ago"
    reached = "yes" if progress.reddit else "no"
    return (f"steps {progress.steps}; last step {last}; sources {len(progress.sources)}; "
            f"reddit reached: {reached}")


def wait_for_idle(client: "Client", directory: str, session_id: str, *, minutes: float,
                  min_grace_seconds: float = MIN_GRACE_SECONDS, poll_seconds: float = POLL_SECONDS,
                  stall_minutes: float = STALL_MINUTES, progress_every_s: float = PROGRESS_EVERY_S,
                  on_progress=None, sleep=time.sleep, clock=time.monotonic, wall=time.time) -> str:
    """Poll until the session is idle (and was seen busy, or the grace window passed), the time
    limit is reached, or the session is stalled (#333: no new step for `stall_minutes` on the wall
    clock, the newest step time read from the session's messages; from the start of the wait when
    no step exists yet). The status is polled every `poll_seconds`; the full message read and the
    progress reader run at most every `progress_every_s`, and each time the progress is taken
    `on_progress(progress)` is called. A transient AgentOpsError from the message read skips that
    read only. Returns "finished", "time limit" or "stalled"."""
    started = clock()
    deadline = started + minutes * 60
    stall_s = stall_minutes * 60
    began = wall()
    seen_busy = False
    quiet_from = began  # the newest step time known so far, else the start of the wait
    last_read = None    # the clock time of the last full progress read
    while True:
        now = clock()
        if now >= deadline:
            return "time limit"
        busy = session_id in client.statuses([directory])
        seen_busy = seen_busy or busy
        if not busy and (seen_busy or now - started >= min_grace_seconds):
            return "finished"
        # a stall is judged on a read taken in this round, so a step of the last 15 s is not missed
        stall_due = wall() - quiet_from >= stall_s
        if last_read is None or now - last_read >= progress_every_s or stall_due:
            last_read = now
            try:
                progress = session_progress(client.messages(directory, session_id))
            except AgentOpsError:
                progress = None  # transient: this round keeps the step time it already had
            if progress is not None:
                if on_progress is not None:
                    on_progress(progress)
                if progress.last_step_s is not None:
                    quiet_from = progress.last_step_s
        if wall() - quiet_from >= stall_s:
            return "stalled"
        until_stall = quiet_from + stall_s - wall()
        sleep(min(poll_seconds, max(0.05, deadline - now), max(0.05, until_stall)))


def load_state(base_url: str | None = None) -> dict | None:
    state = read_json(STATE_FILE)
    if not state or (base_url is None and not pid_alive(state.get("pid"))):
        return None
    try:
        Client(state, base_url=base_url).call("GET", "/session/status", timeout=5)
    except (AgentOpsError, OSError):
        return None
    return state


def start(*, config: dict | None = None, config_path: Path | None = None, enable_exa: bool = False,
          extra_env: dict | None = None) -> dict:
    cfg = config if config is not None else build_config(config_path=config_path)
    password = secrets.token_urlsafe(24)
    port = free_port()
    env = dict(os.environ)
    env.update({"OPENCODE_SERVER_PASSWORD": password, "OPENCODE_CONFIG_CONTENT": json.dumps(cfg),
                "OPENCODE_ENABLE_QUESTION_TOOL": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1"})
    if enable_exa:
        env["OPENCODE_ENABLE_EXA"] = "1"  # requirement 2, `research`: websearch on
    if extra_env:
        env.update(extra_env)
    # The API key is read from the environment by opencode itself (`{env:CODEX_LB_API_KEY}` in
    # opencode.json); this bridge never reads, logs, or writes it (issue #46 rules).
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    log = open(HOME_DIR / "server.log", "a", encoding="utf-8")
    log.write(f"\n--- {now()} start on port {port}\n")
    log.flush()
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
    proc = subprocess.Popen([find_opencode(), "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                            cwd=HOME_DIR, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    state = {"pid": proc.pid, "port": port, "password": password, "started": now(),
            "config": _signature(cfg, enable_exa)}
    write_json(STATE_FILE, state)
    for _ in range(60):
        time.sleep(0.5)
        if proc.poll() is not None:
            raise AgentOpsError(f"opencode server exited at start, see {HOME_DIR / 'server.log'}")
        if load_state():
            return state
    raise AgentOpsError(f"opencode server did not answer in 30 s, see {HOME_DIR / 'server.log'}")


def stop() -> str:
    state = read_json(STATE_FILE)
    if not state or not pid_alive(state.get("pid")):
        return "server not running"
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(state["pid"]), "/T", "/F"], capture_output=True,
                       creationflags=no_window_flags())
    else:
        os.kill(state["pid"], 15)
    return f"server stopped (pid {state['pid']})"


def ensure(*, config_path: Path | None = None, enable_exa: bool = False) -> Client:
    """A client for the running server; start it, or restart an idle one whose settings
    (INCLUDING `enable_exa`, not just the config file) changed."""
    state = load_state()
    wanted = _signature(build_config(config_path=config_path), enable_exa)
    if state and state.get("config") != wanted:
        busy = Client(state).statuses(s["directory"] for s in registry().values())
        if busy:
            raise AgentOpsError("the opencode server runs with other settings and has busy sessions; "
                                "finish them or stop the server first")
        stop()
        state = None
    if not state:
        state = start(config_path=config_path, enable_exa=enable_exa)
    return Client(state)


def current() -> Client:
    state = load_state()
    if not state:
        raise AgentOpsError("opencode server is not running")
    return Client(state)


def registry() -> dict:
    return read_json(SESSIONS_FILE, {}) or {}


def register(session_id: str, **fields) -> None:
    """Record a live bridge session. #679: also the Claude Code session that launched it (its shell's
    `CLAUDE_CODE_SESSION_ID`; None outside Claude Code) and the start time, so the monitor draws the
    run under that session."""
    fields.setdefault("claude_session", os.environ.get("CLAUDE_CODE_SESSION_ID") or None)
    fields.setdefault("started", now())
    with file_lock(SESSIONS_FILE):
        data = registry()
        data[session_id] = fields
        write_json(SESSIONS_FILE, data)


def unregister(session_id: str) -> None:
    with file_lock(SESSIONS_FILE):
        data = registry()
        if data.pop(session_id, None) is not None:
            write_json(SESSIONS_FILE, data)
