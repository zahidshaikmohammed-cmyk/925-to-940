from __future__ import annotations

import gzip
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time as dtime
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from strategy_930 import Candle

IST = ZoneInfo("Asia/Kolkata")
SHARDS = tuple(f"live-{x}.json" for x in "abcdefghij")
SESSION_START = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# Feed freshness is intentionally measured against the local PC clock. A few
# seconds of publication/network delay is normal and must NOT invalidate a
# stock. Only a materially stale snapshot (>4 minutes) is rejected.
MAX_LTP_AGE_SECONDS = 240.0
MAX_CANDLE_FRESHNESS_SECONDS = 240.0
MAX_FUTURE_SKEW_SECONDS = 60.0


@dataclass(frozen=True)
class Health:
    symbol: str
    healthy: bool
    reason: str = ""


@dataclass(frozen=True)
class StockData:
    symbol: str
    candles: tuple[Candle, ...]
    ltp: float
    previous_close: float | None
    health: Health


class PsygridClient:
    def __init__(self, base_url: str, timeout: float = 4.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.last_market_errors: tuple[str, ...] = ()
        self.last_market_coverage: int = 0
        self.last_market_duplicates: tuple[str, ...] = ()

    def _get(self, path: str):
        req = Request(
            self.base + "/" + path.lstrip("/"),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
                "User-Agent": "PSYGRID-925-TO-940/2.0",
            },
        )
        with urlopen(req, timeout=self.timeout) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))

    def ping(self) -> dict:
        payload = self._get("public/live-a.json")
        if not isinstance(payload, dict):
            raise RuntimeError("live-a.json is not a JSON object")
        if payload.get("stock_count") != 45:
            raise RuntimeError(f"live-a.json expected 45 stocks, got {payload.get('stock_count')}")
        return payload

    def preflight_all(self) -> dict[str, dict]:
        """Check every public shard independently without making partial data fatal."""
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=len(SHARDS)) as executor:
            futures = {executor.submit(self._get, f"public/{shard}"): shard for shard in SHARDS}
            for future in as_completed(futures):
                shard = futures[future]
                try:
                    payload = future.result()
                    if not isinstance(payload, dict):
                        results[shard] = {"ok": False, "count": 0, "error": "payload is not an object"}
                        continue
                    stocks = payload.get("stocks")
                    if not isinstance(stocks, dict):
                        results[shard] = {"ok": False, "count": 0, "error": "missing/invalid stocks object"}
                        continue
                    count = len(stocks)
                    declared = payload.get("stock_count")
                    ok = count == 45 and declared == 45
                    results[shard] = {
                        "ok": ok,
                        "count": count,
                        "declared": declared,
                        "error": None if ok else f"declared={declared}, records={count}, expected=45",
                    }
                except Exception as exc:
                    results[shard] = {"ok": False, "count": 0, "error": str(exc)}
        return {shard: results.get(shard, {"ok": False, "count": 0, "error": "no result"}) for shard in SHARDS}

    def market(self) -> dict:
        """Fetch all shards and keep every valid unique stock that is available."""
        out: dict = {}
        errors: list[str] = []
        duplicates: list[str] = []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self._get, f"public/{s}"): s for s in SHARDS}
            for future in as_completed(futures):
                shard = futures[future]
                try:
                    payload = future.result()
                    if not isinstance(payload, dict):
                        errors.append(f"{shard}: payload is not an object")
                        continue
                    stocks = payload.get("stocks")
                    if not isinstance(stocks, dict):
                        errors.append(f"{shard}: missing/invalid stocks object")
                        continue
                    declared = payload.get("stock_count")
                    if declared != 45:
                        errors.append(f"{shard}: declared stock_count={declared}, expected 45; valid records retained")
                    for symbol, stock in stocks.items():
                        if not isinstance(symbol, str) or not symbol.strip():
                            errors.append(f"{shard}: invalid blank symbol skipped")
                            continue
                        if symbol in out:
                            duplicates.append(symbol)
                            continue
                        if not isinstance(stock, dict):
                            errors.append(f"{shard}: {symbol}: invalid stock payload skipped")
                            continue
                        out[symbol] = stock
                except Exception as exc:
                    errors.append(f"{shard}: {exc}")

        self.last_market_errors = tuple(errors)
        self.last_market_duplicates = tuple(sorted(set(duplicates)))
        self.last_market_coverage = len(out)
        if errors:
            print(f"[FEED-WARN] {len(errors)} shard/record issue(s); affected items skipped")
        if duplicates:
            print(f"[FEED-WARN] {len(set(duplicates))} duplicate symbol(s); duplicate copies skipped")
        if not out:
            raise RuntimeError("all shards failed or returned no valid stocks")
        return out

    @staticmethod
    def _ts(value) -> datetime:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), IST)
        text = str(value).replace(" IST", "").strip()
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST)
        return dt.astimezone(IST)

    @classmethod
    def candles(cls, rows) -> tuple[Candle, ...]:
        out: list[Candle] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("complete", True) is False:
                continue
            try:
                candle = Candle(
                    cls._ts(row["timestamp"]),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),
                )
            except Exception:
                continue
            out.append(candle)
        return tuple(sorted(out, key=lambda c: c.ts))

    def stock(self, symbol: str, payload: dict, now: datetime | None = None) -> StockData:
        """Build a live snapshot containing completed 1m candles today.

        Previous close is optional metadata. It is never a feed-health gate.
        A normal few-second LTP/network delay is tolerated. A stock is marked
        stale only when the snapshot is more than four minutes behind the local
        PC clock.
        """
        now = now or datetime.now(IST)
        now_ist = now.astimezone(IST)
        reasons: list[str] = []
        all_candles = self.candles(payload.get("1m", []))

        try:
            ltp = float(payload.get("ltp") or 0.0)
        except (TypeError, ValueError):
            ltp = 0.0
        if ltp <= 0:
            reasons.append("invalid_ltp")

        ltp_timestamp = payload.get("ltp_timestamp")
        if not ltp_timestamp:
            latest = all_candles[-1].ts if all_candles else None
            if latest is None:
                reasons.append("missing_ltp_timestamp_and_no_completed_candle")
            else:
                candle_age = (now_ist - latest).total_seconds()
                if candle_age < -MAX_FUTURE_SKEW_SECONDS:
                    reasons.append(f"future_latest_candle_{candle_age:.1f}s")
                elif candle_age > MAX_CANDLE_FRESHNESS_SECONDS:
                    reasons.append(f"stale_completed_candle_{candle_age:.1f}s")
        else:
            try:
                age = (now_ist - self._ts(ltp_timestamp)).total_seconds()
                if age < -MAX_FUTURE_SKEW_SECONDS:
                    reasons.append(f"future_ltp_timestamp_{age:.1f}s")
                elif age > MAX_LTP_AGE_SECONDS:
                    reasons.append(f"stale_ltp_{age:.1f}s")
            except Exception:
                reasons.append("invalid_ltp_timestamp")

        session = tuple(
            c for c in all_candles
            if c.ts.astimezone(IST).date() == now_ist.date()
            and SESSION_START <= c.ts.astimezone(IST).time() < MARKET_CLOSE
            and c.ts < now_ist.replace(second=0, microsecond=0)
        )
        if len(session) < 5:
            reasons.append(f"insufficient_completed_1m_{len(session)}")

        previous_close = payload.get("previous_close")
        try:
            previous_close = float(previous_close) if previous_close is not None else None
        except (TypeError, ValueError):
            previous_close = None
        # Optional metadata only. Missing/invalid previous close does not
        # invalidate the stock or prevent signal generation.

        return StockData(
            symbol=symbol,
            candles=session,
            ltp=ltp,
            previous_close=previous_close,
            health=Health(symbol, not reasons, ";".join(reasons)),
        )