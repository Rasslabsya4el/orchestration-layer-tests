"""Data for the monitor mod. One JSON object on stdout. The generic collector: it knows the tracker, the agents, the
merge line, the cost and the machine load; everything that belongs to one project is an add-on file (`monitor.addon`
in `config/orchestration.json`, loaded by `addon()`; absent = none).

    python collect.py fast      # sessions + subagents, merge line, machine load (+ the add-on's sections) (~1 s)
    python collect.py tickets   # every issue as a tree: parent, sub-issues, state dot (~10 s, gh)
    python collect.py slow      # Results, Cost (day, week, month) panels of tools/agentops/monitor_dashboard
    python collect.py orch      # ORCH / autopilot / helper session stats per period (cache in bin/.cache)
    python collect.py serve     # long-lived: fast() every monitor.refresh_s into the pane's collector-fast.json (#142, #166)
    python collect.py <mode>    # any extra mode the add-on lists in its MODES

The project root is found from the working directory (or `MONITOR_ROOT`): the nearest folder holding
`config/orchestration.json`. Read-only: nothing here writes to the repo.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

if __name__ == "__main__":  # the add-on imports `collect`: it must be this very module, not a second copy
    sys.modules.setdefault("collect", sys.modules["__main__"])

CONFIG = Path("config") / "orchestration.json"


def find_root() -> Path:
    """The project root: `MONITOR_ROOT`, else the nearest folder up from the working directory that holds the config."""
    env = os.environ.get("MONITOR_ROOT")
    if env:
        return Path(env)
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / CONFIG).is_file():
            return d
    return here


REPO = find_root()


def settings(root: Path | None = None) -> dict:
    """`config/orchestration.json` of the project (empty when there is none)."""
    try:
        return json.loads(((root or REPO) / CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


#: defaults of `monitor.refresh_s` (the pane's fast pull, seconds) and `monitor.agents_every_s` (how long the
#: `claude agents` list is shared, seconds), the same numbers as config/orchestration.json (#139)
REFRESH_S_DEFAULT = 5
AGENTS_EVERY_S_DEFAULT = 20


def monitor_setting(key: str, default: float) -> float:
    """`monitor.<key>` of the project's settings, read at each call (#139): a missing, non-numeric, non-finite or
    <= 0 value is the default."""
    mon = settings().get("monitor")
    value = (mon if isinstance(mon, dict) else {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return default
    return value


sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_REPO_NAME = str(settings().get("repo") or "")
GH_REPO = tuple(_REPO_NAME.split("/", 1)) if "/" in _REPO_NAME else ("", "")  # the tracker: (owner, name)
NOW = datetime.now(timezone.utc)
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_DIR = Path(os.environ.get("MONITOR_CACHE_DIR") or _DEFAULT_CACHE_DIR)  # #113: override
_ADDON: list = []


def addon():
    """The project's add-on module, or None. `monitor.addon` is a file path under the project root; a module already
    registered as `monitor_addon` (the remote program below) wins. Loaded once; a broken file raises."""
    if _ADDON:
        return _ADDON[0]
    mod = sys.modules.get("monitor_addon")
    rel = (settings().get("monitor") or {}).get("addon")
    if mod is None and rel:
        path = (REPO / rel).resolve()
        if path.is_file():
            sys.path.insert(0, str(path.parent))
            spec = importlib.util.spec_from_file_location("monitor_addon", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["monitor_addon"] = mod
            spec.loader.exec_module(mod)
    _ADDON.append(mod)
    return mod


def addon_files() -> dict[str, str]:
    """Name -> source of every file this collector needs on a machine that has none of them: itself and the add-on."""
    out = {"collect": Path(__file__).read_text(encoding="utf-8")}
    mod = addon()
    if mod is not None:
        out["monitor_addon"] = Path(mod.__file__).read_text(encoding="utf-8")
    return out


def remote_program() -> str:
    """Python source to pipe into `python -` on another machine: it registers the collector and the add-on from the
    sources below (`MONITOR_ROOT` = this project's root there) and runs `main()` with the command line it was given."""
    return "\n".join([
        "import json, os, sys, types",
        f"S = json.loads({json.dumps(json.dumps(addon_files()))})",
        f"os.environ['MONITOR_ROOT'] = {str(REPO)!r}",
        "sys.argv[0] = 'collect.py'",
        "for name in ('collect', 'monitor_addon'):",
        "    if name in S:",
        "        m = types.ModuleType(name)",
        "        m.__file__ = '<stdin>'",
        "        sys.modules[name] = m",
        "        exec(compile(S[name], name + '.py', 'exec'), m.__dict__)",
        "sys.modules['collect'].main()",
        ""])


def _plain_duration(seconds: float) -> str:
    """The same ladder the project's formatter has, for a project without one: seconds under ~90 s, minutes, then hours."""
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours, rest = divmod(int(round(minutes)), 60)
    return f"{hours} h {rest:02d} min"


def hd(seconds: float | None) -> str:
    if seconds is None:
        return ""
    add = addon()
    fmt = getattr(add, "human_duration", None) or _plain_duration
    return fmt(max(0.0, float(seconds)))


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def words(slug: str) -> str:
    """`z329_juggernaut_boneshatter` -> `Juggernaut Boneshatter`: never a slug on the page."""
    parts = [p for p in slug.split("_") if not re.fullmatch(r"z?\d+", p)]
    return " ".join(p if p.isupper() else p.capitalize() for p in parts) or slug


# ---------------------------------------------------------------- fast

DIGEST_EVENTS = 8      # the last transcript events Haiku reads to say what an agent does (#683 r2)
DIGEST_TAIL = 96_000   # bytes read from the end of a transcript


def _tool_line(name: str, inp: dict) -> str:
    """One tool call in plain words: its own description when it has one, else its target."""
    for k in ("description", "subject", "activeForm"):
        if isinstance(inp.get(k), str) and inp[k].strip():
            return f"{name}: {inp[k].strip()}"
    for k in ("file_path", "path", "pattern", "command", "query", "url", "prompt", "message", "to"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            v = v.strip().replace("\\", "/")
            if k in ("file_path", "path"):
                v = v.rsplit("/", 1)[-1]
            return f"{name} {k}={v[:110]}"
    if name == "TodoWrite":
        todo = [x for x in inp.get("todos") or [] if isinstance(x, dict) and x.get("status") == "in_progress"]
        if todo:
            return f"todo: {str(todo[0].get('content') or '')[:110]}"
    return name


#: #112: a tool result that names the API rate limit ("API rate limit", any API: GitHub's is the one the strip shows as
#: «ждёт GitHub», as the session's budget) is an event too (`result: ...`), when it is an error or came from a background
#: command; a Read of a file that names the limit is not (this file does)
RATE_LIMIT_RESULT = re.compile(r"API rate limit", re.IGNORECASE)
BACKGROUND_TOOLS = ("Bash", "PowerShell", "Monitor")


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
    return ""


def digest(path: Path) -> dict:
    """{"digest": the last few events of a transcript, oldest first, "dk": its short hash}; {} when unreadable.
    An event = an assistant text (first 160 chars), a tool call (`_tool_line`), or a tool result that names the GitHub
    rate limit (#112). Other tool results are left out."""
    import hashlib
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - DIGEST_TAIL))
            raw = fh.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return {}
    events: list[str] = []
    names: dict[str, str] = {}  # #112: the tool name of each tool call of the tail, by its id
    for line in raw:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        kind = m.get("type")
        if kind not in ("assistant", "user"):
            continue
        for c in (m.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if kind == "assistant" and c.get("type") == "text" and str(c.get("text") or "").strip():
                events.append("say: " + " ".join(str(c["text"]).split())[:160])
            elif kind == "assistant" and c.get("type") == "tool_use":
                names[str(c.get("id") or "")] = str(c.get("name") or "tool")
                events.append(_tool_line(str(c.get("name") or "tool"), c.get("input") or {}))
            elif kind == "user" and c.get("type") == "tool_result":
                text = _result_text(c.get("content"))
                if RATE_LIMIT_RESULT.search(text) and (c.get("is_error") or names.get(str(c.get("tool_use_id") or "")) in BACKGROUND_TOOLS):
                    events.append("result: " + " ".join(text.split())[:160])
    if not events:
        return {}
    text = "\n".join(events[-DIGEST_EVENTS:])
    return {"digest": text, "dk": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]}


def _agent_status():
    """#1005: `tools.agentops.agent_status` (one status word per agent row), named by the settings key `agent_status`;
    None where the key is absent or the project's packages are."""
    return _settings_module("agent_status")


def _settings_module(key: str):
    """The module the settings key names (a dotted name, e.g. `monitor_dashboard`), imported at call time; None when the
    key is absent, the settings file is unreadable, or the module is not in this installation (the core names no repo module)."""
    try:
        name = settings().get(key)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(name, str) or not name:
        return None
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _sub_started(s: dict) -> str | None:
    """#6: when a live subagent began: this pull's time less its running duration (the row has only a human age)."""
    dur = s.get("duration_seconds")
    if not s.get("running") or dur is None:
        return None
    return (NOW - timedelta(seconds=dur)).isoformat(timespec="seconds")


def agents(raw: list | None = None) -> list[dict]:
    """The agents rows the Agents tile draws. `raw` (#136): the `claude agents` list the caller read at this
    instant; given, the shared `agents_raw` read is skipped and this list is used as it is."""
    from tools.agentops import monitor_agents as ma

    try:  # scripts/token_meter.py prices the transcripts; absent = no agents part
        tm = ma._token_meter()
    except ImportError:
        return []
    ags = _agent_status()
    if raw is None:
        # #139: `claude agents --json --all` (~1.2-1.5 CPU-s) is shared for `agents_every_s` across pulls and chats;
        # a failure of the cache path falls back to the direct call
        try:
            raw = json.loads(shared_result("agents_raw", monitor_setting("agents_every_s", AGENTS_EVERY_S_DEFAULT),
                                           ma.live_sessions_raw))
        except Exception:  # noqa: BLE001
            raw = []
        if not raw:  # a failed read is `[]` too, and must not blank the pane for a whole window: read it directly
            raw = ma.live_sessions_raw()
    live = [r for r in raw if r.get("kind") == "interactive" or r.get("state") not in ("done", "stopped", "failed")]
    prices = tm.load_prices(ma.DEFAULT_PRICES_PATH)
    from tools.agentops import monitor_pipeline as mp
    # #153: a finished side thread (idle, its own records older than this many seconds) gets no row
    rows = ma.panel_agents(live, projects_dir=ma.DEFAULT_PROJECTS_DIR, prices=prices,
                           repo_cwd=ma.ROOT, now=NOW, orch_state=ma.load_orch_state(),
                           cut_totals=mp.cut_totals_of(_pipeline_facts_read()),
                           side_thread_idle_drop_s=monitor_setting("side_thread_idle_drop_s", 600))["sessions"]
    # The dispatcher's ORCH keeps the subagents of the issues it holds under `held_issues` (#60);
    # `subagents` has only the rest, so its live workers vanished until their issue was let go.
    def subs_of(r: dict) -> list[dict]:
        return (r.get("subagents") or []) + [s for h in r.get("held_issues") or [] for s in h.get("subagents") or []]

    def sub_status(s: dict, sid: str) -> dict:
        """#1005: a subagent's status word: its transcript tail while running, else its ended state."""
        if ags is None:
            return {}
        if not s.get("running"):
            return {"status": ags.classify_ended("done")}
        path = ma.DEFAULT_PROJECTS_DIR / sid / "subagents" / f"agent-{s.get('agent_id')}.jsonl" if sid and s.get("agent_id") else None
        records = ags.read_tail(path) if path else []
        return {"status": ags.classify(records, kind="subagent", running_subagents=0)}

    out = []
    for r in rows:
        status = r.get("status") or r.get("state")
        sid = r.get("session_id") or ""
        row_status = {}
        if ags is not None:  # #1005: the transcript tail is read once here; the digest below reads it on its own
            row_status = {"status": ags.classify(
                ags.read_tail(ma.DEFAULT_PROJECTS_DIR / f"{sid}.jsonl") if sid else [],
                kind=ags.session_kind(r.get("kind"), r.get("name")), raw_status=r.get("status"),
                running_subagents=sum(1 for s in subs_of(r) if s.get("running")))}
        out.append({
            "sid": r.get("session_id") or "",
            "name": r.get("name") or r.get("session_id", "")[:8],
            "kind": r.get("kind"),
            "busy": status in ("busy", "working", "running"),
            "issue": (r.get("issue") or {}).get("number"),
            "held": r.get("held_issues") or [],
            "ticket_units": r.get("ticket_units") or {},
            "model": r.get("model") or "",
            "ctx": r.get("context") or 0,
            "age": r.get("age_human") or "",
            "last": ((r.get("last_actions") or [{}])[-1] or {}).get("summary", ""),
            # #683 r2: what the agent is doing, as its last transcript events; the mod has Haiku word it.
            **(digest(ma.DEFAULT_PROJECTS_DIR / f"{r.get('session_id')}.jsonl")
               if status in ("busy", "working", "running") and r.get("session_id") else {}),
            "subagents": [{
                "id": s.get("agent_id") or "",
                **(digest(ma.DEFAULT_PROJECTS_DIR / str(r.get("session_id")) / "subagents" / f"agent-{s.get('agent_id')}.jsonl")
                   if s.get("running") and s.get("agent_id") else {}),
                "name": s.get("task") or s.get("agent_type") or "",
                "type": s.get("agent_type") or "",
                "issue": s.get("issue"),
                "model": s.get("model") or "",
                "ctx": s.get("context") or 0,
                "age": s.get("duration_human") or "",
                "busy": bool(s.get("running")),
                "source": s.get("source", "transcript"),
                "started": _sub_started(s),
                **sub_status(s, sid),
            } for s in sorted(subs_of(r), key=lambda s: not s.get("running"))],
            **row_status,
        })
    out.sort(key=lambda r: (not r["busy"], r["kind"] != "interactive", r["name"]))
    try:
        attach_opencode(out, opencode(), ags)
    except Exception:  # noqa: BLE001 -- the opencode server down or odd: no rows, no error line (#678)
        pass
    return out


