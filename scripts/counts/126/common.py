"""Shared loading + pricing for the #126 scripts. Prices are the ticket body's (USD per million tokens).

Every file the scripts write or read lives under the repo's var/count_126/ (git-ignored):
cache/ (extract.py's CSVs, override with env R126_CACHE) and out/ (the q-scripts' CSVs). Created on demand.
"""
import os
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
VAR = os.path.join(REPO, 'var', 'count_126')
CACHE = os.environ.get('R126_CACHE', os.path.join(VAR, 'cache'))
OUT = os.path.join(VAR, 'out')
os.makedirs(OUT, exist_ok=True)

# body prices: in, out, cache read; cache write = 1.25 x in
PRICES = {
    'claude-fable-5-1': (10.0, 50.0, 0.25),
    'claude-opus-5-5': (4.0, 20.0, 0.20),
    'claude-sonnet-5-5': (2.0, 10.0, 0.20),
    'claude-haiku-5-5': (0.10, 0.50, 0.01),
}
# the repo's own table (config/model_prices.json) for the sensitivity: in, out, cr, cw5, cw1h
REPO_PRICES = {
    'claude-fable-5-1': (10.0, 50.0, 0.25, 12.5, 20.0),
    'claude-opus-5-5': (4.0, 20.0, 0.20, 5.0, 8.0),
    'claude-sonnet-5-5': (2.0, 10.0, 0.10, 2.5, 4.0),
    'claude-haiku-5-5': (0.10, 0.50, 0.01, 0.125, 0.2),
}
# the window, UTC: the q-scripts' --since / --until default to it
SINCE = '2026-10-12T00:00:00Z'
UNTIL = '2026-10-17T17:04:00Z'


def window_args(doc):
    """Parse --since / --until (UTC, ISO) for a q-script; returns two tz-aware UTC Timestamps."""
    import argparse
    ap = argparse.ArgumentParser(description=doc.strip().splitlines()[0])
    ap.add_argument('--since', default=SINCE, help='window start, UTC (default %(default)s)')
    ap.add_argument('--until', default=UNTIL, help='window end, UTC, exclusive (default %(default)s)')
    a = ap.parse_args()
    return tuple(_utc(x) for x in (a.since, a.until))


def _utc(x):
    t = pd.Timestamp(x)
    return t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')


def short_model(m):
    for k in ('fable', 'opus', 'sonnet', 'haiku'):
        if k in m:
            return k
    return 'other'


def load(since=None, until=None):
    since = _utc(since or SINCE)
    until = _utc(until or UNTIL)
    """msgs joined with files; adds $ columns (body prices) and role-independent fields."""
    msgs = pd.read_csv(os.path.join(CACHE, 'msgs.csv'), keep_default_na=False)
    files = pd.read_csv(os.path.join(CACHE, 'files.csv'), keep_default_na=False)
    msgs = msgs.drop_duplicates(['mid', 'rid'])
    f = files.rename(columns={'path': 'file'})
    msgs = msgs.merge(f[['file', 'repo', 'dir', 'kind', 'session', 'agent_id', 'agent_type', 'description',
                         'tool_use_id', 'title', 'entrypoint', 'cwd']], on='file', how='left')
    msgs['mshort'] = msgs.model.map(short_model)
    pin = msgs.model.map(lambda m: PRICES.get(m, (0, 0, 0))[0])
    pout = msgs.model.map(lambda m: PRICES.get(m, (0, 0, 0))[1])
    pcr = msgs.model.map(lambda m: PRICES.get(m, (0, 0, 0))[2])
    msgs['c_in'] = msgs.inp * pin / 1e6
    msgs['c_cr'] = msgs.cr * pcr / 1e6
    msgs['c_cw'] = msgs.cw * pin * 1.25 / 1e6
    msgs['c_out'] = msgs.out * pout / 1e6
    msgs['cost'] = msgs.c_in + msgs.c_cr + msgs.c_cw + msgs.c_out
    msgs['ctx'] = msgs.inp + msgs.cr + msgs.cw
    msgs['t'] = pd.to_datetime(msgs.ts, utc=True)
    msgs = msgs[(msgs.t >= since) & (msgs.t < until)].copy()
    # sensitivity: the repo's own price table with the 5m / 1h write split (config/model_prices.json)
    rp = msgs.model.map(lambda m: REPO_PRICES.get(m, (0, 0, 0, 0, 0)))
    msgs['cost_repo'] = (msgs.inp * rp.map(lambda x: x[0]) + msgs.cr * rp.map(lambda x: x[2]) +
                         msgs.cw5 * rp.map(lambda x: x[3]) + msgs.cw1h * rp.map(lambda x: x[4]) +
                         (msgs.cw - msgs.cw5 - msgs.cw1h).clip(lower=0) * rp.map(lambda x: x[3]) +
                         msgs.out * rp.map(lambda x: x[1])) / 1e6
    msgs['cw_repo_1h'] = msgs.cw1h * rp.map(lambda x: x[4]) / 1e6
    # roles
    files = files.copy()
    files['role'] = files.apply(main_role, axis=1)
    prole = files[files.kind == 'main'].set_index('session').role.to_dict()
    ptitle = files[files.kind == 'main'].set_index('session').title.to_dict()
    files['parent_role'] = files.apply(lambda r: prole.get(r.session, 'unknown-parent') if r.kind == 'sub' else '', axis=1)
    files['parent_title'] = files.apply(lambda r: ptitle.get(r.session, '') if r.kind == 'sub' else '', axis=1)
    files['role'] = files.apply(lambda r: ('sub:' + r.parent_role) if r.kind == 'sub' else r.role, axis=1)
    f2 = files.rename(columns={'path': 'file'})[['file', 'role', 'parent_role', 'parent_title']]
    msgs = msgs.merge(f2, on='file', how='left')
    return msgs, files


def main_role(r):
    t = r.title or ''
    if r.kind == 'sub':
        return ''
    if t.startswith('Brain'):
        return 'brain'
    if t.startswith('autopilot'):
        return 'autopilot'
    if t.startswith('orch'):
        return 'orch'
    if r.entrypoint == 'claude-desktop':
        return 'other-chat'
    return 'other-cli'
