"""#340 unit 5: the Расходы tab (web/monitor/tab-cost.js) against the captured collector snapshots.

The expected values are computed here, in Python, from tests/fixtures/monitor/slow.json (the collector's
`slow()` cut) by the rules of the pane (mods/pob-slots/hooks/register.tsx costTab, kit.tsx usd/limPct/modelName):
they are not read back from the tab. The tab is rendered by node (tests/issue340_tab_cost_check.mjs).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TAB = ROOT / "web" / "monitor" / "tab-cost.js"
FIX = ROOT / "tests" / "fixtures" / "monitor"
CHECK = ROOT / "tests" / "issue340_tab_cost_check.mjs"

PERIODS = ("day", "week", "month")
GROUPS = ("roles", "spawn", "days")
GROUP_TABLES = {
    "roles": ["По ролям", "Роль на разных моделях", "По моделям: кто вызывает", "По виду токенов"],
    "spawn": ["ORCH: сам и кого запускает", "ORCH на разных моделях: результат",
              "autopilot: сам и кого запускает", "brain: сам и кого запускает", "Моды", "По чатам"],
    "days": ["По дням", "По тикетам"],
}
ALL_TABLES = [t for g in GROUPS for t in GROUP_TABLES[g]]
assert len(ALL_TABLES) == 12


# ---------------------------------------------------------------- expected values, from the fixture (the pane's rules)

def r_half(x: float) -> int:
    """JS Math.round for the values here (halves go up)."""
    return math.floor(x + 0.5)


def thousands(n: float) -> str:
    return f"{r_half(n):,}".replace(",", " ")


def usd(v: float) -> str:
    return "$" + (thousands(v) if v >= 100 else f"{v:.2f}")


def limpct(v: float, per) -> str | None:
    if per is None:
        return None
    p = v / per
    if p >= 10:
        return f"{r_half(p)} %"
    if p >= 0.1:
        return f"{p:.1f} %"
    if p > 0:
        return "<0.1 %"
    return "0 %"


def ddmm(v: str) -> str:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(v))
    return f"{m.group(3)}.{m.group(2)}" if m else str(v)


def model_name(mid: str) -> str:
    if mid == "<synthetic>":
        return "служебные сообщения"
    m = re.fullmatch(r"claude-([a-z]+)((?:-\d{1,3})+)(?:-\d{8})?", str(mid))
    if not m:
        return str(mid)
    return f"{m.group(1)[0].upper()}{m.group(1)[1:]} {m.group(2)[1:].replace('-', '.')}"


ROLE_NAME = {"orch": "ORCH", "other_sub": "другие субагенты", "other_chat": "другие чаты", "mod": "моды"}
KIND_WORD = {"input": "ввод", "cache_write": "запись в кэш", "cache_read": "чтение из кэша", "output": "вывод"}
ROLE_ORDER = ["brain", "orch", "autopilot", "worker", "worker-strong", "researcher-find", "researcher-how",
              "reviewer-standards", "reviewer-spec", "other_sub", "other_chat", "monitor"]


def role_name(k: str) -> str:
    return ROLE_NAME.get(k, k)


def mod_name(k: str) -> str:
    return "монитор" if k == "monitor" else k


def rm_name(x: dict) -> str:
    return mod_name(x["role"]) if x["group"] == "mod" else role_name(x["role"])


def row(name: str, cost: float, per) -> tuple:
    return (name, usd(cost), limpct(cost, per))


def tok_sum(x: dict) -> int:
    return sum(x["tokens"].values()) if isinstance(x.get("tokens"), dict) else int(x.get("tokens", 0))


def expected_tables(cut: dict, period: str, group: str) -> dict[str, list[tuple]]:
    """title -> ordered rows of (first cell, $ cell, % cell); a None cell is a column the table does not have."""
    per = cut["total"]["limit_usd_per_pct"]
    total = cut["total"]["cost_usd"]
    out: dict[str, list[tuple]] = {}

    def spawn_rows(key: str) -> list[tuple]:
        sp = next((x for x in cut["by_spawner"] if x["key"] == key), None)
        if sp is None:
            return []
        return [row("сам", sp["own"]["cost_usd"], per)] + [row(role_name(r["key"]), r["cost_usd"], per) for r in sp["roles"]]

    if group == "roles":
        out["По ролям"] = [row(role_name(x["key"]), x["cost_usd"], per) for x in cut["by_role"]]
        rm = [x for x in cut.get("by_role_model", []) if tok_sum(x) > 0 or x["cost_usd"] > 0]
        rank = lambda r: ROLE_ORDER.index(r) if r in ROLE_ORDER else len(ROLE_ORDER)  # noqa: E731

        def by_first(a, b):
            return ((a["first"] is None), a["first"] or "", a["model"])

        rm_sorted = sorted(rm, key=lambda x: (rank(x["role"]), x["role"] if rank(x["role"]) == len(ROLE_ORDER) else "",
                                              (x["first"] is None), x["first"] or "", x["model"]))
        out["Роль на разных моделях"] = [
            ((rm_name(x) if (i == 0 or rm_sorted[i - 1]["role"] != x["role"]) else ""), model_name(x["model"]),
             usd(x["cost_usd"]), None)
            for i, x in enumerate(rm_sorted)]
        models: list[tuple] = []
        rm_kept = rm
        for m in cut["by_model"]:
            if not (tok_sum(m) > 0 or m["cost_usd"] > 0):
                continue
            models.append(row(model_name(m["key"]), m["cost_usd"], per))
            for r in rm_kept:
                if r["model"] == m["key"]:
                    models.append(row(rm_name(r), r["cost_usd"], per))
        out["По моделям: кто вызывает"] = models
        out["По виду токенов"] = [row(KIND_WORD.get(k["key"], k["key"]), k["cost_usd"], per)
                                  for k in sorted(cut["by_kind"], key=lambda k: -k["cost_usd"])]
    if group == "spawn":
        orch_rm = [x for x in cut.get("by_role_model", []) if (tok_sum(x) > 0 or x["cost_usd"] > 0)
                   and x["role"] == "orch" and x["led"] > 0]
        orch_rm.sort(key=lambda x: ((x["first"] is None), x["first"] or "", x["model"]))
        out["ORCH: сам и кого запускает"] = spawn_rows("orch")
        out["ORCH на разных моделях: результат"] = [
            (model_name(x["model"]), usd((x["cost_usd"] + x["spawned"]["cost_usd"]) / x["led"]), None) for x in orch_rm]
        out["autopilot: сам и кого запускает"] = spawn_rows("autopilot")
        out["brain: сам и кого запускает"] = spawn_rows("brain")
        out["Моды"] = [row(mod_name(x["key"]), x["cost_usd"], per) for x in cut["by_mod"]]
        chats = [x for x in cut["by_who"] if x["kind"] in ("chat", "untitled")]
        who = []
        for x in chats[:10]:
            name = "чаты без названия" if x["kind"] == "untitled" else " ".join(x["key"].split())
            who.append(row(name, x["cost_usd"], per))
        others = chats[10:]
        if others:
            who.append(row(f"остальные ({len(others)})", sum(x["cost_usd"] for x in others), per))
        out["По чатам"] = who
    if group == "days":
        out["По дням"] = [row(ddmm(d["key"]), d["cost_usd"], per)
                          for d in sorted(cut["by_day"], key=lambda d: d["key"], reverse=True)]
        issues = [(f"#{x['number']}", x.get("title") or "", limpct(x["cost_usd"], per), usd(x["cost_usd"]))
                  for x in cut["by_issue"]]
        rest = cut["issue_rest"]
        if rest["n"] > 0:
            issues.append(("", f"остальные тикеты ({rest['n']})", limpct(rest["cost_usd"], per), usd(rest["cost_usd"])))
        none = cut["no_issue"]
        issues.append(("", "без тикета", limpct(none["cost_usd"], per), usd(none["cost_usd"])))
        out["По тикетам"] = issues
    return out


# ---------------------------------------------------------------- the rendered html, parsed

class _Tables(HTMLParser):
    """Each <section data-table=TITLE> -> rows; a row is <div class="tr"> (the .th head row is skipped); a cell is a
    <span>: its data-c ('usd', 'pct', ...) or None. The first cell is the row's name."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, list[list[tuple]]] = {}
        self.error_text = None
        self._table = None
        self._row = None
        self._cell = None
        self._in_error = False
        self._err_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and "data-table" in a:
            self._table = a["data-table"]
            self.tables[self._table] = []
        if tag == "section" and "data-error" in a:
            self._in_error = True
        if tag == "div" and self._table is not None and "tr" in (a.get("class") or "").split():
            self._row = None if "th" in (a.get("class") or "").split() else []
            if self._row is not None:
                self.tables[self._table].append(self._row)
        if tag == "span" and self._row is not None:
            self._cell = [a.get("data-c"), ""]

    def handle_endtag(self, tag):
        if tag == "span" and self._cell is not None and self._row is not None:
            self._row.append((self._cell[0], self._cell[1]))
            self._cell = None
        if tag == "div" and self._row is not None:
            self._row = None
        if tag == "section":
            self._table = None
            if self._in_error:
                self.error_text = "".join(self._err_parts)
                self._in_error = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell[1] += data
        if self._in_error:
            self._err_parts.append(data)


