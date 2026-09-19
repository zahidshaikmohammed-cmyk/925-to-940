from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime, time as dtime
from time import time_ns
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from strategy_930 import Candle

IST = ZoneInfo("Asia/Kolkata")
ENDPOINT_PATH = "public/live-j.json"
EXPECTED_UNIVERSE = 990
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
    ltp: float
    previous_close: float | None
    health: Health


class PsygridClient:
    """Client for the canonical PSYGRID 990-stock public 1-minute OHLCV feed.

    Canonical endpoint: /public/live-j.json
    Top-level schema: universe_size, stock_count, stocks
    Stock schema: symbol, security_id, previous_close, today_open, candles_1m[]
    No 5m/15m/depth/LTP-timestamp feed is used.
    """

    def __init__(self, base_url: str, timeout: float = 4.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.last_market_errors: tuple[str, ...] = ()
        self.last_market_coverage: int = 0
        self.last_market_duplicates: tuple[str, ...] = ()
        self.last_market_meta: dict = {}

    def _get(self, path: str = ENDPOINT_PATH):
        url = self.base + "/" + path.lstrip("/")
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}_ts={time_ns()}"
        req = Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
                "User-Agent": "PSYGRID-925-TO-940/4.0",
            },
        )
        with urlopen(req, timeout=self.timeout) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))

    def preflight_all(self) -> dict[str, dict]:
        """Validate the single atomic 990-stock endpoint."""
        try:
            payload = self._get(ENDPOINT_PATH)
            if not isinstance(payload, dict):
                return {ENDPOINT_PATH: {"ok": False, "count": 0, "error": "payload is not an object"}}
            stocks = payload.get("stocks")
            if not isinstance(stocks, dict):
                return {ENDPOINT_PATH: {"ok": False, "count": 0, "error": "missing/invalid stocks object"}}
            count = len(stocks)
            declared = payload.get("stock_count")
            universe = payload.get("universe_size")
            errors: list[str] = []
            if universe not in (None, EXPECTED_UNIVERSE):
                errors.append(f"universe_size={universe}, expected={EXPECTED_UNIVERSE}")
            if declared != count:
                errors.append(f"declared stock_count={declared}, actual records={count}")
            if count == 0:
                errors.append("no stock records available")
            return {
                ENDPOINT_PATH: {
                    "ok": not errors,
                    "count": count,
                    "declared": declared,
                    "universe_size": universe,
                    "error": "; ".join(errors) if errors else None,
                }
            }
        except Exception as exc:
            return {ENDPOINT_PATH: {"ok": False, "count": 0, "error": str(exc)}}

    def market(self) -> dict:
        """Fetch one atomic 990-stock snapshot and retain every valid stock payload."""
        errors: list[str] = []
        duplicates: list[str] = []
        try:
            payload = self._get(ENDPOINT_PATH)
        except Exception as exc:
            self.last_market_errors = (f"{ENDPOINT_PATH}: {exc}",)
            self.last_market_coverage = 0
            self.last_market_duplicates = ()
            self.last_market_meta = {}
            raise RuntimeError(f"990-stock endpoint unavailable: {exc}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("990-stock endpoint returned a non-object payload")
        stocks = payload.get("stocks")
        if not isinstance(stocks, dict):
            raise RuntimeError("990-stock endpoint missing/invalid stocks object")

        declared = payload.get("stock_count")
        universe = payload.get("universe_size")
        service_status = payload.get("status")
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}

        if universe not in (None, EXPECTED_UNIVERSE):
            errors.append(f"endpoint universe_size={universe}, expected={EXPECTED_UNIVERSE}")
        if declared != len(stocks):
            errors.append(
                f"endpoint declared stock_count={declared}, actual records={len(stocks)}; valid records retained"
            )

        out: dict = {}
        for symbol, stock in stocks.items():
            if not isinstance(symbol, str) or not symbol.strip():
                errors.append("invalid blank symbol skipped")
                continue
            if not isinstance(stock, dict):
                errors.append(f"{symbol}: invalid stock payload skipped")
                continue
            if symbol in out:
                duplicates.append(symbol)
                continue
            out[symbol] = stock

        self.last_market_errors = tuple(errors)
        self.last_market_duplicates = tuple(sorted(set(duplicates)))
        self.last_market_coverage = len(out)
        self.last_market_meta = {
            "endpoint": ENDPOINT_PATH,
            "universe_size": universe,
            "declared_stock_count": declared,
            "actual_stock_count": len(stocks),
            "status": service_status,
            "session": session,
        }

        if errors:
            print(f"[FEED-WARN] {len(errors)} endpoint/schema issue(s); valid records retained")
        if duplicates:
            print(f"[FEED-WARN] {len(set(duplicates))} duplicate symbol(s); duplicate copies skipped")
        if not out:
            raise RuntimeError("990-stock endpoint returned no valid stock records")
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
        """Create a stock snapshot from the endpoint's 1m OHLCV series."""
        now_ist = (now or datetime.now(IST)).astimezone(IST)
        reasons: list[str] = []
        rows = payload.get("candles_1m")
        if rows is None:
            rows = payload.get("1m", [])
        all_candles = self.candles(rows)

        session = tuple(
            c for c in all_candles
            if c.ts.astimezone(IST).date() == now_ist.date()
            and SESSION_START <= c.ts.astimezone(IST).time() < MARKET_CLOSE
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

        return StockData(
            symbol=symbol,
            candles=session,
            ltp=ltp,
            previous_close=previous_close,
            health=Health(symbol, not reasons, ";".join(reasons)),
        )
