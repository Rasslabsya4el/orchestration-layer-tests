"""PreToolUse hook: the permission guard (#354, unit 1; ADR 0112).

Every session runs in ``bypassPermissions``; this hook is the one thin guard behind it: a short red
list against the stupid (wiping a repo, deleting Windows), never a blocker of ordinary work. Seven
red lines, the reason of a refusal names the line:

1. bulk delete outside the throwaway areas (scratchpad / temp, a POSIX ``/tmp`` only below its first
   folder, ``tmp_*``, caches, and anything inside the session's own worktree except its root, its
   ``.git`` and a junction; ``/tmp`` itself, its glob and a ``..`` segment are not throwaway),
   ``git clean``, ``git worktree remove``;
2. the machine (write verbs on Windows / Program Files / ProgramData / Documents..., the registry,
   ``format``, ``shutdown``, services, scheduled tasks ...);
3. shared git history (force / delete / foreign-remote push, ``git remote`` edits; and outside the
   session's own worktree ``reset --hard``, ``checkout .``, ``branch -D``, ``rebase`` ...);
4. destroying on GitHub (``gh repo delete``, ``gh api -X DELETE`` ...);
5. ending processes (``taskkill``, ``Stop-Process``, ``kill`` ...; the way is
   ``python -m tools.agentops stop``);
6. the harness and secrets (settings files, hooks and the orchestration config outside a worktree;
   ``.env``, ssh keys, ``gh auth token``);
7. a console window on the owner's desktop (#490): ``Start-Process`` without ``-WindowStyle Hidden`` /
   ``-NoNewWindow``, ``cmd /k``, a shell ``start``, ``creationflags=`` with ``CREATE_NEW_CONSOLE`` /
   ``DETACHED_PROCESS`` / ``0x8`` / ``0x10``; the way is ``scripts/launch_hidden.ps1`` or
   ``CREATE_NO_WINDOW`` (``docs/agents/machine.md``). Known limit: the guard sees command text only; a
   driver written to a file and run as ``python drive.py`` passes it.

Text matching only: the hook reads the command text and the paths in it and executes nothing. A wrapper
is opened (``cmd /c|/k``, ``bash|sh [-l]c``, ``wsl [options] [--] <cmd>``, ``pwsh|powershell -c|-Command``,
recursively, #99) and its inner command is judged by every rule as a bare one is; inside ``wsl`` a path is a
Linux path (``/tmp/x`` throwaway, ``/mnt/c/..`` is drive C, any other absolute path is not throwaway). A
wrapper that cannot be parsed (an unbalanced quote) and holds a delete word is refused. An encoded command,
``eval`` and a deletion from inside a script pass by design. The guard fails open: a malformed payload or
any exception means exit 0 and no output.

``decide(payload)`` is the pure decision (the reason, or None); ``guard`` adds the log line;
``main`` reads stdin and writes the deny JSON. Stdlib only, no import from this repo (startup time).
"""

from __future__ import annotations

import ast
import json
import os
import posixpath
import re
import stat
import sys
import time
import warnings

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "var", "agentops", "permission_guard.log")
MAX_TEXT = 120
REFUSAL_SUFFIX = "a refusal is final: write it in the issue, do not retry"
# the red lines' short names, written into the log row as `rule` (#363)
RULES = {1: "bulk delete", 2: "the machine", 3: "shared git history", 4: "destroying on GitHub",
         5: "ending processes", 6: "the harness and secrets", 7: "console window"}
_REASON = re.compile(r"red line (\d+): (.*)", re.DOTALL)

# ----------------------------------------------------------------------------------------------
# paths


_DRIVE = re.compile(r"^[a-z]:/")
_MSYS = re.compile(r"^/(?:cygdrive/)?([a-zA-Z])(?=/|$)")
_ENV_PCT = re.compile(r"%(\w+)%")
_ENV_PS = re.compile(r"\$env:(\w+)", re.IGNORECASE)
_ENV_SH = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _env(name: str) -> str | None:
    up = name.upper()
    val = os.environ.get(name) or os.environ.get(up)
    if val:
        return val
    home = os.path.expanduser("~")
    if up in ("TEMP", "TMP", "TMPDIR"):
        return os.path.join(home, "AppData", "Local", "Temp")
    if up in ("HOME", "USERPROFILE"):
        return home
    if up == "APPDATA":
        return os.path.join(home, "AppData", "Roaming")
    if up == "LOCALAPPDATA":
        return os.path.join(home, "AppData", "Local")
    return None


def _expand(word: str) -> str:
    if word[:1] == "~" and (len(word) == 1 or word[1] in "/\\"):
        word = os.path.expanduser("~") + word[1:]
    if "%" in word:
        word = _ENV_PCT.sub(lambda m: _env(m.group(1)) or m.group(0), word)
    if "$" in word:
        word = _ENV_PS.sub(lambda m: _env(m.group(1)) or m.group(0), word)
        word = _ENV_SH.sub(lambda m: _env(m.group(1) or m.group(2)) or m.group(0), word)
    return word


def _norm(word: str, cwd: str = "") -> str:
    """`word` as an absolute, lower-case, forward-slash, normalised path (relative ones resolved
    against `cwd`). Never raises."""

    p = _expand(word.strip()).replace("\\", "/")
    m = _MSYS.match(p)
    if m:
        p = m.group(1) + ":" + p[m.end(1):]
    p = p.lower()
    if not (_DRIVE.match(p) or p.startswith("/")):
        base = cwd.replace("\\", "/").lower() if cwd else ""
        p = (base + "/" + p) if base else p
    return posixpath.normpath(p)


