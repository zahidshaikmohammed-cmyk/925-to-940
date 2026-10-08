"""Broker-agnostic execution layer (spec sections 24, 28).

The strategy never sees a broker. It produces a Decision; `PreOrderGuard` runs the
mandatory chain below and only then hands an `OrderRequest` to whichever
`BrokerExecutionProvider` is plugged in (paper or Dhan).

    SIGNAL -> RISK CHECK -> POSITION CHECK -> MARGIN CHECK -> DUPLICATE CHECK
           -> MARKET STATUS -> DATA FRESHNESS -> SL VALIDATION -> ORDER VALIDATION -> BROKER

Order style (spec 24): entries are MARKETABLE LIMIT orders (ask + buffer, IOC), never
plain market orders: on expiry afternoons the book can be thin for a second, and under
SEBI's retail-algo framework brokers may convert market orders to market-price-
protection orders anyway. Protective stops are exchange-resident STOP_LOSS (limit)
orders with a wide limit buffer, watched by the engine: if the trigger trades and the
SL is still open after `sl_chase_seconds`, it is cancelled and replaced by an
aggressive limit at the bid minus buffer.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from .config import EngineConfig
from .models import Reason


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OrderRequest:
    correlation_id: str
    security_id: str
    side: str                 # BUY / SELL
    qty: int
    order_type: str           # LIMIT / STOP_LOSS
    price: float
    trigger_price: float = 0.0
    validity: str = "DAY"     # DAY / IOC
    product: str = "INTRADAY"
    exchange_segment: str = "BSE_FNO"


@dataclass
class OrderState:
    order_id: str
    request: OrderRequest
    status: OrderStatus
    filled_qty: int = 0
    avg_price: float | None = None
    updated: datetime | None = None
    message: str = ""


class BrokerDataProvider(Protocol):
    def underlying_ticks(self): ...
    def option_quote(self, strike: int, right: str): ...
    def expiries(self) -> list: ...
    def lot_size(self, security_id: str) -> int: ...


class BrokerExecutionProvider(Protocol):
    def place(self, req: OrderRequest) -> OrderState: ...
    def modify(self, order_id: str, price: float, trigger_price: float, qty: int) -> OrderState: ...
    def cancel(self, order_id: str) -> OrderState: ...
    def status(self, order_id: str) -> OrderState: ...
    def find_by_correlation(self, correlation_id: str) -> OrderState | None: ...
    def positions(self) -> list[dict]: ...
    def open_orders(self) -> list[OrderState]: ...
    def available_margin(self) -> float: ...
    def day_pnl(self) -> float: ...
    def square_off_all(self) -> list[OrderState]: ...
    def account_kill_switch(self) -> bool: ...


def correlation_id(key: str, attempt: int = 0) -> str:
    """Deterministic id from the setup key: a restarted engine re-derives the same id and
    finds its own earlier order instead of sending a duplicate. Dhan allows <= 25 chars."""
    return "SX" + hashlib.sha1(f"{key}#{attempt}".encode()).hexdigest()[:18]


@dataclass
class PreOrderGuard:
    cfg: EngineConfig
    max_data_age_ms: int = 3000
    sent: set[str] = field(default_factory=set)

    def check(self, *, now: datetime, decision_payload: dict, broker: BrokerExecutionProvider,
              data_age_ms: int | None, market_open: bool, live_gate_ok: bool, kill_switch_tripped: bool,
              engine_flat: bool) -> tuple[list[Reason], OrderRequest | None]:
        p = decision_payload
        out: list[Reason] = []
        if kill_switch_tripped:
            return [Reason.KILL_SWITCH], None
        if not live_gate_ok:
            out.append(Reason.LIVE_NOT_VALIDATED)
        # risk + position
        qty = int(p.get("qty") or 0)
        if qty <= 0 or qty > self.cfg.risk.max_lots * 100:
            out.append(Reason.RISK_TOO_HIGH)
        if not engine_flat or any(int(x.get("net_qty", 0)) != 0 for x in broker.positions()):
            out.append(Reason.POSITION_OPEN)
        if broker.open_orders():
            out.append(Reason.POSITION_OPEN)
        # margin: long options need the full premium plus costs up front
        need = float(p.get("outlay") or 0) + float(p.get("est_costs") or 0)
        if broker.available_margin() < need * 1.05:
            out.append(Reason.RISK_TOO_HIGH)
        # duplicates
        cid = correlation_id(p.get("setup", "") + "|" + str(p.get("bar_timestamp")) + "|" + str(p.get("direction")))
        if cid in self.sent or broker.find_by_correlation(cid) is not None:
            out.append(Reason.DUPLICATE_SIGNAL)
        # market status + freshness
        t = now.time()
        if not market_open or not (self.cfg.session.market_open <= t < self.cfg.session.last_entry.replace(second=59)):
            out.append(Reason.OUTSIDE_WINDOW)
        if data_age_ms is None or data_age_ms > self.max_data_age_ms:
            out.append(Reason.DATA_STALE)
        # SL validation
        entry, stop = p.get("entry"), p.get("stop_loss")
        if not (isinstance(entry, (int, float)) and isinstance(stop, (int, float)) and entry > stop > 0):
            out.append(Reason.STOP_TOO_WIDE)
        # order validation
        sec = p.get("security_id")
        if not sec:
            out.append(Reason.DATA_BAD)
        if out:
            return list(dict.fromkeys(out)), None
        tick = self.cfg.risk.tick_size
        limit = round(round((entry + 2 * tick) / tick) * tick, 2)
        req = OrderRequest(cid, str(sec), "BUY", qty, "LIMIT", limit, validity="IOC")
        self.sent.add(cid)
        return [], req


def protective_stop(entry_req: OrderRequest, stop: float, qty: int, tick: float, buffer_frac: float = 0.10) -> OrderRequest:
    """Exchange-resident SL-limit sell. Limit sits buffer_frac below the trigger so a fast
    expiry candle still fills; the engine's chase logic covers a jump through the limit."""
    limit = max(tick, round(round(stop * (1 - buffer_frac) / tick) * tick, 2))
    return OrderRequest(entry_req.correlation_id[:-1] + "S", entry_req.security_id, "SELL", qty, "STOP_LOSS",
                        limit, trigger_price=round(round(stop / tick) * tick, 2))


