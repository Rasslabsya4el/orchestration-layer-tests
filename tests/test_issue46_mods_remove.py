"""Issue #46: `mods remove <name>` and `mods install` keep the owner's loaded-mods line (env CLAUDE_CODE_PLUGIN_DIRS of
the settings file), one key of that file and nothing else.

Seams: `mods.run_install` and `mods.run_remove`. The install root and the repo root are redirected by env
AGENTOPS_MODS_DIR / AGENTOPS_MODS_REPO to temp dirs, HOME is a temp dir, and the settings file is passed in as
`settings_path`. No real folder, settings file or command folder is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import mods

MOD_NAMES_TMP = ("pob-slots", "unslop", "zorblax-monitor", "orch-monitor")
USAGE_BYTES = b'{"t":1,"model":"haiku"}\n{"t":2,"model":"haiku"}\n{"t":3,"model":"opus"}\n'
EVENTS_BYTES = b'{"event":"start"}\n{"event":"stop"}\n'


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(mods_dir))
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return SimpleNamespace(
        mods=mods_dir, repo=repo, settings=tmp_path / "settings.json", commands=commands,
        final=repo / "var" / "agentops", backups=repo / "var" / "agentops" / "settings_backup")


def _four(r) -> list[str]:
    """The four loaded-mods paths, under the temp install root (the real machine's folder is never named)."""
    return [str(r.mods / n) for n in MOD_NAMES_TMP]


def _settings_text(value: str, newline: str = "\r\n") -> bytes:
    """A settings file with odd indentation, CRLF newlines, other keys around env, and the one key's JSON literal."""
    lines = [
        "{",
        '    "theme": "dark",',
        '    "env": {',
        '      "OTHER": "1",',
        f'      "CLAUDE_CODE_PLUGIN_DIRS": {json.dumps(value)}',
        "    },",
        '    "permissions": {"allow": ["Bash(ls:*)"]}',
        "}",
        "",
    ]
    return newline.join(lines).encode("utf-8")


def _repo_mod(r, name: str) -> None:
    """A repository copy of mod `name` (mods/<name>/) for `mods install` to copy."""
    _write(r.repo / "mods" / name / "hooks" / "index.ts", "export const x = 1\n")
    _write(r.repo / "mods" / name / ".claude-plugin" / "plugin.json", '{"name": "%s"}\n' % name)


def _backups(r) -> list[Path]:
    return sorted(r.backups.iterdir()) if r.backups.is_dir() else []


# -- row 1: remove takes the folder's path out of the key, every other byte stays -----------------------------

def test_row1_remove_swaps_only_the_key_literal_and_writes_one_backup(roots, capsys):
    paths = _four(roots)
    old_value = ";".join(paths)
    new_value = ";".join([paths[0], paths[2], paths[3]])
    old = _settings_text(old_value)
    roots.settings.write_bytes(old)
    _write(roots.mods / "unslop" / "usage.jsonl", USAGE_BYTES)

    rc = mods.run_remove("unslop", settings_path=roots.settings)

    out = capsys.readouterr().out
    assert rc == 0, out
    expected = old.replace(json.dumps(old_value).encode(), json.dumps(new_value).encode(), 1)
    assert roots.settings.read_bytes() == expected
    assert json.loads(roots.settings.read_text(encoding="utf-8"))["env"]["CLAUDE_CODE_PLUGIN_DIRS"] == new_value
    assert len(_backups(roots)) == 1, "exactly one backup file"
    assert _backups(roots)[0].read_bytes() == old, "the backup is the file as it was"
    assert f"CLAUDE_CODE_PLUGIN_DIRS: {old_value} -> {new_value}" in out, out
    assert "restart Claude Code once" in out, out
    assert not (roots.mods / "unslop").exists()


# -- row 2: install appends the folder once; listed already: no write, no backup -----------------------------

def test_row2_install_of_an_unlisted_mod_appends_its_folder_once(roots, capsys):
    name = "pob-slots"
    _repo_mod(roots, name)
    others = _four(roots)[1:]
    roots.settings.write_bytes(_settings_text(";".join(others)))

    rc = mods.run_install(name, settings_path=roots.settings, commands_dir=roots.commands)

    out = capsys.readouterr().out
    assert rc == 0, out
    value = json.loads(roots.settings.read_text(encoding="utf-8"))["env"]["CLAUDE_CODE_PLUGIN_DIRS"]
    assert value == ";".join([*others, str(roots.mods / name)])
    assert len(_backups(roots)) == 1
    after_first = roots.settings.read_bytes()

    rc = mods.run_install(name, settings_path=roots.settings, commands_dir=roots.commands)

    assert rc == 0, capsys.readouterr().out
    assert roots.settings.read_bytes() == after_first, "a second install appended again"
    assert len(_backups(roots)) == 1, "a second install that writes nothing made a backup"


def test_row2_install_of_a_listed_mod_leaves_the_file_untouched_and_makes_no_backup(roots, capsys):
    name = "pob-slots"
    _repo_mod(roots, name)
    before = _settings_text(";".join(_four(roots)))
    roots.settings.write_bytes(before)

    rc = mods.run_install(name, settings_path=roots.settings, commands_dir=roots.commands)

    assert rc == 0, capsys.readouterr().out
    assert roots.settings.read_bytes() == before
    assert _backups(roots) == []


# -- row 3: remove of an installed mod keeps its usage records, a second remove says not installed ----------

def test_row3_remove_keeps_every_usage_row_and_a_second_remove_says_not_installed(roots, capsys):
    name = "mod-x"
    folder = roots.mods / name
    _write(folder / "usage.jsonl", USAGE_BYTES)
    _write(folder / "events.jsonl", EVENTS_BYTES)
    _write(folder / "hooks" / "index.ts", "export const x = 1\n")
    roots.settings.write_bytes(_settings_text(";".join([str(folder), str(roots.mods / "pob-slots")])))

    rc = mods.run_remove(name, settings_path=roots.settings)

    assert rc == 0, capsys.readouterr().out
    assert not folder.exists()
    assert (roots.final / f"{name}-usage-final.jsonl").read_bytes() == USAGE_BYTES
    assert (roots.final / f"{name}-events-final.jsonl").read_bytes() == EVENTS_BYTES
    capsys.readouterr()

    rc = mods.run_remove(name, settings_path=roots.settings)

    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"{name}: not installed" in out, out
    assert (roots.final / f"{name}-usage-final.jsonl").read_bytes() == USAGE_BYTES


@pytest.mark.parametrize("final_bytes, expected", [
    (b"r0\nr1\nr2\n", b"r0\nr1\nr2\nr3\n"),
    (b"r0\nr1\nr2", b"r0\nr1\nr2\nr3\n"),  # the copy has no final newline: the appended row starts on its own line
])
def test_row3_an_existing_copy_is_appended_with_the_missing_rows_never_overwritten(roots, final_bytes, expected):
    name = "mod-y"
    folder = roots.mods / name
    _write(folder / "usage.jsonl", b"r1\nr2\nr3\n")
    _write(roots.final / f"{name}-usage-final.jsonl", final_bytes)
    roots.settings.write_bytes(_settings_text(str(folder)))

    assert mods.run_remove(name, settings_path=roots.settings) == 0
    assert (roots.final / f"{name}-usage-final.jsonl").read_bytes() == expected


# -- row 4: a settings file that is not JSON: nothing written, rc 1, one line --------------------------------

def _tree(root: Path) -> dict[str, bytes]:
    """Every file under `root` with its bytes; {} when `root` is absent."""
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("content", [b"not json at all\r\n", b'{"env": {"CLAUDE_CODE_PLUGIN_DIRS": "x"'])
def test_row4_install_with_unreadable_settings_writes_nothing_and_fails_with_one_line(roots, capsys, content):
    name = "pob-slots"
    _repo_mod(roots, name)
    folder = roots.mods / name
    _write(folder / "hooks" / "index.ts", "export const x = 2\n")  # differs from the repo copy: a copy would change it
    _write(folder / "old.ts", "gone from the repo\n")  # not in the repo: a copy would remove it
    before = _tree(folder)
    roots.settings.write_bytes(content)

    rc = mods.run_install(name, force=True, settings_path=roots.settings, commands_dir=roots.commands)

    out = capsys.readouterr().out
    assert rc == 1, out
    assert roots.settings.read_bytes() == content
    assert _backups(roots) == []
    assert _tree(folder) == before, "an install with unreadable settings changed the install folder"
    assert not (folder / ".cache").exists(), "an install with unreadable settings made a backup of the folder"
    settings_lines = [ln for ln in out.splitlines() if "CLAUDE_CODE_PLUGIN_DIRS" in ln]
    assert len(settings_lines) == 1, out


@pytest.mark.parametrize("content", [b"not json at all\r\n", b"[1, 2]\n"])
def test_row4_remove_with_unreadable_settings_leaves_the_folder_and_fails_with_one_line(roots, capsys, content):
    folder = roots.mods / "unslop"
    _write(folder / "usage.jsonl", USAGE_BYTES)
    roots.settings.write_bytes(content)

    rc = mods.run_remove("unslop", settings_path=roots.settings)

    out = capsys.readouterr().out
    assert rc == 1, out
    assert len(out.strip().splitlines()) == 1, out
    assert roots.settings.read_bytes() == content
    assert _backups(roots) == []
    assert (folder / "usage.jsonl").read_bytes() == USAGE_BYTES, "a refused remove deleted the folder"


def test_missing_settings_file_is_not_created_and_the_command_goes_on(roots, capsys):
    folder = roots.mods / "unslop"
    _write(folder / "usage.jsonl", USAGE_BYTES)

    rc = mods.run_remove("unslop", settings_path=roots.settings)

    out = capsys.readouterr().out
    assert rc == 0, out
    assert not roots.settings.exists()
    assert not folder.exists()
    assert "no settings file" in out and "nothing written" in out, out


# -- the name is a plain folder name: no separator, no "..", not empty ---------------------------------------

@pytest.mark.parametrize("name", ["..", "../x", "a/b", "a\\b", "", "."])
def test_a_name_that_is_not_a_plain_folder_name_is_refused(roots, capsys, name):
    outside = roots.mods.parent / "x"
    _write(outside / "usage.jsonl", USAGE_BYTES)
    _write(roots.mods / "keep" / "usage.jsonl", USAGE_BYTES)
    roots.settings.write_bytes(_settings_text(str(roots.mods / "keep")))
    before = roots.settings.read_bytes()

    rc = mods.run_remove(name, settings_path=roots.settings)

    out = capsys.readouterr().out
    assert rc == 1, out
    assert len(out.strip().splitlines()) == 1, out
    assert roots.settings.read_bytes() == before
    assert roots.repo.is_dir() and roots.mods.is_dir()
    assert (outside / "usage.jsonl").read_bytes() == USAGE_BYTES
    assert (roots.mods / "keep" / "usage.jsonl").read_bytes() == USAGE_BYTES


# -- the key or the "env" object absent: the fallback rewrite keeps indent, key order and newlines -----------

def test_absent_key_inside_env_is_added_with_the_files_indent_and_key_order(roots, capsys):
    name = "pob-slots"
    _repo_mod(roots, name)
    roots.settings.write_bytes(b'{\n    "theme": "dark",\n    "env": {\n        "FOO": "1"\n    }\n}\n')

    rc = mods.run_install(name, settings_path=roots.settings, commands_dir=roots.commands)

    assert rc == 0, capsys.readouterr().out
    text = roots.settings.read_text(encoding="utf-8")
    data = json.loads(text)
    assert list(data) == ["theme", "env"]
    assert list(data["env"]) == ["FOO", "CLAUDE_CODE_PLUGIN_DIRS"]
    assert data["env"]["CLAUDE_CODE_PLUGIN_DIRS"] == str(roots.mods / name)
    assert '\n    "env": {\n        "FOO": "1",\n        "CLAUDE_CODE_PLUGIN_DIRS"' in text
    assert text.endswith("}\n")


def test_absent_env_object_is_created_with_crlf_kept(roots, capsys):
    name = "pob-slots"
    _repo_mod(roots, name)
    roots.settings.write_bytes(b'{\r\n  "theme": "dark"\r\n}\r\n')

    rc = mods.run_install(name, settings_path=roots.settings, commands_dir=roots.commands)

    assert rc == 0, capsys.readouterr().out
    raw = roots.settings.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "a bare LF crept in"
    data = json.loads(raw.decode("utf-8"))
    assert data == {"theme": "dark", "env": {"CLAUDE_CODE_PLUGIN_DIRS": str(roots.mods / name)}}
    assert b'\r\n  "env": {\r\n    "CLAUDE_CODE_PLUGIN_DIRS"' in raw


def test_env_that_is_not_an_object_is_refused_and_nothing_is_written(roots, capsys):
    name = "pob-slots"
    _repo_mod(roots, name)
    content = b'{\n  "env": "oops"\n}\n'
    roots.settings.write_bytes(content)

    rc = mods.run_install(name, settings_path=roots.settings, commands_dir=roots.commands)

    out = capsys.readouterr().out
    assert rc == 1, out
    assert roots.settings.read_bytes() == content
    assert _backups(roots) == []
