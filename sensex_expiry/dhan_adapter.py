"""Dhan implementation of the broker interfaces (spec sections 3, 25).

Built against the OFFICIAL `dhanhq` Python SDK v2.2.0 (PyPI), whose source was read
on 2026-10-08. Only methods that exist in that SDK are called:

  REST base https://api.dhan.co/v2
    option_chain(under_security_id, under_exchange_segment, expiry)   POST /optionchain
    expiry_list(under_security_id, under_exchange_segment)            POST /optionchain/expirylist
    intraday_minute_data(security_id, exchange_segment, instrument_type, from, to, interval, oi)
                                                                      POST /charts/intraday
    expired_options_data(security_id, exchange_segment, instrument_type, expiry_flag,
                         expiry_code, strike, drv_option_type, required_data, from, to, interval)
                                                                      POST /charts/rollingoption
    quote_data / ohlc_data / ticker_data                              POST /marketfeed/quote|ohlc|ltp
    place_order / modify_order / cancel_order / get_order_by_id / get_order_by_correlationID
    get_order_list / get_positions / get_trade_book / get_fund_limits / margin_calculator
    kill_switch('ACTIVATE'|'DEACTIVATE') / status_kill_switch          POST/GET /killswitch
  WebSocket wss://api-feed.dhan.co (v2): Ticker(15) / Quote(17) / Full(21) subscriptions,
    100 instruments per subscribe message; Full = LTP, LTQ, LTT, ATP, volume, total
    buy/sell qty, OI, OI day high/low, OHLC and 5-level depth. Exchange codes: IDX=0,
    BSE_FNO=8. Server disconnect codes 805-809.
  Order updates: wss://api-order-update.dhan.co.  20/200-level depth: separate sockets.

Every response FIELD NAME below that the SDK does not itself define is collected in
FIELDS and marked VERIFY: confirm each against one real response during the
live-data-simulation stage. A missing field makes the adapter return None, which
the engine treats as NO_TRADE; it never substitutes a default.
"""
from __future__ import annotations

import csv
import io
import time as _time
from datetime import date, datetime, timedelta
from typing import Any

from .execution import OrderRequest, OrderState, OrderStatus
from .models import IST, OptionQuote, Tick
from .options import parse_dhan_chain

SENSEX_UNDERLYING_SEG = "IDX_I"
SENSEX_SECURITY_ID_EXPECTED = "51"     # VERIFY via scrip master at startup; never trusted blindly
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"   # from the SDK (_security.py)

FIELDS = {  # VERIFY each against a real response
    "order_id": "orderId",
    "order_status": "orderStatus",
    "filled_qty": "filledQty",
    "avg_price": "averageTradedPrice",
    "correlation_id": "correlationId",
    "pos_security_id": "securityId",
    "pos_net_qty": "netQty",
    "pos_realized": "realizedProfit",
    "pos_unrealized": "unrealizedProfit",
    "funds_available": ("availabelBalance", "availableBalance"),
}

STATUS_MAP = {
    "TRANSIT": OrderStatus.PENDING, "PENDING": OrderStatus.OPEN, "PART_TRADED": OrderStatus.PARTIAL,
    "TRADED": OrderStatus.FILLED, "CANCELLED": OrderStatus.CANCELLED, "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.CANCELLED,
}


def _client(client_id: str, access_token: str):
    try:
        from dhanhq import DhanContext, dhanhq   # optional dependency, imported only for live use
    except ImportError as e:   # pragma: no cover - depends on the environment
        raise RuntimeError("pip install dhanhq==2.2.0 to use the Dhan adapter") from e
    return dhanhq(DhanContext(client_id, access_token))


def _ok(resp: Any) -> dict | list | None:
    if isinstance(resp, dict) and str(resp.get("status", "")).lower() == "success":
        return resp.get("data")
    return None


def normalize_ltt(raw_epoch: int, recv: datetime) -> tuple[datetime, str]:
    """Dhan feed times are epoch seconds. Some brokers encode IST wall-clock as if it were
    UTC; detect a ~5h30m offset against local receipt time instead of assuming either."""
    utc_based = datetime.fromtimestamp(raw_epoch, IST)
    if abs((recv - utc_based).total_seconds()) < 3600:
        return utc_based, "UTC_EPOCH"
    ist_based = utc_based - timedelta(hours=5, minutes=30)
    if abs((recv - ist_based).total_seconds()) < 3600:
        return ist_based, "IST_EPOCH"
    return utc_based, "UNRESOLVED"     # quality layer will flag the age


