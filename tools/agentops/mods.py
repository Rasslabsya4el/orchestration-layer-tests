"""`mods status|install|remove|test`: the repository copy of the Claude Code mods (#789), and the owner's loaded-mods
line (#46): `install` and `remove` write env `CLAUDE_CODE_PLUGIN_DIRS` of the settings file, one key and nothing else.

Stdlib only.
The repository copy is ``<repo>/mods/<name>/``; the install root is ``~/.claude/mods``
unless env ``AGENTOPS_MODS_DIR``; the repo root is overridable by ``AGENTOPS_MODS_REPO``.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import install_folder as _install_folder
from .install_folder import (  # noqa: F401
    COMMAND_STUB, HOOK_SUFFIXES, MANIFEST, RUNTIME_PATTERNS, InstallClaimError,
    command_step, const_value as _const, copy_files, diff_files, install_name, install_root, installed_bytes as _installed,
    is_runtime as _is_runtime, list_files as _files, plugin_id, refuse_other_repo_claim, repo_root, source_bytes as _source,
    source_name, sync_shared)
from .project_exports import missing_names

#: The monitor's source folder under `mods/`, named by `monitor.source` of the settings file (the name lives only there).
PLUGIN_SOURCE = source_name() or ""

#: The mods of this checkout: every folder of `mods/` but the shared sources (read, never listed by name, #830).
MOD_NAMES = tuple(sorted(p.name for p in (Path(__file__).resolve().parents[2] / "mods").glob("*")
                         if p.is_dir() and p.name != "shared"))

#: Files the Claude app rewrites itself inside an installed mod (#828, #167): the app regenerates the type
#: declarations under `.claude-plugin/types/` (each folder's own `.gitignore` is `*`; mtimes move with app refreshes,
#: not with hand edits). Every file under that folder is app-generated, whatever its folder is named: it is never
#: compared as differing, never copied over and never a refusal.
APP_GENERATED_DIR = ".claude-plugin/types/"


def _app_generated(rel: str) -> bool:
    return rel.startswith(APP_GENERATED_DIR)


def _when(ts: float | None) -> str:
    """A time a human reads (local ISO, #167); "none" when the path has no commit."""
    return "none" if ts is None else datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _label(name: str, iname: str) -> str:
    return name if iname == name else f"{name} (installed as {iname})"


def compare(name: str) -> tuple[int, list[str]]:
    """(files compared, sorted differing relative paths) between repository copy and install; app-generated files
    (#167) are neither listed as differing nor copied, removed or checked."""
    total, diff = diff_files(repo_root() / "mods" / name, install_root() / install_name(name), name, install_name(name))
    return total, [d for d in diff if not _app_generated(d)]


def _tracked_path(rel: str, name: str) -> str:
    """The repo path whose history holds an install file (#921): `sync_shared` copies `mods/shared/<f>.ts` into
    `<mod>/hooks/` (gitignored there), so such a file is looked up under its source, anything else under the mod."""
    parts = rel.split("/")
    if len(parts) == 2 and parts[0] == "hooks" and (repo_root() / "mods" / "shared" / parts[1]).is_file():
        return f"mods/shared/{parts[1]}"
    return f"mods/{name}/{rel}"


def _last_commit_time(rel: str, name: str) -> int | None:
    r = subprocess.run(["git", "log", "-1", "--format=%ct", "--", _tracked_path(rel, name)],
                       cwd=repo_root(), capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    s = r.stdout.strip()
    return int(s) if r.returncode == 0 and s else None


def _blob_hash(data: bytes) -> str | None:
    """The blob id of `data` as stored, no line-ending filter (a plain hash-object gives another id on CRLF)."""
    r = subprocess.run(["git", "hash-object", "--no-filters", "--stdin"], cwd=repo_root(), input=data,
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = r.stdout.decode().strip()
    return out if r.returncode == 0 and out else None


def _content_committed(rel: str, name: str, data: bytes) -> bool:
    """#921: the bytes (as they are, or with CRLF read as LF) are some committed version of this path.
    #936: looked up in every commit of every branch under ANY path: a blob id is its content, and the path the
    checked-out commit gives a file (`_tracked_path`) is not the path another build kept it under (moved to
    `mods/shared/`, or not yet there), so a path limit refused a build held byte for byte by a branch."""
    # #167: under another plugin id the install holds the installer's own output (the quoted source name rewritten),
    # which is in no commit as it stands: its bytes read back as the source name are looked up too
    back = _source(name, install_name(name), rel, data)
    for variant in dict.fromkeys((data, data.replace(b"\r\n", b"\n"), back, back.replace(b"\r\n", b"\n"))):
        blob = _blob_hash(variant)
        if blob is None:
            continue
        r = subprocess.run(["git", "log", "--all", "-1", "--format=%H", f"--find-object={blob}"],
                           cwd=repo_root(), capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and r.stdout.strip():
            return True
    return False


def run_status(names: list[str]) -> int:
    bad = 0
    for name in names:
        sync_shared(name)
        total, diff = compare(name)
        print(f"{_label(name, install_name(name))}: {total} files compared, {len(diff)} differ")
        for d in diff:
            print(f"  {d}")
        bad += len(diff)
    return 1 if bad else 0


# -- the owner's loaded-mods line: env CLAUDE_CODE_PLUGIN_DIRS of the settings file (#46) -------------------------

#: The settings key that lists the mod folders Claude Code loads: the only key `mods` writes.
PLUGIN_DIRS_KEY = "CLAUDE_CODE_PLUGIN_DIRS"
#: The key's JSON string literal in the raw text; group 1 is its escaped body, between the quotes.
_PLUGIN_DIRS_LITERAL = re.compile(r'"CLAUDE_CODE_PLUGIN_DIRS"\s*:\s*"((?:[^"\\]|\\.)*)"')
_BOM = b"\xef\xbb\xbf"


class SettingsError(Exception):
    """The settings file cannot be read as a JSON object, or the key cannot be written in place: nothing is written."""


class SettingsMissing(SettingsError):
    """There is no settings file at all: nothing to edit, nothing created; the command goes on with rc 0."""


@dataclass
class PluginDirsEdit:
    """One planned write of the settings file: the file as it was (the backup), its text and object, the key's value
    now and the value to write. `span` is where the key's literal sits in the text, None when the key is absent."""
    path: Path
    raw: bytes
    text: str
    data: dict
    old: str
    new: str
    span: tuple[int, int] | None


#: (#94) The throwaway settings folder of one pytest process and the test id it was last reset for.
_THROWAWAY: dict = {}


def _throwaway_settings(test_id: str) -> Path:
    """A settings file under the system temp folder, `{}` afresh for each test (a test that forgot `settings_path`
    writes here, never to ~/.claude); the folder goes away with the process."""
    if "dir" not in _THROWAWAY:
        folder = Path(tempfile.mkdtemp(prefix="agentops-pytest-settings-"))
        atexit.register(shutil.rmtree, folder, ignore_errors=True)
        _THROWAWAY["dir"] = folder
    path = _THROWAWAY["dir"] / "settings.json"
    if _THROWAWAY.get("test") != test_id or not path.exists():
        path.write_text("{}", encoding="utf-8")
        _THROWAWAY["test"] = test_id
    return path


def default_settings_path() -> Path:
    """Env `AGENTOPS_SETTINGS_PATH` when set; else, inside a pytest run (`PYTEST_CURRENT_TEST` set), a throwaway file
    under the temp folder (#94: no test reaches the owner's real file); else `~/.claude/settings.json`."""
    env = os.environ.get("AGENTOPS_SETTINGS_PATH")
    if env:
        return Path(env)
    test_id = os.environ.get("PYTEST_CURRENT_TEST")
    if test_id:
        return _throwaway_settings(test_id.rsplit(" (", 1)[0])
    return Path.home() / ".claude" / "settings.json"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.strip()))


def plan_plugin_dirs(settings_path: Path | None, folder: Path, remove: bool = False) -> PluginDirsEdit | None:
    """The write that puts `folder` into env `CLAUDE_CODE_PLUGIN_DIRS` (or, with `remove`, takes it out), or None when
    the value already is so: no write, no backup. Reads only. Raises SettingsError when the file is unreadable, not a
    JSON object, or its "env" is not an object, or when the key's literal cannot be found exactly once."""
    path = Path(settings_path) if settings_path else default_settings_path()
    try:
        raw = path.read_bytes()
        text = (raw[len(_BOM):] if raw.startswith(_BOM) else raw).decode("utf-8")
        data = json.loads(text)
    except FileNotFoundError as exc:
        raise SettingsMissing(f"{PLUGIN_DIRS_KEY}: no settings file at {path}; nothing written") from exc
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{PLUGIN_DIRS_KEY}: settings file unreadable or not JSON ({path}: {exc}); "
                            f"nothing written") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{PLUGIN_DIRS_KEY}: settings file is not a JSON object ({path}); nothing written")
    if "env" in data and not isinstance(data["env"], dict):
        raise SettingsError(f'{PLUGIN_DIRS_KEY}: "env" in {path} is not an object; nothing written')
    current = (data.get("env") or {}).get(PLUGIN_DIRS_KEY)
    old = current if isinstance(current, str) else ""
    parts = [p for p in old.split(";") if p.strip()]
    target = _norm(str(folder))
    if remove:
        kept = [p for p in parts if _norm(p) != target]
        if len(kept) == len(parts):
            return None
        new = ";".join(kept)
    else:
        if any(_norm(p) == target for p in parts):
            return None
        new = ";".join([*parts, str(folder)])
    span = None
    if isinstance(current, str):
        found = list(_PLUGIN_DIRS_LITERAL.finditer(text))
        if len(found) != 1 or json.loads(f'"{found[0].group(1)}"') != current:
            raise SettingsError(f"{PLUGIN_DIRS_KEY}: the key is written {len(found)} times in {path}, not once; "
                                f"nothing written")
        span = found[0].span(1)
    return PluginDirsEdit(path, raw, text, data, old, new, span)


def _json_rewrite(edit: PluginDirsEdit) -> str:
    """The key is absent: the whole file re-dumped with its own indent (the first indented line's; 2 by default),
    key order kept, its newline style kept."""
    edit.data.setdefault("env", {})[PLUGIN_DIRS_KEY] = edit.new
    indent = "  "
    for line in edit.text.splitlines():
        m = re.match(r"[ \t]+(?=\S)", line)
        if m:
            indent = m.group(0)
            break
    crlf = "\r\n" in edit.text
    body = json.dumps(edit.data, indent=indent, ensure_ascii=False)
    if crlf:
        body = body.replace("\n", "\r\n")
    if edit.text.endswith("\n"):
        body += "\r\n" if crlf else "\n"
    return body


def _backup_settings(raw: bytes) -> Path:
    """A copy of the settings file as it was: var/agentops/settings_backup/settings-<YYYYmmdd-HHMMSS>.json, with a
    counter suffix when that name is taken (a backup is never overwritten)."""
    folder = repo_root() / "var" / "agentops" / "settings_backup"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("settings-%Y%m%d-%H%M%S")
    dst, n = folder / f"{stamp}.json", 1
    while dst.exists():
        dst, n = folder / f"{stamp}-{n}.json", n + 1
    dst.write_bytes(raw)
    return dst


def apply_plugin_dirs(edit: PluginDirsEdit) -> str:
    """Back up the settings file, then write the one key (its literal in place, or the JSON rewrite when the key is
    absent). Returns the line `CLAUDE_CODE_PLUGIN_DIRS: <old> -> <new>`."""
    _backup_settings(edit.raw)
    if edit.span is None:
        body = _json_rewrite(edit)
    else:
        start, end = edit.span
        body = edit.text[:start] + json.dumps(edit.new, ensure_ascii=False)[1:-1] + edit.text[end:]
    edit.path.write_bytes((_BOM if edit.raw.startswith(_BOM) else b"") + body.encode("utf-8"))
    return f"{PLUGIN_DIRS_KEY}: {edit.old} -> {edit.new}"


def _claim_refusal(repo_dir: Path, inst_dir: Path) -> str | None:
    """#11: the refusal line when `inst_dir` belongs to another REPO, read from the mod's own `hooks/project.ts`; None
    when there is no claim. The command file is never a claim (#173). Runs before any write of `run_install`."""
    project = repo_dir / "hooks" / "project.ts"
    if not project.is_file():
        return None
    try:
        refuse_other_repo_claim(inst_dir, _const(project.read_text(encoding="utf-8"), "REPO"))
    except InstallClaimError as exc:
        return str(exc)
    return None


def run_install(name: str, force: bool = False, settings_path: Path | None = None,
                commands_dir: Path | None = None) -> int:
    repo_dir = repo_root() / "mods" / name
    iname = install_name(name)
    inst_dir = install_root() / iname
    label = _label(name, iname)
    commands = commands_dir or _install_folder.commands_dir()
    refusal = _claim_refusal(repo_dir, inst_dir)
    if refusal:
        print(f"{label}: {refusal}")
        return 1
    # #172: hooks that import a name their project.ts does not export would fail to load: nothing is written, --force too
    if (repo_dir / "hooks").is_dir():
        missing = missing_names(repo_dir / "hooks")
        if missing:
            print(f"{label}: refused, hooks/project.ts lacks {', '.join(missing)} that hooks import from ./project")
            return 1
    # the settings edit is planned before any mod file is touched: unreadable settings write nothing at all (#46)
    try:
        edit = plan_plugin_dirs(settings_path, inst_dir)
    except SettingsMissing as exc:
        print(str(exc))
        edit = None
    except SettingsError as exc:
        print(str(exc))
        return 1
    sync_shared(name)
    if not repo_dir.is_dir():
        print(f"{name}: no repository copy at {repo_dir}")
        return 1
    _, diff = compare(name)
    if not force:
        newer = []
        for rel in diff:
            f = inst_dir / rel
            if not f.is_file():
                continue
            ct = _last_commit_time(rel, name)
            mt = f.stat().st_mtime
            # #921: a path with no commit is no excuse (ct None blocks as much as a newer file time does)
            if (ct is None or mt > ct) and not _content_committed(rel, name, f.read_bytes()):
                newer.append(f"{rel} (install {_when(mt)}, last commit {_when(ct)})")
        if newer:
            print(f"{label}: refused, newer in the install than the repository's last commit "
                  f"and in no commit (use --force): {', '.join(newer)}")
            return 1
    copied, removed, backup = copy_files(repo_dir, inst_dir, name, iname, diff)
    _install_folder.journal_append(iname, "install")  # #210: a real install is recorded beside the root
    print(f"{label}: {copied} copied, {removed} removed, backup {backup if diff else '(none needed)'}")
    try:
        line = apply_plugin_dirs(edit) if edit else None
    except OSError as exc:
        print(f"{PLUGIN_DIRS_KEY}: not written: {exc}")
        return 1
    if line:
        print(line)
        print("restart Claude Code once")
    # the slash-command step (#1043, #173): one line when command_step wrote, replaced, kept or removed a command file;
    # none when the shared monitor.md already holds the stub
    step = command_step(repo_dir, commands)
    if step:
        print(f"{label}: {step}")
    return 0


def run_remove(name: str, settings_path: Path | None = None) -> int:
    """`mods remove <name>` (#46): the install folder goes after its usage.jsonl and events.jsonl are kept under
    var/agentops, and the folder's path leaves env `CLAUDE_CODE_PLUGIN_DIRS`. Not installed: one line, the line cleaned."""
    if not plain_folder_name(name):
        print(f"remove: refused, not a plain folder name: {name!r}")
        return 1
    folder = install_root() / name
    try:
        edit = plan_plugin_dirs(settings_path, folder, remove=True)
    except SettingsMissing as exc:
        print(str(exc))
        edit = None
    except SettingsError as exc:
        print(str(exc))
        return 1
    if folder.is_dir():
        try:
            for kind in ("usage", "events"):
                src = folder / f"{kind}.jsonl"
                if src.is_file():
                    _merge_rows(src, repo_root() / "var" / "agentops" / f"{name}-{kind}-final.jsonl")
            shutil.rmtree(folder)
        except OSError as exc:
            print(f"{name}: refused, {exc}; the settings line is unchanged")
            return 1
        _install_folder.journal_append(name, "remove")  # #210: a real removal is recorded beside the root
        print(f"{name}: removed {folder}")
    else:
        print(f"{name}: not installed")
    if edit is not None:
        try:
            line = apply_plugin_dirs(edit)
        except OSError as exc:
            print(f"{PLUGIN_DIRS_KEY}: not written ({edit.path}: {exc})")
            return 1
        print(line)
        print("restart Claude Code once")
    return 0


def plain_folder_name(name: str) -> bool:
    """A name that is one plain folder under the install root: no separator, no drive colon, no "..", not "." or empty."""
    return bool(name) and name not in (".", "..") and ".." not in name and not any(c in name for c in "/\\:")


def _merge_rows(src: Path, dst: Path) -> None:
    """Copy `src` to `dst`; when `dst` exists, append the rows it lacks, never overwrite it. Rows compare line-exact
    (line ending aside), a repeated row counted as often as it occurs in `src`."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.is_file():
        dst.write_bytes(src.read_bytes())
        return
    old = dst.read_bytes()
    have = Counter(line.rstrip(b"\r\n") for line in old.splitlines(keepends=True))
    add = []
    for row in src.read_bytes().splitlines(keepends=True):
        key = row.rstrip(b"\r\n")
        if have[key] > 0:
            have[key] -= 1
        else:
            add.append(row if row.endswith(b"\n") else row + b"\n")
    if add:
        dst.write_bytes((old if not old or old.endswith(b"\n") else old + b"\n") + b"".join(add))


# -- test -------------------------------------------------------------------------------------

def _default_runner(cmd: list[str], cwd: Path) -> tuple[int, str]:
    # stderr joins stdout in the order the runner wrote it (#954): the last line is then the runner's last line
    r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, r.stdout or ""


def _claude() -> str:
    """``claude`` on PATH, else ``CLAUDE_CODE_EXECPATH``, else the installed exe the dispatcher finds (#176: a
    merge started detached has neither, and every mod went red with ``FileNotFoundError``), else the bare name."""
    found = shutil.which("claude") or os.environ.get("CLAUDE_CODE_EXECPATH")
    if found:
        return found
    from .dispatch import find_claude_exe  # lazy: dispatch is a large module, loaded only when this fallback is reached

    try:
        return str(find_claude_exe())
    except SystemExit:  # find_claude_exe's "nothing found": the bare name fails where it is run, as before
        return "claude"


# a count word, whole: `944 failing` is not `944 fail` (#954)
_COUNT_WORD = r"\d+[ \t]+(?:pass(?:ed)?|fail(?:ed|ures?)?)(?![A-Za-z])"
_SUMMARY_LINE = re.compile(r"^[=\s]*(?:" + _COUNT_WORD + r"[ \t]*[,/]?[ \t]*)+(?:in [\d.]+s\b.*)?$", re.I)
_COUNT_TOKEN = re.compile(r"(\d+)[ \t]+(pass(?:ed)?|fail(?:ed|ures?)?)(?![A-Za-z])", re.I)


def parse_counts(output: str) -> tuple[int, int] | None:
    """(passed, failed) from the runner's own summary line(s), or None when there is none (#954).

    A summary line holds only count words: `362 pass / 0 fail`, `19 pass, 0 fail`, `18 pass` (bun, one line each),
    `2 failed, 7 passed in 1s` (pytest). A test name such as `#944 failing writes` or `12 pass by default` is not a
    summary line. The last summary line of each kind wins; a kind the runner did not print is 0 (pytest omits zeros).
    """
    passed = failed = None
    for line in output.splitlines():
        s = line.strip()
        if not _SUMMARY_LINE.match(s):
            continue
        for n, word in _COUNT_TOKEN.findall(s):
            if word.lower().startswith("pass"):
                passed = int(n)
            else:
                failed = int(n)
    if passed is None and failed is None:
        return None
    return passed or 0, failed or 0


def _timeouts_only(code: int, out: str, failed: int) -> bool:
    """Failures exist and every failure line of the output names a timeout."""
    if code == 0 or failed == 0:
        return False
    bad = [ln for ln in out.splitlines() if re.search(r"\b(fail|error|not ok)", ln, re.I)
           and not re.search(r"\d+\s+fail", ln, re.I)]
    return bool(bad) and all(re.search(r"time(?:d)?[ -]?out", ln, re.I) for ln in bad)


def _run_part(label: str, cmd: list[str], cwd: Path, runner, counted: bool = True) -> bool:
    code, out = runner(cmd, cwd)
    if not counted:  # `plugin validate` prints no pass/fail counts: only its exit code is read (#954)
        print(f"  {label}: exit {code}")
        return code == 0
    counts = parse_counts(out)
    if _timeouts_only(code, out, counts[1] if counts else 0):
        code, out = runner(cmd, cwd)
        counts = parse_counts(out)
        label += " (repeated once)"
    if counts is None:  # no summary line: no number is printed, and the part is red (#954)
        print(f"  {label}: no summary line in the output, the pass/fail counts are not read (exit {code})")
        return False
    p, f = counts
    print(f"  {label}: {p} pass, {f} fail, exit {code}")
    return code == 0


def run_test(name: str, runner=None, root: Path | None = None) -> int:
    """Run a mod's tests in ``root`` (default: this checkout); 0 = green, 1 = red (#194: the merge passes its suite checkout)."""
    runner = runner or _default_runner
    d = (root or repo_root()) / "mods" / name
    if not d.is_dir():
        print(f"{name}: no repository copy at {d}")
        return 1
    sync_shared(name, root)
    print(f"{name}:")
    claude = _claude()
    ok = _run_part("plugin validate", [claude, "plugin", "validate", "."], d, runner, counted=False)
    t = d / "test"
    if t.is_dir() and any(f.suffix in (".ts", ".tsx") and ".test." in f.name for f in t.rglob("*")):
        ok = _run_part("plugin test", [claude, "plugin", "test", "."], d, runner) and ok
    else:  # a project that holds none of the mod's tests (a port brings none): nothing to run is not red (#833)
        print("  plugin test: no tests in this project")
    b = d / "bin"
    if b.is_dir() and any(b.glob("test_*.py")):
        ok = _run_part("pytest", [sys.executable, "-m", "pytest", "-q", "-p", "no:faulthandler"], b, runner) and ok
    return 0 if ok else 1


# -- CLI --------------------------------------------------------------------------------------

def add_subparser(sub) -> None:
    p = sub.add_parser("mods", help="the repository copy of the Claude Code mods: status / install / test (#789)")
    s = p.add_subparsers(dest="mods_cmd", required=True)
    st = s.add_parser("status", help="files differing between the repository and the install")
    st.add_argument("name", nargs="?", choices=MOD_NAMES)
    ins = s.add_parser("install", help="copy the repository's files over the install, with a backup")
    ins.add_argument("name", choices=MOD_NAMES)
    ins.add_argument("--force", action="store_true", help="overwrite files newer in the install")
    rm = s.add_parser("remove", help="delete an installed mod's folder (usage kept under var/agentops) and take it "
                                     "out of the loaded-mods line")
    rm.add_argument("name", help="the install folder's plain name (any mod, not only the repository's)")
    t = s.add_parser("test", help="run the mod's own tests from the repository copy")
    t.add_argument("name", choices=MOD_NAMES)


def run_mods_command(args) -> int:
    if args.mods_cmd == "status":
        # a project holds the mods its repository copy has (a ported project has only the monitor)
        return run_status([args.name] if args.name else [n for n in MOD_NAMES if (repo_root() / "mods" / n).is_dir()])
    if args.mods_cmd == "install":
        return run_install(args.name, args.force)
    if args.mods_cmd == "remove":
        return run_remove(args.name)
    return run_test(args.name)


def register(core) -> None:
    core.command("mods", add_subparser, run_mods_command, after="merge-status")
    # issue #186: the monitor collector keeps up with its own code (a step of the dispatcher's `after` phase)
    from .dispatch import collector_tick
    core.tick("monitor collector", collector_tick, phase="after", guard=True)
