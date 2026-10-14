"""Cross-sectional market context built from the valid stocks of one scan.

The "market" is the equal-weighted MEDIAN of the valid universe, not the cap-weighted NIFTY:
it is computed from exactly the same timestamped bars the engines see, so it never uses
information the stocks themselves do not have. Engines that need it (NEEDS_MARKET) report
INSUFFICIENT_DATA when it is invalid instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median

from .features import Features, StockView


@dataclass(frozen=True)
class ContextConfig:
    min_valid_stocks: int = 100            # eligible (liquid, valid) stocks needed for a market
    min_valid_fraction: float = 0.30       # stocks with usable DATA (eligible or not) / received
    min_stocks_per_minute: int = 50        # for a market 1-minute return at that minute


@dataclass(frozen=True)
class MarketContext:
    valid: bool
    reason: str
    n_valid: int
    n_received: int
    ret_open: float | None = None          # median of (C_t / O_0915 - 1)
    ret_prev: float | None = None          # median of (C_t / previous_close - 1)
    breadth_above_vwap: float | None = None  # share of valid stocks with C_t > VWAP_t
    # market 1-minute log-free simple returns keyed by bar start time: median over stocks of
    # C_m / C_{m-1} - 1 for stocks that have both minutes
    minute_returns: dict = field(default_factory=dict)
    stock_ret_open: dict = field(default_factory=dict)

    def market_return_between(self, start: datetime, end: datetime) -> float | None:
        """Compounded market return over bars with start < ts <= end (i.e. from the close of
        the bar at ``start`` to the close of the bar at ``end``). None if any minute missing."""
        total = 1.0
        keys = [k for k in self.minute_returns if start < k <= end]
        if not keys:
            return None
        expected = int((end - start).total_seconds() // 60)
        if len(keys) != expected:
            return None
        for k in keys:
            total *= 1.0 + self.minute_returns[k]
        return total - 1.0


def build_context(views: dict[str, StockView], feats: dict[str, Features], n_received: int,
                  cfg: ContextConfig = ContextConfig(), n_data_ok: int | None = None) -> MarketContext:
    """``views`` are the eligible stocks the market is computed from; ``n_data_ok`` counts every
    stock whose DATA was usable (eligible + merely illiquid), so a universe with many illiquid
    names is not mistaken for a broken feed."""
    n = len(views)
    data_ok = n if n_data_ok is None else n_data_ok
    if data_ok < cfg.min_valid_fraction * n_received:
        return MarketContext(False, f"only_{data_ok}_of_{n_received}_with_usable_data", n, n_received)
    if n < cfg.min_valid_stocks:
        return MarketContext(False, f"only_{n}_eligible_need_{cfg.min_valid_stocks}", n, n_received)
    ret_open, ret_prev, above = [], [], 0
    by_min: dict = {}
    stock_open: dict = {}
    for sym in sorted(views):
        v, f = views[sym], feats[sym]
        ro = f.c[-1] / f.o[0] - 1.0
        ret_open.append(ro)
        stock_open[sym] = ro
        if v.previous_close:
            ret_prev.append(f.c[-1] / v.previous_close - 1.0)
        if f.vwap[-1] is not None and f.c[-1] > f.vwap[-1]:
            above += 1
        for i in range(1, f.n):
            # consecutive minutes only: a gap would mix a multi-minute return into one minute
            if (f.ts[i] - f.ts[i - 1]).total_seconds() == 60:
                by_min.setdefault(f.ts[i], []).append(f.c[i] / f.c[i - 1] - 1.0)
    minute_returns = {k: median(vals) for k, vals in sorted(by_min.items())
                      if len(vals) >= cfg.min_stocks_per_minute}
    return MarketContext(True, "", n, n_received, median(ret_open),
                         median(ret_prev) if ret_prev else None, above / n, minute_returns, stock_open)
