"""#317 unit 2: put one area label on every ticket a plan reaches and make its native sub-issue links."""


import time

from tools.agentops.core_format import human_duration

PROGRESS_EVERY = 20


def estimate_line(n_writes: int, pace_seconds: float) -> str:
    return f"{n_writes} writes, about {human_duration(n_writes * pace_seconds)} at {pace_seconds:g} s each"


class _Counter:
    def __init__(self):
        self.n = 0

    def attach(self, parent, child_id):
        self.n += 1

    def add_label(self, n, label):
        self.n += 1

    def remove_label(self, n, label):
        self.n += 1


def count_writes(issues, plan, areas) -> int:
    """How many gh writes apply_plan would make now (already-applied items are not counted)."""
    c = _Counter()
    apply_plan(issues, plan, areas, c)
    return c.n


class ProgressWriter:
    """Wraps a writer; prints done / left / elapsed after every PROGRESS_EVERY writes."""

    def __init__(self, inner, total, emit=print, clock=time.monotonic):
        self.inner, self.total, self.emit, self.clock = inner, total, emit, clock
        self.done, self.start = 0, clock()

    def _tick(self):
        self.done += 1
        if self.done % PROGRESS_EVERY == 0:
            self.emit(
                f"progress: {self.done} done, {self.total - self.done} left, "
                f"{human_duration(self.clock() - self.start)} elapsed"
            )

    def attach(self, parent, child_id):
        self.inner.attach(parent, child_id)
        self._tick()

    def add_label(self, n, label):
        self.inner.add_label(n, label)
        self._tick()

    def remove_label(self, n, label):
        self.inner.remove_label(n, label)
        self._tick()


def _validate(issues, plan, areas, parents):
    roots = plan.get("roots", {})
    loners = plan.get("loners", {})
    for kind, d in (("roots", roots), ("loners", loners)):
        for k, a in d.items():
            if a not in areas:
                raise ValueError(f"{kind} #{k}: area {a!r} not in areas")
            if int(k) not in issues:
                raise ValueError(f"{kind} #{k}: no such ticket")
    for k in roots:
        if k in loners:
            raise ValueError(f"#{k} is in both roots and loners")
    seen = set()
    sim = dict(parents)
    for t, p in plan.get("links", []):
        for n in (t, p):
            if n not in issues:
                raise ValueError(f"link [{t}, {p}]: #{n} no such ticket")
        if t in seen:
            raise ValueError(f"link [{t}, {p}]: #{t} appears in two links")
        seen.add(t)
        cur, guard = p, 0
        while cur is not None and guard <= len(issues):
            if cur == t:
                raise ValueError(f"link [{t}, {p}] would make a cycle")
            cur, guard = sim.get(cur), guard + 1
        sim[t] = p


def apply_plan(issues, plan, areas, writer, dry_run=False):
    parents = {n: i.parent for n, i in issues.items()}
    _validate(issues, plan, areas, parents)
    target_of = {int(k): a for d in (plan.get("roots", {}), plan.get("loners", {})) for k, a in d.items()}
    out = []
    links = 0
    for t, p in plan.get("links", []):
        if parents[t] == p:
            continue
        out.append(f"link #{t} -> #{p}")
        if not dry_run:
            writer.attach(p, issues[t].id)
        parents[t] = p
        links += 1

    def target(n):
        seen = set()
        while n is not None and n not in seen:
            if n in target_of:
                return target_of[n]
            seen.add(n)
            n = parents.get(n)
        return None

    labelled = unchanged = 0
    missing = []
    for n in sorted(issues):
        tgt = target(n)
        if tgt is None:
            missing.append(n)
            continue
        have = issues[n].labels
        drop = [a for a in have if a in areas and a != tgt]
        add = tgt not in have
        if not drop and not add:
            unchanged += 1
            continue
        parts = []
        if add:
            parts.append(f"+{tgt}")
            if not dry_run:
                writer.add_label(n, tgt)
        for a in drop:
            parts.append(f"-{a}")
            if not dry_run:
                writer.remove_label(n, a)
        out.append(f"#{n}: " + " ".join(parts))
        labelled += 1
    if missing:
        out.append(f"not in the plan ({len(missing)})")
        out.extend(f"#{n}" for n in missing)
    out.append(f"links {links}, labelled {labelled}, unchanged {unchanged}, not in the plan {len(missing)}")
    return out
