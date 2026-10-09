"""Diagnostics of the frozen six-family signals (no strategy rule is changed here).

Every event is one of the frozen pipeline's trades (same signal, same next-bar-open entry).
For each event this module adds, using only candles that exist:
  * forward gross return at 15/30/45/60/120 minutes from the entry (close of the last bar in the
    window). A horizon that would run past the 15:10 bar (the 15:15 square-off) is TRUNCATED and
    excluded; a missing end candle is MISSING and excluded. Both are counted.
  * maximum favourable / adverse excursion over the same window (needs >= 80% of its candles).
  * a signal-strength score recomputed from bars 0..k (the same inputs the rule used).
  * direction, time of day, volatility (ATR% of price) and liquidity rank (from earlier days).
"""
from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

from . import families as F
from .data import LAST_EXIT_SLOT, range6, slot_baseline, universe_for
from .families import atr

HORIZONS = {15: 3, 30: 6, 45: 9, 60: 12, 120: 24}


@dataclass
class Event:
    t: object                      # the frozen Trade
    strength: float | None
    atr_pct: float | None
    liq_rank: int | None
    fwd: dict = field(default_factory=dict)      # minutes -> gross % or None
    mfe: dict = field(default_factory=dict)
    mae: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)   # minutes -> "ok" | "truncated" | "missing"

    @property
    def tod(self):
        k = self.t.signal_slot
        return "09:30-11:00" if k < 21 else ("11:00-13:30" if k < 51 else "13:30-14:45")


# --------------------------------------------------------------------------- strength (bars <= k only)

def strength(t, day, base_v, base_r6):
    k, side, name = t.signal_slot, t.side, t.variant
    fam, _, p, _, _ = F.VARIANTS[name]
    a = atr(day, k)
    if fam == "SWEEP":
        L = p["L"]
        if not a:
            return None
        level = max(day.h[k - L:k]) if side < 0 else min(day.l[k - L:k])
        return (day.h[k] - level) / a if side < 0 else (level - day.l[k]) / a
    if fam == "IMPULSE":
        a3 = atr(day, k - 3) or a
        for j in range(k - 2, k - 5, -1):
            if j - 2 < 0 or not a3:
                continue
            move = day.c[j] - day.o[j - 2]
            if abs(move) >= 3 * a3 and (1 if move > 0 else -1) == side:
                return abs(move) / a3
        return None
    if fam == "SQUEEZE":
        if base_r6 is None or not base_r6[k - 1]:
            return None
        width = max(day.h[k - 6:k]) - min(day.l[k - 6:k])
        return base_r6[k - 1] / width if width > 0 else None       # higher = tighter box
    if fam == "XSRS":
        ref = day.o[0] if k - 6 < 0 else day.c[k - 6]
        return abs(day.c[k] / ref - 1) * 100 if ref else None      # size of the 30-min move
    if fam == "VOLSURP":
        return day.v[k] / base_v[k] if base_v and base_v[k] and day.v[k] is not None else None
    if fam == "FAILBO":
        N = p["N"]
        if not a:
            return None
        hi, lo = max(day.h[0:N]), min(day.l[0:N])
        return (hi - day.c[k]) / a if side < 0 else (day.c[k] - lo) / a
    return None


def forward(t, day, bars: int):
    """(gross %, mfe %, mae %, status) over `bars` bars from the entry bar."""
    e, side, entry = t.entry_slot, t.side, t.entry
    end = e + bars - 1
    if end > LAST_EXIT_SLOT:
        return None, None, None, "truncated"
    window = [s for s in range(e, end + 1) if day.c[s] is not None]
    if day.c[end] is None:
        return None, None, None, "missing"
    ret = side * (day.c[end] / entry - 1) * 100
    if len(window) < 0.8 * bars:
        return ret, None, None, "ok"
    fav = max((side * ((day.h[s] if side > 0 else day.l[s]) / entry - 1) * 100) for s in window)
    adv = min((side * ((day.l[s] if side > 0 else day.h[s]) / entry - 1) * 100) for s in window)
    return ret, max(fav, 0.0), min(adv, 0.0), "ok"