# ---------------------------------------------------------------- opencode agents (#678)

OC_RECENT_MS = 10 * 60 * 1000  # a finished opencode session stays in the tile 10 min
def _oc_launch_cache() -> Path:  # #182: resolved at each use, so a moved CACHE_DIR moves it
    return CACHE_DIR / "opencode_launch.json"


def oc_model(model_id: str | None) -> str:
    """`gpt-5.6-terra` -> `Terra 5.6`, `gpt-6-luna` -> `Luna 6`, `deepseek-flash` -> `DeepSeek`:
    the model column is 10 wide, like the Claude rows' `Opus 5.5`."""
    if not model_id:
        return ""
    try:
        from tools.agentops.models import model_name
        name = model_name(model_id)
    except Exception:  # noqa: BLE001
        name = model_id
    m = re.fullmatch(r"GPT-([\d.]+) (\w+)", name)
    if m:
        return f"{m[2]} {m[1]}"
    return name.split()[0] if name.lower().startswith("deepseek") else name


def _oc_last(messages: list) -> tuple[str, int]:
    """(what the session does now = its newest tool call's title or tool name, the context of its
    newest assistant message with tokens = input + cache read)."""
    last, ctx = "", 0
    for item in reversed(messages):
        info = item.get("info") or {}
        if info.get("role") != "assistant":
            continue
        if not last:
            for p in reversed(item.get("parts") or []):
                if p.get("type") == "tool" and p.get("tool") not in ("todowrite", "todoread"):
                    st = p.get("state") or {}
                    inp = st.get("input") if isinstance(st.get("input"), dict) else {}
                    what = st.get("title") or inp.get("description") or inp.get("command") or inp.get("filePath") or inp.get("query") or ""
                    last = f"{p.get('tool')}: {what}"[:80] if what else str(p.get("tool") or "")
                    break
        tok = info.get("tokens") or {}
        if not ctx and (tok.get("input") or (tok.get("cache") or {}).get("read")):
            ctx = int(tok.get("input") or 0) + int((tok.get("cache") or {}).get("read") or 0)
        if last and ctx:
            break
    return last, ctx


def opencode(client=None, registry: dict | None = None, now_ms: int | None = None,
             launch_cache: Path | None = None) -> list[dict]:
    """The local opencode server's sessions busy now or updated in the last 10 min, each
    {id, title, parent, claude, busy, model, ctx, age, last}. `claude` = the Claude Code session
    that launched the run (#679: the launcher's registry entry, remembered here after the run
    unregisters). Read-only: never creates, aborts or prompts a session. Server down -> []."""
    from tools.agentops import ocserver
    if client is None:
        state = ocserver.load_state()
        if not state:
            return []
        client = ocserver.Client(state)
    reg = ocserver.registry() if registry is None else registry
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cache_path = launch_cache or _oc_launch_cache()
    try:
        launched = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        launched = {}
    changed = False
    for sid, rec in reg.items():
        if isinstance(rec, dict) and rec.get("claude_session") and launched.get(sid, {}).get("claude") != rec["claude_session"]:
            launched[sid] = {"claude": rec["claude_session"], "at": now_ms, "dir": rec.get("directory")}
            changed = True
    dirs = {str(REPO)} | {str(v.get("dir")) for v in launched.values() if v.get("dir")} | \
        {str(r.get("directory")) for r in reg.values() if isinstance(r, dict) and r.get("directory")}
    sessions: dict[str, dict] = {}
    busy: set[str] = set()
    for d in dirs:
        if not Path(d).exists():
            continue
        for s in client.call("GET", "/session", d, timeout=10) or []:
            sessions.setdefault(s["id"], s)
        busy |= {k for k, v in (client.call("GET", "/session/status", d, timeout=10) or {}).items()
                 if (v or {}).get("type") not in (None, "idle")}
    keep = {k: s for k, s in sessions.items()
            if k in busy or now_ms - int((s.get("time") or {}).get("updated") or 0) <= OC_RECENT_MS}
    rows = []
    for sid, s in keep.items():
        t = s.get("time") or {}
        try:
            msgs = client.call("GET", f"/session/{sid}/message?limit=6", s.get("directory"), timeout=10) or []
        except Exception:  # noqa: BLE001
            msgs = []
        last, ctx = _oc_last(msgs)
        root = s
        walked = {root["id"]}  # #264: a parent cycle in the server's rows ends the walk
        while root.get("parentID") and root["parentID"] in sessions and root["parentID"] not in walked:
            root = sessions[root["parentID"]]
            walked.add(root["id"])
        rows.append({
            "id": sid, "title": s.get("title") or sid, "parent": s.get("parentID"),
            "claude": (launched.get(root["id"]) or {}).get("claude"),
            "busy": sid in busy, "model": oc_model((s.get("model") or {}).get("id")), "ctx": ctx,
            "age": hd((now_ms - int(t.get("created") or now_ms)) / 1000 if sid in busy
                      else (int(t.get("updated") or 0) - int(t.get("created") or 0)) / 1000),
            "last": last, "created": int(t.get("created") or 0),
        })
    day = 24 * 3600 * 1000
    stale = [k for k, v in launched.items() if now_ms - int(v.get("at") or 0) > day]
    for k in stale:
        launched.pop(k)
    if changed or stale:
        try:
            cache_path.parent.mkdir(exist_ok=True)
            cache_path.write_text(json.dumps(launched), encoding="utf-8")
        except OSError:
            pass
    rows.sort(key=lambda r: r["created"])
    return rows


def oc_title(title: str) -> str:
    """`agentops-research-2026-10-09-merge-pipeline-practices` -> `research merge pipeline practices`;
    `Research queue systems (@general subagent)` -> `Research queue systems`."""
    t = re.sub(r"\s*\(@[\w-]+ subagent\)$", "", title)
    t = re.sub(r"^agentops-", "", t)
    t = re.sub(r"\d{4}-\d{2}-\d{2}-", "", t)
    return t.replace("-", " ") if " " not in t else t


def attach_opencode(agents_out: list[dict], rows: list[dict], ags=None) -> None:
    """#678: each opencode session becomes a subagent row of the Claude session that launched it,
    its own sub-sessions one level deeper; a run with no launcher record (started before #679, or by a
    session no longer live) is not drawn: the owner removed the «Другое» row (09.10 20:3x).
    #1005: `ags` = tools.agentops.agent_status; given, each row gets its `status` word."""
    by_parent: dict[str | None, list[dict]] = {}
    ids = {r["id"] for r in rows}
    for r in rows:
        by_parent.setdefault(r["parent"] if r["parent"] in ids else None, []).append(r)

    def sub(r: dict, depth: int) -> dict:
        row = {"name": oc_title(r["title"]), "type": "opencode", "issue": None, "model": r["model"],
               "ctx": r["ctx"], "age": r["age"], "busy": r["busy"], "last": r["last"], "depth": depth}
        if ags is not None:
            row["status"] = ags.classify_opencode(r["last"], r["busy"])
        return row

    for root in by_parent.get(None, []):
        host = next((a for a in agents_out if root["claude"] and a.get("sid") == root["claude"]), None)
        if host is None:
            continue
        block = [sub(root, 1)] + [sub(k, 2) for k in sorted(by_parent.get(root["id"], []), key=lambda k: not k["busy"])]
        host["subagents"] = [s for s in host["subagents"] if s.get("busy")] + block + \
            [s for s in host["subagents"] if not s.get("busy")]


KERNEL_NAMES = {"system idle process", "system", "memory compression", "registry", "secure system"}
#: The previous scan of this machine (a remote machine runs this file from stdin, so never next to __file__).
SNAP = Path(tempfile.gettempdir()) / "monitor-machine.json"
SNAP_MAX_AGE = 600  # older = another session of the pane: start over with a double scan