def parse(html: str) -> _Tables:
    p = _Tables()
    p.feed(html)
    p.close()
    return p


def cells_of(rows: list[list[tuple]]) -> list[tuple]:
    """(name, $ cell, % cell) per row, from the cells the tab tags: data-c='usd' and data-c='pct'."""
    out = []
    for cells in rows:
        name = cells[0][1] if cells else ""
        usd_cells = [t for c, t in cells if c == "usd"]
        pct_cells = [t for c, t in cells if c == "pct"]
        out.append((name, usd_cells[0] if usd_cells else None, pct_cells[0] if pct_cells else None))
    return out


# ---------------------------------------------------------------- the node run

def _run_check() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # no console window of its own
    res = subprocess.run([node, str(CHECK), str(TAB), str(FIX)], capture_output=True, text=True,
                         encoding="utf-8", timeout=120, creationflags=flags)
    assert res.returncode == 0, f"node failed: {res.stderr[-2000:]}"
    return json.loads(res.stdout)


@pytest.fixture(scope="module")
def run():
    return _run_check()


@pytest.fixture(scope="module")
def cut_by_period():
    slow = json.loads((FIX / "slow.json").read_text(encoding="utf-8"))
    return {p: slow[f"cost_{p}"]["cut"] for p in PERIODS}


