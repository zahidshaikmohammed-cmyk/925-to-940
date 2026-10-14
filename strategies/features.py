"""Per-stock inputs and feature series, computed once per scan and shared read-only.

Every series is a tuple aligned with ``bars`` (index i = the i-th completed 1-minute bar of
today's session). Engines receive the same object; tuples cannot be mutated, so one engine
cannot alter what another engine sees.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from statistics import median

from strategy_930 import Candle

from . import indicators as ind

ATR_BARS = 14
SESSION_OPEN = time(9, 15)


@dataclass(frozen=True)
class PriorLevels:
    """Levels known before today's open, from saved sessions of the previous trading day."""
    day: str                       # ISO date of the previous trading day used
    pdh: float | None              # previous day high
    pdl: float | None              # previous day low
    pdc: float | None              # previous day close (last 1-minute close of that session)
    or_volume_base: float | None   # median 09:15-09:29 volume over the prior sessions
    or_days: int                   # sessions behind or_volume_base
    or_source: str = ""            # "psygrid" (same feed as live) or "yahoo" (scale may differ)


@dataclass(frozen=True)
class StockView:
    symbol: str
    bars: tuple[Candle, ...]       # today's completed 1-minute bars, validated by quality.py
    previous_close: float | None   # from the feed
    today_open: float | None       # from the feed (else None)
    prior: PriorLevels | None = None


class Features:
    __slots__ = ("o", "h", "l", "c", "v", "ts", "tr", "atr", "vwap", "vwap_sd", "n", "_sides")

    def __init__(self, bars: tuple[Candle, ...]):
        self.ts = tuple(b.ts for b in bars)
        self.o = tuple(b.open for b in bars)
        self.h = tuple(b.high for b in bars)
        self.l = tuple(b.low for b in bars)
        self.c = tuple(b.close for b in bars)
        self.v = tuple(b.volume for b in bars)
        self.n = len(bars)
        self.tr = ind.true_ranges(self.h, self.l, self.c)
        self.atr = ind.rolling_mean(self.tr, ATR_BARS)
        self.vwap, self.vwap_sd = ind.vwap_series(self.h, self.l, self.c, self.v)
        self._sides = {}

    def side(self, s: int) -> "Side":
        """The cached direction-normalised view (+1 LONG, -1 SHORT)."""
        if s not in self._sides:
            self._sides[s] = Side(self, s)
        return self._sides[s]

    @property
    def t(self) -> int:
        """Index of the newest completed bar (the decision bar)."""
        return self.n - 1

    def med_volume(self, end: int, n: int) -> float | None:
        """Median volume of the n bars before index ``end`` (exclusive)."""
        lo = max(0, end - n)
        return median(self.v[lo:end]) if end > lo else None

    def fingerprint(self) -> int:
        return hash((self.ts, self.o, self.h, self.l, self.c, self.v, self.tr, self.atr, self.vwap, self.vwap_sd))


def five_minute_bars(bars: tuple[Candle, ...]) -> list[Candle]:
    """Aggregate 1-minute bars into 5-minute bars on the 09:15 grid.

    A 5-minute bar is emitted only when its last minute (start + 4) is not after the newest
    1-minute bar, i.e. the bucket has fully elapsed. Missing minutes inside a bucket are not
    filled; the bucket is built from the minutes that exist.
    """
    if not bars:
        return []
    last = bars[-1].ts
    out: list[Candle] = []
    cur = None
    for b in bars:
        mins = b.ts.hour * 60 + b.ts.minute - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
        start = b.ts - timedelta(minutes=mins % 5)
        if start + timedelta(minutes=4) > last:
            break
        if cur is None or cur[0] != start:
            if cur is not None:
                out.append(Candle(*cur))
            cur = [start, b.open, b.high, b.low, b.close, b.volume]
        else:
            cur[2] = max(cur[2], b.high)
            cur[3] = min(cur[3], b.low)
            cur[4] = b.close
            cur[5] += b.volume
    if cur is not None:
        out.append(Candle(*cur))
    return out


def index_at_or_after(ts: tuple[datetime, ...], when: datetime) -> int | None:
    for i, t in enumerate(ts):
        if t >= when:
            return i
    return None


def _neg(x):
    return tuple(None if v is None else -v for v in x)


class Side:
    """A direction-normalised view of Features: LONG logic applied to a Side(-1) is exactly the
    SHORT logic. For s = -1 every price is negated and high/low swap (H' = -L, L' = -H,
    O' = -O, C' = -C, VWAP' = -VWAP); volumes, true ranges, ATR and the VWAP s.d. are
    direction-free and shared. ``real(x)`` maps a normalised price back to rupees."""
    __slots__ = ("s", "o", "h", "l", "c", "v", "ts", "tr", "atr", "vwap", "vwap_sd", "n", "base")

    def __init__(self, f: Features, s: int):
        if s not in (1, -1):
            raise ValueError("side must be +1 or -1")
        self.s, self.base = s, f
        self.v, self.ts, self.tr, self.atr, self.vwap_sd, self.n = f.v, f.ts, f.tr, f.atr, f.vwap_sd, f.n
        if s == 1:
            self.o, self.h, self.l, self.c, self.vwap = f.o, f.h, f.l, f.c, f.vwap
        else:
            self.o, self.h, self.l, self.c, self.vwap = _neg(f.o), _neg(f.l), _neg(f.h), _neg(f.c), _neg(f.vwap)

    @property
    def t(self) -> int:
        return self.n - 1

    def real(self, x: float) -> float:
        return self.s * x

    def norm(self, x: float) -> float:
        return self.s * x

    def med_volume(self, end: int, n: int):
        return self.base.med_volume(end, n)
