"""sr.py -- trend-gated support / resistance option-signal engine (NIFTY, BANKNIFTY, SENSEX).

Signal generation only: it prints and logs alerts; it never places orders.

Data: one JSON endpoint (default http://140.245.228.101:10000/public/all.json) carrying, per index,
today's completed 1-minute candles, the Dhan option chain (+ analytics), market depth for the strikes
nearest spot (20 levels NSE, 5 levels SENSEX) and a support/resistance map.

Decision order on every COMPLETED 1-minute candle (monitoring from 09:25 IST). Every gate is mandatory
and independent of the score; each decision is written to sr_audit.jsonl with its reason.

1. DATA     HTTP success is not valid data. Blocked, naming the exact field, when: market_open=false;
            candles_1m empty, not starting 09:15, > 3 missing minutes or any missing in the last 15,
            or the last candle closed > 90 s ago; updated_at > 120 s old; option_chain / depth missing,
            status STARTING (or another not-ready status), no strikes / contracts, a timestamp > 30 s
            old, or content unchanged for > 30 s (stale cache); underlying_ltp > 0.6% from the last
            close; expiry missing or in the past; supports and resistances both empty.
2. REGIME   classify_regime(): BULLISH / BEARISH / SIDEWAYS / TRANSITION / DATA_INVALID.
            DATA_INVALID  fewer than 20 1m candles or fewer than 4 completed 5m bars, or step 1 failed.
            SIDEWAYS      any of: efficiency ratio(20) < 0.20; >= 3 EMA9/EMA20 crosses in 20 bars;
                          ADX14 < 15; |EMA9-EMA20| < 0.10 ATR with |EMA20 slope| < 0.05 ATR.
            BULLISH       ALL of: EMA9 - EMA20 >= 0.10 ATR; EMA20 rose >= 0.05 ATR over 5 bars; EMA9
                          rising over 3 bars; close not more than 0.25 ATR below EMA20; close above
                          EMA50 once EMA50 exists; swing structure (3-bar pivots) not LH_LL / MIXED and
                          close not below the last swing low; 5-minute context up (5m close > 5m EMA9
                          and EMA9 rising [+ EMA9 > EMA20 once 20 bars]; before 10 bars: 5m close slope
                          >= +0.10 ATR5/bar and close above their mean); ADX >= 18 with +DI > -DI (or
                          efficiency >= 0.35 while ADX is not seeded). BEARISH is the mirror.
            TRANSITION    anything else, including 1m/5m disagreement ("timeframe conflict"), and a
                          BULLISH/BEARISH reading that held on only one candle (two are required).
            VWAP is used only when the candles carry traded volume. Index volume is 0, so the
            equal-weighted average is never presented or used as a VWAP.
            BULLISH -> only CALL setups, BEARISH -> only PUT setups, anything else -> no entry.
3. SETUPS   PULLBACK (trend continuation, see detect_pullback), BREAKOUT, REJECTION, RETEST and TRAP at
            merged S/R zones. Zone lifecycle: APPROACHING -> TESTING -> CONFIRMED -> ALERTED, or
            INVALIDATED (with the gate that failed) / EXPIRED (10 min without confirmation, price moved
            away, or the session window changed). A touch alone never alerts.
4. WINDOW   entries: PRIME 09:30-10:45, GOOD 10:45-11:15, SELECTIVE 13:30-14:00 (PULLBACK/RETEST only,
            score +10), SECONDARY 14:00-14:30 (PULLBACK/RETEST/BREAKOUT, score +5). None 11:15-13:30
            and none from 14:30. Hard exit of any open alert at 15:00.
5. DUPES    one alert per trigger candle; same zone + side blocked for 15 min (30 after a stop-out);
            one open alert per index. State persists in sr_state.json and survives a restart.
6. RISK     late (live price > 0.6 ATR past the trigger close) or extended (> 3 ATR beyond EMA20) ->
            reject; structural stop beyond the zone / pullback extreme, 0.6-2.5 ATR; T1 = next opposing
            zone, >= 1.2R on the index.
7. CONTRACT nearest-expiry CE (CALL) or PE (PUT), |delta| 0.40-0.65 nearest 0.52, with real delta,
            gamma, theta, vega and IV, bid/ask not crossed, spread <= 1.5%, OI and volume > 0, depth for
            the contract (agreeing with the chain within 5%) deep enough for the order. Entry = average
            price walking the ask ladder. Lot size from feed metadata or --lot-size, never guessed.
            Premium plan = delta*dS + 0.5*gamma*dS^2 + theta*t (+ vega*dIV). Max loss assumes the stop,
            IV -1 vol point, selling at the bid and round-trip costs; T1 reward / max loss >= 1.0.
8. SCORE    0-100 rank of setups that passed every gate (trigger, level, trend, momentum, option flow,
            room, penalties). >= --min-score (65) to alert. It is NOT a win probability.

Usage (see README / RUNBOOK for Windows PowerShell):
    python sr.py --lot-size NIFTY=<n> --lot-size BANKNIFTY=<n> --lot-size SENSEX=<n> --record snaps
    python sr.py --schema                    payload structure + a per-index DATA HEALTH verdict
    python sr.py --once --explain            one fetch: every setup and the gate that decided it
    python sr.py --replay snaps/20261019     re-run a recorded day offline
    python sr_eval.py snaps --split 2026-11-01   precision / recall / expectancy on recorded sessions
    python sr.py --self-test
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_URL = "http://140.245.228.101:10000/public/all.json"
INDICES = ("NIFTY", "BANKNIFTY", "SENSEX")
SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)
ALIASES = {
    "NIFTY": "NIFTY", "NIFTY50": "NIFTY", "NIFTY 50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY",
    "SENSEX": "SENSEX", "BSESENSEX": "SENSEX",
}


@dataclass
class Config:
    min_score: float = 65.0           # quality rank threshold (NOT a win probability)
    min_rr: float = 1.2               # index reward to the first target zone, in R
    min_net_rr: float = 1.0           # premium reward/risk after costs, slippage and an IV stress
    min_candles: int = 20             # EMA20 fully seeded before any regime is classified (09:35)
    monitor_start: dtime = dtime(9, 25)
    monitor_end: dtime = dtime(15, 30)
    square_off: dtime = dtime(15, 0)  # hard intraday exit: nothing stays open after 15:00
    zone_merge_atr: float = 0.15      # levels closer than this (or 0.04%) form one zone
    zone_merge_pct: float = 0.04
    touch_atr: float = 0.25           # "touched the zone" tolerance
    touch_pct: float = 0.03
    break_atr: float = 0.10           # close beyond the zone by at least this to count as a break
    body_min: float = 0.50
    wick_min: float = 0.40
    sweep_max_atr: float = 1.0        # deeper pierce than this is a break, not a sweep
    retest_window_min: int = 15
    stop_buffer_atr: float = 0.25
    min_stop_atr: float = 0.60
    max_stop_atr: float = 2.50
    chase_atr: float = 0.60           # live price already this far past the trigger close -> skip
    max_extension_atr: float = 3.0    # trigger close this far beyond EMA20 -> extended, skip
    target_gap_atr: float = 0.30      # a zone closer than this to entry is not a target
    approach_atr: float = 1.5         # APPROACHING state / WATCH line when a zone is within this
    setup_expiry_min: int = 10        # APPROACHING / TESTING not confirmed in this long -> EXPIRED
    cooldown_min: int = 15            # same zone + same side not re-alerted for this long
    # trend regime (see classify_regime)
    trend_sep_atr: float = 0.10       # |EMA9 - EMA20| >= this many ATR
    trend_slope20_atr: float = 0.05   # EMA20 moved >= this many ATR over 5 bars
    trend_pullback_atr: float = 0.25  # close may sit at most this far on the wrong side of EMA20
    adx_trend: float = 18.0
    adx_chop: float = 15.0
    er_trend: float = 0.35            # efficiency ratio needed when ADX is not yet seeded
    er_chop: float = 0.20
    max_crosses: int = 3              # EMA9/EMA20 crosses in the last 20 bars -> SIDEWAYS
    swing_k: int = 3                  # pivot = extreme of k bars on each side
    # trend-continuation pullback
    pb_lookback: int = 20
    pb_min_bars: int = 2
    pb_max_bars: int = 10
    impulse_atr: float = 1.5
    pb_min_retrace: float = 0.236
    pb_max_retrace: float = 0.618
    # option contract
    delta_lo: float = 0.40
    delta_hi: float = 0.65
    target_delta: float = 0.52
    max_spread_pct: float = 1.5
    max_quote_gap_pct: float = 5.0    # depth best ask vs chain ask disagreement -> inconsistent
    max_premium_loss_pct: float = 35.0
    iv_stress_pts: float = 1.0        # stop premium also assumes IV falls this many vol points
    require_depth: bool = True
    flow_strikes: int = 3             # strikes each side of ATM for the net OI flow
    buildup_lookback_s: int = 180
    flow_lookback_s: int = 300
    # data freshness
    stale_after_s: int = 90           # last completed candle closed longer ago than this -> stale
    updated_at_max_age_s: int = 120
    chain_stale_s: int = 30           # option chain / depth content unchanged this long -> stale
    max_spot_gap_pct: float = 0.6     # chain underlying vs last candle close
    max_missing_minutes: int = 3
    # costs per round trip (verify against your broker contract note; see round_trip_costs)
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.1
    exch_txn_pct: dict = field(default_factory=lambda: {"NSE": 0.03503, "BSE": 0.0325})
    sebi_per_crore: float = 10.0
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0
    lots: int = 1
    # lot sizes are NOT guessed: from feed metadata, else only what the user passes (--lot-size)
    lot_size: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def num(x: Any, default: float | None = 0.0) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def pick(d: Any, *keys: str, default: Any = None) -> Any:
    """First present, non-null key (exact, then case-insensitive)."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v is not None:
            return v
    return default


def scale(x: float, lo: float, hi: float) -> float:
    """Linear 0..1 between lo and hi (works for lo > hi too)."""
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def parse_ts(v: Any, today: date | None = None) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", v)):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        return datetime.fromtimestamp(x, IST)
    s = str(v).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        hh, mm, *ss = (int(p) for p in s.split(":"))
        d = today or datetime.now(IST).date()
        return datetime(d.year, d.month, d.day, hh, mm, ss[0] if ss else 0, tzinfo=IST)
    s = s.replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)


def beep(enabled: bool) -> None:
    if not enabled:
        return
    try:
        import winsound  # type: ignore
        for _ in range(3):
            winsound.Beep(1800, 180)
    except Exception:
        sys.stdout.write("\a\a\a")
        sys.stdout.flush()


# --------------------------------------------------------------------------
# Data model + tolerant parsing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)


@dataclass
class Leg:
    strike: float
    kind: str                       # CE / PE
    ltp: float = 0.0
    oi: float = 0.0
    prev_oi: float | None = None
    volume: float = 0.0
    iv: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    buildup: str | None = None      # server-side label, display only

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        return self.ltp

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m * 100.0 if m > 0 and self.ask >= self.bid > 0 else 999.0


@dataclass
class Book:
    strike: float
    kind: str
    ltp: float = 0.0
    bids: list = field(default_factory=list)     # [(price, qty, orders)] best first
    asks: list = field(default_factory=list)
    total_buy: float = 0.0
    total_sell: float = 0.0

    def imbalance(self) -> float | None:
        b = self.total_buy or sum(q for _, q, _ in self.bids)
        s = self.total_sell or sum(q for _, q, _ in self.asks)
        return b / s if s > 0 else None

    def fill_price(self, qty: float) -> float | None:
        """Average price to lift `qty` from the ask ladder (None if the ladder is too thin)."""
        left, cost = qty, 0.0
        for p, q, _ in self.asks:
            take = min(left, q)
            cost += take * p
            left -= take
            if left <= 0:
                return cost / qty
        return None


