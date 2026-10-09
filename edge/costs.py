"""Indian intraday (MIS) equity transaction costs, per round trip, as a % of the position.

Statutory and broker charges (rates as known for 2025-2026; verify against the broker's
current schedule before trading -- they change by circular):

    brokerage   Dhan: Rs 20 or 0.03% of the order value per executed order, whichever is lower
    STT         0.025% on the SELL side only (intraday equity)
    exchange    NSE transaction charge 0.00297% of turnover, both sides
    SEBI fee    Rs 10 per crore = 0.0001%, both sides
    GST         18% on (brokerage + exchange charge + SEBI fee)
    stamp duty  0.003% on the BUY side only (intraday)

Slippage is NOT a fee: it is the adverse difference between the modelled price (bar open,
stop level) and the real fill. OHLC data has no bid/ask, so it is an assumption, applied per
side in basis points. The short side of a trade sells first and buys back; STT and stamp
fall on the sell and buy legs regardless of order, so the total is the same.

The previous research used a flat 0.142% round trip (fees + taxes + slippage on a
Rs 100,000 order). It is kept as the "old" scenario for comparison.
"""
from __future__ import annotations

from dataclasses import dataclass

OLD_FLAT_PCT = 0.142


@dataclass(frozen=True)
class CostModel:
    order_value: float = 5000.0          # rupees per position (the user's sizing)
    brokerage_pct: float = 0.03
    brokerage_cap: float = 20.0
    stt_sell_pct: float = 0.025
    exchange_pct: float = 0.00297
    sebi_pct: float = 0.0001
    gst_rate: float = 0.18
    stamp_buy_pct: float = 0.003
    slippage_bps_per_side: float = 2.0   # 0.02% per side

    def fees_pct(self) -> float:
        """Statutory + broker charges for one buy and one sell, % of order value."""
        v = self.order_value
        brokerage_side = min(self.brokerage_cap, v * self.brokerage_pct / 100)
        brokerage = 2 * brokerage_side / v * 100
        exchange = 2 * self.exchange_pct
        sebi = 2 * self.sebi_pct
        gst = self.gst_rate * (brokerage + exchange + sebi)
        return brokerage + self.stt_sell_pct + exchange + sebi + gst + self.stamp_buy_pct

    def slippage_pct(self) -> float:
        return 2 * self.slippage_bps_per_side / 100

    def round_trip_pct(self) -> float:
        return self.fees_pct() + self.slippage_pct()


SCENARIOS = {
    "OLD_FLAT": None,                                        # 0.142% flat (previous research)
    "BASE": CostModel(slippage_bps_per_side=2.0),
    "ADVERSE": CostModel(slippage_bps_per_side=5.0),
    "SEVERE": CostModel(slippage_bps_per_side=10.0),
}


def scenario_cost(name: str) -> float:
    model = SCENARIOS[name]
    return OLD_FLAT_PCT if model is None else model.round_trip_pct()


def describe() -> list[str]:
    lines = []
    for name, model in SCENARIOS.items():
        if model is None:
            lines.append(f"  {name:<9} {OLD_FLAT_PCT:.4f}%  (previous research.py flat assumption)")
        else:
            lines.append(f"  {name:<9} {model.round_trip_pct():.4f}%  = fees {model.fees_pct():.4f}% + slippage "
                         f"{model.slippage_pct():.4f}% ({model.slippage_bps_per_side:g} bps/side), "
                         f"Rs {model.order_value:,.0f} position")
    return lines
