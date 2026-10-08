"""Live / paper runner (spec sections 25, 33, 36). Wires feed -> validator -> candles ->
engine -> guard -> broker -> position monitor, with the kill switch checked first on
every step. The data source and the broker are injected, so the same runner drives
Dhan live, Dhan market data with the paper broker, or a recorded replay in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .audit import AuditLog
from .candles import CandleBuilder
from .config import EngineConfig
from .engine import StrategyEngine
from .execution import OrderRequest, OrderStatus, PreOrderGuard, correlation_id, protective_stop
from .features import atr
from .models import Action, Candle, OptionQuote, PriorDay, Reason, Tick
from .position import Position, manage, open_position, realized
from .quality import TickValidator, assess
from .risk import KillSwitch
from .session_calendar import at
from .state_machine import State, TradeStateMachine


@dataclass
class LiveRunner:
    cfg: EngineConfig
    day: date
    prior: PriorDay | None
    is_expiry: bool
    expiry: date | None
    lot_size: int
    index_id: str
    option_ids: dict[tuple[int, str], str]          # (strike, right) -> security id
    broker: object                                  # BrokerExecutionProvider
    kill: KillSwitch
    audit: AuditLog
    live_gate_ok: bool = False
    capital: float | None = None
    sm: TradeStateMachine = field(default_factory=TradeStateMachine)
    quotes: dict[str, Tick] = field(default_factory=dict)
    pos: Position | None = None
    sl_order_id: str | None = None
    exit_order_id: str | None = None
    exit_reason: Reason | None = None
    exit_sent_at: datetime | None = None
    exit_attempts: int = 0
    exit_chase_seconds: float = 3.0
    max_exit_attempts: int = 5          # stays under the runaway-order trip (6/min) so the cause is named correctly
    _last_emergency: datetime | None = None
    feed_connected: bool = True
    last_index_tick: datetime | None = None

    def __post_init__(self):
        self.session_open = at(self.day, self.cfg.session.market_open)
        self.engine = StrategyEngine(self.cfg, self.day, self.session_open, self.prior, self.is_expiry,
                                     self.expiry, self.lot_size)
        self.validator = TickValidator(self.cfg.data)
        self.builder = CandleBuilder(self.cfg.session.candle_close_grace_s)
        self.opt_builders: dict[str, CandleBuilder] = {}
        self.guard = PreOrderGuard(self.cfg, self.cfg.data.max_underlying_age_ms)
        self.by_sid = {v: k for k, v in self.option_ids.items()}

    # ------------------------------------------------------------ inputs
    def on_tick(self, tick: Tick) -> None:
        a = atr(self.builder.closed, self.cfg.features.atr_period) if tick.security_id == self.index_id else None
        if not self.validator.accept(tick, a):
            return
        if tick.security_id == self.index_id:
            self.last_index_tick = tick.recv_ts
            for c in self.builder.on_tick(tick):
                self._on_bar(c, tick.recv_ts)
        else:
            self.quotes[tick.security_id] = tick
            self.opt_builders.setdefault(tick.security_id, CandleBuilder(self.cfg.session.candle_close_grace_s)).on_tick(tick)

    def on_clock(self, now: datetime) -> None:
        if self.kill.tripped:
            self._emergency(now, self.kill.reason() or "KILL_SWITCH")
            return
        self._poll_exit(now)
        for b in self.opt_builders.values():
            b.on_clock(now)
        for c in self.builder.on_clock(now):
            self._on_bar(c, now)
        if self.pos is not None and now.time() >= self.cfg.session.hard_flat:
            self._exit(now, Reason.EXIT_HARD_FLAT)
        if self.pos is None and now.time() >= self.cfg.session.hard_flat and self.sm.state is not State.DAY_DONE \
                and self.sm.can(State.DAY_DONE):
            self.sm.go(State.DAY_DONE, now, "hard flat time")

    def reconcile(self, now: datetime) -> None:
        """Compare broker truth with engine state; any mismatch trips the kill switch.
        A filled protective stop is booked first, so a legitimate stop-out is not a mismatch."""
        if self.pos is not None and self.sl_order_id:
            st = self.broker.status(self.sl_order_id)
            if st.status is OrderStatus.FILLED:
                if self.sm.state is not State.EXIT_PENDING:
                    self.sm.go(State.EXIT_PENDING, now, "protective stop filled")
                self._closed(now, float(st.avg_price), Reason.EXIT_STOP_PREMIUM)
        expected_qty = self.pos.qty if self.pos is not None else 0
        expected_pending = (1 if self.sl_order_id else 0) + (1 if self.exit_order_id else 0)
        self.kill.check_broker_state(now, self.broker.day_pnl(), self.broker.positions(), expected_qty,
                                     len(self.broker.open_orders()), expected_pending)
        if self.kill.tripped:
            self._emergency(now, self.kill.reason() or "RECONCILE")

    # ------------------------------------------------------------ core
    def _quote(self, strike: int, right: str) -> OptionQuote | None:
        sid = self.option_ids.get((strike, right))
        t = self.quotes.get(sid) if sid else None
        if t is None:
            return None
        return OptionQuote(strike, right, t.ltp, t.bid, t.ask, t.ts, oi=t.oi, volume=t.volume,
                           top5_bid_qty=t.bid_qty, top5_ask_qty=t.ask_qty, security_id=sid)

    def _on_bar(self, bar: Candle, now: datetime) -> None:
        if self.kill.tripped:
            return
        hist = self.builder.closed
        if self.pos is not None and self.sm.state is State.EXIT_PENDING:
            self._poll_exit(now)
            return
        if self.pos is not None:
            sid = self.option_ids[(self.pos.strike, self.pos.right)]
            ob = next((c for c in reversed(self.opt_builders.get(sid, CandleBuilder()).closed) if c.start == bar.start), None)
            prev_stop = self.pos.stop
            manage(self.cfg, self.pos, bar, ob, atr(hist, self.cfg.features.atr_period))
            if not self.pos.open:
                # the model saw the premium stop trade; the broker decides the actual exit
                reason = self.pos.exit_reason or Reason.EXIT_STOP_PREMIUM
                self.pos.exit_ts = self.pos.exit_price = self.pos.exit_reason = None
                self._exit(now, reason)
                return
            if self.pos.stop > prev_stop and self.sl_order_id:
                tick = self.cfg.risk.tick_size
                self.broker.modify(self.sl_order_id, round(self.pos.stop * 0.9 / tick) * tick, self.pos.stop, self.pos.qty)
                self.audit.write("STOP_MODIFIED", now, {"from": prev_stop, "to": self.pos.stop})
            if self.pos.pending_exit is not None:
                self._exit(now, self.pos.pending_exit)
            return
        if not self.sm.accepts_entries:
            return
        opt_ts = max((t.ts for t in self.quotes.values()), default=None)
        q = assess(self.cfg.data, now, hist, self.last_index_tick, self.session_open,
                   option_quote_ts=opt_ts, feed_connected=self.feed_connected)
        dec = self.engine.evaluate_entry(hist, now, q, self._quote, self.capital)
        self.audit.write("DECISION", now, dec.payload)
        if dec.action is not Action.BUY:
            return
        reasons, req = self.guard.check(now=now, decision_payload=dec.payload, broker=self.broker,
                                        data_age_ms=q.data_age_ms, market_open=True, live_gate_ok=self.live_gate_ok,
                                        kill_switch_tripped=self.kill.tripped, engine_flat=self.sm.flat)
        if req is None:
            self.audit.write("GUARD_BLOCK", now, {"reasons": [r.value for r in reasons]})
            return
        for s in (State.DATA_VALID, State.REGIME_IDENTIFIED, State.SETUP_CONFIRMED, State.RISK_APPROVED, State.ORDER_PENDING):
            if self.sm.state is not s:
                self.sm.go(s, now, dec.candidate.setup if dec.candidate else "")
        self.kill.note_order(now)
        st = self.broker.place(req)
        self.audit.write("ORDER", now, {"request": req.__dict__, "status": st.status.value, "order_id": st.order_id})
        if st.status is not OrderStatus.FILLED:
            self.sm.go(State.WAITING, now, f"entry {st.status.value}")
            return
        cand = dec.candidate
        p = dec.payload
        self.pos = open_position(self.cfg, cand.key, cand.setup, cand.direction, p["strike"], p["right"], st.filled_qty,
                                 now, float(st.avg_price), p["stop_frac"], cand.invalidation, p.get("atr") or 1.0,
                                 cand.trigger_price)
        self.engine.on_entry(cand.key)
        self.sm.go(State.POSITION_OPEN, now, "filled")
        sl = protective_stop(req, self.pos.stop, self.pos.qty, self.cfg.risk.tick_size)
        self.kill.note_order(now)
        sl_st = self.broker.place(sl)
        self.sl_order_id = sl_st.order_id
        self.audit.write("FILL", now, {"price": st.avg_price, "qty": st.filled_qty, "stop": self.pos.stop,
                                       "sl_order": sl_st.order_id, "one_r": self.pos.one_r})
        if sl_st.status in (OrderStatus.REJECTED, OrderStatus.UNKNOWN):
            # a long option without a resting stop is still bounded by its premium, but this
            # is an execution failure: exit now rather than run unprotected
            self._exit(now, Reason.EXIT_DATA_FAILURE)
            return
        self.sm.go(State.POSITION_MANAGEMENT, now, "stop placed")

    def _exit(self, now: datetime, reason: Reason) -> None:
        """Start an exit. Exactly one exit order is live at any time; `_poll_exit` chases it."""
        if self.pos is None or self.exit_order_id is not None:
            return
        if self.sm.state is not State.EXIT_PENDING:
            self.sm.go(State.EXIT_PENDING, now, reason.value)
        self.exit_reason = reason
        if self.sl_order_id:
            st = self.broker.status(self.sl_order_id)
            if st.status is OrderStatus.FILLED:
                self._closed(now, float(st.avg_price), Reason.EXIT_STOP_PREMIUM)
                return
            self.broker.cancel(self.sl_order_id)
            st = self.broker.status(self.sl_order_id)
            if st.status is OrderStatus.FILLED:          # filled while we were cancelling
                self._closed(now, float(st.avg_price), Reason.EXIT_STOP_PREMIUM)
                return
            self.sl_order_id = None
        self._send_exit(now)

    def _send_exit(self, now: datetime) -> None:
        sid = self.option_ids[(self.pos.strike, self.pos.right)]
        t = self.quotes.get(sid)
        tick = self.cfg.risk.tick_size
        ref = t.bid if t and t.bid else (t.ltp if t else tick)
        # each retry gives up 3% more price: being flat matters more than the last rupee
        px = max(tick, round(ref * (0.97 - 0.03 * self.exit_attempts) / tick) * tick)
        self.kill.note_order(now)
        req = OrderRequest(correlation_id(self.pos.key, 10 + self.exit_attempts), sid, "SELL", self.pos.qty, "LIMIT",
                           round(px, 2))
        st = self.broker.place(req)
        self.exit_attempts += 1
        self.exit_order_id, self.exit_sent_at = st.order_id, now
        self.audit.write("EXIT_ORDER", now, {"reason": self.exit_reason.value if self.exit_reason else None,
                                             "price": req.price, "attempt": self.exit_attempts,
                                             "status": st.status.value})
        if st.status is OrderStatus.FILLED:
            self._closed(now, float(st.avg_price), self.exit_reason or Reason.EXIT_DATA_FAILURE)

    def _poll_exit(self, now: datetime) -> None:
        if self.pos is None or self.exit_order_id is None:
            return
        st = self.broker.status(self.exit_order_id)
        if st.status is OrderStatus.FILLED:
            self._closed(now, float(st.avg_price), self.exit_reason or Reason.EXIT_DATA_FAILURE)
            return
        if st.status is OrderStatus.PARTIAL:
            return      # let it work; remaining qty is chased after the timeout below
        if st.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED) or \
                (now - self.exit_sent_at).total_seconds() >= self.exit_chase_seconds:
            if st.status not in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                self.broker.cancel(self.exit_order_id)
                st = self.broker.status(self.exit_order_id)
                if st.status is OrderStatus.FILLED:
                    self._closed(now, float(st.avg_price), self.exit_reason or Reason.EXIT_DATA_FAILURE)
                    return
            self.exit_order_id = None
            if self.exit_attempts >= self.max_exit_attempts:
                self.kill.trip("EXIT_NOT_FILLING", now, {"attempts": self.exit_attempts})
                self._emergency(now, "EXIT_NOT_FILLING")
                return
            self._send_exit(now)

    def _closed(self, now: datetime, price: float, reason: Reason) -> None:
        pos = self.pos
        pos.exit_ts, pos.exit_price, pos.exit_reason = now, price, reason
        res = realized(self.cfg, pos)
        self.engine.risk_state.record_exit(res["r_net"], now)
        self.audit.write("TRADE_CLOSED", now, {"key": pos.key, "setup": pos.setup, "entry": pos.entry_price,
                                               "exit": price, "qty": pos.qty, "reason": reason.value, **res})
        self.pos, self.sl_order_id = None, None
        self.exit_order_id, self.exit_reason, self.exit_sent_at, self.exit_attempts = None, None, None, 0
        self.sm.go(State.COOLDOWN, now, reason.value)
        self.sm.go(State.WAITING, now, "cooldown handled by risk state")

    def _emergency(self, now: datetime, why: str) -> None:
        """STOP NEW ENTRIES -> CANCEL PENDING -> ASSESS -> EXIT -> LOCK (spec 33)."""
        if self.sm.state is State.KILLED:
            if not self.broker.positions() and not self.broker.open_orders():
                return
            if self._last_emergency and (now - self._last_emergency).total_seconds() < 5:
                return          # give the previous square-off time to land before repeating it
        self.sm.go(State.KILLED, now, why)
        self._last_emergency = now
        res = self.broker.square_off_all()
        self.audit.write("KILL_SWITCH", now, {"why": why, "square_off": [r.status.value for r in res]})
        self.pos, self.sl_order_id = None, None
        self.exit_order_id, self.exit_reason, self.exit_sent_at = None, None, None