_WORKTREE_TEXT = re.compile(r"^(.*/\.claude/worktrees/[^/]+)(?:/|$)")


def _worktree_root(path: str) -> str | None:
    """The git worktree root that holds `path` (a normalised path), else None. A worktree is told by
    its name (`/.claude/worktrees/<name>`, even when it is not on disk) or by the `.git` FILE the
    nearest repository directory up the path has (a repo root keeps a `.git` directory): so a worktree
    kept anywhere counts."""

    m = _WORKTREE_TEXT.match(path)
    if m:
        return m.group(1)
    cur = path
    for _ in range(64):
        try:
            if os.path.isfile(cur + "/.git"):
                return cur
            if os.path.isdir(cur + "/.git"):
                return None
        except OSError:
            return None
        parent = posixpath.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def _own_worktree(path: str) -> bool:
    return _worktree_root(path) is not None


_TEMPS = tuple(t for t in (os.environ.get("TEMP"), os.environ.get("TMP"), os.environ.get("TMPDIR")) if t)
_TEMP_NORMS = tuple(_norm(t) for t in _TEMPS)
# a POSIX /tmp throwaway is only a path with a first component below /tmp that holds no glob char:
# `/tmp` itself, `/tmp/*` and `/tmp/a*/x` are not throwaway (the raw `..` check is _DOTDOT_RE, in _throwaway)
_POSIX_TMP_RE = r"^/tmp/(?![^/]*[*?\[])[^/]+(/|$)"
_SCRATCH_RE = re.compile(r"/appdata/local/temp(/|$)|" + _POSIX_TMP_RE)
_DOTDOT_RE = re.compile(r"(^|[/\\])\.\.([/\\]|$)")


def _repo_root(parent: str, cwd_norm: str) -> bool:
    if parent == cwd_norm or re.search(r"/\.claude/worktrees/[^/]+$", parent):
        return True
    try:
        return os.path.exists(parent + "/.git")
    except OSError:
        return False


def _throwaway(path: str, cwd_norm: str, raw: str = "") -> bool:
    """Line 1's exempt areas: scratchpad / %TEMP%, a POSIX `/tmp` below its first folder (not `/tmp`
    itself, not a glob in it, and not a `..` segment in `raw`, the path as typed), a `tmp_*` entry at
    a repo root (or under one), a `__pycache__` / `.pytest_cache` directory."""

    if _SCRATCH_RE.search(path) and not (path.startswith("/tmp/") and _DOTDOT_RE.search(raw)):
        return True
    for t in _TEMP_NORMS:
        if path == t or path.startswith(t + "/"):
            return True
    parts = path.split("/")
    if "__pycache__" in parts or ".pytest_cache" in parts:
        return True
    cur = path
    for _ in range(32):
        parent, name = posixpath.split(cur)
        if not name or parent == cur:
            break
        if name.startswith("tmp_") and _repo_root(parent, cwd_norm):
            return True
        cur = parent
    return False


def _is_link(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)  # reparse point


def _reaches_a_link(path: str, limit: int = 5000) -> bool:
    """A symlink / directory junction at `path` or anywhere below it: `rm -r` goes THROUGH one and
    deletes the shared target (the incident of 2026-09-18, docs/agents/machine.md). More than `limit`
    entries below counts as yes."""

    try:
        if _is_link(os.lstat(path)):
            return True
    except OSError:
        return False
    stack, seen = [path], 0
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    seen += 1
                    if seen > limit or _is_link(e.stat(follow_symlinks=False)):
                        return True
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
        except OSError:
            continue
    return False


def _inside_own_worktree(path: str, root: str | None) -> bool:
    """Line 1's second exempt area: `path` strictly inside `root`, the session's own worktree. Not the
    root itself (never delete a worktree), not its `.git`, not a glob or an unresolved variable (it
    could name anything), and nothing on the way to it or below it that is a junction / symlink."""

    if not root or not path.startswith(root + "/"):
        return False
    rel = path[len(root) + 1:]
    if rel.split("/")[0] == ".git" or re.search(r"[*?\[$`]", rel):
        return False
    cur = root
    for part in rel.split("/")[:-1]:
        cur += "/" + part
        try:
            if _is_link(os.lstat(cur)):
                return False
        except OSError:
            break
    return not _reaches_a_link(path)


# line 2 / 6 path classes
_MACHINE_RE = re.compile(r"^c:/(?:windows|program files(?: \(x86\))?|programdata)(?:/|$)"
                         r"|^c:/users/[^/]+/(?:documents|desktop|downloads)(?:/|$)")
_MACHINE_TEXT_RE = re.compile(r"\bc:/(?:windows|program files(?: \(x86\))?|programdata)\b")
_USER_SETTINGS_RE = re.compile(r"^[a-z]:/users/[^/]+/\.claude/settings(?:\.local)?\.json$")
_HARNESS_RE = re.compile(r"(?:^|/)\.claude\.json$"
                         r"|/\.claude/settings(?:\.local)?\.json$"
                         r"|/config/core_templates/settings[^/]*\.json$"
                         r"|/tools/hooks/[^/]+\.py$"
                         r"|/config/orchestration\.json$")
_SECRET_RE = re.compile(r"/\.ssh/id_[^/]*$|/\.aws/credentials$|/gh/hosts\.yml$")
_REGISTRY_RE = re.compile(r"^(?:hk[a-z]{2,4}:|hkey_)")


