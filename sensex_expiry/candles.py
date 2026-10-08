"""Tick -> closed 1-minute candles, and closed 1-minute -> 5/15-minute candles.

Rules (spec section 6):
  * A candle labelled 09:20 covers ticks with exchange time in [09:20:00, 09:21:00).
  * A candle is CLOSED, and only then visible to features, when either a tick from a
    later minute arrives or the wall clock passes the boundary plus a grace period.
  * A late tick (exchange time inside an already-closed minute) is never applied
    retroactively: it is counted and reported, so history is never rewritten.
  * Minutes with no ticks produce no candle; the gap is reported, never filled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import Candle, Tick
from .session_calendar import minute_floor

ONE_MIN = timedelta(minutes=1)


@dataclass
class _Building:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    vol_start: int | None
    vol_last: int | None
    ticks: int = 1


@dataclass
class CandleBuilder:
    grace_s: float = 2.0
    closed: list[Candle] = field(default_factory=list)
    late_ticks: int = 0
    gaps: list[datetime] = field(default_factory=list)
    _cur: _Building | None = None
    _prev_cum_volume: int | None = None

    def on_tick(self, tick: Tick) -> list[Candle]:
        """Apply one tick. Returns the candles this tick closed (0 or 1)."""
        m = minute_floor(tick.ts)
        out: list[Candle] = []
        if self._cur is not None and m < self._cur.start:
            self.late_ticks += 1
            return out
        if self.closed and m <= self.closed[-1].start:
            self.late_ticks += 1
            return out
        if self._cur is not None and m > self._cur.start:
            out.append(self._close())
        if self._cur is None:
            if self.closed:
                expected = self.closed[-1].start + ONE_MIN
                while expected < m:
                    self.gaps.append(expected)
                    expected += ONE_MIN
            self._cur = _Building(m, tick.ltp, tick.ltp, tick.ltp, tick.ltp, tick.volume, tick.volume)
            return out
        c = self._cur
        c.high = max(c.high, tick.ltp)
        c.low = min(c.low, tick.ltp)
        c.close = tick.ltp
        c.ticks += 1
        if tick.volume is not None:
            c.vol_last = tick.volume
            if c.vol_start is None:
                c.vol_start = tick.volume
        return out

    def on_clock(self, now: datetime) -> list[Candle]:
        """Close the building candle once its minute plus grace has passed on the wall clock."""
        if self._cur is not None and now >= self._cur.start + ONE_MIN + timedelta(seconds=self.grace_s):
            return [self._close()]
        return []

    def _close(self) -> Candle:
        c = self._cur
        assert c is not None
        vol = 0
        if c.vol_last is not None:
            base = self._prev_cum_volume if self._prev_cum_volume is not None else c.vol_start
            vol = max(0, c.vol_last - (base or 0))
            self._prev_cum_volume = c.vol_last
        candle = Candle(c.start, c.open, c.high, c.low, c.close, vol, c.ticks)
        self.closed.append(candle)
        self._cur = None
        return candle


def resample(candles: list[Candle], minutes: int, session_open: datetime) -> list[Candle]:
    """Aggregate closed 1m candles into N-minute candles anchored at session_open.

    Only COMPLETE buckets are returned: a bucket is emitted when a candle from a later
    bucket exists, so a partially formed 5m bar is never visible (no look-ahead).
    """
    buckets: dict[datetime, list[Candle]] = {}
    for c in candles:
        k = int((c.start - session_open).total_seconds() // 60) // minutes
        buckets.setdefault(session_open + timedelta(minutes=k * minutes), []).append(c)
    keys = sorted(buckets)
    out = []
    for i, k in enumerate(keys):
        last_needed = k + timedelta(minutes=minutes - 1)
        complete = (i + 1 < len(keys)) or any(c.start == last_needed for c in buckets[k])
        if not complete:
            continue
        g = buckets[k]
        out.append(Candle(k, g[0].open, max(c.high for c in g), min(c.low for c in g), g[-1].close,
                          sum(c.volume for c in g), sum(c.ticks for c in g)))
    return out
