"""Q5: the brain's turns by kind (by the tool each turn calls), $ each; read runs and the break-even of delegating them.
Writes var/count_126/out/q5_*.csv. Token counts from characters use 3.5 chars/token (an inference, Russian/JSON text).

Run (repo root):  python -I scripts/counts/126/q5_brain_actions.py
Args: --since, --until (UTC; default 2026-10-12T00:00Z to 2026-10-17T17:04Z, end exclusive).
Needs var/count_126/cache/ from extract.py.
Last line: count: <read runs of 3+ consecutive read turns> $<sum of their own_turns_usd + carry_usd, USD>
"""
import os, re, sys
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I drops the script's dir from sys.path
from common import load, window_args, OUT, PRICES

CPT = 3.5
m, f = load(*window_args(__doc__))
pd.set_option('display.width', 250); pd.set_option('display.max_rows', 300)
b = m[m.role == 'brain'].sort_values(['file', 't']).copy().reset_index(drop=True)
b['n_in_file'] = b.groupby('file').cost.transform('size')
b['idx'] = b.groupby('file').cumcount()
b['remaining'] = b.n_in_file - b.idx - 1

READ_BASH = re.compile(r'^Bash:(cd [^;&]*[;&]+ *)?(gh (issue (view|list)|api|pr (view|list))|git |grep|rg |sed -n|cat |head|tail|ls|python|timeout|for |S=|echo|powershell|wc|find|type )')
GH_WRITE = re.compile(r'gh issue (create|edit|comment|close|reopen)|gh api .*(PATCH|POST)')


def kind_of(r):
    tools = r.tools.split('|') if r.tools else []
    det = r.tdetail.split('|') if r.tdetail else []
    if not tools:
        return '10 reply to the owner (no tool)'
    if any(t in ('Agent', 'Task') for t in tools):
        return '1 spawn a subagent'
    if any(t in ('SendMessage', 'ListAgents', 'ReadNotifications', 'TaskStop', 'Monitor') for t in tools):
        return '2 message / list / read subagents and monitors'
    if any(d == 'Skill:write-ticket' or GH_WRITE.search(d) for d in det):
        return '3 write or comment a ticket (write-ticket, gh issue create/edit/comment/close)'
    if any(t.startswith('mcp__claude-in-chrome') or t.startswith('mcp__Claude_Browser') or 'computer' in t for t in tools):
        return '4 browser / screen check'
    if any(t in ('Edit', 'Write') or d.startswith('Bash:cat >') for t, d in zip(tools, det)):
        return '5 edit or write a file'
    if any(d.startswith(('Read:', 'Grep:', 'Glob:')) for d in det):
        return '6 read a file (Read/Grep/Glob)'
    if any(d.startswith('Bash') for d in det):
        if any(re.search(r'gh issue (view|list)|gh api|gh pr', d) for d in det):
            return '7 read tickets / PRs through gh'
        return '8 Bash: read logs, git, repo, run scripts'
    return '9 housekeeping (plan, wrap_up, ToolSearch, Skill)'


b['kind'] = b.apply(kind_of, axis=1)
b['read_tok_next'] = b.groupby('file').tr_chars.shift(-1).fillna(0) / CPT   # tool result arriving after this turn
b['um_tok_next'] = b.groupby('file').um_chars.shift(-1).fillna(0) / CPT
tot = b.cost.sum()
k = b.groupby('kind').agg(turns=('cost', 'size'), cost=('cost', 'sum'), ctx_mean=('ctx', 'mean'), out_usd=('c_out', 'sum'),
                          result_tok_mean=('read_tok_next', 'mean'), result_tok_sum=('read_tok_next', 'sum')).round(1)
k['share_pct'] = (k.cost / tot * 100).round(1)
k['usd_per_turn'] = (k.cost / k.turns).round(3)
k.to_csv(os.path.join(OUT, 'q5_brain_kinds.csv'))
print(k)
print('brain total', tot.round(0), 'turns', len(b))
# by trigger and by skill
tg = b.groupby('lastu').agg(turns=('cost', 'size'), cost=('cost', 'sum')).round(1)
print(tg)
sk = b.groupby(b.skill.replace('', '(none)')).agg(turns=('cost', 'size'), cost=('cost', 'sum')).round(1).sort_values('cost', ascending=False).head(8)
print(sk)
tg.to_csv(os.path.join(OUT, 'q5_brain_by_trigger.csv')); sk.to_csv(os.path.join(OUT, 'q5_brain_by_skill.csv'))
# by repo brain
print(b.groupby(['repo', 'kind']).cost.sum().unstack(0).round(0).fillna(0))