def _machine_path(path: str) -> bool:
    return bool(_MACHINE_RE.match(path))


def _harness_write(path: str) -> bool:
    if _USER_SETTINGS_RE.match(path):
        return True
    return bool(_HARNESS_RE.search(path)) and not _own_worktree(path)


def _secret_path(path: str) -> bool:
    if _SECRET_RE.search(path) and not path.endswith(".pub"):
        return True
    base = path.rsplit("/", 1)[-1]
    if base == ".env":
        return True
    return base.startswith(".env.") and not base.endswith((".example", ".sample", ".template", ".dist"))


# ----------------------------------------------------------------------------------------------
# command text -> segments

_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "time", "sudo", "command",
             "exec", "nohup", "xargs", "env", "call", "foreach", "elseif"}


def _strip_heredocs(command: str) -> str:
    if "<<" not in command:
        return command
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC.search(line)
        i += 1
        if m:
            while i < len(lines) and lines[i].strip() != m.group(2):
                i += 1
            i += 1
    return "\n".join(out)


class _Seg:
    __slots__ = ("words", "redirs", "raw")

    def __init__(self, words: list[str], redirs: list[str], raw: str) -> None:
        self.words = words
        self.redirs = redirs
        self.raw = raw


def _segments(text: str) -> list[_Seg]:
    """Split on `;` `|` `&` `&&` `||` and newlines outside quotes; each segment is its words (quotes
    removed) and its `>` / `>>` redirect targets. No shell semantics beyond that."""

    segs: list[_Seg] = []
    words: list[str] = []
    redirs: list[str] = []
    cur: list[str] = []
    inword = False
    quote = ""
    pending = ""  # "" | "path" | "skip": what the next word is, after a `>`
    start = 0
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal inword, pending, cur
        if inword:
            w = "".join(cur)
            if pending == "path":
                redirs.append(w)
            elif pending != "skip":
                words.append(w)
            pending = ""
        cur = []
        inword = False

    def end(at: int) -> None:
        nonlocal words, redirs, start
        flush()
        if words or redirs:
            segs.append(_Seg(words, redirs, text[start:at].strip()))
        words, redirs = [], []
        start = at + 1

    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < n and text[i + 1] in '"$`':
                i += 1
                cur.append(text[i])
            else:
                cur.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            inword = True
        elif ch in " \t\r":
            flush()
        elif ch in ";|&\n":
            end(i)
        elif ch == ">":
            if inword and "".join(cur).isdigit():
                cur, inword = [], False
            flush()
            while i + 1 < n and text[i + 1] == ">":
                i += 1
            if i + 1 < n and text[i + 1] == "&":
                i += 1
                pending = "skip"
            else:
                pending = "path"
        else:
            cur.append(ch)
            inword = True
        i += 1
    end(n)
    return segs


