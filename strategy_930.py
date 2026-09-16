from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from math import isfinite
from statistics import median
from typing import Iterable
from zoneinfo import ZoneInfo

from config import StrategyConfig

IST = ZoneInfo("Asia/Kolkata")
SESSION_START = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Candidate:
    symbol: str
    side: str
    score: float
    entry: float
    stop: float
    target: float
    gap_pct: float
    impulse_pct: float
    impulse_atr: float
    retracement_depth: float
    retracement_volume_ratio: float
    rs_market: float
    rs_sector: float
    vwap_distance_atr: float
    structure: float
    atr_value: float
    retracement_level: float
    tier: int
    reasons: tuple[str, ...]

    @property
    def is_fallback(self) -> bool:
        return self.tier > 1


def finite_ohlcv(c: Candle) -> bool:
    values = (c.open, c.high, c.low, c.close, c.volume)
    return (
        all(isfinite(x) for x in values)
        and min(c.open, c.high, c.low, c.close) > 0
        and c.volume >= 0
        and c.high >= max(c.open, c.close)
        and c.low <= min(c.open, c.close)
    )


def session_candles(candles: Iterable[Candle]) -> list[Candle]:
    """Return only today's regular-session candles.

    The 09:15 candle is the preferred opening reference. Pre-open rows such as
    09:09 are never allowed to become the opening reference by accident.
    """
    cs = sorted(list(candles), key=lambda c: c.ts)
    if not cs:
        return []
    today = cs[-1].ts.astimezone(IST).date()
    return [
        c for c in cs
        if c.ts.astimezone(IST).date() == today
        and SESSION_START <= c.ts.astimezone(IST).time() < MARKET_CLOSE
    ]


def atr(cs: list[Candle] | tuple[Candle, ...], period: int) -> float:
    prev = None
    trs: list[float] = []
    for c in cs:
        tr = c.high - c.low if prev is None else max(
            c.high - c.low,
            abs(c.high - prev),
            abs(c.low - prev),
        )
        trs.append(tr)
        prev = c.close
    return median(trs[-period:]) if trs else 0.0


def vwap(cs: list[Candle] | tuple[Candle, ...]) -> float:
    den = sum(c.volume for c in cs)
    if den:
        return sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in cs) / den
    return sum(c.close for c in cs) / len(cs) if cs else 0.0


def efficiency(cs: list[Candle] | tuple[Candle, ...]) -> float:
    if len(cs) < 2:
        return 0.0
    path = sum(abs(cs[i].close - cs[i - 1].close) for i in range(1, len(cs)))
    return abs(cs[-1].close - cs[0].open) / path if path else 0.0


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def scale(x: float, lo: float, hi: float) -> float:
    return clamp((x - lo) / (hi - lo)) if hi > lo else 0.0


def robust_z(x: float, sample: Iterable[float]) -> float:
    xs = [v for v in sample if isfinite(v)]
    if len(xs) < 5:
        return 0.0
    m = median(xs)
    mad = median(abs(v - m) for v in xs)
    return 0.0 if mad == 0 else 0.6744897501960817 * (x - m) / mad


def _opening_price(cs: list[Candle]) -> float:
    for c in cs:
        if c.ts.astimezone(IST).time() == SESSION_START:
            return c.open
    return cs[0].open


def _leg(cs: list[Candle], side: int, cfg: StrategyConfig):
    start = _opening_price(cs)
    idx = (max if side == 1 else min)(
        range(len(cs)), key=lambda i: cs[i].high if side == 1 else cs[i].low
    )
    if idx < cfg.min_impulse_bars - 1:
        return None
    extreme = cs[idx].high if side == 1 else cs[idx].low
    post = cs[idx + 1 :]
    if (extreme <= start if side == 1 else extreme >= start):
        return None
    if len(post) < cfg.min_retracement_bars:
        return None
    retrace = min(c.low for c in post) if side == 1 else max(c.high for c in post)
    den = abs(extreme - start)
    if den <= 0:
        return None
    depth = abs(extreme - retrace) / den
    if side == 1:
        reclaim = (cs[-1].close - retrace) / max(extreme - retrace, 1e-12)
    else:
        reclaim = (retrace - cs[-1].close) / max(retrace - extreme, 1e-12)
    impulse_pct = side * 100.0 * (extreme / start - 1.0)
    return idx, extreme, retrace, depth, reclaim, impulse_pct, start