def _scan() -> dict[int, dict]:
    """Every process in ONE NtQuerySystemInformation(SystemProcessInformation) call: pid, ppid,
    name, user+kernel CPU seconds, working set, creation time (unix s). 16 ms CPU for 500
    processes; the psutil walk it replaced cost 13 s CPU per pull (09.10), a core per open pane."""
    if sys.platform != "win32":
        return _scan_psutil()
    import ctypes
    import ctypes.wintypes as wt

    ntdll = ctypes.WinDLL("ntdll")
    size = 1 << 20
    while True:
        buf = ctypes.create_string_buffer(size)
        needed = wt.ULONG(0)
        st = ntdll.NtQuerySystemInformation(5, buf, size, ctypes.byref(needed)) & 0xFFFFFFFF
        if st == 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            size = max(size * 2, needed.value + 65536)
            continue
        if st != 0:
            raise OSError(f"NtQuerySystemInformation status 0x{st:08X}")
        break
    raw = buf.raw
    u32 = lambda o: int.from_bytes(raw[o:o + 4], "little")  # noqa: E731
    u64 = lambda o: int.from_bytes(raw[o:o + 8], "little")  # noqa: E731
    out: dict[int, dict] = {}
    off = 0
    while True:
        nxt = u32(off)
        name_len = int.from_bytes(raw[off + 56:off + 58], "little")
        name_ptr = u64(off + 64)
        pid = u64(off + 80)
        name = ctypes.wstring_at(name_ptr, name_len // 2) if name_ptr and name_len else ""
        out[pid] = {"ppid": u64(off + 88), "name": name, "cpu": (u64(off + 40) + u64(off + 48)) / 1e7,
                    "rss": u64(off + 144), "created": u64(off + 32) / 1e7 - 11644473600}
        if not nxt:
            break
        off += nxt
    return out


def _scan_psutil() -> dict[int, dict]:
    import psutil

    out: dict[int, dict] = {}
    for p in psutil.process_iter(["pid", "ppid", "name", "cpu_times", "memory_info", "create_time"]):
        i = p.info
        ct, mi = i.get("cpu_times"), i.get("memory_info")
        if ct is None or mi is None:
            continue
        out[i["pid"]] = {"ppid": i.get("ppid") or 0, "name": i.get("name") or "", "cpu": ct.user + ct.system,
                         "rss": mi.rss, "created": i.get("create_time") or 0}
    return out


def _cpu_clock() -> tuple[float, float]:
    """(busy, busy + idle) CPU seconds summed over every core since boot (Windows: `system`
    already holds interrupt and dpc time)."""
    import psutil

    ct = psutil.cpu_times()
    return ct.user + ct.system, ct.user + ct.system + ct.idle


def _snap_read() -> dict | None:
    try:
        d = json.loads(SNAP.read_text(encoding="utf-8"))
        return d if 0 <= time.time() - d["t"] < SNAP_MAX_AGE else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _snap(t: float, busy: float, total: float, procs: dict[int, dict]) -> dict:
    return {"t": t, "busy": busy, "total": total, "p": {str(pid): [p["created"], p["cpu"]] for pid, p in procs.items()}}


#: #986: the merge's own share of a host, apart from work / agents / you (the payload's `machine.merge`).
MERGE_KEY = "merge"
#: The PC's two lock files whose holder pid is a merge process: the merge line and the full run it spawns after a landing.
MERGE_LOCKS = (("var", "locks", "merge.lock"), ("var", "agentops", "full_run.lock"))
#: A merge's suite on another host (`scripts/suite_on_host.py`): `<repo>-suite-<sha8>.out.ps1` under wscript -> powershell.
SUITE_RUNNER = re.compile(r"-suite-[0-9a-f]{8}\.out\.ps1", re.IGNORECASE)


def _cmdline(pid: int) -> str:
    import psutil
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except psutil.Error:
        return ""


def _exe(pid: int) -> str:
    """#114: the image path of a process (readable where its command line is not); "" when denied or gone."""
    import psutil
    try:
        return psutil.Process(pid).exe()
    except psutil.Error:
        return ""


def merge_roots(procs: dict[int, dict]) -> set[int]:
    """#986: the pids whose whole subtree is a merge's, read from `procs` (the scan).
    * the holder pid of `var/locks/merge.lock` (the merge process: its suite, line checks and `gh` calls are
      its children) and of `var/agentops/full_run.lock` (the full run a landing spawns detached through WMI,
      `merge.py:_spawn_map_run` -> `detach.launch`: its parent is not the merge, so the merge's tree never
      reaches it). A holder counts only when its process was created no later than the lock's `started` + 2 s
      (a later process holds a reused pid), like `util.holder_is_live`.
    * on any host, a powershell run by wscript whose command line names `...-suite-<sha8>.out.ps1`: the one-shot
      suite task of a merge routed to this host (#685); a host that only runs the suite has no merge lock."""
    roots: set[int] = set()
    for parts in MERGE_LOCKS:
        try:
            holder = json.loads(REPO.joinpath(*parts).read_text(encoding="utf-8"))
            pid = int(holder["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        p = procs.get(pid)
        if p is None:
            continue
        try:
            started = datetime.fromisoformat(str(holder.get("started"))).timestamp()  # local naive, ' ' or 'T'
            if p["created"] > started + 2.0:
                continue
        except (ValueError, TypeError):
            pass  # no readable start: the pid alone, the safe side
        roots.add(pid)
    for pid, p in procs.items():
        if p["name"].lower() in ("powershell.exe", "pwsh.exe") and procs.get(p["ppid"], {}).get("name", "").lower() == "wscript.exe" \
                and SUITE_RUNNER.search(_cmdline(pid)):
            roots.add(pid)
    return roots


#: #97: the Claude desktop app's own processes. Its Chromium children carry `--type=` right after the exe; its main
#: process is `...\app\Claude.exe`. A CLI session (`claude.exe --bg --agent ...`, an interactive one, the daemon, the
#: sessions the app hosts from its `claude-code` folder) matches neither.
APP_CHILD = re.compile(r"claude(?:\.exe)?\"?\s+--type=", re.IGNORECASE)
APP_MAIN = re.compile(r"^\"?(?:(?!claude\.exe)[^\"])*[\\/]app[\\/]claude\.exe", re.IGNORECASE)
#: #114: the desktop app's image path (its processes' when the command line cannot be read: `...\app\claude.exe`).
APP_IMAGE = re.compile(r"[\\/]app[\\/]claude\.exe$", re.IGNORECASE)
DISPATCHER = re.compile(r"agentops\s+dispatch\b")
#: The processes whose command line is read (one `psutil` call each): the ones that can be a session, a dispatcher or a test run.
CMDLINE_NAMES = ("claude", "python", "pytest", "py.exe", "py.test")
#: #114: one command-line word: a double-quoted run (a path with spaces) or a bare run of non-space characters.
CMD_WORD = re.compile(r'"([^"]*)"|(\S+)')


def is_app_cmd(name: str, cmd: str, exe: str = "") -> bool:
    """The desktop app's process: by its command line when that can be read, else by its image path (#114)."""
    if not name.startswith("claude"):
        return False
    if cmd:
        return bool(APP_CHILD.search(cmd) or APP_MAIN.search(cmd))
    return bool(APP_IMAGE.search(exe))


def is_test_cmd(cmd: str) -> bool:
    """A test run by its command line: `pytest ...`, `-m pytest`, `claude plugin test` (read at the executable's
    position, so a session whose prompt mentions pytest is not one). #114: a quoted exe path may hold spaces."""
    tok = [(q or w).strip("'") for q, w in CMD_WORD.findall(cmd)]
    if not tok:
        return False
    exe = re.split(r"[\\/]", tok[0])[-1].lower()
    if exe in ("pytest", "pytest.exe", "py.test", "py.test.exe"):
        return True
    if exe in ("claude", "claude.exe") and tok[1:3] == ["plugin", "test"]:
        return True
    return any(a == "-m" and b in ("pytest", "py.test") for a, b in zip(tok, tok[1:]))


def classify_processes(procs: dict[int, dict], cmdlines: dict[int, str], slot_pids=(), daemon_pid: int | None = None,
                       work_key: str = "work", work_prefix: str | None = None, merge_pids=(),
                       own_pid: int | None = None, exes: dict[int, str] | None = None) -> tuple[dict[int, str], set[int]]:
    """#97: pid -> role (`merge`, `work_key`, `tests`, `agents`, `app`; a pid missing from it is the owner's own `you`),
    and the pids that are nobody's load. Decided by the parent chain, first match wins:
    merge (`merge_pids` and below), work (slot pids and the daemon's children with what is below them, names starting
    `work_prefix`), tests (a test run and below it, under a session or the dispatcher), agents (a live CLI session
    process or the dispatcher, and below it), app (the desktop app's main process and its `--type=` children). A name
    alone makes nothing agents: an orphan `python.exe` is the owner's own. `procs` = {pid: {ppid, name, ...}},
    `cmdlines` = {pid: command line}; a session below a work root is agents with its own subtree.
    #114: `exes` = {pid: image path} for the processes whose command line cannot be read; the collector's own chain
    (up to a live session) and the dispatcher daemon are agents, their load counted, not left unmeasured."""
    children: dict[int, list[int]] = {}
    for pid, p in procs.items():
        children.setdefault(p["ppid"], []).append(pid)
    cmd = lambda pid: cmdlines.get(pid, "")  # noqa: E731
    exe = lambda pid: (exes or {}).get(pid, "")  # noqa: E731
    name = lambda pid: procs[pid]["name"].lower()  # noqa: E731
    is_session = lambda pid: (name(pid).startswith("claude") and bool(cmd(pid)) and not is_app_cmd(name(pid), cmd(pid), exe(pid))  # noqa: E731
                              and not is_test_cmd(cmd(pid)))
    is_root = lambda pid: not is_test_cmd(cmd(pid)) and (is_session(pid) or bool(DISPATCHER.search(cmd(pid))))  # noqa: E731

    role: dict[int, str] = {}

    def claim(root: int, r: str, stop=lambda pid: False) -> None:
        stack = [root]
        while stack:
            pid = stack.pop()
            if pid not in procs or pid in role or (pid != root and stop(pid)):
                continue
            role[pid] = r
            stack.extend(children.get(pid, []))

    for pid in merge_pids:
        claim(pid, "merge")
    for pid in slot_pids:
        claim(pid, work_key, stop=is_root)
    skip = {0, 4}
    if daemon_pid and daemon_pid in procs:
        # #114: the daemon itself is agents (its load counted); only its helpers' trees are split off below
        skip.add(daemon_pid)
        role.setdefault(daemon_pid, "agents")
        for c in children.get(daemon_pid, []):
            if not is_root(c):  # a helper session the daemon opened is agents (below)
                claim(c, work_key, stop=is_root)
    if work_prefix:
        for pid in procs:
            if pid not in role and name(pid).startswith(work_prefix):
                role[pid] = work_key
    # This collector and the shells it runs under (on a remote machine: the ssh chain), up to the first
    # live session: that one counts. #114: when that session is found, the chain is agents (its load counted,
    # not unmeasured); with no session above it, the chain is nobody's agent and counts as the owner's `you`.
    pid = own_pid if own_pid is not None else os.getpid()
    # #264: `procs` is a snapshot, and a reused pid can make its ppid links a cycle (a dead ancestor's pid taken by
    # this process's own child); a pid seen twice ends the walk with no session found.
    chain: list[int] = []
    seen: set[int] = set()
    while pid in procs and pid not in seen and not is_session(pid):
        seen.add(pid)
        chain.append(pid)
        pid = procs[pid]["ppid"]
    if pid in procs and pid not in seen:
        for c in chain:
            skip.add(c)
            role.setdefault(c, "agents")

    # The agent trees: from every unclaimed session / dispatcher over the unclaimed processes below it.
    stack = [pid for pid in procs if pid not in role and pid not in skip and is_root(pid)]
    roots = set(stack)
    tree: set[int] = set()
    tests: list[int] = []
    while stack:
        pid = stack.pop()
        if pid in tree:
            continue
        tree.add(pid)
        for c in children.get(pid, []):
            if c in role or c in tree or c in skip:
                continue
            if c not in roots and is_test_cmd(cmd(c)):
                tests.append(c)
            stack.append(c)
    for t in tests:
        claim(t, "tests", stop=lambda pid: pid in role or pid in skip)
    for pid in tree:
        role.setdefault(pid, "agents")
    for pid in procs:
        if pid not in role and is_app_cmd(name(pid), cmd(pid), exe(pid)):
            role[pid] = "app"
    return role, skip


def load_split(procs: dict[int, dict], role: dict[int, str], skip: set[int], prev: dict, dt: float, cores: int,
               busy_pct: float, used_gb: float, work_key: str = "work") -> dict:
    """#97: each role's CPU (percent of the whole machine) and RAM (GB) as the raw sum over its own processes, never
    rescaled; what the machine uses and no process accounts for is `unmeasured` (CPU: the busy share minus every known
    role incl. the merge's; RAM: used GB minus the same). `prev` = the previous scan ({"t", "p": {pid: [created, cpu]}})."""
    keys = (work_key, "agents", "app", "tests", "you", MERGE_KEY)
    cpu = dict.fromkeys(keys, 0.0)
    ram = dict.fromkeys(keys, 0.0)
    for pid, p in procs.items():
        # #114: a skipped pid with a role (the collector's chain, the daemon) is that role's load, not nobody's
        if (pid in skip and pid not in role) or p["name"].lower() in KERNEL_NAMES:
            continue
        r = role.get(pid, "you")
        was = prev["p"].get(str(pid))
        if was and abs(was[0] - p["created"]) < 1:
            spent = p["cpu"] - was[1]
        elif p["created"] >= prev["t"] - 1:
            spent = p["cpu"]  # born inside the interval: all of its time falls in it
        else:
            spent = 0.0  # a reused pid: nothing known
        cpu[r] += max(0.0, spent)
        ram[r] += p["rss"] / 2**30
    cpu = {k: min(100.0, 100.0 * v / dt / cores) for k, v in cpu.items()}  # percent of the whole machine
    cpu_un = max(0.0, busy_pct - sum(cpu.values()))
    ram_un = max(0.0, used_gb - sum(ram.values()))
    merge_cpu = round(cpu.pop(MERGE_KEY), 1)
    merge_mb = round(ram.pop(MERGE_KEY) * 1024)
    cpu = {k: round(v, 1) for k, v in cpu.items()}
    ram = {k: round(v, 1) for k, v in ram.items()}
    cpu["unmeasured"] = round(min(100.0, cpu_un), 1)
    ram["unmeasured"] = round(ram_un, 1)
    return {"cpu": cpu, "ram": ram, "merge": {"cpu": merge_cpu, "cpu_raw": merge_cpu, "ram_mb": merge_mb}}


def machine(slot_pids: set[int], daemon_pid: int | None = None, work_key: str = "work",
            work_prefix: str | None = None) -> dict:
    """CPU and RAM split work / agents / app / tests / you / unmeasured (`work_key` names the work share in the
    payload). #97: a process's role is its parent chain's (`classify_processes`), each share is the raw sum of its
    own processes and `unmeasured` is what the machine uses beyond them. The merge's processes (`merge_roots` and
    everything below) are none of those: they come back as `merge` = {`cpu`: percent of the whole machine, `ram_mb`},
    both 0 with no merge (#986). Per-process CPU = its CPU time spent since the previous pull over the wall time
    between them (a true average, no sampling sleep); the machine's busy share from psutil.cpu_times() deltas the
    same way. The 0.7-s psutil sample it replaced rescaled tiny readings up to the system total, so an idle
    daemon's 0.1 % drew a work share of 6 % and one blue core on an idle PC (09.10)."""
    import psutil

    cores = psutil.cpu_count() or 1
    prev = _snap_read()
    if prev is None:
        p0 = _scan()
        b0, t0 = _cpu_clock()
        prev = _snap(time.time(), b0, t0, p0)
        time.sleep(0.5)
    procs = _scan()
    busy, total = _cpu_clock()
    now = time.time()
    try:
        SNAP.write_text(json.dumps(_snap(now, busy, total, procs)), encoding="utf-8")
    except OSError:
        pass
    dt = max(0.1, now - prev["t"])
    busy_pct = min(100.0, max(0.0, 100.0 * (busy - prev["busy"]) / max(1e-6, total - prev["total"])))
    vm = psutil.virtual_memory()
    total_gb = vm.total / 2**30
    used_gb = total_gb - vm.available / 2**30

    cmdlines = {pid: _cmdline(pid) for pid, p in procs.items() if p["name"].lower().startswith(CMDLINE_NAMES)}
    # #114: a Claude process whose command line cannot be read is told apart by its image path (the app's `\app\`).
    exes = {pid: _exe(pid) for pid, p in procs.items()
            if p["name"].lower().startswith("claude") and not cmdlines.get(pid)}
    role, skip = classify_processes(procs, cmdlines, slot_pids, daemon_pid, work_key, work_prefix, merge_roots(procs),
                                    exes=exes)
    out = load_split(procs, role, skip, prev, dt, cores, busy_pct, used_gb, work_key)
    return {"cores": cores, "cpu": out["cpu"], "cpu_total": round(busy_pct, 1), "interval_s": round(dt, 1),
            "ram": out["ram"], "ram_total": round(total_gb, 1), "ram_free": round(vm.available / 2**30, 1),
            MERGE_KEY: out["merge"]}


MERGE_CMD = [sys.executable, "-m", "tools.agentops", "merge-status", "--json"]


def merge_shape(data: dict) -> dict:
    """#680: the repo's `merge-status --json` (raw seconds, a machine file) plus the human durations
    the card prints: `eta_h`, `elapsed_h` on running; `waited_h`, `eta_start_h` per waiter;
    `took_h` per landed. #116 B1: a did-not-land row (rc != 0) also gets `ended` (its end time, from `landed`);
    its `reason` and red-suite `failed` count are the status row's own. Nothing else is recomputed here:
    the command owns the numbers."""
    run = data.get("running")
    if run:
        run["eta_h"] = hd(run.get("eta_s")) if run.get("eta_s") is not None else ""
        try:
            started = datetime.fromisoformat(run["started"])
            at = datetime.fromisoformat(data["at"]) if data.get("at") else datetime.now(started.tzinfo)
            run["elapsed_h"] = hd(max(0.0, (at - started).total_seconds()))
        except (KeyError, TypeError, ValueError):
            run["elapsed_h"] = ""
    for w in data.get("waiting") or []:
        w["waited_h"] = hd(w.get("waited_s")) if w.get("waited_s") is not None else ""
        w["eta_start_h"] = hd(w.get("eta_start_s")) if w.get("eta_start_s") is not None else ""
    for x in data.get("landed") or []:
        x["took_h"] = hd(x.get("queued_to_land_s")) if x.get("queued_to_land_s") is not None else ""
        if x.get("rc"):  # #116 B1: a did-not-land merge gets its end time (reason and failed come with the row)
            x["ended"] = x.get("landed")
    # #699: `takes_h` is one median suite, the figure the command counts a batch with.
    # #120: the queue end is the command's own `queue_end_s` (seconds from now); no formula over the waiters here.
    # An older command without the key: the running batch's eta only.
    waiting = data.get("waiting") or []
    if run or waiting:
        try:
            from tools.agentops.merge_status import FALLBACK_SUITE_S, _log_facts
            durations, _ = _log_facts(REPO / "var" / "agentops" / "merge.log")
            med = statistics.median(durations) if durations else FALLBACK_SUITE_S
        except Exception:
            med = None
        for w in waiting:
            w["takes_h"] = hd(med) if med else ""
        end = data.get("queue_end_s")
        if not isinstance(end, int):
            end = (run or {}).get("eta_s") or 0
        data["queue_end_h"] = hd(end)
        data["queue_until"] = (datetime.now() + timedelta(seconds=end)).strftime("%H:%M")
    data.setdefault("waiting", [])
    data.setdefault("landed", [])
    return data


def merge_status() -> dict:
    """#680: the merge line's state from `python -m tools.agentops merge-status --json` (read-only,
    under 2 s). Not on main yet -> {"missing": True}: the card says it waits for the repo half."""
    res = subprocess.run(MERGE_CMD, cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
                         timeout=10, creationflags=NO_WINDOW)
    if res.returncode != 0:
        if "invalid choice" in res.stderr:
            return {"missing": True}
        raise RuntimeError((res.stderr.strip() or f"exit {res.returncode}")[-200:])
    return merge_shape(json.loads(res.stdout))


def _orch_version() -> int | None:
    """#157: the orchestration version the monitor shows. In the home repo it is the commit count of HEAD in the project
    root; in a target it is `core_version_count`, which the last core update wrote. Any failure, or neither, is None."""
    try:
        cfg = settings()
        home = cfg.get("home")
        if home and cfg.get("repo") == home:
            res = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(REPO), capture_output=True,
                                 text=True, encoding="utf-8", timeout=10, creationflags=NO_WINDOW)
            return int(res.stdout.strip()) if res.returncode == 0 else None
        count = cfg.get("core_version_count")
        return count if isinstance(count, int) and not isinstance(count, bool) else None
    except Exception:  # noqa: BLE001
        return None


def fast() -> dict:
    out: dict = {"at": datetime.now().strftime("%H:%M:%S")}
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(2)
    mrg = pool.submit(merge_status)
    ag = pool.submit(agents)
    add = addon()
    ctx = SimpleNamespace(root=REPO, sections={})
    if add is not None:
        out.update(add.fast(ctx))
        ctx.sections = out
    try:
        pids, daemon = add.machine_pids(ctx) if add is not None else (set(), None)
        out["machine"] = machine(pids, daemon, work_key=getattr(add, "WORK_KEY", "work"),
                                 work_prefix=getattr(add, "WORK_PREFIX", None))
    except Exception as exc:  # noqa: BLE001
        out["machine_error"] = repr(exc)[:200]
    try:
        out["agents"] = ag.result(timeout=60)
    except Exception as exc:  # noqa: BLE001
        out["agents_error"] = repr(exc)[:200]
    try:
        out["merge"] = mrg.result(timeout=15)
    except Exception as exc:  # noqa: BLE001
        out["merge_error"] = repr(exc)[:200]
    try:  # #6: one row per ticket, joined from the tickets facts with this pull's agents and merge line
        from tools.agentops import monitor_pipeline as mp
        facts = _pipeline_facts_read()
        _pipeline_row_times(facts, out.get("agents") or [], out.get("merge"), mp)
        out["pipeline"] = mp.pipeline_rows(facts, out.get("agents") or [], out.get("merge"),
                                           units=_outcome_units())
    except Exception as exc:  # noqa: BLE001
        out["pipeline_error"] = repr(exc)[:200]
    out["version"] = _orch_version()  # #157: never raises; None when unknown
    return out


# ---------------------------------------------------------------- tickets

def _node_fields(with_comments: bool) -> str:
    """The fields of one issue node. A closed issue is never parked on a run or a merge, so its comments and
    assignees are not asked for (#18)."""
    extra = """
        body
        comments(last: 30) { nodes { body createdAt } }
        assignees(first: 5) { totalCount }""" if with_comments else ""
    return f"""number title state stateReason updatedAt createdAt author {{ login }}
        labels(first: 50) {{ nodes {{ name }} }}
        parent {{ number }}
        blockedBy(first: 50) {{ nodes {{ number state }} }}{extra}"""


def _issues_query(states: str, with_comments: bool) -> str:
    """The tab's issue query for `states` ([OPEN] or [CLOSED]); it also asks `rateLimit { cost remaining }` (#181)."""
    return f"""
query($owner:String!, $name:String!, $endCursor:String) {{
  rateLimit {{ cost remaining }}
  repository(owner:$owner, name:$name) {{
    issues(first:100, after:$endCursor, states: {states}, orderBy:{{field:CREATED_AT, direction:ASC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        {_node_fields(with_comments)}
      }}
    }}
  }}
}}"""


#: #181: the delta read: the DELTA_PAGE issues updated last, open and closed together, the full open node shape.
#: 1 point a call (1 + 20 x 4 < 100); its cost is logged by `_read_delta`.
DELTA_PAGE = 20
QUERY_DELTA = f"""
query($owner:String!, $name:String!) {{
  rateLimit {{ cost remaining }}
  repository(owner:$owner, name:$name) {{
    issues(first:{DELTA_PAGE}, orderBy:{{field:UPDATED_AT, direction:DESC}}) {{
      nodes {{
        {_node_fields(True)}
      }}
    }}
  }}
}}"""

QUERY_OPEN = _issues_query("[OPEN]", True)
QUERY_CLOSED = _issues_query("[CLOSED]", False)
#: #18: the closed issues (kids of an open ticket, shown struck through) change rarely and paged through ~12 pages
#: every compute (2 points a page of 100, measured 2026-10: ~24 points a read): they are read at most this often, from
#: a disk cache; an open page (25 open issues) costs 4 points. A ticket closed inside the window shows from the next
#: read. #181: the tickets pull is a delta read on a snapshot (1 point); a due full read (every FULL_RECONCILE_S, an
#: overflow, a failed delta) forces this closed refresh too: 240 delta pulls + 6 full reads x (4 open + 4 closed) per
#: hour = ~290 points an hour; only a first start with no snapshot may use the closed cache.
CLOSED_TICKETS_TTL_S = 30 * 60
#: a failed closed refresh is not tried again by the next compute: `closed-tickets.attempt` is touched on a failure and
#: no new closed read starts before this has passed since it (the stale cache is served meanwhile).
CLOSED_RETRY_S = 5 * 60


def _parked_on_run(n: dict, labels: list[str]) -> bool:
    """Open, no ready-for-agent / needs-info, unassigned, and one of the last 30 comments opens with the marker line."""
    if n["state"] != "OPEN" or {"ready-for-agent", "needs-info"} & set(labels):
        return False
    if (n.get("assignees") or {}).get("totalCount", 0):
        return False
    for c in (n.get("comments") or {}).get("nodes") or []:
        first = ((c.get("body") or "").splitlines()[:1] or [""])[0]
        if first.startswith("<!-- parked-on-run:"):
            return True
    return False


MERGE_RESULT = re.compile(r"results[\\/](\d{8}T\d{6}-merge-([\w.-]+)\.json)")
MERGE_PARKED = ("<!-- parked-on-merge -->", "<!-- parked-on-merge:")


def _first_line(c: dict) -> str:
    return ((c.get("body") or "").splitlines()[:1] or [""])[0]


def _merge_lost(comments: list[dict], now: datetime, exists, live_branches: set[str]) -> bool:
    """#1089: the newest merge marker among a ticket's comments is a parked-on-merge comment that names a result
    file (`results/<stamp>-merge-<branch>.json`) which does not exist, is older than 30 min, and whose branch is in
    no live merge queue. `exists` takes the file name; `now` is timezone-aware UTC."""
    marks = [c for c in sorted(comments, key=lambda c: c.get("createdAt") or "")
             if _first_line(c).startswith(MERGE_PARKED + ("<!-- merge result -->",))]
    if not marks or not _first_line(marks[-1]).startswith(MERGE_PARKED):
        return False
    newest = marks[-1]
    m = MERGE_RESULT.search(newest.get("body") or "")
    if not m or not newest.get("createdAt"):
        return False
    try:
        parked_at = datetime.fromisoformat(newest["createdAt"].replace("Z", "+00:00"))
    except ValueError:
        return False
    if now - parked_at <= timedelta(minutes=30):
        return False
    file_name, branch = m.group(1), m.group(2)
    return not exists(file_name) and branch not in live_branches


def _live_merge_branches() -> set[str] | None:
    """#1089: the branches the merge line holds (running and waiting); None when merge-status cannot be read,
    so no row is flagged merge_lost."""
    try:
        data = merge_status()
        if data.get("missing"):
            return None
        live = set((data.get("running") or {}).get("branches") or [])
        live |= {w["branch"] for w in data.get("waiting") or [] if w.get("branch")}
        return live
    except Exception:  # noqa: BLE001
        return None


#: #181: one line per GraphQL read of the tickets payload: {"at", "kind": delta|open|closed, "cost", "remaining"}.
GH_COST_LOG = REPO / "var" / "agentops" / "monitor_gh_cost.jsonl"


def _log_gh_cost(kind: str, rate) -> None:
    """Append one cost line for a read; a reply without `rateLimit` logs nothing and a failed write is ignored."""
    if not isinstance(rate, dict) or "cost" not in rate:
        return
    line = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "cost": rate["cost"],
            "remaining": rate.get("remaining")}
    try:
        GH_COST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with GH_COST_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass


def _graphql(query: str, paginate: bool) -> str:
    cmd = ["gh", "api", "graphql"] + (["--paginate"] if paginate else []) + [
        "-f", f"owner={GH_REPO[0]}", "-f", f"name={GH_REPO[1]}", "-f", f"query={query}"]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, creationflags=NO_WINDOW)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-300:])
    return res.stdout


