"""`agentops research <brief.md> --model luna|terra [--issue N]` (requirement 2).

Runs the brief through the opencode bridge with web search on (`OPENCODE_ENABLE_EXA=1`), writes
the report file the brief names, and rejects a report that cites no reddit.com URL -- the
GPT-era harness's rule (now removed, #65), kept.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import ocserver
from .metering import record_run
from .worktree import ROOT

RUNS = ROOT / "artifacts" / "runs"
REPORT_NAME = re.compile(r"`(docs/research/[^`]+\.md)`")
MODEL_ALIASES = {"terra": "gpt-5.6-terra", "luna": "gpt-6-luna"}

TAIL = (
    "\n\nUse live web search for every question; open Reddit threads (reddit.com / old.reddit.com), "
    "the wiki and forum posts, not tooltips alone. Report format, per question: the VERBATIM passages "
    "that answer it, each with its URL, the post date and who says it, the conditions the sources "
    "attach, where sources disagree both sides quoted, then one line 'what it says' with no "
    "recommendation. Do not write any other file: put the complete final report in this message, "
    "in markdown."
)

# `--no-reddit` (#488): a methodology brief's sources are papers, official docs, engineering blogs.
TAIL_NO_REDDIT = (
    "\n\nUse live web search for every question; open the primary sources (papers, official docs, "
    "engineering blogs, the wiki), not tooltips alone. Report format, per question: the VERBATIM passages "
    "that answer it, each with its URL, the post date and who says it, the conditions the sources "
    "attach, where sources disagree both sides quoted, then one line 'what it says' with no "
    "recommendation. Do not write any other file: put the complete final report in this message, "
    "in markdown."
)


def report_path_from_brief(brief: Path) -> Path | None:
    found = [m for m in REPORT_NAME.findall(brief.read_text(encoding="utf-8")) if "/briefs/" not in m]
    return ROOT / found[-1] if found else None


def report_is_good(text: str, *, require_reddit: bool = True) -> bool:
    """A report from tooltips alone is a failed task (issue #46 tests: "the reddit-URL
    rejection"). Ported rule from the GPT-era harness's `good` (now removed, #65)."""
    if len(text) <= 500:
        return False
    if text.count("http") < 3 or len(text) < 3000:
        return False
    return (not require_reddit) or "reddit.com" in text


class ResearchResult:
    def __init__(self, *, slug: str, report_path: str | None, accepted: bool, reason: str,
                text: str, elapsed_s: float, tokens_in: int = 0, tokens_out: int = 0):
        self.slug = slug
        self.report_path = report_path
        self.accepted = accepted
        self.reason = reason
        self.text = text
        self.elapsed_s = elapsed_s
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


def run_research(brief_path: Path, *, model: str = "terra", issue: int | None = None,
                 minutes: float = 60.0, require_reddit: bool = True, config_path: Path | None = None,
                 directory: Path | None = None) -> ResearchResult:
    brief_path = Path(brief_path)
    if not brief_path.is_file():
        raise SystemExit(f"no brief file {brief_path}")
    resolved_model = MODEL_ALIASES.get(model, model)
    slug = brief_path.stem
    target = report_path_from_brief(brief_path)
    if target is None:
        raise SystemExit(f"{brief_path} names no `docs/research/<name>.md` report path")

    directory = directory or ROOT
    started = time.monotonic()
    client = ocserver.ensure(config_path=config_path, enable_exa=True)
    session = client.create_session(str(directory), title=f"agentops-research-{slug}")
    session_id = session["id"]
    ocserver.register(session_id, directory=str(directory), model=resolved_model, task=f"research-{slug}")

    table = ocserver.read_opencode_config(config_path)
    providers = table.get("provider", {})
    provider_id = next(iter(providers), None)

    brief_text = brief_path.read_text(encoding="utf-8")
    client.prompt_async(str(directory), session_id, agent="agentops-research", provider_id=provider_id,
                        model=resolved_model, system="", text=brief_text + (TAIL if require_reddit else TAIL_NO_REDDIT))

    ocserver.wait_for_idle(client, str(directory), session_id, minutes=minutes)

    data = client.messages(str(directory), session_id)
    text = ""
    for item in reversed(data):
        if (item.get("info") or {}).get("role") == "assistant":
            text = "\n".join(p.get("text", "") for p in item.get("parts", []) if p.get("type") == "text").strip()
            break
    usage = client.usage(str(directory), session_id)
    error = client.last_error(str(directory), session_id)
    ocserver.unregister(session_id)

    # A model call that produced no tokens, an error, or an empty reply is a call failure, never
    # scored as a rejected report (issue #46 follow-up, 2026-09-26: it was previously indistinguishable
    # from "no reddit.com URL" -- 0 tokens read as an ordinary rejection, hiding that no real call
    # happened at all).
    if error:
        accepted, reason = False, f"model call failed: {error}"
    elif not usage:
        accepted, reason = False, "model call failed: no assistant reply (0 tokens)"
    elif not text:
        accepted, reason = False, "model call failed: empty reply"
    else:
        accepted = report_is_good(text, require_reddit=require_reddit)
        reason = "accepted" if accepted else ("no reddit.com URL" if require_reddit and "reddit.com" not in text else "report too thin")
    if accepted:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    usage = usage or {}
    elapsed_s = time.monotonic() - started
    record_run(kind="research", model=resolved_model, outcome=reason, wall_s=elapsed_s,
              tokens_in=usage.get("input", 0), tokens_out=usage.get("output", 0), issue=issue,
              session=session_id)

    return ResearchResult(slug=slug, report_path=str(target) if accepted else None, accepted=accepted,
                          reason=reason, text=text, elapsed_s=elapsed_s, tokens_in=usage.get("input", 0),
                          tokens_out=usage.get("output", 0))
