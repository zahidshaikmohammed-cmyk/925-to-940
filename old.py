"""old.py -- structural opening-drive pullback engine with an always-on #1.

Restores the original 09:15-09:30 doctrine (the real opening impulse, a
deep pullback, a reclaim that is NOT glued to the extreme, no VWAP chase)
and makes it time-aware so it can be run at any point of the session:

* Tier 1  A+ setup    : full impulse -> deep pullback -> reclaim doctrine.
* Tier 2  B  setup    : same structure, relaxed confirmation thresholds.
* Tier 3  FORCED      : best trend-pullback-near-VWAP stock when no stock
                        qualifies. Stretched / chasing stocks are penalised,
                        never rewarded.

Stops and targets are structural:
    STOP = pullback extreme -/+ buffer (5-minute ATR based, not 1m noise)
    TP1  = the impulse extreme (the high/low the move must retest)
    TP2  = measured move (pullback extreme + full impulse length, AB=CD)

Usage:
    python old.py              scan, print the #1, then manage the trade live
    python old.py --no-manage  scan and print the #1 only
    python old.py --resume     keep managing the trade saved in active_trade.json
    python old.py --self-test  offline tests only
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import time as systime
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime, timedelta
from math import sqrt
from pathlib import Path
from statistics import median

from psygrid_client import EXPECTED_UNIVERSE, PsygridClient, StockData
from run_engine import (
    BASE_URL,
    Audit,
    is_trading_day,
    load_sector_map,
    now_ist,
    parse_universe,
)
from strategy_930 import (
    IST,
    Candle,
    atr,
    clamp,
    efficiency,
    finite_ohlcv,
    robust_z,
    scale,
    session_candles,
    vwap,
)

SESSION_START = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
BIAS_NAMES = {1: "UP", -1: "DOWN", 0: "MIXED"}


@dataclass(frozen=True)
class OldConfig:
    min_bars: int = 5
    atr_period: int = 10
    liquid_top_n: int = 400          # only the most traded stocks are eligible

    # Structural leg
    min_impulse_pct: float = 0.35
    min_impulse_bars: int = 3
    min_retrace_bars: int = 3

    # Tier 1 (A+)
    t1_depth: tuple[float, float] = (0.38, 0.70)
    t1_reclaim: tuple[float, float] = (0.50, 0.85)
    t1_max_vol_ratio: float = 0.80
    t1_min_efficiency: float = 0.45
    t1_min_persistence: float = 0.55
    t1_max_vwap_ext_a5: float = 1.25
    t1_max_impulse_a5: float = 5.0
    t1_max_retrace_age: float = 1.5   # pullback bars <= 1.5 x impulse bars
    t1_min_breadth_align: float = 0.35

    # Market direction. UP when most liquid stocks are above VWAP and the
    # median stock is green since the open; DOWN is the mirror; else MIXED.
    # A+/B never trade against an UP/DOWN market; forced picks are heavily
    # penalised for it.
    bias_up_breadth: float = 0.60
    bias_down_breadth: float = 0.40
    counter_market_penalty: float = 35.0

    # Tier 2 (B)
    t2_depth: tuple[float, float] = (0.30, 0.78)
    t2_reclaim: tuple[float, float] = (0.35, 0.90)
    t2_max_vol_ratio: float = 1.10
    t2_min_efficiency: float = 0.30
    t2_min_persistence: float = 0.45
    t2_max_vwap_ext_a5: float = 1.75
    t2_max_impulse_a5: float = 6.5
    t2_max_retrace_age: float = 3.0

    # Gap (only when previous close is available)
    max_gap_pct: float = 3.0
    max_gap_z: float = 3.0

    # Risk
    stop_buffer_a5: float = 0.25
    min_risk_pct: float = 0.15
    max_risk_pct: float = 2.5
    min_tp1_r: float = 0.5            # rejects entries glued to the extreme
    min_tp2_r: float = 1.8
    time_stop_minutes: int = 20
    time_stop_min_r: float = 0.5
    max_hold_minutes: int = 60
    square_off: str = "15:15"
    manager_poll_seconds: float = 20.0


@dataclass(frozen=True)
class Context:
    market_return: float                 # median since-open return, liquid universe
    sector_return: float | None          # median since-open return of sector peers
    breadth: float                       # fraction of liquid stocks above VWAP
    gap_z: float = 0.0

    def bias(self, cfg: "OldConfig") -> int:
        """+1 market UP, -1 market DOWN, 0 MIXED."""
        if self.breadth >= cfg.bias_up_breadth and self.market_return > 0:
            return 1
        if self.breadth <= cfg.bias_down_breadth and self.market_return < 0:
            return -1
        return 0


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    tier: int
    score: float
    entry: float
    stop: float
    tp1: float
    tp2: float
    target_basis: str
    risk: float
    tp1_r: float
    tp2_r: float
    impulse_start: float
    impulse_extreme: float
    pullback_extreme: float
    depth: float
    reclaim: float
    impulse_pct: float
    impulse_a5: float
    vol_ratio: float
    vwap: float
    vwap_dist_a5: float
    rs_market: float
    rs_sector: float | None
    breadth_align: float
    atr1: float
    atr5: float
    reasons: tuple[str, ...]


# --------------------------------------------------------------------------
# Time of day
# --------------------------------------------------------------------------

# (start, end, label, stars, advice). The PRIME window is where this doctrine
# was designed to live: the opening impulse and its first pullback are done.
TIME_WINDOWS = (
    (dtime(9, 15), dtime(9, 25), "TOO EARLY", 1, "Opening leg still forming. Wait for 09:30."),
    (dtime(9, 25), dtime(9, 30), "EARLY", 2, "Nearly there. Best run is 09:30-09:45."),
    (dtime(9, 30), dtime(9, 45), "PRIME", 5, "Best window: opening impulse + first pullback."),
    (dtime(9, 45), dtime(10, 30), "GOOD", 4, "Opening structure still valid; volume still high."),
    (dtime(10, 30), dtime(11, 30), "FAIR", 3, "Trend continuation only; take smaller size."),
    (dtime(11, 30), dtime(13, 30), "MIDDAY CHOP", 2, "Low volume, false breaks common. Half size or skip."),
    (dtime(13, 30), dtime(14, 30), "AFTERNOON TREND", 3, "Second trend window; favour stocks aligned with the day."),
    (dtime(14, 30), dtime(15, 0), "LATE", 1, "Little time for TP2; take TP1 and exit."),
    (dtime(15, 0), dtime(15, 30), "NO NEW ENTRIES", 0, "Too close to square-off. Shown for reference only."),
)


def time_window(t: dtime) -> tuple[str, int, str]:
    for start, end, label, stars, advice in TIME_WINDOWS:
        if start <= t < end:
            return label, stars, advice
    return "CLOSED", 0, "Market is closed."


# --------------------------------------------------------------------------
# Maths helpers
# --------------------------------------------------------------------------

def five_minute_bars(cs: list[Candle]) -> list[Candle]:
    """Resample 1m candles into 5m candles aligned to 09:15."""
    buckets: dict[int, list[Candle]] = {}
    for c in cs:
        t = c.ts.astimezone(IST)
        key = (t.hour * 60 + t.minute - (9 * 60 + 15)) // 5
        buckets.setdefault(key, []).append(c)
    out = []
    for key in sorted(buckets):
        b = buckets[key]
        out.append(Candle(
            b[0].ts, b[0].open, max(c.high for c in b), min(c.low for c in b),
            b[-1].close, sum(c.volume for c in b),
        ))
    return out


def atr5(cs: list[Candle], period: int) -> float:
    a1 = atr(cs, period)
    bars = five_minute_bars(cs)
    if len(bars) >= 3:
        return max(atr(bars, period), a1)
    return a1 * sqrt(5.0)


def since_open_return(cs: list[Candle] | tuple[Candle, ...]) -> float:
    if not cs or cs[0].open <= 0:
        return 0.0
    return 100.0 * (cs[-1].close / cs[0].open - 1.0)


def turnover(cs: list[Candle] | tuple[Candle, ...]) -> float:
    return sum(c.close * c.volume for c in cs)


def gap_pct(cs: list[Candle] | tuple[Candle, ...], previous_close: float | None) -> float | None:
    if not cs or not previous_close or previous_close <= 0:
        return None
    return 100.0 * (cs[0].open / previous_close - 1.0)


# --------------------------------------------------------------------------
# Structural leg: session extreme, the swing that made it, the pullback after
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Leg:
    start_idx: int
    extreme_idx: int
    start: float
    extreme: float
    pullback: float
    depth: float
    reclaim: float
    impulse_bars: int
    retrace_bars: int


def structural_leg(cs: list[Candle], side: int, cfg: OldConfig) -> Leg | None:
    """The impulse is the swing into the SESSION extreme (HOD for LONG, LOD
    for SHORT), measured from the opposite extreme before it. Because the
    extreme is the session extreme, price can never already be past it, which
    is exactly the late 'reclaim > 1' entry the any-time engine allowed."""
    n = len(cs)
    if n < cfg.min_impulse_bars + cfg.min_retrace_bars:
        return None
    if side == 1:
        ext_val = max(c.high for c in cs)
        ext_idx = max(i for i, c in enumerate(cs) if c.high == ext_val)
        pre = cs[:ext_idx + 1]
        start_val = min(c.low for c in pre)
        start_idx = max(i for i, c in enumerate(pre) if c.low == start_val)
    else:
        ext_val = min(c.low for c in cs)
        ext_idx = max(i for i, c in enumerate(cs) if c.low == ext_val)
        pre = cs[:ext_idx + 1]
        start_val = max(c.high for c in pre)
        start_idx = max(i for i, c in enumerate(pre) if c.high == start_val)

    impulse_bars = ext_idx - start_idx + 1
    post = cs[ext_idx + 1:]
    if impulse_bars < cfg.min_impulse_bars or len(post) < cfg.min_retrace_bars:
        return None
    length = abs(ext_val - start_val)
    if length <= 0:
        return None
    pullback = min(c.low for c in post) if side == 1 else max(c.high for c in post)
    depth = abs(ext_val - pullback) / length
    swing = abs(ext_val - pullback)
    reclaim = side * (cs[-1].close - pullback) / swing if swing > 0 else 0.0
    return Leg(start_idx, ext_idx, start_val, ext_val, pullback, depth, reclaim,
               impulse_bars, len(post))


# --------------------------------------------------------------------------
# Tier 1 / Tier 2
# --------------------------------------------------------------------------

def _setup(symbol: str, cs: list[Candle], side: int, tier: int, gap: float | None,
           ctx: Context, cfg: OldConfig) -> Signal | None:
    leg = structural_leg(cs, side, cfg)
    if leg is None:
        return None
    a1 = atr(cs, cfg.atr_period)
    a5 = atr5(cs, cfg.atr_period)
    if a1 <= 0 or a5 <= 0:
        return None

    last = cs[-1]
    entry = last.close
    imp = cs[leg.start_idx:leg.extreme_idx + 1]
    post = cs[leg.extreme_idx + 1:]
    impulse_pct = side * 100.0 * (leg.extreme / leg.start - 1.0)
    impulse_a5 = abs(leg.extreme - leg.start) / a5
    vol_ratio = (sum(c.volume for c in post) / len(post)) / max(sum(c.volume for c in imp) / len(imp), 1e-12)
    eff = efficiency(imp)
    persistence = sum(1 for c in imp if side * (c.close - c.open) > 0) / len(imp)
    vw = vwap(cs)
    vw_dist = side * (entry - vw) / a5
    stock_ret = since_open_return(cs)
    rs = side * (stock_ret - ctx.market_return)
    srs = side * (stock_ret - ctx.sector_return) if ctx.sector_return is not None else None
    align = ctx.breadth if side == 1 else 1.0 - ctx.breadth
    age = leg.retrace_bars / leg.impulse_bars

    if tier == 1:
        depth_rng, reclaim_rng = cfg.t1_depth, cfg.t1_reclaim
        max_vol, min_eff, min_pers = cfg.t1_max_vol_ratio, cfg.t1_min_efficiency, cfg.t1_min_persistence
        max_ext, max_imp, max_age = cfg.t1_max_vwap_ext_a5, cfg.t1_max_impulse_a5, cfg.t1_max_retrace_age
    else:
        depth_rng, reclaim_rng = cfg.t2_depth, cfg.t2_reclaim
        max_vol, min_eff, min_pers = cfg.t2_max_vol_ratio, cfg.t2_min_efficiency, cfg.t2_min_persistence
        max_ext, max_imp, max_age = cfg.t2_max_vwap_ext_a5, cfg.t2_max_impulse_a5, cfg.t2_max_retrace_age

    if ctx.bias(cfg) == -side:                      # never against the market
        return None
    if side * (entry - cs[0].open) <= 0:            # never against the stock's own day
        return None
    if gap is not None and (abs(gap) > cfg.max_gap_pct or abs(ctx.gap_z) > cfg.max_gap_z):
        return None
    if impulse_pct < cfg.min_impulse_pct or impulse_a5 > max_imp:
        return None
    if not depth_rng[0] <= leg.depth <= depth_rng[1]:
        return None
    if not reclaim_rng[0] <= leg.reclaim <= reclaim_rng[1]:
        return None
    if vol_ratio > max_vol or eff < min_eff or persistence < min_pers:
        return None
    if not 0.0 < vw_dist <= max_ext:
        return None
    if age > max_age:
        return None
    if tier == 1:
        if side * (last.close - last.open) <= 0:     # trigger bar must agree
            return None
        if align < cfg.t1_min_breadth_align:
            return None

    stop = leg.pullback - side * cfg.stop_buffer_a5 * a5
    risk = side * (entry - stop)
    if risk <= 0 or not cfg.min_risk_pct <= 100.0 * risk / entry <= cfg.max_risk_pct:
        return None
    tp1 = leg.extreme
    tp2 = leg.pullback + side * abs(leg.extreme - leg.start)
    if side * (tp2 - tp1) <= 0:
        tp2 = tp1 + side * risk
    tp1_r = side * (tp1 - entry) / risk
    tp2_r = side * (tp2 - entry) / risk
    if tp1_r < cfg.min_tp1_r or tp2_r < cfg.min_tp2_r:
        return None

    w_sector = 0.06 if srs is not None else 0.0
    parts = (
        (0.18, clamp(1.0 - abs(leg.depth - 0.52) / 0.22)),
        (0.14, scale(tp2_r, 1.5, 3.5)),
        (0.12, scale(impulse_a5, 1.0, 4.0)),
        (0.12, scale(1.0 - vol_ratio, 0.0, 0.5)),
        (0.10, 0.5 * clamp(eff) + 0.5 * persistence),
        (0.08, clamp(1.0 - abs(vw_dist - 0.5) / 1.0)),
        (0.12, scale(rs, 0.0, 1.0)),
        (w_sector, scale(srs or 0.0, 0.0, 0.8)),
        (0.08, scale(align, 0.3, 0.7)),
    )
    total_w = sum(w for w, _ in parts)
    score = 100.0 * sum(w * s for w, s in parts) / total_w
    if tier == 2:
        score -= 5.0

    reasons = (
        "A+_SETUP" if tier == 1 else "B_SETUP",
        f"market={BIAS_NAMES[ctx.bias(cfg)]}",
        f"depth={leg.depth:.3f}",
        f"reclaim={leg.reclaim:.3f}",
        f"impulse_bars={leg.impulse_bars}",
        f"pullback_bars={leg.retrace_bars}",
        f"vol_ratio={vol_ratio:.2f}",
        f"efficiency={eff:.2f}",
        f"persistence={persistence:.2f}",
    ) + (() if srs is not None else ("sector_data=ABSENT_WEIGHT_REDISTRIBUTED",))
    return Signal(
        symbol, "LONG" if side == 1 else "SHORT", tier, score, entry, stop, tp1, tp2,
        "STRUCTURAL: TP1=impulse extreme, TP2=measured move", risk, tp1_r, tp2_r,
        leg.start, leg.extreme, leg.pullback, leg.depth, leg.reclaim, impulse_pct,
        impulse_a5, vol_ratio, vw, vw_dist, rs, srs, align, a1, a5, reasons,
    )


# --------------------------------------------------------------------------
# Tier 3: forced, but only ever a trend pullback near VWAP
# --------------------------------------------------------------------------

def _forced(symbol: str, cs: list[Candle], side: int, ctx: Context, cfg: OldConfig) -> Signal | None:
    a1 = atr(cs, cfg.atr_period)
    a5 = atr5(cs, cfg.atr_period)
    if a1 <= 0 or a5 <= 0:
        return None
    entry = cs[-1].close
    vw = vwap(cs)
    vw_dist = side * (entry - vw) / a5
    trend = side * (entry - cs[0].open) / a5
    stock_ret = since_open_return(cs)
    rs = side * (stock_ret - ctx.market_return)
    srs = side * (stock_ret - ctx.sector_return) if ctx.sector_return is not None else None
    align = ctx.breadth if side == 1 else 1.0 - ctx.breadth
    extreme = max(c.high for c in cs) if side == 1 else min(c.low for c in cs)
    room = side * (extreme - entry) / a5          # distance back to HOD/LOD
    recent = cs[-12:]
    struct = 0.5 * clamp(efficiency(cs)) + 0.5 * (
        sum(1 for c in recent if side * (c.close - c.open) > 0) / len(recent)
    )

    # Near VWAP on the right side is ideal; far beyond it is a chase.
    vw_score = clamp(1.0 - abs(vw_dist - 0.5) / 1.5) if vw_dist > -0.25 else 0.0
    penalty = 0.0
    penalty += 20.0 * scale(vw_dist, 1.75, 5.0)   # stretched from VWAP
    # Buying near the high / selling near the low: full penalty at the
    # extreme, fading out once price has pulled back 1 ATR5 from it.
    penalty += 25.0 * (1.0 - scale(room, 0.3, 1.0))
    penalty += 25.0 * scale(-trend, 0.0, 1.0)     # against the stock's own day
    if ctx.bias(cfg) == -side:                     # against the whole market
        penalty += cfg.counter_market_penalty

    w_sector = 0.05 if srs is not None else 0.0
    parts = (
        (0.20, scale(trend, 0.0, 3.0)),
        (0.20, vw_score),
        (0.15, scale(rs, 0.0, 1.5)),
        (w_sector, scale(srs or 0.0, 0.0, 1.0)),
        (0.15, struct),
        (0.10, scale(align, 0.3, 0.7)),
        (0.15, scale(room, 0.3, 2.0)),
    )
    total_w = sum(w for w, _ in parts)
    score = 100.0 * sum(w * s for w, s in parts) / total_w - penalty

    swing = min(c.low for c in cs[-10:]) if side == 1 else max(c.high for c in cs[-10:])
    stop = swing - side * cfg.stop_buffer_a5 * a5
    risk = side * (entry - stop)
    min_risk = max(0.6 * a5, entry * cfg.min_risk_pct / 100.0)
    max_risk = entry * cfg.max_risk_pct / 100.0
    if risk < min_risk:
        stop, risk = entry - side * min_risk, min_risk
    elif risk > max_risk:
        stop, risk = entry - side * max_risk, max_risk

    if room * a5 >= 0.75 * risk:
        tp1, basis = extreme, "STRUCTURAL: TP1=session extreme"
    else:
        tp1, basis = entry + side * risk, "R-MULTIPLE: no structural level in reach"
    tp2 = entry + side * max(2.0 * risk, abs(tp1 - entry) + risk)

    reasons = (
        "FORCED_BEST_AVAILABLE",
        "NO_STOCK_PASSED_A+_OR_B",
        f"market={BIAS_NAMES[ctx.bias(cfg)]}",
        f"trend={trend:+.2f}ATR5",
        f"room_to_extreme={room:.2f}ATR5",
        f"penalty={penalty:.1f}",
    ) + (() if srs is not None else ("sector_data=ABSENT_WEIGHT_REDISTRIBUTED",))
    return Signal(
        symbol, "LONG" if side == 1 else "SHORT", 3, score, entry, stop, tp1, tp2, basis,
        risk, side * (tp1 - entry) / risk, side * (tp2 - entry) / risk,
        cs[0].open, extreme, swing, 0.0, 0.0, side * stock_ret, abs(entry - cs[0].open) / a5,
        1.0, vw, vw_dist, rs, srs, align, a1, a5, reasons,
    )


def evaluate_stock(symbol: str, candles, previous_close: float | None,
                   ctx: Context, cfg: OldConfig) -> list[Signal]:
    cs = session_candles(candles)
    if len(cs) < cfg.min_bars or any(not finite_ohlcv(c) for c in cs):
        return []
    gap = gap_pct(cs, previous_close)
    for tier in (1, 2):
        found = [s for side in (1, -1) if (s := _setup(symbol, cs, side, tier, gap, ctx, cfg))]
        if found:
            return found
    return [s for side in (1, -1) if (s := _forced(symbol, cs, side, ctx, cfg))]


# --------------------------------------------------------------------------
# Universe
# --------------------------------------------------------------------------

def liquid_universe(healthy: dict[str, StockData], cfg: OldConfig) -> dict[str, StockData]:
    ranked = sorted(healthy.items(), key=lambda kv: turnover(kv[1].candles), reverse=True)
    return dict(ranked[:cfg.liquid_top_n])


def scan(healthy: dict[str, StockData], sectors: dict[str, str], cfg: OldConfig) -> tuple[list[Signal], dict]:
    liquid = liquid_universe(
        {s: d for s, d in healthy.items() if len(d.candles) >= cfg.min_bars}, cfg,
    )
    if not liquid:
        return [], {}
    returns = {s: since_open_return(d.candles) for s, d in liquid.items()}
    market = median(returns.values())
    breadth = sum(1 for d in liquid.values() if d.candles[-1].close > vwap(d.candles)) / len(liquid)
    peers: dict[str, list[float]] = {}
    for s, r in returns.items():
        if sectors.get(s):
            peers.setdefault(sectors[s], []).append(r)
    gaps = {s: g for s, d in liquid.items() if (g := gap_pct(d.candles, d.previous_close)) is not None}
    gap_sample = list(gaps.values())

    signals: list[Signal] = []
    for s, d in liquid.items():
        sector = sectors.get(s)
        sector_ret = median(peers[sector]) if sector and len(peers.get(sector, [])) >= 3 else None
        ctx = Context(market, sector_ret, breadth, robust_z(gaps[s], gap_sample) if s in gaps else 0.0)
        try:
            signals.extend(evaluate_stock(s, d.candles, d.previous_close, ctx, cfg))
        except Exception as exc:
            print(f"[EVAL-WARN] {s}: {exc}", flush=True)
    stats = {"liquid": len(liquid), "market_return": market, "breadth": breadth}
    return signals, stats


def rank_signals(signals: list[Signal]) -> list[Signal]:
    return sorted(signals, key=lambda s: (s.tier, -s.score, -s.rs_market, s.symbol, s.side))


# --------------------------------------------------------------------------
# Trade manager: watches the #1 minute by minute and says what to do
# --------------------------------------------------------------------------

TRADE_FILE = Path("active_trade.json")


@dataclass
class Trade:
    symbol: str
    side: str
    entry: float
    stop: float                 # original stop; defines 1R
    tp1: float
    tp2: float
    opened_at: str              # ISO time the trade was taken
    last_ts: str                # last 1m candle already processed
    current_stop: float
    booked_half: bool = False
    time_check_done: bool = False
    trail_level: float | None = None
    closed: bool = False
    exit_price: float | None = None
    exit_reason: str = ""
    realized_r: float = 0.0

    @property
    def d(self) -> int:
        return 1 if self.side == "LONG" else -1

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)


def trade_from_signal(sig: Signal, opened_at: datetime, last_ts: datetime, entry: float | None = None) -> Trade:
    fill = sig.entry if entry is None else entry
    return Trade(sig.symbol, sig.side, fill, sig.stop, sig.tp1, sig.tp2,
                 opened_at.isoformat(), last_ts.isoformat(), sig.stop)


def save_trade(tr: Trade, path: Path = TRADE_FILE) -> None:
    try:
        path.write_text(json.dumps(asdict(tr), indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[TRADE-WARN] could not save {path}: {exc}")


def load_trade(path: Path = TRADE_FILE) -> Trade | None:
    try:
        return Trade(**json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _close(tr: Trade, price: float, reason: str) -> None:
    share = 0.5 if tr.booked_half else 1.0
    tr.realized_r += share * tr.d * (price - tr.entry) / tr.risk
    tr.closed, tr.exit_price, tr.exit_reason = True, price, reason


def _book_level(tr: Trade) -> float:
    """TP1 or +1R, whichever comes first."""
    dist = tr.d * (tr.tp1 - tr.entry)
    dist = tr.risk if dist <= 0 else min(dist, tr.risk)
    return tr.entry + tr.d * dist


def manage_step(tr: Trade, candles, cfg: OldConfig) -> list[tuple[str, str, str]]:
    """Apply the holding rules to every new completed 1m candle.

    Returns (HH:MM, kind, message) events. kind is STATUS, BOOK, TRAIL or EXIT.
    Within one candle the stop is checked first (worst case), so a candle that
    touches both the stop and a target is treated as a stop-out.
    """
    cs = session_candles(candles)
    last = datetime.fromisoformat(tr.last_ts)
    opened = datetime.fromisoformat(tr.opened_at)
    sq_h, sq_m = (int(x) for x in cfg.square_off.split(":"))
    d, risk = tr.d, tr.risk
    events: list[tuple[str, str, str]] = []

    for i, c in enumerate(cs):
        if tr.closed:
            break
        if c.ts <= last:
            continue
        tr.last_ts = c.ts.isoformat()
        stamp = f"{c.ts.astimezone(IST):%H:%M}"
        done_at = c.ts + timedelta(minutes=1)
        adverse = c.low if d == 1 else c.high
        favour = c.high if d == 1 else c.low
        n_before = len(events)

        if d * (adverse - tr.current_stop) <= 0:
            kind = "BREAKEVEN STOP" if tr.booked_half else "STOP LOSS HIT"
            _close(tr, tr.current_stop, kind)
            events.append((stamp, "EXIT", f"{kind} at Rs {tr.current_stop:.2f}"))
            break
        if d * (favour - tr.tp2) >= 0:
            _close(tr, tr.tp2, "TP2 HIT")
            events.append((stamp, "EXIT", f"TP2 HIT at Rs {tr.tp2:.2f} -- exit everything"))
            break
        if not tr.booked_half and d * (favour - _book_level(tr)) >= 0:
            level = _book_level(tr)
            tr.booked_half = True
            tr.realized_r += 0.5 * d * (level - tr.entry) / risk
            tr.current_stop = tr.entry
            events.append((stamp, "BOOK", f"BOOK 50% NOW at Rs {level:.2f} | MOVE SL TO ENTRY Rs {tr.entry:.2f}"))

        if not tr.time_check_done and done_at >= opened + timedelta(minutes=cfg.time_stop_minutes):
            tr.time_check_done = True
            if not tr.booked_half and d * (c.close - tr.entry) < cfg.time_stop_min_r * risk:
                _close(tr, c.close, "TIME STOP")
                events.append((stamp, "EXIT", f"TIME STOP: not +{cfg.time_stop_min_r}R after "
                                              f"{cfg.time_stop_minutes} min -- EXIT NOW at ~Rs {c.close:.2f}"))
                break

        minute = c.ts.astimezone(IST).hour * 60 + c.ts.astimezone(IST).minute - (9 * 60 + 15)
        if minute % 5 == 4:                        # this candle completed a 5m bar
            upto = cs[:i + 1]
            bars = five_minute_bars(upto)
            vw = vwap(upto)
            bar = bars[-1]
            if d * (bar.close - vw) < 0:
                _close(tr, bar.close, "5M CLOSE WRONG SIDE OF VWAP")
                events.append((stamp, "EXIT", f"EXIT NOW: 5m candle closed {'below' if d == 1 else 'above'} "
                                              f"VWAP Rs {vw:.2f} (~Rs {bar.close:.2f})"))
                break
            if tr.booked_half and len(bars) >= 2:
                prev = bars[-2]
                prev_level = prev.low if d == 1 else prev.high
                if d * (bar.close - prev_level) < 0:
                    _close(tr, bar.close, "5M TRAIL BROKEN")
                    events.append((stamp, "EXIT", f"EXIT NOW: 5m candle closed {'below' if d == 1 else 'above'} "
                                                  f"previous 5m {'low' if d == 1 else 'high'} Rs {prev_level:.2f}"))
                    break
                tr.trail_level = bar.low if d == 1 else bar.high
                events.append((stamp, "TRAIL", f"TRAIL: exit runner if next 5m candle closes "
                                               f"{'below' if d == 1 else 'above'} Rs {tr.trail_level:.2f}"))

        if done_at >= opened + timedelta(minutes=cfg.max_hold_minutes):
            _close(tr, c.close, f"MAX HOLD {cfg.max_hold_minutes} MIN")
            events.append((stamp, "EXIT", f"MAX HOLD {cfg.max_hold_minutes} MIN reached -- EXIT NOW at ~Rs {c.close:.2f}"))
            break
        if done_at.astimezone(IST).time() >= dtime(sq_h, sq_m):
            _close(tr, c.close, "SQUARE OFF")
            events.append((stamp, "EXIT", f"SQUARE-OFF TIME -- EXIT NOW at ~Rs {c.close:.2f}"))
            break

        if len(events) == n_before:
            open_r = d * (c.close - tr.entry) / risk
            events.append((stamp, "STATUS", f"close Rs {c.close:.2f} | {open_r:+.2f}R | SL Rs {tr.current_stop:.2f}"
                                            f"{' | runner' if tr.booked_half else ''} | HOLD"))
    return events


def clock_check(tr: Trade, now: datetime, last_close: float, cfg: OldConfig) -> list[tuple[str, str, str]]:
    """Wall-clock backup for the time rules when no new candle has arrived
    (feed stalled). Allows a 2-minute grace for the candle to show up."""
    if tr.closed:
        return []
    opened = datetime.fromisoformat(tr.opened_at)
    grace = timedelta(minutes=2)
    sq_h, sq_m = (int(x) for x in cfg.square_off.split(":"))
    stamp = f"{now.astimezone(IST):%H:%M}"
    if now >= opened + timedelta(minutes=cfg.max_hold_minutes) + grace:
        reason = f"MAX HOLD {cfg.max_hold_minutes} MIN"
    elif now.astimezone(IST).time() >= dtime(sq_h, sq_m + 2 if sq_m < 58 else sq_m):
        reason = "SQUARE OFF"
    elif (not tr.booked_half and not tr.time_check_done
          and now >= opened + timedelta(minutes=cfg.time_stop_minutes) + grace
          and tr.d * (last_close - tr.entry) < cfg.time_stop_min_r * tr.risk):
        tr.time_check_done = True
        reason = "TIME STOP"
    else:
        return []
    _close(tr, last_close, reason + " (FEED STALLED)")
    return [(stamp, "EXIT", f"{reason}: no fresh candle from the feed -- EXIT NOW at market (last ~Rs {last_close:.2f})")]


def beep() -> None:
    print("\a", end="", flush=True)
    try:
        import winsound
        winsound.Beep(1200, 400)
    except Exception:
        pass


def run_manager(tr: Trade, client: PsygridClient, cfg: OldConfig, audit: Audit) -> None:
    print("\n" + "=" * 96)
    print(f"TRADE MANAGER // {tr.symbol} {tr.side} | entry Rs {tr.entry:.2f} | SL Rs {tr.stop:.2f} | "
          f"TP1 Rs {tr.tp1:.2f} | TP2 Rs {tr.tp2:.2f}")
    print(f"Rules: +{cfg.time_stop_min_r}R within {cfg.time_stop_minutes} min or exit | TP1/+1R -> book 50%, SL to entry | "
          f"trail on 5m closes | 5m close past VWAP -> exit | max {cfg.max_hold_minutes} min")
    print("Checks every completed 1m candle. Beeps on every action. Ctrl+C stops watching (resume: python old.py --resume)")
    print("=" * 96, flush=True)
    save_trade(tr)
    try:
        while not tr.closed:
            now = now_ist()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    raw = client.market()
                if tr.symbol not in raw:
                    raise RuntimeError(f"{tr.symbol} missing from feed")
                data = client.stock(tr.symbol, raw[tr.symbol], now)
                events = manage_step(tr, data.candles, cfg)
                if data.candles:
                    events += clock_check(tr, now, data.candles[-1].close, cfg)
            except Exception as exc:
                print(f"[{now:%H:%M:%S}] feed warning: {exc} -- retrying", flush=True)
                events = []
            for stamp, kind, msg in events:
                if kind == "STATUS":
                    print(f"[{stamp}] {msg}", flush=True)
                else:
                    beep()
                    print(f"\n[{stamp}] >>> {kind}: {msg}\n", flush=True)
            save_trade(tr)
            if tr.closed:
                break
            if now.time() >= MARKET_CLOSE:
                print("Market closed while the trade was open -- square it off manually.")
                break
            systime.sleep(cfg.manager_poll_seconds)
    except KeyboardInterrupt:
        print("\nStopped watching. The trade is saved; resume with: python old.py --resume")
        return
    if tr.closed:
        print("-" * 96)
        print(f"TRADE CLOSED : {tr.exit_reason} | result {tr.realized_r:+.2f}R")
        print("-" * 96)
        audit.event("OLD_TRADE_CLOSED", **asdict(tr))


def ask_fill(sig: Signal) -> float | None:
    """Ask whether the trade was taken; returns the fill price or None."""
    try:
        ans = input(f"\nDid you take {sig.symbol} {sig.side}? [Enter] = manage at Rs {sig.entry:.2f} | "
                    f"type your fill price | n = skip: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if ans in ("n", "no", "skip"):
        return None
    if not ans:
        return sig.entry
    try:
        fill = float(ans)
    except ValueError:
        print("Not a number -- skipping the trade manager.")
        return None
    d = 1 if sig.side == "LONG" else -1
    if d * (fill - sig.stop) <= 0:
        print("That fill is already beyond the stop loss -- skipping the trade manager.")
        return None
    return fill


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def print_time_guide(t: dtime) -> None:
    label, stars, advice = time_window(t)
    print(f"TIME WINDOW  : {label} {'*' * stars}{'.' * (5 - stars)}  -> {advice}")
    print("BEST TIMES   : 09:30-09:45 PRIME | 09:45-10:30 GOOD | 13:30-14:30 AFTERNOON | avoid 11:30-13:30 and after 15:00")


def confidence(sig: Signal, stars: int) -> str:
    base = {1: 3, 2: 2, 3: 1}[sig.tier]
    total = base + (1 if stars >= 4 else 0) - (1 if stars <= 2 else 0)
    return ("LOW", "LOW", "MEDIUM", "HIGH", "HIGH")[max(0, min(4, total))]


def print_signal(sig: Signal, t: dtime, cfg: OldConfig) -> None:
    label, stars, _ = time_window(t)
    tier_name = {1: "A+ SETUP", 2: "B SETUP", 3: "FORCED (best available, no clean setup)"}[sig.tier]
    d = 1 if sig.side == "LONG" else -1
    print("\n" + "-" * 96)
    print("#1 TRADE RIGHT NOW")
    print("-" * 96)
    print(f"SYMBOL       : {sig.symbol}")
    print(f"DIRECTION    : {sig.side}")
    print(f"TIER         : {sig.tier} {tier_name}")
    print(f"CONFIDENCE   : {confidence(sig, stars)}  (tier {sig.tier} x {label} window)")
    print(f"SCORE        : {sig.score:.2f}/100")
    print(f"ENTRY        : Rs {sig.entry:.2f}  (limit; do not chase beyond Rs {sig.entry + d * 0.25 * sig.risk:.2f})")
    print(f"STOP LOSS    : Rs {sig.stop:.2f}  (risk Rs {sig.risk:.2f}/share)")
    print(f"TP1          : Rs {sig.tp1:.2f}  ({sig.tp1_r:.2f}R)  -> book 50%, move stop to entry")
    print(f"TP2          : Rs {sig.tp2:.2f}  ({sig.tp2_r:.2f}R)")
    print(f"TARGET BASIS : {sig.target_basis}")
    print(f"TIME STOP    : exit if not +0.5R within {cfg.time_stop_minutes} min | square off by {cfg.square_off}")
    if sig.tier < 3:
        print(f"STRUCTURE    : impulse {sig.impulse_start:.2f} -> {sig.impulse_extreme:.2f} | pullback to {sig.pullback_extreme:.2f}")
        print(f"               depth {sig.depth * 100:.1f}% | reclaim {sig.reclaim * 100:.1f}% | vol ratio {sig.vol_ratio:.2f}x")
    print(f"IMPULSE      : {sig.impulse_pct:+.3f}% / {sig.impulse_a5:.2f} ATR5")
    print(f"VWAP         : Rs {sig.vwap:.2f} | distance {sig.vwap_dist_a5:+.2f} ATR5")
    print(f"RS MARKET    : {sig.rs_market:+.3f}%")
    print(f"RS SECTOR    : {'n/a' if sig.rs_sector is None else f'{sig.rs_sector:+.3f}%'}")
    print(f"BREADTH ALIGN: {sig.breadth_align * 100:.0f}% of liquid stocks agree with this direction")
    print(f"ATR 1m / 5m  : {sig.atr1:.3f} / {sig.atr5:.3f}")
    print(f"REASONS      : {' | '.join(sig.reasons)}")
    if stars == 0:
        print("WARNING      : NO NEW ENTRIES window -- shown for reference only.")
    elif sig.tier == 3:
        print("NOTE         : forced pick -- trade smaller size than an A+/B setup.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="old.py structural opening-drive engine")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--no-manage", action="store_true", help="print the #1 and exit")
    parser.add_argument("--resume", action="store_true", help="resume managing active_trade.json")
    args = parser.parse_args(argv)

    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromNames(["tests.test_old", "tests.test_old_manager"])
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 10

    cfg = OldConfig()
    audit = Audit()
    if args.resume:
        tr = load_trade()
        if tr is None or tr.closed:
            print("No open trade saved in active_trade.json.")
            return 40
        run_manager(tr, PsygridClient(args.base_url), cfg, audit)
        return 0
    print("=" * 96)
    print("OLD.PY // STRUCTURAL OPENING-DRIVE PULLBACK ENGINE | A+ -> B -> FORCED | ALWAYS ONE #1")
    print("=" * 96)
    now = now_ist()
    if not is_trading_day(now.date()):
        print(f"NOT A NORMAL NSE EQUITY TRADING DAY: {now.date().isoformat()}")
        return 20
    if not SESSION_START <= now.time() < MARKET_CLOSE:
        print("Market is closed. Best time to run: 09:30-09:45 IST.")
        return 22
    print_time_guide(now.time())

    client = PsygridClient(args.base_url)
    try:
        raw = client.market()
    except Exception as exc:
        print(f"FATAL: no usable stock feed returned: {exc}")
        return 30
    scan_time = now_ist()
    parsed = parse_universe(client, raw, scan_time)
    healthy = {s: d for s, d in parsed.items() if d.health.healthy}
    signals, stats = scan(healthy, load_sector_map(), cfg)
    print(f"SCAN TIME    : {scan_time:%Y-%m-%d %H:%M:%S} IST")
    print(f"UNIVERSE     : {len(parsed)}/{EXPECTED_UNIVERSE} received | healthy {len(healthy)} | liquid top {stats.get('liquid', 0)}")
    if stats:
        bias = Context(stats["market_return"], None, stats["breadth"]).bias(cfg)
        allowed = {1: "LONGS ONLY", -1: "SHORTS ONLY", 0: "both directions"}[bias]
        print(f"MARKET       : {BIAS_NAMES[bias]} -> {allowed} | median since-open {stats['market_return']:+.3f}% | "
              f"breadth above VWAP {stats['breadth'] * 100:.0f}%")
    counts = {t: len({s.symbol for s in signals if s.tier == t}) for t in (1, 2, 3)}
    print(f"STOCKS       : A+={counts[1]} | B={counts[2]} | forced-only={counts[3]}")

    ranked = rank_signals(signals)
    if not ranked:
        print("FATAL: not enough completed candles to score any stock yet. Run at/after 09:30.")
        return 32
    best = ranked[0]
    print_signal(best, scan_time.time(), cfg)
    runners = [s for s in ranked[1:] if s.symbol != best.symbol][:3]
    if runners:
        print("\nRUNNERS-UP   :")
        for s in runners:
            print(f"  T{s.tier} {s.symbol:<12} {s.side:<5} score {s.score:6.2f} | entry {s.entry:.2f} SL {s.stop:.2f} TP1 {s.tp1:.2f} TP2 {s.tp2:.2f}")
    print("\nNOTE: deterministic research signal; not a guarantee of profit.")
    audit.event("OLD_SIGNAL", scan_time=scan_time.isoformat(), window=time_window(scan_time.time())[0],
                counts=counts, selected=asdict(best))

    if args.no_manage:
        return 0
    if time_window(scan_time.time())[1] == 0:
        print("No new entries this late -- trade manager not started.")
        return 0
    fill = ask_fill(best)
    if fill is None:
        return 0
    last_ts = parsed[best.symbol].candles[-1].ts
    run_manager(trade_from_signal(best, now_ist(), last_ts, fill), client, cfg, audit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
