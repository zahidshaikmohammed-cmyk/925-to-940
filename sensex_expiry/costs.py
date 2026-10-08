"""Transaction costs for a long option round trip (spec section 31).

Rates come from CostConfig; they were researched on 2026-10-08 and MUST be checked
against a real Dhan contract note before the paper stage ends.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import CostConfig


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst

    def as_dict(self) -> dict:
        return {k: round(v, 2) for k, v in self.__dict__.items()} | {"total": round(self.total, 2)}


def round_trip(buy_premium: float, sell_premium: float, qty: int, cfg: CostConfig) -> CostBreakdown:
    buy_val, sell_val = buy_premium * qty, sell_premium * qty
    turnover = buy_val + sell_val
    brokerage = 2 * cfg.brokerage_per_order if qty > 0 else 0.0
    stt = sell_val * cfg.stt_sell_premium
    exchange = turnover * cfg.exchange_txn_premium
    sebi = turnover * cfg.sebi_fee
    stamp = buy_val * cfg.stamp_buy
    gst = (brokerage + exchange + sebi) * cfg.gst
    return CostBreakdown(brokerage, stt, exchange, sebi, stamp, gst)


def expiry_exercise_stt_warning(settlement_value: float, qty: int, rate: float = 0.0015) -> float:
    """STT charged if an ITM long option is left to exercise: on settlement VALUE, not premium.
    This is why every position is flat by the hard cutoff (spec 47)."""
    return settlement_value * qty * rate
