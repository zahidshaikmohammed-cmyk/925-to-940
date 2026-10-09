"""Generate every variant's trades, session by session, with no future information."""
from __future__ import annotations

import random

from . import families as F
from .data import Dataset, Eligibility, range6, slot_baseline, universe_for
from .sim import make_trade

WARMUP = 10                    # first sessions only build baselines


def run(ds: Dataset, rules: Eligibility = Eligibility(), say=None, variants=None):
    """-> (trades, per-session info). Trades are generated for sessions WARMUP.. only."""
    names = variants or list(F.VARIANTS)
    trades, info = [], {}
    for i in range(WARMUP, len(ds.sessions)):
        d = ds.sessions[i]
        universe, excluded = universe_for(ds, i, rules)
        bars = {s: ds.days[s][d] for s in universe if d in ds.days[s]}
        info[d] = {"universe": len(universe), "with_bars": len(bars), "excluded": excluded}
        base_v, base_r6 = {}, {}
        need_v = any(F.VARIANTS[n][0] == "VOLSURP" for n in names)
        need_r = any(F.VARIANTS[n][0] == "SQUEEZE" for n in names)
        for s in bars:
            if need_v:
                base_v[s] = slot_baseline(ds, s, i, lambda day: day.v)
            if need_r:
                base_r6[s] = slot_baseline(ds, s, i, range6)
        for name in names:
            family, kind, params, target_r, hold = F.VARIANTS[name]
            if kind == "cross":
                taken = set()
                for k in F.XS_SLOTS:
                    for sym, side, stop in F.xsrs(bars, universe, k, sign=params["sign"]):
                        if sym in taken:
                            continue
                        t = make_trade(family, name, sym, d, bars[sym], k, side, stop, target_r, hold)
                        if t:
                            taken.add(sym)
                            trades.append(t)
                continue
            for sym, day in bars.items():
                for k in range(F.K_MIN, F.K_MAX + 1):
                    if day.c[k] is None:
                        continue
                    sig = F.single_signal(name, day, k, base_v.get(sym), base_r6.get(sym))
                    if not sig:
                        continue
                    t = make_trade(family, name, sym, d, day, k, sig[0], sig[1], target_r, hold)
                    if t:
                        trades.append(t)
                        break                    # one trade per symbol per variant per day
        if say:
            say(f"  {d}: universe {len(universe)}, trades so far {len(trades)}")
    return trades, info


def random_baseline(ds: Dataset, trades, seed: int = 7):
    """One matched random trade per real trade: same symbol, day, risk %, target and hold;
    random signal bar in the same window and a random side. The 'no skill' reference."""
    rnd = random.Random(seed)
    out = []
    for t in trades:
        day = ds.days[t.symbol][t.day]
        _, _, _, target_r, hold = F.VARIANTS[t.variant]
        for _ in range(20):
            k = rnd.randint(F.K_MIN, F.K_MAX)
            if day.c[k] is None:
                continue
            side = rnd.choice((1, -1))
            stop = day.c[k] * (1 - side * t.risk_pct / 100)
            r = make_trade(t.family, t.variant, t.symbol, t.day, day, k, side, stop, target_r, hold)
            if r:
                out.append(r)
                break
    return out
