"""#343 unit 1 (rule 1): the tests repo's default branch holds every workflow file the core dispatches, and a send never
overwrites that branch. GitHub is faked (the ``gh`` runner: a dict of the default branch's workflow files); the send
runs real git in temp dirs with a local bare repo as the remote, except the one refusal that names a GitHub remote, which
is refused before any push. The registry is ``tools/agentops/workflow_sync.py``.

Tested here: (1) a registry file absent from the default branch is written, (2) an identical one is not written, (3) a
different one is written, (4) the send refuses the live default branch name, (5) the outside case: a planted registry
entry with no file in ``.github/workflows/`` turns the rule-1 test red and names the file, (6) every dispatch site of
``tools/agentops`` takes its workflow file name from the registry. Plus the two dispatch paths write the files before
they dispatch."""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.agentops import github_run, map_github, workflow_sync as ws
from tools.agentops.test_copy_send import send
from tools.agentops.core_check import read_manifest
from tools.agentops.util import AgentOpsError, no_window_flags

REPO = "owner/copy"  # the tests repo's slug, as the send and the dispatch see it
GH_REMOTE = "https://github.com/owner/copy.git"
WALL = datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc)
AGENTOPS = Path(__file__).resolve().parents[1] / "tools" / "agentops"
WORKFLOW_FILE_RE = re.compile(r"[\w.-]+\.yml")
PROBE_DEFAULT = ["api", f"repos/{REPO}", "--jq", ".default_branch"]
CLEAN = {"pkg/a.py": "A = 1\n", "tests/test_a.py": "def test_a():\n    assert True\n"}
PRODUCT = "Zorblax"
OWNER = "Ivan" + r"\s+Surname"


@pytest.fixture(autouse=True)
def _test_copy_is_the_repo(monkeypatch):
    # the repo named by test_copy_remote is the tests copy the sync writes to; the settings are faked, not read
    monkeypatch.setattr(ws, "_settings", lambda: {"test_copy_remote": GH_REMOTE})


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FakeGh:
    """The ``gh`` seam. ``files`` is the default branch's workflow files (name -> bytes), ``default`` the live default
    branch's name. Every call is recorded; a contents read of a missing file raises a 404 like gh does."""

    def __init__(self, files=None, default="trunk"):
        self.files = dict(files or {})
        self.default = default
        self.calls: list[list[str]] = []
        self.puts: list[tuple[str, dict]] = []

    def __call__(self, args, input=None):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["workflow", "run"]:
            return ""
        if args[:2] == ["run", "list"]:
            return json.dumps([{"databaseId": 7, "event": "workflow_dispatch",
                                "createdAt": "2026-10-29T10:00:05Z", "status": "queued"}])
        if args == PROBE_DEFAULT:
            return self.default + "\n"
        if args[:1] == ["api"] and args[1:3] == ["-X", "PUT"]:
            name = args[3].rsplit("/", 1)[1]
            body = json.loads(input)
            self.puts.append((name, body))
            self.files[name] = base64.b64decode(body["content"])
            return "{}"
        if args[:1] == ["api"]:
            name = args[1].split("?", 1)[0].rsplit("/", 1)[1]
            if name not in self.files:
                raise subprocess.CalledProcessError(1, args, output="", stderr="gh: Not Found (HTTP 404)")
            data = self.files[name]
            return json.dumps({"name": name, "sha": _blob_sha(data),
                               "content": base64.encodebytes(data).decode("ascii")})
        raise AssertionError(f"unexpected gh call {args}")


def _source(name: str) -> bytes:
    return (ws.source_dir() / name).read_bytes()


def _machine(tmp_path: Path, monkeypatch) -> None:
    machine = tmp_path / "machine"
    machine.mkdir()
    (machine / "test_copy_patterns.json").write_text(json.dumps({"product": PRODUCT, "owner-name": OWNER}),
                                                     encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(machine))


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, creationflags=no_window_flags()).stdout.strip()