def test_module_contract(run):
    m = run["module"]
    assert m["id"] == "cost"
    assert m["title"] == "Расходы"
    assert m["order"] == 30
    assert m["endpoints"] == ["slow"]


def test_all_twelve_tables_present_for_each_period_and_grouping(run, cut_by_period):
    """N of N: for each period (3), the tables of each grouping are exactly the pane's, and the 12 of the three
    groupings together are all present."""
    checked = 0
    total = 0
    for p in PERIODS:
        seen = set()
        for g in GROUPS:
            total += 1
            html = run["renders"][f"{p}/{g}"]
            titles = set(parse(html).tables)
            assert titles == set(GROUP_TABLES[g]), f"{p}/{g}: tables {sorted(titles)}"
            checked += 1
            seen |= titles
        total += 1
        assert seen == set(ALL_TABLES)
        checked += 1
    print(f"\ntables present: {checked} of {total} period x grouping checks")
    assert checked == total


def test_cost_and_limit_cells_equal_the_fixture(run, cut_by_period):
    """N of N: every row of every table, for each period and grouping: the name, the $ cell and the % cell equal the
    values computed from the fixture."""
    checked = 0
    total = 0
    for p in PERIODS:
        cut = cut_by_period[p]
        for g in GROUPS:
            expected = expected_tables(cut, p, g)
            parsed = parse(run["renders"][f"{p}/{g}"]).tables
            for title in GROUP_TABLES[g]:
                exp_rows = expected[title]
                got = cells_of(parsed[title])
                total += 1
                assert len(got) == len(exp_rows), f"{p}/{g} '{title}': {len(got)} rows, fixture has {len(exp_rows)}"
                checked += 1
                for i, (e, o) in enumerate(zip(exp_rows, got)):
                    # the expected tuple: (name, $, %) for the cards; a 4-tuple for the issue table (name, title, %, $)
                    if title == "По тикетам":
                        e_name, e_title, e_pct, e_usd = e
                        assert o[0] == e_name and o[1] == e_usd and o[2] == e_pct, \
                            f"{p}/{g} '{title}' row {i}: got {o}, fixture {e}"
                    elif title == "Роль на разных моделях":
                        e_name, _model, e_usd, _ = e
                        assert o[0] == e_name and o[1] == e_usd, f"{p}/{g} '{title}' row {i}: got {o}, fixture {e}"
                    else:
                        e_name, e_usd, e_pct = e
                        assert o[0] == e_name, f"{p}/{g} '{title}' row {i}: name {o[0]!r}, fixture {e_name!r}"
                        assert o[1] == e_usd, f"{p}/{g} '{title}' row {i}: $ {o[1]!r}, fixture {e_usd!r}"
                        assert o[2] == e_pct, f"{p}/{g} '{title}' row {i}: % {o[2]!r}, fixture {e_pct!r}"
                    checked += 1
                    total += 1
    print(f"\ncells equal the fixture: {checked} of {total} checks")
    assert checked == total


