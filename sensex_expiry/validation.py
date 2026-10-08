"""Real-data baseline validation (Phase 5-6). NOTHING here tunes the strategy.

The locked EngineConfig() runs unchanged. This module only measures it:

  TEST A  underlying signal on ALL trading days (expiry gate bypassed BY DESIGN, option
          gates not applicable). R = underlying points / stop distance, no costs: it
          measures whether the entry and exit logic has information, not money.
  TEST B  option P&L on expiry days with the full locked engine, costs and slippage.
  TEST C  each setup separately (S1, S2, S3), for A and B.
  TEST D  each time window.            TEST E  each market regime at signal time.

Controls (each a NULL the strategy must beat, never tuned):
  C1 RANDOM_ENTRY       random day/time in the entry window, random side, stop = median
                        strategy stop in ATR units, same exit engine
  C2 RANDOM_DIRECTION   same entry bar as each strategy trade, coin-flip side, same stop
  C3 RANDOM_SAME_HOLD   same day, random time and side, held exactly as long as the
                        paired strategy trade, no stop
  C4 RANDOM_SAME_STOP   same day, random time and side, the paired trade's exact stop
  C5 SIMPLE_ATM_BUY     (options) every expiry day 09:35: ATM CE if price above the open,
                        else PE; 30% premium stop; flat 15:10
  C6 SIMPLE_ORB         first close beyond OR15 (09:30-12:00); stop at the other side
  C7 SIMPLE_BREAKOUT    first close beyond prior-day high/low (09:35-14:45); 1.5 ATR stop
  C8 BUY_AND_HOLD       long 09:35 open -> 15:10; R unit = 1.5 ATR at entry

Random controls are repeated `n_null` times with different seeds; the strategy's mean R
is compared against that whole null distribution (randomization p-value).
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import backtest as bt
from .config import EngineConfig
from .engine import StrategyEngine
from .features import atr, opening_range
from .models import Candle, Direction, Reason
from .options import choose_strike
from .position import manage, open_position
from .quality import assess
from .session_calendar import at

WINDOWS = (("09:15", "09:30"), ("09:30", "10:00"), ("10:00", "11:00"), ("11:00", "12:00"), ("12:00", "13:00"),
           ("13:00", "14:00"), ("14:00", "14:30"), ("14:30", "15:10"))
REGIME_GROUPS = {
    "Trend": ("STRONG_BULL", "STRONG_BEAR"), "Range": ("RANGE",), "Compression": ("COMPRESSION",),
    "Expansion": ("EXPANSION",), "Reversal": ("REVERSAL", "FAILED_BREAKOUT"), "Unclear": ("UNCLEAR",),
}
SETUPS = ("S1_SWEEP_RECLAIM", "S2_ORB_ACCEPT", "S3_LATE_COMPRESSION")


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


# ------------------------------------------------------------------ underlying exit engine

@dataclass
class UTrade:
    """Same attribute names as backtest.Trade so metrics()/breakdown() work on both."""
    day: date
    setup: str
    direction: str
    entry_ts: datetime
    exit_ts: datetime
    entry: float
    exit: float
    qty: int
    one_r: float             # stop distance in index points
    r_gross: float
    r_net: float
    mfe_r: float
    exit_reason: str
    hold_min: float
    tags: dict
    mae_r: float = 0.0
    costs: float = 0.0
    slippage: float = 0.0
    regime: str = ""
    signal_ts: datetime | None = None


def simulate_underlying(cfg: EngineConfig, c: list[Candle], i: int, direction: Direction, risk_pts: float,
                        invalidation: float | None, prior_close: float | None, *, rules: bool = True,
                        hold_bars: int | None = None, intrabar_stop: float | None = None) -> dict | None:
    """Enter at c[i].open. rules=True applies the locked exit order translated to the index:
      intrabar stop at entry -/+ 1.2 x stop distance (the premium stop's delta translation)
      until +1R MFE, then at entry (the breakeven ratchet); close-based exits: hard flat,
      invalidation, 2-ATR chandelier after +1R, 15-bar time stop below +0.5R, 60-bar max hold;
      close-based exits fill at the next bar's open.
    rules=False: hold exactly `hold_bars` bars (or to hard flat), optional intrabar stop."""
    if i >= len(c) or risk_pts <= 0 or c[i].start.time() >= cfg.session.hard_flat:
        return None
    s = direction.sign
    ex = cfg.exits
    entry = c[i].open
    stop = intrabar_stop if intrabar_stop is not None else (entry - s * 1.2 * risk_pts if rules else None)
    best_close, mfe, mae = entry, 0.0, 0.0
    pending, held = None, 0
    exit_px = exit_ts = reason = None
    for j in range(i, len(c)):
        b = c[j]
        if pending is not None:
            exit_px, exit_ts, reason = b.open, b.start, pending
            break
        held += 1
        fav = (b.high - entry) if s > 0 else (entry - b.low)
        adv = (entry - b.low) if s > 0 else (b.high - entry)
        mae = max(mae, adv / risk_pts)
        if stop is not None and ((s > 0 and b.low <= stop) or (s < 0 and b.high >= stop)):
            exit_px = min(stop, b.open) if s > 0 else max(stop, b.open)
            exit_ts, reason = b.start, "EXIT_STOP"
            break
        mfe = max(mfe, fav / risk_pts)
        if rules and mfe >= ex.breakeven_at_r:
            stop = max(stop, entry) if s > 0 else min(stop, entry)
        if s * (b.close - best_close) > 0:
            best_close = b.close
        a = atr(c[:j + 1], cfg.features.atr_period, prior_close)
        if (b.start + timedelta(minutes=1)).time() >= cfg.session.hard_flat:
            pending = "EXIT_HARD_FLAT"
        elif not rules:
            if hold_bars is not None and held >= hold_bars:
                pending = "EXIT_HOLD_DONE"
        elif invalidation is not None and s * (b.close - invalidation) < 0:
            pending = "EXIT_INVALIDATION"
        elif ex.policy == "TRAIL" and mfe >= ex.trail_after_r and a and s * (b.close - (best_close - s * ex.trail_atr_mult * a)) < 0:
            pending = "EXIT_TRAIL"
        elif held >= ex.time_stop_bars and mfe < ex.time_stop_min_r:
            pending = "EXIT_TIME_STOP"
        elif held >= ex.max_hold_bars:
            pending = "EXIT_MAX_HOLD"
        if ex.policy in ("FIXED_2R", "FIXED_3R") and rules and pending is None:
            k = 2.0 if ex.policy == "FIXED_2R" else 3.0
            if mfe >= k:
                exit_px, exit_ts, reason = entry + s * k * risk_pts, b.start, "EXIT_TARGET"
                break
    if exit_px is None:
        exit_px, exit_ts, reason = c[-1].close, c[-1].start, "EXIT_END_OF_DATA"
    r = s * (exit_px - entry) / risk_pts
    return {"entry_ts": c[i].start, "exit_ts": exit_ts, "entry": entry, "exit": exit_px, "r": r, "mfe_r": mfe,
            "mae_r": -mae, "reason": reason, "hold_min": (exit_ts - c[i].start).total_seconds() / 60}


def _ut(day, setup, direction, sim, risk_pts, tags, regime="", signal_ts=None) -> UTrade:
    return UTrade(day.day, setup, direction.value, sim["entry_ts"], sim["exit_ts"], sim["entry"], sim["exit"], 0,
                  risk_pts, round(sim["r"], 4), round(sim["r"], 4), round(sim["mfe_r"], 4), sim["reason"],
                  sim["hold_min"], tags, mae_r=round(sim["mae_r"], 4), regime=regime, signal_ts=signal_ts)


# ------------------------------------------------------------------ TEST A

def run_underlying_day(cfg: EngineConfig, day: bt.DayData) -> list[UTrade]:
    """The locked signal engine on one day, every gate except expiry and option checks."""
    session_open = at(day.day, cfg.session.market_open)
    eng = StrategyEngine(cfg, day.day, session_open, day.prior, True, day.expiry, day.lot_size)
    c = day.underlying
    pc = day.prior.close if day.prior else None
    out: list[UTrade] = []
    busy_until = -1
    tags = dict(day.tags) | {"is_expiry": day.is_expiry}
    for t in range(len(c) - 1):
        if t <= busy_until:
            continue
        q = assess(cfg.data, c[t].start, c[:t + 1], None, session_open, backtest=True)
        blocked, ctx = eng.evaluate_signal(c[:t + 1], c[t].start + timedelta(minutes=1), q, require_expiry=False)
        if blocked is not None:
            continue
        cand = ctx["cand"]
        sim = simulate_underlying(cfg, c, t + 1, cand.direction, cand.stop_distance, cand.invalidation, pc)
        if sim is None:
            continue
        eng.on_entry(cand.key)
        tr = _ut(day, cand.setup, cand.direction, sim, cand.stop_distance, tags, ctx["extra"]["regime"], c[t].start)
        out.append(tr)
        eng.risk_state.record_exit(tr.r_net, sim["exit_ts"] + timedelta(minutes=1))
        busy_until = next((k for k, x in enumerate(c) if x.start >= sim["exit_ts"]), len(c))
    return out


def _map(fn, items, workers: int):
    if workers <= 1:
        return [fn(x) for x in items]
    with ProcessPoolExecutor(workers) as pool:
        return list(pool.map(fn, items, chunksize=4))


class _DayRunner:
    def __init__(self, cfg, kind):
        self.cfg, self.kind = cfg, kind

    def __call__(self, day):
        if self.kind == "A":
            return run_underlying_day(self.cfg, day)
        return bt.run_day(self.cfg, day)[0]


def test_a(cfg: EngineConfig, days: list[bt.DayData], workers: int = 1) -> list[UTrade]:
    res = _map(_DayRunner(cfg, "A"), sorted(days, key=lambda d: d.day), workers)
    return [t for day_trades in res for t in day_trades]


def test_b(cfg: EngineConfig, days: list[bt.DayData], workers: int = 1) -> list[bt.Trade]:
    exp = [d for d in sorted(days, key=lambda d: d.day) if d.is_expiry and d.options]
    res = _map(_DayRunner(cfg, "B"), exp, workers)
    return [t for day_trades in res for t in day_trades]


# ------------------------------------------------------------------ controls (underlying)

def _entry_window_idx(cfg, c):
    return [i for i in range(1, len(c)) if cfg.session.no_entry_before <= c[i].start.time() <= cfg.session.last_entry]


def control_random_entry(cfg, days, trades, rnd) -> list[UTrade]:
    """C1: same NUMBER of trades, random days/times/sides, stop = median strategy stop in ATR."""
    if not trades:
        return []
    ratios = []
    by_day = {d.day: d for d in days}
    for t in trades:
        d = by_day[t.day]
        i = next(k for k, x in enumerate(d.underlying) if x.start == t.entry_ts)
        a = atr(d.underlying[:i], cfg.features.atr_period, d.prior.close if d.prior else None)
        if a:
            ratios.append(t.one_r / a)
    k_atr = statistics.median(ratios) if ratios else 1.5
    out = []
    pool = [d for d in days if len(d.underlying) > 60]
    for _ in range(len(trades)):
        d = rnd.choice(pool)
        c = d.underlying
        i = rnd.choice(_entry_window_idx(cfg, c))
        a = atr(c[:i], cfg.features.atr_period, d.prior.close if d.prior else None) or 1.0
        side = rnd.choice((Direction.LONG, Direction.SHORT))
        dist = k_atr * a
        sim = simulate_underlying(cfg, c, i, side, dist, c[i - 1].close - side.sign * dist, d.prior.close if d.prior else None)
        if sim:
            out.append(_ut(d, "C1_RANDOM_ENTRY", side, sim, dist, d.tags))
    return out


def _paired(cfg, days, trades, rnd, mode) -> list[UTrade]:
    by_day = {d.day: d for d in days}
    out = []
    for t in trades:
        d = by_day[t.day]
        c = d.underlying
        pc = d.prior.close if d.prior else None
        i0 = next(k for k, x in enumerate(c) if x.start == t.entry_ts)
        if mode == "C2_RANDOM_DIRECTION":
            i, side = i0, rnd.choice((Direction.LONG, Direction.SHORT))
            sim = simulate_underlying(cfg, c, i, side, t.one_r, c[i - 1].close - side.sign * t.one_r, pc)
        else:
            i = rnd.choice(_entry_window_idx(cfg, c))
            side = rnd.choice((Direction.LONG, Direction.SHORT))
            if mode == "C3_RANDOM_SAME_HOLD":
                sim = simulate_underlying(cfg, c, i, side, t.one_r, None, pc, rules=False,
                                          hold_bars=max(1, int(round(t.hold_min))))
            else:   # C4_RANDOM_SAME_STOP
                sim = simulate_underlying(cfg, c, i, side, t.one_r, c[i - 1].close - side.sign * t.one_r, pc)
        if sim:
            out.append(_ut(d, mode, side, sim, t.one_r, d.tags))
    return out


def control_simple_orb(cfg, days) -> list[UTrade]:
    out = []
    for d in days:
        c = d.underlying
        so = at(d.day, cfg.session.market_open)
        for i in range(15, len(c) - 1):
            if c[i].start.time() > time(12, 0):
                break
            orng = opening_range(c[:i + 1], so, 15)
            if orng is None:
                continue
            hi, lo = orng
            side = Direction.LONG if c[i].close > hi else Direction.SHORT if c[i].close < lo else None
            if side is None:
                continue
            stop = lo if side is Direction.LONG else hi
            dist = abs(c[i].close - stop)
            sim = simulate_underlying(cfg, c, i + 1, side, dist, None, None, rules=False, intrabar_stop=stop)
            if sim and dist > 0:
                out.append(_ut(d, "C6_SIMPLE_ORB", side, sim, dist, d.tags))
            break
    return out


def control_simple_breakout(cfg, days) -> list[UTrade]:
    out = []
    for d in days:
        if d.prior is None:
            continue
        c = d.underlying
        pc = d.prior.close
        for i in _entry_window_idx(cfg, c)[:-1]:
            side = Direction.LONG if c[i].close > d.prior.high else Direction.SHORT if c[i].close < d.prior.low else None
            if side is None:
                continue
            a = atr(c[:i + 1], cfg.features.atr_period, pc) or 1.0
            dist = 1.5 * a
            stop = c[i].close - side.sign * dist
            sim = simulate_underlying(cfg, c, i + 1, side, dist, None, pc, rules=False, intrabar_stop=stop)
            if sim:
                out.append(_ut(d, "C7_SIMPLE_BREAKOUT", side, sim, dist, d.tags))
            break
    return out


def control_buy_hold(cfg, days) -> list[UTrade]:
    out = []
    for d in days:
        c = d.underlying
        i = next((k for k, x in enumerate(c) if x.start.time() >= cfg.session.no_entry_before), None)
        if i is None:
            continue
        a = atr(c[:i], cfg.features.atr_period, d.prior.close if d.prior else None) or 1.0
        sim = simulate_underlying(cfg, c, i, Direction.LONG, 1.5 * a, None, None, rules=False)
        if sim:
            tr = _ut(d, "C8_BUY_AND_HOLD", Direction.LONG, sim, 1.5 * a, d.tags)
            tr.tags = dict(tr.tags) | {"pct": (sim["exit"] / sim["entry"] - 1) * 100}
            out.append(tr)
    return out


# ------------------------------------------------------------------ controls (options, test B)

def _option_sim(cfg, d, i, side, stop_frac, invalidation, qty, spread_pct, setup, rules=True):
    c = d.underlying
    spot = c[i - 1].close
    strike, right = choose_strike(spot, side, cfg.options)
    series = d.options.get((strike, right), {})
    ob = series.get(c[i].start)
    if ob is None:
        return None
    tick, slip = cfg.risk.tick_size, cfg.risk.slippage_ticks * cfg.risk.tick_size
    half = max(tick, spread_pct * ob.open / 2)
    pos = open_position(cfg, f"{setup}{i}", setup, side, strike, right, qty, c[i].start, round(ob.open + half + slip, 2),
                        stop_frac, invalidation, atr(c[:i], cfg.features.atr_period) or 1.0, spot)
    pos.log.append({"event": "META", "regime": "", "signal_ts": c[i - 1].start, "entry_half_spread": half,
                    "spread_pct": spread_pct})
    for j in range(i, len(c)):
        o = series.get(c[j].start)
        if o is not None:
            h = max(tick, spread_pct * o.open / 2)
            o = Candle(o.start, o.open - h, o.high - h, o.low - h, o.close - h, o.volume)
        if rules:
            manage(cfg, pos, c[j], o, atr(c[:j + 1], cfg.features.atr_period))
        else:   # simple control: premium stop + hard flat only
            if o is None:
                continue
            pos.bars_held += 1
            pos.peak_premium = max(pos.peak_premium, o.high)
            pos.trough_premium = min(pos.trough_premium, o.low)
            if o.low <= pos.stop:
                pos.exit_ts, pos.exit_price, pos.exit_reason = o.start, round(max(min(pos.stop, o.open) - slip, 0.05), 2), Reason.EXIT_STOP_PREMIUM
            elif (c[j].start + timedelta(minutes=1)).time() >= cfg.session.hard_flat:
                pos.exit_ts, pos.exit_price, pos.exit_reason = o.start, round(max(o.close - slip, 0.05), 2), Reason.EXIT_HARD_FLAT
        if not pos.open:
            break
    if pos.open:
        last = max(series, default=None)
        pos.exit_ts, pos.exit_price, pos.exit_reason = c[-1].start, (series[last].close if last else 0.05), Reason.EXIT_DATA_FAILURE
    return bt._trade(cfg, d, pos)


def option_controls(cfg, days, trades, rnd, spread_pct=0.005) -> dict[str, list]:
    exp = {d.day: d for d in days if d.is_expiry and d.options}
    out = {"C1_RANDOM_ENTRY": [], "C2_RANDOM_DIRECTION": [], "C4_RANDOM_SAME_STOP": []}
    for t in trades:
        d = exp.get(t.day)
        if d is None:
            continue
        c = d.underlying
        i0 = next((k for k, x in enumerate(c) if x.start == t.entry_ts), None)
        if i0 is None:
            continue
        a = atr(c[:i0], cfg.features.atr_period) or 1.0
        for mode in out:
            side = rnd.choice((Direction.LONG, Direction.SHORT))
            if mode == "C2_RANDOM_DIRECTION":
                i = i0
            elif mode == "C1_RANDOM_ENTRY":
                d2 = rnd.choice(list(exp.values()))
                c2 = d2.underlying
                i = rnd.choice(_entry_window_idx(cfg, c2))
                tr = _option_sim(cfg, d2, i, side, 0.30, c2[i - 1].close - side.sign * 1.5 * (atr(c2[:i], 14) or 1.0),
                                 t.qty, spread_pct, mode)
                if tr:
                    out[mode].append(tr)
                continue
            else:
                i = rnd.choice(_entry_window_idx(cfg, c))
            dist = 1.5 * a
            tr = _option_sim(cfg, d, i, side, 0.30, c[i - 1].close - side.sign * dist, t.qty, spread_pct, mode)
            if tr:
                out[mode].append(tr)
    return out


def control_simple_atm_buy(cfg, days, qty=20, spread_pct=0.005) -> list:
    out = []
    for d in days:
        if not (d.is_expiry and d.options):
            continue
        c = d.underlying
        i = next((k for k, x in enumerate(c) if x.start.time() >= cfg.session.no_entry_before), None)
        if i is None:
            continue
        side = Direction.LONG if c[i - 1].close > c[0].open else Direction.SHORT
        tr = _option_sim(cfg, d, i, side, 0.30, None, qty, spread_pct, "C5_SIMPLE_ATM_BUY", rules=False)
        if tr:
            out.append(tr)
    return out


# ------------------------------------------------------------------ statistics

def full_metrics(trades) -> dict:
    m = bt.metrics(trades)
    if m.get("n", 0) == 0:
        return m
    m_g = bt.metrics(trades, "r_gross")
    m.update({
        "gross_expectancy_r": m_g["expectancy_r"], "net_expectancy_r": m["expectancy_r"],
        "avg_mae_r": statistics.mean(t.mae_r for t in trades),
        "total_costs_rs": round(sum(t.costs for t in trades), 2),
        "total_slippage_rs": round(sum(t.slippage for t in trades), 2),
        "total_risk_rs": round(sum(t.one_r for t in trades if t.qty), 2),
    })
    return m


def t_ci(rs, alpha=0.05) -> tuple[float, float]:
    n = len(rs)
    if n < 2:
        return (float("nan"), float("nan"))
    se = statistics.stdev(rs) / math.sqrt(n)
    z = 1.96 if alpha == 0.05 else 2.576
    return statistics.mean(rs) - z * se, statistics.mean(rs) + z * se


def significance(rs: list[float]) -> dict:
    if len(rs) < 2:
        return {"n": len(rs), "note": "too few trades for any inference"}
    lo, hi, p_le0 = bt.bootstrap_mean_ci(rs)
    sd = statistics.stdev(rs)
    mean = statistics.mean(rs)
    tl, th = t_ci(rs)
    # one-sided test of mean > 0 by sign-flip randomization (exact for symmetric null)
    rnd = random.Random(17)
    hits = sum(1 for _ in range(5000) if statistics.mean([x * rnd.choice((-1, 1)) for x in rs]) >= mean)
    return {"n": len(rs), "mean_r": mean, "sd_r": sd, "t_ci95": [tl, th], "bootstrap_ci95": [lo, hi],
            "prob_mean_gt_0": 1 - p_le0, "cohens_d": mean / sd if sd > 0 else 0.0,
            "p_signflip_mean_gt_0": hits / 5000,
            "n_needed_for_0.15R_80pct_power": math.ceil(((1.645 + 0.842) * sd / 0.15) ** 2) if sd > 0 else None,
            "n_needed_for_observed_effect": (math.ceil(((1.645 + 0.842) * sd / mean) ** 2) if mean > 0 and sd > 0 else None)}


def null_distribution(fn, n_null: int, seed: int) -> list[float]:
    means = []
    for k in range(n_null):
        tr = fn(random.Random(seed + k))
        if tr:
            means.append(statistics.mean(t.r_net for t in tr))
    return means


def randomization_p(strategy_mean: float, null_means: list[float]) -> float:
    if not null_means:
        return float("nan")
    return (1 + sum(m >= strategy_mean for m in null_means)) / (1 + len(null_means))


def holm(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted((p, k) for k, p in pvals.items() if p == p)
    m, out, running = len(items), {}, 0.0
    for i, (p, k) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def benjamini_hochberg(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted((p, k) for k, p in pvals.items() if p == p)
    m, out = len(items), {}
    prev = 1.0
    for rank in range(m, 0, -1):
        p, k = items[rank - 1]
        prev = min(prev, p * m / rank)
        out[k] = prev
    return out


def by_window(trades) -> dict:
    out = {}
    for a, b in WINDOWS:
        sel = [t for t in trades if _t(a) <= (t.signal_ts or t.entry_ts).time() < _t(b)]
        out[f"{a}-{b}"] = full_metrics(sel)
    return out


def by_regime(trades) -> dict:
    return {g: full_metrics([t for t in trades if t.regime in members]) for g, members in REGIME_GROUPS.items()}


def chronological_halves(trades, frac=0.7) -> dict:
    days = sorted({t.day for t in trades})
    if not days:
        return {}
    cut = days[int(len(days) * frac)] if len(days) > 1 else days[0]
    return {"first_70pct_days": full_metrics([t for t in trades if t.day < cut]),
            "last_30pct_days": full_metrics([t for t in trades if t.day >= cut]), "cut_day": str(cut)}


# ------------------------------------------------------------------ look-ahead audit

def lookahead_audit(cfg: EngineConfig, days: list[bt.DayData], sample: int = 8, seed: int = 1) -> dict:
    """Run on the REAL dataset. Each check must pass or the baseline is void."""
    rnd = random.Random(seed)
    exp = [d for d in days if d.is_expiry and d.options]
    pick = rnd.sample(exp, min(sample, len(exp))) if exp else []
    checks: dict[str, list] = {"truncation_equality": [], "future_poison_invariance": [], "underlying_poison_invariance": [],
                               "prior_day_is_previous_session": [], "entries_after_signal_bar": [],
                               "testA_truncation_equality": []}
    for d in pick:
        c = d.underlying
        if len(c) < 120:
            continue
        cut = rnd.randint(60, len(c) - 30)
        full, part = [], []
        bt.run_day(cfg, d, trace=full)
        bt.run_day(cfg, bt.DayData(d.day, d.prior, True, d.expiry, c[:cut], d.options, d.lot_size), trace=part)
        iso = c[cut - 1].start.isoformat()
        a = [p for p in full if p["bar_timestamp"] <= iso]
        checks["truncation_equality"].append(a == part[:len(a)])
        cut_ts = c[cut].start
        pois = {k: {ts: (x if ts < cut_ts else Candle(x.start, x.open * 5, x.high * 5, x.low * 5, x.close * 5))
                    for ts, x in s.items()} for k, s in d.options.items()}
        b = []
        bt.run_day(cfg, bt.DayData(d.day, d.prior, True, d.expiry, c, pois, d.lot_size), trace=b)
        checks["future_poison_invariance"].append([p for p in full if p["bar_timestamp"] < cut_ts.isoformat()] ==
                                                  [p for p in b if p["bar_timestamp"] < cut_ts.isoformat()])
        und = c[:cut] + [Candle(x.start, x.open * 1.05, x.high * 1.05, x.low * 1.05, x.close * 1.05) for x in c[cut:]]
        u = []
        bt.run_day(cfg, bt.DayData(d.day, d.prior, True, d.expiry, und, d.options, d.lot_size), trace=u)
        checks["underlying_poison_invariance"].append([p for p in full if p["bar_timestamp"] < cut_ts.isoformat()] ==
                                                      [p for p in u if p["bar_timestamp"] < cut_ts.isoformat()])
        ta_full = [t.entry_ts for t in run_underlying_day(cfg, d) if t.exit_ts < c[cut - 2].start]
        ta_cut = [t.entry_ts for t in run_underlying_day(cfg, bt.DayData(d.day, d.prior, d.is_expiry, d.expiry, c[:cut],
                                                                          {}, d.lot_size)) if t.exit_ts < c[cut - 2].start]
        checks["testA_truncation_equality"].append(ta_full == ta_cut)
    ordered = sorted(days, key=lambda x: x.day)
    consistency = []
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.prior is None:
            continue
        checks["prior_day_is_previous_session"].append(cur.prior.day < cur.day)
        if cur.prior.day == prev.day:
            hi, lo = max(x.high for x in prev.underlying), min(x.low for x in prev.underlying)
            consistency.append(abs(cur.prior.high / hi - 1) < 0.001 and abs(cur.prior.low / lo - 1) < 0.001)
    for d in pick:
        for t in bt.run_day(cfg, d)[0]:
            checks["entries_after_signal_bar"].append(t.signal_ts is not None and t.entry_ts > t.signal_ts)
    summary = {k: {"checked": len(v), "passed": sum(v), "ok": all(v)} for k, v in checks.items()}
    summary["ALL_PASS"] = all(v["ok"] for v in summary.values() if isinstance(v, dict) and v["checked"])
    # data consistency (not look-ahead): daily-bar prior H/L vs the previous session's minutes, 0.1% tolerance
    summary["data_consistency_prior_vs_intraday"] = {"checked": len(consistency), "passed": sum(consistency)}
    return summary


# ------------------------------------------------------------------ orchestration

def write_trades_csv(path: Path, trades) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = ["day", "setup", "direction", "regime", "signal_ts", "entry_ts", "exit_ts", "entry", "exit", "qty", "one_r",
            "r_gross", "r_net", "mfe_r", "mae_r", "hold_min", "exit_reason", "costs", "slippage"]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for t in trades:
            d = asdict(t)
            w.writerow([d.get(k) for k in keys])


@dataclass
class ValidationRun:
    cfg: EngineConfig
    days: list
    out: Path
    n_null: int = 200
    workers: int = 1
    label: str = "REAL"
    results: dict = field(default_factory=dict)

    def run(self) -> dict:
        cfg, days = self.cfg, sorted(self.days, key=lambda d: d.day)
        R: dict = {"label": self.label, "config_hash": cfg.config_hash(), "engine_version": cfg.version,
                   "optimisation": "NONE - locked EngineConfig() as committed",
                   "days_total": len(days), "expiry_days": sum(d.is_expiry for d in days),
                   "expiry_days_with_options": sum(1 for d in days if d.is_expiry and d.options),
                   "first_day": str(days[0].day) if days else None, "last_day": str(days[-1].day) if days else None}
        R["lookahead_audit"] = lookahead_audit(cfg, days)

        A = test_a(cfg, days, self.workers)
        B = test_b(cfg, days, self.workers)
        write_trades_csv(self.out / "trades_testA_underlying.csv", A)
        write_trades_csv(self.out / "trades_testB_options.csv", B)

        def block(trades):
            return {"metrics": full_metrics(trades), "significance": significance([t.r_net for t in trades])}

        R["TEST_A_all_days_underlying"] = block(A)
        R["TEST_A_expiry_days_only_underlying"] = block([t for t in A if t.tags.get("is_expiry")])
        R["TEST_B_expiry_options"] = block(B)
        R["TEST_C_setups"] = {"A": {s: block([t for t in A if t.setup == s]) for s in SETUPS},
                              "B": {s: block([t for t in B if t.setup == s]) for s in SETUPS}}
        R["TEST_D_windows"] = {"A": by_window(A), "B": by_window(B)}
        R["TEST_E_regimes"] = {"A": by_regime(A), "B": by_regime(B)}
        R["holdout_split"] = {"A": chronological_halves(A), "B": chronological_halves(B)}

        # ---- controls
        ctrl_A = {
            "C1_RANDOM_ENTRY": lambda r: control_random_entry(cfg, days, A, r),
            "C2_RANDOM_DIRECTION": lambda r: _paired(cfg, days, A, r, "C2_RANDOM_DIRECTION"),
            "C3_RANDOM_SAME_HOLD": lambda r: _paired(cfg, days, A, r, "C3_RANDOM_SAME_HOLD"),
            "C4_RANDOM_SAME_STOP": lambda r: _paired(cfg, days, A, r, "C4_RANDOM_SAME_STOP"),
        }
        mean_A = statistics.mean([t.r_net for t in A]) if A else float("nan")
        cA = {}
        for name, fn in ctrl_A.items():
            nulls = null_distribution(fn, self.n_null, 1000) if A else []
            cA[name] = {"null_mean_of_means": statistics.mean(nulls) if nulls else None,
                        "null_p95": sorted(nulls)[int(.95 * len(nulls))] if nulls else None,
                        "randomization_p": randomization_p(mean_A, nulls)}
        for name, tr in (("C6_SIMPLE_ORB", control_simple_orb(cfg, days)),
                         ("C7_SIMPLE_BREAKOUT", control_simple_breakout(cfg, days)),
                         ("C8_BUY_AND_HOLD", control_buy_hold(cfg, days))):
            m = full_metrics(tr)
            cA[name] = {"metrics": m, "perm_p_strategy_better": bt.permutation_vs_baseline([t.r_net for t in A],
                                                                                           [t.r_net for t in tr])}
            if name == "C8_BUY_AND_HOLD" and tr:
                cA[name]["mean_day_pct"] = statistics.mean(t.tags["pct"] for t in tr)
        R["controls_A"] = cA

        mean_B = statistics.mean([t.r_net for t in B]) if B else float("nan")
        cB = {}
        if B:
            nullsB = {k: [] for k in ("C1_RANDOM_ENTRY", "C2_RANDOM_DIRECTION", "C4_RANDOM_SAME_STOP")}
            for k in range(max(20, self.n_null // 5)):
                oc = option_controls(cfg, days, B, random.Random(5000 + k))
                for name, tr in oc.items():
                    if tr:
                        nullsB[name].append(statistics.mean(t.r_net for t in tr))
            for name, nulls in nullsB.items():
                cB[name] = {"null_mean_of_means": statistics.mean(nulls) if nulls else None,
                            "randomization_p": randomization_p(mean_B, nulls)}
        atm = control_simple_atm_buy(cfg, days)
        write_trades_csv(self.out / "trades_control_C5_simple_atm_buy.csv", atm)
        cB["C5_SIMPLE_ATM_BUY"] = {"metrics": full_metrics(atm),
                                   "perm_p_strategy_better": bt.permutation_vs_baseline([t.r_net for t in B],
                                                                                        [t.r_net for t in atm])}
        R["controls_B"] = cB

        # ---- multiple testing: every primary hypothesis vs its random-entry null
        fam = {}
        for s in SETUPS:
            ta = [t for t in A if t.setup == s]
            if ta:
                nulls = null_distribution(lambda r, ta=ta: control_random_entry(cfg, days, ta, r), max(50, self.n_null // 4), 2000)
                fam[f"A:{s}"] = randomization_p(statistics.mean(t.r_net for t in ta), nulls)
            tb = [t for t in B if t.setup == s]
            if tb:
                fam[f"B:{s}"] = significance([t.r_net for t in tb]).get("p_signflip_mean_gt_0", float("nan"))
        if A:
            fam["A:ALL"] = cA["C1_RANDOM_ENTRY"]["randomization_p"]
        if B:
            fam["B:ALL"] = significance([t.r_net for t in B]).get("p_signflip_mean_gt_0", float("nan"))
        R["multiple_testing"] = {"raw_p": fam, "holm_adjusted": holm(fam), "bh_fdr_adjusted": benjamini_hochberg(fam),
                                 "family_size": len(fam),
                                 "note": "every setup x test tried is in the family; nothing was selected"}
        R["verdict"] = verdict(R)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "validation_report.json").write_text(json.dumps(R, indent=2, default=str))
        (self.out / "VALIDATION_REPORT.md").write_text(render_markdown(R, A, B))
        self.results = R
        return R


def verdict(R: dict) -> dict:
    """Pre-registered decision rule. YES needs every condition; NO needs clear evidence against."""
    sB = R["TEST_B_expiry_options"]["significance"]
    sA = R["TEST_A_all_days_underlying"]["significance"]
    adj = R["multiple_testing"]["holm_adjusted"]
    reasons = []
    if not R["lookahead_audit"].get("ALL_PASS", False):
        return {"answer": "INCONCLUSIVE", "reasons": ["look-ahead audit did not pass: results void"]}
    nB = sB.get("n", 0)
    meanB = sB.get("mean_r", float("nan"))
    hiB = (sB.get("bootstrap_ci95") or [float("nan")] * 2)[1]
    loB = (sB.get("bootstrap_ci95") or [float("nan")] * 2)[0]
    yes = (nB >= 60 and loB > 0 and adj.get("B:ALL", 1) < 0.05 and adj.get("A:ALL", 1) < 0.05
           and R["controls_B"].get("C5_SIMPLE_ATM_BUY", {}).get("perm_p_strategy_better", 1) < 0.05)
    if yes:
        return {"answer": "YES", "reasons": ["all pre-registered evidence conditions met"]}
    if nB >= 30 and hiB == hiB and hiB < 0:
        reasons.append(f"option P&L 95% CI entirely below zero ({loB:.3f}, {hiB:.3f}) on {nB} trades")
        return {"answer": "NO", "reasons": reasons}
    if sA.get("n", 0) >= 100 and (sA.get("bootstrap_ci95") or [0, 0])[1] < 0:
        reasons.append("underlying signal 95% CI entirely below zero on all days: the entries have no edge")
        return {"answer": "NO", "reasons": reasons}
    reasons.append(f"option trades n={nB}, mean={meanB:.3f}R, CI=({loB:.3f}, {hiB:.3f}) includes 0 or n<60")
    need = sB.get("n_needed_for_0.15R_80pct_power")
    if need:
        reasons.append(f"~{need} option trades needed to detect +0.15R at 80% power (one-sided 5%)")
    return {"answer": "INCONCLUSIVE", "reasons": reasons}


def _fmt(v, nd=3):
    if v is None:
        return "-"
    if isinstance(v, float):
        if v != v:
            return "nan"
        if math.isinf(v):
            return "inf"
        return f"{v:.{nd}f}"
    return str(v)


COLS = ("n", "win_rate", "avg_win_r", "avg_loss_r", "expectancy_r", "profit_factor", "total_r", "max_drawdown_r",
        "avg_mae_r", "avg_mfe_r", "avg_hold_min", "best_r", "worst_r", "worst_losing_streak", "total_costs_rs",
        "total_slippage_rs", "gross_expectancy_r", "net_expectancy_r")


def _row(name, m):
    return "| " + name + " | " + " | ".join(_fmt(m.get(k)) for k in COLS) + " |"


def _table(rows: dict) -> str:
    head = "| test | " + " | ".join(COLS) + " |\n|" + "---|" * (len(COLS) + 1)
    return head + "\n" + "\n".join(_row(k, v.get("metrics", v)) for k, v in rows.items())


def render_markdown(R: dict, A, B) -> str:
    L = [f"# SENSEX Expiry Engine — Baseline Validation ({R['label']})", "",
         f"config `{R['config_hash']}` · optimisation: **{R['optimisation']}** · days {R['days_total']} "
         f"({R['first_day']} → {R['last_day']}) · expiry days {R['expiry_days']} (with options: {R['expiry_days_with_options']})",
         "", f"## VERDICT: **{R['verdict']['answer']}**", ""] + [f"- {x}" for x in R["verdict"]["reasons"]]
    L += ["", "## Look-ahead audit", "```", json.dumps(R["lookahead_audit"], indent=1), "```"]
    L += ["", "## Tests A / B (R units; A = index points / stop, no costs; B = option rupees / 1R, net of costs)", "",
          _table({"A all days": R["TEST_A_all_days_underlying"], "A expiry days": R["TEST_A_expiry_days_only_underlying"],
                  "B expiry options": R["TEST_B_expiry_options"]})]
    L += ["", "### Significance", "```", json.dumps({k: R[k]["significance"] for k in
                                                    ("TEST_A_all_days_underlying", "TEST_B_expiry_options")},
                                                   indent=1, default=str), "```"]
    for t in ("A", "B"):
        L += ["", f"## Test C — setups ({t})", "", _table(R["TEST_C_setups"][t])]
        L += ["", f"## Test D — windows by signal time ({t})", "", _table(R["TEST_D_windows"][t])]
        L += ["", f"## Test E — regime at signal ({t})", "", _table(R["TEST_E_regimes"][t])]
        L += ["", f"### Chronological 70/30 ({t})", "", _table({k: v for k, v in R["holdout_split"][t].items()
                                                              if isinstance(v, dict)})]
    L += ["", "## Controls (Test A, underlying)", "```", json.dumps(R["controls_A"], indent=1, default=str), "```",
          "", "## Controls (Test B, options)", "```", json.dumps(R["controls_B"], indent=1, default=str), "```",
          "", "## Multiple-testing correction", "```", json.dumps(R["multiple_testing"], indent=1, default=str), "```",
          "", "## Trade lists", "", "Complete lists: `trades_testA_underlying.csv`, `trades_testB_options.csv`, "
          "`trades_control_C5_simple_atm_buy.csv`.", "", "### Test B trades", "",
          "| day | setup | dir | regime | entry_ts | exit_ts | entry | exit | qty | 1R ₹ | R gross | R net | MFE | MAE | exit |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in B:
        L.append(f"| {t.day} | {t.setup} | {t.direction} | {t.regime} | {t.entry_ts:%H:%M} | {t.exit_ts:%H:%M} | "
                 f"{t.entry} | {t.exit} | {t.qty} | {t.one_r} | {t.r_gross} | {t.r_net} | {t.mfe_r} | {t.mae_r} | {t.exit_reason} |")
    return "\n".join(L) + "\n"
