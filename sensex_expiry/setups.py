"""The three pre-registered setups (spec section 15). Every setup is a HYPOTHESIS.

Each detector is STATELESS: it recomputes from the closed candles c[0..t] and fires
only on the bar t that first completes the pattern. Statelessness makes live and
backtest identical by construction and makes look-ahead testable by truncation.

All triggers are CLOSE-based (bar t closes beyond the trigger level). The order is
sent after bar t closes and the backtest fills it at bar t+1's option open plus
spread and slippage. Intrabar triggers were rejected: 1-minute history cannot say
whether a stop-entry would have filled before or after the stop in the same bar.

Long logic is written once. Shorts run the same code on price-mirrored candles
(p -> -p, high <-> low), so the two sides cannot drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import EngineConfig
from .features import Levels, atr, compression_ratio, is_displacement, opening_range, reference_levels
from .models import Candle, Direction, PriorDay, Reason, SetupCandidate


def mirror(c: list[Candle]) -> list[Candle]:
    return [Candle(x.start, -x.open, -x.low, -x.high, -x.close, x.volume, x.ticks) for x in c]


def mirror_prior(p: PriorDay | None) -> PriorDay | None:
    return None if p is None else PriorDay(p.day, -p.low, -p.high, -p.close)


def _unmirror_name(name: str) -> str:
    table = {"PDH": "PDL", "PDL": "PDH", "ORH": "ORL", "ORL": "ORH", "BOX_HIGH": "BOX_LOW"}
    if name in table:
        return table[name]
    if name.startswith("SWH"):
        return "SWL" + name[3:]
    if name.startswith("SWL"):
        return "SWH" + name[3:]
    return name


@dataclass
class _Raw:
    setup: str
    trigger: float
    invalidation: float
    level_name: str
    level: float
    room_level: float | None
    reasons: list[Reason]
    confirmations: dict[str, bool]


LEVEL_PRIORITY = ("PDL", "ORL", "SWL")


def _room(levels: Levels, close: float, a: float, exclude: float) -> float | None:
    for name, price, _ in levels.above(close + 0.1 * a):
        if abs(price - exclude) > 0.1 * a:
            return price
    return None


def _sweep_long(c: list[Candle], session_open: datetime, prior: PriorDay | None, cfg: EngineConfig) -> _Raw | None:
    sc, fc = cfg.setups, cfg.features
    t = len(c) - 1
    a = atr(c, fc.atr_period, prior.close if prior else None)
    if a is None or t < 2:
        return None
    levels = reference_levels(c, session_open, prior, fc)
    cands = [x for x in levels.items if x[0].startswith(LEVEL_PRIORITY)]
    cands.sort(key=lambda x: next(i for i, p in enumerate(LEVEL_PRIORITY) if x[0].startswith(p)))
    for r in range(t - 1, max(t - 1 - sc.sweep_trigger_bars, 0), -1):
        # bar t must be the FIRST close above the reclaim bar's high
        if not c[t].close > c[r].high or any(c[j].close > c[r].high for j in range(r + 1, t)):
            continue
        for name, lv, born in cands:
            if not c[r].close > lv + sc.sweep_reclaim_buffer_atr * a:
                continue
            for s in range(r, max(r - sc.sweep_reclaim_bars, 0), -1):
                if s < 1 or born > s - sc.level_min_age_bars:
                    continue
                if not c[s].low < lv - sc.sweep_min_pen_atr * a:
                    continue
                if c[s - 1].close < lv:          # price must arrive from above: a sweep, not a breakdown
                    continue
                if any(c[j].close > lv + sc.sweep_reclaim_buffer_atr * a for j in range(s, r)):
                    continue                      # r must be the first reclaim
                extreme = min(x.low for x in c[s:r + 1])
                if extreme < lv - sc.sweep_max_pen_atr * a:
                    continue                      # too deep: that was a breakdown
                if c[r].close - extreme < sc.sweep_min_rejection_atr * a:
                    continue
                inval = extreme - sc.stop_buffer_atr * a
                confl = any(abs(p - lv) <= 0.25 * a and n != name for n, p, _ in levels.items)
                disp = is_displacement(c[r], a, fc, 1) or is_displacement(c[t], a, fc, 1)
                return _Raw("S1_SWEEP_RECLAIM", c[t].close, inval, name, lv, _room(levels, c[t].close, a, lv),
                            [Reason.SWEEP_DETECTED, Reason.LEVEL_RECLAIMED],
                            {"displacement": disp, "level_confluence": confl})
    return None


def _orb_long(c: list[Candle], session_open: datetime, prior: PriorDay | None, cfg: EngineConfig) -> _Raw | None:
    sc, fc = cfg.setups, cfg.features
    t = len(c) - 1
    orng = opening_range(c, session_open, fc.or_minutes)
    a = atr(c, fc.atr_period, prior.close if prior else None)
    if orng is None or a is None:
        return None
    orh = orng[0]
    first = next((i for i, x in enumerate(c) if (x.start - session_open).total_seconds() >= fc.or_minutes * 60), None)
    if first is None:
        return None
    n = sc.orb_accept_closes
    acc = None
    for i in range(first + n - 1, t + 1):
        if all(c[j].close > orh + sc.orb_accept_buffer_atr * a for j in range(i - n + 1, i + 1)):
            acc = i
            break
    if acc is None or acc >= t:
        return None
    for q in range(acc + 1, min(acc + sc.orb_retest_bars, t - 1) + 1):
        if c[q].low <= orh + sc.orb_retest_tol_atr * a and c[q].close > orh:
            # trigger: first close above the retest bar's high within 3 bars, acceptance intact
            if any(c[j].close < orh for j in range(acc, t + 1)):
                return None
            if t - q > 3 or not c[t].close > c[q].high or any(c[j].close > c[q].high for j in range(q + 1, t)):
                return None
            inval = min(c[q].low, orh) - sc.stop_buffer_atr * a
            levels = reference_levels(c, session_open, prior, fc)
            return _Raw("S2_ORB_ACCEPT", c[t].close, inval, "ORH", orh, _room(levels, c[t].close, a, orh),
                        [Reason.ORB_ACCEPTANCE, Reason.ORB_RETEST_HELD],
                        {"displacement": is_displacement(c[t], a, fc, 1), "level_confluence": False})
    return None


def _compression_long(c: list[Candle], session_open: datetime, prior: PriorDay | None, cfg: EngineConfig) -> _Raw | None:
    sc, fc = cfg.setups, cfg.features
    n = fc.compression_window
    t = len(c) - 1
    if t < n + 5:
        return None
    pc = prior.close if prior else None
    a_prev = atr(c[:-1], 60, pc)
    a = atr(c, fc.atr_period, pc)
    box = c[t - n:t]
    comp = compression_ratio(box, n, a_prev)
    if comp is None or a is None or comp > fc.compression_max:
        return None
    hi, lo = max(x.high for x in box), min(x.low for x in box)
    if not (c[t].close > hi + sc.s3_break_buffer_atr * a and is_displacement(c[t], a, fc, 1)):
        return None
    base = (hi + lo) / 2 if sc.s3_stop == "MID" else lo
    inval = base - sc.stop_buffer_atr * a
    levels = reference_levels(c, session_open, prior, fc)
    return _Raw("S3_LATE_COMPRESSION", c[t].close, inval, "BOX_HIGH", hi, _room(levels, c[t].close, a, hi),
                [Reason.COMPRESSION_BREAK, Reason.VOLATILITY_EXPANSION, Reason.DISPLACEMENT],
                {"displacement": True, "level_confluence": False})


DETECTORS = {
    "S1_SWEEP_RECLAIM": _sweep_long,
    "S2_ORB_ACCEPT": _orb_long,
    "S3_LATE_COMPRESSION": _compression_long,
}


def detect(c: list[Candle], session_open: datetime, prior: PriorDay | None, cfg: EngineConfig) -> list[SetupCandidate]:
    """All setups that fire on the last closed bar, both directions."""
    out: list[SetupCandidate] = []
    if not c:
        return out
    t_time = c[-1].start.time()
    mc, mp = mirror(c), mirror_prior(prior)
    for name in cfg.setups.enabled:
        fn = DETECTORS[name]
        if name == "S2_ORB_ACCEPT" and t_time > cfg.session.s2_last_entry:
            continue
        if name == "S3_LATE_COMPRESSION" and not (cfg.session.s3_window_start <= t_time <= cfg.session.s3_window_end):
            continue
        for direction, data, pr in ((Direction.LONG, c, prior), (Direction.SHORT, mc, mp)):
            raw = fn(data, session_open, pr, cfg)
            if raw is None:
                continue
            s = direction.sign
            out.append(SetupCandidate(
                setup=raw.setup, direction=direction, bar_ts=c[-1].start,
                trigger_price=s * raw.trigger, invalidation=s * raw.invalidation,
                level_name=raw.level_name if s > 0 else _unmirror_name(raw.level_name),
                level=s * raw.level, room_level=None if raw.room_level is None else s * raw.room_level,
                reasons=list(raw.reasons), confirmations=dict(raw.confirmations)))
    return out
