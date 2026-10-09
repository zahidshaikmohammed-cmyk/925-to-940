"""Load the 60-day 5-minute history, audit it, and build per-symbol day grids.

A day grid has 75 slots (09:15 .. 15:25). A slot with no candle stays None -- it is never
filled with a previous price or a zero volume. Every reference value used for a decision on
day D (universe, liquidity, volume-by-time-of-day, typical ranges) comes from days before D.
"""
from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from statistics import median

SLOTS = 75                     # 09:15 .. 15:25
LAST_EXIT_SLOT = 71            # the 15:10 bar; its close is the 15:15 square-off
OPEN_MIN = 9 * 60 + 15


def slot_of(ts: datetime) -> int | None:
    m = ts.hour * 60 + ts.minute - OPEN_MIN
    if m < 0 or m % 5 or ts.second:
        return None
    s = m // 5
    return s if s < SLOTS else None


def slot_time(s: int) -> str:
    m = OPEN_MIN + 5 * s
    return f"{m // 60:02d}:{m % 60:02d}"


class DayBars:
    """One symbol's session: o/h/l/c/v lists of length 75 with None for missing candles."""
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self):
        self.o = [None] * SLOTS
        self.h = [None] * SLOTS
        self.l = [None] * SLOTS
        self.c = [None] * SLOTS
        self.v = [None] * SLOTS

    def present(self, s: int) -> bool:
        return self.c[s] is not None

    def count(self) -> int:
        return sum(1 for x in self.c if x is not None)

    def first_open(self):
        return self.o[0]

    def last_close(self):
        for s in range(SLOTS - 1, -1, -1):
            if self.c[s] is not None:
                return self.c[s]
        return None

    def turnover(self) -> float:
        return sum(self.c[s] * self.v[s] for s in range(SLOTS) if self.c[s] is not None and self.v[s])


@dataclass
class Dataset:
    days: dict                                  # sym -> {date: DayBars}
    sessions: list                              # full trading days used, sorted
    fetched_at: str | None = None
    failed: dict = field(default_factory=dict)
    audit: dict = field(default_factory=dict)


def read_raw(path: str | Path) -> dict:
    return json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))


def build(raw: dict, min_symbols_share: float = 0.5) -> Dataset:
    """Parse the raw history file into day grids and an audit (nothing is silently repaired)."""
    fetched_at = raw.get("fetched_at")
    failed = dict(raw.get("failed") or {})
    symbols = raw.get("symbols") or {}
    audit = {"symbols_loaded": len(symbols), "symbols_failed": len(failed), "failed": failed,
             "off_grid": 0, "duplicates": 0, "invalid_ohlc": 0, "zero_volume": 0, "stale_runs": 0,
             "bars": 0, "possible_corporate_actions": []}
    days: dict = {}
    for sym, rows in symbols.items():
        per: dict = {}
        seen = set()
        for r in rows:
            ts = datetime.fromisoformat(r[0])
            o, h, l, c, v = r[1], r[2], r[3], r[4], r[5]
            s = slot_of(ts)
            if s is None:
                audit["off_grid"] += 1
                continue
            key = (ts.date(), s)
            if key in seen:
                audit["duplicates"] += 1
                continue
            seen.add(key)
            if None in (o, h, l, c) or min(o, h, l, c) <= 0 or h < max(o, c) or l > min(o, c) or h < l:
                audit["invalid_ohlc"] += 1
                continue
            d = per.setdefault(ts.date(), DayBars())
            d.o[s], d.h[s], d.l[s], d.c[s], d.v[s] = o, h, l, c, (v if v is not None and v >= 0 else None)
            audit["bars"] += 1
            if not v:
                audit["zero_volume"] += 1
        for d in per.values():                    # stale: 6+ consecutive identical candles, no volume
            run = 0
            for s in range(1, SLOTS):
                same = (d.c[s] is not None and d.c[s - 1] is not None and d.o[s] == d.c[s] == d.h[s] == d.l[s]
                        == d.c[s - 1] and not d.v[s])
                run = run + 1 if same else 0
                if run == 6:
                    audit["stale_runs"] += 1
        days[sym] = per

    # sessions: dates where most symbols have the full day; the download day is dropped when partial
    all_dates = sorted({d for per in days.values() for d in per})
    full_counts = {d: sum(1 for per in days.values() if d in per and per[d].present(LAST_EXIT_SLOT))
                   for d in all_dates}
    sessions = [d for d in all_dates if full_counts[d] >= min_symbols_share * max(1, len(days))]
    audit["dates_seen"] = len(all_dates)
    audit["sessions"] = len(sessions)
    audit["dropped_dates"] = {str(d): f"{full_counts[d]} symbols reach 15:10" for d in all_dates if d not in sessions}
    audit["coverage_by_date"] = {str(d): (sum(1 for per in days.values() if d in per),
                                          sum(1 for per in days.values() if d in per and per[d].count() == SLOTS))
                                 for d in sessions}
    slot_cov = []
    for per in days.values():
        n = sum(per[d].count() for d in sessions if d in per)
        slot_cov.append(n / (SLOTS * len(sessions)) if sessions else 0)
    audit["symbol_slot_coverage"] = {
        ">=99%": sum(1 for x in slot_cov if x >= 0.99), "95-99%": sum(1 for x in slot_cov if 0.95 <= x < 0.99),
        "80-95%": sum(1 for x in slot_cov if 0.80 <= x < 0.95), "<80%": sum(1 for x in slot_cov if x < 0.80)}
    missing_slots = Counter()
    for per in days.values():
        for d in sessions:
            if d in per:
                for s in range(SLOTS):
                    if not per[d].present(s):
                        missing_slots[s] += 1
    audit["missing_by_slot_top"] = [(slot_time(s), n) for s, n in missing_slots.most_common(5)]
    for sym, per in days.items():                 # overnight gaps > 15%: splits/bonuses/news
        prev = None
        for d in sessions:
            if d not in per:
                continue
            if prev is not None and per[d].first_open() and prev:
                gap = per[d].first_open() / prev - 1
                if abs(gap) > 0.15:
                    audit["possible_corporate_actions"].append((sym, str(d), round(gap * 100, 1)))
            prev = per[d].last_close()
    return Dataset(days=days, sessions=sessions, fetched_at=fetched_at, failed=failed, audit=audit)


