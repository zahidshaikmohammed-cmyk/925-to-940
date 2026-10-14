"""Deterministic synthetic sessions for the strategy-engine tests.

Every scenario is a list of 1-minute bars from 09:15 IST built from explicit numbers, so a
test can recompute the expected engine values independently by hand.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from strategy_930 import IST, Candle
from strategies.scanner import StockInput

DAY = datetime(2026, 10, 13, 9, 15, tzinfo=IST)     # a Tuesday, NSE trading day


def ts(i: int) -> datetime:
    return DAY + timedelta(minutes=i)


def scan_after(n_bars: int) -> datetime:
    """Scan time 8 s after the n-th bar (index n-1) completed."""
    return ts(n_bars) + timedelta(seconds=8)


class Tape:
    """Append bars one at a time; o = previous close unless given."""

    def __init__(self, start: float = 100.0):
        self.bars: list[Candle] = []
        self.last = start

    def bar(self, c: float, v: float = 10_000, up: float = 0.03, dn: float = 0.03, o: float | None = None,
            h: float | None = None, l: float | None = None) -> "Tape":
        o = self.last if o is None else o
        hi = max(o, c) + up if h is None else h
        lo = min(o, c) - dn if l is None else l
        self.bars.append(Candle(ts(len(self.bars)), round(o, 4), round(hi, 4), round(lo, 4), round(c, 4), float(v)))
        self.last = c
        return self

    def flat(self, n: int, level: float, amp: float = 0.05, v: float = 10_000, wick: float = 0.03) -> "Tape":
        """n bars oscillating +/- amp around ``level`` (deterministic zig-zag)."""
        for k in range(n):
            self.bar(level + (amp if k % 2 == 0 else -amp), v, wick, wick)
        return self

    def walk(self, n: int, step: float, v: float = 10_000, wick: float = 0.03) -> "Tape":
        for _ in range(n):
            self.bar(self.last + step, v, wick, wick)
        return self

    def done(self) -> tuple[Candle, ...]:
        return tuple(self.bars)


def filler_universe(n: int, n_bars: int, seed: int = 7, drift: float = 0.0) -> dict[str, StockInput]:
    """n liquid random-walk stocks (a market for the cross-sectional engines)."""
    rng = random.Random(seed)
    out = {}
    for k in range(n):
        p = rng.uniform(200, 2000)
        pc = p
        bars = []
        for i in range(n_bars):
            o = p
            c = o * (1 + drift + rng.gauss(0, 0.0008))
            h = max(o, c) * (1 + abs(rng.gauss(0, 0.0004)))
            l = min(o, c) * (1 - abs(rng.gauss(0, 0.0004)))
            bars.append(Candle(ts(i), o, h, l, c, 20_000 * rng.uniform(0.5, 1.5)))
            p = c
        out[f"F{k:03d}"] = StockInput(f"F{k:03d}", tuple(bars), pc, bars[0].open)
    return out


# ------------------------------------------------------------------ engine scenarios
# Each returns (bars, previous_close, prior) for a textbook LONG instance of one engine.
from strategies.features import PriorLevels  # noqa: E402


def sc_order_flow():
    t = Tape(102.0)
    t.walk(20, -0.10, v=40_000, wick=0.1)
    t.flat(20, 100.0, amp=0.08, v=10_000, wick=0.15)
    for _ in range(10):
        t.bar(t.last + 0.12, v=40_000, up=0.01, dn=0.25)
    return t.done(), 100.0, None


def _opening_range_with_low(t: Tape):
    t.flat(5, 100.0, amp=0.1, v=30_000, wick=0.15)
    t.bar(99.8, v=30_000, up=0.1, dn=0.0, l=99.5)        # ORL = 99.50 at 09:20
    t.flat(9, 100.0, amp=0.1, v=30_000, wick=0.15)


def sc_sweep():
    t = Tape(100.0)
    _opening_range_with_low(t)
    t.flat(30, 100.1, amp=0.12, v=20_000, wick=0.12)
    t.bar(99.75, v=70_000, o=99.85, h=99.88, l=99.35)    # sweep 0.15 below ORL, close back above
    return t.done(), 100.0, None


def sc_failed_breakdown():
    t = Tape(100.0)
    _opening_range_with_low(t)
    t.flat(16, 99.9, amp=0.12, v=20_000, wick=0.12)
    t.bar(99.40, v=16_000, up=0.05, dn=0.05)             # accepted below ORL ...
    t.bar(99.35, v=15_000, up=0.05, dn=0.10)
    t.bar(99.38, v=15_000, up=0.05, dn=0.05)
    t.bar(99.78, v=45_000, up=0.02, dn=0.03)             # ... then fails back inside
    return t.done(), 100.0, None


def sc_trend_pullback():
    t = Tape(100.0)
    for k in range(60):
        t.bar(t.last + (0.06 if k % 2 == 0 else 0.0), v=20_000, up=0.05, dn=0.05)
    for _ in range(10):
        t.bar(t.last + 0.08, v=25_000, up=0.05, dn=0.03)  # impulse
    for _ in range(4):
        t.bar(t.last - 0.15, v=8_000, up=0.02, dn=0.05)   # light-volume pullback
    t.bar(t.last + 0.55, v=30_000, up=0.01, dn=0.03)      # continuation
    return t.done(), 100.0, None


def sc_relative_strength():
    t = Tape(100.0)
    for k in range(85):
        t.bar(t.last + (0.08 if k % 2 == 0 else -0.02), v=(15_000 if k < 70 else 35_000), up=0.05, dn=0.05)
    for k in range(15):
        t.bar(t.last + (0.22 if k % 2 == 0 else -0.21), v=35_000, up=0.05, dn=0.08)
    t.bar(t.last + 0.45, v=60_000, up=0.01, dn=0.03)
    return t.done(), 100.0, None


def sc_compression():
    """A slow 15-bar coil (small candles drifting across a 0.8-rupee box), then a decisive
    break that is NOT chased: the entry stays within 60% of the risk from the box edge."""
    import math
    t = Tape(100.0)
    t.flat(15, 100.0, amp=0.3, v=40_000, wick=0.2)
    t.flat(55, 100.0, amp=0.35, v=20_000, wick=0.25)
    for k in range(15):
        t.bar(round(100.0 + 0.4 * math.sin(2 * math.pi * (k + 1) / 15), 2), v=12_000, up=0.02, dn=0.02)
    hi = max(b.high for b in t.bars[-15:])
    t.bar(hi + 0.30, v=80_000, up=0.02, dn=0.02)          # expansion
    t.bar(hi + 0.32, v=40_000, up=0.02, dn=0.03)          # confirmation
    return t.done(), 100.0, None


def sc_orb():
    t = Tape(100.0)
    for k in range(15):
        t.bar(t.last + (0.08 if k % 2 == 0 else 0.0), v=60_000, up=0.05, dn=0.05)
    for k in range(5):
        t.bar(t.last + (-0.1 if k % 2 == 0 else 0.1), v=30_000, up=0.05, dn=0.05)
    t.bar(t.last + 0.30, v=70_000, up=0.02, dn=0.05)
    return t.done(), 99.5, PriorLevels("2026-10-12", None, None, None, 300_000.0, 14, "psygrid")


def sc_vwap_reclaim():
    from strategies.features import Features
    t = Tape(103.0)
    t.walk(30, -0.12, v=20_000, wick=0.2)
    t.walk(5, 0.15, v=15_000, wick=0.15)
    vw0 = Features(t.done()).vwap[-1]
    t.bar(vw0 + 0.5, v=60_000, o=t.last + 0.02, h=vw0 + 0.55, l=t.last - 0.02)   # cross
    t.bar(t.last + 0.15, v=20_000, up=0.1, dn=0.1)
    vw = Features(t.done()).vwap[-1]
    t.bar(vw + 0.35, v=12_000, up=0.08, l=vw + 0.15)
    vw = Features(t.done()).vwap[-1]
    t.bar(vw + 0.20, v=10_000, up=0.06, l=vw - 0.05)     # retest touch
    t.bar(t.last + 0.22, v=30_000, up=0.02, dn=0.05)     # trigger
    return t.done(), 101.0, None                         # gapped up to 103, now +0.5% on the day


def sc_exhaustion():
    t = Tape(100.0)
    t.flat(120, 100.0, amp=0.1, v=80_000, wick=0.1)
    t.walk(5, -0.75, v=20_000, wick=0.05)
    t.bar(96.10, v=540_000, o=t.last, h=t.last + 0.05, l=95.45)  # selling climax
    t.bar(96.05, v=120_000, up=0.05, dn=0.05)
    t.bar(96.55, v=140_000, up=0.03, dn=0.05)            # first close above the climax high
    return t.done(), 100.0, None


def sc_structural():
    prior = PriorLevels("2026-10-12", 101.0, 99.0, 100.2, None, 0)
    t = Tape(100.3)
    t.flat(15, 100.4, amp=0.15, v=30_000, wick=0.1)
    for k in range(25):
        if k % 8 == 4:
            t.bar(100.7, v=20_000, h=100.95, dn=0.1)      # tests of PDH from below
        else:
            t.bar(100.5 + (0.15 if k % 2 == 0 else -0.15), v=20_000, up=0.12, dn=0.12)
    t.bar(101.40, v=70_000, up=0.03, dn=0.05)             # break
    t.walk(3, 0.15, v=30_000, wick=0.08)                  # departure
    t.bar(101.55, v=15_000, up=0.05, dn=0.05)
    t.bar(101.25, v=12_000, up=0.05, l=101.02)            # retest
    t.bar(101.68, v=30_000, up=0.02, dn=0.04)             # trigger
    return t.done(), 100.2, prior


SCENARIOS = {
    "Order_Flow_Imbalance_Engine": sc_order_flow,
    "Liquidity_Sweep_Reclaim_Engine": sc_sweep,
    "Failed_Breakout_Breakdown_Engine": sc_failed_breakdown,
    "Trend_Pullback_Continuation_Engine": sc_trend_pullback,
    "Relative_Strength_Weakness_Engine": sc_relative_strength,
    "Volatility_Compression_Expansion_Engine": sc_compression,
    "Regime_Filtered_ORB_Engine": sc_orb,
    "VWAP_Reclaim_Rejection_Engine": sc_vwap_reclaim,
    "Exhaustion_Mean_Reversion_Engine": sc_exhaustion,
    "Structural_Break_Retest_Engine": sc_structural,
}

MIRROR = 200.0


def mirror_bars(bars, pivot: float = MIRROR):
    """Price reflection p -> pivot - p (high and low swap): a LONG pattern becomes SHORT."""
    return tuple(Candle(b.ts, round(pivot - b.open, 4), round(pivot - b.low, 4), round(pivot - b.high, 4),
                        round(pivot - b.close, 4), b.volume) for b in bars)


def mirror_prior(p):
    if p is None:
        return None
    m = lambda x: None if x is None else MIRROR - x
    return PriorLevels(p.day, m(p.pdl), m(p.pdh), m(p.pdc), p.or_volume_base, p.or_days, p.or_source)


def mirror_universe(u: dict, symbol: str = "TEST") -> dict:
    """The scenario stock reflected about 200; every filler stock about twice its own first
    open (so prices stay positive and the market's direction reverses)."""
    out = {}
    for s, si in u.items():
        pv = MIRROR if s == symbol else round(2 * si.bars[0].open, 2)
        m = lambda x: None if x is None else pv - x
        out[s] = StockInput(s, mirror_bars(si.bars, pv), m(si.previous_close), m(si.today_open),
                            si.healthy, si.health_reason)
    return out


def scenario_universe(name: str, symbol: str = "TEST", n_filler: int = 150, drift: float = 0.0002):
    """The scenario stock inside a liquid synthetic market of n_filler stocks."""
    bars, pc, prior = SCENARIOS[name]()
    u = filler_universe(n_filler, len(bars), drift=drift)
    u[symbol] = StockInput(symbol, bars, pc, bars[0].open)
    return u, {symbol: prior} if prior else {}, scan_after(len(bars))
