"""Precision layer for run_engine: market regime, trend persistence and the final pick.

The impulse/retracement score (strategy_930) says how textbook a setup's shape is.
This module asks the question that decides whether the trade works: is the move
likely to *continue*? It uses only today's completed 1-minute candles:

* Market breadth: share of healthy stocks closing above their own VWAP. Trades
  must go with the tape (LONG_ONLY / SHORT_ONLY / NEUTRAL).
* Trend persistence (0-100), from the trade's side:
    - VWAP hold: share of recent candles that closed on the trade's side of the
      running VWAP, and how rarely price crossed it;
    - 5-minute structure: higher highs and higher lows (mirrored for shorts);
    - volume agreement: volume on candles moving the trade's way vs against it;
    - opening range: price held beyond the 09:15-09:29 range;
    - relative strength against both the market and the sector;
  minus reversal warnings: a climax candle, a rejection wick at the day's
  extreme, repeated failed breaks of the extreme, and a late-day stretched move.
* Final pick: Tier 1/2 setups that pass the regime, time-of-day and persistence
  rules, ranked by conviction = a blend of setup score and persistence.

All thresholds live in StrategyConfig. They are starting values: grade_signals.py
measures what each score band actually returns so they can be set from results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from statistics import median
from typing import Iterable, Sequence

from config import StrategyConfig
from strategy_930 import IST, Candidate, Candle, atr, clamp, scale, session_candles, vwap


def _hhmm(text: str) -> dtime:
    hh, mm = (int(x) for x in text.split(":"))
    return dtime(hh, mm)


# --------------------------------------------------------------------------- breadth

@dataclass(frozen=True)
class Breadth:
    above_vwap: float  # share of counted stocks closing above their VWAP (0..1)
    counted: int
    regime: str        # LONG_ONLY | SHORT_ONLY | NEUTRAL

    def allows(self, side: str) -> bool:
        if self.regime == "LONG_ONLY":
            return side == "LONG"
        if self.regime == "SHORT_ONLY":
            return side == "SHORT"
        return True


def market_breadth(candle_sets: Iterable[Sequence[Candle]], cfg: StrategyConfig) -> Breadth:
    above = counted = 0
    for candles in candle_sets:
        cs = session_candles(candles)
        if len(cs) < cfg.min_completed_1m:
            continue
        counted += 1
        if cs[-1].close > vwap(cs):
            above += 1
    share = above / counted if counted else 0.5
    if counted and share >= cfg.breadth_long_only:
        regime = "LONG_ONLY"
    elif counted and share <= cfg.breadth_short_only:
        regime = "SHORT_ONLY"
    else:
        regime = "NEUTRAL"
    return Breadth(share, counted, regime)


# ----------------------------------------------------------------------- persistence

@dataclass(frozen=True)
class Persistence:
    score: float           # 0..100
    vwap_hold: float       # 0..1
    vwap_crosses: int
    structure: float       # 0..1
    volume_agreement: float  # 0..1, share of directional volume moving the trade's way
    opening_range: float   # 0..1
    rs_agreement: float    # 0..1
    penalty: float         # points subtracted
    warnings: tuple[str, ...]


def running_vwap(cs: Sequence[Candle]) -> list[float]:
    out, pv, vol, tp_sum = [], 0.0, 0.0, 0.0
    for i, c in enumerate(cs, 1):
        tp = (c.high + c.low + c.close) / 3.0
        pv += tp * c.volume
        vol += c.volume
        tp_sum += tp
        out.append(pv / vol if vol else tp_sum / i)
    return out


def five_minute_bars(cs: Sequence[Candle]) -> list[tuple[float, float]]:
    """(high, low) of each 5-minute bucket, in order."""
    buckets: dict[tuple[int, int], list[float]] = {}
    for c in cs:
        t = c.ts.astimezone(IST)
        key = (t.hour, t.minute // 5)
        hl = buckets.get(key)
        if hl is None:
            buckets[key] = [c.high, c.low]
        else:
            hl[0] = max(hl[0], c.high)
            hl[1] = min(hl[1], c.low)
    return [(h, l) for (h, l) in buckets.values()]


def _structure(cs: Sequence[Candle], side: int, pairs: int) -> float:
    bars = five_minute_bars(cs)[-(pairs + 1):]
    if len(bars) < 3:
        return 0.5
    points = 0.0
    for (h0, l0), (h1, l1) in zip(bars, bars[1:]):
        points += 0.5 * (side * (h1 - h0) > 0) + 0.5 * (side * (l1 - l0) > 0)
    return points / (len(bars) - 1)


def _opening_range(cs: Sequence[Candle], side: int, cfg: StrategyConfig) -> float:
    end = _hhmm(cfg.opening_range_end)
    rng = [c for c in cs if c.ts.astimezone(IST).time() < end]
    if len(rng) < 10 or len(rng) == len(cs):
        return 0.5  # range not complete yet: neutral
    hi, lo = max(c.high for c in rng), min(c.low for c in rng)
    last = cs[-1].close
    if hi <= lo:
        return 0.5
    position = (last - lo) / (hi - lo)  # <0 below the range, >1 above it
    if side == -1:
        position = 1.0 - position
    return clamp(position)


def persistence(
    candles: Sequence[Candle], side: str, rs_market: float, rs_sector: float,
    has_sector: bool, cfg: StrategyConfig,
) -> Persistence:
    s = -1 if side == "SHORT" else 1
    cs = session_candles(candles)
    if len(cs) < cfg.min_completed_1m:
        return Persistence(0.0, 0, 0, 0, 0, 0, 0, 0, ("insufficient_candles",))
    a = atr(cs, cfg.atr_period) or 1e-12
    vw = running_vwap(cs)

    window = cs[-cfg.persistence_window_bars:]
    vw_window = vw[-len(window):]
    signs = [s * (c.close - v) for c, v in zip(window, vw_window)]
    vwap_hold = sum(1 for x in signs if x > 0) / len(signs)
    recent = signs[-cfg.vwap_cross_window_bars:]
    crosses = sum(1 for x, y in zip(recent, recent[1:]) if (x > 0) != (y > 0))

    structure = _structure(cs, s, cfg.structure_pairs)

    flow = cs[-cfg.vwap_cross_window_bars:]
    with_vol = sum(c.volume for c in flow if s * (c.close - c.open) > 0)
    against_vol = sum(c.volume for c in flow if s * (c.close - c.open) < 0)
    volume_agreement = with_vol / (with_vol + against_vol) if with_vol + against_vol else 0.5

    opening_range = _opening_range(cs, s, cfg)

    rs_part = scale(rs_market, 0.0, 1.5)
    rs_agreement = 0.5 * rs_part + 0.5 * scale(rs_sector, 0.0, 1.0) if has_sector else rs_part

    warnings: list[str] = []
    penalty = 0.0
    med_vol = median(c.volume for c in cs) or 1.0
    for c in cs[-3:]:
        if c.volume >= cfg.climax_volume_multiple * med_vol and (c.high - c.low) >= cfg.climax_range_atr * a:
            warnings.append("climax_candle")
            penalty += cfg.penalty_climax
            break
    extreme = max(c.high for c in cs) if s == 1 else min(c.low for c in cs)
    for c in cs[-3:]:
        rng = c.high - c.low
        wick = (c.high - max(c.open, c.close)) if s == 1 else (min(c.open, c.close) - c.low)
        at_extreme = abs((c.high if s == 1 else c.low) - extreme) <= 0.25 * a
        if rng >= a and wick >= cfg.rejection_wick_ratio * rng and at_extreme:
            warnings.append("rejection_wick_at_extreme")
            penalty += cfg.penalty_rejection
            break
    failed = 0
    best = cs[0].high if s == 1 else cs[0].low
    start = max(1, len(cs) - cfg.vwap_cross_window_bars)
    for i, c in enumerate(cs[1:], 1):
        if i >= start:
            probe = c.high if s == 1 else c.low
            if s * (probe - best) >= -0.10 * a and s * (best - c.close) >= 0.50 * a:
                failed += 1
        best = max(best, c.high) if s == 1 else min(best, c.low)
    if failed >= cfg.failed_break_count:
        warnings.append(f"failed_breaks_{failed}")
        penalty += cfg.penalty_failed_breaks
    now = cs[-1].ts.astimezone(IST).time()
    day_move = s * 100.0 * (cs[-1].close / cs[0].open - 1.0)
    if now >= _hhmm(cfg.late_day_after) and day_move >= cfg.late_day_extended_pct:
        warnings.append("late_day_extended")
        penalty += cfg.penalty_late_extended

    raw = 100.0 * (
        cfg.p_vwap_hold * vwap_hold
        + cfg.p_vwap_crosses * (1.0 - min(crosses, 6) / 6.0)
        + cfg.p_structure * structure
        + cfg.p_volume * scale(volume_agreement, 0.5, 0.75)
        + cfg.p_opening_range * opening_range
        + cfg.p_relative_strength * rs_agreement
    )
    return Persistence(
        score=clamp(raw - penalty, 0.0, 100.0),
        vwap_hold=vwap_hold,
        vwap_crosses=crosses,
        structure=structure,
        volume_agreement=volume_agreement,
        opening_range=opening_range,
        rs_agreement=rs_agreement,
        penalty=penalty,
        warnings=tuple(warnings),
    )


# ------------------------------------------------------------------------ the pick

@dataclass(frozen=True)
class Pick:
    candidate: Candidate
    persistence: Persistence
    conviction: float
    blockers: tuple[str, ...]  # empty = tradable

    @property
    def tradable(self) -> bool:
        return not self.blockers


def entry_window(now: datetime, cfg: StrategyConfig) -> tuple[bool, str, float]:
    """(entries allowed, reason if not, extra persistence required now)."""
    t = now.astimezone(IST).time()
    if t < _hhmm(cfg.no_entry_before):
        return False, f"no entries before {cfg.no_entry_before}", 0.0
    if t >= _hhmm(cfg.no_entry_after):
        return False, f"no new entries after {cfg.no_entry_after}", 0.0
    if _hhmm(cfg.lunch_start) <= t < _hhmm(cfg.lunch_end):
        return True, "", cfg.lunch_extra_persistence
    return True, "", 0.0


def day_move_pct(candles: Sequence[Candle], c: Candidate) -> float:
    """Today's move in the trade's direction, from the previous close when known (else the open)."""
    cs = session_candles(candles)
    if not cs or cs[0].open <= 0:
        return 0.0
    side = -1 if c.side == "SHORT" else 1
    from_prev_close = (1.0 + c.gap_pct / 100.0) * (cs[-1].close / cs[0].open) - 1.0
    return side * 100.0 * from_prev_close


def conviction(c: Candidate, p: Persistence, cfg: StrategyConfig) -> float:
    return cfg.conviction_setup_weight * c.score + (1.0 - cfg.conviction_setup_weight) * p.score


def rank_picks(
    candidates: Iterable[Candidate],
    candles_by_symbol: dict[str, Sequence[Candle]],
    has_sector: dict[str, bool],
    breadth: Breadth,
    now: datetime,
    cfg: StrategyConfig,
) -> list[Pick]:
    """Every Tier 1/2 candidate with its persistence, conviction and blockers.

    Tradable picks come first (highest conviction first), then near-misses.
    """
    allowed, window_reason, extra = entry_window(now, cfg)
    required = cfg.min_trend_persistence + extra + (cfg.neutral_extra_persistence if breadth.regime == "NEUTRAL" else 0.0)
    picks: list[Pick] = []
    for c in candidates:
        if c.tier > 2:
            continue
        candles = candles_by_symbol.get(c.symbol) or ()
        p = persistence(candles, c.side, c.rs_market, c.rs_sector, has_sector.get(c.symbol, False), cfg)
        blockers: list[str] = []
        moved = day_move_pct(candles, c)
        if moved >= cfg.max_day_move_pct:
            blockers.append(f"already_moved_{moved:+.1f}%_today")
        if not allowed:
            blockers.append(window_reason)
        if not breadth.allows(c.side):
            blockers.append(f"against_market_{breadth.regime}")
        if c.score < cfg.min_signal_score:
            blockers.append(f"setup_score_{c.score:.0f}<{cfg.min_signal_score:.0f}")
        if p.score < required:
            blockers.append(f"persistence_{p.score:.0f}<{required:.0f}")
        picks.append(Pick(c, p, conviction(c, p, cfg), tuple(blockers)))
    return sorted(picks, key=lambda k: (not k.tradable, -k.conviction, k.candidate.tier, k.candidate.symbol, k.candidate.side))
