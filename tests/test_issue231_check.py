"""#231 unit 1 (D119): the test copy check. Real git in a temp repo; the machine file (``test_copy_patterns.json``)
sits in a temp ``AGENTOPS_MACHINE_DIR``. The product name is a made-up one (``Zorblax``); the email, the user path
and the Telegram line are spelled in parts, so this file itself never carries a shape the check would hit.

Tested here: row 1 of the issue (a planted product name and an email: exit 1, both file:line named with their
kinds, neither the name nor the email text printed), the excluded set (docs/research/, var/, artifacts/, the leak
patterns file (not copied, not read by the check), a path a repo setting lists: no hit, absent from the snapshot), a clean repo (exit 0), a binary and a UTF-16 file
(scanned like text), a planted name in a path (line 0), and the Telegram shape (a word and 6-12 digits on one line)."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from tools.agentops import test_copy_check as tc
from tools.agentops.test_copy_cmd import run_test_copy_command

PRODUCT = "Zorblax"
EMAIL = "ivan" + "@" + "mailhost" + ".io"
USER_PATH = "C:" + "\\Users\\" + "someone" + "\\notes.txt"
BS = chr(92)  # a backslash, so no escape has to be read twice in a pattern test
TG_LINE ="# te" + "legram 1234" + "56789\n"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _machine(tmp_path: Path, monkeypatch, patterns: dict | None) -> Path:
    machine = tmp_path / "machine"
    machine.mkdir()
    if patterns is not None:
        (machine / "test_copy_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(machine))
    return machine


def _check(repo: Path, capsys) -> tuple[int, str]:
    rc = run_test_copy_command(argparse.Namespace(test_copy_command="check", root=repo))
    return rc, capsys.readouterr().out


def test_planted_product_name_and_email_exit_1_without_the_text(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT})
    repo = _repo(tmp_path, {
        "tests/test_planted.py": "X = 1\nLABEL = '" + PRODUCT + " lab'\n",
        "tests/test_mail.py": "# contact " + EMAIL + "\n",
    })
    rc, out = _check(repo, capsys)
    assert rc == 1
    assert "tests/test_planted.py:2 product" in out
    assert "tests/test_mail.py:1 email" in out
    assert "email=1" in out and "product=1" in out
    assert PRODUCT not in out
    assert EMAIL not in out


def test_excluded_paths_give_no_hit_and_are_absent_from_snapshot(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT})
    repo = _repo(tmp_path, {
        "docs/research/x.md": "mail " + EMAIL + "\n",
        "var/log.txt": EMAIL + "\n",
        "artifacts/a.txt": EMAIL + "\n",
        "config/core_leak_patterns.json": json.dumps({"L1": PRODUCT}),
        "config/orchestration.json": json.dumps({"test_copy_exclude": ["private/"]}),
        "private/p.py": "X = '" + PRODUCT + "'\n",
        "tests/ok.py": "X = 1\n",
    })
    files = tc.snapshot_files(repo)
    assert files == ["config/orchestration.json", "tests/ok.py"]
    assert tc.scan(repo) == []
    rc, out = _check(repo, capsys)
    assert rc == 0
    assert "docs/research/x.md" not in out and "var/log.txt" not in out


def test_clean_repo_exits_0_and_names_the_unchecked_kinds(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, None)
    repo = _repo(tmp_path, {"tests/test_ok.py": "X = 1\n", "docs/a.md": "plain\n"})
    rc, out = _check(repo, capsys)
    assert rc == 0
    assert "not checked: product" in out
    assert "not checked: owner-name" in out
    assert "L1-L3" not in out
    assert "hits per kind: email=0 user-path=0 telegram-id=0" in out


def test_binary_file_is_scanned_like_text(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch, None)
    repo = _repo(tmp_path, {"tests/blob.bin": bytes([0, 1]) + EMAIL.encode() + b"\n"})
    assert tc.scan(repo) == [tc.Hit("tests/blob.bin", 1, "email")]
    rc, _ = _check(repo, capsys)
    assert rc == 1


def test_utf16_file_is_decoded_and_scanned(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, None)
    text = "x" + "\n" + "# " + EMAIL + "\n"
    repo = _repo(tmp_path, {
        "tests/with_bom.txt": text.encode("utf-16"),
        "tests/no_bom.txt": text.encode("utf-16-le"),
    })
    assert tc.scan(repo) == [tc.Hit("tests/no_bom.txt", 2, "email"), tc.Hit("tests/with_bom.txt", 2, "email")]


def test_a_planted_name_in_a_path_is_a_line_zero_hit(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, {"product": PRODUCT})
    repo = _repo(tmp_path, {"tests/test_" + PRODUCT.lower() + "_case.py": "X = 1\n"})
    assert tc.scan(repo) == [tc.Hit("tests/test_zorblax_case.py", 0, "product")]


def test_user_path_and_telegram_shapes_are_hits(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, None)
    repo = _repo(tmp_path, {
        "tests/paths.py": "P = '" + USER_PATH + "'\n",
        "tests/planted_ids.py": TG_LINE + "x = 123456789\n",
    })
    hits = tc.scan(repo)
    assert tc.Hit("tests/paths.py", 1, "user-path") in hits
    assert tc.Hit("tests/planted_ids.py", 1, "telegram-id") in hits
    assert not any(h.file == "tests/planted_ids.py" and h.line == 2 for h in hits)


def test_listed_path_matches_exactly_or_as_a_folder_never_as_a_prefix(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, None)
    repo = _repo(tmp_path, {
        "config/orchestration.json": json.dumps({"test_copy_exclude": ["foo.py", "private"]}),
        "foo.py": "X = 1\n", "foo.py.bak": "X = 1\n",
        "private/a.py": "X = 1\n", "private2/b.py": "X = 1\n",
    })
    assert tc.snapshot_files(repo) == ["config/orchestration.json", "foo.py.bak", "private2/b.py"]


def test_leak_file_is_not_read_by_the_check(tmp_path, monkeypatch):
    """#285 rule 3: the repo's own leak words are not this check's kinds; a leak file that does not parse is no error."""
    _machine(tmp_path, monkeypatch, None)
    repo = _repo(tmp_path, {"config/core_leak_patterns.json": "{not json", "tests/ok.py": "X = 1\n"})
    assert list(tc.load_patterns(repo).compiled) == ["email", "user-path", "telegram-id"]


