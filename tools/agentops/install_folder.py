"""Where the monitor's mod installs: the install root, the plugin id of the source folder, the repo root (#1096).

Core: `port` names a target's install folder from these, and a core file may not import `mods` (a `local_modules`
entry, ADR 0109 point 4), so the values live here and `mods` imports them back. The repo root is overridable by env
`AGENTOPS_MODS_REPO`, the install root by `AGENTOPS_MODS_DIR`. Stdlib only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from .core_check import MONITOR_LITERAL

#: The plugin id when the settings name no monitor source folder and no `monitor.plugin_id`.
DEFAULT_PLUGIN_ID = "monitor"
_PLUGIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def repo_root() -> Path:
    env = os.environ.get("AGENTOPS_MODS_REPO")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def install_root() -> Path:
    env = os.environ.get("AGENTOPS_MODS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "mods"


# -- #210: the journal of real installs and removals, one line each, beside the install root -----------------------

#: The journal's file name, in the folder that holds the install root (never inside it).
JOURNAL_NAME = "mods-journal.log"


def journal_path() -> Path:
    """`install_root().parent / "mods-journal.log"`: follows `install_root()`, so a test's temp root never touches the
    real journal."""
    return install_root().parent / JOURNAL_NAME


def journal_append(name: str, action: str) -> None:
    """Append one line `<local time ISO to seconds>\\t<name>\\t<action>\\t<pid>\\n` for a real install (`"install"`) or
    removal (`"remove"`) of the top-level folder `name`. A write failure (OSError) is swallowed: the install goes on."""
    line = f"{datetime.now().isoformat(timespec='seconds')}\t{name}\t{action}\t{os.getpid()}\n"
    try:
        with journal_path().open("ab") as fh:
            fh.write(line.encode("utf-8"))
    except OSError:
        pass


def journal_entries(path: Path | None = None) -> list[tuple[datetime, str, str]]:
    """The journal's parsed lines `(naive local time, name, action)` in file order; malformed lines are skipped; a
    missing or unreadable journal is []. `path` reads that file instead of `journal_path()` (the session guard reads
    the owner's real journal by path)."""
    try:
        text = (journal_path() if path is None else path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    out: list[tuple[datetime, str, str]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[1] or parts[2] not in ("install", "remove") or not parts[3].isdigit():
            continue
        try:
            when = datetime.fromisoformat(parts[0])
        except ValueError:
            continue
        if when.tzinfo is not None:
            continue
        out.append((when, parts[1], parts[2]))
    return out


def commands_dir() -> Path:
    """The user's Claude Code slash-command folder (`~/.claude/commands`), where the monitor's one command file goes (#1043, #173)."""
    return Path.home() / ".claude" / "commands"


def _monitor_settings(root: Path) -> dict:
    """The `monitor` object of `<root>/config/orchestration.json`; empty when the file or the object is absent."""
    path = root / "config" / "orchestration.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON in settings file: {path} ({exc})") from exc
    mon = data.get("monitor") if isinstance(data, dict) else None
    return mon if isinstance(mon, dict) else {}


def source_name(root: Path | None = None) -> str | None:
    """The monitor's source folder under `mods/`: `monitor.source` of the settings at `root` (default this repo).
    A target names none: with `monitor.plugin_id` set and its literal monitor folder under `mods/`, that folder is the
    source (#149). None otherwise, so no folder is special."""
    root = root or repo_root()
    mon = _monitor_settings(root)
    src = mon.get("source")
    if src is None:
        if mon.get("plugin_id") is not None and (root / "mods" / MONITOR_LITERAL).is_dir():
            return MONITOR_LITERAL
        return None
    if not isinstance(src, str) or not _PLUGIN_ID.fullmatch(src):
        raise RuntimeError(f"monitor.source must be a plain folder name, got {src!r}")
    return src


def plugin_id(root: Path | None = None, default: str | None = None) -> str:
    """The plugin id the monitor's source folder installs under (#885): `monitor.plugin_id` of the settings at
    `root` (default this repo); else `default`; else the source folder's own name; else DEFAULT_PLUGIN_ID."""
    root = root or repo_root()
    pid = _monitor_settings(root).get("plugin_id")
    if pid is None:
        return default or source_name(root) or DEFAULT_PLUGIN_ID
    if not isinstance(pid, str) or not _PLUGIN_ID.fullmatch(pid):
        raise RuntimeError(f"monitor.plugin_id must be a plain folder name, got {pid!r} in {root / 'config'}")
    if pid != source_name(root) and (root / "mods" / pid).is_dir():
        raise RuntimeError(f"monitor.plugin_id {pid!r} is the name of another mod folder in {root / 'mods'}")
    return pid


def install_name(name: str) -> str:
    """The install folder name of mod `name`: the plugin id for the monitor's source folder, the mod's own name otherwise."""
    src = source_name()
    return plugin_id() if src is not None and name == src else name


# -- #11: a monitor install never takes a folder or a command that another REPO holds ----------------------------

_REPO_CONST = re.compile(r"^\s*export\s+const\s+REPO\s*=\s*['\"]([^'\"]*)['\"]", re.MULTILINE)


class InstallClaimError(Exception):
    """An install folder already belongs to another REPO (#11): the install writes nothing. The shared command file
    `monitor.md` is never a claim (#173)."""


def _same_repo(a: str, b: str) -> bool:
    def norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(p.strip()))

    return norm(a) == norm(b)


def _installed_repo(folder: Path) -> str | None:
    """The REPO constant of the monitor mod installed at `folder` (its `hooks/project.ts`); None when absent."""
    try:
        match = _REPO_CONST.search((folder / "hooks" / "project.ts").read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def refuse_other_repo_claim(folder: Path, repo: str | None, hint: str = "") -> None:
    """Raise InstallClaimError, writing nothing, when `folder` holds a mod of another REPO than `repo`. No `repo`, or
    nothing installed there, is no claim and passes. The command file is never a claim (#173: one `monitor.md` for
    every repo). `hint` is appended to the refusal's message."""
    if not repo:
        return
    held = _installed_repo(folder)
    if held is not None and not _same_repo(held, repo):
        raise InstallClaimError(f"refused: the monitor folder {folder} is installed for another REPO ({held}), "
                                f"not {repo}; nothing written{hint}")


# -- #45: the install itself: one copy of the code for `mods install` and for the port ------------------------------

#: The manifest the host reads the plugin id from (its top-level `name`).
MANIFEST = ".claude-plugin/plugin.json"

#: Run-time files: not source, never compared, copied or removed. A path with a directory part
#: named in "dirs", or a file name matching one of "files".
RUNTIME_PATTERNS = {
    "dirs": [".cache", "__pycache__", ".pytest_cache", "node_modules"],
    "files": ["*.bak", "*.log", "*.jsonl", "*.before-*"],
}

#: The hook files that carry the plugin id as a quoted string under another install name (#1043).
HOOK_SUFFIXES = (".ts", ".tsx", ".json")


def sync_shared(name: str, root: Path | None = None) -> None:
    """Copy mods/shared/*.ts into <mod>/hooks/: a plugin imports only inside its own folder (#798)."""
    root = root or repo_root()
    src = root / "mods" / "shared"
    dst = root / "mods" / name / "hooks"
    if not src.is_dir() or not dst.is_dir():
        return
    for f in src.glob("*.ts"):
        data = f.read_bytes()
        if not (dst / f.name).is_file() or (dst / f.name).read_bytes() != data:
            (dst / f.name).write_bytes(data)


def is_runtime(rel: Path) -> bool:
    # the repository's own ignore file at a mod's root is not part of the installed folder
    if rel.as_posix() == ".gitignore":
        return True
    if any(part in RUNTIME_PATTERNS["dirs"] for part in rel.parts[:-1]):
        return True
    return any(fnmatch.fnmatch(rel.name, pat) for pat in RUNTIME_PATTERNS["files"])


def installed_bytes(name: str, iname: str, rel: str, data: bytes) -> bytes:
    """The bytes the install holds for repository file `rel` (#885, #1043). Under another plugin id the manifest's
    top-level `name` changes (the host keys a plugin by it), and every quoted source name in the hook files under
    `hooks/` changes to the install name (the host's state rejects writes of atoms another plugin id owns); every
    other byte is the repository's."""
    if iname == name:
        return data
    if rel == MANIFEST:
        pat = re.compile(r'("name"\s*:\s*)"' + re.escape(name) + '"')
        text, n = pat.subn(lambda m: m.group(1) + json.dumps(iname), data.decode("utf-8"), count=1)
        if n != 1 or json.loads(text).get("name") != iname:
            raise RuntimeError(f"{MANIFEST} of {name} has no top-level name {name!r} to rename to {iname!r}")
        return text.encode("utf-8")
    if rel.startswith("hooks/") and rel.endswith(HOOK_SUFFIXES):
        quoted = re.compile(b"(['\"])" + re.escape(name.encode("utf-8")) + b"\\1")
        return quoted.sub(lambda m: m.group(1) + iname.encode("utf-8") + m.group(1), data)
    return data


def source_bytes(name: str, iname: str, rel: str, data: bytes) -> bytes:
    """The inverse of `installed_bytes` (#167): the repository bytes an install file `rel` was written from, i.e. the
    quoted install name in `hooks/` and the manifest's top-level `name` read back as the source name. The install's own
    output is in no commit as it stands, so a check for "is this some committed version" looks up these bytes too."""
    if iname == name:
        return data
    if rel == MANIFEST:
        return re.sub(rb'("name"\s*:\s*)"' + re.escape(iname.encode("utf-8")) + b'"',
                      lambda m: m.group(1) + json.dumps(name).encode("utf-8"), data, count=1)
    if rel.startswith("hooks/") and rel.endswith(HOOK_SUFFIXES):
        quoted = re.compile(b"(['\"])" + re.escape(iname.encode("utf-8")) + b"\\1")
        return quoted.sub(lambda m: m.group(1) + name.encode("utf-8") + m.group(1), data)
    return data


def list_files(base: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not base.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in RUNTIME_PATTERNS["dirs"]]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.relative_to(base)
            if not is_runtime(rel):
                out[rel.as_posix()] = p
    return out


def diff_files(repo_dir: Path, inst_dir: Path, name: str, iname: str) -> tuple[int, list[str]]:
    """(files compared, sorted differing relative paths) between the repository copy and the install."""
    repo, inst = list_files(repo_dir), list_files(inst_dir)
    names = set(repo) | set(inst)
    diff = [n for n in names
            if n not in repo or n not in inst
            or installed_bytes(name, iname, n, repo[n].read_bytes()) != inst[n].read_bytes()]
    return len(names), sorted(diff)


def copy_files(repo_dir: Path, inst_dir: Path, name: str, iname: str, diff: list[str]) -> tuple[int, int, Path]:
    """Bring the install to the repository copy for the paths in `diff`: a replaced or removed install file is first
    saved under the install's `.cache/backup-<time>`. Returns (copied, removed, the backup folder)."""
    cache = inst_dir / "bin" / ".cache" if (inst_dir / "bin").is_dir() else inst_dir / ".cache"
    backup = cache / time.strftime("backup-%Y%m%d-%H%M%S")
    copied = removed = 0
    for rel in diff:
        src, dst = repo_dir / rel, inst_dir / rel
        if dst.is_file():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup / rel)
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(installed_bytes(name, iname, rel, src.read_bytes()))
            copied += 1
        elif dst.is_file():
            dst.unlink()
            removed += 1
    return copied, removed, backup


#: The one slash command every monitor shares (#173): `<commands>/monitor.md`, owned by no repo, so the name is a
#: constant of the core, not a setting of a repo's `project.ts`.
COMMAND_NAME = "monitor"
#: The core stub the install writes `<commands>/monitor.md` from: no repo, no mod folder, no placeholder (#173).
COMMAND_STUB = Path(__file__).resolve().parents[2] / "config" / "core_templates" / "monitor_command.md.stub"
#: The fixed lines of the per-repo command file the old stub wrote (#1043, #11, filled with a title, a mod folder and a
#: command name). A file of this shape is an old monitor's file (`monitor.md` of the old shape included); the one token
#: before "mod's" is the old mod's name when the old stub had one. Matched on "\n" line ends.
OLD_COMMAND_SHAPE = re.compile(
    r"---\n"
    r"description: Open the [^\n]* monitor pane \(machine, runs, tickets\)\n"
    r"disable-model-invocation: true\n"
    r"---\n"
    r"Open the [^\n]* monitor pane\. \(Answered by the (?:[A-Za-z0-9_.-]+ )?mod's command\.run hook, [^\n]*; "
    r"this file only lists /[^\n]* in the desktop composer, which reads its command list at startup\.\)\n?")


def const_value(text: str, name: str) -> str | None:
    """The quoted value of `export const <name> = '...'` in a project.ts, or None (#11)."""
    m = re.search(rf"""^\s*export\s+const\s+{name}\s*=\s*['"]([^'"]*)['"]""", text, re.M)
    return m.group(1) if m else None


def _is_old_command(text: str) -> bool:
    return OLD_COMMAND_SHAPE.fullmatch(text.replace("\r\n", "\n")) is not None


def _remove_old_commands(commands_dir: Path) -> list[str]:
    """Remove each `<commands>/<name>.md` other than `monitor.md` whose text is the old stub's shape (#173); any other
    file is the owner's and stays. Returns the removed names."""
    removed = []
    for f in sorted(commands_dir.glob("*.md")):
        if f.name == f"{COMMAND_NAME}.md" or not f.is_file():
            continue
        if _is_old_command(f.read_bytes().decode("utf-8", errors="replace")):
            f.unlink()
            removed.append(f.name)
    return removed


def command_step(repo_dir: Path, commands_dir: Path) -> str | None:
    """The slash-command step (#173): a monitor mod (its `hooks/project.ts` exists) gets `<commands>/monitor.md`, written
    from the core stub when absent. A present file that already holds the stub's text is left alone; one of the old
    per-repo shape is replaced, its old text saved beside it as `monitor.md.before-<time>`; any other text is the owner's
    and is left alone. The old per-repo command files (`<name>.md`, ...) of the old shape are removed. Never a
    refusal. Returns the line to print, or None when nothing was written, replaced or removed."""
    if not (repo_dir / "hooks" / "project.ts").is_file():
        return None
    notes = []
    removed = _remove_old_commands(commands_dir)
    if removed:
        notes.append(f"removed the old command files {', '.join(removed)}")
    body = COMMAND_STUB.read_text(encoding="utf-8")
    target = commands_dir / f"{COMMAND_NAME}.md"
    if not target.is_file():
        commands_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body.encode("utf-8"))
        notes.append(f"command file written: {target}")
    else:
        current = target.read_bytes().decode("utf-8", errors="replace")
        if current.replace("\r\n", "\n") == body:
            pass
        elif _is_old_command(current):
            backup = commands_dir / f"{COMMAND_NAME}.md.before-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(target, backup)
            target.write_bytes(body.encode("utf-8"))
            notes.append(f"command file replaced: {target} (its old text saved as {backup})")
        else:
            notes.append(f"command file kept: {target} holds other text than the core's (left alone)")
    return "; ".join(notes) or None


