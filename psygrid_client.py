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


@dataclass(frozen=True)
class Health:
    symbol: str
    healthy: bool
    reason: str = ""


@dataclass(frozen=True)
class StockData:
    symbol: str
    candles: tuple[Candle, ...]
    # The endpoint is 1m-OHLCV only. For execution/ranking the engine uses
    # the latest completed 1m close as the current observable price.
    ltp: float
    # Optional endpoint metadata. Strategy calculations do not depend on it.
    previous_close: float | None
    health: Health


class PsygridClient:
    """Client for the canonical PSYGRID public 1-minute OHLCV feed.

    Canonical stock schema:
      symbol, security_id, candles_1m[]
    Optional metadata such as previous_close/today_open may exist, but is not
    required for signal generation. No 5m/15m/depth/LTP-timestamp feed is used.
    """

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
                "User-Agent": "PSYGRID-925-TO-940/3.0",
            },
        )
        with urlopen(req, timeout=self.timeout) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))

    def preflight_all(self) -> dict[str, dict]:
        """Check every public shard independently; never make one shard fatal."""
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
                    # A shard is operational if its declared and actual counts
                    # agree with the intended 45-stock partition.
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
        """Fetch all shards and retain every valid unique stock payload."""
        out: dict = {}
        errors: list[str] = []
        duplicates: list[str] = []

        with ThreadPoolExecutor(max_workers=len(SHARDS)) as executor:
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
        """Parse only genuine 1-minute OHLCV records."""
        out: list[Candle] = []
        if not isinstance(rows, list):
            return ()
        for row in rows:
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
                if candle.open <= 0 or candle.high <= 0 or candle.low <= 0 or candle.close <= 0:
                    continue
                if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
                    continue
                if candle.volume < 0:
                    continue
            except Exception:
                continue
            out.append(candle)
        return tuple(sorted(out, key=lambda c: c.ts))

    def stock(self, symbol: str, payload: dict, now: datetime | None = None) -> StockData:
        """Create a stock snapshot from the endpoint's 1m OHLCV series.

        There is deliberately NO stale-LTP check. The endpoint does not expose
        a separate LTP timestamp. The latest completed 1m close is the current
        observable price, and only session candle availability is validated.
        """
        now_ist = (now or datetime.now(IST)).astimezone(IST)
        reasons: list[str] = []

        # Canonical endpoint key is candles_1m. Keep a read-only compatibility
        # fallback for older fixtures; no 5m/15m data is consumed.
        rows = payload.get("candles_1m")
        if rows is None:
            rows = payload.get("1m", [])
        all_candles = self.candles(rows)

        session = tuple(
            c for c in all_candles
            if c.ts.astimezone(IST).date() == now_ist.date()
            and SESSION_START <= c.ts.astimezone(IST).time() < MARKET_CLOSE
            # Never use the currently forming minute as completed OHLCV.
            and c.ts.astimezone(IST) < now_ist.replace(second=0, microsecond=0)
        )
        if len(session) < 5:
            reasons.append(f"insufficient_completed_1m_{len(session)}")

        ltp = session[-1].close if session else 0.0
        if ltp <= 0:
            reasons.append("no_usable_completed_1m_close")

        previous_close = payload.get("previous_close")
        try:
            previous_close = float(previous_close) if previous_close is not None else None
            if previous_close is not None and previous_close <= 0:
                previous_close = None
        except (TypeError, ValueError):
            previous_close = None
        # previous_close and today_open are optional metadata only. They never
        # affect health or signal generation.

        return StockData(
            symbol=symbol,
            candles=session,
            ltp=ltp,
            previous_close=previous_close,
            health=Health(symbol, not reasons, ";".join(reasons)),
        )
