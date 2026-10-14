"""Data-validity gate applied to every stock before any engine sees it.

The outcome for each stock is one of: valid (a StockView), INVALID_DATA (the feed itself is
bad for this stock), INSUFFICIENT_DATA (too little / stale / gappy data to decide) or
INELIGIBLE (valid data but too illiquid or too cheap to trade). A stock that fails here is
reported under that status by every engine, so a data failure is never counted as
"no setup" and no stock is silently dropped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from statistics import median

from strategy_930 import IST, Candle

from .core import INELIGIBLE, INSUFFICIENT_DATA, INVALID_DATA
from .features import PriorLevels, StockView

SESSION_START = time(9, 15)
LAST_BAR = time(15, 29)
ONE_MIN = timedelta(minutes=1)


@dataclass(frozen=True)
class QualityConfig:
    max_age_minutes: int = 2            # newest bar must have closed within this many minutes
    gap_window_minutes: int = 30        # look for missing minutes over this recent span ...
    max_missing_in_window: int = 3      # ... and allow at most this many
    min_coverage: float = 0.90          # bars present / minutes elapsed since 09:15
    min_price: float = 20.0             # rupees
    min_median_turnover: float = 200_000.0  # rupees per minute, median of the last 30 bars
    turnover_bars: int = 30


@dataclass(frozen=True)
class QualityResult:
    symbol: str
    status: str | None          # None = valid
    reason: str
    view: StockView | None


def _finite_bar(b: Candle) -> bool:
    vals = (b.open, b.high, b.low, b.close, b.volume)
    return (all(isinstance(x, (int, float)) and math.isfinite(x) for x in vals)
            and min(b.open, b.high, b.low, b.close) > 0 and b.volume >= 0
            and b.high >= max(b.open, b.close) and b.low <= min(b.open, b.close))


def check_stock(symbol: str, bars, previous_close, today_open, healthy: bool, health_reason: str,
                scan_time: datetime, cfg: QualityConfig = QualityConfig(),
                prior: PriorLevels | None = None) -> QualityResult:
    """Validate one stock's completed bars as of ``scan_time`` (IST, aware)."""
    def bad(status, why):
        return QualityResult(symbol, status, why, None)

    unhealthy_status = INSUFFICIENT_DATA if "insufficient" in (health_reason or "") else INVALID_DATA
    if not healthy and not bars:
        return bad(unhealthy_status, f"feed_unhealthy:{health_reason or 'no_bars'}")
    if not bars:
        return bad(INSUFFICIENT_DATA, "no_completed_bars")
    scan = scan_time.astimezone(IST)
    minute = scan.replace(second=0, microsecond=0)
    prev_ts = None
    for b in bars:
        if not _finite_bar(b):
            return bad(INVALID_DATA, "malformed_ohlcv")
        ts = b.ts.astimezone(IST)
        if ts.second or ts.microsecond:
            return bad(INVALID_DATA, "timestamp_not_on_minute")
        if ts.date() != scan.date():
            return bad(INVALID_DATA, f"bar_from_other_day_{ts.date()}")
        if not SESSION_START <= ts.time() <= LAST_BAR:
            return bad(INVALID_DATA, f"bar_outside_session_{ts:%H:%M}")
        if ts + ONE_MIN > minute:
            return bad(INVALID_DATA, f"incomplete_or_future_bar_{ts:%H:%M}")
        if prev_ts is not None and ts <= prev_ts:
            return bad(INVALID_DATA, "duplicate_or_unordered_timestamps")
        prev_ts = ts
    first, last = bars[0].ts.astimezone(IST), bars[-1].ts.astimezone(IST)
    if first.time() != SESSION_START:
        return bad(INSUFFICIENT_DATA, f"missing_09:15_bar_first_{first:%H:%M}")
    age = int((minute - (last + ONE_MIN)).total_seconds() // 60)
    if age > cfg.max_age_minutes:
        return bad(INSUFFICIENT_DATA, f"stale_{age}m")
    elapsed = int((last - first).total_seconds() // 60) + 1
    if len(bars) / elapsed < cfg.min_coverage:
        return bad(INSUFFICIENT_DATA, f"coverage_{len(bars)}/{elapsed}")
    window_start = last - timedelta(minutes=cfg.gap_window_minutes - 1)
    expected = min(cfg.gap_window_minutes, elapsed)
    present = sum(1 for b in bars if b.ts.astimezone(IST) >= window_start)
    if expected - present > cfg.max_missing_in_window:
        return bad(INSUFFICIENT_DATA, f"gaps_{expected - present}_in_last_{cfg.gap_window_minutes}m")
    if not healthy:
        # bars parse fine but the feed client flagged the stock; respect it
        return bad(unhealthy_status, f"feed_unhealthy:{health_reason}")
    if bars[-1].close < cfg.min_price:
        return bad(INELIGIBLE, f"price_below_{cfg.min_price:g}")
    recent = bars[-cfg.turnover_bars:]
    turnover = median(b.close * b.volume for b in recent)
    if turnover < cfg.min_median_turnover:
        return bad(INELIGIBLE, f"median_turnover_{turnover:,.0f}_per_min")
    pc = previous_close if isinstance(previous_close, (int, float)) and math.isfinite(previous_close) \
        and previous_close > 0 else None
    to = today_open if isinstance(today_open, (int, float)) and math.isfinite(today_open) \
        and today_open > 0 else None
    bars_ist = tuple(Candle(b.ts.astimezone(IST), b.open, b.high, b.low, b.close, b.volume) for b in bars)
    return QualityResult(symbol, None, "", StockView(symbol, bars_ist, pc, to, prior))