# -- #45: registering the folder in the owner's `env.CLAUDE_CODE_PLUGIN_DIRS` (a JSON edit of that one key) --------

PLUGIN_DIRS_KEY = "CLAUDE_CODE_PLUGIN_DIRS"


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n﻿":
        i += 1
    return i


def _string_end(text: str, i: int) -> int:
    """Index after the string that opens at `i`."""
    i += 1
    while text[i] != '"':
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _value_end(text: str, i: int) -> int:
    """Index after the JSON value that starts at `i`."""
    if text[i] == '"':
        return _string_end(text, i)
    if text[i] in "{[":
        depth = 0
        while True:
            ch = text[i]
            if ch == '"':
                i = _string_end(text, i)
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
    while i < len(text) and text[i] not in ",}] \t\r\n":
        i += 1
    return i


def _members(text: str, open_at: int) -> tuple[list[tuple[str, int, int]], int]:
    """([(key, value start, value end)], index of the closing brace) of the object that opens at `open_at`."""
    out: list[tuple[str, int, int]] = []
    i = _skip_ws(text, open_at + 1)
    while text[i] != "}":
        key_end = _string_end(text, i)
        key = json.loads(text[i:key_end])
        i = _skip_ws(text, key_end)
        i = _skip_ws(text, i + 1)  # past the colon
        end = _value_end(text, i)
        out.append((key, i, end))
        i = _skip_ws(text, end)
        if text[i] == ",":
            i = _skip_ws(text, i + 1)
    return out, i