def _hard_exhaustion(
    idx: int,
    impulse_atr: float,
    atr_value: float,
    final_close: float,
    extreme: float,
    vwap_distance_atr: float,
    cfg: StrategyConfig,
) -> bool:
    if idx > cfg.late_extreme_bar:
        return True
    if impulse_atr > cfg.max_impulse_atr:
        return True
    if abs(final_close - extreme) / max(atr_value, 1e-12) < cfg.exhaustion_reclaim_distance_atr:
        return True
    if abs(vwap_distance_atr) > cfg.max_extension_from_vwap_atr:
        return True
    return False


def _gap_pct(start: float, previous_close: float | None) -> float:
    if previous_close is None or previous_close <= 0:
        return 0.0
    return 100.0 * (start / previous_close - 1.0)


def _emergency_candidate(
    symbol: str,
    side: int,
    name: str,
    cs: list[Candle],
    entry: float,
    previous_close: float | None,
    market_return: float,
    sector_return: float,
    cfg: StrategyConfig,
) -> Candidate | None:
    a = atr(cs, cfg.atr_period)
    if a <= 0 or entry <= 0 or len(cs) < cfg.min_completed_1m:
        return None

    start = _opening_price(cs)
    last = cs[-1].close
    vw = vwap(cs)
    stock_ret = 100.0 * (last / start - 1.0)
    gap = _gap_pct(start, previous_close)
    rs = side * (stock_ret - market_return)
    srs = side * (stock_ret - sector_return)
    impulse = side * stock_ret
    move_atr = abs(last - start) / a
    vw_dist = side * (last - vw) / a
    eff = efficiency(cs)
    recent_move = side * (last - cs[-4].close) / a if len(cs) >= 4 else side * (last - cs[0].close) / a
    directional_score = clamp(
        0.5
        + 0.20 * impulse / max(1.0, abs(impulse))
        + 0.20 * clamp(recent_move / 2.0)
        + 0.10 * clamp(vw_dist / 2.0)
    )
    rs_score = clamp(0.5 + 0.15 * rs + 0.10 * srs)
    structure_score = clamp(0.5 * clamp(eff) + 0.5 * directional_score)

    if side == 1:
        structural_low = min(c.low for c in cs[-5:])
        stop = min(structural_low - 0.20 * a, entry - 0.35 * a)
    else:
        structural_high = max(c.high for c in cs[-5:])
        stop = max(structural_high + 0.20 * a, entry + 0.35 * a)

    risk = abs(entry - stop)
    if risk <= 0:
        return None
    target_distance = max(cfg.minimum_rr * risk, cfg.minimum_target_atr * a)
    target = entry + side * target_distance

    penalty = 0.0
    if move_atr > cfg.max_impulse_atr:
        penalty += 10.0
    if abs(vw_dist) > cfg.max_extension_from_vwap_atr:
        penalty += 8.0

    score = 20.0 + 35.0 * directional_score + 25.0 * rs_score + 20.0 * structure_score - penalty
    reasons = (
        "FORCED_ENTRY_TIER_3",
        "PATTERN_GATES_BYPASSED_AFTER_TIER_1_2_FAILURE",
        "PREVIOUS_CLOSE_NOT_REQUIRED_FOR_SIGNAL",
        f"session_candles={len(cs)}",
        f"directional_score={directional_score:.3f}",
        f"gap={'NA' if previous_close is None else f'{gap:+.3f}%'}",
        f"move_atr={move_atr:.3f}",
        f"rs_market={rs:+.3f}%",
        f"rs_sector={srs:+.3f}%",
        f"vw_dist_atr={vw_dist:+.3f}",
    )
    return Candidate(
        symbol=symbol, side=name, score=score, entry=entry, stop=stop, target=target,
        gap_pct=gap, impulse_pct=impulse, impulse_atr=move_atr,
        retracement_depth=0.0, retracement_volume_ratio=1.0,
        rs_market=rs, rs_sector=srs, vwap_distance_atr=vw_dist,
        structure=structure_score, atr_value=a, retracement_level=last,
        tier=3, reasons=reasons,
    )