def _head(words: list[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(words):
        w = words[i]
        if _ASSIGN.match(w):
            i += 1
            continue
        s = w.lstrip("({&")
        if not s or s.lower() in _KEYWORDS:
            i += 1
            continue
        cmd = re.split(r"[\\/]", s)[-1].lower()
        if cmd.endswith(".exe"):
            cmd = cmd[:-4]
        return cmd, words[i + 1:]
    return "", []


# ----------------------------------------------------------------------------------------------
# the red lines

_DELETE_VERBS = {"rm", "ri", "remove-item", "del", "erase", "rd", "rmdir"}
_WRITE_VERBS = _DELETE_VERBS | {"mv", "move", "ren", "rename", "cp", "copy", "tee", "move-item", "mi",
                                "copy-item", "cpi", "rename-item", "rni", "set-content", "add-content",
                                "out-file", "new-item", "ni"}
_READ_SECRET_VERBS = {"cat", "type", "get-content", "gc", "echo", "less", "more", "head", "tail", "bat",
                      "cp", "copy", "copy-item", "cpi"}
_REG_PS_VERBS = {"set-itemproperty", "new-itemproperty", "remove-itemproperty", "new-item", "remove-item",
                 "sp", "rp", "ni", "ri"}
_MACHINE_BARE = {"format", "diskpart", "shutdown", "restart-computer", "stop-computer", "bcdedit", "netsh",
                 "stop-service", "set-service", "remove-service", "uninstall-package"}
_KILL = {"taskkill", "stop-process", "spps", "kill", "pkill", "killall", "tskill"}
_UNIX_RECURSIVE = re.compile(r"^-[rRfivId]*[rR][rRfivId]*$")
_WINFLAG = re.compile(r"^/[a-zA-Z?](?::\S*)?$")


def _paths(args: list[str]) -> list[str]:
    return [a for a in args if a and not a.startswith("-") and not _WINFLAG.match(a)]


def _recursive(args: list[str]) -> bool:
    for a in args:
        low = a.lower()
        if low in ("/s", "--recursive") or _UNIX_RECURSIVE.match(a):
            return True
        if low.startswith("-r") and "-recurse".startswith(low):
            return True
    return False


def _write_targets(cmd: str, args: list[str], redirs: list[str]) -> list[str]:
    out = list(redirs)
    if cmd in _WRITE_VERBS:
        out.extend(_paths(args))
    elif cmd in ("sed", "perl") and any(a.startswith("-i") or a == "--in-place" for a in args):
        out.extend(_paths(args))
    return out


_COPY_VERBS = {"cp", "copy", "copy-item", "cpi"}


def _copy_dest(args: list[str]) -> list[str]:
    """A copy writes only its destination: `-Destination` (any case, `-Destination:x` too), else the
    last path operand. A copy source is a read."""

    for i, a in enumerate(args):
        low = a.lower()
        if low == "-destination" and i + 1 < len(args):
            return [args[i + 1]]
        if low.startswith("-destination:") and len(a) > len("-destination:"):
            return [a[len("-destination:"):]]
    ps = _paths(args)
    return ps[-1:]


def _git_parts(args: list[str], cwd: str) -> tuple[str, list[str], str]:
    """`(subcommand, its arguments, the directory git works in)` for `git <args>`."""

    eff = _norm(cwd) if cwd else ""
    i = 0
    while i < len(args):
        w = args[i]
        if w == "-C" and i + 1 < len(args):
            eff = _norm(args[i + 1], eff)
            i += 2
        elif w == "-c" and i + 1 < len(args):
            i += 2
        elif w.startswith("-"):
            i += 1
        else:
            break
    if i >= len(args):
        return "", [], eff
    return args[i].lower(), args[i + 1:], eff


_FILE_DELETE_VERBS = {"rm", "ri", "remove-item", "del", "erase"}
_JUNCTION_NOTE = (" -- del / Remove-Item follow a directory junction into its target and delete the target's "
                  "files; remove only the link with: cmd /c rmdir <junction>")


def _under_a_link(path: str, root: str) -> bool:
    """`path` is, or lies below, a junction / symlink inside the worktree `root` (not looking above it)."""

    if not path.startswith(root + "/"):
        return False
    cur = root
    for part in path[len(root) + 1:].split("/"):
        cur += "/" + part
        try:
            if _is_link(os.lstat(cur)):
                return True
        except OSError:
            return False
    return False


_SHELL_HEADS = {"bash", "sh", "zsh", "dash", "ksh"}
_PS_HEADS = {"pwsh", "powershell"}
_CMD_FLAG = re.compile(r"^/(?:[dsqau]|[vef]:\w+)$", re.IGNORECASE)
_SHELL_VALUE_OPTS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
_PS_VALUE_OPTS = ("-executionpolicy", "-windowstyle", "-workingdirectory", "-configurationname", "-inputformat",
                  "-outputformat", "-version", "-settingsfile", "-custompipename")
_WSL_VALUE_OPTS = {"-d", "--distribution", "-u", "--user", "--cd", "--shell-type", "--distribution-id"}
_WSL_MANAGE = {"--install", "--list", "-l", "--import", "--import-in-place", "--export", "--unregister",
               "--terminate", "-t", "--shutdown", "--update", "--status", "--set-default", "-s",
               "--set-default-version", "--set-version", "--mount", "--unmount", "--help", "--version", "-v",
               "--manage"}
_MAX_WRAP_DEPTH = 8
_DELETE_WORD = re.compile(r"(?<![\w.-])(?:rm|ri|remove-item|del|erase|rd|rmdir)(?![\w-])", re.IGNORECASE)


def _ps_opt(low: str, *fulls: str) -> bool:
    return len(low) > 1 and any(f.startswith(low) for f in fulls)


def _inner_of(rest: list[str]):
    """The inner command a wrapper was given: one word holding spaces is a command line (re-parsed), several
    words are the command's own words; nothing -> None."""

    if not rest:
        return None
    if len(rest) == 1 and re.search(r"\s", rest[0]):
        return rest[0]
    return rest


def _unwrap_cmd(cmd, args):
    """`(wrapper name, inner)` when `cmd args` runs another command line, else None (#449, #465, #99). `inner` is a
    string to parse as a command line, or the list of the inner command's words. Wrappers: `cmd /c|/k`,
    `bash|sh [-l]c <script>`, `wsl [options] [--] <cmd>`, `pwsh|powershell [options] -c|-Command <cmd>`."""

    if cmd == "cmd":
        i = 0
        while i < len(args) and _CMD_FLAG.match(args[i]):
            i += 1
        if i < len(args) and args[i].lower() in ("/c", "/k"):
            inner = _inner_of(args[i + 1:])
            return ("cmd", inner) if inner else None
        return None
    if cmd in _SHELL_HEADS:
        i = 0
        while i < len(args):
            a = args[i]
            if a in _SHELL_VALUE_OPTS:
                i += 2
            elif re.fullmatch(r"-[A-Za-z]+", a):
                if "c" in a:  # `-c`, `-lc`, `-ec`: the next word is the script
                    return (cmd, args[i + 1]) if i + 1 < len(args) else None
                i += 1
            elif a.startswith("--") and a != "--":
                i += 1
            else:
                return None  # a script file, or `--`
        return None
    if cmd in _PS_HEADS:
        i = 0
        while i < len(args):
            low = args[i].lower()
            if not low.startswith("-"):
                rest = args[i:]  # no flag: PowerShell takes the words as the command
            elif _ps_opt(low, "-command"):
                rest = args[i + 1:]
            elif _ps_opt(low, "-file", "-encodedcommand") or low == "-ec":
                return None  # a script file / an encoded command: not read
            elif low in ("-ep", "-wd") or _ps_opt(low, *_PS_VALUE_OPTS):
                i += 2
                continue
            else:
                i += 1
                continue
            inner = _inner_of(rest)
            return (cmd, inner) if inner else None
        return None
    if cmd == "wsl":
        i = 0
        while i < len(args):
            a = args[i]
            low = a.lower()
            if low in _WSL_MANAGE:
                return None
            if a == "--":
                i += 1
                break
            if low in ("-e", "--exec"):
                i += 1
                break
            if low in _WSL_VALUE_OPTS:
                i += 2
            elif a.startswith("-"):
                i += 1
            else:
                break
        inner = _inner_of(args[i:])
        return ("wsl", inner) if inner else None
    return None


def _unbalanced(text: str) -> bool:
    """A quote opened and never closed, read as `_segments` reads quotes."""

    quote, i, n = "", 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < n and text[i + 1] in '"$`':
                i += 1
        elif ch in "'\"":
            quote = ch
        i += 1
    return bool(quote)


_WSL_MNT = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$", re.DOTALL)


def _wsl_word(w: str) -> str:
    """A word of a command run inside wsl, as the guard's Windows-minded `_norm` must read it: `/mnt/c/x` is
    `C:/x`; every other absolute Linux path (and `~`, `$VAR`) is moved under a folder that is nothing throwaway,
    so `/c/x` is not read as `C:/x` and `$TMPDIR` not as the Windows temp. `/tmp/..` stays."""

    m = _WSL_MNT.match(w)
    if m:
        return m.group(1) + ":/" + (m.group(2) or "")
    if w[:1] in ("~", "$") or _MSYS.match(w):
        return "/wsl.localhost/linux/" + w.lstrip("/").replace("$", "_").replace("~", "_")  # no Windows expansion
    return w


def _line1(cmd, args, redirs, cwd_norm, cwd, sub, gargs, session_cwd=""):
    if cmd == "git":
        if sub == "clean" or (sub == "worktree" and gargs[:1] == ["remove"]):
            return True
        return False
    if cmd in _DELETE_VERBS and _recursive(args):
        paths = _paths(args)
        if not paths:
            return True
        own = None  # the session's own worktree (the payload's cwd), looked up once, only when needed
        for p in paths:
            n = _norm(p, cwd)
            if _throwaway(n, cwd_norm, p):
                continue
            if own is None:
                own = (_worktree_root(_norm(session_cwd)) if session_cwd else None) or ""
            if not _inside_own_worktree(n, own):
                return True
    if cmd in _FILE_DELETE_VERBS and not _recursive(args):
        # a non-recursive `del` / `Remove-Item` on a junction (or below one) follows it into its target
        # and deletes the target's files (#449); `cmd /c rmdir <junction>` removes only the link and is
        # not in this set
        root = (_worktree_root(_norm(session_cwd)) if session_cwd else None) or ""
        for p in _paths(args):
            if root and _under_a_link(_norm(p, cwd), root):
                return _JUNCTION_NOTE
    return False


_PY_HEADS = {"python", "python3", "py", "pythonw"}
_PY_TREE_DELETE = re.compile(r"\b(?:rmtree|removedirs|rmdir)\s*\(")
# a file delete: `os.remove(`, a bare imported `remove(`, or any `.unlink(` / `unlink(`; a `.remove(` on another
# object (`table.remove`, `list.remove`, `set.remove`) deletes no file (#1131)
_PY_FILE_DELETE = re.compile(r"\bos\s*\.\s*remove\s*\(|(?<![\w.])remove\s*\(|\bunlink\s*\(")
_PY_LOOP = re.compile(r"\b(?:walk|rglob|glob|iterdir|listdir|scandir)\s*\(|\bfor\b|\bwhile\b")
_PY_TARGET = re.compile(r"\b(?:rmtree|removedirs|rmdir|walk|Path|PurePath|rglob|scandir|listdir|iterdir)\s*\(\s*"
                        r"[rRbB]?(?:'([^'\n]*)'|\"([^\"\n]*)\")")


_PY_HEAD_BEFORE = re.compile(r"(?<![\w.-])(?:python[\d.]*|py|pythonw)(?:\.exe)?(?=\s)", re.I)
_AST_TREE_NAMES = {"rmtree", "removedirs", "rmdir"}
_AST_LOOP_CALLS = {"walk", "rglob", "glob", "iterdir", "listdir", "scandir"}
_AST_TARGET_CALLS = _AST_TREE_NAMES | {"walk", "Path", "PurePath", "rglob", "scandir", "listdir", "iterdir"}
_AST_LOOP_NODES = (ast.For, ast.AsyncFor, ast.While, ast.comprehension)
_AST_CODE_FROM_TEXT = {"exec", "eval", "compile"}  # code built from a string: the tree cannot see it


def _python_source(raw: str, full: str) -> str:
    """The Python the segment runs: the body of every heredoc whose opener line runs a python head (stdin), or
    the segment itself when it has no heredoc. A heredoc that writes some other file is text (#845)."""

    if "<<" not in raw:
        return raw
    lines = full.split("\n")
    bodies: list[str] = []
    i = 0
    while i < len(lines):
        opener = lines[i]
        m = _HEREDOC.search(opener)
        i += 1
        if not m:
            continue
        body = []
        while i < len(lines) and lines[i].strip() != m.group(2):
            body.append(lines[i])
            i += 1
        i += 1
        if _PY_HEAD_BEFORE.search(opener[:m.start()]):
            bodies.append("\n".join(body))
    return "\n".join(bodies)


def _callee_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _code_from_text(call: ast.Call) -> bool:
    """`exec` / `eval` / `compile` reached by any road: a bare name, `builtins.exec`, `getattr(b, 'exec')(...)`,
    `__builtins__['exec'](...)`. (`re.compile` builds no code.)"""

    f = call.func
    if isinstance(f, ast.Name):
        return f.id in _AST_CODE_FROM_TEXT
    if isinstance(f, ast.Attribute):
        return f.attr in _AST_CODE_FROM_TEXT and not (f.attr == "compile" and isinstance(f.value, ast.Name)
                                                      and f.value.id == "re")
    return isinstance(f, (ast.Call, ast.Subscript))


def _runs_a_process(call: ast.Call) -> bool:
    f = call.func
    if not isinstance(f, ast.Attribute) or not isinstance(f.value, ast.Name):
        return False
    return f.value.id == "subprocess" or (
        f.value.id == "os" and (f.attr in ("system", "popen") or f.attr.startswith(("exec", "spawn"))))


def _py_ast_scan(src: str):
    """(note, literal targets) of the first delete the script's tree calls, or (None, []) when it calls none;
    None when the source does not parse or builds code from a string, so the regex path judges it (#35)."""

    if not src.strip():
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # a shell-quoted `\C` is no business of the hook's stderr
            tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    calls = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)), key=lambda n: (n.lineno, n.col_offset))
    if any(_code_from_text(c) for c in calls):
        return None
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    if any(_runs_a_process(c) for c in calls) and any(
            _PY_TREE_DELETE.search(t) or _PY_FILE_DELETE.search(t) for t in strings):
        return None  # a child process may run the delete a string names
    loop = any(isinstance(n, _AST_LOOP_NODES) for n in ast.walk(tree)) or any(
        _callee_name(c) in _AST_LOOP_CALLS for c in calls)

    def file_delete(c: ast.Call) -> bool:
        f = c.func
        if isinstance(f, ast.Name):
            return f.id in ("remove", "unlink")
        if isinstance(f, ast.Attribute):  # `.remove(` only on `os` (`os`, `shutil.os`): a list / set / table remove deletes no file
            v = f.value
            return f.attr == "unlink" or (f.attr == "remove" and (
                (isinstance(v, ast.Name) and v.id == "os") or (isinstance(v, ast.Attribute) and v.attr == "os")))
        return False

    hit = next((c for c in calls if _callee_name(c) in _AST_TREE_NAMES), None)
    if hit is None and loop:
        hit = next((c for c in calls if file_delete(c)), None)
    if hit is None:
        return None, []
    text = src.split("\n")[hit.lineno - 1]
    targets = []
    for c in calls:
        if _callee_name(c) in _AST_TARGET_CALLS and c.args and isinstance(c.args[0], ast.Constant):
            v = c.args[0].value
            if isinstance(v, bytes):
                v = v.decode("latin-1")
            if isinstance(v, str) and "\n" not in v:
                targets.append(v)
    return " ".join(text.split())[:MAX_TEXT], targets