def _add_member(text: str, members: list[tuple[str, int, int]], open_at: int, close_at: int, key: str,
                value_json: str, level: int, unit: str) -> str:
    """`text` with `"key": value_json` appended as the last member of the object; only the added lines change.
    A one-line object gets the member on the same line."""
    nl = "\r\n" if "\r\n" in text else "\n"
    member = f"{json.dumps(key)}: {value_json}"
    multiline = "\n" in text[open_at:close_at]
    if not multiline:
        if not members:
            return text[:open_at + 1] + member + text[close_at:]
        return text[:members[-1][2]] + ", " + member + text[members[-1][2]:]
    indent = unit * level
    if members:
        line_start = text.rfind("\n", 0, members[0][1]) + 1
        indent = re.match(r"[ \t]*", text[line_start:]).group(0) or indent
    close_indent = indent[:max(len(indent) - len(unit), 0)]
    if not members:
        return text[:open_at + 1] + nl + indent + member + nl + close_indent + text[close_at:]
    return text[:members[-1][2]] + "," + nl + indent + member + text[members[-1][2]:]


def with_plugin_dir(text: str, folder: str) -> str:
    """`text` (the owner's settings file) with `folder` appended to `env.CLAUDE_CODE_PLUGIN_DIRS`; `text` itself when it
    is listed already. Every other byte is kept: only the one value changes, or one member is added."""
    norm = lambda d: os.path.normcase(os.path.normpath(d.strip()))  # noqa: E731
    json.loads(text.lstrip("﻿"))  # a file that does not parse is not touched
    root = _skip_ws(text, 0)
    if text[root] != "{":
        raise ValueError("the settings file is not a JSON object")
    unit = "\t" if re.search(r"^\t", text, re.M) else "  "
    nl = "\r\n" if "\r\n" in text else "\n"
    top, top_close = _members(text, root)
    folder_json = json.dumps(folder, ensure_ascii=False)
    env = next(((k, s, e) for k, s, e in reversed(top) if k == "env"), None)
    if env is None:
        key = json.dumps(PLUGIN_DIRS_KEY)
        if "\n" in text[root:top_close]:
            value = "{" + nl + unit * 2 + key + ": " + folder_json + nl + unit + "}"
        else:
            value = "{" + key + ": " + folder_json + "}"
        return _add_member(text, top, root, top_close, "env", value, 1, unit)
    if text[env[1]] != "{":
        raise ValueError('"env" of the settings file is not an object')
    inner, inner_close = _members(text, env[1])
    current = next(((k, s, e) for k, s, e in reversed(inner) if k == PLUGIN_DIRS_KEY), None)
    if current is None:
        return _add_member(text, inner, env[1], inner_close, PLUGIN_DIRS_KEY, folder_json, 2, unit)
    if text[current[1]] != '"':
        raise ValueError(f'"env.{PLUGIN_DIRS_KEY}" of the settings file is not a string')
    listed = [d for d in json.loads(text[current[1]:current[2]]).split(";") if d.strip()]
    if norm(folder) in {norm(d) for d in listed}:
        return text
    return text[:current[1]] + json.dumps(";".join([*listed, folder]), ensure_ascii=False) + text[current[2]:]


def plan_plugin_dir(folder: Path, settings_path: Path) -> str:
    """#52: the text the settings file gets with `folder` in `env.CLAUDE_CODE_PLUGIN_DIRS` (the file's own text when
    the folder is listed already; a new file when absent). Reads only: a settings error raises ValueError here,
    before any write, and nothing is written."""
    path = Path(settings_path)
    if not path.is_file():
        return json.dumps({"env": {PLUGIN_DIRS_KEY: str(folder)}}, indent=2) + "\n"
    return with_plugin_dir(path.read_bytes().decode("utf-8"), str(folder))


def write_settings(settings_path: Path, text: str) -> bool:
    """#52: write `text` as the settings file, atomically: a temporary file beside it, swapped in by `os.replace`; the
    temporary file is removed whenever the swap did not happen. True when the file changed; False when it holds `text`
    already (0 bytes written). No process is started."""
    path = Path(settings_path)
    if path.is_file() and path.read_bytes().decode("utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-plugin-dirs")
    try:
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True