def _repo(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _bare(tmp_path: Path) -> str:
    bare = tmp_path / "copy.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    return str(bare)


# (1) (2) (3): ensure_workflows writes what the default branch lacks or holds differently

def test_a_registry_file_absent_from_the_default_branch_is_written():
    gh = FakeGh(files={})
    written = ws.ensure_workflows(REPO, "trunk", gh)
    assert written == list(ws.DISPATCHED_WORKFLOWS)
    for name in written:
        bodies = [body for n, body in gh.puts if n == name]
        assert len(bodies) == 1 and "sha" not in bodies[0]  # a new file carries no sha
        assert bodies[0]["branch"] == "trunk"
        assert base64.b64decode(bodies[0]["content"]) == _source(name)


def test_an_identical_file_on_the_default_branch_is_not_written():
    gh = FakeGh(files={name: _source(name) for name in ws.DISPATCHED_WORKFLOWS})
    assert ws.ensure_workflows(REPO, "trunk", gh) == []
    assert gh.puts == []


def test_a_different_file_on_the_default_branch_is_written_over_with_its_sha():
    stale = b"name: stale\n"
    gh = FakeGh(files={"tests.yml": stale, "map.yml": _source("map.yml")})
    assert ws.ensure_workflows(REPO, "trunk", gh) == ["tests.yml"]
    ((name, body),) = gh.puts
    assert name == "tests.yml"
    assert body["sha"] == _blob_sha(stale)
    assert base64.b64decode(body["content"]) == _source("tests.yml")


def test_the_puts_go_to_the_contents_api_of_the_tests_repo_on_the_given_branch():
    gh = FakeGh(files={})
    ws.ensure_workflows(REPO, "trunk", gh)
    paths = [args for args in gh.calls if args[:3] == ["api", "-X", "PUT"]]
    assert paths and all(args[3].startswith(f"repos/{REPO}/contents/.github/workflows/") for args in paths)


# (4): the send refuses the live default branch name

def test_send_refuses_the_live_default_branch_name(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, CLEAN)
    gh = FakeGh(default="trunk")
    result = send(repo, "trunk", GH_REMOTE, runner=gh)
    assert not result.sent and result.sha is None
    assert ("test copy: branch trunk is the test copy's default branch, never overwritten, suite on the PC"
            in capsys.readouterr().out.splitlines())
    assert PROBE_DEFAULT in gh.calls  # the live name came from GitHub, not from a list in the code


def test_a_local_remote_has_no_github_default_to_read(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, CLEAN)
    bare = _bare(tmp_path)
    gh = FakeGh(default="trunk")
    result = send(repo, "trunk", bare, runner=gh)
    assert result.sent  # a local bare repo has no GitHub default branch: nothing to refuse by
    assert gh.calls == []


# (5): the rule-1 test, and the outside case of row 6 (a planted registry entry)

def _rule_one_holds() -> None:
    """Every dispatched file is in ``.github/workflows`` and an empty default branch is made to hold all of them."""
    missing = [name for name in ws.DISPATCHED_WORKFLOWS if not (ws.source_dir() / name).is_file()]
    assert not missing, f"dispatched workflow file(s) not in .github/workflows: {', '.join(missing)}"
    assert ws.ensure_workflows(REPO, "trunk", FakeGh(files={})) == list(ws.DISPATCHED_WORKFLOWS)


def test_every_dispatched_file_exists_in_the_repo_and_is_held():
    _rule_one_holds()


def test_a_planted_dispatched_file_without_a_source_is_red_and_names_it(monkeypatch):
    monkeypatch.setattr(ws, "DISPATCHED_WORKFLOWS", (*ws.DISPATCHED_WORKFLOWS, "planted-ghost.yml"))
    with pytest.raises(AssertionError, match="planted-ghost.yml"):
        _rule_one_holds()


# (6): every dispatch site takes its workflow file name from the registry

def _is_str(node, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


REGISTRY = {name for name, value in vars(ws).items() if isinstance(value, str) and value.endswith(".yml")}


def _is_registry_ref(value: ast.expr) -> bool:
    # ``workflow_sync.<registry constant>``, or a bare registry name
    if isinstance(value, ast.Attribute):
        return value.attr in REGISTRY and isinstance(value.value, ast.Name) and value.value.id == "workflow_sync"
    return isinstance(value, ast.Name) and value.id in REGISTRY


def _registry_aliases(tree: ast.Module) -> set[str]:
    # the module-level names holding a registry constant: an import of one, or ``X = workflow_sync.<constant>``
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("workflow_sync"):
            names |= {alias.asname or alias.name for alias in node.names if alias.name in REGISTRY}
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
              and _is_registry_ref(node.value)):
            names.add(node.targets[0].id)
    return names


def _dispatch_sites(path: Path) -> list[tuple[str, int, bool]]:
    # every ["workflow", "run", <name>, ...] list of the file: (file, line, the name is a registry constant)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _registry_aliases(tree)
    sites = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.List) and len(node.elts) >= 3 and _is_str(node.elts[0], "workflow")
                and _is_str(node.elts[1], "run")):
            continue
        arg = node.elts[2]
        if isinstance(arg, ast.Name):
            ok = arg.id in aliases
        elif isinstance(arg, ast.Attribute):
            ok = _is_registry_ref(arg)
        else:
            ok = False
        sites.append((path.name, node.lineno, ok))
    return sites


