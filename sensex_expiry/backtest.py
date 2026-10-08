"""Event-driven expiry-day backtester and validation statistics (spec sections 27-35).

Fill model (deliberately pessimistic, spec 24):
  entry  = option OPEN of the bar after the signal bar + half spread + slippage
  exits  = see position.py; stops fill at min(stop, bar open) - slippage
  no option print in the fill bar -> the entry is cancelled (no fill is assumed)
History contains no bid/ask, so the half spread is modelled as
max(tick, spread_pct x price / 2) and stressed in the sensitivity runs.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from .config import EngineConfig
from .engine import StrategyEngine
from .features import atr
from .models import Action, Candle, Direction, OptionQuote, PriorDay, Reason
from .options import choose_strike
from .position import Position, manage, open_position, realized
from .quality import assess
from .session_calendar import at


@dataclass
class DayData:
    day: date
    prior: PriorDay | None
    is_expiry: bool
    expiry: date | None
    underlying: list[Candle]
    options: dict[tuple[int, str], dict[datetime, Candle]]
    lot_size: int = 20
    tags: dict = field(default_factory=dict)      # e.g. gap, vol regime, convention


@dataclass
class Trade:
    day: date
    setup: str
    direction: str
    entry_ts: datetime
    exit_ts: datetime
    entry: float
    exit: float
    qty: int
    one_r: float
    r_gross: float
    r_net: float
    mfe_r: float
    exit_reason: str
    hold_min: float
    tags: dict


@dataclass
class BacktestResult:
    trades: list[Trade]
    decisions: int
    no_trade_reasons: dict[str, int]


def _quote_fn(day: DayData, bar_start: datetime, spread_pct: float, tick: float):
    def q(strike: int, right: str) -> OptionQuote | None:
        c = day.options.get((strike, right), {}).get(bar_start)
        if c is None:
            return None
        half = max(tick, spread_pct * c.close / 2)
        return OptionQuote(strike, right, c.close, round(c.close - half, 2), round(c.close + half, 2),
                           bar_start + timedelta(minutes=1))
    return q


def run_day(cfg: EngineConfig, day: DayData, spread_pct: float = 0.005, capital: float | None = None,
            trace: list | None = None) -> tuple[list[Trade], dict[str, int], int]:
    session_open = at(day.day, cfg.session.market_open)
    eng = StrategyEngine(cfg, day.day, session_open, day.prior, day.is_expiry, day.expiry, day.lot_size)
    tick = cfg.risk.tick_size
    slip = cfg.risk.slippage_ticks * tick
    trades: list[Trade] = []
    reasons: dict[str, int] = {}
    decisions = 0
    pos: Position | None = None
    pending_entry = None
    c = day.underlying
    for t in range(len(c)):
        bar = c[t]
        hist = c[:t + 1]
        if pending_entry is not None:
            dec, plan = pending_entry
            pending_entry = None
            ob = day.options.get((plan["strike"], plan["right"]), {}).get(bar.start)
            if ob is not None and bar.start.time() < cfg.session.hard_flat:
                half = max(tick, spread_pct * ob.open / 2)
                fill = round(ob.open + half + slip, 2)
                cand = dec.candidate
                pos = open_position(cfg, cand.key, cand.setup, cand.direction, plan["strike"], plan["right"],
                                    plan["qty"], bar.start, fill, plan["stop_frac"], cand.invalidation,
                                    plan["atr"] or 1.0, cand.trigger_price)
                eng.on_entry(cand.key)
            else:
                reasons["ENTRY_NOT_FILLED"] = reasons.get("ENTRY_NOT_FILLED", 0) + 1
        if pos is not None and pos.open:
            ob = day.options.get((pos.strike, pos.right), {}).get(bar.start)
            if ob is not None:
                # everything we can sell at is on the bid: shift the bar down by the half spread
                half = max(tick, spread_pct * ob.open / 2)
                ob = Candle(ob.start, ob.open - half, ob.high - half, ob.low - half, ob.close - half, ob.volume)
            manage(cfg, pos, bar, ob, atr(hist, cfg.features.atr_period, day.prior.close if day.prior else None))
            if not pos.open:
                trades.append(_trade(cfg, day, pos))
                eng.risk_state.record_exit(trades[-1].r_net, bar.start + timedelta(minutes=1))
                pos = None
        if pos is None and pending_entry is None:
            q = assess(cfg.data, bar.start, hist, None, session_open, backtest=True)
            dec = eng.evaluate_entry(hist, bar.start + timedelta(minutes=1), q,
                                     _quote_fn(day, bar.start, spread_pct, tick), capital)
            decisions += 1
            if trace is not None:
                trace.append(dec.payload)
            if dec.action is Action.BUY:
                p = dec.payload
                pending_entry = (dec, {"strike": p["strike"], "right": p["right"], "qty": p["qty"],
                                       "stop_frac": p["stop_frac"], "atr": p.get("atr")})
            else:
                for r in dec.reasons:
                    reasons[r.value] = reasons.get(r.value, 0) + 1
    if pos is not None and pos.open:
        # safety net: data ended with a position open. Mark at the last print, flagged.
        last = max((k for k in day.options.get((pos.strike, pos.right), {})), default=None)
        px = day.options[(pos.strike, pos.right)][last].close if last else 0.05
        pos.exit_ts, pos.exit_price, pos.exit_reason = c[-1].start, round(max(px - slip, 0.05), 2), Reason.EXIT_DATA_FAILURE
        trades.append(_trade(cfg, day, pos))
    return trades, reasons, decisions


def _trade(cfg: EngineConfig, day: DayData, pos: Position) -> Trade:
    r = realized(cfg, pos)
    return Trade(day.day, pos.setup, pos.direction.value, pos.entry_ts, pos.exit_ts, pos.entry_price, pos.exit_price,
                 pos.qty, round(pos.one_r, 2), r["r_gross"], r["r_net"], r["mfe_r"], pos.exit_reason.value,
                 (pos.exit_ts - pos.entry_ts).total_seconds() / 60, dict(day.tags))


def run(cfg: EngineConfig, days: list[DayData], spread_pct: float = 0.005) -> BacktestResult:
    trades, reasons, n = [], {}, 0
    for d in sorted(days, key=lambda x: x.day):
        tr, rs, k = run_day(cfg, d, spread_pct)
        trades += tr
        n += k
        for key, v in rs.items():
            reasons[key] = reasons.get(key, 0) + v
    return BacktestResult(trades, n, reasons)


# ---------------------------------------------------------------- statistics

def metrics(trades: list[Trade], key: str = "r_net") -> dict:
    rs = [getattr(t, key) for t in trades]
    n = len(rs)
    if n == 0:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    down = [min(0.0, r) for r in rs]
    dsd = math.sqrt(sum(x * x for x in down) / n)
    srt = sorted(rs)

    def pct(p):
        return srt[min(n - 1, max(0, int(round(p * (n - 1)))))]
    top5 = sorted(rs, reverse=True)[:5]
    return {
        "n": n, "win_rate": len(wins) / n, "avg_win_r": statistics.mean(wins) if wins else 0.0,
        "avg_loss_r": statistics.mean(losses) if losses else 0.0, "expectancy_r": statistics.mean(rs),
        "median_r": statistics.median(rs), "p25": pct(.25), "p75": pct(.75), "p90": pct(.90), "p95": pct(.95),
        "best_r": srt[-1], "worst_r": srt[0], "total_r": sum(rs),
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else math.inf,
        "sharpe_per_trade": statistics.mean(rs) / sd if sd > 0 else 0.0,
        "sortino_per_trade": statistics.mean(rs) / dsd if dsd > 0 else 0.0,
        "max_drawdown_r": mdd, "recovery_factor": sum(rs) / mdd if mdd > 0 else math.inf,
        "worst_losing_streak": worst,
        "expectancy_ex_top5_r": statistics.mean(sorted(rs)[:-5]) if n > 5 else float("nan"),
        "share_of_profit_from_top5": (sum(top5) / sum(rs)) if sum(rs) > 0 else float("nan"),
        "avg_hold_min": statistics.mean(t.hold_min for t in trades),
        "avg_mfe_r": statistics.mean(t.mfe_r for t in trades),
    }


def breakdown(trades: list[Trade], by) -> dict:
    groups: dict = {}
    for t in trades:
        groups.setdefault(by(t), []).append(t)
    return {k: metrics(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


def bootstrap_mean_ci(rs: list[float], n_boot: int = 5000, seed: int = 7, alpha: float = 0.05) -> tuple[float, float, float]:
    """(lower, upper, P(mean <= 0)) of the per-trade mean R."""
    if not rs:
        return (float("nan"), float("nan"), 1.0)
    rnd = random.Random(seed)
    n = len(rs)
    means = sorted(statistics.mean(rnd.choices(rs, k=n)) for _ in range(n_boot))
    lo = means[int(alpha / 2 * n_boot)]
    hi = means[int((1 - alpha / 2) * n_boot) - 1]
    return lo, hi, sum(m <= 0 for m in means) / n_boot


def monte_carlo(rs: list[float], n_paths: int = 5000, horizon: int | None = None, risk_pct: float = 0.005,
                ruin_dd_pct: float = 0.20, seed: int = 11) -> dict:
    """Resample trade sequences with replacement. Ruin = equity drawdown of ruin_dd_pct
    (default 20%) at fixed-fractional risk_pct per trade."""
    if not rs:
        return {}
    rnd = random.Random(seed)
    h = horizon or len(rs)
    finals, dds, streaks, recov = [], [], [], []
    ruined = 0
    for _ in range(n_paths):
        eq, peak, mdd, cur, worst, since_peak, longest = 1.0, 1.0, 0.0, 0, 0, 0, 0
        for r in rnd.choices(rs, k=h):
            eq *= (1 + risk_pct * r)
            if eq >= peak:
                peak, since_peak = eq, 0
            else:
                since_peak += 1
                longest = max(longest, since_peak)
            mdd = max(mdd, 1 - eq / peak)
            cur = cur + 1 if r <= 0 else 0
            worst = max(worst, cur)
        finals.append(eq - 1)
        dds.append(mdd)
        streaks.append(worst)
        recov.append(longest)
        ruined += mdd >= ruin_dd_pct
    finals.sort()
    dds.sort()
    streaks.sort()

    def q(xs, p):
        return xs[min(len(xs) - 1, int(p * len(xs)))]
    return {"paths": n_paths, "horizon": h, "mean_return": statistics.mean(finals), "median_return": q(finals, .5),
            "p05_return": q(finals, .05), "p95_return": q(finals, .95), "median_dd": q(dds, .5),
            "dd_95": q(dds, .95), "dd_99": q(dds, .99), "worst_dd": dds[-1], "losing_streak_95": q(streaks, .95),
            "longest_underwater_trades_95": sorted(recov)[int(.95 * len(recov))],
            "prob_ruin": ruined / n_paths, "best_path": finals[-1], "worst_path": finals[0]}


def permutation_vs_baseline(a: list[float], b: list[float], n_perm: int = 5000, seed: int = 3) -> float:
    """One-sided p-value that mean(a) > mean(b) arises by chance."""
    if not a or not b:
        return 1.0
    rnd = random.Random(seed)
    obs = statistics.mean(a) - statistics.mean(b)
    pool = a + b
    hits = 0
    for _ in range(n_perm):
        rnd.shuffle(pool)
        if statistics.mean(pool[:len(a)]) - statistics.mean(pool[len(a):]) >= obs:
            hits += 1
    return hits / n_perm


def walk_forward_folds(days: list[date], train: int, validate: int, test: int) -> list[dict]:
    """Rolling folds over ORDERED trading days: train -> validate -> test, rolled by `test`."""
    d = sorted(days)
    out, i = [], 0
    while i + train + validate + test <= len(d):
        out.append({"train": d[i:i + train], "validate": d[i + train:i + train + validate],
                    "test": d[i + train + validate:i + train + validate + test]})
        i += test
    return out


def random_baseline(cfg: EngineConfig, days: list[DayData], trades: list[Trade], seed: int = 5,
                    spread_pct: float = 0.005) -> list[Trade]:
    """Same days, same number of entries, same time distribution, RANDOM direction and
    the same exit engine with a stop of the strategy's median underlying distance.
    A setup that cannot beat this has no entry edge (spec 29)."""
    rnd = random.Random(seed)
    by_day: dict[date, list[Trade]] = {}
    for t in trades:
        by_day.setdefault(t.day, []).append(t)
    out: list[Trade] = []
    for d in days:
        if d.day not in by_day:
            continue
        for real in by_day[d.day]:
            c = d.underlying
            idx = [i for i, x in enumerate(c) if x.start == real.entry_ts]
            if not idx:
                continue
            i = max(1, min(len(c) - 2, idx[0] + rnd.randint(-15, 15)))
            direction = rnd.choice([Direction.LONG, Direction.SHORT])
            a = atr(c[:i], cfg.features.atr_period) or 1.0
            spot = c[i - 1].close
            strike, right = choose_strike(spot, direction, cfg.options)
            ob = d.options.get((strike, right), {}).get(c[i].start)
            if ob is None:
                continue
            fill = ob.open + max(cfg.risk.tick_size, spread_pct * ob.open / 2) + cfg.risk.slippage_ticks * cfg.risk.tick_size
            inval = spot - direction.sign * 2.0 * a
            pos = open_position(cfg, f"rnd{i}", "RANDOM", direction, strike, right, real.qty, c[i].start, fill,
                                0.35, inval, a, spot)
            for j in range(i + 1, len(c)):
                o = d.options.get((strike, right), {}).get(c[j].start)
                manage(cfg, pos, c[j], o, atr(c[:j + 1], cfg.features.atr_period))
                if not pos.open:
                    break
            if pos.open:
                last = max(d.options.get((strike, right), {}), default=None)
                px = d.options[(strike, right)][last].close if last else 0.05
                pos.exit_ts, pos.exit_price, pos.exit_reason = c[-1].start, px, Reason.EXIT_DATA_FAILURE
            out.append(_trade(cfg, d, pos))
    return out


def sensitivity(cfg: EngineConfig, days: list[DayData]) -> dict:
    """Cost and slippage stress (spec 31): the edge must survive 1.5x costs and 2x slippage."""
    out = {}
    for name, c2, sp in (("base", cfg, 0.005), ("spread_2x", cfg, 0.010),
                         ("slippage_2x", replace(cfg, risk=replace(cfg.risk, slippage_ticks=cfg.risk.slippage_ticks * 2)), 0.005),
                         ("costs_1.5x", replace(cfg, costs=replace(cfg.costs, brokerage_per_order=cfg.costs.brokerage_per_order * 1.5,
                                                                    stt_sell_premium=cfg.costs.stt_sell_premium * 1.5,
                                                                    exchange_txn_premium=cfg.costs.exchange_txn_premium * 1.5)), 0.005)):
        res = run(c2, days, sp)
        out[name] = metrics(res.trades)
    return out