@dataclass
class Chain:
    expiry: str = ""
    spot: float = 0.0
    legs: dict = field(default_factory=dict)     # (strike, kind) -> Leg
    analytics: dict = field(default_factory=dict)

    def strikes(self) -> list[float]:
        return sorted({k[0] for k in self.legs})

    def step(self) -> float:
        s = self.strikes()
        diffs = sorted(b - a for a, b in zip(s, s[1:]) if b > a)
        return diffs[len(diffs) // 2] if diffs else 50.0

    def leg(self, strike: float, kind: str) -> Leg | None:
        return self.legs.get((strike, kind))

    def atm(self, spot: float) -> float | None:
        s = self.strikes()
        return min(s, key=lambda k: abs(k - spot)) if s else None


@dataclass
class RawLevel:
    price: float
    name: str
    confluence: int = 1
    kind: str = "intraday"           # intraday / major / option


@dataclass
class Snapshot:
    index: str
    candles: list
    prev_close: float | None
    today_open: float | None
    ltp: float | None
    updated_at: datetime | None
    chain: Chain | None
    depth: dict                      # (strike, kind) -> Book
    levels: list                     # [RawLevel]
    meta: dict = field(default_factory=dict)   # section status / timestamps / lot size, as the feed sent them


def _norm_index(k: str) -> str | None:
    key = re.sub(r"[\s_\-]", "", str(k).upper())
    for alias, name in ALIASES.items():
        if key == re.sub(r"\s", "", alias):
            return name
    return None


def index_blocks(payload: Any) -> dict[str, dict]:
    """Find the NIFTY / BANKNIFTY / SENSEX blocks wherever they sit (top level or one deep)."""
    out: dict[str, dict] = {}
    pools = [payload]
    if isinstance(payload, dict):
        pools += [v for v in payload.values() if isinstance(v, dict)]
    elif isinstance(payload, list):
        for row in payload:
            name = _norm_index(pick(row, "index", "name", "symbol", default="")) if isinstance(row, dict) else None
            if name:
                out[name] = row
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        for k, v in pool.items():
            name = _norm_index(k)
            if name and isinstance(v, dict) and name not in out:
                out[name] = v
    return out


def parse_candles(rows: Any, today: date | None = None) -> list[Candle]:
    if isinstance(rows, dict):          # {"timestamp": [...], "open": [...]} column form
        cols = [pick(rows, k) for k in ("timestamp", "open", "high", "low", "close", "volume")]
        if cols[0] and all(isinstance(c, list) for c in cols[:5]):
            rows = [list(r) for r in zip(*[c if c is not None else [0] * len(cols[0]) for c in cols])]
        else:
            rows = list(rows.values())
    seen: dict[datetime, Candle] = {}
    for r in rows or []:
        if isinstance(r, dict):
            ts = parse_ts(pick(r, "timestamp", "ts", "time", "datetime", "date", "t", "start_time"), today)
            vals = [num(pick(r, *ks), None) for ks in (("open", "o"), ("high", "h"), ("low", "l"),
                                                       ("close", "c"), ("volume", "v", "vol"))]
        elif isinstance(r, (list, tuple)) and len(r) >= 5:
            ts = parse_ts(r[0], today)
            vals = [num(x, None) for x in list(r[1:6]) + [None] * (6 - len(r))][:5]
        else:
            continue
        o, h, lo, c, v = vals
        if ts is None or None in (o, h, lo, c):
            continue
        if h < max(o, c) or lo > min(o, c) or lo <= 0:
            continue                    # broken candle geometry
        t = ts.astimezone(IST)
        if not SESSION_OPEN <= t.time() < SESSION_CLOSE:
            continue
        seen[t] = Candle(t, o, h, lo, c, v or 0.0)
    out = sorted(seen.values(), key=lambda c: c.ts)
    if out:                              # keep only the latest session in the payload
        last_day = out[-1].ts.date()
        out = [c for c in out if c.ts.date() == last_day]
    return out


def _leg(d: dict, strike: float, kind: str) -> Leg:
    g = pick(d, "greeks", default={}) or {}
    first = lambda v: (v[0].get("price") if v and isinstance(v[0], dict) else v[0]) if isinstance(v, list) and v else v
    prev_oi = num(pick(d, "previous_oi", "prev_oi", "oi_prev", "previous_open_interest"), None)
    return Leg(
        strike=strike, kind=kind,
        ltp=num(pick(d, "last_price", "ltp", "lastPrice", "price")),
        oi=num(pick(d, "oi", "open_interest", "openInterest")),
        prev_oi=prev_oi,
        volume=num(pick(d, "volume", "vol", "traded_volume")),
        iv=num(pick(d, "implied_volatility", "iv", "IV")),
        delta=num(pick(g, "delta", default=pick(d, "delta"))),
        gamma=num(pick(g, "gamma", default=pick(d, "gamma"))),
        theta=num(pick(g, "theta", default=pick(d, "theta"))),
        vega=num(pick(g, "vega", default=pick(d, "vega"))),
        bid=num(first(pick(d, "top_bid_price", "best_bid_price", "bid_price", "best_bid", "bid"))),
        ask=num(first(pick(d, "top_ask_price", "best_ask_price", "ask_price", "best_ask", "ask"))),
        bid_qty=num(pick(d, "top_bid_quantity", "best_bid_qty", "bid_qty", "bid_quantity")),
        ask_qty=num(pick(d, "top_ask_quantity", "best_ask_qty", "ask_qty", "ask_quantity")),
        buildup=pick(d, "buildup", "build_up", "oi_buildup"),
    )


def parse_chain(oc: Any) -> Chain | None:
    if not isinstance(oc, dict):
        return None
    data = pick(oc, "data", default=None)
    if isinstance(data, dict) and pick(oc, "strikes", "oc") is None:
        oc = {**oc, **data}
    ch = Chain(expiry=str(pick(oc, "expiry", "expiry_date", default="")),
               spot=num(pick(oc, "underlying_ltp", "last_price", "spot", "underlying")),
               analytics=pick(oc, "analytics", default={}) or {})
    rows = pick(oc, "strikes", "oc", "chain", default=[])
    items: Iterable = rows.items() if isinstance(rows, dict) else ((None, r) for r in rows or [])
    for key, r in items:
        if not isinstance(r, dict):
            continue
        strike = num(pick(r, "strike", "strike_price", "strikePrice", default=key), None)
        if not strike:
            continue
        for kind, keys in (("CE", ("ce", "CE", "call", "calls")), ("PE", ("pe", "PE", "put", "puts"))):
            d = pick(r, *keys)
            if isinstance(d, dict):
                ch.legs[(strike, kind)] = _leg(d, strike, kind)
    return ch if ch.legs else None


def _ladder(rows: Any) -> list:
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            p = num(pick(r, "price", "p"))
            q = num(pick(r, "quantity", "qty", "q"))
            o = num(pick(r, "orders", "num_orders", "order_count", "n"))
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            p, q, o = num(r[0]), num(r[1]), num(r[2]) if len(r) > 2 else 0.0
        else:
            continue
        if p > 0 and q > 0:
            out.append((p, q, o))
    return out


def parse_depth(dp: Any) -> dict:
    rows: list = []
    if isinstance(dp, list):
        rows = dp
    elif isinstance(dp, dict):
        inner = pick(dp, "contracts", "data", "items", "strikes")
        if isinstance(inner, list):
            rows = inner
        else:
            for k, v in (inner if isinstance(inner, dict) else dp).items():
                if isinstance(v, dict):
                    sub = [x for kk, x in v.items() if kk.upper() in ("CE", "PE") and isinstance(x, dict)]
                    if sub:
                        for kk, x in v.items():
                            if kk.upper() in ("CE", "PE") and isinstance(x, dict):
                                rows.append({"strike": pick(v, "strike", default=k), "type": kk.upper(), **x})
                    else:
                        rows.append(v)
    out: dict = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        strike = num(pick(r, "strike", "strike_price"), None)
        kind = str(pick(r, "type", "option_type", "kind", "right", "ce_pe", default="")).upper()
        kind = "CE" if kind in ("CE", "CALL", "C") else "PE" if kind in ("PE", "PUT", "P") else ""
        if not strike or not kind:
            continue
        bk = Book(strike, kind, ltp=num(pick(r, "last_price", "ltp")),
                  bids=sorted(_ladder(pick(r, "bid", "bids", "buy")), key=lambda x: -x[0]),
                  asks=sorted(_ladder(pick(r, "ask", "asks", "sell", "offer")), key=lambda x: x[0]),
                  total_buy=num(pick(r, "total_buy_quantity", "total_buy_qty", "total_buy", "tbq")),
                  total_sell=num(pick(r, "total_sell_quantity", "total_sell_qty", "total_sell", "tsq")))
        out[(strike, kind)] = bk
    return out


MAJOR_RE = re.compile(r"PD[HLC]|PW[HLC]|PM[HLC]|PREV|WEEK|MONTH|DAILY|WEEKLY|PIVOT|CPR|\bBC\b|\bTC\b|"
                      r"\b[RS][1-3]\b|_[RS][1-3]|20D|20_D|DAY_SWING|DAILY_SWING", re.I)
OPTION_RE = re.compile(r"OI|WALL|PAIN|CALL|PUT", re.I)


def _level_kind(name: str, hint: Any) -> str:
    h = str(hint or "").lower()
    if "option" in h or "oi" == h:
        return "option"
    if "major" in h or "daily" in h or "weekly" in h:
        return "major"
    if "intraday" in h:
        return "intraday"
    if OPTION_RE.search(name):
        return "option"
    if MAJOR_RE.search(name):
        return "major"
    return "intraday"


def parse_levels(lv: Any, spot: float) -> list[RawLevel]:
    out: list[RawLevel] = []
    if not isinstance(lv, dict):
        return out
    for key in ("supports", "resistances"):
        for e in pick(lv, key, default=[]) or []:
            if not isinstance(e, dict):
                continue
            p = num(pick(e, "price", "level", "value"), None)
            if not p:
                continue
            name = str(pick(e, "name", "label", "names", default="LEVEL"))
            if isinstance(pick(e, "names"), list):
                name = "+".join(map(str, e["names"]))
            conf = int(num(pick(e, "confluence", "confluence_count", "count", "strength"), 1) or 1)
            out.append(RawLevel(p, name, max(1, conf), _level_kind(name, pick(e, "type", "kind", "category", "group", "source"))))
    ref = pick(lv, "reference", default={}) or {}
    if isinstance(ref, dict) and spot > 0:
        for name, v in ref.items():
            p = num(v.get("price") if isinstance(v, dict) else v, None)
            if not p or abs(p - spot) / spot > 0.03:
                continue
            if any(abs(p - x.price) <= spot * 0.0001 for x in out):
                continue
            out.append(RawLevel(p, str(name), 1, _level_kind(str(name), None)))
    return out


TS_KEYS = ("updated_at", "last_updated", "fetched_at", "as_of", "timestamp", "ts")
LOT_KEYS = ("lot_size", "lotSize", "lot", "market_lot", "lot_qty")


def _lot_size(*objs: Any) -> int | None:
    for o in objs:
        v = num(pick(o, *LOT_KEYS), None)
        if v and v > 0 and float(v).is_integer():
            return int(v)
    return None


def parse_snapshot(name: str, block: dict, today: date | None = None) -> Snapshot:
    candles = parse_candles(pick(block, "candles_1m", "candles", "1m", default=[]), today)
    raw_oc = pick(block, "option_chain", "chain", "oc")
    raw_dp = pick(block, "depth", "market_depth")
    raw_lv = pick(block, "levels", "sr", "support_resistance")
    chain = parse_chain(raw_oc)
    ltp = num(pick(block, "ltp", "last_price"), None)
    spot = (chain.spot if chain and chain.spot else None) or ltp or (candles[-1].close if candles else 0.0)
    first_leg = None
    if isinstance(raw_oc, dict):
        rows = pick(raw_oc, "strikes", "oc", default=[])
        r0 = (next(iter(rows.values()), None) if isinstance(rows, dict) else (rows[0] if rows else None)) if rows else None
        first_leg = pick(r0, "ce", "CE", "call") if isinstance(r0, dict) else None
    meta = {
        "market_open": pick(block, "market_open", "is_market_open"),
        "chain_present": raw_oc is not None,
        "chain_status": pick(raw_oc, "status", "state") if isinstance(raw_oc, dict) else None,
        "chain_ts": parse_ts(pick(raw_oc, *TS_KEYS), today) if isinstance(raw_oc, dict) else None,
        "depth_present": raw_dp is not None,
        "depth_status": pick(raw_dp, "status", "state") if isinstance(raw_dp, dict) else None,
        "depth_ts": parse_ts(pick(raw_dp, *TS_KEYS), today) if isinstance(raw_dp, dict) else None,
        "levels_present": raw_lv is not None,
        "lot_size": _lot_size(raw_oc, block, first_leg),
    }
    return Snapshot(
        index=name, candles=candles,
        prev_close=num(pick(block, "previous_close", "prev_close"), None),
        today_open=num(pick(block, "today_open", "open"), None),
        ltp=ltp, updated_at=parse_ts(pick(block, "updated_at", "last_updated"), today),
        chain=chain, depth=parse_depth(pick(block, "depth", "market_depth")),
        levels=parse_levels(raw_lv, spot or 0.0), meta=meta,
    )


def live_spot(s: Snapshot) -> float:
    if s.chain and s.chain.spot > 0:
        return s.chain.spot
    if s.ltp:
        return s.ltp
    return s.candles[-1].close if s.candles else 0.0


# --------------------------------------------------------------------------
# Indicators (standard definitions, computed from raw candles)
# --------------------------------------------------------------------------

def ema(vals: list[float], n: int) -> list[float | None]:
    """EMA seeded with the SMA of the first n values; alpha = 2/(n+1)."""
    out: list[float | None] = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_wilder(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = sum(max(x, 0.0) for x in ch[:n]) / n
    al = sum(max(-x, 0.0) for x in ch[:n]) / n
    rs = lambda g, l: 50.0 if g == l == 0 else 100.0 if l == 0 else 100.0 - 100.0 / (1 + g / l)
    out[n] = rs(ag, al)
    for i in range(n + 1, len(closes)):
        x = ch[i - 1]
        ag = (ag * (n - 1) + max(x, 0.0)) / n
        al = (al * (n - 1) + max(-x, 0.0)) / n
        out[i] = rs(ag, al)
    return out


def true_ranges(cs: list[Candle]) -> list[float]:
    return [c.range if i == 0 else max(c.range, abs(c.high - cs[i - 1].close), abs(c.low - cs[i - 1].close))
            for i, c in enumerate(cs)]


def atr_wilder(cs: list[Candle], n: int = 14) -> list[float | None]:
    tr = true_ranges(cs)
    out: list[float | None] = [None] * len(cs)
    if len(cs) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(cs)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def adx_wilder(cs: list[Candle], n: int = 14) -> tuple[float | None, float | None, float | None]:
    """(ADX, +DI, -DI) at the last candle; ADX needs 2n candles."""
    if len(cs) < n + 1:
        return None, None, None
    tr, pdm, mdm = [], [], []
    for i in range(1, len(cs)):
        up = cs[i].high - cs[i - 1].high
        dn = cs[i - 1].low - cs[i].low
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(cs[i].range, abs(cs[i].high - cs[i - 1].close), abs(cs[i].low - cs[i - 1].close)))
    s_tr, s_p, s_m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs: list[float] = []

    def di() -> tuple[float, float, float]:
        p = 100 * s_p / s_tr if s_tr else 0.0
        m = 100 * s_m / s_tr if s_tr else 0.0
        return p, m, (100 * abs(p - m) / (p + m) if p + m else 0.0)

    p, m, dx = di()
    dxs.append(dx)
    for i in range(n, len(tr)):
        s_tr = s_tr - s_tr / n + tr[i]
        s_p = s_p - s_p / n + pdm[i]
        s_m = s_m - s_m / n + mdm[i]
        p, m, dx = di()
        dxs.append(dx)
    if len(dxs) < n:
        return None, p, m
    adx = sum(dxs[:n]) / n
    for x in dxs[n:]:
        adx = (adx * (n - 1) + x) / n
    return adx, p, m


def macd(closes: list[float], fast: int = 12, slow: int = 26, sig: int = 9):
    """(line, signal, hist) series; None until defined."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    start = next((i for i, x in enumerate(line) if x is not None), None)
    signal: list[float | None] = [None] * len(closes)
    if start is not None:
        for i, v in enumerate(ema([x for x in line[start:]], sig)):
            signal[start + i] = v
    hist = [l - s if l is not None and s is not None else None for l, s in zip(line, signal)]
    return line, signal, hist


def vwap_series(cs: list[Candle]) -> tuple[list[float], list[float], bool]:
    """Session VWAP of HLC3 and its weighted standard deviation. Equal weights when volume is 0."""
    weighted = sum(c.volume for c in cs) > 0
    sw = swp = swp2 = 0.0
    vw, sd = [], []
    for c in cs:
        tp = (c.high + c.low + c.close) / 3.0
        w = c.volume if weighted else 1.0
        sw += w
        swp += w * tp
        swp2 += w * tp * tp
        v = swp / sw if sw else tp
        vw.append(v)
        sd.append(math.sqrt(max(swp2 / sw - v * v, 0.0)) if sw else 0.0)
    return vw, sd, weighted


def resample(cs: list[Candle], minutes: int = 5, drop_forming: bool = True) -> list[Candle]:
    """09:15-anchored N-minute bars; the last bar is dropped if it is not complete."""
    buckets: dict[int, list[Candle]] = {}
    for c in cs:
        m = c.ts.hour * 60 + c.ts.minute - (9 * 60 + 15)
        buckets.setdefault(m // minutes, []).append(c)
    out = []
    for k in sorted(buckets):
        b = buckets[k]
        out.append(Candle(b[0].ts, b[0].open, max(x.high for x in b), min(x.low for x in b), b[-1].close,
                          sum(x.volume for x in b)))
    if drop_forming and cs and out:
        m = cs[-1].ts.hour * 60 + cs[-1].ts.minute - (9 * 60 + 15)
        if m % minutes != minutes - 1:
            out.pop()
    return out


def slope(vals: list[float]) -> float:
    """Least-squares slope per step."""
    n = len(vals)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = sum(vals) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (v - my) for i, v in enumerate(vals)) / den


@dataclass
class Indicators:
    close: float
    atr: float
    ema9: float | None
    ema20: float | None
    ema50: float | None
    ema9_prev: float | None
    ema9_5m: float | None
    ema20_5m: float | None
    close_5m: float | None
    vwap: float
    vwap_sd: float
    vwap_weighted: bool
    rsi: float | None
    rsi_prev: float | None
    adx: float | None
    pdi: float | None
    mdi: float | None
    macd_hist: float | None
    macd_hist_prev: float | None
    speed: float                    # pts / minute, slope of the last 5 closes
    day_high: float
    day_low: float


def compute_indicators(cs: list[Candle]) -> Indicators:
    closes = [c.close for c in cs]
    e9, e20, e50 = ema(closes, 9), ema(closes, 20), ema(closes, 50)
    atr = atr_wilder(cs, 14)[-1]
    if atr is None:
        tr = true_ranges(cs)
        atr = sum(tr) / len(tr)
    atr = max(atr, closes[-1] * 0.0001)
    b5 = resample(cs, 5)
    c5 = [c.close for c in b5]
    e9_5, e20_5 = ema(c5, 9), ema(c5, 20)
    vw, sd, weighted = vwap_series(cs)
    rsi = rsi_wilder(closes, 14)
    adx, pdi, mdi = adx_wilder(cs, 14)
    _, _, hist = macd(closes)
    return Indicators(
        close=closes[-1], atr=atr,
        ema9=e9[-1], ema20=e20[-1], ema50=e50[-1], ema9_prev=e9[-3] if len(e9) >= 3 else None,
        ema9_5m=e9_5[-1] if e9_5 else None, ema20_5m=e20_5[-1] if e20_5 else None,
        close_5m=c5[-1] if c5 else None,
        vwap=vw[-1], vwap_sd=sd[-1], vwap_weighted=weighted,
        rsi=rsi[-1], rsi_prev=rsi[-2] if len(rsi) >= 2 else None,
        adx=adx, pdi=pdi, mdi=mdi,
        macd_hist=hist[-1], macd_hist_prev=hist[-2] if len(hist) >= 2 else None,
        speed=slope(closes[-5:]),
        day_high=max(c.high for c in cs), day_low=min(c.low for c in cs),
    )


# --------------------------------------------------------------------------
# Zones
# --------------------------------------------------------------------------

KIND_WEIGHT = {"major": 3.0, "option": 2.5, "intraday": 1.5}


@dataclass
class Zone:
    price: float
    names: list
    confluence: int
    kinds: set
    strength: float                  # 0..1

    @property
    def label(self) -> str:
        n = "+".join(dict.fromkeys(self.names))
        return n if len(n) <= 42 else n[:39] + "..."


def build_zones(levels: list[RawLevel], spot: float, atr: float, cfg: Config) -> list[Zone]:
    tol = max(atr * cfg.zone_merge_atr, spot * cfg.zone_merge_pct / 100.0)
    groups: list[list[RawLevel]] = []
    for lv in sorted(levels, key=lambda x: x.price):
        if groups and lv.price - groups[-1][-1].price <= tol:
            groups[-1].append(lv)
        else:
            groups.append([lv])
    zones = []
    for g in groups:
        w = [KIND_WEIGHT.get(x.kind, 1.5) * x.confluence for x in g]
        price = sum(x.price * wi for x, wi in zip(g, w)) / sum(w)
        conf = max(max(x.confluence for x in g), len(g))
        kinds = {x.kind for x in g}
        raw = sum(KIND_WEIGHT.get(k, 1.5) for k in kinds) + 1.2 * (conf - 1)
        zones.append(Zone(price, [x.name for x in g], conf, kinds, min(1.0, raw / 8.0)))
    return zones


# --------------------------------------------------------------------------
# Option flow
# --------------------------------------------------------------------------

class ChainHistory:
    """Rolling (time, {(strike, kind): (ltp, oi)}) snapshots for build-up and recent OI flow."""

    def __init__(self, keep_s: int = 900):
        self.keep_s = keep_s
        self.rows: deque = deque()

    def add(self, t: datetime, chain: Chain) -> None:
        if self.rows and (t - self.rows[-1][0]).total_seconds() < 2:
            return
        self.rows.append((t, {k: (l.ltp, l.oi) for k, l in chain.legs.items()}))
        while self.rows and (t - self.rows[0][0]).total_seconds() > self.keep_s:
            self.rows.popleft()

    def ago(self, t: datetime, seconds: int) -> dict | None:
        """Snapshot 60%-200% of `seconds` old, closest to `seconds` ago (None across an outage gap)."""
        best = None
        for ts, snap in self.rows:
            age = (t - ts).total_seconds()
            if seconds * 0.6 <= age <= seconds * 2 and (best is None or abs(age - seconds) < best[0]):
                best = (abs(age - seconds), snap)
        return best[1] if best else None


def buildup(price_chg: float, oi_chg: float, oi: float, ltp: float) -> str:
    if oi <= 0 or ltp <= 0 or abs(oi_chg) / oi < 0.002 or abs(price_chg) / ltp < 0.003:
        return "neutral"
    if oi_chg > 0:
        return "long build-up" if price_chg > 0 else "short build-up"
    return "short covering" if price_chg > 0 else "long unwinding"


@dataclass
class Flow:
    pcr_oi: float | None
    pcr_vol: float | None
    max_pain: float | None
    net_day: float | None            # (dPE - dCE)/(|dPE|+|dCE|) near ATM, day change; +1 bullish
    net_recent: float | None         # same over the last ~5 minutes
    atm: float | None
    avg_iv_ce: float
    avg_iv_pe: float


def max_pain(chain: Chain) -> float | None:
    strikes = chain.strikes()
    if not strikes:
        return None
    def payout(x: float) -> float:
        t = 0.0
        for (k, kind), leg in chain.legs.items():
            t += leg.oi * (max(0.0, x - k) if kind == "CE" else max(0.0, k - x))
        return t
    return min(strikes, key=payout)


def chain_flow(chain: Chain, spot: float, hist: ChainHistory, now: datetime, cfg: Config) -> Flow:
    ce_oi = sum(l.oi for (k, t), l in chain.legs.items() if t == "CE")
    pe_oi = sum(l.oi for (k, t), l in chain.legs.items() if t == "PE")
    ce_v = sum(l.volume for (k, t), l in chain.legs.items() if t == "CE")
    pe_v = sum(l.volume for (k, t), l in chain.legs.items() if t == "PE")
    atm = chain.atm(spot)
    strikes = chain.strikes()
    near: list[float] = []
    if atm is not None:
        i = strikes.index(atm)
        near = strikes[max(0, i - cfg.flow_strikes): i + cfg.flow_strikes + 1]

    def net(prev_of) -> float | None:
        d_ce = d_pe = 0.0
        ok = False
        for k in near:
            for kind in ("CE", "PE"):
                leg = chain.leg(k, kind)
                p = prev_of(k, kind, leg)
                if leg is None or p is None:
                    continue
                ok = True
                if kind == "CE":
                    d_ce += leg.oi - p
                else:
                    d_pe += leg.oi - p
        tot = abs(d_ce) + abs(d_pe)
        return (d_pe - d_ce) / tot if ok and tot > 0 else None

    old = hist.ago(now, cfg.flow_lookback_s)
    ivs = lambda kind: [l.iv for (k, t), l in chain.legs.items() if t == kind and k in near and l.iv > 0]
    ce_iv, pe_iv = ivs("CE"), ivs("PE")
    return Flow(
        pcr_oi=pe_oi / ce_oi if ce_oi else None,
        pcr_vol=pe_v / ce_v if ce_v else None,
        max_pain=max_pain(chain),
        net_day=net(lambda k, kind, leg: leg.prev_oi if leg else None),
        net_recent=net(lambda k, kind, leg: (old.get((k, kind)) or (None, None))[1] if old else None),
        atm=atm,
        avg_iv_ce=sum(ce_iv) / len(ce_iv) if ce_iv else 0.0,
        avg_iv_pe=sum(pe_iv) / len(pe_iv) if pe_iv else 0.0,
    )


def leg_buildup(leg: Leg | None, hist: ChainHistory, now: datetime, cfg: Config) -> str:
    if leg is None:
        return "n/a"
    old = hist.ago(now, cfg.buildup_lookback_s)
    if not old or (leg.strike, leg.kind) not in old:
        return "n/a"
    p0, oi0 = old[(leg.strike, leg.kind)]
    return buildup(leg.ltp - p0, leg.oi - oi0, leg.oi, leg.ltp)


def oi_change(leg: Leg | None, hist: ChainHistory, now: datetime, seconds: int) -> tuple[float | None, float | None]:
    """(day change from previous_oi, recent change over `seconds`) of one leg."""
    if leg is None:
        return None, None
    day = leg.oi - leg.prev_oi if leg.prev_oi is not None else None
    old = hist.ago(now, seconds)
    recent = leg.oi - old[(leg.strike, leg.kind)][1] if old and (leg.strike, leg.kind) in old else None
    return day, recent


# --------------------------------------------------------------------------
# Session windows (IST). Start inclusive, end exclusive.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    name: str
    entries: bool                    # may a NEW position be alerted in this window?
    extra_score: float               # added to --min-score inside this window
    kinds: tuple | None              # setup kinds allowed (None = every trend-aligned kind)


WINDOWS = (
    (dtime(9, 15), dtime(9, 25), Window("PRE-MONITOR", False, 0, None)),
    (dtime(9, 25), dtime(9, 30), Window("WARM-UP", False, 0, None)),
    (dtime(9, 30), dtime(10, 45), Window("PRIME", True, 0, None)),
    (dtime(10, 45), dtime(11, 15), Window("GOOD", True, 0, None)),
    (dtime(11, 15), dtime(13, 30), Window("NO TRADE", False, 0, None)),
    (dtime(13, 30), dtime(14, 0), Window("SELECTIVE", True, 10, ("PULLBACK", "RETEST"))),
    (dtime(14, 0), dtime(14, 30), Window("SECONDARY", True, 5, ("PULLBACK", "RETEST", "BREAKOUT"))),
    (dtime(14, 30), dtime(15, 30), Window("NO NEW ENTRIES", False, 0, None)),
)


def session_window(t: datetime | dtime) -> Window:
    tt = t.astimezone(IST).time() if isinstance(t, datetime) else t
    for a, b, w in WINDOWS:
        if a <= tt < b:
            return w
    return Window("CLOSED", False, 0, None)


# --------------------------------------------------------------------------
# Data health: transport success is not valid market data
# --------------------------------------------------------------------------

NOT_READY = {"STARTING", "INIT", "INITIALIZING", "INITIALISING", "WAITING", "PENDING", "ERROR", "FAILED",
             "STALE", "STOPPED", "DISCONNECTED", "UNAVAILABLE", "OFFLINE", "CLOSED", "EMPTY", "NO_DATA"}


@dataclass
class Health:
    trend: list = field(default_factory=list)   # blocks regime classification and every signal
    trade: list = field(default_factory=list)   # blocks alerts (contract / room cannot be verified)
    warn: list = field(default_factory=list)

    @property
    def ok_trend(self) -> bool:
        return not self.trend

    @property
    def ok_trade(self) -> bool:
        return not self.trend and not self.trade

    def summary(self) -> str:
        if self.ok_trade:
            return "OK" + (f" (warnings: {'; '.join(self.warn)})" if self.warn else "")
        return "BLOCKED -- " + "; ".join(self.trend + self.trade)


def missing_minutes(cs: list[Candle]) -> list[str]:
    """Minutes absent between 09:15 and the last candle."""
    if not cs:
        return []
    have = {c.ts.strftime("%H:%M") for c in cs}
    out = []
    m, end = 9 * 60 + 15, cs[-1].ts.hour * 60 + cs[-1].ts.minute
    while m <= end:
        stamp = f"{m // 60:02d}:{m % 60:02d}"
        if stamp not in have:
            out.append(stamp)
        m += 1
    return out


def chain_fingerprint(snap: "Snapshot") -> tuple | None:
    ch = snap.chain
    if not ch:
        return None
    atm = ch.atm(ch.spot or live_spot(snap))
    ks = sorted(ch.strikes(), key=lambda k: abs(k - (atm or 0)))[:6]
    return (ch.spot,) + tuple((k, t, l.ltp, l.oi, l.bid, l.ask) for k in sorted(ks) for t in ("CE", "PE")
                              for l in [ch.leg(k, t)] if l)


def depth_fingerprint(snap: "Snapshot") -> tuple | None:
    if not snap.depth:
        return None
    return tuple(sorted((k, b.ltp, b.bids[:1] and b.bids[0][:2], b.asks[:1] and b.asks[0][:2], b.total_buy, b.total_sell)
                        for k, b in snap.depth.items()))


def assess(snap: "Snapshot", now: datetime, live: bool, st: "IndexState", cfg: Config,
           market_open: Any = None) -> Health:
    """Every reason this snapshot may not be used, naming the exact field and index."""
    h = Health()
    n = snap.index
    mo = snap.meta.get("market_open", None) if snap.meta.get("market_open") is not None else market_open
    if mo is False:
        h.trend.append(f"{n}: market_open=false")
    cs = snap.candles
    if not cs:
        h.trend.append(f"{n}: candles_1m empty (0 completed candles)")
    else:
        miss = missing_minutes(cs)
        last15 = [m for m in miss if m >= cs[-min(15, len(cs))].ts.strftime("%H:%M")]
        if cs[0].ts.strftime("%H:%M") != "09:15":
            h.trend.append(f"{n}: candles_1m starts at {cs[0].ts:%H:%M}, not 09:15 (indicators would be wrong)")
        elif len(miss) > cfg.max_missing_minutes or last15:
            h.trend.append(f"{n}: candles_1m missing minutes {', '.join(miss[:6])}{' ...' if len(miss) > 6 else ''}")
        elif miss:
            h.warn.append(f"{n}: candles_1m missing {', '.join(miss)}")
        if live:
            age = (now - (cs[-1].ts + timedelta(minutes=1))).total_seconds()
            if age > cfg.stale_after_s:
                h.trend.append(f"{n}: candles_1m stale (last candle {cs[-1].ts:%H:%M}, nothing newer for "
                               f"> {cfg.stale_after_s}s)")
    if live and snap.updated_at is not None:
        age = (now - snap.updated_at).total_seconds()
        if age > cfg.updated_at_max_age_s:
            h.trend.append(f"{n}: updated_at {snap.updated_at:%H:%M:%S} older than {cfg.updated_at_max_age_s}s")
    for fld, v in (("previous_close", snap.prev_close), ("today_open", snap.today_open), ("ltp", snap.ltp)):
        if v is None:
            h.warn.append(f"{n}: {fld}=null")

    # ---- option chain
    m = snap.meta
    status = str(m.get("chain_status") or "").upper()
    if not m.get("chain_present", snap.chain is not None):
        h.trade.append(f"{n}: option_chain missing")
    elif status in NOT_READY:
        h.trade.append(f"{n}: option_chain.status={status}")
    elif snap.chain is None:
        h.trade.append(f"{n}: option_chain has no parsable strikes")
    else:
        ch = snap.chain
        if ch.spot <= 0:
            h.trade.append(f"{n}: option_chain.underlying_ltp missing")
        elif cs and abs(ch.spot - cs[-1].close) / cs[-1].close * 100 > cfg.max_spot_gap_pct:
            h.trade.append(f"{n}: option_chain.underlying_ltp {ch.spot:.1f} inconsistent with last close {cs[-1].close:.1f}")
        if not ch.expiry:
            h.trade.append(f"{n}: option_chain.expiry missing")
        else:
            exp = parse_ts(ch.expiry[:10] + "T15:30:00")
            if exp is None:
                h.trade.append(f"{n}: option_chain.expiry {ch.expiry!r} unparsable")
            elif exp.date() < now.date():
                h.trade.append(f"{n}: option_chain.expiry {ch.expiry[:10]} is in the past (stale chain)")
        if status and status not in NOT_READY and status not in ("OK", "LIVE", "READY", "RUNNING", "ACTIVE", "STREAMING"):
            h.warn.append(f"{n}: option_chain.status={status} (unrecognised, treated as ready)")
        if live and m.get("chain_ts") is not None:
            age = (now - m["chain_ts"]).total_seconds()
            if age > cfg.chain_stale_s:
                h.trade.append(f"{n}: option_chain timestamp {m['chain_ts']:%H:%M:%S} older than {cfg.chain_stale_s}s")
        if st.chain_fp_since is not None and (now - st.chain_fp_since).total_seconds() > cfg.chain_stale_s:
            h.trade.append(f"{n}: option_chain unchanged for > {cfg.chain_stale_s}s since "
                           f"{st.chain_fp_since:%H:%M:%S} (stale cache)")

    # ---- depth
    dstatus = str(m.get("depth_status") or "").upper()
    if cfg.require_depth:
        if not m.get("depth_present", bool(snap.depth)):
            h.trade.append(f"{n}: depth missing")
        elif dstatus in NOT_READY:
            h.trade.append(f"{n}: depth.status={dstatus}")
        elif not snap.depth:
            h.trade.append(f"{n}: depth has no contracts")
        else:
            if live and m.get("depth_ts") is not None and (now - m["depth_ts"]).total_seconds() > cfg.chain_stale_s:
                h.trade.append(f"{n}: depth timestamp {m['depth_ts']:%H:%M:%S} older than {cfg.chain_stale_s}s")
            if st.depth_fp_since is not None and (now - st.depth_fp_since).total_seconds() > cfg.chain_stale_s:
                h.trade.append(f"{n}: depth unchanged for > {cfg.chain_stale_s}s since {st.depth_fp_since:%H:%M:%S} (stale)")

    # ---- levels (room to the next opposing level must be verifiable)
    if not snap.levels:
        h.trade.append(f"{n}: levels.supports and levels.resistances empty (room to next level unverifiable)")
    return h


# --------------------------------------------------------------------------
# Trend regime: BULLISH / BEARISH / SIDEWAYS / TRANSITION / DATA_INVALID
# --------------------------------------------------------------------------

BULLISH, BEARISH, SIDEWAYS, TRANSITION, DATA_INVALID = "BULLISH", "BEARISH", "SIDEWAYS", "TRANSITION", "DATA_INVALID"


@dataclass
class Regime:
    state: str
    reasons: list
    m: dict = field(default_factory=dict)

    @property
    def direction(self) -> int:
        return 1 if self.state == BULLISH else -1 if self.state == BEARISH else 0


def swings(cs: list[Candle], k: int) -> tuple[list, list]:
    """Confirmed pivots: a high above the k bars before it and not exceeded by the k bars after it."""
    highs, lows = [], []
    for i in range(k, len(cs) - k):
        h, l = cs[i].high, cs[i].low
        if h > max(c.high for c in cs[i - k:i]) and h >= max(c.high for c in cs[i + 1:i + k + 1]):
            highs.append((i, h))
        if l < min(c.low for c in cs[i - k:i]) and l <= min(c.low for c in cs[i + 1:i + k + 1]):
            lows.append((i, l))
    return highs, lows


def structure(cs: list[Candle], k: int) -> tuple[str, float | None, float | None]:
    """(HH_HL / LH_LL / MIXED / UNKNOWN, last swing high, last swing low)."""
    highs, lows = swings(cs, k)
    hi = highs[-1][1] if highs else None
    lo = lows[-1][1] if lows else None
    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN", hi, lo
    up_h, up_l = highs[-1][1] > highs[-2][1], lows[-1][1] > lows[-2][1]
    if up_h and up_l:
        return "HH_HL", hi, lo
    if not up_h and not up_l:
        return "LH_LL", hi, lo
    return "MIXED", hi, lo


def efficiency(closes: list[float], n: int = 20) -> float:
    """Kaufman efficiency ratio: net move / path length over n bars (1 = straight line)."""
    w = closes[-(n + 1):]
    path = sum(abs(b - a) for a, b in zip(w, w[1:]))
    return abs(w[-1] - w[0]) / path if path > 0 else 0.0


def ema_crosses(e9: list, e20: list, n: int = 20) -> int:
    signs = [1 if a > b else -1 for a, b in zip(e9[-n:], e20[-n:]) if a is not None and b is not None and a != b]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def five_minute_direction(cs: list[Candle]) -> tuple[int, str, int]:
    """(+1 / -1 / 0, explanation, completed 5m bars) from correctly aggregated 09:15-anchored 5m bars."""
    b5 = resample(cs, 5)
    n5 = len(b5)
    if n5 < 4:
        return 0, f"{n5} completed 5m bars", n5
    c5 = [b.close for b in b5]
    if n5 >= 10:                                  # EMA9 and its previous value both seeded
        e9 = ema(c5, 9)
        up = c5[-1] > e9[-1] and e9[-1] >= e9[-2]
        dn = c5[-1] < e9[-1] and e9[-1] <= e9[-2]
        note = f"5m close {c5[-1]:.1f} vs 5m EMA9 {e9[-1]:.1f}"
        if n5 >= 20:
            e20 = ema(c5, 20)
            up, dn = up and e9[-1] > e20[-1], dn and e9[-1] < e20[-1]
            note += f", 5m EMA20 {e20[-1]:.1f}"
    else:
        tr5 = true_ranges(b5)
        atr5 = sum(tr5[-6:]) / len(tr5[-6:])
        w = c5[-6:]
        sl = slope(w) / atr5 if atr5 > 0 else 0.0
        mean = sum(w) / len(w)
        up, dn = sl >= 0.10 and c5[-1] > mean, sl <= -0.10 and c5[-1] < mean
        note = f"5m slope {sl:+.2f} ATR5/bar over {len(w)} bars"
    return (1 if up else -1 if dn else 0), note, n5


def classify_once(cs: list[Candle], cfg: Config) -> Regime:
    if len(cs) < cfg.min_candles:
        return Regime(DATA_INVALID, [f"warming up: {len(cs)}/{cfg.min_candles} completed 1m candles"])
    closes = [c.close for c in cs]
    e9, e20, e50 = ema(closes, 9), ema(closes, 20), ema(closes, 50)
    atr = atr_wilder(cs, 14)[-1] or (sum(true_ranges(cs)) / len(cs))
    atr = max(atr, closes[-1] * 0.0001)
    close = closes[-1]
    k20 = min(5, len(cs) - 20)
    slope20 = (e20[-1] - e20[-1 - k20]) / atr if k20 > 0 else 0.0
    slope9 = (e9[-1] - e9[-4]) / atr
    sep = (e9[-1] - e20[-1]) / atr
    er = efficiency(closes, 20)
    crosses = ema_crosses(e9, e20, 20)
    adx, pdi, mdi = adx_wilder(cs, 14)
    struct, s_hi, s_lo = structure(cs, cfg.swing_k)
    d5, note5, n5 = five_minute_direction(cs)
    m = {"close": close, "ema9": e9[-1], "ema20": e20[-1], "ema50": e50[-1], "atr": atr, "sep_atr": sep,
         "slope9_atr": slope9, "slope20_atr": slope20, "er": er, "crosses": crosses, "adx": adx, "pdi": pdi,
         "mdi": mdi, "structure": struct, "swing_high": s_hi, "swing_low": s_lo, "dir5": d5, "n5": n5,
         "note5": note5}
    if n5 < 4:
        return Regime(DATA_INVALID, [f"only {n5} completed 5m bars (need 4)"], m)

    def fails(d: int) -> list[str]:
        f = []
        word = "bull" if d == 1 else "bear"
        if sep * d < cfg.trend_sep_atr:
            f.append(f"EMA9-EMA20 {sep:+.2f} ATR (need {'+' if d == 1 else '-'}{cfg.trend_sep_atr})")
        if slope20 * d < cfg.trend_slope20_atr:
            f.append(f"EMA20 slope {slope20:+.2f} ATR/5 bars")
        if slope9 * d <= 0:
            f.append(f"EMA9 slope {slope9:+.2f} ATR/3 bars")
        if (close - e20[-1]) * d < -cfg.trend_pullback_atr * atr:
            f.append(f"close {'below' if d == 1 else 'above'} EMA20 by >{cfg.trend_pullback_atr} ATR")
        if e50[-1] is not None and (close - e50[-1]) * d <= 0:
            f.append(f"close {'below' if d == 1 else 'above'} EMA50")
        if struct == ("LH_LL" if d == 1 else "HH_HL") or struct == "MIXED":
            f.append(f"swing structure {struct}")
        if d == 1 and s_lo is not None and close < s_lo:
            f.append(f"close below last swing low {s_lo:.1f}")
        if d == -1 and s_hi is not None and close > s_hi:
            f.append(f"close above last swing high {s_hi:.1f}")
        if d5 != d:
            f.append(f"5m context not {word}ish ({note5})")
        if adx is not None:
            if adx < cfg.adx_trend or (pdi - mdi) * d <= 0:
                f.append(f"ADX {adx:.0f} +DI {pdi:.0f} -DI {mdi:.0f}")
        elif er < cfg.er_trend:
            f.append(f"efficiency {er:.2f} < {cfg.er_trend} (ADX not seeded)")
        return f

    chop = []
    if er < cfg.er_chop:
        chop.append(f"efficiency ratio {er:.2f} < {cfg.er_chop}")
        m["chop"] = True
    if crosses >= cfg.max_crosses:
        chop.append(f"{crosses} EMA9/EMA20 crosses in 20 bars")
    if adx is not None and adx < cfg.adx_chop:
        chop.append(f"ADX {adx:.0f} < {cfg.adx_chop:.0f}")
    if abs(sep) < cfg.trend_sep_atr and abs(slope20) < cfg.trend_slope20_atr:
        chop.append("EMA9/EMA20 flat and intertwined")
    if chop:
        return Regime(SIDEWAYS, chop, m)
    fb, fs = fails(1), fails(-1)
    if not fb:
        return Regime(BULLISH, [f"EMA9>EMA20 rising, 5m up ({note5}), structure {struct}, "
                                f"{'ADX %.0f' % adx if adx is not None else 'ER %.2f' % er}"], m)
    if not fs:
        return Regime(BEARISH, [f"EMA9<EMA20 falling, 5m down ({note5}), structure {struct}, "
                                f"{'ADX %.0f' % adx if adx is not None else 'ER %.2f' % er}"], m)
    lean = 1 if len(fb) <= len(fs) else -1
    why = fb if lean == 1 else fs
    conflict = []
    if d5 and (sep > 0) != (d5 > 0):
        conflict.append(f"timeframe conflict: 1m EMAs {'up' if sep > 0 else 'down'}, 5m {'up' if d5 > 0 else 'down'}")
    return Regime(TRANSITION, conflict + [f"leans {'BULLISH' if lean == 1 else 'BEARISH'} but: " + "; ".join(why)], m)


def classify_regime(cs: list[Candle], cfg: Config) -> Regime:
    """A trend counts only when it holds on the last TWO completed candles (no first-cross entries)."""
    r = classify_once(cs, cfg)
    if r.state in (BULLISH, BEARISH):
        prev = classify_once(cs[:-1], cfg)
        if prev.state != r.state:
            return Regime(TRANSITION, [f"{r.state} on {cs[-1].ts:%H:%M} only; previous candle was {prev.state} "
                                       "(waiting for a second confirming candle)"], r.m)
    return r


# --------------------------------------------------------------------------
# Setups, scoring, contract selection
# --------------------------------------------------------------------------

@dataclass
class Setup:
    kind: str                        # BREAKOUT / REJECTION / RETEST / TRAP
    side: int                        # +1 CALL, -1 PUT
    zone: Zone
    quality: float                   # 0..1 candle quality
    note: str
    stop_ref: float | None = None    # structural invalidation point (PULLBACK: the pullback extreme)


@dataclass
class OptionPick:
    strike: float
    kind: str
    entry: float
    bid: float
    ask: float
    ltp: float
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    oi: float
    oi_day: float | None
    volume: float
    spread_pct: float
    book_ratio: float | None
    depth_fill: bool
    sl: float = 0.0
    t1: float = 0.0
    t2: float = 0.0


@dataclass
class Signal:
    index: str
    time: str
    expiry: str
    setup: str
    side: str                        # CALL / PUT
    zone: str
    zone_price: float
    confluence: int
    score: float
    grade: str
    entry: float
    stop: float
    target1: float
    target2: float
    t1_label: str
    t2_label: str
    rr1: float
    rr2: float
    option: dict
    parts: dict
    reasons: list
    risks: list
    lot_size: int
    lots: int
    regime: str = ""
    regime_why: str = ""
    window: str = ""
    trigger_ts: str = ""             # completed candle that confirmed the setup (ISO)
    entry_lo: float = 0.0            # acceptable underlying entry zone
    entry_hi: float = 0.0
    next_level: str = ""             # next opposing level (= T1 reference)
    lot_source: str = ""
    costs_rs: float = 0.0
    max_loss_rs: float = 0.0         # premium loss at the stop incl. IV stress, slippage and costs
    reward_rs: float = 0.0           # premium gain at T1 after slippage and costs
    net_rr: float = 0.0


def detect_setups(cs: list[Candle], zones: list[Zone], ind: Indicators, broken: list, now: datetime,
                  cfg: Config) -> list[Setup]:
    c, p = cs[-1], cs[-2]
    prior = cs[-6:-1]
    atr = ind.atr
    tol = max(cfg.touch_atr * atr, c.close * cfg.touch_pct / 100.0)
    buf = cfg.break_atr * atr
    rng = max(c.range, 1e-9)
    body = c.body / rng
    pos = (c.close - c.low) / rng              # 1 = closed at the high
    lower_wick = (min(c.open, c.close) - c.low) / rng
    upper_wick = (c.high - max(c.open, c.close)) / rng
    out: list[Setup] = []

    def recent_break(L: float, side: int, min_age: int) -> bool:
        for price, s, t in broken:
            age = (c.ts - t).total_seconds() / 60.0
            if s == side and abs(price - L) <= tol and min_age <= age <= cfg.retest_window_min:
                return True
        return False

    for z in zones:
        L = z.price
        if abs(c.close - L) > 3 * atr and abs(c.low - L) > 3 * atr and abs(c.high - L) > 3 * atr:
            continue
        # ---- breaks
        for side in (1, -1):
            beyond = (c.close - L) * side
            was = (p.close - L) * side <= 0
            outer = pos if side == 1 else 1 - pos
            if was and beyond >= buf and body >= cfg.body_min and outer >= 0.6 and (c.close - c.open) * side > 0:
                q = 0.45 * scale(body, cfg.body_min, 0.9) + 0.35 * scale(beyond / atr, cfg.break_atr, 0.6) + 0.2 * outer
                trap = recent_break(L, -side, 1)
                out.append(Setup("TRAP" if trap else "BREAKOUT", side, z, min(1.0, q + (0.1 if trap else 0)),
                                 f"closed {beyond:.1f} pts {'above' if side == 1 else 'below'} with {body:.0%} body"
                                 + ("; reverses a failed break" if trap else "")))
        # ---- holds at the zone (rejection / retest), side = direction of the trade
        for side in (1, -1):
            extreme = c.low if side == 1 else c.high
            touched = (extreme - L) * side <= tol
            pierce = (L - extreme) * side                 # >0: traded through the zone
            held = (c.close - L) * side >= 0.05 * atr
            wick = lower_wick if side == 1 else upper_wick
            outer = pos if side == 1 else 1 - pos
            reversal = (c.close - (p.high if side == 1 else p.low)) * side > 0 and (c.close - c.open) * side > 0
            if not (touched and held and pierce <= cfg.sweep_max_atr * atr and outer >= 0.55):
                continue
            if recent_break(L, side, 2):
                if wick >= 0.30 or (c.close - c.open) * side > 0:
                    out.append(Setup("RETEST", side, z, min(1.0, 0.7 + 0.3 * outer),
                                     f"retest of the broken zone held ({wick:.0%} wick)"))
                continue
            came_from = sum(1 for x in prior if (x.close - L) * side > 0)
            if came_from < 3 or not (wick >= cfg.wick_min or reversal):
                continue
            sweep = pierce > buf
            q = 0.5 * scale(max(wick, 0.45 if reversal else 0.0), cfg.wick_min - 0.05, 0.75) + 0.3 * outer \
                + 0.2 * scale((c.close - L) * side / atr, 0.05, 0.5) + (0.1 if sweep else 0.0)
            out.append(Setup("REJECTION", side, z, min(1.0, q),
                             ("SWEEP & reclaim, " if sweep else "") + f"{wick:.0%} wick"
                             + (", closed beyond prev candle" if reversal else "")))
    return out


def record_breaks(cs: list[Candle], zones: list[Zone], ind: Indicators, broken: list, cfg: Config) -> None:
    c, p = cs[-1], cs[-2]
    buf = cfg.break_atr * ind.atr
    for z in zones:
        for side in (1, -1):
            if (p.close - z.price) * side <= 0 and (c.close - z.price) * side >= buf:
                broken.append((z.price, side, c.ts))
    broken[:] = [b for b in broken if (c.ts - b[2]).total_seconds() <= cfg.retest_window_min * 60 + 60]


def contract_problems(l: Leg, bk: "Book | None", qty: float, cfg: Config) -> list[str]:
    """Everything that makes a contract untradeable or its data untrustworthy. Nothing is filled in."""
    p = []
    if l.ltp <= 0:
        p.append("no LTP")
    if l.bid <= 0 or l.ask <= 0:
        p.append("no bid/ask")
    elif l.ask < l.bid:
        p.append(f"crossed quote bid {l.bid} > ask {l.ask}")
    elif l.spread_pct > cfg.max_spread_pct and l.ask - l.bid > 0.30:
        p.append(f"spread {l.spread_pct:.2f}% > {cfg.max_spread_pct}%")
    missing = [g for g, v in (("delta", l.delta), ("gamma", l.gamma), ("theta", l.theta), ("vega", l.vega),
                              ("iv", l.iv)) if not v or (g in ("gamma", "vega", "iv") and v < 0)]
    if missing:
        p.append("missing greeks: " + ", ".join(missing))
    if l.oi <= 0:
        p.append("no OI")
    if l.volume <= 0:
        p.append("no volume today")
    if cfg.require_depth:
        if bk is None or not bk.asks or not bk.bids:
            p.append("no market depth for this contract")
    elif l.ask_qty and l.ask_qty < qty:
        p.append(f"ask qty {l.ask_qty:.0f} < order qty {qty:.0f}")
    if bk is not None and bk.asks:
        if bk.fill_price(qty) is None:
            p.append(f"ask depth {sum(q for _, q, _ in bk.asks):.0f} < order qty {qty:.0f}")
        if l.ask > 0 and abs(bk.asks[0][0] - l.ask) / l.ask * 100 > cfg.max_quote_gap_pct:
            p.append(f"depth ask {bk.asks[0][0]} vs chain ask {l.ask} disagree")
        if bk.bids and bk.bids[0][0] > bk.asks[0][0]:
            p.append("depth book crossed")
    return p


def choose_option(chain: Chain, depth: dict, side: int, spot: float, lot_qty: float, cfg: Config,
                  hist: ChainHistory, now: datetime) -> tuple[OptionPick | None, str]:
    kind = "CE" if side == 1 else "PE"
    legs = [l for (k, t), l in chain.legs.items() if t == kind]
    if not legs:
        return None, f"option_chain has no {kind} legs"
    if not any(l.delta for l in legs):
        return None, f"option_chain greeks missing (delta absent on every {kind})"
    pool = [l for l in legs if l.delta * side > 0 and cfg.delta_lo <= abs(l.delta) <= cfg.delta_hi]
    if not pool:
        return None, f"no {kind} with |delta| {cfg.delta_lo}-{cfg.delta_hi}"
    ok, bad = [], []
    for l in pool:
        probs = contract_problems(l, depth.get((l.strike, kind)), lot_qty, cfg)
        (bad if probs else ok).append((l, probs))
    if not ok:
        return None, "no tradable " + kind + ": " + "; ".join(f"{int(l.strike)}{kind} {pr[0]}" for l, pr in bad[:3])
    vmax = max(l.volume for l, _ in ok) or 1.0
    def rank(l: Leg) -> float:
        d = 1 - abs(abs(l.delta) - cfg.target_delta) / 0.15
        return 0.5 * d + 0.3 * l.volume / vmax + 0.2 * (1 - min(l.spread_pct, cfg.max_spread_pct) / cfg.max_spread_pct)
    best = max((l for l, _ in ok), key=rank)
    bk = depth.get((best.strike, kind))
    fill = bk.fill_price(lot_qty) if bk and bk.asks else None
    bid = bk.bids[0][0] if bk and bk.bids else best.bid
    ask = bk.asks[0][0] if bk and bk.asks else best.ask
    entry = fill or ask
    day, _ = oi_change(best, hist, now, cfg.flow_lookback_s)
    return OptionPick(best.strike, kind, entry, bid, ask, best.ltp, best.delta, best.gamma, best.theta, best.vega,
                      best.iv, best.oi, day, best.volume, best.spread_pct, bk.imbalance() if bk else None,
                      fill is not None), ""


def premium_at(op: OptionPick, d_spot: float, side: int, minutes: float = 0.0, d_iv: float = 0.0) -> float:
    """Premium after an index move of d_spot, `minutes` of decay and an IV change of d_iv vol points:
    delta*dS + 0.5*gamma*dS^2 + theta*days + vega*dIV. A local Taylor estimate, not a pricing model."""
    d = op.delta * d_spot + 0.5 * op.gamma * d_spot * d_spot + op.theta * minutes / 375.0 + op.vega * d_iv
    return max(0.05, op.entry + d)


def round_trip_costs(buy: float, sell: float, qty: float, index: str, cfg: Config) -> float:
    """Rupee cost of one buy + one sell of an index option (brokerage, STT on the sell premium, exchange
    transaction charge, SEBI fee, stamp duty on the buy, GST). Rates are Config values: verify them."""
    bt, stv = buy * qty, sell * qty
    exch = cfg.exch_txn_pct["BSE" if index == "SENSEX" else "NSE"] / 100.0 * (bt + stv)
    brokerage = 2 * cfg.brokerage_per_order
    sebi = cfg.sebi_per_crore / 1e7 * (bt + stv)
    gst = cfg.gst_pct / 100.0 * (brokerage + exch + sebi)
    return brokerage + cfg.stt_sell_pct / 100.0 * stv + exch + sebi + cfg.stamp_buy_pct / 100.0 * bt + gst


def detect_pullback(cs: list[Candle], regime: Regime, zones: list[Zone], ind: Indicators, cfg: Config) -> Setup | None:
    """Trend continuation: impulse with the trend -> controlled pullback -> FIRST re-acceleration candle.

    For a BULLISH regime (mirror for BEARISH), on the last pb_lookback completed candles:
      impulse    H = highest high before the trigger, L0 = lowest low at/before H; H-L0 >= 1.5 ATR
      pullback   2-10 candles after H; P = their lowest low; retrace (H-P)/(H-L0) in 0.236-0.618;
                 no pullback close more than 0.25 ATR below EMA20; average pullback range <= average
                 impulse range (controlled, not a reversal)
      trigger    the last candle closes above the previous candle's high and above EMA9, bullish body
                 >= 40% of its range, and it is the first such close since P (no late entries);
                 close not more than 0.25 ATR above H
      stop       below P (structural)
    """
    d = regime.direction
    if d == 0 or len(cs) < cfg.pb_lookback + 1:
        return None
    look = cs[-(cfg.pb_lookback + 1):]
    off = len(cs) - len(look)
    e9 = ema([c.close for c in cs], 9)
    e20 = ema([c.close for c in cs], 20)
    c, p = look[-1], look[-2]
    atr = ind.atr
    body = look[:-1]
    ext = max(range(len(body)), key=lambda i: body[i].high * d if d == 1 else -body[i].low)
    H = body[ext].high if d == 1 else body[ext].low
    seg = body[ext + 1:]
    if not cfg.pb_min_bars <= len(seg) <= cfg.pb_max_bars:
        return None
    pre = body[:ext + 1]
    o_i = min(range(len(pre)), key=lambda i: pre[i].low) if d == 1 else max(range(len(pre)), key=lambda i: pre[i].high)
    L0 = pre[o_i].low if d == 1 else pre[o_i].high
    impulse = (H - L0) * d
    if impulse < cfg.impulse_atr * atr or ext - o_i < 2:
        return None
    p_i = min(range(len(seg)), key=lambda i: seg[i].low) if d == 1 else max(range(len(seg)), key=lambda i: seg[i].high)
    P = seg[p_i].low if d == 1 else seg[p_i].high
    retr = (H - P) * d / impulse
    if not cfg.pb_min_retrace <= retr <= cfg.pb_max_retrace:
        return None
    base = off + ext + 1
    for j, x in enumerate(seg):
        ref = e20[base + j]
        if ref is not None and (x.close - ref) * d < -cfg.trend_pullback_atr * atr:
            return None                                   # pullback closed through EMA20: not controlled
    imp_rng = sum(x.range for x in pre[o_i:]) / len(pre[o_i:])
    pb_rng = sum(x.range for x in seg) / len(seg)
    if pb_rng > imp_rng:
        return None
    for j in range(p_i + 1, len(seg)):                    # an earlier reclaim already happened -> late
        prev = seg[j - 1]
        if (seg[j].close - (prev.high if d == 1 else prev.low)) * d > 0:
            return None
    rng = max(c.range, 1e-9)
    ok_trigger = ((c.close - (p.high if d == 1 else p.low)) * d > 0 and (c.close - c.open) * d > 0
                  and c.body / rng >= 0.4 and e9[-1] is not None and (c.close - e9[-1]) * d > 0
                  and (c.close - H) * d <= 0.25 * atr)
    if not ok_trigger:
        return None
    tol = max(cfg.touch_atr * atr, c.close * cfg.touch_pct / 100.0)
    held = [z for z in zones if abs(z.price - P) <= tol]
    if held:
        zone = max(held, key=lambda z: z.strength)
        where = f"pullback held at {zone.label} {zone.price:.1f}"
    else:
        zone = Zone(P, ["PULLBACK " + ("LOW" if d == 1 else "HIGH")], 1, {"structure"}, 0.25)
        where = "pullback low not at a mapped level"
    q = 0.4 * (1 - min(abs(retr - 0.45) / 0.25, 1)) + 0.3 * min(c.body / rng, 1) + 0.3 * (1 - pb_rng / imp_rng)
    note = (f"impulse {impulse:.1f} pts ({impulse / atr:.1f} ATR) to {H:.1f}, {len(seg)}-bar pullback "
            f"retraced {retr:.0%} to {P:.1f}, re-acceleration close {c.close:.1f}; {where}")
    return Setup("PULLBACK", d, zone, min(1.0, q + (0.1 if held else 0.0)), note, stop_ref=P)


def grade(score: float) -> str:
    return "A+" if score >= 82 else "A" if score >= 72 else "B" if score >= 60 else "C"


def evaluate(snap: Snapshot, st: "IndexState", setup: Setup, ind: Indicators, zones: list[Zone], flow: Flow | None,
             now: datetime, cfg: Config, regime: Regime | None = None,
             window: Window | None = None) -> tuple[Signal | None, str, float, str]:
    """Apply the mandatory risk / room / liquidity gates, then score one trend-aligned setup.

    Returns (signal or None, reason if rejected, score, rejection category). Categories:
    extended, structure, room, data, contract, liquidity, risk, score. The score only ranks setups
    that already passed every gate; it is not a probability."""
    cs = snap.candles
    c = cs[-1]
    d = setup.side
    atr = ind.atr
    L = setup.zone.price
    spot = live_spot(snap)
    entry = spot if spot > 0 else c.close
    tol = max(cfg.touch_atr * atr, c.close * cfg.touch_pct / 100.0)

    window = window or session_window(now)
    if (entry - c.close) * d > cfg.chase_atr * atr:
        return None, f"late: live price {(entry - c.close) * d:.1f} pts past the trigger close", 0, "extended"
    if ind.ema20 is not None and (c.close - ind.ema20) * d > cfg.max_extension_atr * atr:
        return None, (f"extended: close {(c.close - ind.ema20) * d / atr:.1f} ATR beyond EMA20 "
                      f"(max {cfg.max_extension_atr})"), 0, "extended"
    # ---- stop (structural)
    tight_stop = False
    if setup.kind == "PULLBACK" and setup.stop_ref is not None:
        stop = setup.stop_ref - d * cfg.stop_buffer_atr * atr
    elif setup.kind == "BREAKOUT":
        stop = L - d * max(cfg.stop_buffer_atr * atr + 0.1 * atr, tol)
        if (entry - stop) * d > 1.5 * atr:           # big breakout candle: stop at its midpoint instead
            mid = (c.high + c.low) / 2.0 - d * 0.1 * atr
            if (entry - mid) * d >= cfg.min_stop_atr * atr:
                stop, tight_stop = mid, True
    elif setup.kind == "TRAP":                      # beyond the failed-break candle's extreme
        stop = (c.low if d == 1 else c.high) - d * cfg.stop_buffer_atr * atr
    else:
        stop = (c.low if d == 1 else c.high) - d * cfg.stop_buffer_atr * atr
        stop = min(stop, L - tol) if d == 1 else max(stop, L + tol)
    risk = (entry - stop) * d
    if risk <= 0:
        return None, f"stop {stop:.1f} is not beyond the entry {entry:.1f}", 0, "structure"
    if risk > cfg.max_stop_atr * atr:
        return None, f"structural stop too wide ({risk:.1f} pts = {risk / atr:.1f} ATR)", 0, "structure"
    if risk < cfg.min_stop_atr * atr:
        stop = entry - d * cfg.min_stop_atr * atr
        risk = cfg.min_stop_atr * atr
    # ---- targets = next zones in the trade direction
    ahead = sorted((z for z in zones if (z.price - entry) * d > cfg.target_gap_atr * atr and z is not setup.zone),
                   key=lambda z: (z.price - entry) * d)
    if ahead:
        t1 = ahead[0].price - d * 0.1 * atr
        t1_label = ahead[0].label
        if len(ahead) > 1:
            t2, t2_label = ahead[1].price - d * 0.1 * atr, ahead[1].label
        else:
            t2, t2_label = entry + d * max(2.5 * risk, (t1 - entry) * d * 1.6), "2.5R / extension"
    else:
        t1, t1_label = entry + d * 1.5 * risk, "1.5R (no mapped zone)"
        t2, t2_label = entry + d * 2.5 * risk, "2.5R"
    rr1 = (t1 - entry) * d / risk
    rr2 = (t2 - entry) * d / risk
    if rr1 < cfg.min_rr:
        return None, f"no room: next opposing level {t1_label} only {rr1:.2f}R away", 0, "room"
    if snap.chain is None:
        return None, "no option chain", 0, "data"
    if snap.meta.get("lot_size"):
        lot, lot_src = snap.meta["lot_size"], "feed metadata"
    elif cfg.lot_size.get(snap.index):
        lot, lot_src = cfg.lot_size[snap.index], "--lot-size (user-verified)"
    else:
        return None, (f"lot size unverified: feed has no lot_size and no --lot-size {snap.index}=N given"), 0, "contract"
    op, why = choose_option(snap.chain, snap.depth, d, spot, lot * cfg.lots, cfg, st.hist, now)
    if op is None:
        return None, why, 0, "liquidity"

    reasons: list[str] = [f"{setup.kind} at {setup.zone.label} {L:.1f}: {setup.note}"]
    risks: list[str] = []
    if tight_stop:
        risks.append("stop at the breakout candle's midpoint; a retest of the zone can tag it (RETEST re-entry)")
    parts: dict[str, float] = {}

    # 1. trigger (20)
    parts["trigger"] = 20 * setup.quality
    # 2. level strength (15)
    parts["level"] = 15 * setup.zone.strength
    if setup.zone.confluence >= 2:
        reasons.append(f"confluence {setup.zone.confluence} ({'/'.join(sorted(setup.zone.kinds))})")
    # 3. trend (20)
    t = 0.0
    if ind.ema9 is not None and ind.ema20 is not None:
        if (ind.ema9 - ind.ema20) * d > 0:
            t += 6
            reasons.append(f"EMA9 {'>' if d == 1 else '<'} EMA20")
        else:
            risks.append("EMA9/EMA20 against the trade")
    if ind.ema9 is not None and ind.ema9_prev is not None and (ind.ema9 - ind.ema9_prev) * d > 0:
        t += 3
    if ind.vwap_weighted:                    # a true traded-volume VWAP only; an index TWAP is not used
        if (c.close - ind.vwap) * d > 0:
            t += 5
            reasons.append(f"{'above' if d == 1 else 'below'} VWAP {ind.vwap:.1f}")
        else:
            risks.append(f"on the wrong side of VWAP {ind.vwap:.1f}")
    if ind.ema9_5m is not None and ind.ema20_5m is not None:
        if (ind.ema9_5m - ind.ema20_5m) * d > 0:
            t += 4
            reasons.append("5m EMA9/20 aligned")
    elif ind.ema9_5m is not None and ind.close_5m is not None and (ind.close_5m - ind.ema9_5m) * d > 0:
        t += 3
    if ind.ema9 is not None and (c.close - ind.ema9) * d > 0:
        t += 2
    parts["trend"] = t if ind.vwap_weighted else t * 20.0 / 15.0
    # 4. momentum (10)
    m = 0.0
    if ind.rsi is not None:
        if (d == 1 and 50 <= ind.rsi <= 72) or (d == -1 and 28 <= ind.rsi <= 50):
            m += 4
        elif setup.kind in ("REJECTION", "RETEST") and ((d == 1 and ind.rsi < 50) or (d == -1 and ind.rsi > 50)):
            m += 2                                  # reversal off an extreme
        if ind.rsi_prev is not None and (ind.rsi - ind.rsi_prev) * d > 0:
            m += 2
    if ind.macd_hist is not None:
        if ind.macd_hist * d > 0:
            m += 2
        if ind.macd_hist_prev is not None and (ind.macd_hist - ind.macd_hist_prev) * d > 0:
            m += 2
    if ind.adx is not None and ind.pdi is not None and ind.mdi is not None and setup.kind in ("BREAKOUT", "TRAP"):
        if ind.adx >= 20 and (ind.pdi - ind.mdi) * d > 0:
            m = min(10.0, m + 2)
            reasons.append(f"ADX {ind.adx:.0f} trending")
    parts["momentum"] = min(10.0, m)
    # 5. option flow (25)
    f = 0.0
    chain = snap.chain
    step = chain.step()
    hold = setup.kind in ("REJECTION", "RETEST", "PULLBACK")
    # hold trades want writers DEFENDING the zone; breaks want writers on the zone LEAVING
    defend_kind = ("PE" if d == 1 else "CE") if hold else ("CE" if d == 1 else "PE")
    # call writers sit at / above a zone, put writers at / below it
    lo, hi = (L - step / 2, L + step) if defend_kind == "CE" else (L - step, L + step / 2)
    near = [k for k in chain.strikes() if lo <= k <= hi]
    lvl_legs = [chain.leg(k, defend_kind) for k in near if chain.leg(k, defend_kind)]
    if lvl_legs:
        lg = max(lvl_legs, key=lambda x: x.oi)
        day, recent = oi_change(lg, st.hist, now, cfg.flow_lookback_s)
        side_max = max((l.oi for (k, tt), l in chain.legs.items() if tt == defend_kind), default=0) or 1.0
        share = lg.oi / side_max
        sign = 1 if hold else -1             # hold: OI rising is good; break: OI falling is good
        comp = []
        if day is not None and lg.prev_oi:
            comp.append(scale(sign * day / max(lg.prev_oi, 1.0), -0.05, 0.25))
        if recent is not None and lg.oi:
            comp.append(scale(sign * recent / lg.oi, -0.005, 0.03))
        lvl = sum(comp) / len(comp) if comp else 0.5
        wall = share if hold else 1 - share
        f += 6 * lvl + 4 * wall
        chg = recent if recent is not None else day
        verb = "writing" if (chg or 0) > 0 else "unwinding" if (chg or 0) < 0 else "flat"
        reasons.append(f"{defend_kind} {int(lg.strike)} OI {lg.oi / 1e5:.1f}L ({share:.0%} of max)"
                       + (f", {verb} {chg / 1e5:+.2f}L" if chg is not None else ""))
        if hold and share >= 0.6:
            reasons.append(f"{defend_kind} wall at the zone")
        if not hold and share >= 0.7 and (chg or 0) > 0:
            risks.append(f"{defend_kind} writers still adding at the zone ({share:.0%} of max OI)")
    else:
        f += 5
    if flow:
        vals = [x for x in (flow.net_day, flow.net_recent) if x is not None]
        net = (0.4 * flow.net_day + 0.6 * flow.net_recent) if len(vals) == 2 else (vals[0] if vals else None)
        if net is not None:
            f += 8 * scale(net * d, -0.2, 0.6)
            if net * d > 0.2:
                reasons.append(f"ATM OI flow {'bullish (puts written)' if d == 1 else 'bearish (calls written)'} {net:+.2f}")
            elif net * d < -0.2:
                risks.append(f"ATM OI flow against ({net:+.2f})")
        else:
            f += 4
        if flow.pcr_oi:
            f += 3 * (scale(flow.pcr_oi, 0.7, 1.2) if d == 1 else scale(flow.pcr_oi, 1.3, 0.8))
    own = chain.leg(op.strike, op.kind)
    opp = chain.leg(op.strike, "PE" if op.kind == "CE" else "CE")
    b_own, b_opp = leg_buildup(own, st.hist, now, cfg), leg_buildup(opp, st.hist, now, cfg)
    if b_own in ("long build-up", "short covering"):
        f += 2
    if b_opp in ("short build-up", "long unwinding"):
        f += 2
    if "n/a" in (b_own, b_opp):
        f += 1
    if b_own in ("short build-up", "long unwinding"):
        risks.append(f"{op.kind} {int(op.strike)} shows {b_own}")
    parts["flow"] = min(25.0, f)
    # 6. room (10)
    parts["room"] = 10 * scale(rr1, cfg.min_rr, 3.0)

    # penalties
    pen = 0.0
    anchor, aname = (ind.vwap, "VWAP") if ind.vwap_weighted else (ind.ema20 or ind.close, "EMA20")
    stretch = (c.close - anchor) * d / atr
    if setup.kind in ("BREAKOUT", "TRAP") and stretch > 2.5:
        pen += 8
        risks.append(f"stretched {stretch:.1f} ATR from {aname}")
    if ind.rsi is not None and setup.kind == "BREAKOUT" and ((d == 1 and ind.rsi > 78) or (d == -1 and ind.rsi < 22)):
        pen += 5
        risks.append(f"RSI {ind.rsi:.0f} extreme")
    if op.book_ratio is not None and op.book_ratio < 0.6:
        pen += 3
        risks.append(f"{op.kind} book heavy on the offer (buy/sell {op.book_ratio:.2f})")
    avg_iv = (flow.avg_iv_ce if op.kind == "CE" else flow.avg_iv_pe) if flow else 0.0
    if avg_iv and op.iv > 1.3 * avg_iv:
        pen += 3
        risks.append(f"IV {op.iv:.1f} rich vs ATM avg {avg_iv:.1f}")
    if op.spread_pct > cfg.max_spread_pct * 0.66:
        pen += 2
    if snap.chain.expiry and snap.chain.expiry[:10] == now.date().isoformat():
        if now.time() >= dtime(13, 30):
            pen += 5
            risks.append("expiry day afternoon: fast theta / gamma")
        else:
            risks.append("expiry day: premium moves fast")
    parts["penalty"] = -pen
    if flow and flow.net_recent is None:
        risks.append(f"recent OI flow unknown (engine has < {cfg.flow_lookback_s // 60} min of chain history)")
    score = max(0.0, min(100.0, sum(parts.values())))

    # ---- premium plan under price, time and IV change; costs and slippage (mandatory gate)
    qty = lot * cfg.lots
    op.sl = premium_at(op, stop - entry, d, minutes=10)
    op.t1 = premium_at(op, t1 - entry, d, minutes=15)
    op.t2 = premium_at(op, t2 - entry, d, minutes=30)
    loss_pct = (op.entry - op.sl) / op.entry * 100.0
    if loss_pct > cfg.max_premium_loss_pct:
        return None, f"premium risk {loss_pct:.0f}% to the structural stop > {cfg.max_premium_loss_pct:.0f}%", score, "risk"
    slip = max(op.ask - op.bid, 0.05)                      # exits sell at the bid
    sl_exit = max(0.05, op.sl - op.vega * cfg.iv_stress_pts - slip)
    t1_exit = op.t1 - slip
    loss_rs = (op.entry - sl_exit) * qty + round_trip_costs(op.entry, sl_exit, qty, snap.index, cfg)
    costs_t1 = round_trip_costs(op.entry, t1_exit, qty, snap.index, cfg)
    gain_rs = (t1_exit - op.entry) * qty - costs_t1
    net_rr = gain_rs / loss_rs if loss_rs > 0 else 0.0
    if net_rr < cfg.min_net_rr:
        return None, (f"after costs/slippage T1 pays Rs {gain_rs:,.0f} vs Rs {loss_rs:,.0f} risk "
                      f"({net_rr:.2f} < {cfg.min_net_rr})"), score, "room"
    need = cfg.min_score + window.extra_score
    if score < need:
        return None, f"score {score:.0f} < {need:.0f} ({window.name})", score, "score"
    lo_e, hi_e = sorted((c.close - d * 0.15 * atr, c.close + d * cfg.chase_atr * atr))
    sig = Signal(
        index=snap.index, time=c.ts.strftime("%H:%M"), expiry=chain.expiry, setup=setup.kind,
        side="CALL" if d == 1 else "PUT", zone=setup.zone.label, zone_price=round(L, 2),
        confluence=setup.zone.confluence, score=round(score, 1), grade=grade(score),
        entry=round(entry, 2), stop=round(stop, 2), target1=round(t1, 2), target2=round(t2, 2),
        t1_label=t1_label, t2_label=t2_label, rr1=round(rr1, 2), rr2=round(rr2, 2),
        option={k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(op).items()},
        parts={k: round(v, 1) for k, v in parts.items()}, reasons=reasons, risks=risks,
        lot_size=lot, lots=cfg.lots,
        regime=regime.state if regime else "", regime_why="; ".join(regime.reasons) if regime else "",
        window=window.name, trigger_ts=c.ts.isoformat(), entry_lo=round(lo_e, 2), entry_hi=round(hi_e, 2),
        next_level=f"{t1_label} ~{t1 + d * 0.1 * atr:.1f}", lot_source=lot_src, costs_rs=round(costs_t1, 2),
        max_loss_rs=round(loss_rs, 2), reward_rs=round(gain_rs, 2), net_rr=round(net_rr, 2),
    )
    return sig, "", score, ""


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

STATE_VERSION = 1
APPROACHING, TESTING, CONFIRMED, ALERTED, INVALIDATED, EXPIRED = (
    "APPROACHING", "TESTING", "CONFIRMED", "ALERTED", "INVALIDATED", "EXPIRED")


@dataclass
class Trade:
    sig: Signal
    stop: float
    t1_hit: bool = False
    open: bool = True


@dataclass
class IndexState:
    hist: ChainHistory = field(default_factory=ChainHistory)
    last_candle: datetime | None = None
    broken: list = field(default_factory=list)            # [(zone price, side, break candle ts)]
    cooldown: list = field(default_factory=list)          # [(zone price, side, until)] after a stop-out
    alerts: list = field(default_factory=list)            # [{price, side, kind, trigger, at}]
    tracks: dict = field(default_factory=dict)            # lifecycle of zones / pullbacks in play
    trade: Trade | None = None
    signals: list = field(default_factory=list)
    chain_fp: tuple | None = None
    chain_fp_since: datetime | None = None
    depth_fp: tuple | None = None
    depth_fp_since: datetime | None = None
    health_text: str = ""
    regime_text: str = ""
    stale_warned: bool = False


def signal_from_dict(d: dict) -> Signal:
    names = {f for f in Signal.__dataclass_fields__}
    return Signal(**{k: v for k, v in d.items() if k in names})


class Engine:
    def __init__(self, cfg: Config, indices: Iterable[str] = INDICES, journal: Path | None = None,
                 sound: bool = True, explain: bool = False, out=None, audit: Path | None = None,
                 state_path: Path | None = None, keep_audit: bool = False):
        self.cfg = cfg
        self.indices = list(indices)
        self.journal = journal
        self.audit_path = audit
        self.state_path = state_path
        self.sound = sound
        self.explain = explain
        self.out = out or sys.stdout
        self.keep_audit = keep_audit
        self.audit_rows: list[dict] = []
        self.state = {i: IndexState() for i in self.indices}
        self.session: date | None = None
        self.feed_text = ""
        self.dirty = False

    # -- output ---------------------------------------------------------------
    def say(self, s: str = "") -> None:
        print(s, file=self.out)
        try:
            self.out.flush()
        except Exception:
            pass

    def _append(self, path: Path | None, row: dict) -> None:
        if not path:
            return
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError as exc:
            self.say(f"write to {path} failed: {exc}")

    def log(self, event: str, data: dict) -> None:
        self._append(self.journal, {"event": event, "at": datetime.now(IST).isoformat(timespec="seconds"), **data})

    def audit(self, event: str, index: str, now: datetime, reason: str = "", **ctx: Any) -> None:
        row = {"event": event, "index": index, "at": now.isoformat(timespec="seconds"), "reason": reason, **ctx}
        if self.keep_audit:
            self.audit_rows.append(row)
        self._append(self.audit_path, row)

    # -- persistence ----------------------------------------------------------
    def ensure_session(self, day: date) -> None:
        if self.session == day:
            return
        if self.session is not None:                      # new trading day: nothing carries over
            self.state = {i: IndexState() for i in self.indices}
        self.session = day
        if self.state_path and self.state_path.exists():
            try:
                self.load_state(json.loads(self.state_path.read_text(encoding="utf-8")), day)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.say(f"state file {self.state_path} unreadable ({exc}); starting clean")

    def load_state(self, data: dict, day: date) -> None:
        if data.get("version") != STATE_VERSION or data.get("date") != day.isoformat():
            return
        for name, d in (data.get("indices") or {}).items():
            if name not in self.state:
                continue
            st = self.state[name]
            st.last_candle = parse_ts(d.get("last_candle"))
            st.broken = [(p, s, parse_ts(t)) for p, s, t in d.get("broken", [])]
            st.cooldown = [(p, s, parse_ts(t)) for p, s, t in d.get("cooldown", [])]
            st.alerts = list(d.get("alerts", []))
            st.tracks = dict(d.get("tracks", {}))
            tr = d.get("trade")
            if tr:
                st.trade = Trade(signal_from_dict(tr["sig"]), tr["stop"], tr.get("t1_hit", False), tr.get("open", True))
        self.say(f"restored state from {self.state_path} ({day})")

    def save_state(self) -> None:
        if not self.state_path or not self.dirty or self.session is None:
            return
        data = {"version": STATE_VERSION, "date": self.session.isoformat(), "indices": {}}
        for name, st in self.state.items():
            data["indices"][name] = {
                "last_candle": st.last_candle.isoformat() if st.last_candle else None,
                "broken": [(p, s, t.isoformat()) for p, s, t in st.broken],
                "cooldown": [(p, s, t.isoformat()) for p, s, t in st.cooldown],
                "alerts": st.alerts, "tracks": st.tracks,
                "trade": {"sig": asdict(st.trade.sig), "stop": st.trade.stop, "t1_hit": st.trade.t1_hit,
                          "open": st.trade.open} if st.trade else None,
            }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
            tmp.replace(self.state_path)
            self.dirty = False
        except OSError as exc:
            self.say(f"state save failed: {exc}")

    # -- one fetch ------------------------------------------------------------
    def process(self, payload: Any, now: datetime, live: bool = True) -> list[Signal]:
        now = now.astimezone(IST)
        self.ensure_session(now.date())
        blocks = index_blocks(payload)
        fired: list[Signal] = []
        if not blocks:
            text = "FEED INVALID: response has no NIFTY / BANKNIFTY / SENSEX blocks"
            if text != self.feed_text:
                self.say(f"{now:%H:%M:%S} {text}")
                self.audit("feed_invalid", "ALL", now, text)
            self.feed_text = text
            return fired
        if self.feed_text:
            self.say(f"{now:%H:%M:%S} feed OK again")
            self.feed_text = ""
        top_open = pick(payload, "market_open", "is_market_open") if isinstance(payload, dict) else None
        for name in self.indices:
            st = self.state[name]
            block = blocks.get(name)
            if block is None:
                text = f"BLOCKED -- {name}: index block missing from payload"
                if text != st.health_text:
                    self.say(f"{now:%H:%M:%S} DATA {name}: {text}")
                    self.audit("data_health", name, now, text)
                    st.health_text = text
                self.manage(None, st, now, None, name)
                continue
            try:
                snap = parse_snapshot(name, block, now.date())
            except Exception as exc:                        # a bad block must not stop the others
                self.say(f"{name}: could not parse ({exc!r})")
                self.audit("data_health", name, now, f"parse error {exc!r}")
                continue
            sig = self.process_index(snap, now, live, top_open)
            if sig:
                fired.append(sig)
        self.save_state()
        return fired

    def process_index(self, snap: Snapshot, now: datetime, live: bool, top_open: Any = None) -> Signal | None:
        cfg, st, name = self.cfg, self.state[snap.index], snap.index
        fp = chain_fingerprint(snap)
        if fp is None or fp != st.chain_fp:
            st.chain_fp, st.chain_fp_since = fp, (now if fp is not None else None)
        dfp = depth_fingerprint(snap)
        if dfp is None or dfp != st.depth_fp:
            st.depth_fp, st.depth_fp_since = dfp, (now if dfp is not None else None)
        health = assess(snap, now, live, st, cfg, top_open)
        text = health.summary()
        if text != st.health_text:
            self.say(f"{now:%H:%M:%S} DATA {name}: {text}")
            self.audit("data_health", name, now, text, trend_issues=health.trend, trade_issues=health.trade,
                       warnings=health.warn)
            st.health_text = text
        if snap.chain and not any("option_chain" in x for x in health.trade):
            st.hist.add(now, snap.chain)                   # only fresh, ready chains feed OI-change history
        self.manage(snap, st, now, health)
        cs = snap.candles
        if len(cs) < 2 or cs[-1].ts == st.last_candle:
            return None
        first_seen = st.last_candle is None
        st.last_candle = cs[-1].ts
        self.dirty = True
        window = session_window(now)
        ind = compute_indicators(cs)
        regime = classify_regime(cs, cfg) if health.ok_trend else Regime(DATA_INVALID, list(health.trend))
        spot = live_spot(snap)
        zones = build_zones(snap.levels, spot or ind.close, ind.atr, cfg)
        flow = chain_flow(snap.chain, spot, st.hist, now, cfg) if snap.chain else None
        self.board(snap, ind, zones, flow, spot, regime, window)
        setups = detect_setups(cs, zones, ind, st.broken, now, cfg)
        pb = detect_pullback(cs, regime, zones, ind, cfg) if regime.direction else None
        if pb:
            setups.append(pb)
        record_breaks(cs, zones, ind, st.broken, cfg)
        self.update_tracks(snap, st, cs, zones, ind, setups, window, now)

        candidates: list[tuple[Setup, Signal, str]] = []
        for s in setups:
            key = self.track_key(s)
            ctx = self.context(snap, ind, regime, window, s)
            self.audit("detected", name, now, s.note, **ctx)
            event, why = self.gate(snap, st, s, regime, window, health, live, first_seen, now, ind)
            sig = None
            if not event:
                sig, why, sc, cat = evaluate(snap, st, s, ind, zones, flow, now, cfg, regime, window)
                if sig is None:
                    event = f"rejected_{cat}"
                    ctx["score"] = round(sc, 1)
            if event:
                self.audit(event, name, now, why, **ctx)
                self.finish_track(st, key, INVALIDATED, f"{event}: {why}", now, name)
                line = f"   . {s.kind:9} {'CALL' if s.side == 1 else 'PUT ':4} @ {s.zone.label} {s.zone.price:.1f} -> {event}: {why}"
                if self.explain:
                    self.say(line)
                elif s.side == regime.direction and event not in ("duplicate", "rejected_open_trade"):
                    self.say(line)                          # trend-aligned near-misses stay visible
                continue
            candidates.append((s, sig, key))
            if self.explain:
                self.say(f"   . {s.kind:9} {'CALL' if s.side == 1 else 'PUT ':4} @ {s.zone.label} "
                         f"{s.zone.price:.1f} -> all gates passed, score {sig.score:.0f}")
        if not candidates:
            return None
        candidates.sort(key=lambda x: -x[1].score)
        s, sig, key = candidates[0]
        for s2, sig2, key2 in candidates[1:]:
            self.audit("superseded", name, now, f"same candle as better {s.kind} (score {sig.score:.0f})",
                       **self.context(snap, ind, regime, window, s2))
            self.finish_track(st, key2, INVALIDATED, "superseded by a better setup on the same candle", now, name)
        st.alerts.append({"price": round(s.zone.price, 2), "side": s.side, "kind": s.kind,
                          "trigger": cs[-1].ts.isoformat(), "at": now.isoformat()})
        st.trade = Trade(sig, sig.stop)
        st.stale_warned = False
        st.signals.append(sig)
        self.finish_track(st, key, ALERTED, f"score {sig.score:.0f}", now, name)
        self.audit("alerted", name, now, f"{sig.setup} {sig.side} score {sig.score:.0f}", signal=asdict(sig))
        self.print_signal(sig)
        self.log("signal", asdict(sig))
        beep(self.sound)
        self.dirty = True
        return sig

    # -- mandatory gates (independent of the score) ---------------------------
    def gate(self, snap: Snapshot, st: IndexState, s: Setup, regime: Regime, window: Window, health: Health,
             live: bool, first_seen: bool, now: datetime, ind: Indicators) -> tuple[str, str]:
        cfg, cs = self.cfg, snap.candles
        side = "CALL" if s.side == 1 else "PUT"
        if not health.ok_trend:
            return "suppressed_data", "; ".join(health.trend)
        if regime.direction == 0:
            return "rejected_trend", f"regime {regime.state}: " + "; ".join(regime.reasons)
        if s.side != regime.direction:
            return "rejected_trend", f"counter-trend {side} in a {regime.state} regime"
        if not health.ok_trade:
            return "suppressed_data", "; ".join(health.trade)
        if live and first_seen and (now - (cs[-1].ts + timedelta(minutes=1))).total_seconds() > 60:
            return "suppressed_data", "startup: trigger candle closed before this process saw it"
        if not window.entries:
            return "rejected_window", f"{window.name}: no new entries"
        if window.kinds and s.kind not in window.kinds:
            return "rejected_window", f"{window.name} allows only {'/'.join(window.kinds)}"
        if st.trade and st.trade.open:
            return "rejected_open_trade", f"{snap.index} {st.trade.sig.side} from {st.trade.sig.time} still open"
        trig = cs[-1].ts.isoformat()
        tol = 0.3 * ind.atr
        for a in st.alerts:
            if a["trigger"] == trig:
                return "duplicate", f"already alerted on the {cs[-1].ts:%H:%M} candle"
            at = parse_ts(a["at"])
            if a["side"] == s.side and abs(a["price"] - s.zone.price) <= tol and at and \
                    (now - at).total_seconds() < cfg.cooldown_min * 60:
                return "duplicate", f"{side} at {a['price']:.1f} already alerted at {at:%H:%M} (cooldown {cfg.cooldown_min}m)"
        for p, sd, until in st.cooldown:
            if sd == s.side and abs(p - s.zone.price) <= tol and until and now < until:
                return "duplicate", f"zone {p:.1f} stopped out earlier; blocked until {until:%H:%M}"
        return "", ""

    # -- lifecycle: APPROACHING -> TESTING -> CONFIRMED -> ALERTED / INVALIDATED / EXPIRED
    @staticmethod
    def track_key(s: Setup) -> str:
        return (f"PB{s.side:+d}@{s.zone.price:.1f}" if "structure" in s.zone.kinds else f"Z@{s.zone.price:.1f}")

    def set_track(self, st: IndexState, key: str, state: str, price: float, label: str, now: datetime,
                  window: Window, name: str, reason: str = "") -> None:
        old = st.tracks.get(key)
        if old and old["state"] == state:
            return
        st.tracks[key] = {"state": state, "price": round(price, 2), "label": label,
                          "since": old["since"] if old else now.isoformat(), "window": window.name}
        self.audit("state", name, now, reason, key=key, state=state, previous=old["state"] if old else None,
                   zone=label, price=round(price, 2))
        self.dirty = True

    def finish_track(self, st: IndexState, key: str, state: str, reason: str, now: datetime, name: str) -> None:
        old = st.tracks.pop(key, None)
        self.audit("state", name, now, reason, key=key, state=state, previous=old["state"] if old else None)
        self.dirty = True

    def update_tracks(self, snap: Snapshot, st: IndexState, cs: list[Candle], zones: list[Zone], ind: Indicators,
                      setups: list[Setup], window: Window, now: datetime) -> None:
        cfg, c, name = self.cfg, cs[-1], snap.index
        tol = max(cfg.touch_atr * ind.atr, c.close * cfg.touch_pct / 100.0)
        for key, t in list(st.tracks.items()):
            if t["state"] not in (APPROACHING, TESTING):
                continue
            since = parse_ts(t["since"])
            if t["window"] != window.name:
                self.finish_track(st, key, EXPIRED, f"window changed {t['window']} -> {window.name}", now, name)
            elif since and (now - since).total_seconds() > cfg.setup_expiry_min * 60:
                self.finish_track(st, key, EXPIRED, f"not confirmed within {cfg.setup_expiry_min} min", now, name)
            elif abs(t["price"] - c.close) > cfg.approach_atr * 1.5 * ind.atr:
                self.finish_track(st, key, EXPIRED, "price moved away", now, name)
        for z in zones:
            dist = abs(z.price - c.close)
            if dist > cfg.approach_atr * ind.atr:
                continue
            key = f"Z@{z.price:.1f}"
            touched = c.low - tol <= z.price <= c.high + tol
            toward = (z.price - c.close) * ind.speed > 0
            cur = st.tracks.get(key, {}).get("state")
            if touched:
                self.set_track(st, key, TESTING, z.price, z.label, now, window, name, "candle traded into the zone")
            elif toward and cur is None:
                self.set_track(st, key, APPROACHING, z.price, z.label, now, window, name,
                               f"{dist:.1f} pts away, moving {'up' if ind.speed > 0 else 'down'}")
        for s in setups:
            self.set_track(st, self.track_key(s), CONFIRMED, s.zone.price, s.zone.label, now, window, name,
                           f"{s.kind} {'CALL' if s.side == 1 else 'PUT'}: {s.note}")

    def context(self, snap: Snapshot, ind: Indicators, regime: Regime, window: Window, s: Setup | None) -> dict:
        c = snap.candles[-1]
        m = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in regime.m.items() if k != "note5"}
        ctx = {"candle": {"ts": c.ts.isoformat(), "o": c.open, "h": c.high, "l": c.low, "c": c.close},
               "spot": live_spot(snap), "regime": regime.state, "regime_why": regime.reasons, "metrics": m,
               "window": window.name, "atr": round(ind.atr, 2)}
        if s:
            ctx.update(setup=s.kind, side="CALL" if s.side == 1 else "PUT", zone=s.zone.label,
                       zone_price=round(s.zone.price, 2))
        return ctx

    # -- trade management (alerts only; nothing is sent to a broker) ----------
    def manage(self, snap: Snapshot | None, st: IndexState, now: datetime, health: Health | None,
               name: str = "") -> None:
        tr = st.trade
        if not tr or not tr.open:
            return
        s = tr.sig
        d = 1 if s.side == "CALL" else -1
        px = live_spot(snap) if snap else 0.0
        fresh = bool(snap and health and health.ok_trend and
                     not any("option_chain" in x or "depth" in x for x in health.trade))
        prem = None
        if fresh:
            bk = snap.depth.get((s.option["strike"], s.option["kind"]))
            leg = snap.chain.leg(s.option["strike"], s.option["kind"]) if snap.chain else None
            prem = (bk.bids[0][0] if bk and bk.bids else None) or (leg.bid if leg and leg.bid > 0 else None)
        tag = f"{s.index} {int(s.option['strike'])}{s.option['kind']}"
        event = None
        if now.time() >= self.cfg.square_off:
            event, tr.open = f"HARD EXIT {self.cfg.square_off:%H:%M}", False
        elif px <= 0 or not fresh:
            if not st.stale_warned:
                self.say(f"\n!!! {now:%H:%M:%S} {tag} {s.side} is open but live data is not usable "
                         f"({st.health_text}). Manage it manually; stop {tr.stop:.1f}.\n")
                self.audit("trade_blind", s.index, now, st.health_text)
                st.stale_warned = True
            return
        elif (px - tr.stop) * d <= 0:
            event, tr.open = ("STOP (breakeven)" if tr.t1_hit else "STOP LOSS"), False
        elif prem is not None and not tr.t1_hit and prem <= s.option["sl"]:
            event, tr.open = "PREMIUM STOP (option fell to its planned stop before the index did)", False
        elif (px - s.target2) * d >= 0:
            event, tr.open = "TARGET 2", False
        elif not tr.t1_hit and (px - s.target1) * d >= 0:
            event = "TARGET 1 -> stop moved to entry"
            tr.t1_hit, tr.stop = True, s.entry
        st.stale_warned = False
        if not event:
            return
        self.dirty = True
        qty = s.lot_size * s.lots
        pnl = None
        if prem is not None and not tr.open:
            pnl = (prem - s.option["entry"]) * qty - round_trip_costs(s.option["entry"], prem, qty, s.index, self.cfg)
        prem_txt = (f" | sell at bid {prem:.2f} (entry {s.option['entry']:.2f})" if prem is not None
                    else " | option bid unavailable: exit at market")
        pnl_txt = f" | net P&L ~Rs {pnl:,.0f} after costs" if pnl is not None else ""
        self.say(f"\n>>> {now:%H:%M:%S} {tag} {s.side}: {event} at index {px:.1f}{prem_txt}{pnl_txt}\n")
        self.log("trade", {"index": s.index, "signal_time": s.time, "event": event, "index_price": px,
                           "premium": prem, "entry_premium": s.option["entry"], "net_pnl": pnl})
        self.audit("trade", s.index, now, event, index_price=px, premium=prem, net_pnl=pnl)
        if not tr.open and event == "STOP LOSS":        # a failed zone stays blocked for 30 minutes
            st.cooldown.append((s.zone_price, d, now + timedelta(minutes=30)))
        beep(self.sound and event.startswith("TARGET"))

    # -- printing -------------------------------------------------------------
    def board(self, snap: Snapshot, ind: Indicators, zones: list[Zone], flow: Flow | None, spot: float,
              regime: Regime, window: Window) -> None:
        cs = snap.candles
        px = spot or ind.close
        e = (f"EMA9 {ind.ema9:.1f} {'>' if ind.ema9 > ind.ema20 else '<'} EMA20 {ind.ema20:.1f}"
             if ind.ema9 and ind.ema20 else "EMA warm-up")
        vw = f"VWAP {ind.vwap:.1f}" if ind.vwap_weighted else "VWAP n/a (index volume 0)"
        adx = f"ADX {ind.adx:.0f}" if ind.adx is not None else "ADX --"
        pcr = f"PCR {flow.pcr_oi:.2f}" if flow and flow.pcr_oi else ""
        below = sorted((z for z in zones if z.price < px), key=lambda z: px - z.price)
        above = sorted((z for z in zones if z.price >= px), key=lambda z: z.price - px)
        fmt = lambda z: f"{z.label} {z.price:.1f} ({abs(z.price - px):.1f}p/{abs(z.price - px) / ind.atr:.1f}ATR)" if z else "--"
        self.say(f"{cs[-1].ts:%H:%M} {snap.index:9} {px:10.2f} | {regime.state:12} | {window.name:14} | {e} {adx} "
                 f"ATR {ind.atr:.1f} {vw} {pcr} | S {fmt(below[0] if below else None)} | "
                 f"R {fmt(above[0] if above else None)}")
        st = self.state[snap.index]
        rtext = f"{regime.state}: {'; '.join(regime.reasons)}"
        if rtext != st.regime_text:
            self.say(f"      regime {rtext}")
            self.audit("regime", snap.index, cs[-1].ts + timedelta(minutes=1), rtext, state=regime.state)
            st.regime_text = rtext
        sp = ind.speed
        target = (above[0] if above else None) if sp > 0 else (below[0] if below else None)
        if target and abs(target.price - px) <= self.cfg.approach_atr * ind.atr and abs(sp) > 0.05 * ind.atr:
            dist = abs(target.price - px)
            res = target.price >= px
            hint = ""
            ch = snap.chain
            if ch:
                k = ch.atm(target.price)
                ce, pe = ch.leg(k, "CE"), ch.leg(k, "PE")
                if ce and pe:
                    hint = f" | {int(k)} CE OI {ce.oi / 1e5:.1f}L PE OI {pe.oi / 1e5:.1f}L"
            d = regime.direction
            plan = ("trend BULLISH: only a break & hold (CALL) is tradeable" if d == 1 and res else
                    "trend BULLISH: only a bounce (CALL) is tradeable" if d == 1 else
                    "trend BEARISH: only a rejection (PUT) is tradeable" if d == -1 and res else
                    "trend BEARISH: only a break & hold (PUT) is tradeable" if d == -1 else
                    f"regime {regime.state}: no directional entry")
            self.say(f"      APPROACHING {'^' if res else 'v'} {'resistance' if res else 'support'} "
                     f"{target.label} {target.price:.1f}: {dist:.1f} pts at {abs(sp):.1f} pts/min "
                     f"(~{dist / abs(sp):.0f} min), confluence {target.confluence}{hint} | {plan}")

    def print_signal(self, s: Signal) -> None:
        o = s.option
        lot_qty = s.lot_size * s.lots
        line = "=" * 96
        bk = f"{o['book_ratio']:.2f}x" if o.get("book_ratio") else "n/a"
        oid = f" ({o['oi_day'] / 1e5:+.2f}L vs previous_oi)" if o.get("oi_day") is not None else ""
        self.say(f"\n{line}\n ALERT {s.index} {('BULLISH' if s.side == 'CALL' else 'BEARISH')} -> BUY {s.side}  "
                 f"{int(o['strike'])} {o['kind']}  exp {s.expiry}   {s.setup}   {s.time} ({s.window})\n{line}")
        self.say(f" regime   : {s.regime} -- {s.regime_why}")
        self.say(f" setup    : {s.setup} @ {s.zone} {s.zone_price:.1f} (confluence {s.confluence})")
        self.say(f" entry    : index {s.entry:.1f}; acceptable entry zone {s.entry_lo:.1f} - {s.entry_hi:.1f} "
                 f"(outside it: skip, do not chase)")
        self.say(f" invalid  : index stop {s.stop:.1f} ({abs(s.entry - s.stop):.1f} pts, structural)")
        self.say(f" targets  : T1 {s.target1:.1f} [{s.t1_label}] {s.rr1:.1f}R   T2 {s.target2:.1f} [{s.t2_label}] "
                 f"{s.rr2:.1f}R   next opposing level {s.next_level}")
        self.say(f" option   : est. buy {o['entry']:.2f}{' (walked through depth)' if o['depth_fill'] else ''}  "
                 f"bid {o['bid']:.2f} ask {o['ask']:.2f} | plan SL {o['sl']:.2f}  T1 {o['t1']:.2f}  T2 {o['t2']:.2f}")
        self.say(f" greeks   : delta {o['delta']:+.2f} gamma {o['gamma']:.4f} theta {o['theta']:.1f} vega {o['vega']:.1f} "
                 f"IV {o['iv']:.1f} | OI {o['oi'] / 1e5:.1f}L{oid} vol {o['volume'] / 1e5:.1f}L | spread "
                 f"{o['spread_pct']:.2f}% | book buy/sell {bk}")
        self.say(f" risk     : {s.lots} lot x {s.lot_size} = {lot_qty} qty (lot size: {s.lot_source}) | max loss "
                 f"~Rs {s.max_loss_rs:,.0f} (stop, IV -{self.cfg.iv_stress_pts:g}pt, bid exit, costs) | T1 net "
                 f"~Rs {s.reward_rs:,.0f} | net R:R {s.net_rr:.2f} | costs ~Rs {s.costs_rs:,.0f}")
        self.say(f" score    : {s.score:.0f}/100 [{s.grade}] -- quality rank among gated setups, NOT a win probability")
        self.say("            " + "  ".join(f"{k} {v:+.1f}" for k, v in s.parts.items()))
        for r in s.reasons:
            self.say(f"   +  {r}")
        for r in s.risks:
            self.say(f"   !  {r}")
        self.say(line + "\n")


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

class FeedError(Exception):
    """Transport-level failure: no usable HTTP response (distinct from a response with invalid data)."""


def fetch(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "sr.py", "Accept": "application/json",
                                               "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FeedError(f"transport: {getattr(exc, 'reason', exc)}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FeedError(f"HTTP 200 but body is not JSON ({exc})") from exc


def describe(x: Any, depth: int = 0, key: str = "", max_depth: int = 5) -> list[str]:
    pad = "  " * depth
    if isinstance(x, dict):
        lines = [f"{pad}{key}{{{len(x)} keys}}"]
        if depth < max_depth:
            items = list(x.items())
            numeric_keys = all(re.fullmatch(r"-?\d+(\.\d+)?", str(k)) for k, _ in items) and len(items) > 3
            for k, v in (items[:2] if numeric_keys else items[:40]):
                lines += describe(v, depth + 1, f"{k}: ", max_depth)
            if numeric_keys:
                lines.append(f"{pad}  ... {len(items) - 2} more numeric keys")
        return lines
    if isinstance(x, list):
        lines = [f"{pad}{key}[{len(x)} items]"]
        if x and depth < max_depth:
            lines += describe(x[0], depth + 1, "[0]: ", max_depth)
        return lines
    return [f"{pad}{key}{type(x).__name__} = {str(x)[:60]}"]


def now_ist() -> datetime:
    return datetime.now(IST)


def replay_now(payload: Any) -> datetime:
    """Clock for an offline snapshot without a timestamped name: one minute after its latest candle."""
    latest = None
    for b in index_blocks(payload).values():
        cs = parse_candles(pick(b, "candles_1m", "candles", "1m", default=[]))
        if cs and (latest is None or cs[-1].ts > latest):
            latest = cs[-1].ts
    return (latest + timedelta(seconds=60)) if latest else now_ist()


SNAP_RE = re.compile(r"(\d{8})_(\d{6})")


def snapshot_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    files = [f for f in path.rglob("*") if f.is_file() and (f.name.endswith(".json") or f.name.endswith(".json.gz"))]
    return sorted(files, key=lambda f: (SNAP_RE.search(f.name).groups() if SNAP_RE.search(f.name) else ("", ""), f.name))


def load_snapshot(f: Path) -> Any:
    import gzip
    raw = gzip.decompress(f.read_bytes()) if f.name.endswith(".gz") else f.read_bytes()
    return json.loads(raw.decode("utf-8"))


def snapshot_time(f: Path, payload: Any) -> datetime:
    m = SNAP_RE.search(f.name)
    return datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S").replace(tzinfo=IST) if m else replay_now(payload)


def save_snapshot(folder: Path, now: datetime, payload: Any) -> None:
    import gzip
    day = folder / f"{now:%Y%m%d}"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{now:%Y%m%d_%H%M%S}.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode("utf-8")))


def run_live(eng: Engine, url: str, interval: float, once: bool, record: Path | None,
             record_every: float = 15.0) -> None:
    cfg = eng.cfg
    fails, last_rec, down_since = 0, None, None
    eng.say(f"sr.py | {', '.join(eng.indices)} | {url} | poll {interval:.0f}s | monitoring "
            f"{cfg.monitor_start:%H:%M}-{cfg.monitor_end:%H:%M} IST | entries PRIME 09:30-10:45, GOOD -11:15, "
            f"SELECTIVE 13:30-14:00, SECONDARY -14:30 | hard exit {cfg.square_off:%H:%M} | "
            f"audit {eng.audit_path} | state {eng.state_path}")
    eng.say("Alerts are decision support only; nothing is sent to a broker.")
    while True:
        now = now_ist()
        if not once and now.time() >= cfg.monitor_end:
            eng.say(f"{now:%H:%M:%S} monitoring window over ({cfg.monitor_end:%H:%M} IST); done.")
            return
        if not once and now.time() < cfg.monitor_start:
            wait = (datetime.combine(now.date(), cfg.monitor_start, IST) - now).total_seconds()
            eng.say(f"{now:%H:%M:%S} waiting for {cfg.monitor_start:%H:%M} IST ({wait / 60:.0f} min)")
            time.sleep(min(60.0, wait + 1))
            continue
        try:
            payload = fetch(url)
        except FeedError as exc:
            fails += 1
            down_since = down_since or now
            eng.say(f"{now:%H:%M:%S} FEED FAILURE ({exc}); retry #{fails}. No signals while the feed is down.")
            eng.audit("feed_failure", "ALL", now, str(exc))
            if once:
                return
            time.sleep(min(30.0, interval * 2 ** min(fails, 4)))
            continue
        if fails:
            eng.say(f"{now:%H:%M:%S} feed reconnected after {fails} failure(s), "
                    f"{(now - down_since).total_seconds():.0f}s down; freshness checks decide what is usable")
            eng.audit("feed_recovered", "ALL", now, f"after {fails} failures")
            fails, down_since = 0, None
        if record and (last_rec is None or (now - last_rec).total_seconds() >= record_every):
            try:
                save_snapshot(record, now, payload)
                last_rec = now
            except OSError as exc:
                eng.say(f"record failed: {exc}")
        eng.process(payload, now_ist(), live=True)
        if once:
            return
        time.sleep(max(0.5, interval - (now_ist() - now).total_seconds()))


def run_replay(eng: Engine, path: Path) -> None:
    files = snapshot_files(path)
    eng.say(f"replaying {len(files)} snapshot(s) from {path}")
    for f in files:
        try:
            payload = load_snapshot(f)
        except (OSError, ValueError) as exc:
            eng.say(f"{f.name}: unreadable ({exc})")
            continue
        eng.process(payload, snapshot_time(f, payload), live=False)
    eng.say(f"\n{sum(len(s.signals) for s in eng.state.values())} alert(s).")


def parse_lot_sizes(items: list[str] | None, base: dict) -> dict:
    out = dict(base)
    for it in items or []:
        k, _, v = it.partition("=")
        if _norm_index(k) and v.strip().isdigit():
            out[_norm_index(k)] = int(v)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Trend-gated support/resistance option-signal engine.")
    ap.add_argument("indices", nargs="*", help="NIFTY BANKNIFTY SENSEX (default all)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between fetches (default 3)")
    ap.add_argument("--min-score", type=float, default=Config.min_score)
    ap.add_argument("--min-rr", type=float, default=Config.min_rr)
    ap.add_argument("--min-net-rr", type=float, default=Config.min_net_rr)
    ap.add_argument("--min-candles", type=int, default=Config.min_candles)
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--lot-size", action="append", metavar="INDEX=N",
                    help="lot size you verified from the exchange contract specs, e.g. NIFTY=65 (used only "
                         "when the feed carries no lot_size)")
    ap.add_argument("--no-require-depth", action="store_true", help="allow alerts without market depth")
    ap.add_argument("--once", action="store_true", help="fetch once and exit")
    ap.add_argument("--explain", action="store_true", help="print every setup and why it passed or failed")
    ap.add_argument("--schema", action="store_true", help="print the payload structure and a data-health verdict")
    ap.add_argument("--record", type=Path, help="save fetched payloads (gzip) into DIR/YYYYMMDD/")
    ap.add_argument("--record-every", type=float, default=15.0, help="seconds between recorded snapshots")
    ap.add_argument("--replay", type=Path, help="run over saved snapshot file(s) / folder")
    ap.add_argument("--journal", type=Path, default=Path("sr_signals.jsonl"))
    ap.add_argument("--audit", type=Path, default=Path("sr_audit.jsonl"), help="every decision with its reason")
    ap.add_argument("--state", type=Path, default=Path("sr_state.json"), help="restart-recovery state (live only)")
    ap.add_argument("--no-beep", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_sr")
        return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1
    cfg = Config(min_score=a.min_score, min_rr=a.min_rr, min_net_rr=a.min_net_rr, min_candles=a.min_candles,
                 lots=max(1, a.lots), require_depth=not a.no_require_depth)
    cfg.lot_size = parse_lot_sizes(a.lot_size, cfg.lot_size)
    if a.schema:
        try:
            payload = load_snapshot(a.replay) if a.replay and a.replay.is_file() else fetch(a.url)
        except (FeedError, OSError, ValueError) as exc:
            print(f"FEED FAILURE: {exc}")
            return 1
        print("\n".join(describe(payload)))
        found = index_blocks(payload)
        now = now_ist() if not a.replay else snapshot_time(a.replay, payload)
        print(f"\nHTTP/JSON OK. market_open (top level) = {pick(payload, 'market_open') if isinstance(payload, dict) else None}")
        print(f"indices found: {sorted(found) or 'NONE'}")
        for n, b in found.items():
            s = parse_snapshot(n, b, now.date())
            ch = s.chain
            print(f"\n{n}: {len(s.candles)} candles, chain "
                  f"{'%d legs exp %s spot %.1f' % (len(ch.legs), ch.expiry, ch.spot) if ch else 'NOT PARSED'} "
                  f"(status {s.meta.get('chain_status')}), depth {len(s.depth)} books (status "
                  f"{s.meta.get('depth_status')}), levels {len(s.levels)}, lot_size in feed {s.meta.get('lot_size')}")
            if ch:
                legs = list(ch.legs.values())
                print(f"   legs with delta {sum(1 for l in legs if l.delta)}, gamma {sum(1 for l in legs if l.gamma)}, "
                      f"theta {sum(1 for l in legs if l.theta)}, iv {sum(1 for l in legs if l.iv)}, bid/ask "
                      f"{sum(1 for l in legs if l.bid > 0 and l.ask > 0)}, previous_oi "
                      f"{sum(1 for l in legs if l.prev_oi is not None)} of {len(legs)}")
            h = assess(s, now, not a.replay, IndexState(), cfg, pick(payload, "market_open") if isinstance(payload, dict) else None)
            print(f"   DATA HEALTH: {h.summary()}")
            for w in h.warn:
                print(f"   warning: {w}")
        return 0

    indices = [x for x in (_norm_index(i) for i in a.indices) if x] or list(INDICES)
    live = not a.replay
    eng = Engine(cfg, indices, journal=a.journal, sound=not a.no_beep, explain=a.explain or a.once,
                 audit=a.audit, state_path=a.state if live else None)
    try:
        if a.replay:
            run_replay(eng, a.replay)
        else:
            run_live(eng, a.url, a.interval, a.once, a.record, a.record_every)
    except KeyboardInterrupt:
        eng.save_state()
        eng.say("\nstopped (state saved).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
