// Расходы tab (#340 unit 5): the pane's Cost tab (the monitor mod's hooks/register.tsx costTab) as a pure render.
// Reads the `slow` mode (cost_day / cost_week / cost_month: the collector's cut) and the period and grouping chosen in
// ui.state (period day|week|month, default week; grouping roles|spawn|days, default roles). Every table is drawn whole.
// Tests: tests/test_issue340_tab_cost.py (through tests/issue340_tab_cost_check.mjs).

export const id = "cost";
export const title = "Расходы";
export const order = 30;
export const endpoints = ["slow"];

const PERIODS = [["day", "День"], ["week", "Неделя"], ["month", "Месяц"]];
const GROUPS = [["roles", "Роли и модели"], ["spawn", "Кто кого запускает"], ["days", "Дни и тикеты"]];
const ROLE_NAME = { orch: "ORCH", other_sub: "другие субагенты", other_chat: "другие чаты", mod: "моды" };
const ROLE_ORDER = ["brain", "orch", "autopilot", "worker", "worker-strong", "researcher-find", "researcher-how",
  "reviewer-standards", "reviewer-spec", "other_sub", "other_chat", "monitor"];
const KIND_WORD = { input: "ввод", cache_write: "запись в кэш", cache_read: "чтение из кэша", output: "вывод" };
const MODEL_WORD = { "<synthetic>": "служебные сообщения" };
// The tickets tab's state dots (copied from web/monitor/tab-tickets.js: its STATE_WORD and dot). The held `work` state
// needs the tickets read, which this tab does not take, so no ticket is drawn held.
const STATES = ["owner", "ready", "blocked", "open", "triage", "closed", "dropped"];
const WORD = {
  owner: "ready-for-human", blocked: "blocked", ready: "ready-for-agent",
  triage: "needs-triage", open: "open", closed: "closed", dropped: "not planned",
};
const DOT = {
  owner: "background:var(--acc)",
  ready: "background:var(--ready)",
  blocked: "background:var(--ag)",
  triage: "background:var(--fnt)",
  open: "background:none;box-shadow:inset 0 0 0 1.5px var(--mut)",
  closed: "background:#48484a",
  dropped: "background:none;box-shadow:inset 0 0 0 1.5px var(--fnt);opacity:.6",
};

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const rHalf = x => Math.floor(x + 0.5);
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const sumBy = (xs, f) => xs.reduce((a, x) => a + f(x), 0);

