"""Deterministic synthetic PSYGRID payloads for tests and the 989-stock benchmark.

Produces the exact /public/live.json shape (stocks -> candles_1m rows with
"YYYY-MM-DD HH:MM:SS IST" timestamps, previous_close, today_open). Never used by the
live or backtest decision path.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def make_payload(n_stocks: int = 989, session: date = date(2026, 9, 30), minutes: int = 375,
                 seed: int = 7, planted: dict | None = None, broken: bool = True,
                 planted_size: float = 250000.0) -> dict:
    """minutes=30 -> 09:15..09:44 only; 375 -> full session to 15:29.

    planted: {symbol: drift_pct_per_minute} forces a clean trend on chosen stocks.
    broken:  adds realistic damage (missing candles, stale feed, junk rows, no prev close).
    """
    rnd = random.Random(seed)
    t0 = datetime(session.year, session.month, session.day, 9, 15, tzinfo=IST)
    planted = planted or {}
    stocks = {}
    for i in range(n_stocks):
        sym = f"STK{i:04d}"
        prev_close = round(rnd.uniform(50, 3000), 2)
        gap = rnd.gauss(0, 0.6) / 100.0
        price = prev_close * (1 + gap)
        drift = planted.get(sym, rnd.gauss(0, 0.012)) / 100.0
        vol = rnd.uniform(0.0008, 0.0035)
        size = rnd.uniform(500, 200000)
        if sym in planted:
            size, vol = planted_size, 0.0006
        rows = []
        for k in range(minutes):
            o = price
            c = max(0.5, o * (1 + drift + rnd.gauss(0, vol)))
            hi = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
            lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
            v = int(size * (2.5 if k < 15 else 1.0) * rnd.uniform(0.4, 1.6))
            rows.append({"timestamp": (t0 + timedelta(minutes=k)).strftime("%Y-%m-%d %H:%M:%S IST"),
                         "open": round(o, 2), "high": round(hi, 2), "low": round(lo, 2),
                         "close": round(c, 2), "volume": v})
            price = c
        payload = {"symbol": sym, "security_id": str(1000 + i), "previous_close": prev_close,
                   "today_open": rows[0]["open"] if rows else None, "candles_1m": rows}
        if broken and sym not in planted:
            r = i % 97
            if r == 1:
                payload["candles_1m"] = rows[:5]                       # too few bars
            elif r == 2:
                payload["candles_1m"] = [x for j, x in enumerate(rows) if not 20 <= j < 30] if minutes >= 30 else rows[:20]
            elif r == 3:
                payload["previous_close"] = None                       # missing metadata
            elif r == 4:
                payload["candles_1m"] = rows[:1] + [{"timestamp": "bad", "open": "x"}]
            elif r == 5:
                payload = "not-an-object"
        stocks[sym] = payload
    return {"service": "PSYGRID", "universe_size": 990, "stock_count": n_stocks, "status": "OK",
            "session": {"date": session.isoformat(), "status": "LIVE"}, "stocks": stocks}


def make_index_payload(session: date = date(2026, 9, 30), minutes: int = 375, seed: int = 11,
                       drift_pct: float = 0.0) -> dict:
    rnd = random.Random(seed)
    t0 = datetime(session.year, session.month, session.day, 9, 15, tzinfo=IST)
    price, rows = 22500.0, []
    for k in range(minutes):
        o = price
        c = o * (1 + drift_pct / 100.0 + rnd.gauss(0, 0.0004))
        rows.append({"timestamp": (t0 + timedelta(minutes=k)).strftime("%Y-%m-%d %H:%M:%S IST"),
                     "open": round(o, 2), "high": round(max(o, c) + 2, 2), "low": round(min(o, c) - 2, 2),
                     "close": round(c, 2), "volume": 1000})
        price = c
    return {"symbol": "NIFTY", "1m": rows}