def _python_delete(cmd, raw, full, cwd_norm, cwd, session_cwd):
    """Line 1 through Python (#835): a `python -c` / `python -` heredoc script that deletes a tree
    (`shutil.rmtree`, `os.rmdir` / `os.removedirs`, `.rmdir()`, a walk / glob loop of `remove` / `unlink`)
    is refused unless every literal path it names is in the shell form's exempt areas; a script that names
    no literal path (a variable) is refused. A single literal-file `os.remove` and a plain run pass."""

    if cmd not in _PY_HEADS:
        return False
    # python reads a heredoc as stdin only when ITS OWN segment holds `<<`; a heredoc that merely writes a
    # script (`cat > f <<EOF`) in another segment of the call is text, not code (#845)
    s = full if "<<" in raw else raw
    scan = _py_ast_scan(_python_source(raw, full))
    if scan is not None:
        # the script parsed: judge the calls it makes, not the text it holds (#35)
        if scan[0] is None:
            return False
        note = f" [matched: {scan[0]}]"
        targets = scan[1]
    else:
        tree = _PY_TREE_DELETE.search(s)
        loop = None if tree else (_PY_FILE_DELETE.search(s) if _PY_LOOP.search(s) else None)
        hit = tree or loop
        if not hit:
            return False
        a, b = s.rfind("\n", 0, hit.start()) + 1, s.find("\n", hit.end())
        note = f" [matched: {' '.join(s[a:b if b >= 0 else len(s)].split())[:MAX_TEXT]}]"
        targets = [(a or b).replace("\\\\", "\\") for a, b in _PY_TARGET.findall(s)]
    if not targets:
        return note
    own = None
    for t in targets:
        if "{" in t or "$" in t:
            return note
        n = _norm(t, cwd)
        if _throwaway(n, cwd_norm, t):
            continue
        if own is None:
            own = (_worktree_root(_norm(session_cwd)) if session_cwd else None) or ""
        if not _inside_own_worktree(n, own):
            return note
    return False


