"""`areas propose`: what a target's brain needs to propose its areas (issue #321). Read-only;
lists roots, loners and labels in use, sorts nothing by meaning. Stdlib only."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from tools.agentops.areas import AreasError, Issue

CORE_LABELS_PATH = Path(__file__).resolve().parents[2] / "config" / "core_labels.json"


def core_label_names() -> set[str]:
    return set(json.loads(CORE_LABELS_PATH.read_text(encoding="utf-8")))


def propose_text(issues: dict[int, Issue], known: set[str]) -> str:
    """`known` = core labels + areas + program_labels."""
    kids: dict[int, list[int]] = {}
    for n, i in issues.items():
        if i.parent is not None and i.parent in issues:
            kids.setdefault(i.parent, []).append(n)

    def descendants(n: int) -> list[int]:
        out, stack = [], list(kids.get(n, []))
        while stack:
            k = stack.pop()
            out.append(k)
            stack.extend(kids.get(k, []))
        return out

    roots = []
    loners = []
    for n, i in sorted(issues.items()):
        if i.parent is not None and i.parent in issues:
            continue
        d = descendants(n)
        if d:
            roots.append((len(d), n, d))
        else:
            loners.append(i)
    roots.sort(key=lambda r: (-r[0], r[1]))
    lines = ["roots:"]
    for cnt, n, d in roots:
        c = sum(1 for k in d if issues[k].state == "closed")
        lines.append(f"#{n} {issues[n].title}: {cnt} in the tree ({cnt - c} open, {c} closed)")
    lc = sum(1 for i in loners if i.state == "closed")
    lines.append(f"loners: {len(loners)} ({len(loners) - lc} open, {lc} closed)")
    used = Counter(lb for i in issues.values() for lb in i.labels)
    ordered = sorted(used.items(), key=lambda kv: (-kv[1], kv[0]))
    lines.append("labels in use:")
    lines.extend(f"{name} x{cnt}" for name, cnt in ordered)
    outside = [name for name, _ in ordered if name not in known]
    lines.append("outside the lists:")
    lines.extend(outside or ["none"])
    return "\n".join(lines)


def run_propose(settings: dict, gh) -> int:
    known = core_label_names() | set(settings.get("areas") or []) | set(settings.get("program_labels") or [])
    try:
        issues = gh.load_issues()
    except AreasError as exc:
        print(f"error: {exc}")
        return 1
    print(propose_text(issues, known))
    return 0