def _yml_literals(path: Path) -> list[tuple[str, int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [(path.name, node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and WORKFLOW_FILE_RE.fullmatch(node.value)]


def _scan(paths: list[Path]) -> tuple[list[str], int]:
    # the violations of the dispatch sites and the literal file names, and how many dispatch sites were seen
    violations: list[str] = []
    sites = 0
    for path in paths:
        found = _dispatch_sites(path)
        sites += len(found)
        violations += [f"{f}:{line} workflow name is not a registry constant" for f, line, ok in found if not ok]
        violations += [f"{f}:{line} literal {value!r}" for f, line, value in _yml_literals(path)]
    return violations, sites


def test_dispatch_sites_take_the_workflow_file_from_the_registry():
    paths = sorted(p for p in AGENTOPS.glob("*.py") if p.name != "workflow_sync.py")
    violations, sites = _scan(paths)
    assert violations == []
    assert sites >= 2  # not vacuous: the map build and the suite dispatch


def test_a_planted_dispatch_site_with_an_unregistered_name_is_red(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def go(gh, repo, unregistered):\n"
        "    gh([\"workflow\", \"run\", unregistered, \"-R\", repo])\n"
        "    gh([\"workflow\", \"run\", \"x.yml\", \"-R\", repo])\n"
        "    WORKFLOW = \"tests.yml\"\n"
        "    gh([\"workflow\", \"run\", WORKFLOW, \"-R\", repo])\n", encoding="utf-8")
    violations, sites = _scan([planted])
    assert sites == 3
    assert "planted.py:2 workflow name is not a registry constant" in violations
    assert "planted.py:5 workflow name is not a registry constant" in violations


# the dispatch paths write the registry's files before they dispatch

def test_the_map_dispatch_writes_its_workflow_file_before_the_dispatch():
    gh = FakeGh(files={})
    assert map_github.dispatch_map(REPO, "map-build/abc", gh, wall=lambda: WALL, sleep=lambda s: None) == 7
    put = next(i for i, args in enumerate(gh.calls) if args[:3] == ["api", "-X", "PUT"])
    dispatch = next(i for i, args in enumerate(gh.calls) if args[:2] == ["workflow", "run"])
    assert put < dispatch
    assert ["workflow", "run", ws.MAP_WORKFLOW, "-R", REPO, "--ref", "map-build/abc", "--json"] in gh.calls


def test_the_suite_dispatch_writes_its_workflow_file_before_the_dispatch():
    gh = FakeGh(files={})
    assert github_run.dispatch(REPO, "merge-suite/x", 2, gh, wall=lambda: WALL, sleep=lambda s: None) == 7
    put = next(i for i, args in enumerate(gh.calls) if args[:3] == ["api", "-X", "PUT"])
    dispatch = next(i for i, args in enumerate(gh.calls) if args[:2] == ["workflow", "run"])
    assert put < dispatch
    assert ["workflow", "run", ws.TESTS_WORKFLOW, "-R", REPO, "--ref", "merge-suite/x", "--json"] in gh.calls


# (7) the guard: only the test copy named by test_copy_remote is ever written to

def test_the_copy_slug_is_the_slug_of_the_test_copy_remote_setting():
    assert ws._copy_slug() == "owner/copy"


def test_ensure_workflows_writes_nothing_to_a_repo_that_is_not_the_test_copy():
    gh = FakeGh(files={})
    assert ws.ensure_workflows("owner/orch", "main", gh) == []
    assert gh.calls == [] and gh.puts == []


def test_prepare_dispatch_on_the_test_copy_reads_its_default_branch_and_writes_the_registry():
    gh = FakeGh(files={}, default="trunk")
    assert ws.prepare_dispatch(REPO, gh) == list(ws.DISPATCHED_WORKFLOWS)
    assert PROBE_DEFAULT in gh.calls
    assert all(name in gh.files for name in ws.DISPATCHED_WORKFLOWS)


def test_own_runners_never_touch_the_working_repo(monkeypatch):
    monkeypatch.setattr(ws, "_settings", lambda: {"test_copy_remote": "", "github_own_runners": True,
                                                  "repo": "owner/orch"})
    gh = FakeGh(files={}, default="main")
    assert ws.prepare_dispatch("owner/orch", gh) == []
    assert gh.calls == [] and gh.puts == []


# (2) failure paths: a non-404 failed gh call becomes an AgentOpsError whose first line is the first stderr line

class FailingGh(FakeGh):
    """A FakeGh whose calls ``fails`` selects raise a failed gh call with ``stderr`` (exit 1, nothing written)."""

    def __init__(self, stderr: str, fails, **kwargs):
        super().__init__(**kwargs)
        self.stderr = stderr
        self.fails = fails

    def __call__(self, args, input=None):
        if self.fails(list(args)):
            self.calls.append(list(args))
            raise subprocess.CalledProcessError(1, list(args), output="", stderr=self.stderr)
        return super().__call__(args, input=input)


FORBIDDEN = "gh: Resource not accessible by integration (HTTP 403)\nsee the docs"


def _is_read(args) -> bool:
    return args[:1] == ["api"] and "-X" not in args and "/contents/" in args[1]


def _is_put(args) -> bool:
    return args[:3] == ["api", "-X", "PUT"]


def test_a_403_on_the_read_writes_nothing_and_fails_with_its_first_stderr_line():
    gh = FailingGh(FORBIDDEN, _is_read, files={})
    with pytest.raises(AgentOpsError) as exc:
        ws.ensure_workflows(REPO, "trunk", gh)
    assert str(exc.value).splitlines()[0] == FORBIDDEN.splitlines()[0]
    assert gh.files == {} and gh.puts == []


def test_a_403_on_the_put_fails_with_its_cause_and_writes_nothing():
    gh = FailingGh(FORBIDDEN, _is_put, files={})
    with pytest.raises(AgentOpsError) as exc:
        ws.ensure_workflows(REPO, "trunk", gh)
    assert str(exc.value).splitlines()[0] == FORBIDDEN.splitlines()[0]
    assert gh.files == {}


@pytest.mark.parametrize("stderr", [
    "gh: Bad Gateway (HTTP 502)\nupstream answered 404 Not Found (HTTP 404) elsewhere",
    "gh: Forbidden (HTTP 403)\nrequest 404-abc was refused",
])
def test_a_404_that_is_not_on_the_status_line_is_a_failure(stderr):
    gh = FailingGh(stderr, _is_read, files={})
    with pytest.raises(AgentOpsError) as exc:
        ws.ensure_workflows(REPO, "trunk", gh)
    assert str(exc.value).splitlines()[0] == stderr.splitlines()[0]
    assert gh.puts == []


def _dispatch(site: str, gh):
    if site == "map":
        return map_github.dispatch_map(REPO, "map-build/abc", gh, wall=lambda: WALL, sleep=lambda s: None)
    return github_run.dispatch(REPO, "merge-suite/x", 2, gh, wall=lambda: WALL, sleep=lambda s: None)


@pytest.mark.parametrize("site", ["map", "suite"])
@pytest.mark.parametrize("fails", [_is_read, _is_put], ids=["read-403", "put-403"])
def test_a_failed_write_stops_both_dispatch_sites_before_the_dispatch(site, fails):
    gh = FailingGh(FORBIDDEN, fails, files={})
    with pytest.raises(AgentOpsError) as exc:
        _dispatch(site, gh)
    assert str(exc.value).splitlines()[0] == FORBIDDEN.splitlines()[0]
    assert not any(args[:2] == ["workflow", "run"] for args in gh.calls)


# (3) the manifest: the registry's files and this module are in the core manifest

def test_the_registry_files_and_the_module_are_in_the_core_manifest():
    entries = set(read_manifest())
    wanted = [f"{ws.WORKFLOW_DIR}/{name}" for name in ws.DISPATCHED_WORKFLOWS]
    wanted += ["tools/agentops/workflow_sync.py", "tests/test_issue343_workflow_sync.py"]
    assert [path for path in wanted if path not in entries] == []
