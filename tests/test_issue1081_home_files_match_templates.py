"""Issue #1081: the home's three target-owned files hold only headings Pocock's own templates have.

Pocock's setup writes these files at a target from the target's answers; the home's copies may hold
no extra `##` section (the rules this repo adds live in the core files, `docs/agents/workflow.md` and
the `docs/agents/local/` counterparts). The test reads the installed mattpocock-skills plugin's templates
and the home's three files, and asserts every `##` heading of a home file exists in its template.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.agentops import port

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR_PARTS = ("skills", "engineering", "setup-matt-pocock-skills")

#: home file -> the plugin template Pocock's setup writes it from.
HOME_TO_TEMPLATE = {
    "docs/agents/issue-tracker.md": "issue-tracker-github.md",
    "docs/agents/triage-labels.md": "triage-labels.md",
    "docs/agents/domain.md": "domain.md",
}


def _template_dir() -> Path:
    """The newest installed version folder of the plugin that holds the setup skill's templates."""
    try:
        cache = port.resolve_plugin(Path.home() / ".claude" / "settings.json")[1]  # read only
    except port.PortError as exc:
        pytest.skip(f"the mattpocock-skills plugin is not enabled and complete on this machine: {exc}")
    if not cache.is_dir():
        pytest.skip(f"the mattpocock-skills plugin is not installed: {cache} is absent")
    versions = sorted((p for p in cache.iterdir() if p.is_dir()), key=lambda p: p.name)
    for version in reversed(versions):
        candidate = version.joinpath(*TEMPLATE_DIR_PARTS)
        if candidate.is_dir():
            return candidate
    pytest.skip(f"no installed version of {cache} ships {'/'.join(TEMPLATE_DIR_PARTS)}")


def _headings(path: Path) -> set[str]:
    """Every `## ` heading of a markdown file, outside fenced code blocks."""
    found: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("## "):
            found.add(line[3:].strip())
    return found


@pytest.mark.parametrize("home_file", sorted(HOME_TO_TEMPLATE))
@pytest.mark.live
def test_every_home_heading_exists_in_the_pocock_template(home_file):
    """Checks each home file's headings exist in the installed mattpocock-skills plugin's template (`core pocock-check` covers it for the owner)."""
    template = _template_dir() / HOME_TO_TEMPLATE[home_file]
    assert template.is_file(), f"the plugin template is missing: {template}"
    home_headings = _headings(REPO_ROOT / home_file)
    template_headings = _headings(template)
    extra = sorted(home_headings - template_headings)
    assert not extra, (
        f"{home_file} has headings Pocock's {HOME_TO_TEMPLATE[home_file]} lacks: {extra}; "
        "move their rules to docs/agents/workflow.md or docs/agents/local/ (issue #1081)"
    )
