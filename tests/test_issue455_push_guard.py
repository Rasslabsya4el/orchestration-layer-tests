"""#455 unit 2: red line 3 decides an agent's `git push` by the repo it resolves to, and `gh repo create` is refused
outside `<owner>/<trial_repo_prefix>*`. Table-driven over `permission_guard.decide`, on tmp git repos whose origin is
only a URL (no network). The settings are a tmp file named by `AGENTOPS_CORE_SETTINGS`."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.hooks import permission_guard as pg  # noqa: E402

OWNER = "acme-owner"
OWN = f"{OWNER}/orchestration-layer"
MIRROR = f"{OWNER}/orch-it"
TARGET = f"{OWNER}/Poe-Junkroan"
TEST_COPY = f"{OWNER}/orchestration-layer-tests"
OUTSIDE = "octocat/Hello-World"
PREFIX = "orch-trial-"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    data = json.loads((ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    data.update(repo=OWN, targets=[{"repo": TARGET, "folder": "x"}], mirror_repos=[MIRROR],
                test_copy_remote=f"https://github.com/{TEST_COPY}.git", trial_repo_prefix=PREFIX)
    path = tmp_path / "settings" / "orchestration.json"
    path.parent.mkdir()
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_CORE_SETTINGS", str(path))
    return path


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _clone(tmp_path, name, origin=None, **remotes):
    """A tmp git repo with `origin` (and further named remotes) set to the given URLs; no network."""
    path = tmp_path / name
    path.mkdir()
    _git("init", "-q", cwd=path)
    if origin:
        _git("remote", "add", "origin", origin, cwd=path)
    for remote, url in remotes.items():
        _git("remote", "add", remote, url, cwd=path)
    return path


def _decide(text, cwd):
    return pg.decide({"tool_name": "Bash", "tool_input": {"command": text}, "cwd": str(cwd), "session_id": "s"})


def _allowed(text, cwd):
    verdict = _decide(text, cwd)
    assert verdict is None, (text, verdict)


def _refused(text, cwd, *names):
    verdict = _decide(text, cwd)
    assert verdict and verdict.startswith("permission guard, red line 3"), (text, verdict)
    for name in names:
        assert name in verdict, (text, verdict)


URL_FORMS = [  # (url template with {r}, repo)
    "https://github.com/{r}", "https://github.com/{r}.git", "git\x40github.com:{r}.git", "git\x40github.com:{r}",
    "ssh://git\x40github.com/{r}.git", "https://user\x40github.com/{r}.git/",
]


@pytest.mark.parametrize("form", URL_FORMS)
@pytest.mark.parametrize("repo", [OWN, MIRROR, TARGET, TEST_COPY, f"{OWNER}/{PREFIX}x", MIRROR.upper()])
def test_url_of_an_allowed_repo_goes_on_to_the_remaining_clauses(settings, tmp_path, form, repo):
    _allowed(f"git push {form.format(r=repo)} x", tmp_path)


@pytest.mark.parametrize("form", URL_FORMS)
@pytest.mark.parametrize("repo", [OUTSIDE, f"{OWNER}/scratch-x", f"octocat/{PREFIX}x", f"{OWNER}/orch-it-2"])
def test_url_of_any_other_repo_is_refused_naming_it(settings, tmp_path, form, repo):
    _refused(f"git push {form.format(r=repo)} x", tmp_path, repo)


def test_url_of_another_host_or_shape_is_refused_as_unresolved(settings, tmp_path):
    _refused("git push https://example.com/o/r.git x", tmp_path, "unresolved")
    _refused("git push https://github.com/only-one-part x", tmp_path, "unresolved")


@pytest.mark.parametrize("tail", ["--force", "-f", "--delete", "--force-with-lease", "-fu"])
def test_flags_still_refuse_a_push_to_an_allowed_repo(settings, tmp_path, tail):
    _refused(f"git push {tail} https://github.com/{MIRROR}.git x", tmp_path)


@pytest.mark.parametrize("ref", ["+x", ":x"])
def test_refspecs_with_plus_or_colon_still_refuse(settings, tmp_path, ref):
    _refused(f"git push https://github.com/{MIRROR}.git {ref}", tmp_path)
    _refused(f"git push origin {ref}", tmp_path)


@pytest.mark.parametrize("push", ["git -C {c} push", "git -C {c} push origin", "git -C {c} push -u origin b",
                                  "git push", "git push origin", "git push -u origin b"])
def test_clone_of_an_outside_repo_is_refused_naming_it(settings, tmp_path, push):
    clone = _clone(tmp_path, "hello", "https://github.com/octocat/Hello-World.git")
    cwd = tmp_path if "-C" in push else clone
    _refused(push.format(c=clone.as_posix()), cwd, OUTSIDE)


@pytest.mark.parametrize("origin", [f"https://github.com/{OWN}.git", f"git\x40github.com:{OWN}.git",
                                    f"https://github.com/{MIRROR}.git", f"https://github.com/{TARGET}",
                                    f"https://github.com/{OWNER}/{PREFIX}t455a.git",
                                    f"https://github.com/{TEST_COPY}.git"])
@pytest.mark.parametrize("push", ["git -C {c} push", "git -C {c} push -u origin issue-455-2", "git push",
                                  "git push origin issue-455-2", "git push origin main"])
def test_clone_of_an_allowed_repo_is_allowed(settings, tmp_path, origin, push):
    clone = _clone(tmp_path, "mine", origin)
    cwd = tmp_path if "-C" in push else clone
    _allowed(push.format(c=clone.as_posix()), cwd)


def test_a_named_remote_is_resolved_by_its_own_url(settings, tmp_path):
    clone = _clone(tmp_path, "two", f"https://github.com/{OWN}.git", backup="git\x40github.com:octocat/Hello-World.git",
                   fine=f"https://github.com/{MIRROR}.git")
    _refused("git push backup main", clone, OUTSIDE)
    _allowed("git push fine main", clone)
    _allowed("git push origin main", clone)


def test_a_remote_that_does_not_resolve_is_refused(settings, tmp_path):
    bare = _clone(tmp_path, "bare")  # no remote at all
    _refused("git push", bare, "unresolved")
    _refused("git push origin main", bare, "unresolved")
    _refused("git push backup main", bare, "unresolved")
    local = _clone(tmp_path, "local", str(tmp_path / "somewhere" / "r.git"))  # a path is not a GitHub repo
    _refused("git push", local, "unresolved")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()  # a folder git does not know
    _refused(f"git -C {plain.as_posix()} push", tmp_path, "unresolved")


def test_a_directory_that_does_not_exist_changes_nothing(settings):
    # git itself fails there, so no push can reach any repo: the old verdicts stand
    gone = "C:/no/such/clone" if sys.platform == "win32" else "/no/such/clone"
    assert _decide("git push origin issue-1", gone) is None
    assert _decide("git push", gone) is None
    assert "red line 3" in _decide("git push backup main", gone)


def test_unreadable_settings_leave_the_origin_of_the_hooks_own_repo_as_the_allowed_set(tmp_path, monkeypatch):
    home = _clone(tmp_path, "home", f"https://github.com/{OWN}.git")  # the repo the hook file lives in
    monkeypatch.setattr(pg, "_REPO_ROOT", str(home))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("AGENTOPS_CORE_SETTINGS", str(tmp_path / "missing.json"))
    clone = _clone(tmp_path, "hello", "https://github.com/octocat/Hello-World.git")
    _refused("git push", clone, OUTSIDE)
    _refused("git push https://github.com/octocat/Hello-World.git x", tmp_path, OUTSIDE)
    _allowed(f"git push https://github.com/{OWN}.git x", tmp_path)
    _refused(f"git push https://github.com/{MIRROR}.git x", tmp_path, MIRROR)


def test_an_exception_in_the_new_check_refuses_it_does_not_let_the_push_through(settings, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(pg, "_remote_url", boom)
    monkeypatch.setattr(pg, "_repo_of_url", boom)
    clone = _clone(tmp_path, "mine", f"https://github.com/{OWN}.git")
    _refused("git push", clone, "boom")
    _refused("git push https://github.com/o/r.git x", clone, "boom")


@pytest.mark.parametrize("name", [f"{OWNER}/{PREFIX}x", f"{PREFIX}x", f"{OWNER}/{PREFIX}t455c-2"])
def test_gh_repo_create_inside_the_trial_prefix_is_allowed(settings, tmp_path, name):
    _allowed(f"gh repo create {name} --private", tmp_path)
    _allowed(f"gh repo create {name} --public --clone", tmp_path)


@pytest.mark.parametrize("name", [f"{OWNER}/scratch-x", "scratch-x", f"octocat/{PREFIX}x", f"{OWNER}/x{PREFIX}",
                                  f"{OWNER}/orch-it-copy"])
def test_gh_repo_create_outside_the_trial_prefix_is_refused_naming_it(settings, tmp_path, name):
    _refused(f"gh repo create {name} --private", tmp_path, name)


def test_gh_repo_create_cannot_hide_the_name_behind_a_flag_value(settings, tmp_path):
    _refused(f"gh repo create -d {PREFIX}x {OWNER}/scratch-x --private", tmp_path, "scratch-x")
    _allowed(f"gh repo create -d 'some words' {OWNER}/{PREFIX}x --private", tmp_path)
    _refused("gh repo create --private", tmp_path, "unresolved")


def test_other_gh_repo_verbs_and_the_deletes_are_unchanged(settings, tmp_path):
    _allowed("gh repo view octocat/Hello-World", tmp_path)
    _allowed("gh repo list", tmp_path)
    verdict = _decide(f"gh repo delete {OWNER}/{PREFIX}x --yes", tmp_path)
    assert verdict and verdict.startswith("permission guard, red line 4"), verdict


def test_the_orch_unit_branch_push_of_this_repo_stays_allowed(settings, tmp_path):
    clone = _clone(tmp_path, "unit", f"https://github.com/{OWN}.git")
    _allowed("git push -u origin issue-455-u2", clone)
    _allowed("git push origin main", clone)


# ---- send-back on unit 2: the cheap, concrete holes of a text-matching guard ----

HELLO = "https://github.com/octocat/Hello-World.git"


@pytest.mark.parametrize("opt", ["--repo={u}", "--repo {u}"])
def test_repo_option_is_the_target_of_the_push(settings, tmp_path, opt):
    mine = _clone(tmp_path, "mine", f"https://github.com/{OWN}.git")
    elsewhere = _clone(tmp_path, "elsewhere", HELLO)
    _refused("git push " + opt.format(u=HELLO) + " x", mine, OUTSIDE)  # an allowed origin does not carry it
    _allowed("git push " + opt.format(u=f"https://github.com/{MIRROR}.git") + " x", elsewhere)
    _allowed("git push " + opt.format(u="origin") + " x", mine)
    _refused("git push " + opt.format(u="origin") + " x", elsewhere, OUTSIDE)


@pytest.mark.parametrize("text,form", [
    (f"git -c remote.origin.pushurl={HELLO} push", "remote."),
    (f"git -c remote.origin.url={HELLO} push origin", "remote."),
    (f"git -c url.{HELLO}.insteadOf=https://github.com/x/y push", "url."),
    ("git --git-dir=/elsewhere/.git push", "--git-dir"),
    ("git --git-dir /elsewhere/.git push", "--git-dir"),
    ("git --work-tree=/elsewhere push", "--work-tree"),
    ("git --work-tree /elsewhere push origin", "--work-tree"),
    ("GIT_DIR=/elsewhere/.git git push", "GIT_DIR"),
    ("GIT_CONFIG_COUNT=1 git push", "GIT_CONFIG_COUNT"),
    ("GIT_CONFIG_GLOBAL=/x/cfg git push", "GIT_CONFIG_GLOBAL"),
])
def test_a_form_that_redirects_the_push_is_refused_naming_it(settings, tmp_path, text, form):
    mine = _clone(tmp_path, "mine", f"https://github.com/{OWN}.git")
    _refused(text, mine, form)


def test_unrelated_global_options_do_not_trip_the_redirect_check(settings, tmp_path):
    mine = _clone(tmp_path, "mine", f"https://github.com/{OWN}.git")
    _allowed("git -c user.name=x -c core.editor=true push origin b", mine)
    _allowed(f"git -C {mine.as_posix()} -c user.name=x push", tmp_path)


def test_a_two_word_global_option_does_not_hide_the_subcommand(settings, tmp_path):
    # `--git-dir <path>` used to read <path> as the subcommand, so the verb after it was never judged
    assert "red line 3" in _decide("git --git-dir /some/dir reset --hard", tmp_path)
    assert "red line 3" in _decide("git --work-tree /some/dir branch -D x", tmp_path)


def test_the_push_url_is_what_a_push_reaches(settings, tmp_path):
    clone = _clone(tmp_path, "mine", f"https://github.com/{OWN}.git")
    _git("config", "remote.origin.pushurl", HELLO, cwd=clone)
    _refused("git push", clone, OUTSIDE)
    _refused("git push origin b", clone, OUTSIDE)


def test_push_default_names_the_remote_when_none_is_given(settings, tmp_path):
    clone = _clone(tmp_path, "mine", f"https://github.com/{OWN}.git", fork=HELLO)
    _git("config", "remote.pushDefault", "fork", cwd=clone)
    _refused("git push", clone, OUTSIDE)
    _refused("git push -u", clone, OUTSIDE)
    _allowed("git push origin b", clone)  # a remote named on the line wins


@pytest.mark.parametrize("path", ["$NOPE_455/gone", "~/gone-455", "%NOPE_455%/gone", "$(pwd)/gone", "`pwd`/gone"])
def test_a_missing_directory_of_a_non_literal_path_is_refused_as_unresolved(settings, tmp_path, path):
    _refused(f'git -C "{path}" push', tmp_path, "unresolved")


def test_a_cd_to_a_non_literal_path_is_refused_as_unresolved(settings, tmp_path):
    _refused('cd "$NOPE_455/gone" && git push origin b', tmp_path, "unresolved")
    _refused('cd ~/gone-455 && git push', tmp_path, "unresolved")


def test_a_literal_missing_directory_still_passes(settings, tmp_path):
    assert _decide(f"git -C {(tmp_path / 'gone').as_posix()} push origin b", tmp_path) is None


def test_a_refusal_for_unreadable_settings_says_so(tmp_path, monkeypatch):
    home = _clone(tmp_path, "home", f"https://github.com/{OWN}.git")
    monkeypatch.setattr(pg, "_REPO_ROOT", str(home))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("AGENTOPS_CORE_SETTINGS", str(tmp_path / "missing.json"))
    _refused(f"git push {HELLO} x", tmp_path, OUTSIDE, "settings unreadable")
    _refused(f"gh repo create {OWNER}/scratch-x", tmp_path, "settings unreadable")