def parse_scrip_master(text: str, expiry: date) -> dict:
    """Find SENSEX index id, and the option contracts (strike, right) -> (security_id, lot)
    for one expiry, from the compact scrip master CSV. Column names VERIFY on first use."""
    rows = csv.DictReader(io.StringIO(text))
    out = {"index_id": None, "options": {}}
    for r in rows:
        exch, inst = r.get("SEM_EXM_EXCH_ID"), r.get("SEM_INSTRUMENT_NAME")
        sym = (r.get("SEM_TRADING_SYMBOL") or "").upper()
        if exch == "BSE" and inst == "INDEX" and sym == "SENSEX":
            out["index_id"] = r.get("SEM_SMST_SECURITY_ID")
        if exch == "BSE" and inst == "OPTIDX" and sym.startswith("SENSEX") and not sym.startswith("SENSEX50"):
            exp = (r.get("SEM_EXPIRY_DATE") or "")[:10]
            if exp != expiry.isoformat():
                continue
            try:
                strike = int(round(float(r.get("SEM_STRIKE_PRICE") or 0)))
                lot = int(float(r.get("SEM_LOT_UNITS") or 0))
            except ValueError:
                continue
            right = r.get("SEM_OPTION_TYPE")
            if right in ("CE", "PE") and strike > 0 and lot > 0:
                out["options"][(strike, right)] = (r.get("SEM_SMST_SECURITY_ID"), lot)
    return out


class DhanBroker:
    """BrokerExecutionProvider + snapshot data for Dhan. Live order methods refuse to run
    unless `armed` is True, which only the live runner sets after the validation gate."""

    def __init__(self, client_id: str, access_token: str, armed: bool = False, client=None):
        self.c = client or _client(client_id, access_token)
        self.armed = armed
        self._last_call = 0.0

    # ---------------------------------------------------------------- data
    def _throttle(self, min_gap: float = 0.25) -> None:
        gap = _time.monotonic() - self._last_call
        if gap < min_gap:
            _time.sleep(min_gap - gap)
        self._last_call = _time.monotonic()

    def expiries(self, index_id: str = SENSEX_SECURITY_ID_EXPECTED) -> list[date]:
        self._throttle()
        data = _ok(self.c.expiry_list(int(index_id), SENSEX_UNDERLYING_SEG))
        out = []
        for x in data or []:
            try:
                out.append(date.fromisoformat(str(x)[:10]))
            except ValueError:
                continue
        return sorted(out)

    def chain(self, expiry: date, index_id: str = SENSEX_SECURITY_ID_EXPECTED) -> tuple[float | None, dict]:
        self._throttle(3.0)    # option-chain limit: one unique request per 3 s (Dhan docs; VERIFY)
        resp = self.c.option_chain(int(index_id), SENSEX_UNDERLYING_SEG, expiry.isoformat())
        return parse_dhan_chain(resp, datetime.now(IST)) if _ok(resp) is not None else (None, {})

    def intraday_bars(self, security_id: str, segment: str, instrument: str, day_from: date, day_to: date) -> dict | None:
        self._throttle()
        return _ok(self.c.intraday_minute_data(security_id, segment, instrument, day_from.isoformat(),
                                               day_to.isoformat(), 1))

    def expired_option_bars(self, index_id: str, expiry_code: int, strike_rel: str, right: str,
                            day_from: date, day_to: date) -> dict | None:
        """Rolling ATM-relative history ('ATM', 'ATM+1', 'ATM-1', ...). The `strike` field
        in the response identifies the actual contract each minute; history.py rebuilds
        fixed-strike series from it."""
        self._throttle()
        return _ok(self.c.expired_options_data(index_id, "BSE_FNO", "OPTIDX", "WEEK", expiry_code, strike_rel,
                                               "CALL" if right == "CE" else "PUT",
                                               ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
                                               day_from.isoformat(), day_to.isoformat(), 1))

    # ---------------------------------------------------------------- orders
    def _require_armed(self) -> None:
        if not self.armed:
            raise PermissionError("DhanBroker is not armed: live orders are blocked by the validation gate")

    def _state(self, d: dict | None, req: OrderRequest | None) -> OrderState:
        if not isinstance(d, dict):
            return OrderState("", req, OrderStatus.UNKNOWN, message="no data")  # type: ignore[arg-type]
        return OrderState(str(d.get(FIELDS["order_id"], "")), req,  # type: ignore[arg-type]
                          STATUS_MAP.get(str(d.get(FIELDS["order_status"], "")).upper(), OrderStatus.UNKNOWN),
                          int(d.get(FIELDS["filled_qty"]) or 0), d.get(FIELDS["avg_price"]),
                          datetime.now(IST))

    def place(self, req: OrderRequest) -> OrderState:
        # the system is long-only: a BUY opens risk and needs the arm; a SELL only ever reduces
        # a position (exit, stop, square-off) and must work even after DISARM
        if req.side == "BUY":
            self._require_armed()
        resp = self.c.place_order(security_id=req.security_id, exchange_segment=req.exchange_segment,
                                  transaction_type=req.side, quantity=req.qty, order_type=req.order_type,
                                  product_type=req.product, price=req.price, trigger_price=req.trigger_price,
                                  validity=req.validity, tag=req.correlation_id)
        return self._state(_ok(resp), req)

    def modify(self, order_id: str, price: float, trigger_price: float, qty: int) -> OrderState:
        self._require_armed()
        cur = self.status(order_id)
        resp = self.c.modify_order(order_id, cur.request.order_type if cur.request else "STOP_LOSS", "",
                                   qty, price, trigger_price, 0, "DAY")
        return self._state(_ok(resp), cur.request)

    def cancel(self, order_id: str) -> OrderState:
        return self._state(_ok(self.c.cancel_order(order_id)), None)

    def status(self, order_id: str) -> OrderState:
        return self._state(_ok(self.c.get_order_by_id(order_id)), None)

    def find_by_correlation(self, correlation_id: str) -> OrderState | None:
        d = _ok(self.c.get_order_by_correlationID(correlation_id))
        if isinstance(d, list):
            d = d[0] if d else None
        return self._state(d, None) if d else None

    def positions(self) -> list[dict]:
        out = []
        for p in _ok(self.c.get_positions()) or []:
            out.append({"security_id": str(p.get(FIELDS["pos_security_id"])), "net_qty": int(p.get(FIELDS["pos_net_qty"]) or 0),
                        "pnl": float(p.get(FIELDS["pos_realized"]) or 0) + float(p.get(FIELDS["pos_unrealized"]) or 0)})
        return out

    def open_orders(self) -> list[OrderState]:
        return [s for s in (self._state(o, None) for o in (_ok(self.c.get_order_list()) or []))
                if s.status in (OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PARTIAL)]

    def available_margin(self) -> float:
        d = _ok(self.c.get_fund_limits()) or {}
        for k in FIELDS["funds_available"]:
            if k in d:
                return float(d[k])
        return 0.0        # unknown margin = no margin: the guard then refuses the order

    def day_pnl(self) -> float:
        return sum(p["pnl"] for p in self.positions())

    def square_off_all(self) -> list[OrderState]:
        """Emergency: cancel every open order, then sell every long position with a
        marketable limit. Works whether or not the broker is armed: exits are always allowed."""
        out = []
        for o in self.open_orders():
            out.append(self.cancel(o.order_id))
        for p in self.positions():
            if p["net_qty"] > 0:
                resp = self.c.place_order(security_id=p["security_id"], exchange_segment="BSE_FNO",
                                          transaction_type="SELL", quantity=p["net_qty"], order_type="MARKET",
                                          product_type="INTRADAY", price=0)
                out.append(self._state(_ok(resp), None))
        return out

    def account_kill_switch(self) -> bool:
        """Dhan's account-level kill switch disables trading for the rest of the day."""
        return _ok(self.c.kill_switch("ACTIVATE")) is not None


