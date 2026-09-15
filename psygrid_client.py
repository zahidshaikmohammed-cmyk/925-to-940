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

    def _get(self, path: str):
        req = Request(
            self.base + "/" + path.lstrip("/"),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
                "User-Agent": "PSYGRID-925-TO-940/1.0",
            },
        )
        with urlopen(req, timeout=self.timeout) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))

    def ping(self) -> dict:
        """Fetch one public shard as a low-cost connectivity preflight."""
        payload = self._get("public/live-a.json")
        if not isinstance(payload, dict):
            raise RuntimeError("live-a.json is not a JSON object")
        if payload.get("stock_count") != 45:
            raise RuntimeError(f"live-a.json expected 45 stocks, got {payload.get('stock_count')}")
        return payload

    def market(self) -> dict:
        """Fetch all ten 45-stock shards concurrently and require exact 450 coverage."""
        out: dict = {}
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self._get, f"public/{s}"): s for s in SHARDS}
            for future in as_completed(futures):
                shard = futures[future]
                try:
                    payload = future.result()
                    stocks = payload.get("stocks")
                    if payload.get("stock_count") != 45 or not isinstance(stocks, dict):
                        raise RuntimeError(f"{shard}: invalid shard")
                    overlap = set(out) & set(stocks)
                    if overlap:
                        raise RuntimeError(f"{shard}: duplicate symbols {sorted(overlap)[:5]}")
                    out.update(stocks)
                except Exception as exc:
                    errors.append(f"{shard}: {exc}")
        if errors:
            raise RuntimeError(" | ".join(errors))
        if len(out) != 450:
            raise RuntimeError(f"expected exactly 450 stocks, got {len(out)}")
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
        now = now or datetime.now(IST)
        reasons: list[str] = []
        candles = self.candles(payload.get("1m", []))

        try:
            ltp = float(payload.get("ltp") or 0.0)
        except (TypeError, ValueError):
            ltp = 0.0
        if ltp <= 0:
            reasons.append("invalid_ltp")

        ltp_timestamp = payload.get("ltp_timestamp")
        if not ltp_timestamp:
            reasons.append("missing_ltp_timestamp")
        else:
            try:
                age = (now - self._ts(ltp_timestamp)).total_seconds()
                if age < -5:
                    reasons.append(f"future_ltp_timestamp_{age:.1f}s")
                elif age > 10:
                    reasons.append(f"stale_ltp_{age:.1f}s")
            except Exception:
                reasons.append("invalid_ltp_timestamp")

        expected = {
            datetime.combine(now.date(), dtime(9, 15 + i), IST)
            for i in range(15)
        }
        got = {
            c.ts.replace(second=0, microsecond=0)
            for c in candles
            if c.ts.date() == now.date() and dtime(9, 15) <= c.ts.time() < dtime(9, 30)
        }
        if len(got) != 15 or not expected.issubset(got):
            reasons.append("missing_09_15_to_09_29_grid")

        opening = tuple(
            c for c in candles
            if c.ts.date() == now.date() and dtime(9, 15) <= c.ts.time() < dtime(9, 30)
        )

        # Psygrid now exposes previous_close explicitly. Use it as the primary
        # source because it is a session-level value, not an inferred 15m close.
        previous_close = payload.get("previous_close")
        try:
            previous_close = float(previous_close) if previous_close is not None else None
        except (TypeError, ValueError):
            previous_close = None

        # Compatibility fallback for an older Psygrid build.
        if previous_close is None:
            for candle in self.candles(payload.get("15m", [])):
                if candle.ts.date() < now.date() and candle.ts.time() <= dtime(15, 30):
                    previous_close = candle.close
        if previous_close is None or previous_close <= 0:
            reasons.append("missing_previous_close")

        return StockData(
            symbol=symbol,
            candles=opening,
            ltp=ltp,
            previous_close=previous_close,
            health=Health(symbol, not reasons, ";".join(reasons)),
        )
