from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.request import Request, urlopen

from strategy import BenchmarkSnapshot, CandidateSnapshot, Candle, HTFContext, parse_ohlcv

SHARDS = tuple(f"live-{x}.json" for x in "abcdefghij")


@dataclass(frozen=True)
class PsygridConfig:
    base_url: str
    timeout_seconds: float = 5.0

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")


class PsygridClient:
    """Read-only adapter for the current PSYGRID public JSON contract."""

    def __init__(self, config: PsygridConfig):
        self.config = config

    def get(self, path: str) -> dict[str, Any]:
        req = Request(self.config.url(path), headers={"Accept": "application/json", "Accept-Encoding": "gzip"})
        with urlopen(req, timeout=self.config.timeout_seconds) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))

    def market(self) -> dict[str, Any]:
        stocks: dict[str, Any] = {}
        for shard in SHARDS:
            payload = self.get(f"/public/{shard}")
            if payload.get("stock_count") != 45:
                raise RuntimeError(f"PSYGRID shard {shard} expected 45 stocks, got {payload.get('stock_count')}")
            shard_stocks = payload.get("stocks")
            if not isinstance(shard_stocks, dict):
                raise RuntimeError(f"PSYGRID shard {shard} has invalid stocks object")
            overlap = set(stocks).intersection(shard_stocks)
            if overlap:
                raise RuntimeError(f"duplicate symbols across PSYGRID shards: {sorted(overlap)[:5]}")
            stocks.update(shard_stocks)
        if len(stocks) != 450:
            raise RuntimeError(f"PSYGRID universe expected 450 unique stocks, got {len(stocks)}")
        return {"stocks": stocks}

    @staticmethod
    def candles(payload: dict[str, Any], timeframe: str) -> tuple[Candle, ...]:
        rows = payload.get(timeframe, [])
        if not isinstance(rows, list):
            raise RuntimeError(f"invalid {timeframe} candle array")
        return tuple(parse_ohlcv(row) for row in rows if isinstance(row, dict) and row.get("complete", True))

    def benchmark(self) -> BenchmarkSnapshot:
        payload = self.get("/public/nifty.json")
        rows = self.candles(payload, "1m")
        if not rows or payload.get("ltp") is None:
            raise RuntimeError("NIFTY live 1m data unavailable")
        return BenchmarkSnapshot(rows[0].open, float(payload["ltp"]))

    def sector_return(self, endpoint: str) -> float:
        payload = self.get(f"/public/{endpoint}.json")
        rows = self.candles(payload, "1m")
        if not rows or payload.get("ltp") is None:
            raise RuntimeError(f"sector index {endpoint} live data unavailable")
        return 100.0 * (float(payload["ltp"]) / rows[0].open - 1.0)

    def snapshot(
        self,
        symbol: str,
        stock_payload: dict[str, Any],
        benchmark: BenchmarkSnapshot,
        sector_returns_pct: tuple[float, ...],
        htf: HTFContext,
    ) -> CandidateSnapshot:
        candles = self.candles(stock_payload, "1m")
        if len(candles) < 10:
            raise RuntimeError(f"{symbol}: fewer than 10 completed 1m candles")
        current_price = float(stock_payload["ltp"])
        current_vwap = session_vwap(candles)
        return CandidateSnapshot(
            symbol=symbol,
            candles=candles,
            benchmark=benchmark,
            sector_returns_pct=sector_returns_pct,
            htf=htf,
            current_price=current_price,
            current_vwap=current_vwap,
            relative_volume=None,
        )


def session_vwap(candles: tuple[Candle, ...]) -> float:
    """Exact OHLCV VWAP from genuine candles; no interpolation or fill."""
    numerator = 0.0
    denominator = 0.0
    for c in candles:
        if c.volume < 0:
            raise ValueError("negative volume")
        typical = (c.high + c.low + c.close) / 3.0
        numerator += typical * c.volume
        denominator += c.volume
    if denominator <= 0:
        raise RuntimeError("cannot calculate VWAP from zero volume")
    return numerator / denominator
