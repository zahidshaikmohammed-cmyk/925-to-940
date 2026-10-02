"""Real PSYGRID feed access + validation.

Reads only the existing public endpoints (no new acquisition, no writes, no new
listening port). A decision may be published ONLY if every check passes:

    trading_day        today is an NSE trading day
    session_date       the feed's session date is TODAY (never a previous day)
    feed_fresh         feed clock (session.current_time_ist) within max_feed_age of the
                       local clock; if the feed has no clock, the newest candle must be
                       recent (after 15:30 the newest candle must be >= 15:29 instead)
    cutoff_history     the 09:44 candle (last bar before the freeze) exists
    valid_universe     enough stocks have usable pre-09:45 history
"""
from __future__ import annotations

import contextlib
import io
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta

from psygrid_client import PsygridClient

from .selector_config import SelectorConfig
from .selector_data import IST, RawSession, SessionInput

STOCKS_PATH = "public/live.json"
INDEX_PATH = "public/nifty.json"


@dataclass(frozen=True)
class FeedHealth:
    ok: bool
    checks: tuple            # ((name, passed, detail), ...)
    local_time: str
    feed_clock: str | None
    feed_age_seconds: float | None
    session_date: str | None
    market_status: str | None
    universe_count: int
    valid_count: int
    missing_count: int
    latest_candle: str | None
    index_available: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [list(c) for c in self.checks]
        return d

    def failures(self) -> list[str]:
        return [f"{n}: {detail}" for n, passed, detail in self.checks if not passed]


def fetch_payloads(base_url: str, timeout: float = 15.0):
    """One atomic stock snapshot + optional NIFTY candles. Raises on stock-feed failure."""
    client = PsygridClient(base_url, timeout=timeout)
    with contextlib.redirect_stdout(io.StringIO()):
        stocks = client._get(STOCKS_PATH)
    try:
        index = client._get(INDEX_PATH)
    except Exception:
        index = None
    return stocks, index


def _feed_clock(raw: RawSession) -> datetime | None:
    session = raw.feed_meta.get("session") if isinstance(raw.feed_meta.get("session"), dict) else {}
    value = session.get("current_time_ist") or session.get("updated_at")
    if not value:
        return None
    try:
        return PsygridClient._ts(value)
    except Exception:
        return None


def validate_feed(raw: RawSession, si: SessionInput, now: datetime, cfg: SelectorConfig,
                  is_trading_day) -> FeedHealth:
    now = now.astimezone(IST)
    checks = []
    td = bool(is_trading_day(now.date()))
    checks.append(("trading_day", td, f"{now.date()} {'is' if td else 'is NOT'} a trading day"))
    same_day = raw.session_date == now.date()
    checks.append(("session_date", same_day, f"feed session {raw.session_date}, local date {now.date()}"))

    latest = max((s.ts[-1] for s in raw.stocks.values() if len(s)), default=None)
    clock = _feed_clock(raw)
    age = None
    if clock is not None:
        age = (now - clock).total_seconds()
        fresh = -60 <= age <= cfg.max_feed_age_seconds
        detail = f"feed clock {clock:%H:%M:%S}, age {age:.0f}s (max {cfg.max_feed_age_seconds}s)"
    elif latest is not None:
        if now.time() >= time(15, 30):
            fresh = latest.time() >= time(15, 29)
            detail = f"no feed clock; market closed, newest candle {latest:%H:%M}"
        else:
            age = (now - (latest + timedelta(minutes=1))).total_seconds()
            fresh = age <= cfg.max_feed_age_seconds
            detail = f"no feed clock; newest candle {latest:%H:%M}, age {age:.0f}s"
    else:
        fresh, detail = False, "feed contains no candles"
    if clock is not None and now.time() >= time(15, 30) and not fresh:
        # After the close the server may stop advancing its clock; accept a complete session.
        if latest is not None and latest.time() >= time(15, 29) and same_day:
            fresh, detail = True, detail + " -- market closed, full session present"
    checks.append(("feed_fresh", fresh, detail))

    need = si.cutoff - timedelta(minutes=1)
    has_cutoff_bar = sum(1 for s in si.stocks.values() if len(s) and s.ts[-1] >= need)
    checks.append(("cutoff_history", has_cutoff_bar > 0,
                   f"{has_cutoff_bar} stocks have the {need:%H:%M} candle"))

    universe = len(si.received_symbols)
    valid = sum(1 for s in si.stocks.values()
                if len(s) >= cfg.min_bars and (si.cutoff - s.ts[-1]).total_seconds() / 60 - 1 <= cfg.max_stale_minutes)
    need_valid = max(cfg.min_valid_stocks, int(cfg.min_valid_fraction * universe))
    checks.append(("valid_universe", valid >= need_valid,
                   f"{valid}/{universe} stocks with usable 09:45 history (need {need_valid})"))

    session = raw.feed_meta.get("session") if isinstance(raw.feed_meta.get("session"), dict) else {}
    status = session.get("status") or raw.feed_meta.get("status")
    return FeedHealth(
        ok=all(p for _, p, _ in checks), checks=tuple(checks), local_time=now.isoformat(),
        feed_clock=clock.isoformat() if clock else None, feed_age_seconds=age,
        session_date=raw.session_date.isoformat(), market_status=None if status is None else str(status),
        universe_count=universe, valid_count=valid, missing_count=universe - valid,
        latest_candle=latest.isoformat() if latest else None, index_available=si.index is not None,
    )


def render_health(h: FeedHealth) -> str:
    lines = ["FEED DIAGNOSTICS",
             f"  local time     : {h.local_time}",
             f"  feed clock     : {h.feed_clock or 'not provided'}  (age {'n/a' if h.feed_age_seconds is None else f'{h.feed_age_seconds:.0f}s'})",
             f"  session date   : {h.session_date} | market status {h.market_status}",
             f"  universe/valid : {h.universe_count} received, {h.valid_count} valid, {h.missing_count} missing/unusable",
             f"  newest candle  : {h.latest_candle} | NIFTY candles {'yes' if h.index_available else 'no (universe median used)'}"]
    lines += [f"  [{'PASS' if p else 'FAIL'}] {n:<15} {d}" for n, p, d in h.checks]
    return "\n".join(lines)
