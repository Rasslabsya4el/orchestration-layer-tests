"""#147: the bare-stem alias of a scripts/ or tools/ top file counts only when that stem names the target itself.
A string ``"from appkit.runs.store import x"`` names the src package ``appkit``, not ``scripts/appkit.py``."""
from tools.agentops.test_referrers import _Graph

_STRING = 'CODE = "from appkit.runs.store import x"\n'


def _sources(with_package: bool) -> dict[str, str]:
    src = {
        "scripts/appkit.py": "X = 1\n",
        "src/appkit/runs/store.py": "x = 1\n",
        "src/appkit/runs/rebalance.py": _STRING,
    }
    if with_package:
        src["src/appkit/__init__.py"] = ""
    return src


def test_a_string_import_of_a_src_package_is_not_a_referrer_of_the_same_named_script():
    refs = _Graph(_sources(with_package=True)).referrers("scripts/appkit.py")
    assert "src/appkit/runs/rebalance.py" not in refs


def test_the_same_string_without_the_package_still_makes_the_script_a_referred_target():
    srcs = _sources(with_package=False)
    refs = _Graph(srcs).referrers("scripts/appkit.py")
    assert "src/appkit/runs/rebalance.py" in refs


def test_a_nested_script_without_a_bare_name_keeps_its_alias():
    # scripts/sub/tool.py has no top-level name in the graph, so no other file owns the stem tool
    srcs = {"scripts/sub/tool.py": "X = 1\n", "src/app/run.py": 'CODE = "import tool"\n'}
    assert "src/app/run.py" in _Graph(srcs).referrers("scripts/sub/tool.py")


def test_a_file_that_puts_the_scripts_folder_on_the_import_path_still_refers_to_the_same_named_script():
    srcs = _sources(with_package=True)
    srcs["src/appkit/runs/rebalance.py"] = '"""Doc: scripts"""\nPATH = "scripts/jobs.py"\n' + _STRING
    srcs["tests/test_launcher_shadow.py"] = 'FOLDER = "scripts"\nCODE = "import appkit"\n'
    refs = _Graph(srcs).referrers("scripts/appkit.py")
    assert "tests/test_launcher_shadow.py" in refs
    assert "src/appkit/runs/rebalance.py" not in refs