def _line2(cmd, args, redirs, cwd):
    targets = _write_targets(cmd, args, redirs)
    if cmd in _COPY_VERBS:  # a copy writes only its destination; its source is a read (#409)
        targets = list(redirs) + _copy_dest(args)
    for t in targets:
        if _machine_path(_norm(t, cwd)):
            return True
    if cmd in _WRITE_VERBS or cmd in ("sed", "perl"):
        if _MACHINE_TEXT_RE.search(" ".join(args).lower().replace("\\", "/")):
            return True
    if cmd == "reg" and args and args[0].lower() in ("add", "delete", "import", "restore"):
        return True
    if cmd in _REG_PS_VERBS and any(_REGISTRY_RE.match(a.lower()) for a in args):
        return True
    if cmd in _MACHINE_BARE:
        return True
    low = [a.lower() for a in args]
    if cmd in ("sc", "sc.exe") and low[:1] and low[0] in ("stop", "delete", "config"):
        return True
    if cmd == "schtasks" and any(a in ("/create", "/delete", "/change") for a in low):
        return True
    if cmd in ("net", "net1") and low[:1] and low[0] in ("user", "localgroup"):
        return True
    if cmd == "winget" and low[:1] == ["uninstall"]:
        return True
    if cmd == "msiexec" and any(a.startswith("/x") for a in low):
        return True
    return False