def ws_instruments(index_id: str, option_ids: list[str]) -> list[tuple[int, str, int]]:
    """Subscription tuples for dhanhq.marketfeed.MarketFeed (version='v2'):
    index as Ticker (an index has no depth/volume), options as Full (depth + OI)."""
    IDX, BSE_FNO, TICKER, FULL = 0, 8, 15, 21
    return [(IDX, index_id, TICKER)] + [(BSE_FNO, sid, FULL) for sid in option_ids]


def tick_from_feed(msg: dict, recv: datetime) -> Tick | None:
    """Convert one dhanhq MarketFeed message (Ticker/Quote/Full dict) into a Tick."""
    try:
        sid = str(msg["security_id"])
        ltp = float(msg["LTP"])
    except (KeyError, TypeError, ValueError):
        return None
    raw = msg.get("LTT")
    if isinstance(raw, (int, float)):
        ts, _ = normalize_ltt(int(raw), recv)
    elif isinstance(raw, str):
        # SDK 2.2.0 formats LTT with utcfromtimestamp(...).strftime('%H:%M:%S'). Whether that
        # string is IST or UTC wall-clock depends on how Dhan encodes the epoch: resolve it
        # against receipt time rather than assuming.
        try:
            hh, mm, ss = (int(x) for x in raw.split(":")[:3])
            ts = recv.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        except ValueError:
            return None
        shifted = ts + timedelta(hours=5, minutes=30)
        if abs((recv - ts).total_seconds()) > 3600 and abs((recv - shifted).total_seconds()) < 3600:
            ts = shifted
    else:
        ts = recv
    depth = msg.get("depth") or []
    bid = float(depth[0]["bid_price"]) if depth else None
    ask = float(depth[0]["ask_price"]) if depth else None
    return Tick(sid, ts, ltp, recv, volume=msg.get("volume"), oi=msg.get("OI"), bid=bid, ask=ask,
                bid_qty=sum(int(d["bid_quantity"]) for d in depth) if depth else None,
                ask_qty=sum(int(d["ask_quantity"]) for d in depth) if depth else None)


def option_quote_from_tick(t: Tick, strike: int, right: str) -> OptionQuote:
    return OptionQuote(strike, right, t.ltp, t.bid, t.ask, t.ts, oi=t.oi, volume=t.volume,
                       top5_bid_qty=t.bid_qty, top5_ask_qty=t.ask_qty, security_id=t.security_id)