def load(path: str | Path) -> Dataset:
    return build(read_raw(path))


# --------------------------------------------------------------------------- eligibility

@dataclass(frozen=True)
class Eligibility:
    lookback_days: int = 10
    min_history_days: int = 8
    min_price: float = 50.0
    top_n_liquidity: int = 300


def universe_for(ds: Dataset, i: int, rules: Eligibility = Eligibility()):
    """Eligible symbols for session i using ONLY sessions before i. Returns (symbols, exclusions)."""
    hist = ds.sessions[max(0, i - rules.lookback_days):i]
    prev = ds.sessions[i - 1] if i > 0 else None
    reasons = Counter()
    ranked = []
    for sym, per in ds.days.items():
        have = [per[d] for d in hist if d in per]
        if len(have) < rules.min_history_days or prev not in per:
            reasons["insufficient history"] += 1
            continue
        pc = per[prev].last_close()
        if pc is None or pc < rules.min_price:
            reasons["price below Rs %g" % rules.min_price] += 1
            continue
        ranked.append((median(x.turnover() for x in have), sym))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    reasons[f"liquidity rank > {rules.top_n_liquidity}"] += max(0, len(ranked) - rules.top_n_liquidity)
    return [s for _, s in ranked[:rules.top_n_liquidity]], dict(reasons)


def slot_baseline(ds: Dataset, sym: str, i: int, values, lookback: int = 10, min_values: int = 5):
    """Per-slot median over the previous `lookback` sessions of values(DayBars) -> list[75]."""
    per = ds.days[sym]
    hist = [per[d] for d in ds.sessions[max(0, i - lookback):i] if d in per]
    cols = defaultdict(list)
    for day in hist:
        for s, x in enumerate(values(day)):
            if x is not None:
                cols[s].append(x)
    return [median(cols[s]) if len(cols[s]) >= min_values else None for s in range(SLOTS)]


def range6(day: DayBars):
    """Range of the 6 bars ending at each slot (None unless all 6 present)."""
    out = [None] * SLOTS
    for s in range(5, SLOTS):
        hs, ls = day.h[s - 5:s + 1], day.l[s - 5:s + 1]
        if None not in hs and None not in ls:
            out[s] = max(hs) - min(ls)
    return out
