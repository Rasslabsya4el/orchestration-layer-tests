"""Q4: subagents that ran on Opus or Fable (parent, subagent_type, model pin) and every subagent the brain chats started.
Writes var/count_126/out/q4_*.csv.

Run (repo root):  python -I scripts/counts/126/q4_subagents.py
Args: --since, --until (UTC; default 2026-10-12T00:00Z to 2026-10-17T17:04Z, end exclusive).
Needs var/count_126/cache/ from extract.py.
Last line: count: <brain-started subagents of type general-purpose on Sonnet> $<their cost, USD>
"""
import os
import sys
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I drops the script's dir from sys.path
from common import load, window_args, OUT

m, f = load(*window_args(__doc__))
sp = pd.read_csv(os.path.join(os.path.dirname(OUT), 'cache', 'spawns.csv'), keep_default_na=False)
pd.set_option('display.width', 250); pd.set_option('display.max_rows', 300); pd.set_option('display.max_colwidth', 90)
sp = sp.drop_duplicates('tool_use_id')
sub = m[m.kind == 'sub'].copy()
sub = sub.merge(sp[['tool_use_id', 'subagent_type', 'model', 'description', 'run_in_background']].rename(
    columns={'model': 'pin', 'description': 'sdesc', 'subagent_type': 'req_type'}), on='tool_use_id', how='left')
sub['pin'] = sub.pin.fillna('(no spawn record)').replace('', '(not pinned)')
sub['req_type'] = sub.req_type.fillna('(no spawn record)').replace('', '(omitted)')

# a: Opus/Fable subagents by parent role, type, pin
x = sub[sub.mshort.isin(['opus', 'fable'])]
a = x.groupby(['parent_role', 'agent_type', 'req_type', 'pin', 'mshort']).agg(
    agents=('file', 'nunique'), turns=('cost', 'size'), cost=('cost', 'sum')).round(1).reset_index().sort_values('cost', ascending=False)
a.to_csv(os.path.join(OUT, 'q4_opus_fable_subs.csv'), index=False)
print(a.head(25).to_string(index=False))
print('opus+fable sub total $', x.cost.sum().round(0), ' by repo:'); print(x.groupby('repo').cost.sum().round(0).to_dict())
print('by model', x.groupby('mshort').cost.sum().round(0).to_dict())
# the definition behind the type name: does the repo still have .claude/agents/<type>.md?
print(sorted(os.listdir(os.path.join(os.path.dirname(__file__), '..', '..', '..', '.claude', 'agents'))))

# b: every subagent the brain chats started, by subagent_type (meta) and model
b = sub[sub.parent_role == 'brain']
bt = b.groupby(['agent_type', 'mshort']).agg(agents=('file', 'nunique'), turns=('cost', 'size'), cost=('cost', 'sum'),
                                              ctx_med=('ctx', 'median')).round(1).reset_index().sort_values('cost', ascending=False)
bt.to_csv(os.path.join(OUT, 'q4_brain_spawns_by_type.csv'), index=False)
print(bt.to_string(index=False))

# purposes: description of each brain-started agent (one row per agent), grouped; written to csv for reading
ag = b.groupby('file').agg(repo=('repo', 'first'), parent=('parent_title', 'first'), agent_type=('agent_type', 'first'),
                           model=('mshort', 'first'), desc=('description', 'first'), cost=('cost', 'sum'), turns=('cost', 'size'),
                           t0=('t', 'min')).reset_index()
fp = f.rename(columns={'path': 'file'})[['file', 'first_prompt']]
ag = ag.merge(fp, on='file')
ag['first_prompt'] = ag.first_prompt.str.replace('\n', ' ').str.slice(0, 260)
ag.sort_values('cost', ascending=False).drop(columns='file').round(2).to_csv(os.path.join(OUT, 'q4_brain_spawns_each.csv'), index=False)
print(len(ag), 'brain-started agents; $', ag.cost.sum().round(0))
print(ag.groupby('agent_type').agg(n=('cost', 'size'), cost=('cost', 'sum'), turns_med=('turns', 'median')).round(1))
gp = ag[(ag.agent_type == 'general-purpose') & (ag.model == 'sonnet')]
print(f'count: {len(gp)} ${gp.cost.sum():.0f}')