def _build_candidate(
    symbol: str,
    side: int,
    name: str,
    cs: list[Candle],
    entry: float,
    previous_close: float | None,
    market_return: float,
    sector_return: float,
    gap_z: float,
    cfg: StrategyConfig,
    tier: int,
):
    if tier == 3:
        return _emergency_candidate(symbol, side, name, cs, entry, previous_close, market_return, sector_return, cfg)

    a = atr(cs, cfg.atr_period)
    if a <= 0:
        return None
    start = _opening_price(cs)
    stock_ret = 100.0 * (cs[-1].close / start - 1.0)
    gap = _gap_pct(start, previous_close)
    vw = vwap(cs)
    leg = _leg(cs, side, cfg)
    if not leg:
        return None

    idx, extreme, retrace, depth, reclaim, impulse_pct, opening_price = leg
    impulse_atr = abs(extreme - opening_price) / a
    rs = side * (stock_ret - market_return)
    srs = side * (stock_ret - sector_return)
    impulse_bars = cs[: idx + 1]
    retrace_bars = cs[idx + 1 :]
    impulse_volume = sum(c.volume for c in impulse_bars) / max(len(impulse_bars), 1)
    retrace_volume = sum(c.volume for c in retrace_bars) / max(len(retrace_bars), 1)
    vol_ratio = retrace_volume / max(impulse_volume, 1e-12)
    eff = efficiency(impulse_bars)
    vw_dist = side * (cs[-1].close - vw) / a
    extension = abs(cs[-1].close - vw) / a
    persistence = sum(1 for c in cs[1:] if side * (c.close - c.open) > 0) / max(len(cs) - 1, 1)

    # Previous close/gap is deliberately informational only. It can never
    # reject or penalize a signal because the live Psygrid schema may omit it.
    _ = gap
    _ = gap_z
    if impulse_pct < cfg.min_impulse_pct:
        return None
    if _hard_exhaustion(idx, impulse_atr, a, cs[-1].close, extreme, vw_dist, cfg):
        return None

    strict = tier == 1
    if strict:
        if not cfg.min_retracement_depth <= depth <= cfg.max_retracement_depth:
            return None
        if reclaim < cfg.min_reclaim_ratio:
            return None
        if eff < cfg.min_directional_efficiency:
            return None
        if vol_ratio > cfg.max_retracement_volume_ratio:
            return None
        if persistence < cfg.min_persistence:
            return None
        if cfg.require_vwap_confirmation and vw_dist <= 0:
            return None
    else:
        if not cfg.fallback_retrace_min <= depth <= cfg.fallback_retrace_max:
            return None
        if reclaim < cfg.fallback_reclaim_min:
            return None
        if eff < cfg.fallback_efficiency_min:
            return None
        if vol_ratio > cfg.fallback_volume_ratio_max:
            return None
        if persistence < cfg.fallback_persistence_min:
            return None
        if vw_dist < -cfg.fallback_vwap_tolerance_atr:
            return None
        if extension > cfg.fallback_extension_atr_max:
            return None

    si = scale(impulse_pct, cfg.min_impulse_pct, 1.5)
    sr = clamp(1.0 - abs(depth - 0.52) / 0.22)
    sm = scale(rs, 0.10, 0.80)
    ssr = scale(srs, 0.05, 0.60)
    sv = scale(1.0 - vol_ratio, 0.0, 0.50)
    sw = scale(max(vw_dist, 0.0), 0.0, 1.25)
    st = 0.5 * clamp(eff) + 0.5 * clamp(reclaim)
    sx = scale(impulse_atr, 0.5, 2.5)
    score = 100.0 * (
        cfg.w_impulse * si
        + cfg.w_retracement * sr
        + cfg.w_relative_strength * sm
        + cfg.w_sector_strength * ssr
        + cfg.w_volume * sv
        + cfg.w_vwap * sw
        + cfg.w_structure * st
        + cfg.w_volatility * sx
    )
    if tier > 1:
        score -= 8.0 * (tier - 1)

    stop = retrace - cfg.stop_atr_buffer * a if side == 1 else retrace + cfg.stop_atr_buffer * a
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    risk_pct = 100.0 * risk / entry
    if not cfg.minimum_risk_pct <= risk_pct <= cfg.maximum_risk_pct:
        return None
    target_distance = max(cfg.minimum_rr * risk, cfg.minimum_target_atr * a)
    target = entry + side * target_distance
    reasons = (
        "STRICT" if tier == 1 else f"FALLBACK_TIER_{tier}",
        "PREVIOUS_CLOSE_NOT_REQUIRED_FOR_SIGNAL",
        f"session_candles={len(cs)}",
        f"depth={depth:.3f}",
        f"reclaim={reclaim:.3f}",
        f"impulse_atr={impulse_atr:.3f}",
        f"vol_ratio={vol_ratio:.3f}",
        f"persistence={persistence:.3f}",
        f"vw_dist_atr={vw_dist:.3f}",
    )
    return Candidate(
        symbol=symbol, side=name, score=score, entry=entry, stop=stop, target=target,
        gap_pct=gap, impulse_pct=impulse_pct, impulse_atr=impulse_atr,
        retracement_depth=depth, retracement_volume_ratio=vol_ratio,
        rs_market=rs, rs_sector=srs, vwap_distance_atr=vw_dist,
        structure=st, atr_value=a, retracement_level=retrace,
        tier=tier, reasons=reasons,
    )