def build_events(ds, trades):
    """Attach diagnostics to the frozen trades. Baselines and liquidity come from earlier days."""
    idx = {d: i for i, d in enumerate(ds.sessions)}
    uni_cache, vb, rb = {}, {}, {}
    out = []
    for t in trades:
        i = idx[t.day]
        if i not in uni_cache:
            uni, _ = universe_for(ds, i)
            uni_cache[i] = {s: r for r, s in enumerate(uni, 1)}
        day = ds.days[t.symbol][t.day]
        key = (t.symbol, i)
        fam = F.VARIANTS[t.variant][0]
        if fam == "VOLSURP" and key not in vb:
            vb[key] = slot_baseline(ds, t.symbol, i, lambda d: d.v)
        if fam == "SQUEEZE" and key not in rb:
            rb[key] = slot_baseline(ds, t.symbol, i, range6)
        a = atr(day, t.signal_slot)
        ev = Event(t, strength(t, day, vb.get(key), rb.get(key)),
                   a / day.c[t.signal_slot] * 100 if a and day.c[t.signal_slot] else None,
                   uni_cache[i].get(t.symbol))
        for minutes, bars in HORIZONS.items():
            r, fav, adv, status = forward(t, day, bars)
            ev.fwd[minutes], ev.mfe[minutes], ev.mae[minutes], ev.status[minutes] = r, fav, adv, status
        ev.fwd["EOD"] = forward(t, day, LAST_EXIT_SLOT - t.entry_slot + 1)[0]
        out.append(ev)
    return out


def matched_random(ds, events, seed=5):
    """Same symbol and day, random signal bar and side: forward returns with no skill."""
    rnd = random.Random(seed)
    out = []
    for ev in events:
        day = ds.days[ev.t.symbol][ev.t.day]
        for _ in range(20):
            k = rnd.randint(F.K_MIN, F.K_MAX)
            if day.o[k + 1] is None:
                continue

            class R:
                pass
            r = R()
            r.entry_slot, r.side, r.entry = k + 1, rnd.choice((1, -1)), day.o[k + 1]
            res = {m: forward(r, day, b) for m, b in HORIZONS.items()}
            out.append(res)
            break
    return out


# --------------------------------------------------------------------------- statistics

def boot_mean(values_by_day: dict, threshold: float = 0.0, draws: int = 1000, seed: int = 3):
    """Day-clustered bootstrap of the pooled mean: (mean, lo, hi, p(mean <= threshold), n, days)."""
    days = [d for d, xs in values_by_day.items() if xs]
    n = sum(len(values_by_day[d]) for d in days)
    if not n:
        return None
    mean = sum(sum(values_by_day[d]) for d in days) / n
    rnd = random.Random(seed)
    ms = []
    for _ in range(draws):
        tot = cnt = 0
        for _ in days:
            xs = values_by_day[days[rnd.randrange(len(days))]]
            tot += sum(xs)
            cnt += len(xs)
        ms.append(tot / cnt)
    ms.sort()
    return mean, ms[int(0.025 * draws)], ms[int(0.975 * draws) - 1], sum(1 for m in ms if m <= threshold) / draws, n, len(days)


def by_day(events, value):
    out = defaultdict(list)
    for ev in events:
        v = value(ev)
        if v is not None:
            out[ev.t.day].append(v)
    return out


def quantile_edges(values, q=5):
    xs = sorted(v for v in values if v is not None)
    if len(xs) < 5 * q:
        return None
    return [xs[int(len(xs) * i / q)] for i in range(1, q)]


def bucket(x, edges):
    if x is None or edges is None:
        return None
    b = 0
    while b < len(edges) and x >= edges[b]:
        b += 1
    return b


def concurrency(trades):
    """Max simultaneously open trades and trades per day, per family."""
    per = defaultdict(lambda: defaultdict(list))
    for t in trades:
        per[t.family][t.day].append((t.entry_slot, t.exit_slot))
    out = {}
    for fam, days in per.items():
        peaks, counts = [], []
        for spans in days.values():
            counts.append(len(spans))
            peaks.append(max(sum(1 for a, b in spans if a <= s <= b) for s in range(76)))
        out[fam] = (median(counts), max(counts), median(peaks), max(peaks))
    return out


def capital_drawdown(trades, cost, positions=19, size=5000.0):
    """A ₹19k account at 5x margin: at most `positions` x ₹5,000 open at once, signals taken in
    time order, first come first served. Returns (trades taken, net ₹, max drawdown ₹)."""
    by = defaultdict(list)
    for t in trades:
        by[t.day].append(t)
    eq = peak = dd = 0.0
    taken = 0
    for d in sorted(by):
        open_until = []
        for t in sorted(by[d], key=lambda x: (x.entry_slot, x.symbol)):
            open_until = [x for x in open_until if x >= t.entry_slot]
            if len(open_until) >= positions:
                continue
            open_until.append(t.exit_slot)
            taken += 1
            eq += size * (t.gross_pct - cost) / 100
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
    return taken, eq, dd