def _line3(cmd, args, sub, gargs, eff):
    if cmd != "git":
        return False
    if sub == "push":
        pos: list[str] = []
        for a in gargs:
            low = a.lower()
            if low in ("--force", "-f", "--delete", "-d", "--force-if-includes") or low.startswith("--force-with-lease"):
                return True
            if a.startswith("-") and not a.startswith("--") and len(a) > 1 and ("f" in a[1:] or "d" in a[1:]):
                return True
            if a.startswith("-"):
                continue
            pos.append(a)
        if pos and pos[0][:1] not in ("+", ":") and pos[0].lower() != "origin":
            return True
        if any(p[:1] in ("+", ":") for p in pos):
            return True
        return False
    if sub == "remote":
        return bool(gargs) and gargs[0].lower() in ("add", "set-url", "remove", "rm", "rename")
    if _own_worktree(eff):
        return False
    if sub == "reset":
        return "--hard" in gargs
    if sub in ("checkout", "restore"):
        return "." in gargs
    if sub == "branch":
        return "-D" in gargs or ("--delete" in gargs and ("--force" in gargs or "-f" in gargs))
    return sub in ("rebase", "filter-branch")


def _line4(cmd, args):
    if cmd != "gh":
        return False
    words = [a for a in args if not a.startswith("-")]
    group = words[0].lower() if words else ""
    verb = words[1].lower() if len(words) > 1 else ""
    if group == "repo":
        return verb in ("delete", "edit", "archive", "rename")
    if group in ("issue", "release"):
        return verb == "delete"
    if group == "secret":
        return verb in ("set", "delete", "remove")
    if group == "auth":
        return verb in ("logout", "refresh", "setup-git")
    if group == "label":
        return verb in ("delete", "edit")
    if group == "api":
        low = [a.lower() for a in args]
        deleting = False
        for j, a in enumerate(low):
            if re.match(r"^(?:-x|--method)=?delete$", a):
                deleting = True
            if a in ("-x", "--method") and j + 1 < len(low) and low[j + 1] == "delete":
                deleting = True
        # #482: DELETE .../issues/<n>/sub_issue only unlinks a sub-issue from its parent
        # #7: DELETE .../issues/<n>/dependencies/blocked_by/<id> only removes a blocked-by link
        unlink = any(
            re.search(r"(?:^|/)issues/\d+/sub_issue(?:\?.*)?$", a)
            or re.search(r"^(?:/?repos/[^/]+/[^/]+/)?issues/\d+/dependencies/blocked_by/\d+$", a)
            for a in low
        )
        return deleting and not unlink
    return False


def _line5(cmd, args):
    if cmd in _KILL:
        return True
    if cmd == "wmic":
        low = [a.lower() for a in args]
        return "process" in low and ("delete" in low or "terminate" in low)
    return False


def _line6(cmd, args, redirs, cwd, sub, gargs):
    if cmd == "gh" and [a.lower() for a in args if not a.startswith("-")][:2] == ["auth", "token"]:
        return True
    targets = _write_targets(cmd, args, redirs)
    if cmd in _COPY_VERBS:
        targets = list(redirs) + _copy_dest(args)
    for t in targets:
        if _harness_write(_norm(t, cwd)):
            return True
    words = gargs if cmd == "git" else args
    if (cmd in _READ_SECRET_VERBS) or (cmd == "git" and sub == "add"):
        for w in _paths(words):
            if _secret_path(_norm(w, cwd)):
                return True
    return False


_WIN_FLAGS = re.compile(r"create_new_console|detached_process|\b0x0*(?:8|10)\b", re.IGNORECASE)
_NOT_LAUNCHERS = {"grep", "rg", "egrep", "fgrep", "findstr", "select-string", "sls", "cat", "head", "tail",
                  "echo", "git", "sed", "type", "get-content", "gh"}
_HIDE_HINT = " -- start it hidden: scripts/launch_hidden.ps1 or CREATE_NO_WINDOW (docs/agents/machine.md)"


def _line7(cmd, args, raw):
    if cmd in _NOT_LAUNCHERS:
        return False
    if cmd == "cmd" and "/k" in [a.lower() for a in args]:
        return _HIDE_HINT
    low = [a.lower() for a in args]
    if cmd == "start":
        return _HIDE_HINT
    if cmd in ("start-process", "saps"):
        if "-nonewwindow" in low:
            return False
        for j, a in enumerate(low):
            if a.startswith("-windowstyle") and (a.endswith(":hidden") or (j + 1 < len(low) and low[j + 1] == "hidden")):
                return False
        return _HIDE_HINT
    if "creationflags" in raw.lower() and _WIN_FLAGS.search(raw):
        return _HIDE_HINT
    return False


def _reason(line: int, text: str) -> str:
    return f"permission guard, red line {line}: {' '.join(text.split())[:MAX_TEXT]}"


