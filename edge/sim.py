"""Trade simulation on 5-minute OHLC with no look-ahead.

Rules (fixed for every family):
* A signal is formed on the CLOSE of bar k. The trade enters at the OPEN of bar k+1 -- the
  first price available after the signal. If bar k+1 has no candle, there is no trade.
* The stop is known at the signal; the target is set from the actual entry (R = |entry - stop|).
  If the entry price is already at or beyond the stop, there is no trade.
* From the entry bar on, each bar is checked: a bar opening beyond the stop fills at its open
  (gap), else a bar touching the stop fills at the stop; then the target likewise.
* When one bar touches BOTH stop and target, OHLC cannot tell which came first. The base result
  assumes the STOP (conservative); the optimistic alternative (target first) is recorded too.
* Time exit at the close of the last bar in the holding window (hold = 3 bars = 15 minutes,
  6 bars = 30 minutes), never later than the 15:10 bar's close (the 15:15 square-off).
* Missing candles inside the window are skipped (no price is invented); the next real candle's
  open decides any gap through the stop or target.
"""
from __future__ import annotations

from dataclasses import dataclass

from .data import LAST_EXIT_SLOT, DayBars

MAX_RISK_PCT = 3.0
MIN_RISK_PCT = 0.10


@dataclass
class Trade:
    family: str
    variant: str
    symbol: str
    day: object
    signal_slot: int
    entry_slot: int
    side: int
    entry: float
    stop: float
    target: float | None
    exit: float
    exit_slot: int
    reason: str
    ambiguous: bool
    gross_pct: float
    gross_pct_optimistic: float
    risk_pct: float


def simulate(day: DayBars, k: int, side: int, stop: float, target_r: float | None, hold: int):
    """Returns (entry_slot, entry, target, exit, exit_slot, reason, ambiguous, optimistic_exit) or None."""
    e = k + 1
    if e > LAST_EXIT_SLOT or day.o[e] is None:
        return None
    entry = day.o[e]
    risk = side * (entry - stop)
    if risk <= 0:
        return None
    risk_pct = risk / entry * 100
    if risk_pct > MAX_RISK_PCT or risk_pct < MIN_RISK_PCT:
        return None
    target = entry + side * target_r * risk if target_r else None
    last = min(e + hold - 1, LAST_EXIT_SLOT)
    exit_price, exit_slot, reason, ambiguous, optimistic = None, None, "TIME", False, None
    for s in range(e, last + 1):
        if day.c[s] is None:
            continue
        o, h, lo = day.o[s], day.h[s], day.l[s]
        stop_hit = (lo <= stop) if side > 0 else (h >= stop)
        tgt_hit = target is not None and ((h >= target) if side > 0 else (lo <= target))
        if stop_hit:
            gapped = (o <= stop) if side > 0 else (o >= stop)
            exit_price = o if gapped else stop
            exit_slot, reason = s, "STOP"
            if tgt_hit and not gapped:
                ambiguous = True
                optimistic = target
            break
        if tgt_hit:
            gapped = (o >= target) if side > 0 else (o <= target)
            exit_price = o if gapped else target
            exit_slot, reason = s, "TARGET"
            break
    if exit_price is None:
        for s in range(last, e - 1, -1):
            if day.c[s] is not None:
                exit_price, exit_slot = day.c[s], s
                break
    if exit_price is None:
        return None
    return e, entry, target, exit_price, exit_slot, reason, ambiguous, optimistic if optimistic else exit_price


def make_trade(family, variant, symbol, day_key, day: DayBars, k, side, stop, target_r, hold) -> Trade | None:
    r = simulate(day, k, side, stop, target_r, hold)
    if r is None:
        return None
    e, entry, target, exit_price, exit_slot, reason, ambiguous, optimistic = r
    return Trade(family, variant, symbol, day_key, k, e, side, entry, stop, target, exit_price, exit_slot, reason,
                 ambiguous, side * (exit_price / entry - 1) * 100, side * (optimistic / entry - 1) * 100,
                 side * (entry - stop) / entry * 100)
