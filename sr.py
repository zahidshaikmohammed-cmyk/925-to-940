"""sr.py -- live support / resistance option-signal engine (NIFTY, BANKNIFTY, SENSEX).

Data: one JSON endpoint (default http://140.245.228.101:10000/public/all.json) carrying,
per index, today's 1-minute candles, the Dhan option chain (+ analytics), 20/5-level
market depth for the 25 strikes nearest spot, and a support/resistance map.

What it does, every poll (default every 3 s):

1. TRACK  -- live price (option-chain underlying_ltp, ~3 s fresh) against every mapped
             level. Levels closer than ~0.15 ATR are merged into one zone; each zone gets
             a strength from its confluence count and source (major > option wall > intraday).
             The board shows which zone price is heading for, the distance in points/ATR,
             the speed (least-squares slope of the last 5 closes) and an ETA.
2. TRIGGER -- only on a COMPLETED 1-minute candle (nothing repaints):
             BREAKOUT   prev close on one side, this close beyond the zone by >= 0.10 ATR,
                        body >= 50% of range, close in the outer 40% of the candle.
             REJECTION  price came from the other side (3 of the prior 5 closes), the candle
                        touched the zone (within max(0.25 ATR, 0.03%)) and closed back away
                        from it with a >= 40% wick or a close beyond the previous candle.
                        A pierce of the zone that is reclaimed is tagged SWEEP.
             RETEST     a zone broken 2-15 minutes ago is retested from the new side and held.
             TRAP       a zone broken 1-15 minutes ago is closed back through (failed break).
3. CONFIRM -- indicators computed here from the raw candles (exact, standard definitions):
             EMA9 / EMA20 / EMA50 (SMA-seeded), 5-minute EMA9 / EMA20, session VWAP with
             +/-1 and 2 sigma bands, Wilder RSI14, Wilder ATR14, Wilder ADX14 with +DI/-DI,
             MACD 12/26/9. Index volume is normally 0, so the VWAP is then equal-weighted
             (a session TWAP of HLC3) and labelled "VWAP=" instead of "VWAP".
             Option flow: OI change at the level strike (writers defending or unwinding),
             net CE vs PE OI change around ATM (day change from previous_oi and the last
             ~5 minutes from this engine's own snapshots), per-contract build-up computed
             over ~3 minutes (long build-up / short build-up / short covering / long
             unwinding), and PCR.
4. SELECT -- the CE (bullish) or PE (bearish) of the nearest expiry with |delta| in 0.40-0.65
             closest to 0.52, a tight bid/ask spread and the most volume. The fill price is
             walked through the depth ladder for your lot count. Premium stop/targets come
             from delta + gamma (dP = delta*dS + 0.5*gamma*dS^2), so they track the index plan.
5. SCORE  -- 0-100: trigger 20, level strength 15, trend 20, momentum 10, option flow 25,
             room to the next zone (R multiple) 10, minus penalties (VWAP stretch, overbought /
             oversold breakouts, wide spread, rich IV, adverse book, expiry-day afternoon).
             A signal prints and beeps only if score >= --min-score (65), the reward to the
             next zone is >= --min-rr (1.2R) and price has not already run away (0.6 ATR).
6. MANAGE -- each signal is tracked: T1 hit -> stop to entry, T2 / SL / 15:15 -> closed.
             Everything goes to sr_signals.jsonl.

Usage:
    python sr.py                       live, all three indices
    python sr.py NIFTY BANKNIFTY       chosen indices only
    python sr.py --once --explain      one fetch: board + every setup and why it passed/failed
    python sr.py --schema              print the structure the endpoint returns (to verify fields)
    python sr.py --record snaps/       save every fetch, then later:
    python sr.py --replay snaps/       run the engine over saved snapshots (offline review)
    python sr.py --lots 2 --min-score 70
    python sr.py --self-test

Signals are decision support, not advice; size every trade to your own risk.
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
    min_score: float = 65.0
    min_rr: float = 1.2               # reward to the first target zone, in R
    min_candles: int = 20             # EMA20 fully seeded before the first signal (09:35)
    first_entry: dtime = dtime(9, 20)
    last_entry: dtime = dtime(15, 0)
    square_off: dtime = dtime(15, 15)
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
    target_gap_atr: float = 0.30      # a zone closer than this to entry is not a target
    approach_atr: float = 1.5         # board WATCH line when a zone is within this
    cooldown_min: int = 15            # same zone + same side not re-signalled for this long
    delta_lo: float = 0.40
    delta_hi: float = 0.65
    target_delta: float = 0.52
    max_spread_pct: float = 1.5
    max_premium_loss_pct: float = 35.0
    flow_strikes: int = 3             # strikes each side of ATM for the net OI flow
    buildup_lookback_s: int = 180
    flow_lookback_s: int = 300
    stale_after_s: int = 180
    lots: int = 1
    lot_size: dict = field(default_factory=lambda: {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20})


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


def parse_snapshot(name: str, block: dict, today: date | None = None) -> Snapshot:
    candles = parse_candles(pick(block, "candles_1m", "candles", "1m", default=[]), today)
    chain = parse_chain(pick(block, "option_chain", "chain", "oc"))
    ltp = num(pick(block, "ltp", "last_price"), None)
    spot = (chain.spot if chain and chain.spot else None) or ltp or (candles[-1].close if candles else 0.0)
    return Snapshot(
        index=name, candles=candles,
        prev_close=num(pick(block, "previous_close", "prev_close"), None),
        today_open=num(pick(block, "today_open", "open"), None),
        ltp=ltp, updated_at=parse_ts(pick(block, "updated_at", "last_updated"), today),
        chain=chain, depth=parse_depth(pick(block, "depth", "market_depth")),
        levels=parse_levels(pick(block, "levels", "sr", "support_resistance"), spot or 0.0),
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
        """Snapshot at least 60% of `seconds` old, closest to `seconds` ago."""
        best = None
        for ts, snap in self.rows:
            age = (t - ts).total_seconds()
            if age >= seconds * 0.6 and (best is None or abs(age - seconds) < best[0]):
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
# Setups, scoring, contract selection
# --------------------------------------------------------------------------

@dataclass
class Setup:
    kind: str                        # BREAKOUT / REJECTION / RETEST / TRAP
    side: int                        # +1 CALL, -1 PUT
    zone: Zone
    quality: float                   # 0..1 candle quality
    note: str


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


def choose_option(chain: Chain, depth: dict, side: int, spot: float, lot_qty: float, cfg: Config,
                  hist: ChainHistory, now: datetime) -> tuple[OptionPick | None, str]:
    kind = "CE" if side == 1 else "PE"
    legs = [l for (k, t), l in chain.legs.items() if t == kind and l.ltp > 0]
    if not legs:
        return None, f"no {kind} quotes"
    have_greeks = any(abs(l.delta) > 0 for l in legs)
    step = chain.step()
    if have_greeks:
        pool = [l for l in legs if cfg.delta_lo <= abs(l.delta) <= cfg.delta_hi]
    else:                               # no greeks: ATM and one strike in the money
        atm = chain.atm(spot)
        pool = [l for l in legs if atm is not None and 0 <= (atm - l.strike) * side <= step]
    pool = [l for l in pool if l.bid > 0 and l.ask >= l.bid and
            (l.spread_pct <= cfg.max_spread_pct or l.ask - l.bid <= 0.30)]
    if not pool:
        return None, f"no liquid {kind} with |delta| {cfg.delta_lo}-{cfg.delta_hi} and spread <= {cfg.max_spread_pct}%"
    vmax = max(l.volume for l in pool) or 1.0
    def rank(l: Leg) -> float:
        d = 1 - abs(abs(l.delta) - cfg.target_delta) / 0.15 if have_greeks else 1 - abs(l.strike - spot) / step
        return 0.5 * d + 0.3 * l.volume / vmax + 0.2 * (1 - min(l.spread_pct, cfg.max_spread_pct) / cfg.max_spread_pct)
    best = max(pool, key=rank)
    bk = depth.get((best.strike, kind))
    fill = bk.fill_price(lot_qty) if bk and bk.asks else None
    bid = bk.bids[0][0] if bk and bk.bids else best.bid
    ask = bk.asks[0][0] if bk and bk.asks else best.ask
    entry = fill or ask
    day, _ = oi_change(best, hist, now, cfg.flow_lookback_s)
    return OptionPick(best.strike, kind, entry, bid, ask, best.ltp, best.delta, best.gamma, best.theta, best.vega,
                      best.iv, best.oi, day, best.volume, best.spread_pct, bk.imbalance() if bk else None,
                      fill is not None), ""


def premium_at(op: OptionPick, d_spot: float, side: int, minutes: float = 0.0) -> float:
    """Premium after an index move of d_spot: delta + gamma (+ theta over `minutes`)."""
    delta = op.delta if op.delta else 0.5 * side
    d = delta * d_spot + 0.5 * op.gamma * d_spot * d_spot + op.theta * minutes / 375.0
    return max(0.05, op.entry + d)


def grade(score: float) -> str:
    return "A+" if score >= 82 else "A" if score >= 72 else "B" if score >= 60 else "C"


def evaluate(snap: Snapshot, st: "IndexState", setup: Setup, ind: Indicators, zones: list[Zone], flow: Flow | None,
             now: datetime, cfg: Config) -> tuple[Signal | None, str, float]:
    """Score one setup. Returns (signal or None, reason if rejected, score)."""
    cs = snap.candles
    c = cs[-1]
    d = setup.side
    atr = ind.atr
    L = setup.zone.price
    spot = live_spot(snap)
    entry = spot if spot > 0 else c.close
    tol = max(cfg.touch_atr * atr, c.close * cfg.touch_pct / 100.0)

    if (entry - c.close) * d > cfg.chase_atr * atr:
        return None, f"move already gone ({(entry - c.close) * d:.1f} pts past the trigger close)", 0
    # ---- stop
    tight_stop = False
    if setup.kind == "BREAKOUT":
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
    if risk > cfg.max_stop_atr * atr:
        return None, f"stop too wide ({risk:.1f} pts = {risk / atr:.1f} ATR)", 0
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
        return None, f"no room: next zone {t1_label} only {rr1:.2f}R away", 0
    if snap.chain is None:
        return None, "no option chain", 0
    lot = cfg.lot_size.get(snap.index, 1)
    op, why = choose_option(snap.chain, snap.depth, d, spot, lot * cfg.lots, cfg, st.hist, now)
    if op is None:
        return None, why, 0

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
    if (c.close - ind.vwap) * d > 0:
        t += 5
        reasons.append(f"{'above' if d == 1 else 'below'} VWAP{'' if ind.vwap_weighted else '='} {ind.vwap:.1f}")
    else:
        risks.append(f"on the wrong side of VWAP{'' if ind.vwap_weighted else '='} {ind.vwap:.1f}")
    if ind.ema9_5m is not None and ind.ema20_5m is not None:
        if (ind.ema9_5m - ind.ema20_5m) * d > 0:
            t += 4
            reasons.append("5m EMA9/20 aligned")
    elif ind.ema9_5m is not None and ind.close_5m is not None and (ind.close_5m - ind.ema9_5m) * d > 0:
        t += 3
    if ind.ema9 is not None and (c.close - ind.ema9) * d > 0:
        t += 2
    parts["trend"] = t
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
    hold = setup.kind in ("REJECTION", "RETEST")
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
    stretch = (c.close - ind.vwap) * d / atr
    if setup.kind in ("BREAKOUT", "TRAP") and stretch > 2.5:
        pen += 8
        risks.append(f"stretched {stretch:.1f} ATR from VWAP")
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
    score = max(0.0, min(100.0, sum(parts.values())))
    if score < cfg.min_score:
        return None, f"score {score:.0f} < {cfg.min_score:.0f}", score

    op.sl = max(premium_at(op, stop - entry, d), op.entry * (1 - cfg.max_premium_loss_pct / 100.0))
    op.t1 = premium_at(op, t1 - entry, d, minutes=15)
    op.t2 = premium_at(op, t2 - entry, d, minutes=30)
    sig = Signal(
        index=snap.index, time=c.ts.strftime("%H:%M"), expiry=chain.expiry, setup=setup.kind,
        side="CALL" if d == 1 else "PUT", zone=setup.zone.label, zone_price=round(L, 2),
        confluence=setup.zone.confluence, score=round(score, 1), grade=grade(score),
        entry=round(entry, 2), stop=round(stop, 2), target1=round(t1, 2), target2=round(t2, 2),
        t1_label=t1_label, t2_label=t2_label, rr1=round(rr1, 2), rr2=round(rr2, 2),
        option={k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(op).items()},
        parts={k: round(v, 1) for k, v in parts.items()}, reasons=reasons, risks=risks,
        lot_size=lot, lots=cfg.lots,
    )
    return sig, "", score


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

def zone_key(price: float, atr: float, side: int) -> tuple[int, int]:
    return round(price / max(atr, 1e-9)), side


@dataclass
class Trade:
    sig: Signal
    stop: float
    key: tuple = ()
    t1_hit: bool = False
    open: bool = True


@dataclass
class IndexState:
    hist: ChainHistory = field(default_factory=ChainHistory)
    last_candle: datetime | None = None
    broken: list = field(default_factory=list)
    cooldown: dict = field(default_factory=dict)          # (rounded zone, side) -> until
    trade: Trade | None = None
    signals: list = field(default_factory=list)


class Engine:
    def __init__(self, cfg: Config, indices: Iterable[str] = INDICES, journal: Path | None = None,
                 sound: bool = True, explain: bool = False, out=None):
        self.cfg = cfg
        self.indices = list(indices)
        self.journal = journal
        self.sound = sound
        self.explain = explain
        self.out = out or sys.stdout
        self.state = {i: IndexState() for i in self.indices}

    def say(self, s: str = "") -> None:
        print(s, file=self.out)
        try:
            self.out.flush()
        except Exception:
            pass

    def log(self, event: str, data: dict) -> None:
        if not self.journal:
            return
        try:
            with self.journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": event, "at": datetime.now(IST).isoformat(timespec="seconds"), **data},
                                    default=str) + "\n")
        except OSError as exc:
            self.say(f"journal write failed: {exc}")

    # -- one fetch ------------------------------------------------------------
    def process(self, payload: Any, now: datetime, live: bool = True) -> list[Signal]:
        blocks = index_blocks(payload)
        fired: list[Signal] = []
        for name in self.indices:
            block = blocks.get(name)
            if block is None:
                continue
            try:
                snap = parse_snapshot(name, block, now.date())
            except Exception as exc:                        # a bad block must not stop the others
                self.say(f"{name}: could not parse ({exc!r})")
                continue
            sig = self.process_index(snap, now, live)
            if sig:
                fired.append(sig)
        return fired

    def process_index(self, snap: Snapshot, now: datetime, live: bool) -> Signal | None:
        cfg, st = self.cfg, self.state[snap.index]
        if snap.chain:
            st.hist.add(now, snap.chain)
        self.manage(snap, st, now)
        cs = snap.candles
        if len(cs) < 2 or cs[-1].ts == st.last_candle:
            return None
        first_seen = st.last_candle is None
        st.last_candle = cs[-1].ts
        ind = compute_indicators(cs)
        spot = live_spot(snap)
        zones = build_zones(snap.levels, spot or ind.close, ind.atr, cfg)
        flow = chain_flow(snap.chain, spot, st.hist, now, cfg) if snap.chain else None
        self.board(snap, ind, zones, flow, spot)
        setups = detect_setups(cs, zones, ind, st.broken, now, cfg)
        record_breaks(cs, zones, ind, st.broken, cfg)

        blocked = self.blocked(snap, cs, now, live, first_seen)
        best: tuple[float, Signal] | None = None
        for s in setups:
            key = zone_key(s.zone.price, ind.atr, s.side)
            sig, why, sc = evaluate(snap, st, s, ind, zones, flow, now, cfg)
            if sig and blocked:
                sig, why = None, blocked
            if sig and st.cooldown.get(key) and st.cooldown[key] > cs[-1].ts:
                sig, why = None, f"cooldown on this zone until {st.cooldown[key]:%H:%M}"
            if self.explain:
                tag = f"PASS {sig.score:.0f}" if sig else f"skip ({why})"
                self.say(f"   . {s.kind:9} {'CALL' if s.side == 1 else 'PUT ':4} @ {s.zone.label} "
                         f"{s.zone.price:.1f} -> {tag}")
            if sig and (best is None or sig.score > best[0]):
                best = (sig.score, sig)
        if not best:
            return None
        sig = best[1]
        key = zone_key(sig.zone_price, ind.atr, 1 if sig.side == "CALL" else -1)
        st.cooldown[key] = cs[-1].ts + timedelta(minutes=cfg.cooldown_min)
        st.trade = Trade(sig, sig.stop, key)
        st.signals.append(sig)
        self.print_signal(sig)
        self.log("signal", asdict(sig))
        beep(self.sound)
        return sig

    def blocked(self, snap: Snapshot, cs: list[Candle], now: datetime, live: bool, first_seen: bool) -> str:
        cfg, st = self.cfg, self.state[snap.index]
        t = cs[-1].ts.time()
        if len(cs) < cfg.min_candles:
            return f"warming up ({len(cs)}/{cfg.min_candles} candles)"
        if not cfg.first_entry <= t <= cfg.last_entry:
            return "outside entry window"
        if st.trade and st.trade.open:
            return f"{snap.index} trade already open"
        if live:
            age = (now - cs[-1].ts).total_seconds()
            if first_seen and age > 180:
                return "startup: last candle is old"
            if age > 60 + cfg.stale_after_s:
                return f"stale candles ({age:.0f}s old)"
        return ""

    # -- trade management -----------------------------------------------------
    def manage(self, snap: Snapshot, st: IndexState, now: datetime) -> None:
        tr = st.trade
        if not tr or not tr.open:
            return
        s = tr.sig
        d = 1 if s.side == "CALL" else -1
        px = live_spot(snap)
        if px <= 0:
            return
        leg = snap.chain.leg(s.option["strike"], s.option["kind"]) if snap.chain else None
        bk = snap.depth.get((s.option["strike"], s.option["kind"]))
        prem = (bk.bids[0][0] if bk and bk.bids else None) or (leg.bid if leg and leg.bid else leg.ltp if leg else None)
        tag = f"{snap.index} {int(s.option['strike'])}{s.option['kind']}"
        prem_txt = f" | premium {prem:.2f} (entry {s.option['entry']:.2f}, {prem - s.option['entry']:+.2f})" if prem else ""
        event = None
        if (px - tr.stop) * d <= 0:
            event = "STOP (breakeven)" if tr.t1_hit else "STOP LOSS"
            tr.open = False
        elif (px - s.target2) * d >= 0:
            event = "TARGET 2"
            tr.open = False
        elif not tr.t1_hit and (px - s.target1) * d >= 0:
            event = "TARGET 1 -> stop moved to entry"
            tr.t1_hit = True
            tr.stop = s.entry
        elif now.time() >= self.cfg.square_off:
            event = "TIME EXIT 15:15"
            tr.open = False
        if event:
            self.say(f"\n>>> {now:%H:%M:%S} {tag} {s.side}: {event} at index {px:.1f}{prem_txt}\n")
            self.log("trade", {"index": snap.index, "signal_time": s.time, "event": event, "index_price": px,
                               "premium": prem, "entry_premium": s.option["entry"]})
            if not tr.open and event == "STOP LOSS":    # a failed zone stays blocked for 30 minutes
                st.cooldown[tr.key] = max(st.cooldown.get(tr.key, now), now + timedelta(minutes=30))
            beep(self.sound and event.startswith("TARGET"))

    # -- printing -------------------------------------------------------------
    def board(self, snap: Snapshot, ind: Indicators, zones: list[Zone], flow: Flow | None, spot: float) -> None:
        cs = snap.candles
        px = spot or ind.close
        e = ("EMA9>20" if ind.ema9 > ind.ema20 else "EMA9<20") if ind.ema9 and ind.ema20 else "EMA warm-up"
        vw = "VWAP" if ind.vwap_weighted else "VWAP="
        rsi = f"RSI {ind.rsi:.0f}" if ind.rsi is not None else "RSI --"
        adx = f"ADX {ind.adx:.0f}" if ind.adx is not None else ""
        pcr = f"PCR {flow.pcr_oi:.2f}" if flow and flow.pcr_oi else ""
        below = sorted((z for z in zones if z.price < px), key=lambda z: px - z.price)
        above = sorted((z for z in zones if z.price >= px), key=lambda z: z.price - px)
        fmt = lambda z: f"{z.label} {z.price:.1f} ({abs(z.price - px):.1f}p/{abs(z.price - px) / ind.atr:.1f}ATR)" if z else "--"
        self.say(f"{cs[-1].ts:%H:%M} {snap.index:9} {px:10.2f} | {vw} {ind.vwap:.1f} ({(px - ind.vwap) / ind.atr:+.1f}ATR) "
                 f"{e} {rsi} {adx} ATR {ind.atr:.1f} {pcr} | S {fmt(below[0] if below else None)} | "
                 f"R {fmt(above[0] if above else None)}")
        sp = ind.speed
        target = (above[0] if above else None) if sp > 0 else (below[0] if below else None)
        if target and abs(target.price - px) <= self.cfg.approach_atr * ind.atr and abs(sp) > 0.05 * ind.atr:
            dist = abs(target.price - px)
            eta = dist / abs(sp)
            res = target.price >= px
            ch = snap.chain
            hint = ""
            if ch:
                k = ch.atm(target.price)
                ce, pe = ch.leg(k, "CE"), ch.leg(k, "PE")
                if ce and pe:
                    hint = f" | {int(k)} CE OI {ce.oi / 1e5:.1f}L PE OI {pe.oi / 1e5:.1f}L"
            plan = "rejection -> PUT, break & hold -> CALL" if res else "bounce -> CALL, break & hold -> PUT"
            self.say(f"      WATCH {'^' if res else 'v'} heading to {'resistance' if res else 'support'} "
                     f"{target.label} {target.price:.1f}: {dist:.1f} pts at {abs(sp):.1f} pts/min (~{eta:.0f} min), "
                     f"confluence {target.confluence}{hint} | {plan}")

    def print_signal(self, s: Signal) -> None:
        o = s.option
        lot_qty = s.lot_size * s.lots
        risk_rs = (o["entry"] - o["sl"]) * lot_qty
        line = "=" * 92
        bk = f"{o['book_ratio']:.2f}x" if o.get("book_ratio") else "n/a"
        oid = f" ({o['oi_day'] / 1e5:+.2f}L today)" if o.get("oi_day") is not None else ""
        self.say(f"\n{line}\n SIGNAL {s.index} BUY {s.side}  {int(o['strike'])} {o['kind']}  exp {s.expiry}   "
                 f"score {s.score:.0f}/100  [{s.grade}]   {s.time}\n{line}")
        self.say(f" setup    : {s.setup} @ {s.zone} {s.zone_price:.1f} (confluence {s.confluence})")
        self.say(f" index    : entry {s.entry:.1f}  SL {s.stop:.1f} ({abs(s.entry - s.stop):.1f})  "
                 f"T1 {s.target1:.1f} [{s.t1_label}] {s.rr1:.1f}R  T2 {s.target2:.1f} [{s.t2_label}] {s.rr2:.1f}R")
        self.say(f" option   : buy @ {o['entry']:.2f}{' (depth fill)' if o['depth_fill'] else ''}  bid {o['bid']:.2f} "
                 f"ask {o['ask']:.2f}  SL {o['sl']:.2f}  T1 {o['t1']:.2f}  T2 {o['t2']:.2f}")
        self.say(f" greeks   : delta {o['delta']:+.2f} gamma {o['gamma']:.4f} theta {o['theta']:.1f} vega {o['vega']:.1f} "
                 f"IV {o['iv']:.1f} | OI {o['oi'] / 1e5:.1f}L{oid} vol {o['volume'] / 1e5:.1f}L | spread "
                 f"{o['spread_pct']:.2f}% | book buy/sell {bk}")
        self.say(f" size     : {s.lots} lot x {s.lot_size} = {lot_qty} qty, premium risk ~Rs {risk_rs:,.0f}")
        self.say(" score    : " + "  ".join(f"{k} {v:+.1f}" for k, v in s.parts.items()))
        for r in s.reasons:
            self.say(f"   +  {r}")
        for r in s.risks:
            self.say(f"   !  {r}")
        self.say(line + "\n")


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def fetch(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "sr.py", "Accept": "application/json",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


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
    """Clock for an offline snapshot: one minute after its latest candle."""
    latest = None
    for b in index_blocks(payload).values():
        cs = parse_candles(pick(b, "candles_1m", "candles", "1m", default=[]))
        if cs and (latest is None or cs[-1].ts > latest):
            latest = cs[-1].ts
    return (latest + timedelta(seconds=60)) if latest else now_ist()


def run_live(eng: Engine, url: str, interval: float, once: bool, record: Path | None) -> None:
    fails = 0
    eng.say(f"sr.py | {', '.join(eng.indices)} | {url} | poll {interval:.0f}s | min score {eng.cfg.min_score:.0f} | "
            f"min R {eng.cfg.min_rr} | journal {eng.journal}")
    while True:
        now = now_ist()
        if not once and now.time() >= dtime(15, 31):
            eng.say("market closed; done.")
            return
        if not once and now.time() < SESSION_OPEN:
            time.sleep(min(30.0, (datetime.combine(now.date(), SESSION_OPEN, IST) - now).total_seconds() + 1))
            continue
        try:
            payload = fetch(url)
            fails = 0
        except (urllib.error.URLError, OSError, ValueError) as exc:
            fails += 1
            eng.say(f"{now:%H:%M:%S} fetch failed ({exc}); retry #{fails}")
            if once:
                return
            time.sleep(min(30.0, interval * 2 ** min(fails, 4)))
            continue
        if record:
            record.mkdir(parents=True, exist_ok=True)
            (record / f"{now:%Y%m%d_%H%M%S}.json").write_text(json.dumps(payload), encoding="utf-8")
        eng.process(payload, now_ist(), live=True)
        if once:
            return
        time.sleep(max(0.5, interval - (now_ist() - now).total_seconds()))


def run_replay(eng: Engine, path: Path) -> None:
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    eng.say(f"replaying {len(files)} snapshot(s) from {path}")
    for f in files:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            eng.say(f"{f.name}: unreadable ({exc})")
            continue
        m = re.search(r"(\d{8})_(\d{6})", f.name)
        now = datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S").replace(tzinfo=IST) if m else replay_now(payload)
        eng.process(payload, now, live=False)
    eng.say(f"\n{sum(len(s.signals) for s in eng.state.values())} signal(s).")


def parse_lot_sizes(items: list[str] | None, base: dict) -> dict:
    out = dict(base)
    for it in items or []:
        k, _, v = it.partition("=")
        if _norm_index(k) and v.strip().isdigit():
            out[_norm_index(k)] = int(v)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Support/resistance option-signal engine.")
    ap.add_argument("indices", nargs="*", help="NIFTY BANKNIFTY SENSEX (default all)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between fetches (default 3)")
    ap.add_argument("--min-score", type=float, default=Config.min_score)
    ap.add_argument("--min-rr", type=float, default=Config.min_rr)
    ap.add_argument("--min-candles", type=int, default=Config.min_candles)
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--lot-size", action="append", metavar="INDEX=N", help="override a lot size, e.g. NIFTY=75")
    ap.add_argument("--once", action="store_true", help="fetch once and exit")
    ap.add_argument("--explain", action="store_true", help="print every setup and why it passed or failed")
    ap.add_argument("--schema", action="store_true", help="print the structure of the endpoint payload")
    ap.add_argument("--record", type=Path, help="save each fetched payload into this folder")
    ap.add_argument("--replay", type=Path, help="run over saved snapshot file(s) / folder")
    ap.add_argument("--journal", type=Path, default=Path("sr_signals.jsonl"))
    ap.add_argument("--no-beep", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_sr")
        return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1
    if a.schema:
        try:
            payload = json.loads(a.replay.read_text(encoding="utf-8")) if a.replay and a.replay.is_file() else fetch(a.url)
        except Exception as exc:
            print(f"could not load payload: {exc}")
            return 1
        print("\n".join(describe(payload)))
        found = index_blocks(payload)
        print(f"\nindices found: {sorted(found) or 'NONE'}")
        for n, b in found.items():
            s = parse_snapshot(n, b)
            ch = s.chain
            print(f"{n}: {len(s.candles)} candles, chain {'%d legs exp %s spot %.1f' % (len(ch.legs), ch.expiry, ch.spot) if ch else 'MISSING'}, "
                  f"depth {len(s.depth)} books, levels {len(s.levels)}, prev close {s.prev_close}")
            if ch:
                greeks = sum(1 for l in ch.legs.values() if l.delta)
                quotes = sum(1 for l in ch.legs.values() if l.bid > 0)
                prev = sum(1 for l in ch.legs.values() if l.prev_oi is not None)
                print(f"   legs with greeks {greeks}, with bid/ask {quotes}, with previous_oi {prev}")
        return 0

    indices = [x for x in (_norm_index(i) for i in a.indices) if x] or list(INDICES)
    cfg = Config(min_score=a.min_score, min_rr=a.min_rr, min_candles=a.min_candles, lots=max(1, a.lots))
    cfg.lot_size = parse_lot_sizes(a.lot_size, cfg.lot_size)
    eng = Engine(cfg, indices, journal=a.journal, sound=not a.no_beep, explain=a.explain or a.once)
    try:
        if a.replay:
            run_replay(eng, a.replay)
        else:
            run_live(eng, a.url, a.interval, a.once, a.record)
    except KeyboardInterrupt:
        eng.say("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