def _read_delta() -> list[dict]:
    """The DELTA_PAGE issues updated last (#181), one 1-point call; raises on a failed or malformed read."""
    data = json.loads(_graphql(QUERY_DELTA, False))["data"]
    nodes = data["repository"]["issues"]["nodes"]
    _log_gh_cost("delta", data.get("rateLimit"))
    return nodes


def _read_issues(query: str, kind: str | None = None) -> list[dict]:
    """The issue nodes of a (paginated) read; with `kind` ("open" / "closed") each page's cost is logged (#181)."""
    nodes: list[dict] = []
    dec = json.JSONDecoder()
    text, i = _graphql(query, True), 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        nodes.extend(obj["data"]["repository"]["issues"]["nodes"])
        if kind:
            _log_gh_cost(kind, obj["data"].get("rateLimit"))
    return nodes


def _write_cache(path: Path, text: str) -> None:
    """Write a cache file through a temp file; a failed write (a locked file on Windows) is ignored, the cache is
    only a saving."""
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)  # #182: the folder of the file (the install folder's bin/.cache, or CACHE_DIR)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _read_closed_cache(path: Path) -> list[dict] | None:
    """The cached closed issues, or None when the file is absent, unreadable or not a list of issue nodes."""
    try:
        nodes = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(nodes, list) or not all(isinstance(n, dict) and "number" in n for n in nodes):
        return None
    return nodes