def test_headline_and_limit_share_match_the_fixture(run, cut_by_period):
    """The period's headline names the total $ and the share of the limit (weekly for a day or a week, monthly for a
    month), the way the pane words it."""
    for p in PERIODS:
        cut = cut_by_period[p]
        html = run["renders"][f"{p}/roles"]
        total = cut["total"]
        assert usd(total["cost_usd"]) in html
        if total["limit_pct"] is not None:
            word = "месячного" if total.get("limit_days", 7) > 7 else "недельного"
            assert f"{r_half(total['limit_pct'])} % {word} лимита" in html, p


def test_no_plan_drops_the_percent_column(run):
    """Without a plan price (limit_usd_per_pct None) no % cell and no % header is drawn; the $ cells stay."""
    html = run["errors"]["noPlan"]
    parsed = parse(html).tables
    assert "По ролям" in parsed
    for title, rows in parsed.items():
        for cells in rows:
            assert all(c != "pct" for c, _t in cells), f"'{title}' still has a % cell"
            assert any(c == "usd" for c, _t in cells), f"'{title}' lost its $ cell"
    assert "% лимита" not in html


def test_error_card_shows_the_read_reason(run):
    """A failed slow read with no cut draws the error card with the reason; no table is drawn."""
    p = parse(run["errors"]["readFailed"])
    assert p.error_text is not None, "no error card"
    assert "Расходы не посчитаны" in run["errors"]["readFailed"]
    assert "gh: сервер не отвечает" in p.error_text
    assert p.tables == {}


def test_cut_error_is_named_in_the_error_card(run):
    p = parse(run["errors"]["cutFailed"])
    assert p.error_text is not None
    assert "bad cut" in p.error_text
    assert p.tables == {}


def test_stale_read_keeps_the_tables_and_names_the_error(run):
    """A failed slow read with the old data held: the tables stay, and one line above them says the data is old and why."""
    html = run["errors"]["stale"]
    line = "Данные устарели: gh: сервер не отвечает"
    assert line in html
    assert html.index(line) < html.index("data-table="), "the stale line must sit above the tables"
    p = parse(html)
    assert p.error_text is None, "old data is not an error card"
    assert "По ролям" in p.tables


def test_ticket_rows_carry_a_state_dot_only_on_issues(run, cut_by_period):
    """Each ticket of the cut has one state dot (the tickets tab's dot), the rest rows have none."""
    html = run["renders"]["week/days"]
    issues = cut_by_period["week"]["by_issue"]
    assert html.count('<i class="d"') == len(issues)
    assert 'data-tip="closed"' in html


def test_ticket_numbers_link_to_the_chosen_repos_issues(run, cut_by_period):
    """With ui.repo set, #n is a link to https://github.com/<label>/issues/<n>; without it the number is plain text."""
    n = cut_by_period["week"]["by_issue"][0]["number"]
    linked = run["links"]
    assert f'href="https://github.com/Rasslabsya4el/orchestration-layer/issues/{n}"' in linked
    assert f">#{n}</a>" in linked
    plain = run["renders"]["week/days"]
    assert "href=" not in plain
    assert f">#{n}</span>" in plain


def test_no_answer_yet_is_a_waiting_line_not_an_error(run):
    html = run["errors"]["waiting"]
    assert "загружается" in html
    assert parse(html).error_text is None


def test_period_and_grouping_switches_are_drawn(run):
    """Each period and each grouping is a control; the chosen one is pressed (the controls carry their values)."""
    html = run["renders"]["week/roles"]
    for p in PERIODS:
        assert f'data-cost-period="{p}"' in html
    for g in GROUPS:
        assert f'data-cost-group="{g}"' in html
    assert re.search(r'data-cost-period="week" aria-pressed="true"', html)
    assert re.search(r'data-cost-group="roles" aria-pressed="true"', run["renders"]["month/spawn"]) is None
    assert re.search(r'data-cost-group="spawn" aria-pressed="true"', run["renders"]["month/spawn"])
