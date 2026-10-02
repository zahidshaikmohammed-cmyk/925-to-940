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
    """Return today's regular-session candles only.

    Pre-open records (for example 09:09) can exist in Psygrid. They are never
    allowed into the intraday signal geometry.
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


def _pivot_start(cs: list[Candle], idx: int, side: int, cfg: StrategyConfig) -> float:
    """Return a deterministic pre-impulse pivot for a later-session leg."""
    lookback = min(8, max(3, idx))
    window = cs[max(0, idx - lookback):idx]
    if not window:
        return _opening_price(cs)
    return min(c.low for c in window) if side == 1 else max(c.high for c in window)


def _leg(cs: list[Candle], side: int, cfg: StrategyConfig):
    """Find a completed impulse followed by a meaningful pullback.

    The search is intraday and rolling: later scans are not frozen to the
    absolute session high/low. The 09:15 open anchors the opening leg; later
    legs use a local pre-impulse pivot. A genuine deep retracement is preferred
    over a merely recent shallow pullback.
    """
    n = len(cs)
    if n < cfg.min_impulse_bars + cfg.min_retracement_bars:
        return None

    opening = _opening_price(cs)
    first = max(cfg.min_impulse_bars - 1, n - 36)
    last = n - cfg.min_retracement_bars - 1
    candidates = []

    for idx in range(first, last + 1):
        start = opening if idx <= cfg.late_extreme_bar else _pivot_start(cs, idx, side, cfg)
        extreme = cs[idx].high if side == 1 else cs[idx].low
        if extreme <= start if side == 1 else extreme >= start:
            continue

        den = abs(extreme - start)
        if den <= 0:
            continue
        local_atr = atr(cs[max(0, idx - cfg.atr_period + 1):idx + 1], cfg.atr_period)
        impulse_pct = side * 100.0 * (extreme / start - 1.0)
        impulse_atr = den / max(local_atr, 1e-12)
        if impulse_pct < cfg.min_impulse_pct or impulse_atr > cfg.max_impulse_atr:
            continue

        post = cs[idx + 1:]
        if len(post) < cfg.min_retracement_bars:
            continue
        retrace = min(c.low for c in post) if side == 1 else max(c.high for c in post)
        depth = abs(extreme - retrace) / den
        if not 0.25 <= depth <= 0.85:
            continue

        if side == 1:
            reclaim = (cs[-1].close - retrace) / max(extreme - retrace, 1e-12)
        else:
            reclaim = (retrace - cs[-1].close) / max(retrace - extreme, 1e-12)

        candidates.append((idx, extreme, retrace, depth, reclaim, impulse_pct, impulse_atr, start))

    if not candidates:
        return None

    return sorted(
        candidates,
        key=lambda x: (
            0 if cfg.min_retracement_depth <= x[3] <= cfg.max_retracement_depth else 1,
            abs(x[3] - 0.52),
            -x[0],
            -x[6],
        ),
    )[0]


def _gap_pct(start: float, previous_close: float | None) -> float:
    if previous_close is None or previous_close <= 0:
        return 0.0
    return 100.0 * (start / previous_close - 1.0)


def _risk_target(entry: float, stop: float, side: int, a: float, cfg: StrategyConfig):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    risk_pct = 100.0 * risk / entry
    if not cfg.minimum_risk_pct <= risk_pct <= cfg.maximum_risk_pct:
        return None
    target = entry + side * max(cfg.minimum_rr * risk, cfg.minimum_target_atr * a)
    return risk, target


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
    has_sector: bool = True,
) -> Candidate | None:
    """Tier 3: always rank a healthy stock, but never pretend it has a setup."""
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
    vw_dist = side * (last - vw) / a

    leg = _leg(cs, side, cfg)
    if leg:
        idx, extreme, retrace, depth, reclaim, impulse_pct, impulse_atr, leg_start = leg
        setup_label = "RECENT_IMPULSE_RETRACEMENT_WEAK"
        retracement_score = clamp(1.0 - abs(depth - 0.52) / 0.52)
        retracement_level = retrace
    else:
        idx = -1
        depth = 0.0
        reclaim = 0.0
        impulse_atr = 0.0
        impulse_pct = side * stock_ret
        setup_label = "NO_QUALIFYING_RETRACEMENT"
        retracement_score = 0.0
        retracement_level = last

    recent = cs[-12:]
    recent_move = side * (recent[-1].close - recent[0].open) / a
    recent_eff = efficiency(recent)
    persistence = sum(1 for c in recent[1:] if side * (c.close - c.open) > 0) / max(len(recent) - 1, 1)

    momentum_score = scale(recent_move, 0.25, 2.5)
    rs_score = scale(rs, -0.50, 2.00)
    srs_score = scale(srs, -0.50, 2.00)
    vwap_score = scale(vw_dist, -0.50, 1.50)
    structure_score = 0.55 * clamp(recent_eff) + 0.45 * persistence

    extension_penalty = (
        22.0 * scale(abs(recent_move), 2.0, 3.5)
        + 18.0 * scale(abs(vw_dist), 1.75, 3.0)
    )
    no_retrace_penalty = 20.0 if not leg else 0.0

    if has_sector:
        score = 100.0 * (
            0.22 * momentum_score
            + 0.20 * retracement_score
            + 0.20 * rs_score
            + 0.10 * srs_score
            + 0.12 * vwap_score
            + 0.16 * structure_score
        ) - extension_penalty - no_retrace_penalty
    else:
        # No genuine sector data for this stock: sector_return silently
        # equalled market_return, which would make srs a duplicate of rs
        # rather than an independent signal. Drop that term instead of
        # scoring a fabricated "sector edge", and redistribute its 0.10
        # weight proportionally across the remaining (already 0.90-summed)
        # terms so a fairly-scored stock is compared on equal footing.
        score = 100.0 * (
            0.22 * momentum_score
            + 0.20 * retracement_score
            + 0.20 * rs_score
            + 0.12 * vwap_score
            + 0.16 * structure_score
        ) / 0.90 - extension_penalty - no_retrace_penalty
    if not leg:
        score = min(score, 58.0)

    if side == 1:
        structural_low = min(c.low for c in cs[-5:])
        stop = min(structural_low - 0.20 * a, entry - 0.35 * a)
    else:
        structural_high = max(c.high for c in cs[-5:])
        stop = max(structural_high + 0.20 * a, entry + 0.35 * a)
    rt = _risk_target(entry, stop, side, a, cfg)
    if rt is None:
        return None
    _, target = rt

    reasons = (
        "FORCED_ENTRY_TIER_3",
        "PATTERN_GATES_BYPASSED_AFTER_TIER_1_2_FAILURE",
        "PREVIOUS_CLOSE_NOT_REQUIRED_FOR_SIGNAL",
        f"setup={setup_label}",
        f"recent_window={len(recent)}",
        f"move_atr={abs(recent_move):.3f}",
        f"directional_move={recent_move:+.3f}ATR",
        f"retracement={depth:.3f}",
        f"reclaim={reclaim:.3f}",
        f"rs_market={rs:+.3f}%",
        f"rs_sector={srs:+.3f}%",
        f"vw_dist_atr={vw_dist:+.3f}",
        f"extension_penalty={extension_penalty:.2f}",
    ) + (() if has_sector else ("sector_data=ABSENT_WEIGHT_REDISTRIBUTED",))
    return Candidate(
        symbol=symbol,
        side=name,
        score=score,
        entry=entry,
        stop=stop,
        target=target,
        gap_pct=gap,
        impulse_pct=impulse_pct,
        impulse_atr=impulse_atr if leg else abs(recent_move),
        retracement_depth=depth,
        retracement_volume_ratio=1.0,
        rs_market=rs,
        rs_sector=srs,
        vwap_distance_atr=vw_dist,
        structure=structure_score,
        atr_value=a,
        retracement_level=retracement_level,
        tier=3,
        reasons=reasons,
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
    has_sector: bool = True,
):
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

    idx, extreme, retrace, depth, reclaim, impulse_pct, impulse_atr, opening_price = leg
    rs = side * (stock_ret - market_return)
    srs = side * (stock_ret - sector_return)
    impulse_bars = cs[max(0, idx - cfg.atr_period + 1):idx + 1]
    retrace_bars = cs[idx + 1:]
    impulse_volume = sum(c.volume for c in impulse_bars) / max(len(impulse_bars), 1)
    retrace_volume = sum(c.volume for c in retrace_bars) / max(len(retrace_bars), 1)
    vol_ratio = retrace_volume / max(impulse_volume, 1e-12)
    eff = efficiency(impulse_bars)
    vw_dist = side * (cs[-1].close - vw) / a
    extension = abs(cs[-1].close - vw) / a
    persistence = sum(1 for c in cs[1:] if side * (c.close - c.open) > 0) / max(len(cs) - 1, 1)

    _ = gap
    _ = gap_z
    if impulse_pct < cfg.min_impulse_pct:
        return None

    if tier == 1:
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
    elif tier == 2:
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
    else:
        return None

    si = scale(impulse_pct, cfg.min_impulse_pct, 1.5)
    sr = clamp(1.0 - abs(depth - 0.52) / 0.22)
    sm = scale(rs, 0.10, 0.80)
    ssr = scale(srs, 0.05, 0.60)
    sv = scale(1.0 - vol_ratio, 0.0, 0.50)
    sw = scale(max(vw_dist, 0.0), 0.0, 1.25)
    st = 0.5 * clamp(eff) + 0.5 * clamp(reclaim)
    sx = scale(impulse_atr, 0.5, 2.5)
    if has_sector:
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
    else:
        # No genuine sector data for this stock: sector_return silently
        # equalled market_return, which would make ssr a duplicate of sm
        # rather than an independent signal. Drop that term instead of
        # scoring a fabricated "sector edge", and redistribute its weight
        # proportionally across the remaining terms so a stock is never
        # scored higher merely for lacking sector classification.
        active_weight = 1.0 - cfg.w_sector_strength
        score = 100.0 * (
            cfg.w_impulse * si
            + cfg.w_retracement * sr
            + cfg.w_relative_strength * sm
            + cfg.w_volume * sv
            + cfg.w_vwap * sw
            + cfg.w_structure * st
            + cfg.w_volatility * sx
        ) / active_weight
    if tier > 1:
        score -= 8.0 * (tier - 1)

    stop = retrace - cfg.stop_atr_buffer * a if side == 1 else retrace + cfg.stop_atr_buffer * a
    rt = _risk_target(entry, stop, side, a, cfg)
    if rt is None:
        return None
    _, target = rt

    reasons = (
        "STRICT" if tier == 1 else f"FALLBACK_TIER_{tier}",
        "PREVIOUS_CLOSE_NOT_REQUIRED_FOR_SIGNAL",
        "setup=IMPULSE_RETRACEMENT",
        f"leg_index={idx}",
        f"depth={depth:.3f}",
        f"reclaim={reclaim:.3f}",
        f"impulse_atr={impulse_atr:.3f}",
        f"vol_ratio={vol_ratio:.3f}",
        f"persistence={persistence:.3f}",
        f"vw_dist_atr={vw_dist:.3f}",
    ) + (() if has_sector else ("sector_data=ABSENT_WEIGHT_REDISTRIBUTED",))
    return Candidate(
        symbol=symbol,
        side=name,
        score=score,
        entry=entry,
        stop=stop,
        target=target,
        gap_pct=gap,
        impulse_pct=impulse_pct,
        impulse_atr=impulse_atr,
        retracement_depth=depth,
        retracement_volume_ratio=vol_ratio,
        rs_market=rs,
        rs_sector=srs,
        vwap_distance_atr=vw_dist,
        structure=st,
        atr_value=a,
        retracement_level=retrace,
        tier=tier,
        reasons=reasons,
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
    has_sector: bool = True,
):
    cs = session_candles(candles)
    if len(cs) < cfg.min_completed_1m or any(not finite_ohlcv(c) for c in cs):
        return None
    if entry <= 0:
        return None

    if tier == 3:
        candidates = [
            _emergency_candidate(
                symbol, side, name, cs, entry, previous_close,
                market_return, sector_return, cfg, has_sector,
            )
            for side, name in ((1, "LONG"), (-1, "SHORT"))
        ]
        return max(
            (c for c in candidates if c is not None),
            key=lambda c: (c.score, c.side == "LONG"),
            default=None,
        )

    _ = gap_history
    candidates = [
        _build_candidate(
            symbol, side, name, cs, entry, previous_close,
            market_return, sector_return, 0.0, cfg, tier, has_sector,
        )
        for side, name in ((1, "LONG"), (-1, "SHORT"))
    ]
    return max(
        (c for c in candidates if c is not None),
        key=lambda c: (c.score, c.side == "LONG"),
        default=None,
    )


def evaluate_tiers(
    symbol: str,
    candles: Iterable[Candle],
    entry: float,
    previous_close: float | None,
    market_return: float,
    sector_return: float,
    gap_history: Iterable[float],
    cfg: StrategyConfig,
    has_sector: bool = True,
) -> list[Candidate]:
    out: list[Candidate] = []
    for tier in (1, 2, 3):
        candidate = evaluate(
            symbol, candles, entry, previous_close,
            market_return, sector_return, gap_history, cfg, tier=tier, has_sector=has_sector,
        )
        if candidate is not None:
            out.append(candidate)
    return out


def rank(candidates: Iterable[Candidate], side: str) -> Candidate | None:
    xs = [c for c in candidates if c.side == side]
    return sorted(
        xs,
        key=lambda c: (
            -c.score,
            c.tier,
            -c.rs_market,
            -c.rs_sector,
            -c.vwap_distance_atr,
            c.symbol,
        ),
    )[0] if xs else None