def _closed_issues(force: bool = False) -> tuple[list[dict], bool]:
    """(the closed issues, read fresh): from `.cache/closed-tickets.json` while it is younger than
    CLOSED_TICKETS_TTL_S unless `force` (#18); a failed read serves the older file (read fresh False) and touches
    `closed-tickets.attempt`, and no new read starts before CLOSED_RETRY_S has passed since it; it raises only when
    there is no readable cache. An unreadable or non-list cache counts as absent."""
    path = CACHE_DIR / "closed-tickets.json"
    attempt = CACHE_DIR / "closed-tickets.attempt"
    cached = _read_closed_cache(path)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        cached = None
    if cached is not None and age < CLOSED_TICKETS_TTL_S and not force:
        return cached, True
    if cached is not None:
        try:
            if time.time() - attempt.stat().st_mtime < CLOSED_RETRY_S:
                return cached, False
        except OSError:
            pass
    try:
        nodes = _read_issues(QUERY_CLOSED, "closed")
    except RuntimeError:
        if cached is None:
            raise
        _write_cache(attempt, "")
        return cached, False
    _write_cache(path, json.dumps(nodes, ensure_ascii=False))
    try:
        attempt.unlink(missing_ok=True)
    except OSError:
        pass
    return nodes, True


def _open_numbers_file() -> Path:
    return CACHE_DIR / "open-tickets.json"


#: #6: the pipeline's ticket facts (written by the tickets pull, read by the fast pull) and the label-time cache.
#: #182: resolved at each use, beside `collector-fast.json` (the folder of `fast_path()`): the installed pane's folder
#: when the collector writes there, else CACHE_DIR. Fixed at import they sat in the repo's cache, which the tests write.
def _pipeline_tickets_path() -> Path:
    return fast_path().parent / "pipeline-tickets.json"


def _pipeline_labels_path() -> Path:
    return fast_path().parent / "pipeline-labels.json"