# ---- read runs: consecutive turns of kinds 6,7,8
rd = b.kind.str[0].isin(['6', '7', '8']) & ~b.kind.str.startswith(('10',))
rd = b.kind.str.startswith(('6 ', '7 ', '8 '))
b['rd'] = rd
runid = (b.rd != b.rd.shift()).cumsum() + b.file.factorize()[0] * 100000
b['run'] = runid
runs = []
for rid, g in b[b.rd].groupby('run'):
    model = g.mshort.iloc[0]
    pin, pcr = {'fable': (10, .25), 'opus': (4, .2), 'sonnet': (2, .2)}.get(model, (4, .2))
    tok = g.read_tok_next.sum()
    R = g.remaining.iloc[-1]
    own = g.cost.sum()                              # the brain's own turns in the run
    carry = tok * (1.25 * pin + R * pcr) / 1e6      # what the read results cost afterwards, carried to session end
    runs.append(dict(file=g.file.iloc[0], repo=g.repo.iloc[0], model=model, k=len(g), tok=tok, R=R, own_turns_usd=own, carry_usd=carry,
                     mean_turn_usd=g.cost.mean(), ctx=g.ctx.mean()))
runs = pd.DataFrame(runs)
runs['brain_total'] = runs.own_turns_usd + runs.carry_usd
# delegated alternative: one spawn turn + one report turn at the brain's mean turn price, the sub's cost
# (Haiku find: 0.002 USD/turn, Sonnet how: 0.028 USD/turn; measured in q4 / q1), plus the report (1.5k tokens) carried
def deleg(r):
    pin, pcr = {'fable': (10, .25), 'opus': (4, .2), 'sonnet': (2, .2)}.get(r.model, (4, .2))
    sub_find = 0.012 + 0.002 * r.k * 1.0    # cold start ~ first turn of a Haiku researcher + its turns
    sub_how = 0.10 + 0.028 * r.k
    report = 1500 * (1.25 * pin + r.R * pcr) / 1e6
    base = 2 * r.mean_turn_usd
    return pd.Series(dict(deleg_find=base + sub_find + report, deleg_how=base + sub_how + report))
runs = pd.concat([runs, runs.apply(deleg, axis=1)], axis=1)
runs['save_find'] = (runs.brain_total - runs.deleg_find)
runs['save_how'] = (runs.brain_total - runs.deleg_how)
runs['kbucket'] = pd.cut(runs.k, [0, 1, 2, 4, 8, 1000], labels=['1', '2', '3-4', '5-8', '9+'])
sm = runs.groupby('kbucket', observed=True).agg(runs=('k', 'size'), turns=('k', 'sum'), brain_usd=('brain_total', 'sum'), tok_mean=('tok', 'mean'),
                                    deleg_find=('deleg_find', 'sum'), deleg_how=('deleg_how', 'sum'),
                                    save_find_pos=('save_find', lambda x: x[x > 0].sum()), save_how_pos=('save_how', lambda x: x[x > 0].sum()),
                                    n_pos_how=('save_how', lambda x: (x > 0).sum())).round(1)
print(sm)
sm.to_csv(os.path.join(OUT, 'q5_read_runs_breakeven.csv'))
print('read runs', len(runs), 'brain cost of them', runs.brain_total.sum().round(0), ' own-turn part', runs.own_turns_usd.sum().round(0), 'carry part', runs.carry_usd.sum().round(0))
print('saving if every run with positive saving went to Haiku find:', runs.save_find.clip(lower=0).sum().round(0), ' to Sonnet how:', runs.save_how.clip(lower=0).sum().round(0))
runs.round(3).to_csv(os.path.join(OUT, 'q5_read_runs.csv'), index=False)

# break-even tokens by brain model and remaining turns: tokens T where T*rate(R) = sub cost + report*rate(R)
rows = []
for model, (pin, pcr) in {'fable': (10, .25), 'opus': (4, .2)}.items():
    for R in (10, 20, 40):
        rate = (1.25 * pin + R * pcr) / 1e6
        for nm, subc in (('haiku find task ($0.015)', 0.015), ('sonnet how task ($0.45)', 0.445)):
            rows.append(dict(brain=model, remaining_turns=R, usd_per_1k_tok=round(rate * 1000, 4), delegate_to=nm,
                             breakeven_tokens_read=round(subc / rate + 1500)))
be = pd.DataFrame(rows)
be.to_csv(os.path.join(OUT, 'q5_breakeven_tokens.csv'), index=False)
print(be.to_string(index=False))
# report size the brain actually received from its helpers (notification text + tool result of Agent calls), tokens
rep = b[b.lastu.isin(['notif'])].um_chars / CPT
print('notification text tokens: median', rep.median().round(0), 'p90', rep.quantile(.9).round(0))
long = runs[runs.k >= 3]
print(f'count: {len(long)} ${long.brain_total.sum():.0f}')