def _hits_in_line(tmp_path: Path, line: str, kind: str) -> bool:
    """Whether ``scan`` reports ``kind`` for this one line (the built-in patterns only, no git needed)."""
    (tmp_path / "line.txt").write_text(line + "\n", encoding="utf-8")
    hits = tc.scan(tmp_path, tc.Patterns(dict(tc.BUILTIN), ()), ["line.txt"])
    return any(h.kind == kind and h.line == 1 for h in hits)


@pytest.mark.parametrize("text", [
    "ja" + "ne@" + "corp" + ".org",
    "ja" + "ne@" + "mail." + "example-corp.com",
    "x = 'bo" + "b@" + "host.co.uk'",
    # a reserved domain is allowed only when nothing follows it
    "ja" + "ne@" + "example.com.corp.io",
    "ja" + "ne@" + "corp.test.com",
    "ja" + "ne@" + "localhost.evil.io",
    "x@" + "mail.invalid.org",
    # a real address after a string escape
    BS + "n" + "ja" + "ne@" + "corp.org",
    '"' + BS + "t" + "ja" + 'ne@' + 'corp.org"',
    "a" + BS + "r" + "ja" + "ne@" + "corp.org",
])
def test_real_looking_email_still_hits(tmp_path, text):
    assert _hits_in_line(tmp_path, text, "email")


@pytest.mark.parametrize("text", [
    "a@" + "example.com", "a@" + "example.org", "a@" + "example.net", "a@" + "mail.invalid", "a@" + "host.test",
    "a@" + "localhost", "a@" + "sub.example.com", "t@" + "x.invalid", "agentops-test@" + "example.invalid",
    "u@" + "localhost", "see a@" + "example.com, then",
    BS + "n" + "@pytest.mark.parametrize", "@pytest.mark.skip", BS + "t" + "@pytest.mark.skip",
])
def test_reserved_domain_or_decorator_is_not_an_email(tmp_path, text):
    assert not _hits_in_line(tmp_path, text, "email")


def _shape(kind: str, text: str) -> bool:
    return bool(tc.BUILTIN[kind].search(text))


@pytest.mark.parametrize("text", [
    "C:" + BS + "Users" + BS + "ja" + "ne" + BS + "notes", "c:/" + "users/" + "ja" + "ne", "/ho" + "me/" + "ja" + "ne/x",
    "/Us" + "ers/" + "username2/x",
    # a placeholder name with a tail is a real-looking account
    "C:" + BS + "Users" + BS + "user" + ".example", "/ho" + "me/" + "you" + ".smith", "/Us" + "ers/" + "u" + ".ivanov",
])
def test_real_looking_user_path_still_hits(text):
    assert _shape("user-path", text)


@pytest.mark.parametrize("name", ["user", "u", "username", "you", "name", "<you>", "[name]", "{user}", ".hidden"])
def test_placeholder_account_is_not_a_user_path(name):
    assert not _shape("user-path", "C:" + BS + "Users" + BS + name + BS + "x")
    assert not _shape("user-path", "/ho" + "me/" + name + "/x")


def test_placeholder_account_with_doubled_backslashes_is_not_a_user_path():
    assert not _shape("user-path", "C:" + BS * 2 + "Users" + BS * 2 + "u")
    assert not _shape("user-path", "C:" + BS + "Users" + BS + "user" + BS + "x")
