"""#41 unit 3: the selection rules of a built map -- the shared-file full-suite rule (rule 4), the fallback reason
``map commit not an ancestor`` (rule 2 / row 5) and the selected line ``selected N of T tests (map <sha8>)``."""
import pytest

from tests.test_select_build_map import SUITE, _git, _repo
from tools.agentops.test_select import build_map, map_dir, read_meta, select_for_diff


@pytest.mark.parametrize("path", ["tests/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg",
                                  "tools/agentops/test_select.py"])
def test_change_to_a_file_every_test_imports_runs_the_full_suite(tmp_path, path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("# touched\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    sel = select_for_diff(repo, sha, head, mdir)
    assert sel.full
    assert sel.line == "full suite: change touches a file every test imports"
    assert sel.ids == []


def test_map_from_a_commit_that_is_not_an_ancestor_of_the_tested_head_runs_the_full_suite(tmp_path):
    repo = tmp_path / "repo"
    base = _repo(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "pkg" / "a.py").write_text("def a():\n    return 1  # side\n")
    _git(repo, "commit", "-q", "-am", "side")
    side = _git(repo, "rev-parse", "HEAD")
    mdir = map_dir(tmp_path / "main")
    build_map(repo, side, mdir, SUITE)
    _git(repo, "checkout", "-q", "-")  # back to the branch the tested head sits on: side is not its ancestor
    (repo / "pkg" / "b.py").write_text("def b():\n    return 2  # main\n")
    _git(repo, "commit", "-q", "-am", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    sel = select_for_diff(repo, base, head, mdir)
    assert sel.full
    assert sel.line == "full suite: map commit not an ancestor"


def test_diff_touching_one_module_selects_its_tests_on_the_map_line(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    (repo / "pkg" / "b.py").write_text("def b():\n    return 2  # touched\n")
    _git(repo, "commit", "-q", "-am", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    sel = select_for_diff(repo, sha, head, mdir)
    assert not sel.full, sel.line
    assert sel.line == f"selected 1 of 4 tests (map {sha[:8]})"
    assert sel.ids == ["tests/test_b.py::test_b"]
    assert sel.total == 4 and sel.total > len(sel.ids)


@pytest.mark.parametrize("path", ["config/orchestration.json", "config/core_templates/x.stub",
                                  "tools/agentops/core_manifest.txt", "docs/agents/orch.md",
                                  ".claude/agents/worker.md", ".claude/skills/s/SKILL.md",
                                  "CLAUDE.md", "GLOSSARY.md"])
def test_change_to_a_file_no_test_imports_but_tests_read_runs_the_full_suite_as_a_data_change(tmp_path, path):
    """Brain's addition to rule 4 (#41): config/, the core manifest and the rule documents are read by tests
    through open(), so the testmon map cannot see them: the fourth allowed full-suite reason is ``data change``."""
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("touched\n")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    sel = select_for_diff(repo, sha, head, mdir)
    assert sel.full
    assert sel.line == "full suite: data change"


def test_a_plain_document_still_selects_no_full_suite(tmp_path):
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("n\n")
    _git(repo, "add", "docs/notes.md")
    _git(repo, "commit", "-q", "-m", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    assert not select_for_diff(repo, sha, head, mdir).full


def test_a_new_test_file_that_is_also_a_referrer_is_counted_once(tmp_path):
    """pkg/d.py is unmapped (nothing executes it), its referrer tests/test_d.py is a new file
    (status A). The file runs whole, 2 tests: N must be 2, not the 4 a double count gives."""
    repo = tmp_path / "repo"
    _repo(repo)
    (repo / "pkg" / "d.py").write_text("def d():\n    return 4\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1b")
    base = _git(repo, "rev-parse", "HEAD")
    mdir = map_dir(tmp_path / "main")
    build_map(repo, base, mdir, SUITE)
    assert read_meta(mdir)["total"] == 4
    (repo / "pkg" / "d.py").write_text("def d():\n    return 5\n")
    (repo / "tests" / "test_d.py").write_text(
        "from pkg.d import d\n\ndef test_d_one():\n    assert d() == 5\n\ndef test_d_two():\n    assert d() == 5\n")
    _git(repo, "add", "pkg/d.py", "tests/test_d.py")  # not -A: the map run leaves __pycache__ in the tree
    _git(repo, "commit", "-q", "-m", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    sel = select_for_diff(repo, base, head, mdir)
    assert not sel.full, sel.line
    assert sel.new_tests == 2
    assert sel.line == f"selected 2 of 4 tests in the map (+2 added since, map {base[:8]})", sel.line


def test_every_mapped_test_plus_k_new_ones_names_k_on_the_line(tmp_path):
    """all four mapped tests run (their three files change) and a new file adds 2 tests:
    N = 6 > T = 4, so the line must say the 2 are added since the map."""
    repo = tmp_path / "repo"
    sha = _repo(repo)
    mdir = map_dir(tmp_path / "main")
    build_map(repo, sha, mdir, SUITE)
    for name in ("a", "b", "c"):
        p = repo / "tests" / f"test_{name}.py"
        p.write_text(p.read_text() + "\n# touched\n")
    (repo / "tests" / "test_new.py").write_text(
        "def test_new_one():\n    assert True\n\ndef test_new_two():\n    assert True\n")
    _git(repo, "add", "tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_new.py")
    _git(repo, "commit", "-q", "-m", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    sel = select_for_diff(repo, sha, head, mdir)
    assert not sel.full, sel.line
    assert sel.line == f"selected 6 of 4 tests in the map (+2 added since, map {sha[:8]})", sel.line


def test_a_changed_manifest_entry_adds_the_core_check_own_tests_not_every_test_that_runs_the_check(tmp_path, monkeypatch):
    """#217: ``dispatch.py`` is a manifest entry and imports ``core_check.py``, so a test that merely runs
    the check's code is in ``code_deps[core_check.py]``. Only the tests whose subject is the core check (named
    ``core_check`` / ``manifest`` / ``leak`` / ``avoid_words``) join the diff's selection by that rule."""
    from tools.agentops import core_check
    monkeypatch.setattr(core_check, "read_manifest", lambda *a, **k: ["tools/agentops/dispatch.py"])
    repo = tmp_path / "repo"
    sha = _repo(repo)
    (repo / "tools" / "agentops").mkdir(parents=True)
    (repo / "tools" / "__init__.py").write_text("")
    (repo / "tools" / "agentops" / "__init__.py").write_text("")
    (repo / "tools" / "agentops" / "core_check.py").write_text("def check():\n    return 1\n")
    (repo / "tools" / "agentops" / "dispatch.py").write_text(
        "from tools.agentops import core_check\n\ndef go():\n    return core_check.check()\n")
    tests = {
        "test_agentops_no_window.py": "def test_guard():\n    assert True\n",
        "test_agentops_dispatch.py": "from tools.agentops import dispatch\n\ndef test_go():\n    assert dispatch.go() == 1\n",
        "test_issue103_core_check_x.py": "from tools.agentops import core_check\n\ndef test_own_a():\n    assert core_check.check() == 1\n",
        "test_issue779_manifest.py": "from tools.agentops import core_check\n\ndef test_own_b():\n    assert core_check.check() == 1\n",
        "test_port_thing.py": "from tools.agentops import core_check\n\ndef test_runs_the_check_only():\n    assert core_check.check() == 1\n",
    }
    for name, text in tests.items():
        (repo / "tests" / name).write_text(text)
    _git(repo, "add", "tools/__init__.py", "tools/agentops", "tests/test_*.py", "-f")
    _git(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "tools/agentops/__pycache__")
    _git(repo, "commit", "-q", "-m", "c1b")
    base = _git(repo, "rev-parse", "HEAD")
    mdir = map_dir(tmp_path / "main")
    build_map(repo, base, mdir, SUITE)
    (repo / "tools" / "agentops" / "dispatch.py").write_text(
        "from tools.agentops import core_check\n\ndef go():\n    return core_check.check()  # touched\n")
    _git(repo, "add", "tools/agentops/dispatch.py")  # not the tree: the map run leaves __pycache__ in it
    _git(repo, "commit", "-q", "-m", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    sel = select_for_diff(repo, base, head, mdir)
    assert not sel.full, sel.line
    assert "tests/test_issue103_core_check_x.py::test_own_a" in sel.ids
    assert "tests/test_issue779_manifest.py::test_own_b" in sel.ids
    assert "tests/test_agentops_dispatch.py::test_go" in sel.ids  # through code_deps[dispatch.py]
    assert "tests/test_port_thing.py::test_runs_the_check_only" not in sel.ids


def test_a_changed_manifest_document_also_adds_the_two_files_that_read_every_manifest_file(tmp_path, monkeypatch):
    """#221: ``test_issue1080_pocock_skill_check.py`` and ``test_issue1082_pocock_contract.py`` read the text of
    every manifest file, and their names do not match ``CORE_CHECK_TEST``; a changed manifest ``.md`` entry
    (no map row) selects both whole, beside the core check's own tests."""
    from tools.agentops import core_check
    monkeypatch.setattr(core_check, "read_manifest", lambda *a, **k: ["docs/notes.md"])
    repo = tmp_path / "repo"
    sha = _repo(repo)
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "notes.md").write_text("one\n")
    tests = {
        "test_issue93_leak_x.py": "def test_own_c():\n    assert True\n",
        "test_issue779_manifest.py": "def test_own_b():\n    assert True\n",
        "test_issue1080_pocock_skill_check.py": "def test_reads_all_a():\n    assert True\n",
        "test_issue1082_pocock_contract.py": "def test_reads_all_b():\n    assert True\n",
        "test_port_thing.py": "def test_unrelated():\n    assert True\n",
    }
    for name, text in tests.items():
        (repo / "tests" / name).write_text(text)
    _git(repo, "add", "docs/notes.md", "tests/test_*.py", "-f")
    _git(repo, "commit", "-q", "-m", "c1b")
    base = _git(repo, "rev-parse", "HEAD")
    mdir = map_dir(tmp_path / "main")
    build_map(repo, base, mdir, SUITE)
    (repo / "docs" / "notes.md").write_text("two\n")
    _git(repo, "add", "docs/notes.md")
    _git(repo, "commit", "-q", "-m", "c2")
    head = _git(repo, "rev-parse", "HEAD")
    sel = select_for_diff(repo, base, head, mdir)
    assert not sel.full, sel.line
    assert "tests/test_issue779_manifest.py::test_own_b" in sel.ids
    assert "tests/test_issue93_leak_x.py::test_own_c" in sel.ids
    assert "tests/test_issue1080_pocock_skill_check.py::test_reads_all_a" in sel.ids
    assert "tests/test_issue1082_pocock_contract.py::test_reads_all_b" in sel.ids
    assert "tests/test_port_thing.py::test_unrelated" not in sel.ids
