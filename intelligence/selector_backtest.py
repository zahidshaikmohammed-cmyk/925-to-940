"""Historical walk-forward replay.

For each archived session, in date order:
  1. freeze the 09:45 information set (selector_data.freeze_information_set)
  2. load baselines and calibration history from STRICTLY EARLIER sessions only
  3. decide() -> immutable snapshot -> store
  4. only now reveal post-cutoff candles to the outcome evaluator -> store outcomes
  5. feature validation: per-feature Spearman IC between the 09:45 signed feature and
     each stock's forward return, stored per day
  6. store today's pre-cutoff baselines for FUTURE sessions

The v1 model has no fitted parameters, so nothing is trained on future days. Any
future fitted model must be trained inside this loop on `history` only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from statistics import mean, median

from .selector_945 import decide
from .selector_config import SelectorConfig
from .selector_data import freeze_information_set, load_session_file
from .selector_outcomes import evaluate_decision, forward_returns_universe
from .selector_store import DecisionExists, Store


@dataclass(frozen=True)
class DayResult:
    session_date: str
    symbol: str
    direction: str
    score: float
    outcomes: dict


def session_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(list(p.glob("*.json")) + list(p.glob("*.json.gz")))
        elif p.exists():
            files.append(p)
    return files


def _spearman(xs: dict, ys: dict) -> tuple[float | None, int]:
    keys = [k for k in xs if xs[k] is not None and ys.get(k) is not None]
    n = len(keys)
    if n < 10:
        return None, n

    def ranks(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            for q in range(i, j + 1):
                r[order[q]] = (i + j) / 2.0
            i = j + 1
        return r

    rx = ranks([xs[k] for k in keys])
    ry = ranks([ys[k] for k in keys])
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return (cov / sqrt(vx * vy) if vx > 0 and vy > 0 else None), n


def run_backtest(paths: list[str], db_path: str, cfg: SelectorConfig, weights: dict,
                 sectors: dict | None = None, log=print) -> list[DayResult]:
    store = Store(db_path)
    results: list[DayResult] = []
    raws = []
    for path in session_files(paths):
        try:
            raws.append(load_session_file(path))
        except Exception as exc:
            log(f"[SKIP] {path}: {exc}")
    raws.sort(key=lambda r: r.session_date)
    for raw in raws:
        day = raw.session_date.isoformat()
        si = freeze_information_set(raw, cfg.cutoff)                      # 1
        baselines = store.baselines(day, cfg.baseline_lookback_days, cfg.baseline_min_days)   # 2
        history = store.history(before=day, modes=("backtest",))
        try:
            snap, inner = decide(si, cfg, weights, "backtest", sectors, baselines, history)   # 3
            store.save_decision(snap)
        except DecisionExists:
            snap = store.get_decision(day, "backtest")
            inner = None
            log(f"[KEEP] {day}: decision already stored (immutable) -- re-evaluating outcomes only")
        except RuntimeError as exc:
            log(f"[SKIP] {day}: {exc}")
            continue
        d = snap.to_dict()
        outs = evaluate_decision(d, raw, cfg.horizons_min, cfg.move_threshold_pct, cfg.prob_threshold_pct)   # 4
        store.save_outcomes(snap.decision_id, outs)
        if inner is not None:
            table = inner["table"]
            ref = {s: table.features[s]["current_price"] for s in table.eligible}
            ic_rows = []
            for h in cfg.horizons_min:                                    # 5
                fwd = forward_returns_universe(raw, si.cutoff, h, ref)
                for name in weights["signed"]:
                    ic, n = _spearman({s: table.features[s].get(name) for s in table.eligible}, fwd)
                    ic_rows.append((name, h, ic, n))
            store.save_ic(day, ic_rows)
            store.save_baselines(day, {s: (f.get("cumulative_volume"), f.get("realized_vol_pct"))   # 6
                                       for s, f in table.features.items() if f})
        results.append(DayResult(day, d["selected_symbol"], d["direction"], d["selection_score"],
                                 {o.horizon_min: o.to_dict() for o in outs}))
        h15 = next((o for o in outs if o.horizon_min == cfg.default_horizon_min), outs[0])
        log(f"{day}  {d['selected_symbol']:<12} {d['direction']:<4} score {d['selection_score']:5.1f} | "
            f"{h15.horizon_min}m {h15.forward_return_pct if h15.forward_return_pct is None else round(h15.forward_return_pct, 3)}% {h15.outcome}")
    store.close()
    return results


def _stats(rets: list[float], mfes: list[float], maes: list[float]) -> str:
    if not rets:
        return "n=0"
    hits = sum(1 for r in rets if r > 0)
    return (f"n={len(rets):<4} hit {hits / len(rets):6.1%} | mean {mean(rets):+.3f}% | median {median(rets):+.3f}% | "
            f"MFE {mean(mfes):+.3f}% | MAE {mean(maes):+.3f}% | expectancy {mean(rets):+.3f}%/trade")


def report(db_path: str, cfg: SelectorConfig, train_fraction: float = 0.6) -> str:
    store = Store(db_path)
    rows = store.history(before="9999-12-31", modes=("backtest",))
    ic = store.ic_rows()
    store.close()
    lines = ["=" * 78, "PSYGRID 945 -- WALK-FORWARD BACKTEST REPORT", "=" * 78]
    if not rows:
        return "\n".join(lines + ["No backtest decisions stored yet."])
    days = sorted({d["decision_date"] for d, _ in rows})
    split = days[int(len(days) * train_fraction)] if len(days) > 1 else days[-1]
    lines.append(f"Sessions: {len(days)} ({days[0]} .. {days[-1]}) | train < {split} <= validation")

    def collect(filter_fn, h):
        r, mf, ma = [], [], []
        for d, outs in rows:
            o = outs.get(h)
            if filter_fn(d) and o and o.get("complete") and o.get("forward_return_pct") is not None:
                r.append(o["forward_return_pct"])
                mf.append(o["mfe_pct"])
                ma.append(o["mae_pct"])
        return r, mf, ma

    sections = [
        ("ALL", lambda d: True),
        ("TRAIN", lambda d: d["decision_date"] < split),
        ("VALIDATION (out-of-sample)", lambda d: d["decision_date"] >= split),
        ("DIRECTION UP", lambda d: d["direction"] == "UP"),
        ("DIRECTION DOWN", lambda d: d["direction"] == "DOWN"),
    ]
    liq = lambda d: d["feature_snapshot"].get("pct_turnover") or 0.0
    sections += [("LIQUIDITY TOP THIRD", lambda d: liq(d) >= 2 / 3),
                 ("LIQUIDITY MIDDLE THIRD", lambda d: 1 / 3 <= liq(d) < 2 / 3),
                 ("LIQUIDITY BOTTOM THIRD", lambda d: liq(d) < 1 / 3)]
    sections += [(f"REGIME {r}", (lambda rr: lambda d: d["market_context"]["regime"] == rr)(r))
                 for r in ("UP", "DOWN", "MIXED")]
    for h in cfg.horizons_min:
        lines += ["", f"--- HORIZON +{h} min ---"]
        for name, fn in sections:
            lines.append(f"  {name:<28} {_stats(*collect(fn, h))}")

    lines += ["", "SELECTION FREQUENCY (top 10)"]
    for sym, n in Counter(d["selected_symbol"] for d, _ in rows).most_common(10):
        lines.append(f"  {sym:<14} {n}")

    lines += ["", "PROBABILITY CALIBRATION (walk-forward predicted vs realized, default horizon)"]
    pairs = [(d["estimated_probability"], o.get("forward_return_pct"), d["probability_status"])
             for d, outs in rows for hh, o in outs.items()
             if hh == cfg.default_horizon_min and o.get("complete") and o.get("forward_return_pct") is not None]
    calibrated = [(p, r) for p, r, s in pairs if s.startswith("EMPIRICAL")]
    if len(calibrated) < 20:
        lines.append(f"  only {len(calibrated)} decisions had an empirical probability -- not enough to assess "
                     f"calibration (all others were UNCALIBRATED)")
    else:
        brier = mean((p - (r > cfg.prob_threshold_pct)) ** 2 for p, r in calibrated)
        lines.append(f"  n={len(calibrated)} | Brier score {brier:.4f} (0.25 = coin-flip baseline)")
        for lo, hi in ((0, .5), (.5, .6), (.6, .7), (.7, 1.01)):
            b = [(p, r) for p, r in calibrated if lo <= p < hi]
            if b:
                lines.append(f"  predicted {lo:.1f}-{min(hi, 1):.1f}: n={len(b):<4} realized "
                             f"{sum(1 for _, r in b if r > cfg.prob_threshold_pct) / len(b):.1%}")

    lines += ["", "FEATURE VALIDATION -- mean daily Spearman IC (feature at 09:45 vs forward return)",
              "  (a feature with |t| < 1 has not yet justified its weight -- candidate for removal)"]
    by = defaultdict(list)
    for _, feat, h, v, _n in ic:
        if v is not None:
            by[(feat, h)].append(v)
    for h in cfg.horizons_min:
        lines.append(f"  horizon +{h} min:")
        for (feat, hh), vals in sorted(by.items()):
            if hh != h:
                continue
            m = mean(vals)
            sd = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0.0
            t = m / (sd / sqrt(len(vals))) if sd > 0 else 0.0
            flag = "" if abs(t) >= 1 else "  <- not yet justified"
            lines.append(f"    {feat:<20} IC {m:+.4f}  t {t:+5.2f}  days {len(vals)}{flag}")
    return "\n".join(lines + ["=" * 78])
