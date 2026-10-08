"""Causal features of the SENSEX underlying (spec sections 8, 10-13).

Every function takes the list of CLOSED candles up to and including bar t and uses
nothing after t. Swing points are confirmed `k` bars after the pivot and are only
returned once confirmed. tests/test_sensex_expiry_core.py proves this by truncation.

The SENSEX index has no traded volume (FACT: an index is not a traded instrument, so
Dhan's IDX_I feed carries LTP only). Volume, and therefore VWAP, cannot be computed
from the underlying; see spec section 12 for why VWAP was removed from v1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import FeatureConfig
from .models import Candle, PriorDay


def true_ranges(c: list[Candle], prev_close: float | None = None) -> list[float]:
    out = []
    pc = prev_close
    for x in c:
        out.append(x.range if pc is None else max(x.high - x.low, abs(x.high - pc), abs(x.low - pc)))
        pc = x.close
    return out


def atr(c: list[Candle], n: int, prev_close: float | None = None) -> float | None:
    """Wilder ATR over closed bars; seeded once at least min(n, 5) bars exist."""
    tr = true_ranges(c, prev_close)
    if len(tr) < min(n, 5):
        return None
    seed_n = min(n, len(tr))
    a = sum(tr[:seed_n]) / seed_n
    for x in tr[seed_n:]:
        a = (a * (n - 1) + x) / n
    return a


def opening_range(c: list[Candle], session_open: datetime, minutes: int) -> tuple[float, float] | None:
    """(high, low) of the first `minutes` 1m candles; None until the last of them has closed."""
    end = session_open + timedelta(minutes=minutes)
    window = [x for x in c if session_open <= x.start < end]
    if not c or c[-1].start < end - timedelta(minutes=1) or len(window) < max(1, minutes - 1):
        return None
    return max(x.high for x in window), min(x.low for x in window)


def efficiency_ratio(c: list[Candle], n: int) -> float | None:
    if len(c) < n + 1:
        return None
    closes = [x.close for x in c[-(n + 1):]]
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return abs(closes[-1] - closes[0]) / path if path > 0 else 0.0


def compression_ratio(c: list[Candle], n: int, atr_value: float | None) -> float | None:
    """Range of the last n bars / (ATR * sqrt(n)). ~1 for a random walk; low = compressed."""
    if atr_value is None or atr_value <= 0 or len(c) < n:
        return None
    w = c[-n:]
    return (max(x.high for x in w) - min(x.low for x in w)) / (atr_value * n ** 0.5)


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: str        # "H" or "L"
    confirmed_at: int


def swings(c: list[Candle], k: int) -> list[Swing]:
    """Fractal pivots confirmed k bars later. Bar j is a swing high if its high is strictly
    above the k bars before and at least the k bars after; only j + k <= t is returned."""
    out = []
    for j in range(k, len(c) - k):
        h, lo = c[j].high, c[j].low
        if h > max(x.high for x in c[j - k:j]) and h >= max(x.high for x in c[j + 1:j + k + 1]):
            out.append(Swing(j, h, "H", j + k))
        if lo < min(x.low for x in c[j - k:j]) and lo <= min(x.low for x in c[j + 1:j + k + 1]):
            out.append(Swing(j, lo, "L", j + k))
    return out


def structure(sw: list[Swing]) -> str:
    """UP = last two swing highs and lows both rising; DOWN = both falling; else MIXED."""
    hs = [s.price for s in sw if s.kind == "H"][-2:]
    ls = [s.price for s in sw if s.kind == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return "MIXED"
    if hs[1] > hs[0] and ls[1] > ls[0]:
        return "UP"
    if hs[1] < hs[0] and ls[1] < ls[0]:
        return "DOWN"
    return "MIXED"


def is_displacement(x: Candle, atr_value: float | None, cfg: FeatureConfig, direction: int) -> bool:
    """Wide bar, mostly body, closing in its outer quarter in `direction` (+1 up, -1 down)."""
    if atr_value is None or x.range <= 0:
        return False
    if x.range < cfg.displacement_range_atr * atr_value or x.body < cfg.displacement_body_frac * x.range:
        return False
    pos = (x.close - x.low) / x.range
    return pos >= 0.75 if direction > 0 else pos <= 0.25


def linreg_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2
    my = sum(values) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (v - my) for i, v in enumerate(values)) / den


@dataclass(frozen=True)
class Levels:
    """Reference levels known at bar t, each with the bar index from which it is valid."""
    items: tuple[tuple[str, float, int], ...]

    def below(self, price: float) -> list[tuple[str, float, int]]:
        return sorted((x for x in self.items if x[1] < price), key=lambda x: -x[1])

    def above(self, price: float) -> list[tuple[str, float, int]]:
        return sorted((x for x in self.items if x[1] > price), key=lambda x: x[1])


def reference_levels(c: list[Candle], session_open: datetime, prior: PriorDay | None,
                     cfg: FeatureConfig) -> Levels:
    items: list[tuple[str, float, int]] = []
    if prior is not None:
        items += [("PDH", prior.high, -1), ("PDL", prior.low, -1), ("PDC", prior.close, -1)]
    orng = opening_range(c, session_open, cfg.or_minutes)
    if orng is not None:
        born = cfg.or_minutes - 1
        items += [("ORH", orng[0], born), ("ORL", orng[1], born)]
    for s in swings(c, cfg.swing_k):
        items.append((f"SW{s.kind}@{c[s.index].start:%H%M}", s.price, s.confirmed_at))
    return Levels(tuple(items))


@dataclass(frozen=True)
class FeatureSnapshot:
    t: int
    ts: datetime
    close: float
    atr: float | None
    atr_slow: float | None
    er: float | None
    compression: float | None
    structure: str
    orh: float | None
    orl: float | None
    session_high: float
    session_low: float
    slope: float
    last_tr: float


def snapshot(c: list[Candle], session_open: datetime, prior: PriorDay | None, cfg: FeatureConfig) -> FeatureSnapshot:
    pc = prior.close if prior else None
    a = atr(c, cfg.atr_period, pc)
    a_slow = atr(c, 60, pc)
    orng = opening_range(c, session_open, cfg.or_minutes)
    tr = true_ranges(c, pc)
    return FeatureSnapshot(
        t=len(c) - 1, ts=c[-1].start, close=c[-1].close, atr=a, atr_slow=a_slow,
        er=efficiency_ratio(c, cfg.er_window),
        compression=compression_ratio(c, cfg.compression_window, a_slow or a),
        structure=structure(swings(c, cfg.swing_k)),
        orh=orng[0] if orng else None, orl=orng[1] if orng else None,
        session_high=max(x.high for x in c), session_low=min(x.low for x in c),
        slope=linreg_slope([x.close for x in c[-cfg.er_window:]]),
        last_tr=tr[-1] if tr else 0.0)
