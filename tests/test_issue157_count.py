"""Issue 157 unit 1: a core update that takes a home commit also records `core_version_count` (int) in the
target's `config/orchestration.json`: the commits on the home's main up to and including the taken one, the same
number `git rev-list --count <sha>` gives at the home. `core_targets` (running at the home, with a checkout)
counts locally, so an update makes no extra GitHub call.

Rows: 1 a target taking home commit X gets the count up to X, in the update's own commit; 2 a count that
cannot be had writes no key and the update still succeeds; 3 a second update to the same head changes nothing;
4 a count that cannot be had for a new head leaves no stale count behind.

Fakes: the home is a real local git repo (the count comes from its history); `FakeGh` of the unit 1115 tests."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_issue1115_targets import HEAD, Completed, FakeGh, _core_update_commits, _git, _home, _settings_of, _target, _write
from tools.agentops import core_targets, core_update


def _home_with_commits(tmp_path: Path, extra: int) -> tuple[Path, str, int]:
    home = _home(tmp_path, targets=None)
    for n in range(extra):
        _write(home, f"note{n}.txt", str(n))
        _git(home, "add", "-A")
        _git(home, "commit", "-q", "-m", f"c{n}")
    sha = _git(home, "rev-parse", "HEAD").stdout.strip()
    count = int(_git(home, "rev-list", "--count", sha).stdout.strip())
    return home, sha, count


def _run(tmp_path: Path, folder: Path, home: Path, sha: str) -> list[str]:
    gh = FakeGh(head=sha)
    return core_targets.run_core_targets([{"folder": str(folder), "repo": "owner/t1"}], home_root=home, gh_runner=gh)


# Row 1 -- the target takes home commit X: the count up to X lands in the update's own commit.
def test_row1_taken_commit_records_the_home_count(tmp_path: Path) -> None:
    home, sha, count = _home_with_commits(tmp_path, extra=3)
    t1 = _target(tmp_path, "t1")

    lines = _run(tmp_path, t1, home, sha)

    assert lines == ["owner/t1: taken " + sha[:8]], lines
    assert count == 4
    settings = _settings_of(t1)
    assert settings["core_version"] == sha
    assert settings["core_version_count"] == count
    assert "config/orchestration.json" in _git(t1, "diff", "--name-only", "HEAD~1", "HEAD").stdout.split()
    assert json.loads(_git(t1, "show", "HEAD:config/orchestration.json").stdout)["core_version_count"] == count


# Row 2 -- the count cannot be had (the home folder is no git checkout): taken, no key, no crash.
def test_row2_count_lookup_failing_writes_no_key_and_the_update_succeeds(tmp_path: Path) -> None:
    _, sha, _ = _home_with_commits(tmp_path, extra=1)
    t1 = _target(tmp_path, "t1")
    not_a_checkout = tmp_path / "plain"
    not_a_checkout.mkdir()

    lines = _run(tmp_path, t1, not_a_checkout, sha)

    assert lines == ["owner/t1: taken " + sha[:8]], lines
    settings = _settings_of(t1)
    assert settings["core_version"] == sha
    assert "core_version_count" not in settings, settings


# Row 3 -- a second update to the same head writes zero files and makes no commit.
def test_row3_second_update_to_the_same_head_changes_nothing(tmp_path: Path) -> None:
    home, sha, count = _home_with_commits(tmp_path, extra=2)
    t1 = _target(tmp_path, "t1")
    _run(tmp_path, t1, home, sha)
    head_before = _git(t1, "rev-parse", "HEAD").stdout.strip()
    bytes_before = (t1 / "config" / "orchestration.json").read_bytes()

    lines = _run(tmp_path, t1, home, sha)

    assert lines == ["owner/t1: taken " + sha[:8]], lines
    assert _git(t1, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(t1, "status", "--porcelain", "--untracked-files=no").stdout.strip() == ""
    assert (t1 / "config" / "orchestration.json").read_bytes() == bytes_before
    assert len(_core_update_commits(t1)) == 1
    assert _settings_of(t1)["core_version_count"] == count


# Row 4 -- a count held for an older head is not left behind when the new head's count cannot be had.
def test_row4_no_stale_count_survives_a_head_with_no_count(tmp_path: Path) -> None:
    home, sha, count = _home_with_commits(tmp_path, extra=1)
    t1 = _target(tmp_path, "t1")
    _run(tmp_path, t1, home, sha)
    assert _settings_of(t1)["core_version_count"] == count
    _write(home, "more.txt", "x")
    _git(home, "add", "-A")
    _git(home, "commit", "-q", "-m", "more")
    newer = _git(home, "rev-parse", "HEAD").stdout.strip()
    not_a_checkout = tmp_path / "plain"
    not_a_checkout.mkdir()

    lines = _run(tmp_path, t1, not_a_checkout, newer)

    assert lines == ["owner/t1: taken " + newer[:8]], lines
    settings = _settings_of(t1)
    assert settings["core_version"] == newer
    assert "core_version_count" not in settings, settings


# Rows 5-7 -- an update with no local counter (the dispatcher retry, the target's own start, the CLI) asks GitHub
# once for the count: the page number of the `rel="last"` Link header of `commits?sha=<sha>&per_page=1`.
class CountGh(FakeGh):
    """`FakeGh` plus the one count call. `link` is the Link header (None: no header); `mode` 'fail' makes the call
    exit 1, 'raise' makes the runner raise."""

    def __init__(self, head: str, link: str | None = None, mode: str = "ok") -> None:
        super().__init__(head=head)
        self.link = link
        self.mode = mode

    def __call__(self, args):
        if "commits?sha=" in args[1]:
            self.calls.append(list(args))
            if self.mode == "raise":
                raise RuntimeError("runner broke")
            if self.mode == "fail":
                return Completed(1, "", "gh: boom")
            head = "HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n"
            if self.link is not None:
                head += f"Link: {self.link}\r\n"
            return Completed(0, head + "\r\n[{}]\n")
        return super().__call__(args)


def _update_without_counter(folder: Path, gh: CountGh):
    return core_update.run_core_update(folder, gh_runner=gh, live_check=lambda: False)


LINK_41 = ('<https://api.github.com/repositories/1/commits?sha=x&per_page=1&page=2>; rel="next", '
           '<https://api.github.com/repositories/1/commits?sha=x&per_page=1&page=41>; rel="last"')


def test_row5_no_counter_takes_the_last_page_of_the_link_header(tmp_path: Path) -> None:
    t1 = _target(tmp_path, "t1")
    gh = CountGh(HEAD, link=LINK_41)

    _update_without_counter(t1, gh)

    assert _settings_of(t1)["core_version_count"] == 41
    count_calls = [call for call in gh.calls if "commits?sha=" in call[1]]
    assert len(count_calls) == 1 and "--include" in count_calls[0], gh.calls
    assert f"sha={HEAD}" in count_calls[0][1] and "per_page=1" in count_calls[0][1]


def test_row6_no_link_header_means_one_commit(tmp_path: Path) -> None:
    t1 = _target(tmp_path, "t1")

    _update_without_counter(t1, CountGh(HEAD, link=None))

    assert _settings_of(t1)["core_version_count"] == 1


def test_row7_a_failing_count_call_leaves_no_key_and_the_update_succeeds(tmp_path: Path) -> None:
    for mode in ("fail", "raise"):
        t1 = _target(tmp_path, f"t-{mode}")
        gh = CountGh(HEAD, link=LINK_41, mode=mode)

        result = _update_without_counter(t1, gh)

        assert result.home_head == HEAD
        settings = _settings_of(t1)
        assert settings["core_version"] == HEAD
        assert "core_version_count" not in settings, (mode, settings)
        assert len([call for call in gh.calls if "commits?sha=" in call[1]]) == 1, "never retried"