def _pipeline_tickets_write(issues: dict, cut_totals: dict | None = None) -> str | None:
    """#6: the facts the Progress pipeline joins, with each ready ticket's `ready-for-agent` time (one gh timeline call
    per ticket per label change, cached on disk). Returns the error text, or None."""
    try:
        from tools.agentops import monitor_pipeline as mp
        from tools.agentops.gh_call import run_gh
        try:
            cache = json.loads(_pipeline_labels_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        cache = cache if isinstance(cache, dict) else {}
        facts = mp.pipeline_facts(issues, repo=f"{GH_REPO[0]}/{GH_REPO[1]}", gh=run_gh, cache=cache,
                                  cut_totals=cut_totals)
        _write_cache(_pipeline_labels_path(), json.dumps(cache))
        _write_cache(_pipeline_tickets_path(), json.dumps(facts, ensure_ascii=False))
        return None
    except Exception as exc:  # noqa: BLE001 - the pipeline is a part of the tickets payload, its failure is named there
        return repr(exc)[:200]


def _pipeline_facts_read() -> dict:
    try:
        facts = json.loads(_pipeline_tickets_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return facts if isinstance(facts, dict) else {}


def _pipeline_row_times(facts: dict, sessions: list, merge, mp) -> None:
    """#151 "с начала": the first `ready-for-agent` time of each Pipeline row's ticket that the label cache has no
    entry for (one timeline call each, then cached on disk), set on `facts` as `label_at`. The fast pull's rows
    need it: a ticket ORCH holds has lost its label. A failure here is the caller's pipeline_error."""
    from tools.agentops.gh_call import run_gh
    try:
        cache = json.loads(_pipeline_labels_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    cache = cache if isinstance(cache, dict) else {}
    if mp.fill_row_times(facts, mp.row_numbers(facts, sessions, merge), repo=f"{GH_REPO[0]}/{GH_REPO[1]}",
                         gh=run_gh, cache=cache):
        _write_cache(_pipeline_labels_path(), json.dumps(cache))


def _cut_totals(nodes: list[dict]) -> dict[int, int]:
    """#151 rule 2: number -> the cut-table total n of the ticket's newest ORCH cut-table comment (`ma.cut_table_total`
    over its comments, oldest first). A failure is no totals, never a failed tickets pull."""
    try:
        from tools.agentops import monitor_agents as ma
        totals: dict[int, int] = {}
        for node in nodes:
            comments = sorted((node.get("comments") or {}).get("nodes") or [], key=lambda c: c.get("createdAt") or "")
            total = ma.cut_table_total([c.get("body") or "" for c in comments])
            if total is not None:
                totals[node["number"]] = total
        return totals
    except Exception:  # noqa: BLE001
        return {}


def _outcome_units() -> dict[str, dict]:
    """#151: the outcome log's `k/n` progress by issue number as str. A failed read is no units, never a failed pull."""
    try:
        from tools.agentops import monitor_agents as ma
        return {str(n): u for n, u in ma.units_by_issue(ma._load_outcome_rows(ma.DEFAULT_OUTCOMES_LOG)).items()}
    except Exception:  # noqa: BLE001
        return {}


def _group_facts(n: dict) -> dict:
    """#155 unit 2: one issue node (QUERY_OPEN / QUERY_CLOSED) in the shape `ticket_groups.ticket_group` takes (a
    `gh issue list --json` record). Labels, parent, blockedBy and comments pass as the node has them; the node's
    `assignees.totalCount` becomes a list of that length, since the group asks only whether there is one."""
    assignees = int((n.get("assignees") or {}).get("totalCount", 0))
    return {"number": n["number"], "state": n["state"], "labels": n["labels"]["nodes"], "parent": n.get("parent"),
            "blockedBy": n["blockedBy"], "assignees": [None] * assignees,
            "comments": (n.get("comments") or {}).get("nodes") or []}


#: #181: a full read of every issue is made at least this often; between, a pull reads the delta page only.
FULL_RECONCILE_S = 10 * 60
SNAPSHOT_NAME = "tickets-snapshot.json"


def _read_snapshot() -> dict | None:
    """The snapshot {"full_at", "watermark", "nodes"}, or None when absent, unreadable, malformed or empty."""
    try:
        snap = json.loads((CACHE_DIR / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or not isinstance(snap.get("full_at"), (int, float)) \
            or not isinstance(snap.get("watermark"), str) or not isinstance(snap.get("nodes"), list) \
            or not snap["nodes"] or not all(isinstance(n, dict) and "number" in n for n in snap["nodes"]):
        return None
    return snap


def _write_snapshot(nodes: list[dict], full_at: float) -> None:
    """Write the snapshot, unless the file on disk holds a newer watermark from a full read that is not older than
    this one (a slower concurrent writer must not replace newer data); a later full read always writes (#181)."""
    watermark = max((n.get("updatedAt") or "" for n in nodes), default="")
    disk = _read_snapshot()
    if disk is not None and disk["watermark"] > watermark and disk["full_at"] >= full_at:
        return
    _write_cache(CACHE_DIR / SNAPSHOT_NAME,
                 json.dumps({"full_at": full_at, "watermark": watermark, "nodes": nodes}, ensure_ascii=False))


def _closed_shape(node: dict) -> dict:
    """A closed node as `QUERY_CLOSED` returns it: no body, comments or assignees (the delta page carries them)."""
    if node.get("state") == "OPEN":
        return node
    return {k: v for k, v in node.items() if k not in ("body", "comments", "assignees")}


def _snapshot_with_delta() -> list[dict] | None:
    """#181: every issue node from the snapshot merged with the delta page (one 1-point read), written back with its
    `full_at` kept; None when a full read is due: no usable snapshot, one older than FULL_RECONCILE_S, a delta read
    that failed, or a full delta page whose oldest node is not older than the watermark (more changed than it holds)."""
    snap = _read_snapshot()
    if snap is None or not 0 <= time.time() - snap["full_at"] < FULL_RECONCILE_S:
        return None
    try:
        page = _read_delta()
        oldest = min((n["updatedAt"] for n in page), default="")
        by_number = {n["number"]: n for n in snap["nodes"]}
        by_number.update({n["number"]: _closed_shape(n) for n in page})
        # a blocker that closed is on the page, the ticket it blocked is not: its blocker states come from the map
        for node in by_number.values():
            for b in (node.get("blockedBy") or {}).get("nodes") or []:
                if b["number"] in by_number:
                    b["state"] = by_number[b["number"]]["state"]
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None
    if len(page) >= DELTA_PAGE and oldest >= snap["watermark"]:
        return None
    nodes = sorted(by_number.values(), key=lambda n: n["number"])
    _write_snapshot(nodes, snap["full_at"])
    return nodes


def tickets() -> dict:
    had_snapshot = _read_snapshot() is not None
    nodes = _snapshot_with_delta()
    if nodes is None:
        # a full read made because a snapshot exists but is due refreshes the closed list: an issue opened and closed
        # since the last full read is in the snapshot and must stay; only a first start may use the closed cache
        nodes = _full_nodes(force_closed=had_snapshot)
        _write_snapshot(nodes, time.time())
    return _tickets_payload(nodes)


def _full_nodes(force_closed: bool = False) -> list[dict]:
    nodes = _read_issues(QUERY_OPEN, "open")
    # #18: a number the previous compute had open and this one lacks was closed (or moved) inside the closed cache's
    # window: the closed read is forced now, so the ticket shows as closed at once instead of vanishing for 30 min.
    now_open = {n["number"] for n in nodes}
    try:
        was_open = set(json.loads(_open_numbers_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        was_open = set()
    closed, fresh = _closed_issues(force=force_closed or bool(was_open - now_open))
    nodes += [c for c in closed if c["number"] not in now_open]  # a ticket reopened inside the window is open now
    if fresh or not was_open:  # a forced read that failed keeps the old set, so the next compute tries again
        _write_cache(_open_numbers_file(), json.dumps(sorted(now_open)))
    nodes.sort(key=lambda n: n["number"])  # the old read was one list by creation order = number order
    return nodes


def _tickets_payload(nodes: list[dict]) -> dict:
    live = _live_merge_branches()
    now = datetime.now(timezone.utc)
    results_dir = REPO / "var" / "agentops" / "results"
    # #155 unit 2: each issue's «По тикетам» group, decided by the one function `sweep list` uses (ticket_groups)
    from tools.agentops import ticket_groups as tg
    from tools.agentops import monitor_pipeline as mp
    facts = {n["number"]: _group_facts(n) for n in nodes}
    group_of = {number: tg.ticket_group(f, facts) for number, f in facts.items()}
    issues = {}
    for n in nodes:
        labels = [l["name"] for l in n["labels"]["nodes"]]
        merge_lost = live is not None and n["state"] == "OPEN" and _merge_lost(
            (n.get("comments") or {}).get("nodes") or [], now, lambda f: (results_dir / f).exists(), live)
        # #171: the wait and return facts of a Pipeline row (the body's Parked: line, the newest parked-on-run line and
        # send-back time), read here once; a closed ticket has none
        comment_nodes = (n.get("comments") or {}).get("nodes") or []
        wait_facts = {"parked_line": mp.parked_line(n.get("body")), "parked_run": mp.parked_run_line(comment_nodes),
                      "send_back_at": mp.send_back_at(comment_nodes)}
        issues[n["number"]] = {
            **{k: v for k, v in wait_facts.items() if v},
            "n": n["number"], "title": n["title"], "open": n["state"] == "OPEN",
            "reason": n["stateReason"], "labels": labels,
            "parent": (n.get("parent") or {}).get("number"),
            "blockers": [b["number"] for b in n["blockedBy"]["nodes"] if b["state"] == "OPEN"],
            "updated": n["updatedAt"], "created": n["createdAt"],
            "author": (n.get("author") or {}).get("login"), "kids": [],
            "parked_on_run": _parked_on_run(n, labels),
            "parked": "parked" in labels,
            "merge_lost": merge_lost,
            "group": group_of[n["number"]],
        }
    for i_ in issues.values():
        p = i_["parent"]
        if p in issues:
            issues[p]["kids"].append(i_["n"])
    for i_ in issues.values():
        i_["kids"].sort(key=lambda k: (not issues[k]["open"], k))
        if not i_["open"]:
            i_["state"] = "closed"
        elif set(i_["labels"]) & {"needs-info", "ready-for-human"}:
            i_["state"] = "owner"
        elif i_["blockers"]:
            i_["state"] = "blocked"
        elif "ready-for-agent" in i_["labels"]:
            i_["state"] = "ready"
        elif "needs-triage" in i_["labels"]:
            i_["state"] = "triage"
        else:
            i_["state"] = "open"

    def subtree_has_open(n: int) -> bool:
        x = issues[n]
        return x["open"] or any(subtree_has_open(k) for k in x["kids"])

    # Roots: open tickets whose parent is closed or absent. A goal = a root with sub-tickets.
    roots = [x["n"] for x in issues.values()
             if x["open"] and (x["parent"] is None or not issues.get(x["parent"], {"open": False})["open"])]
    goals = sorted((n for n in roots if issues[n]["kids"]), key=lambda n: -n)
    loose = sorted((n for n in roots if not issues[n]["kids"]), key=lambda n: -n)

    keep: set[int] = set()

    def mark(n: int) -> None:
        keep.add(n)
        for k in issues[n]["kids"]:
            mark(k)

    for n in roots:
        mark(n)
    for n in list(keep):
        issues[n]["blockers"] = [b for b in issues[n]["blockers"] if b in issues]
    pipe_error = _pipeline_tickets_write(issues, _cut_totals(nodes))
    # #155 unit 2: the open tickets per monitor group, in the monitor's order (a closed issue's group is None: no count)
    groups = dict(tg.group_counts(i_["group"] for i_ in issues.values()))
    return {
        "at": datetime.now().strftime("%H:%M:%S"),
        "goals": goals, "loose": loose, "groups": groups,
        "issues": {str(n): {k: v for k, v in issues[n].items() if k not in ("updated",)} for n in keep},
        **({"pipeline_error": pipe_error} if pipe_error else {}),
    }


# ---------------------------------------------------------------- slow

def _memo(fn):
    """One call per process: panel_cost runs three times here (day, week, month) and would rescan
    every transcript and ask gh each time; the same result serves all three windows."""
    box: dict = {}

    def wrapped(*args, **kwargs):
        key = (args, tuple(sorted(kwargs.items())))
        if key not in box:
            box[key] = fn(*args, **kwargs)
        return box[key]
    return wrapped


def tier_rates(price: dict, prompt: int) -> dict:
    """The rate row for a call that has no Turn (a mod call): the entry itself, or for a
    model with `tiers` (Haiku 5.5) the tier its prompt (input + cache read + cache write) falls in, as
    token_meter.turn_price does for a turn."""
    tiers = price.get("tiers")
    if not tiers:
        return price
    return tiers["above"] if prompt > tiers["prompt_bound"] else tiers["up_to"]


def limit_usd_per_pct() -> float | None:
    """#1094: 1 % of the project's weekly limit at API prices, the setting `limit_usd_per_pct` of
    `config/orchestration.json` (the owner's plan, measured 09.10). Read at each call, so a changed config is seen by
    the next mode. No default: absent, or not a finite positive number, is None (no plan known; the money column is
    hidden)."""
    v = settings().get("limit_usd_per_pct")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        return None
    return float(v)


def limit_pct(usd: float, days: int) -> float | None:
    """% of the limit of a period of `days` (#1013 row 5): the weekly limit (100 % = limit_usd_per_pct() x 100 dollars)
    for a period up to a week, prorated (x days / 7) beyond it: a month is against 30/7 of the weekly limit.
    None where no plan is known (#1094)."""
    per = limit_usd_per_pct()
    if per is None:
        return None
    return usd / (per * max(days, 7) / 7)


MONITOR_USAGE = Path(__file__).resolve().parent.parent / "activity-usage.jsonl"  # the mod folder's own meter
TOKEN_KINDS = ("input", "cache_write", "cache_read", "output")
#: The orchestration core's subagent roles (CLAUDE.md "Roles"); any other agent type of the harness (general-purpose,
#: web-fetch, ...) is not a role: one `other_sub` row that names its types.
SUB_ROLES = ("worker", "worker-strong", "researcher-find", "researcher-how", "reviewer-standards", "reviewer-spec")


def period_window(days: int, now: datetime) -> tuple[datetime, datetime]:
    """Local calendar days: midnight of the day `days-1` days before now's date, up to now."""
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


def mod_calls(path: Path, key: str) -> list[dict]:
    """A mod's usage jsonl as call dicts; a `via: fork` row ran on the chat's own model: counted, unpriced."""
    if not path.exists():
        return []
    calls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            calls.append({"key": key, "ts": ts, "model": row.get("model", ""),
                          "input": int(row.get("input_tokens", 0) or 0),
                          "cache_write": int(row.get("cache_creation_input_tokens", 0) or 0),
                          "cache_read": int(row.get("cache_read_input_tokens", 0) or 0),
                          "output": int(row.get("output_tokens", 0) or 0),
                          "priced": row.get("via") != "fork"})
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return calls


def titled_sessions(paths) -> set[str]:
    """Session ids (file stems) of the main transcripts that hold a `custom-title` line. A session without one
    carries its first user message as `turn.title` (token_meter's fallback): not a chat's name."""
    out = set()
    for p in paths:
        try:
            with open(p, "rb") as f:
                for line in f:
                    if line.startswith(b'{"type":"custom-title"'):
                        out.add(Path(p).stem)
                        break
        except OSError:
            continue
    return out


def _issue_row(info: dict[int, dict] | None, n: int) -> dict:
    """What a row of `by_issue` needs to draw by itself when the Tickets payload has no entry for it (it only
    holds tickets under open roots): `title`, `open`, `reason`, `state` = the fields the pane's `iconState` reads.
    A closed ticket is `closed`; an open one the plain `open` (labels, blockers and kids stay with Tickets)."""
    i = (info or {}).get(n)
    if not i:
        return {}
    return {"title": i["title"], "open": bool(i["open"]), "reason": i.get("reason"),
            "state": "open" if i["open"] else "closed"}


def issue_info() -> dict[int, dict]:
    """Title and state of every closed ticket: one `gh issue list` (706 tickets, ~5 s), asked once per process.
    A failed call gives {}: the rows stay bare, as before."""
    try:
        res = subprocess.run(
            ["gh", "issue", "list", "--repo", "/".join(GH_REPO), "--state", "closed", "--json",
             "number,title,stateReason", "--limit", "3000"],
            capture_output=True, text=True, encoding="utf-8", timeout=60, creationflags=NO_WINDOW)
        rows = json.loads(res.stdout) if res.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    return {int(r["number"]): {"title": r["title"], "open": False, "reason": r.get("stateReason")} for r in rows}


#: one closed-list read per process: `slow()` asks for it on every call, the list is the same (#1113)
_ISSUE_INFO_ONCE = _memo(issue_info)


def cost_cut(turns, prices: dict, tm, *, since: datetime, until: datetime, mods=(), top_issues: int = 20,
             titled: set[str] | None = None, closed: tuple[dict, set] | None = None,
             issue_info: dict[int, dict] | None = None) -> dict:
    """One population of spend (every turn and mod call in [since, until]) cut ten ways; each cut sums
    to `total` (`by_spawner` + `by_mod` together). Pure: no file, no clock, no gh. `titled` = the sessions that
    have a custom title (None = trust every `turn.title`): a turn of any other session goes to the one `untitled`
    row. `by_role`: a main session's own turns under its role (brain / orch / autopilot / the add-on kinds, any other
    chat = `other_chat`), a subagent's under its own (SUB_ROLES, else `other_sub`), a mod call under `mod`.
    `by_spawner`: the same main roles, each = what it spent itself and what its subagents spent, by role.
    `by_role_model`: role x model (a mod is its own role there): `n` = the sessions / agents / calls with a live
    turn on that model (a session that changed model counts under each); a main session is LED by the model of
    most of its own live turns, and what it spawned (`spawned`) and the tickets it closed (`closed`, of the
    `ended` ones; `closed` = `_closed_by_session()`'s pair, ORCH only) stand in its leading model's row."""
    title_of: dict[str, str] = {}
    for t in turns:
        if t.agent_id is None and t.title and t.session_id not in title_of:
            title_of[t.session_id] = t.title

    def blank() -> dict:
        return {"turns": 0, "unpriced_turns": 0, "tok": {k: 0 for k in TOKEN_KINDS}, "cost": 0.0}

    total = blank()
    by_kind_cost = {k: 0.0 for k in TOKEN_KINDS}
    who: dict[str, dict] = {}
    role: dict[str, dict] = {}
    spawner: dict[str, dict] = {}
    mod: dict[tuple, dict] = {}
    brain_titles = tuple(getattr(tm, "BRAIN_TITLE_ALIASES", ()))
    rm: dict[tuple, dict] = {}
    # the model that leads a main session: most of its own live turns inside the period, else of all its turns
    lead_in: dict[str, dict] = {}
    lead_all: dict[str, dict] = {}
    for t in turns:
        if t.agent_id is not None or t.timestamp is None:
            continue
        if t.input_tokens + t.cache_creation_input_tokens + t.cache_read_input_tokens + t.output_tokens <= 0:
            continue
        for box in ((lead_in, lead_all) if since <= t.timestamp <= until else (lead_all,)):
            c = box.setdefault(t.session_id, {})
            c[t.model or "(unknown)"] = c.get(t.model or "(unknown)", 0) + 1

    def lead_of(sid) -> str:
        c = lead_in.get(sid) or lead_all.get(sid)
        return max(c, key=lambda m: (c[m], m)) if c else "(unknown)"

    def rm_slot(role_key, model_id, group) -> dict:
        return slot(rm, (role_key, model_id), group=group, units=set(), led=set(), spawned=blank(), sp_agents=set(),
                    first=None, last=None)
    model: dict[str, dict] = {}
    day: dict[str, dict] = {}
    issues: dict[int, dict] = {}
    no_issue = {"turns": 0, "tokens": 0, "cost_usd": 0.0}
    tz = since.tzinfo

    def add(acc: dict, toks: dict, cost: float, unpriced: bool) -> None:
        acc["turns"] += 1
        acc["unpriced_turns"] = acc.get("unpriced_turns", 0) + (1 if unpriced else 0)
        acc["cost"] += cost
        for k in TOKEN_KINDS:
            acc["tok"][k] += toks[k]

    def slot(d: dict, key, **extra) -> dict:
        if key not in d:
            d[key] = {**blank(), **extra}
        return d[key]

    def kind_costs(toks: dict, rates: dict, one_hour: int) -> dict:
        out = {}
        for k in TOKEN_KINDS:
            n = toks[k] - (one_hour if k == "cache_write" else 0)
            out[k] = n * rates[k] / 1_000_000
        if one_hour:
            r1 = rates.get("cache_write_1h")
            out["cache_write"] += one_hour * (r1 if r1 is not None else rates["cache_write"]) / 1_000_000
        return out

    def ingest(when, toks, model_id, costs, who_key, who_kind, sub, agent_type, agent, sid, weights, is_mod,
               main_role="other_chat"):
        unpriced = costs is None
        cost = 0.0 if unpriced else sum(costs.values())
        add(total, toks, cost, unpriced)
        if not unpriced:
            for k in TOKEN_KINDS:
                by_kind_cost[k] += costs[k]
        w = slot(who, who_key, kind=who_kind, sessions=set(), sub_usd=0.0)
        add(w, toks, cost, unpriced)
        if sid:
            w["sessions"].add(sid)
        if sub:
            w["sub_usd"] += cost
        # a unit of a role: a session (main), an agent (sub), a call (mod); a turn of no tokens (the `<synthetic>`
        # one-turn sessions of a dead limit) is no unit of anything
        live = sum(toks.values()) > 0
        a = rm_slot(who_key if is_mod else (agent_type if agent_type in SUB_ROLES else "other_sub") if sub
                    else main_role, model_id, "mod" if is_mod else "sub" if sub else "main")
        add(a, toks, cost, unpriced)
        if live:
            d = when.astimezone(tz).date().isoformat()
            a["first"] = d if a["first"] is None or d < a["first"] else a["first"]
            a["last"] = d if a["last"] is None or d > a["last"] else a["last"]
            if not is_mod:
                a["units"].add((sid, agent) if sub else sid)
                lead = rm_slot(main_role, lead_of(sid), "main")
                lead["led"].add(sid)
                if sub:
                    lead["sp_agents"].add((sid, agent))
        if sub:
            add(rm_slot(main_role, lead_of(sid), "main")["spawned"], toks, cost, unpriced)
        if is_mod:
            add(slot(role, "mod", group="mod", units=set(), types={}), toks, cost, unpriced)
            add(slot(mod, (who_key, model_id)), toks, cost, unpriced)
        else:
            sub_role = agent_type if agent_type in SUB_ROLES else "other_sub"
            r = slot(role, sub_role if sub else main_role, group="sub" if sub else "main", units=set(), types={})
            add(r, toks, cost, unpriced)
            sp = slot(spawner, main_role, sessions=set(), chats=set(), own=blank(), sub=blank(), agents=set(), roles={})
            add(sp, toks, cost, unpriced)
            if who_kind == "chat":
                sp["chats"].add(who_key)
            if live:
                sp["sessions"].add(sid)
                r["units"].add((sid, agent) if sub else sid)
            if sub:
                add(sp["sub"], toks, cost, unpriced)
                sr = slot(sp["roles"], sub_role, units=set(), types={})
                add(sr, toks, cost, unpriced)
                if live:
                    sp["agents"].add((sid, agent))
                    sr["units"].add((sid, agent))
                if sub_role == "other_sub":
                    for acc in (r, sr):
                        ty = acc["types"].setdefault(agent_type, {"agents": set(), "cost_usd": 0.0})
                        ty["cost_usd"] += cost
                        if live:
                            ty["agents"].add((sid, agent))
            else:
                add(sp["own"], toks, cost, unpriced)
        add(slot(model, model_id), toks, cost, unpriced)
        add(slot(day, when.astimezone(tz).date().isoformat()), toks, cost, unpriced)
        ntok = sum(toks.values())
        for gk, wt in weights.items():
            if gk.startswith("#"):
                i = issues.setdefault(int(gk[1:]), {"turns": 0, "tokens": 0, "cost_usd": 0.0})
            else:
                i = no_issue
            i["turns"] += 1
            i["tokens"] += round(ntok * wt)
            i["cost_usd"] += cost * wt

    for t in turns:
        if t.timestamp is None or not (since <= t.timestamp <= until):
            continue
        toks = {"input": t.input_tokens, "cache_write": t.cache_creation_input_tokens,
                "cache_read": t.cache_read_input_tokens, "output": t.output_tokens}
        costs = None
        price = prices.get(t.model or "")
        if price is not None and tm.turn_cost(t, prices) is not None:
            one_hour = min(t.cache_creation_1h_tokens, t.cache_creation_input_tokens)
            costs = kind_costs(toks, tm.turn_price(t, price), one_hour)
        title = (title_of.get(t.session_id) if t.agent_id is not None else None) or t.title
        title = title.strip() if title else ""
        if titled is not None and t.session_id not in titled:
            title = ""
        k = _kind(title)
        if k:
            who_key, who_kind = {"orch": "ORCH"}.get(k, k), k
        elif title:
            who_key, who_kind = title, "chat"
        else:
            who_key, who_kind = "", "untitled"
        low = title.lower()
        # a brain chat is NAMED after the brain title (`Brain`, `Brain: <topic>`); the title only holding the word
        # somewhere (a pasted hand-off as a title) is any other chat
        main_role = k or ("brain" if low and any(low.startswith(b) for b in brain_titles) else "other_chat")
        ingest(t.timestamp, toks, t.model or "(unknown)", costs, who_key, who_kind, t.agent_id is not None,
               t.agent_type or "?", t.agent_id, t.session_id, tm.issue_group_weights(t), False, main_role)

    for c in mods:
        if not (since <= c["ts"] <= until):
            continue
        toks = {k: c[k] for k in TOKEN_KINDS}
        price = prices.get(c["model"]) if c["priced"] else None
        costs = kind_costs(toks, tier_rates(price, toks["input"] + toks["cache_write"] + toks["cache_read"]), 0) if price is not None else None
        ingest(c["ts"], toks, c["model"] or "(unknown)", costs, c["key"], "mod", False, "", None, None,
               {"UNATTRIBUTED": 1.0}, True)

    def tk(a: dict) -> int:
        return sum(a["tok"].values())

    def desc(rows: list[dict]) -> list[dict]:
        return sorted(rows, key=lambda r: -r["cost_usd"])

    days_rows = []
    d = until.astimezone(tz).date()
    while d >= since.date():
        key = d.isoformat()
        a = day.get(key)  # #1013 row 4: a day the log holds nothing for is no row (it was drawn as $0.00)
        if a is not None:
            days_rows.append({"key": key, "turns": a["turns"], "tokens": tk(a), "cost_usd": a["cost"]})
        d -= timedelta(days=1)
    window_days = (until.astimezone(tz).date() - since.date()).days + 1  # the per-day rates divide by the window's days
    by_issue = desc([{"number": n, **v, **_issue_row(issue_info, n)} for n, v in issues.items()])
    rest = by_issue[top_issues:]

    def types(a: dict) -> list[dict]:
        return desc([{"key": k, "agents": len(v["agents"]), "cost_usd": v["cost_usd"]} for k, v in a["types"].items()])

    def part(a: dict) -> dict:
        return {"turns": a["turns"], "tokens": tk(a), "cost_usd": a["cost"]}

    def tickets(role_key: str, v: dict) -> dict:
        if closed is None or role_key != "orch":
            return {"ended": None, "closed": None}
        done, ended = closed
        return {"ended": sum(1 for sid in v["led"] if sid[:8] in ended),
                "closed": sum(len(done.get(sid[:8], ())) for sid in v["led"])}
    return {
        "since": since.isoformat(timespec="seconds"), "until": until.isoformat(timespec="seconds"),
        "total": {"cost_usd": total["cost"], "turns": total["turns"], "unpriced_turns": total["unpriced_turns"],
                  "tokens": dict(total["tok"]), "limit_pct": limit_pct(total["cost"], window_days), "limit_usd_per_pct": limit_usd_per_pct(),
                  "limit_days": max(window_days, 7)},
        "by_kind": [{"key": k, "tokens": total["tok"][k], "cost_usd": by_kind_cost[k]} for k in TOKEN_KINDS],
        "by_who": desc([{"key": k, "kind": v["kind"], "sessions": len(v["sessions"]), "turns": v["turns"],
                         "tokens": tk(v), "cost_usd": v["cost"], "sub_usd": v["sub_usd"]} for k, v in who.items()]),
        "days": window_days,
        "by_role": desc([{"key": k, "group": v["group"], "n": v["turns"] if k == "mod" else len(v["units"]),
                          **part(v), "types": types(v)} for k, v in role.items()]),
        "by_spawner": desc([{"key": k, "sessions": len(v["sessions"]), "chats": sorted(v["chats"]), **part(v),
                             "own": part(v["own"]), "sub": {**part(v["sub"]), "agents": len(v["agents"])},
                             "roles": desc([{"key": rk, "agents": len(rv["units"]), **part(rv), "types": types(rv)}
                                            for rk, rv in v["roles"].items()])} for k, v in spawner.items()]),
        "by_mod": sorted(({"key": k, "model": m, "calls": v["turns"], "unpriced_calls": v["unpriced_turns"],
                           **part(v)} for (k, m), v in mod.items()), key=lambda r: (r["key"], -r["cost_usd"])),
        "by_role_model": desc([{"role": rk, "group": v["group"], "model": m,
                                "n": v["turns"] if v["group"] == "mod" else len(v["units"]), "led": len(v["led"]),
                                **part(v), "unpriced_turns": v["unpriced_turns"], "first": v["first"],
                                "last": v["last"], "spawned": {**part(v["spawned"]), "agents": len(v["sp_agents"])},
                                **tickets(rk, v)} for (rk, m), v in rm.items()]),
        "by_model": desc([{"key": k, "turns": v["turns"], "unpriced_turns": v["unpriced_turns"], "tokens": tk(v),
                           "cost_usd": v["cost"]} for k, v in model.items()]),
        "by_day": days_rows,
        "by_issue": by_issue[:top_issues],
        "issue_rest": {"n": len(rest), "tokens": sum(r["tokens"] for r in rest),
                       "cost_usd": sum(r["cost_usd"] for r in rest)},
        "no_issue": no_issue,
    }


COST_WINDOWS = (("cost_day", 1), ("cost_week", 7), ("cost_month", 30))


def _core_token_meter():
    """The repo's own `scripts/token_meter.py` (#161): the `cut` of a repo whose settings name no cost module."""
    from tools.agentops import monitor_agents as ma

    return ma._token_meter()


def slow() -> dict:
    out: dict = {"at": datetime.now().strftime("%H:%M:%S")}
    md = _settings_module("monitor_dashboard")  # the panel parts read the transcripts through its token_meter; absent = `cut` alone (#161)
    infos = _ISSUE_INFO_ONCE  # module-level: the closed list is read once per process, not once per slow() call (#1113)
    if md is not None:
        tm = md.token_meter
        tm.scan_projects_dir = _memo(tm.scan_projects_dir)
        tm.fetch_closed_issues = _memo(tm.fetch_closed_issues)
        # The Results tab is gone (owner, 06.10): no `results` pull.
        jobs = (("cost_day", lambda: md.panel_cost(days=1)),
                ("cost_week", lambda: md.panel_cost(days=7)), ("cost_month", lambda: md.panel_cost(days=30)))
        for key, fn in jobs:
            try:
                out[key] = fn()
            except Exception as exc:  # noqa: BLE001
                out[f"{key}_error"] = repr(exc)[:200]
        for key, _days in COST_WINDOWS:
            cost = out.get(key)
            if isinstance(cost, dict):
                cost.pop("refactor", None)
                for k in ("by_issue", "by_session"):
                    if isinstance(cost.get(k), list):
                        cost[k] = cost[k][:20]
    else:
        try:
            tm = _core_token_meter()
        except Exception as exc:  # noqa: BLE001
            for key, _days in COST_WINDOWS:
                out[f"{key}_error"] = repr(exc)[:200]
            return out
        for key, _days in COST_WINDOWS:
            out[key] = {}
    scan = _memo(tm.scan_projects_dir)  # one scan of the transcripts serves the three windows
    for key, days in COST_WINDOWS:
        if isinstance(out.get(key), dict):
            try:
                since, until = period_window(days, datetime.now().astimezone())
                out[key]["cut"] = cost_cut(
                    scan(tm.DEFAULT_PROJECTS_DIR).turns, tm.load_prices(tm.DEFAULT_PRICES_PATH), tm,
                    since=since, until=until,
                    titled=titled_sessions(tm.discover_main_transcripts(tm.DEFAULT_PROJECTS_DIR)),
                    closed=_closed_by_session(), issue_info=infos(),
                    mods=mod_calls(MONITOR_USAGE, "monitor"))
            except Exception as exc:  # noqa: BLE001
                out[key]["cut_error"] = repr(exc)[:200]
    return out


# ---------------------------------------------------------------- orch

def _orch_cache() -> Path:  # #182: resolved at each use, so a moved CACHE_DIR moves it
    return CACHE_DIR / "orch.json"
CACHE_VERSION = 2
PERIODS = (("day", 1), ("week", 7), ("month", 30))
#: A session's kind by its custom title: `orch <repo> #568 #564`, `autopilot #486`,
#: (the names the project's launchers give them). The add-on's `SESSION_KINDS` (name, compiled regex) pairs add
#: the project's own helper-session kinds.
BASE_KINDS = (("orch", re.compile(r"^orch(?:\s|#|$)", re.I)),
              ("autopilot", re.compile(r"^autopilot(?:\s|#|$)", re.I)))


def kinds_list() -> tuple:
    return BASE_KINDS + tuple(getattr(addon(), "SESSION_KINDS", None) or ())


def _kind(title: str | None) -> str | None:
    for kind, rx in kinds_list():
        if title and rx.match(title.strip()):
            return kind
    return None


def _sum_turns(turns: list, prices: dict, tm) -> dict:
    cost = 0.0
    unpriced = 0
    for t in turns:
        c = tm.turn_cost(t, prices)
        if c is None:
            unpriced += 1
        else:
            cost += c
    # ctx: token_meter's Turn.context (input + cache_write + cache_read) summed per turn = context
    # size x turns, the cached prefix counted again on every turn. new: tokens actually processed
    # fresh per turn (uncached input + cache_write + output), the cached re-reads left out.
    new = sum(t.input_tokens + t.cache_creation_input_tokens + t.output_tokens for t in turns)
    return {"turns": len(turns), "ctx": sum(t.context for t in turns), "new": new, "cost": cost, "unpriced": unpriced}


def _closed_by_session() -> tuple[dict[str, set[int]], set[str]]:
    """`artifacts/runs/events.jsonl`: the dispatcher's `orch_end` event, one per issue a session
    held when it ended, `outcome` read back from GitHub at that moment (dispatch.py `_finish` /
    `_resolve_outcome`). Returns (session8 -> issues it ended `closed`, every session8 that ended)."""
    closed: dict[str, set[int]] = {}
    ended: set[str] = set()
    path = REPO / "artifacts" / "runs" / "events.jsonl"
    if not path.is_file():
        return closed, ended
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("kind") not in ("orch_end", "orch_session_end"):
                continue
            s = str(e.get("session") or "")[:8]
            if not s:
                continue
            ended.add(s)
            if e.get("kind") == "orch_end" and e.get("outcome") == "closed" and e.get("issue") is not None:
                closed.setdefault(s, set()).add(int(e["issue"]))
    return closed, ended


def orch() -> dict:
    """ORCH / autopilot / helper sessions per period: counts, subagents by agent_type and by
    model, turns, tokens (token_meter's `context`), $ with subagents, issues closed per ORCH.
    Every transcript is parsed by scripts/token_meter.py and its summary cached by path + mtime + size."""
    started = time.time()
    sys.path.insert(0, str(REPO / "scripts"))
    try:  # token_meter absent: the ORCH part is empty, not an error
        import token_meter as tm
    except ImportError:
        return {"at": datetime.now().strftime("%H:%M:%S")}

    prices_path = tm.DEFAULT_PRICES_PATH
    prices = tm.load_prices(prices_path)
    stamp = [CACHE_VERSION, prices_path.stat().st_mtime_ns]
    cache: dict = {}
    try:
        raw = json.loads(_orch_cache().read_text(encoding="utf-8"))
        if raw.get("stamp") == stamp:
            cache = raw.get("files", {})
    except (OSError, ValueError):
        pass
    fresh: dict = {}
    parsed = 0

    def cached(path: Path, compute) -> dict:
        nonlocal parsed
        st = path.stat()
        key = str(path)
        hit = cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            fresh[key] = hit
            return hit[2]
        parsed += 1
        val = compute()
        fresh[key] = [st.st_mtime_ns, st.st_size, val]
        return val

    def main_summary(path: Path) -> dict:
        mt = tm.parse_main_transcript(path)
        stamps = [t.timestamp for t in mt.turns if t.timestamp is not None]
        out = _sum_turns(mt.turns, prices, tm)
        out.update(title=mt.title, first=min(stamps).isoformat() if stamps else None)
        return out

    def sub_summary(path: Path, meta: dict) -> dict:
        turns = tm.parse_subagent_transcript(path, meta)
        models = [t.model for t in turns if t.model and not t.model.startswith("<")]
        out = _sum_turns(turns, prices, tm)
        out.update(type=meta.get("agentType") or "?", model=models[-1] if models else "?")
        return out

    projects = tm.DEFAULT_PROJECTS_DIR
    sessions = []
    for path in tm.discover_main_transcripts(projects):
        m = cached(path, lambda p=path: main_summary(p))
        kind = _kind(m.get("title"))
        if kind is None or not m.get("first"):
            continue
        subs = []
        sub_dir = projects / path.stem / "subagents"
        if sub_dir.is_dir():
            for sp in sorted(sub_dir.glob("*.jsonl")):
                meta_path = sp.with_name(sp.stem + ".meta.json")
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
                except (OSError, ValueError):
                    meta = {}
                subs.append(cached(sp, lambda p=sp, mm=meta: sub_summary(p, mm)))
        sessions.append((path.stem[:8], kind, parse_ts(m["first"]), m, subs))

    cache_file = _orch_cache()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stamp": stamp, "files": fresh}), encoding="utf-8")
    tmp.replace(cache_file)

    closed, ended = _closed_by_session()
    periods = {}
    for name, days in PERIODS:
        since = NOW - timedelta(days=days)
        kinds: dict = {}
        for k, _ in kinds_list():
            kinds[k] = {"sessions": 0, "ended": 0, "closed": 0, "subagents": 0, "turns": 0, "ctx": 0, "new": 0,
                        "cost": 0.0, "sub_cost": 0.0, "unpriced": 0, "by_type": {}, "by_model": {}}
        for sid, kind, first, m, subs in sessions:
            if first is None or first < since:
                continue
            row = kinds[kind]
            row["sessions"] += 1
            if kind == "orch" and sid in ended:
                row["ended"] += 1
                row["closed"] += len(closed.get(sid, ()))
            row["subagents"] += len(subs)
            for x in [m, *subs]:
                row["turns"] += x["turns"]
                row["ctx"] += x["ctx"]
                row["new"] += x["new"]
                row["cost"] += x["cost"]
                row["unpriced"] += x["unpriced"]
            for sub in subs:
                row["sub_cost"] += sub["cost"]
                row["by_type"][sub["type"]] = row["by_type"].get(sub["type"], 0) + 1
                row["by_model"][sub["model"]] = row["by_model"].get(sub["model"], 0) + 1
        for row in kinds.values():
            row["cost"] = round(row["cost"], 2)
            row["sub_cost"] = round(row["sub_cost"], 2)
        periods[name] = {"since": since.isoformat(timespec="seconds"), "until": NOW.isoformat(timespec="seconds"),
                         "kinds": kinds}
    return {"at": datetime.now().strftime("%H:%M:%S"), "took": hd(time.time() - started), "parsed": parsed,
            "periods": periods}




#: seconds a lock of this mode may stand before it is a dead collector's: per mode, set from the pane's timeoutMs
#: (register.tsx:321-329: fast 60 s, tickets 180 s, slow 300 s, orch 180 s) and the longest live read of the mode.
#: In minutes: fast 1.5, tickets 2.5, slow 5.5, orch 3.3 (#1113).
#: `agents_raw` (#139, 60 s): the `claude agents` list, its read times out at 30 s (monitor_agents.live_sessions_raw)
SHARED_LIMITS = {"fast": 90, "tickets": 150, "slow": 330, "orch": 200, "agents_raw": 60}
#: a waiter still beside a live-looking lock this many seconds after the stale limit stops waiting (#1113)
SHARED_WAIT_MARGIN_S = 5


def shared_limits(mode: str) -> tuple[float, float]:
    """(stale, deadline) of `mode`, in seconds: a lock older than `stale` is a dead collector's, and a waiter
    gives up on a live-looking lock `deadline` seconds after it starts waiting (#1113)."""
    stale = SHARED_LIMITS[mode]
    return stale, stale + SHARED_WAIT_MARGIN_S


def shared_result(mode: str, fresh_for: float, compute) -> str:
    """The JSON of `compute()`, shared through `.cache/shared-<mode>.json`: served from the file
    while it is younger than `fresh_for`; computed under `shared-<mode>.lock` (one collector at a
    time; a lock older than the mode's stale limit is a dead collector's and is taken over at once).
    A caller that finds a live collector waits for its file; at the deadline it serves the holder's
    last file, else computes its own read without the lock and without saving it. It never raises
    from the wait and never computes beside a live collector before the deadline (#1113)."""
    shared = CACHE_DIR / f"shared-{mode}.json"
    lock = shared.with_suffix(".lock")
    stale, wait_for = shared_limits(mode)

    def fresh() -> str | None:
        try:
            if time.time() - shared.stat().st_mtime < fresh_for:
                return shared.read_text(encoding="utf-8")
        except OSError:
            pass
        return None

    text = fresh()
    if text is not None:
        return text
    CACHE_DIR.mkdir(exist_ok=True)
    deadline = time.time() + wait_for
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            text = fresh()
            if text is not None:
                return text
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink(missing_ok=True)  # a dead collector's lock: taken over at once
                    continue
            except OSError:
                continue  # the holder released it between the two calls
            if time.time() >= deadline:
                # the holder overran the wait: its last file if there is one (no second read), else our own read
                # beside its lock; our own read is not saved, the holder's file stays the one on disk
                try:
                    return shared.read_text(encoding="utf-8")
                except OSError:
                    return json.dumps(compute(), default=str, ensure_ascii=False)
            time.sleep(0.2)
    try:
        text = fresh()  # the holder may have finished between our last look and the lock
        if text is not None:
            return text
        text = json.dumps(compute(), default=str, ensure_ascii=False)
        tmp = shared.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        for _ in range(10):  # a reader holding the file open makes the swap fail on Windows: retry
            try:
                os.replace(tmp, shared)
                break
            except PermissionError:
                time.sleep(0.05)
        else:
            shared.write_text(text, encoding="utf-8")
            tmp.unlink(missing_ok=True)
        return text
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


#: the long-lived collector's files (#142): its one lock (in CACHE_DIR) and the fast data it keeps current (at
#: `fast_path()`, the pane's path, #166; CACHE_DIR's file only when the cache is overridden)
COLLECTOR_LOCK = "collector.lock"
COLLECTOR_FAST = "collector-fast.json"


def _pid_alive(pid: int) -> bool:
    """Whether `pid` is a live process. psutil when it imports; else POSIX signal 0. Without psutil on Windows there is
    no safe signal 0 (it is Ctrl+C to the console group), so the pid is taken as dead and its lock is taken over."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        return psutil.pid_exists(pid)
    if sys.platform == "win32":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_holder_is_live_other(lock: Path) -> bool:
    """True when `lock` names a live pid other than ours; a missing, unreadable or dead holder is not live."""
    try:
        pid = json.loads(lock.read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError, AttributeError):
        return False
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    return _pid_alive(pid)


def _acquire_collector_lock(lock: Path) -> bool:
    """Take `lock` for this process. False when a live other collector holds it; a dead or unreadable lock is removed
    and taken over at once (O_EXCL, so two starts cannot both take it)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_holder_is_live_other(lock):
                return False
            lock.unlink(missing_ok=True)  # a dead collector's lock
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid()}))
        return True
    return False


def fast_path() -> Path:
    """Where the collector writes `collector-fast.json` (#166): the file the installed pane reads,
    `<install root>/<plugin id>/bin/.cache/collector-fast.json` (install root: `install_folder.install_root()`, env
    AGENTOPS_MODS_DIR else ~/.claude/mods; plugin id: `monitor.plugin_id` of this project). CACHE_DIR when the cache is
    overridden (`MONITOR_CACHE_DIR`, or CACHE_DIR moved off its default folder), when the install folder does not exist
    (none is created), or when the path cannot be computed. Never raises."""
    fallback = CACHE_DIR / COLLECTOR_FAST
    if os.environ.get("MONITOR_CACHE_DIR") or CACHE_DIR != _DEFAULT_CACHE_DIR:
        return fallback
    try:
        from tools.agentops import install_folder  # the same import as `opencode()` (#679)
        folder = install_folder.install_root() / install_folder.plugin_id(REPO)
        if not folder.is_dir():
            return fallback
        return folder / "bin" / ".cache" / COLLECTOR_FAST
    except Exception:  # noqa: BLE001 - a path that cannot be computed is the cache folder, not an error
        return fallback


def _write_collector_fast(doc: dict) -> None:
    """`doc` to `fast_path()` through a tmp file and os.replace; a reader holding the file makes the swap fail on
    Windows, so it is retried (the same idiom as shared_result). The folder of the path is created when missing (the
    install folder's `bin/.cache`, #166; CACHE_DIR already exists)."""
    text = json.dumps(doc, default=str, ensure_ascii=False)
    target = fast_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(10):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            time.sleep(0.05)
    target.write_text(text, encoding="utf-8")
    tmp.unlink(missing_ok=True)


def _set_now(epoch: float) -> None:
    """The module's `NOW` (UTC) for this iteration: the readers of `NOW` (durations, ages, periods) measure from it, so a
    long-lived collector must reassign it, not keep the value it had at import (#142)."""
    global NOW
    NOW = datetime.fromtimestamp(epoch, timezone.utc)


def serve(iterations: int | None = None, sleep=time.sleep, now=time.time) -> None:
    """The long-lived collector (#142): every `monitor.refresh_s` it computes `fast()` once and writes
    `fast_path()` as `{"at_ms", "refresh_s", "data"}`, then sleeps the interval less the time the
    computation took (at least 0.2 s). A `fast()` that raises writes nothing for that iteration; an OSError from the
    write is one stderr line; both log one line to stderr and the loop goes on. `iterations=N` runs N iterations and returns (tests); None runs until stopped. A live other collector (pid
    in `collector.lock`) makes it return at once without computing; the lock is removed on exit. Prints nothing to stdout."""
    lock = CACHE_DIR / COLLECTOR_LOCK
    if not _acquire_collector_lock(lock):
        return
    try:
        done = 0
        while iterations is None or done < iterations:
            done += 1
            started = now()
            _set_now(started)  # before fast(): the data of this iteration is measured from this NOW
            try:
                data = fast()
            except MemoryError:  # #276: the dispatcher's memory cap ended an allocation: end, never limp on
                print("collect serve: MemoryError: ended at the memory cap", file=sys.stderr)
                raise
            except Exception as exc:  # noqa: BLE001
                data = None
                print(f"collect serve: fast() failed: {exc!r}"[:300], file=sys.stderr)
            interval = monitor_setting("refresh_s", REFRESH_S_DEFAULT)
            if data is not None:
                try:
                    _write_collector_fast({"at_ms": int(now() * 1000), "refresh_s": interval, "data": data})
                except OSError as exc:  # disk full, a locked file after the retries: the loop and the lock go on
                    print(f"collect serve: write failed: {exc!r}"[:300], file=sys.stderr)
            if iterations is not None and done >= iterations:
                return
            sleep(max(0.2, interval - (now() - started)))
    finally:
        lock.unlink(missing_ok=True)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "fast"
    if mode == "serve":  # the long-lived collector (#142): writes its file, prints nothing
        serve()
        return
    extra = dict(getattr(addon(), "MODES", None) or {})
    if mode in extra:
        try:
            data = extra[mode](*sys.argv[2:7])
        except Exception as exc:  # noqa: BLE001
            data = {"error": repr(exc)[:200]}
    else:
        compute = {"fast": fast, "tickets": tickets, "slow": slow, "orch": orch}[mode]
        # Several chats load this mod: a result younger than its mode's refresh is shared, not
        # recomputed (`fast` under the `monitor.refresh_s` cadence, #139), and while one collector computes, the others
        # wait for its file instead of computing the same thing beside it.
        # fast: one second under the pull interval, so every pull finds the file stale; 0.5 s is the floor
        fresh_for = {"fast": max(0.5, monitor_setting("refresh_s", REFRESH_S_DEFAULT) - 1), "tickets": 12, "slow": 590,
                     "orch": 890}.get(mode)
        if fresh_for:
            sys.stdout.write(shared_result(mode, fresh_for, compute))
            sys.exit(0)
        data = compute()
    # a run on a remote machine (`here`) writes to an ssh pipe in its own code page: plain ASCII JSON survives it
    sys.stdout.write(json.dumps(data, default=str, ensure_ascii=sys.argv[-1] == "here"))


if __name__ == "__main__":
    main()