def evaluate(
    symbol: str,
    candles: Iterable[Candle],
    entry: float,
    previous_close: float | None,
    market_return: float,
    sector_return: float,
    gap_history: Iterable[float],
    cfg: StrategyConfig,
    tier: int = 1,
):
    cs = session_candles(candles)
    if len(cs) < cfg.min_completed_1m or any(not finite_ohlcv(c) for c in cs):
        return None
    if entry <= 0:
        return None

    if tier == 3:
        longs = _emergency_candidate(symbol, 1, "LONG", cs, entry, previous_close, market_return, sector_return, cfg)
        shorts = _emergency_candidate(symbol, -1, "SHORT", cs, entry, previous_close, market_return, sector_return, cfg)
        return max((c for c in (longs, shorts) if c is not None), key=lambda c: (c.score, c.side == "LONG"), default=None)

    # gap_history is retained only for API compatibility with earlier builds.
    _ = gap_history
    best = None
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        candidate = _build_candidate(
            symbol, side, name, cs, entry, previous_close,
            market_return, sector_return, 0.0, cfg, tier,
        )
        if candidate is not None and (best is None or candidate.score > best.score):
            best = candidate
    return best


def evaluate_tiers(
    symbol: str,
    candles: Iterable[Candle],
    entry: float,
    previous_close: float | None,
    market_return: float,
    sector_return: float,
    gap_history: Iterable[float],
    cfg: StrategyConfig,
) -> list[Candidate]:
    out: list[Candidate] = []
    for tier in (1, 2, 3):
        candidate = evaluate(
            symbol, candles, entry, previous_close,
            market_return, sector_return, gap_history, cfg, tier=tier,
        )
        if candidate is not None:
            out.append(candidate)
    return out


def rank(candidates: Iterable[Candidate], side: str) -> Candidate | None:
    xs = [c for c in candidates if c.side == side]
    return sorted(
        xs,
        key=lambda c: (-c.score, c.tier, -c.rs_market, -c.rs_sector, -c.vwap_distance_atr, c.symbol),
    )[0] if xs else None