def decide(payload) -> str | None:
    """The reason to refuse this tool call, or None. Pure: no I/O beyond reading the environment
    and, for a `tmp_*` path, one `.git` existence check."""

    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool_name")
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return None
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) else ""
    if tool in ("Edit", "Write", "MultiEdit"):
        fp = ti.get("file_path")
        if not isinstance(fp, str) or not fp:
            return None
        path = _norm(fp, cwd)
        if _machine_path(path):
            return _reason(2, fp)
        if _harness_write(path):
            return _reason(6, fp)
        return None
    if tool not in ("Bash", "PowerShell"):
        return None
    command = ti.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    hit = _judge(_segments(_strip_heredocs(command)), command, cwd, cwd, False, 0, False)
    if hit is None:
        return None
    line, note, raw = hit
    return _reason(line, raw) + (note if isinstance(note, str) else "")


def _judge_wrapper(seg, name, inner, command, eff, cwd, wsl, depth, synth):
    """Open the wrapper `name` of segment `seg` and judge what it runs. `(line, note, text)` or None."""

    raw = seg.raw or command
    text = inner if isinstance(inner, str) else " ".join(inner)
    if _DELETE_WORD.search(raw if not synth else text):
        why = ""
        if depth >= _MAX_WRAP_DEPTH:
            why = f"nested deeper than {_MAX_WRAP_DEPTH} levels"
        elif (not synth and _unbalanced(raw)) or (isinstance(inner, str) and _unbalanced(inner)):
            why = "cannot be parsed (unbalanced quote)"
        if why:
            return 1, f" -- {name} wrapper {why} and holds a delete word", raw
    if depth >= _MAX_WRAP_DEPTH:
        return None
    if isinstance(inner, str):
        segs, synth_in = _segments(_strip_heredocs(inner)), False
    else:
        segs, synth_in = [_Seg(list(inner), [], text)], True
    # a wrapper of Windows (cmd, PowerShell) leaves wsl; wsl enters it; a shell keeps where it is
    inner_wsl = True if name == "wsl" else False if name in ("cmd", *_PS_HEADS) else wsl
    r = _judge(segs, text, eff, cwd, inner_wsl, depth + 1, synth_in)
    return None if r is None else (r[0], r[1], raw)


def _judge(segs, command, eff, cwd, wsl, depth, synth):
    """The first red line the segments cross, `(line, note, text of the segment)`, else None. `cwd` is the
    session's directory; `eff` the directory the commands run in; a wrapper's inner segments are judged here
    again, one level deeper (#99)."""

    for seg in segs:
        if wsl:
            seg = _Seg([_wsl_word(w) for w in seg.words], [_wsl_word(w) for w in seg.redirs], seg.raw)
        cmd, args = _head(seg.words)
        if cmd in ("cd", "chdir", "set-location", "sl", "pushd"):
            ps = _paths(args)
            if ps:
                eff = _norm(ps[0], eff)
            continue
        wrapped = _unwrap_cmd(cmd, args)
        if wrapped:
            r = _judge_wrapper(seg, wrapped[0], wrapped[1], command, eff, cwd, wsl, depth, synth)
            if r:
                return r
        sub, gargs, geff = ("", [], eff)
        if cmd == "git":
            sub, gargs, geff = _git_parts(args, eff)
        eff_norm = _norm(eff) if eff else ""
        for line, hit in (
            (2, lambda: _line2(cmd, args, seg.redirs, eff)),
            (1, lambda: _line1(cmd, args, seg.redirs, eff_norm, eff, sub, gargs, cwd)),
            (1, lambda: _python_delete(cmd, seg.raw, command, eff_norm, eff, cwd)),
            (3, lambda: _line3(cmd, args, sub, gargs, geff)),
            (4, lambda: _line4(cmd, args)),
            (5, lambda: _line5(cmd, args)),
            (6, lambda: _line6(cmd, args, seg.redirs, eff, sub, gargs)),
            (7, lambda: _line7(cmd, args, seg.raw)),
        ):
            h = hit()
            if h:
                return line, h, seg.raw or command
        if not cmd and seg.redirs:
            for t in seg.redirs:
                p = _norm(t, eff)
                if _machine_path(p):
                    return 2, True, seg.raw
                if _harness_write(p):
                    return 6, True, seg.raw
    return None


# ----------------------------------------------------------------------------------------------
# hook plumbing


def _log(payload: dict, reason: str) -> None:
    try:
        path = os.environ.get("PERMISSION_GUARD_LOG") or LOG_PATH
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        ti = payload.get("tool_input") or {}
        text = ti.get("command") or ti.get("file_path") or ""
        m = _REASON.search(reason)
        line = int(m.group(1)) if m else None
        # `hit` is the segment (or path) that matched, as the reason carries it; `text` is the
        # command's head, which for a chained command may not hold the hit at all (#363)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": payload.get("session_id"),
               "tool": payload.get("tool_name"), "line": line, "rule": RULES.get(line),
               "hit": m.group(2).split(" -- ")[0][:MAX_TEXT] if m else None,
               "text": " ".join(str(text).split())[:MAX_TEXT]}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def guard(payload) -> dict | None:
    """`decide` plus the log line: the deny JSON to print, or None. Never raises."""

    try:
        reason = decide(payload)
    except Exception:
        return None
    if reason is None:
        return None
    _log(payload, reason)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"{reason}; {REFUSAL_SUFFIX}"}}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        out = guard(payload)
        if out is not None:
            sys.stdout.write(json.dumps(out))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