// A plain space groups the thousands (the pane's `thousands`).
const thousands = n => String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
const usd = v => `$${v >= 100 ? thousands(v) : Number(v).toFixed(2)}`;
const tokH = n => (n >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n));
// A cost as a share of the plan's limit; null when there is no plan price.
const limPct = (v, per) => {
  if (per == null) return null;
  const p = v / per;
  return p >= 10 ? `${rHalf(p)} %` : p >= 0.1 ? `${p.toFixed(1)} %` : p > 0 ? "<0.1 %" : "0 %";
};
const ddmm = v => { const m = /(\d{4})-(\d{2})-(\d{2})/.exec(String(v)); return m ? `${m[3]}.${m[2]}` : String(v); };
const modelName = id => {
  const s = String(id);
  if (MODEL_WORD[s]) return MODEL_WORD[s];
  const m = /^claude-([a-z]+)((?:-\d{1,3})+)(?:-\d{8})?$/.exec(s);
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2].slice(1).split("-").join(".")}` : s;
};
const plural = (n, a, b, c) => { const m100 = n % 100, m10 = n % 10; return m100 >= 11 && m100 <= 14 ? c : m10 === 1 ? a : m10 >= 2 && m10 <= 4 ? b : c; };
const rate = v => (v >= 10 ? thousands(v) : v >= 0.05 ? v.toFixed(1) : v > 0 ? "<0.1" : "0");
const perRun = v => (v <= 0 ? "$0" : v < 0.0005 ? "<$0.001" : v < 0.1 ? `$${v.toFixed(3)}` : usd(v));
const roleName = k => ROLE_NAME[k] ?? k;
const modName = k => (k === "monitor" ? "монитор" : k);
const roleRank = r => { const i = ROLE_ORDER.indexOf(r); return i < 0 ? ROLE_ORDER.length : i; };
const subNote = v => (v >= 0.005 ? `субагенты ${usd(v)}` : "");
const dateSpan = x => (x.first == null ? "" : x.first === x.last || x.last == null ? ddmm(x.first) : `${ddmm(x.first)} – ${ddmm(x.last)}`);
const daysIn = x => (x.first == null ? 1 : Math.max(1, Math.round((Date.parse(x.last ?? x.first) - Date.parse(x.first)) / 86400000) + 1));
const byFirst = (a, b) => ((a.first == null ? 1 : 0) - (b.first == null ? 1 : 0)) || cmp(a.first ?? "", b.first ?? "") || cmp(a.model, b.model);
// The pane's stateOf without the held set: a closed not-planned ticket is `dropped`, else its state.
const ticketState = x => (!x.open && x.reason === "NOT_PLANNED" ? "dropped" : STATES.includes(x.state) ? x.state : "open");
const ticketDot = x => {
  const st = ticketState(x);
  return `<i class="d" data-tip="${esc(WORD[st])}" style="${DOT[st]};display:inline-block;margin-right:6px;vertical-align:-1px"></i>`;
};

// One table cell: k is name (the row's first cell; `lead` is raw markup before its text, `href` links the text), txt,
// num, usd (a $ figure), pct (a % of the limit), bar (a share).
function cellHtml(c) {
  if (c.k === "bar") return `<span class="bar"><i style="width:${(Math.max(0, c.t) * 100).toFixed(2)}%"></i></span>`;
  if (c.k === "usd") return `<span class="num" data-c="usd">${esc(c.t)}</span>`;
  if (c.k === "pct") return `<span class="num" data-c="pct">${esc(c.t)}</span>`;
  if (c.k === "name") {
    const text = c.href ? `<a href="${esc(c.href)}" target="_blank" rel="noopener">${esc(c.t)}</a>` : esc(c.t);
    return `<span class="c-ti">${c.lead ?? ""}${text}</span>`;
  }
  if (c.k === "txt") return `<span class="c-st">${esc(c.t)}</span>`;
  return `<span class="num">${esc(c.t)}</span>`;
}

function tableHtml(title, head, rows, sub) {
  const headRow = `<div class="tr th"><i></i>${head.map((h, i) => `<span class="${i === 0 ? "c-ti" : "num"}">${esc(h)}</span>`).join("")}</div>`;
  const body = rows.map(r => `<div class="tr${r.sub ? " sub" : ""}"><i></i>${r.cells.map(cellHtml).join("")}</div>`).join("");
  return `<section class="w full" data-table="${esc(title)}"><div class="lb"><b>${esc(title)}</b>${sub ? `<span class="r">${esc(sub)}</span>` : ""}</div>`
    + `<div class="tblw"><div class="tbl">${headRow}${body}</div></div></section>`;
}

function switchesHtml(period, group) {
  const periods = PERIODS.map(([v, l]) =>
    `<button type="button" data-cost-period="${v}" aria-pressed="${v === period}" data-set="period:${v}">${l}</button>`).join("");
  const groups = GROUPS.map(([v, l]) =>
    `<button type="button" data-cost-group="${v}" aria-pressed="${v === group}" data-set="grouping:${v}">${l}</button>`).join("");
  return `<div class="repos" role="group" aria-label="период">${periods}</div><div class="repos" role="group" aria-label="группа">${groups}</div>`;
}

export function render(snapshot, ui) {
  const st = ui?.state ?? {};
  const period = PERIODS.some(p => p[0] === st.period) ? st.period : "week";
  const group = GROUPS.some(g => g[0] === st.grouping) ? st.grouping : "roles";
  const slow = snapshot?.slow ?? {};
  const c = slow.data ? slow.data[`cost_${period}`] : undefined;
  const cut = c?.cut;
  const head = switchesHtml(period, group);
  if (!cut) {
    const why = slow.error ?? c?.cut_error;
    if (why) {
      return head + `<section class="w full" data-error="cost"><div class="lb"><b>Расходы не посчитаны</b></div><div class="c-st">${esc(why)}</div></section>`;
    }
    return head + `<section class="w full"><div class="lb"><b>Расходы</b></div><div class="c-st">загружается</div></section>`;
  }

  // the chosen repo's issue links: the label of its entry in snapshot.repos; no `/` in it means plain numbers
  const repos = Array.isArray(snapshot?.repos) ? snapshot.repos : [];
  const want = ui?.repo != null ? String(ui.repo) : null;
  const entry = want == null ? null : repos.find(r => r && String(r.id) === want);
  const label = entry ? String(entry.label ?? "") : "";
  const issueBase = label.includes("/") ? `https://github.com/${label}/issues/` : null;

  const per = cut.total.limit_usd_per_pct ?? null;
  const total = cut.total.cost_usd;
  const allTok = Object.values(cut.total.tokens ?? {}).reduce((a, b) => a + b, 0);
  const p = cut.total.limit_pct ?? null;
  const limWord = (cut.total.limit_days ?? 7) > 7 ? "месячного" : "недельного";
  const lim = p == null ? null : `≈ ${thousands(p)} % ${limWord} лимита`;
  const headLine = [usd(total), lim, `${tokH(allTok)} токенов`].filter(Boolean).join(" · ");
  const periodText = ddmm(cut.since) === ddmm(cut.until) ? ddmm(cut.since) : `${ddmm(cut.since)} – ${ddmm(cut.until)}`;
  const days = cut.days ?? 1;
  const limitLine = per == null ? "" : `1 % недельного лимита ≈ $${per} по ценам API`;

  // money columns of a table: the share bar, the % of the limit (only with a plan price), $, tokens
  const MONEY_HEAD = ["доля", ...(per == null ? [] : ["% лимита"]), "$", "токенов"];
  const moneyRow = (name, cost, tokens, den, mid = [], sub = false) => ({
    sub,
    cells: [{ t: name, k: "name" }, ...mid,
      { t: den > 0 ? cost / den : 0, k: "bar" },
      ...(per == null ? [] : [{ t: limPct(cost, per) ?? "", k: "pct" }]),
      { t: usd(cost), k: "usd" },
      { t: tokH(tokens), k: "num" }],
  });
  const countWord = x => `${thousands(x.n)} ${x.group === "main" ? plural(x.n, "сессия", "сессии", "сессий")
    : x.group === "sub" ? plural(x.n, "субагент", "субагента", "субагентов") : plural(x.n, "вызов", "вызова", "вызовов")}`;
  const rmName = x => (x.group === "mod" ? modName(x.role) : roleName(x.role));

  const roles = () => {
    const rows = cut.by_role.map(x => moneyRow(roleName(x.key), x.cost_usd, x.tokens, total, [{ t: countWord(x), k: "txt" }]));
    return tableHtml("По ролям", ["роль", "сколько", ...MONEY_HEAD], rows);
  };

  const rmKept = (cut.by_role_model ?? []).filter(x => x.tokens > 0 || x.cost_usd > 0);
  const rmByRole = [...rmKept].sort((a, b) => roleRank(a.role) - roleRank(b.role)
    || (roleRank(a.role) === ROLE_ORDER.length ? cmp(a.role, b.role) : 0) || byFirst(a, b));

  const roleModel = () => {
    const rows = rmByRole.map((x, i) => {
      const d = daysIn(x);
      const first = i === 0 || rmByRole[i - 1].role !== x.role;
      return {
        cells: [
          { t: first ? rmName(x) : "", k: "name" },
          { t: modelName(x.model), k: "txt" },
          { t: dateSpan(x), k: "txt" },
          { t: thousands(x.n), k: "num" },
          { t: x.n > 0 ? rate(x.n / d) : "", k: "num" },
          { t: x.n > 0 ? perRun(x.cost_usd / x.n) : "", k: "num" },
          { t: x.n > 0 ? tokH(Math.round(x.tokens / x.n)) : "", k: "num" },
          { t: usd(x.cost_usd), k: "usd" },
        ],
      };
    });
    return tableHtml("Роль на разных моделях", ["роль", "модель", "дни", "запусков", "в день", "$ за запуск", "токенов за запуск", "$"], rows);
  };

  const byModels = () => {
    const rows = [];
    for (const m of cut.by_model.filter(x => x.tokens > 0 || x.cost_usd > 0)) {
      rows.push(moneyRow(modelName(m.key), m.cost_usd, m.tokens, total, [{ t: "", k: "txt" }]));
      for (const r of rmKept.filter(x => x.model === m.key)) {
        rows.push(moneyRow(rmName(r), r.cost_usd, r.tokens, total, [{ t: countWord(r), k: "txt" }], true));
      }
    }
    return tableHtml("По моделям: кто вызывает", ["модель / роль", "сколько", ...MONEY_HEAD], rows);
  };

  const byKind = () => {
    const rows = [...cut.by_kind].sort((a, b) => b.cost_usd - a.cost_usd)
      .map(k => moneyRow(KIND_WORD[k.key] ?? k.key, k.cost_usd, k.tokens, total));
    return tableHtml("По виду токенов", ["вид", ...MONEY_HEAD], rows);
  };

  // «кто запускает»: the spawner itself, then the roles it starts (the test's expected rows: the same order)
  const spawnTable = (key, name) => {
    const sp = cut.by_spawner.find(x => x.key === key);
    const exHead = ["субагентов", "за сессию", ...(days > 1 ? ["в день"] : [])];
    const rows = [];
    let sub;
    if (sp) {
      sub = `${thousands(sp.sessions)} ${plural(sp.sessions, "сессия", "сессии", "сессий")} · всего ${usd(sp.cost_usd)}`;
      rows.push(moneyRow("сам", sp.own.cost_usd, sp.own.tokens, sp.cost_usd, exHead.map(() => ({ t: "", k: "txt" }))));
      for (const r of sp.roles) {
        const ex = [{ t: thousands(r.agents), k: "num" },
          { t: sp.sessions > 0 ? rate(r.agents / sp.sessions) : "", k: "num" }];
        if (days > 1) ex.push({ t: rate(r.agents / days), k: "num" });
        rows.push(moneyRow(roleName(r.key), r.cost_usd, r.tokens, sp.cost_usd, ex));
      }
    }
    return tableHtml(`${name}: сам и кого запускает`, ["кто", ...exHead, ...MONEY_HEAD], rows, sub);
  };

  const orchResults = () => {
    const orchRm = rmKept.filter(x => x.role === "orch" && x.led > 0).sort(byFirst);
    const rows = orchRm.map(x => {
      const orchCost = x.cost_usd + x.spawned.cost_usd;
      return {
        cells: [
          { t: modelName(x.model), k: "name" },
          { t: dateSpan(x), k: "txt" },
          { t: thousands(x.led), k: "num" },
          { t: rate(x.spawned.agents / x.led), k: "num" },
          { t: usd(orchCost / x.led), k: "usd" },
          { t: x.closed == null ? "" : thousands(x.closed), k: "num" },
          { t: x.closed ? usd(orchCost / x.closed) : "", k: "num" },
        ],
      };
    });
    return tableHtml("ORCH на разных моделях: результат",
      ["модель", "дни", "сессий", "субагентов за сессию", "$ за сессию", "тикетов закрыто", "$ за тикет"], rows);
  };

  const mods = () => {
    const modSum = sumBy(cut.by_mod, x => x.cost_usd);
    const exHead = ["модель", "вызовов", ...(days > 1 ? ["в день"] : [])];
    const rows = cut.by_mod.map(x => {
      const ex = [{ t: modelName(x.model), k: "txt" }, { t: thousands(x.calls), k: "num" }];
      if (days > 1) ex.push({ t: rate(x.calls / days), k: "num" });
      return moneyRow(modName(x.key), x.cost_usd, x.tokens, modSum, ex);
    });
    return tableHtml("Моды", ["мод", ...exHead, ...MONEY_HEAD], rows, `${usd(modSum)} из ${usd(total)}`);
  };

  const chats = () => {
    const chatRows = cut.by_who.filter(x => x.kind === "chat" || x.kind === "untitled");
    const whoName = x => (x.kind === "untitled" ? "чаты без названия" : String(x.key).split(/\s+/).filter(Boolean).join(" "));
    const items = chatRows.slice(0, 10).map(x => ({ name: whoName(x), cost: x.cost_usd, tokens: x.tokens, note: subNote(x.sub_usd) }));
    const others = chatRows.slice(10);
    if (others.length) {
      items.push({ name: `остальные (${others.length})`, cost: sumBy(others, x => x.cost_usd),
        tokens: sumBy(others, x => x.tokens), note: subNote(sumBy(others, x => x.sub_usd)) });
    }
    const chatSum = sumBy(chatRows, x => x.cost_usd);
    const rows = items.map(x => moneyRow(x.name, x.cost, x.tokens, chatSum, [{ t: x.note, k: "txt" }]));
    return tableHtml("По чатам", ["чат", "субагенты", ...MONEY_HEAD], rows, `${usd(chatSum)} из ${usd(total)}`);
  };

  const dayTable = () => {
    const dayMax = Math.max(0, ...cut.by_day.map(d => d.cost_usd));
    const rows = [...cut.by_day].sort((a, b) => cmp(b.key, a.key)).map(d => moneyRow(ddmm(d.key), d.cost_usd, d.tokens, dayMax));
    return tableHtml("По дням", ["день", ...MONEY_HEAD], rows);
  };

  // «По тикетам»: a ticket of the cut has its state dot and its number (a link when the repo's label is known); the
  // rest rows (the tickets past the cut, no ticket) have neither
  const ticketsTable = () => {
    const tRow = (cell, title, cost, tokens) => ({
      cells: [
        cell,
        { t: title, k: "txt" },
        ...(per == null ? [] : [{ t: limPct(cost, per) ?? "", k: "pct" }]),
        { t: usd(cost), k: "usd" },
        { t: tokH(tokens), k: "num" },
      ],
    });
    const rows = cut.by_issue.map(x => tRow(
      { t: `#${x.number}`, k: "name", lead: ticketDot(x), href: issueBase ? issueBase + x.number : null },
      x.title || "", x.cost_usd, x.tokens));
    if (cut.issue_rest.n > 0) {
      rows.push(tRow({ t: "", k: "name" }, `остальные тикеты (${cut.issue_rest.n})`, cut.issue_rest.cost_usd, cut.issue_rest.tokens));
    }
    rows.push(tRow({ t: "", k: "name" }, "без тикета", cut.no_issue.cost_usd, cut.no_issue.tokens));
    return tableHtml("По тикетам", ["тикет", "название", ...(per == null ? [] : ["% лимита"]), "$", "токенов"], rows);
  };

  const tables = {
    roles: () => [roles(), roleModel(), byModels(), byKind()],
    spawn: () => [spawnTable("orch", "ORCH"), orchResults(), spawnTable("autopilot", "autopilot"),
      spawnTable("brain", "brain"), mods(), chats()],
    days: () => [dayTable(), ticketsTable()],
  };

  const headerLines = `<div class="lb"><span>${esc(periodText)}</span><b>${esc(headLine)}</b></div>`;
  // a failed read with the old data still held: the tables are drawn, the line above them says the data is old
  const stale = slow.error ? `<div class="c-st">Данные устарели: ${esc(slow.error)}</div>` : "";
  const footer = limitLine ? `<div class="c-st">${esc(limitLine)}</div>` : "";
  return head + headerLines + stale + tables[group]().join("") + footer;
}
