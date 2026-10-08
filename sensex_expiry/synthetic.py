"""SYNTHETIC expiry days for tests and the self-test ONLY.

Nothing produced from this module is evidence of anything about real markets: a
random walk has, by construction, no edge, which is exactly why it is useful. The
backtester run on pure random walks must show expectancy <= 0 after costs; if it
ever shows a robust positive edge on noise, the backtester is leaking information.
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

from .backtest import DayData
from .models import IST, Candle, PriorDay
from .options import bs_price

SUBSTEPS = 4


def make_day(d: date, seed: int, spot0: float = 81_000.0, ann_vol: float = 0.13, iv: float = 0.14,
             drift_per_min: float = 0.0, strikes_each_side: int = 12, inject_sweep: bool = False,
             is_expiry: bool = True, prior: PriorDay | None = None) -> DayData:
    rnd = random.Random(seed)
    open_ts = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
    close_ts = datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST)
    n = 375
    sig = ann_vol / math.sqrt(252 * 375 * SUBSTEPS)
    path = [spot0]
    for k in range(n * SUBSTEPS):
        bump = 0.0
        if inject_sweep and 120 * SUBSTEPS <= k < 124 * SUBSTEPS:
            bump = -0.00025          # sharp push down ...
        if inject_sweep and 124 * SUBSTEPS <= k < 132 * SUBSTEPS:
            bump = +0.00030          # ... then a reclaim with follow-through
        path.append(path[-1] * math.exp(drift_per_min / SUBSTEPS + bump + sig * rnd.gauss(0, 1)))
    und: list[Candle] = []
    for m in range(n):
        seg = path[m * SUBSTEPS:(m + 1) * SUBSTEPS + 1]
        und.append(Candle(open_ts + timedelta(minutes=m), round(seg[0], 2), round(max(seg), 2),
                          round(min(seg), 2), round(seg[-1], 2)))
    if prior is None:
        prior = PriorDay(d - timedelta(days=1), spot0 * 1.004, spot0 * 0.996, spot0 * (1 + rnd.gauss(0, 0.002)))
    atm0 = int(round(spot0 / 100) * 100)
    options: dict = {}
    year_s = 365 * 24 * 3600
    for k in range(-strikes_each_side, strikes_each_side + 1):
        strike = atm0 + 100 * k
        for right in ("CE", "PE"):
            series = {}
            for m in range(n):
                vals = []
                for j in range(SUBSTEPS + 1):
                    ts = open_ts + timedelta(minutes=m, seconds=60 * j / SUBSTEPS)
                    t_years = max((close_ts - ts).total_seconds(), 0) / year_s
                    vals.append(max(0.05, round(bs_price(path[m * SUBSTEPS + j], strike, t_years, iv, right) / 0.05) * 0.05))
                series[open_ts + timedelta(minutes=m)] = Candle(open_ts + timedelta(minutes=m), vals[0], max(vals),
                                                                min(vals), vals[-1], 1000, SUBSTEPS)
            options[(strike, right)] = series
    return DayData(d, prior, is_expiry, d if is_expiry else None, und, options, 20, {"synthetic": True})


def make_days(n: int, seed: int = 1, start: date = date(2025, 9, 4)) -> list[DayData]:
    days, d = [], start
    for i in range(n):
        days.append(make_day(d, seed * 1000 + i))
        d += timedelta(days=7)
    return days