class PaperBroker:
    """In-memory broker for live-data paper trading. Conservative: buys fill at the ask
    plus slippage, sells at the bid minus slippage, an IOC limit that cannot fill at its
    price is cancelled, stops fill at the bid once the bid trades through the trigger."""

    def __init__(self, cfg: EngineConfig, capital: float, quote_fn):
        self.cfg = cfg
        self.cash = capital
        self.quote_fn = quote_fn          # security_id -> (bid, ask) or None
        self.orders: dict[str, OrderState] = {}
        self.pos: dict[str, int] = {}
        self.realized = 0.0
        self.cost_basis: dict[str, float] = {}
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"P{self._n:06d}"

    def place(self, req: OrderRequest) -> OrderState:
        oid = self._id()
        st = OrderState(oid, req, OrderStatus.OPEN)
        self.orders[oid] = st
        if req.order_type == "LIMIT":
            self._try_fill(st)
            if st.status is OrderStatus.OPEN and req.validity == "IOC":
                st.status = OrderStatus.CANCELLED
                st.message = "IOC not marketable"
        return st

    def _try_fill(self, st: OrderState) -> None:
        q = self.quote_fn(st.request.security_id)
        if not q:
            return
        bid, ask = q
        slip = self.cfg.risk.slippage_ticks * self.cfg.risk.tick_size
        r = st.request
        if r.side == "BUY" and ask and ask <= r.price:
            self._fill(st, ask + slip)
        elif r.side == "SELL" and bid:
            if r.order_type == "STOP_LOSS" and bid <= r.trigger_price and bid - slip >= r.price:
                self._fill(st, bid - slip)      # below the limit it stays open: the chase logic takes over
            elif r.order_type == "LIMIT" and bid >= r.price:
                self._fill(st, bid - slip)

    def _fill(self, st: OrderState, px: float) -> None:
        r = st.request
        st.status, st.filled_qty, st.avg_price = OrderStatus.FILLED, r.qty, round(px, 2)
        sgn = 1 if r.side == "BUY" else -1
        self.pos[r.security_id] = self.pos.get(r.security_id, 0) + sgn * r.qty
        self.cash -= sgn * px * r.qty
        if r.side == "BUY":
            self.cost_basis[r.security_id] = px
        else:
            self.realized += (px - self.cost_basis.get(r.security_id, px)) * r.qty

    def poll(self) -> None:
        for st in self.orders.values():
            if st.status is OrderStatus.OPEN:
                self._try_fill(st)

    def modify(self, order_id, price, trigger_price, qty):
        st = self.orders[order_id]
        st.request = OrderRequest(st.request.correlation_id, st.request.security_id, st.request.side, qty,
                                  st.request.order_type, price, trigger_price, st.request.validity)
        return st

    def cancel(self, order_id):
        st = self.orders[order_id]
        if st.status is OrderStatus.OPEN:
            st.status = OrderStatus.CANCELLED
        return st

    def status(self, order_id):
        return self.orders[order_id]

    def find_by_correlation(self, cid):
        return next((o for o in self.orders.values() if o.request.correlation_id == cid), None)

    def positions(self):
        return [{"security_id": k, "net_qty": v} for k, v in self.pos.items() if v]

    def open_orders(self):
        return [o for o in self.orders.values() if o.status is OrderStatus.OPEN]

    def available_margin(self):
        return self.cash

    def day_pnl(self):
        return self.realized

    def square_off_all(self):
        out = []
        for o in self.open_orders():
            self.cancel(o.order_id)
        for sec, q in list(self.pos.items()):
            if q > 0:
                out.append(self.place(OrderRequest(f"SQ{sec}"[:20], sec, "SELL", q, "LIMIT", 0.05)))
        return out

    def account_kill_switch(self):
        return True


def is_market_open(now: datetime, cfg: EngineConfig, trading_day: bool) -> bool:
    return trading_day and cfg.session.market_open <= now.time() < cfg.session.market_close

