"""Risk engine (spec sections 22-23) and the independent kill switch (spec 33).

1R is the rupee loss if the premium stop fills at its price, plus modelled
slippage and round-trip costs. Quantity is the largest whole number of lots whose
1R fits inside capital x risk_per_trade_pct, subject to every hard cap. If not even
one lot fits, the answer is NO_TRADE, never "round up".
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import EngineConfig
from .costs import round_trip
from .models import Reason


@dataclass(frozen=True)
class SizeResult:
    lots: int
    qty: int
    risk_per_lot: float
    one_r: float
    outlay: float
    est_costs: float
    reasons: tuple[Reason, ...]

    @property
    def approved(self) -> bool:
        return self.lots > 0 and not self.reasons


def size_position(cfg: EngineConfig, entry: float, stop: float, lot_size: int, capital: float | None = None) -> SizeResult:
    r = cfg.risk
    cap = capital if capital is not None else r.capital
    budget = cap * r.risk_per_trade_pct
    slip = r.slippage_ticks * r.tick_size
    if lot_size <= 0 or not entry > stop > 0:
        return SizeResult(0, 0, 0, 0, 0, 0, (Reason.RISK_TOO_HIGH,))
    per_unit = (entry + slip) - (stop - slip)
    costs_lot = round_trip(entry + slip, stop - slip, lot_size, cfg.costs).total
    risk_lot = per_unit * lot_size + costs_lot
    lots = int(budget // risk_lot)
    lots = min(lots, r.max_lots)
    outlay_lot = (entry + slip) * lot_size
    lots = min(lots, int((cap * r.max_outlay_pct) // outlay_lot) if outlay_lot > 0 else 0)
    # catastrophic bound: if the stop never fills (feed, API or engine failure) the whole
    # premium can go to zero, so the premium outlay is capped at max_outlay_r x the risk budget
    lots = min(lots, int((cfg.options.max_outlay_r * budget) // outlay_lot) if outlay_lot > 0 else 0)
    reasons = () if lots > 0 else (Reason.RISK_TOO_HIGH,)
    return SizeResult(lots, lots * lot_size, risk_lot, risk_lot * lots, outlay_lot * lots,
                      round_trip(entry + slip, stop - slip, lots * lot_size, cfg.costs).total if lots else 0.0, reasons)


def kelly_fraction(win_rate: float, avg_win_r: float, avg_loss_r: float = 1.0) -> float:
    """Full Kelly for a two-outcome approximation. Reported only (spec 23): the engine
    never sizes from it; it is a ceiling check that fixed-fractional risk stays far below."""
    if avg_win_r <= 0 or avg_loss_r <= 0:
        return 0.0
    b = avg_win_r / avg_loss_r
    return max(0.0, win_rate - (1 - win_rate) / b)


@dataclass
class DailyRiskState:
    day: str
    realized_r: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    open_positions: int = 0
    last_exit: datetime | None = None
    traded_keys: set[str] = field(default_factory=set)

    def record_exit(self, r_multiple: float, when: datetime) -> None:
        self.realized_r += r_multiple
        self.open_positions = max(0, self.open_positions - 1)
        self.consecutive_losses = self.consecutive_losses + 1 if r_multiple < 0 else 0
        self.last_exit = when


def pre_trade_checks(cfg: EngineConfig, st: DailyRiskState, now: datetime, key: str) -> list[Reason]:
    r = cfg.risk
    out = []
    if st.open_positions >= r.max_open_positions:
        out.append(Reason.POSITION_OPEN)
    if st.trades >= r.max_trades_per_day:
        out.append(Reason.MAX_TRADES)
    # open risk counts against the daily limit: a new trade must fit inside what is left
    if st.realized_r - 1.0 < -r.max_daily_loss_r - 1e-9:
        out.append(Reason.DAILY_LOSS_LIMIT)
    if st.consecutive_losses >= r.max_consecutive_losses:
        out.append(Reason.CONSECUTIVE_LOSSES)
    if st.last_exit is not None and now - st.last_exit < timedelta(minutes=r.cooldown_minutes):
        out.append(Reason.COOLDOWN)
    if key in st.traded_keys:
        out.append(Reason.DUPLICATE_SIGNAL)
    return out


class KillSwitch:
    """Logically independent of the strategy. It reads broker truth (positions, orders,
    P&L) and its own thresholds; the strategy cannot reset it. Once tripped it persists
    to disk and the engine refuses to start until a human deletes the lock file."""

    def __init__(self, lock_path: Path, max_daily_loss_rupees: float, max_qty: int, max_orders_per_minute: int = 6):
        self.lock_path = Path(lock_path)
        self.max_daily_loss = max_daily_loss_rupees
        self.max_qty = max_qty
        self.max_orders_per_minute = max_orders_per_minute
        self._order_times: list[datetime] = []

    @property
    def tripped(self) -> bool:
        return self.lock_path.exists()

    def reason(self) -> str | None:
        if not self.tripped:
            return None
        try:
            return json.loads(self.lock_path.read_text()).get("reason")
        except (OSError, ValueError):
            return "UNREADABLE_LOCK"

    def trip(self, reason: str, now: datetime, detail: dict | None = None) -> None:
        if self.tripped:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(json.dumps({"reason": reason, "at": now.isoformat(), "detail": detail or {}}))

    def note_order(self, now: datetime) -> None:
        self._order_times = [t for t in self._order_times if now - t < timedelta(minutes=1)] + [now]
        if len(self._order_times) > self.max_orders_per_minute:
            self.trip("RUNAWAY_ORDER_LOOP", now, {"orders_last_minute": len(self._order_times)})

    def check_broker_state(self, now: datetime, day_pnl: float, positions: list[dict], expected_qty: int,
                           pending_orders: int, expected_pending: int) -> None:
        """positions: [{"security_id", "net_qty"}] straight from the broker."""
        if day_pnl <= -abs(self.max_daily_loss):
            self.trip("DAILY_LOSS_LIMIT", now, {"day_pnl": day_pnl})
        gross = sum(abs(int(p.get("net_qty", 0))) for p in positions)
        if gross > self.max_qty:
            self.trip("MAX_QTY_EXCEEDED", now, {"gross_qty": gross})
        if gross != expected_qty:
            self.trip("UNEXPECTED_POSITION", now, {"broker_qty": gross, "engine_qty": expected_qty})
        if pending_orders > expected_pending:
            self.trip("UNEXPECTED_ORDERS", now, {"broker_pending": pending_orders, "engine_pending": expected_pending})


def r_multiple(pnl_rupees: float, one_r: float) -> float:
    return pnl_rupees / one_r if one_r > 0 else math.nan
