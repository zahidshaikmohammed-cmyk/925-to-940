"""Daily reports + cumulative research report (live or backtest store).

The research report answers, from stored data only:
  which features predicted returns (daily IC, t-stat)   | which were noise (|t| < 1)
  which regimes / liquidity groups / directions / horizons worked
  how stable the selected stock was (rank at 09:35 / 09:40)
  how concentrated selections were (unique symbols, top share, HHI)
  whether the score related to outcomes (score vs return, daily model-score IC)
  whether the probability is calibrated (Brier, reliability buckets, ECE, n)
Probabilities are assessed ONLY for decisions whose probability was estimated from
earlier sessions (status EMPIRICAL_WALK_FORWARD); UNCALIBRATED ones are excluded.
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from math import sqrt
from pathlib import Path
from statistics import mean, median

from .selector_config import SelectorConfig
from .selector_outcomes import forward_returns_universe


def spearman(xs: dict, ys: dict) -> tuple[float | None, int]:
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

    rx, ry = ranks([xs[k] for k in keys]), ranks([ys[k] for k in keys])
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx, vy = sum((a - mx) ** 2 for a in rx), sum((b - my) ** 2 for b in ry)
    return (cov / sqrt(vx * vy) if vx > 0 and vy > 0 else None), n


def record_universe_research(store, snap, raw, cfg: SelectorConfig, weights: dict) -> int:
    """Downstream of a stored decision: forward returns of every eligible stock (complete
    windows only) + per-feature and whole-model IC. Reads the decision's stored feature
    matrix; never touches the decision. Returns the number of horizons recorded."""
    d = snap.to_dict()
    feats = store.decision_features(snap.decision_id)
    ref = {s: f["current_price"] for s, (elig, _, f) in feats.items() if elig and f.get("current_price")}
    from datetime import datetime
    cutoff = datetime.fromisoformat(d["cutoff"])
    net: dict = {}
    for _, sym, direction, raw_score, _ in store.decision_rankings(snap.decision_id):
        net[sym] = net.get(sym, 0.0) + (raw_score if direction == "UP" else -raw_score)
    done = 0
    for h in cfg.horizons_min:
        if store.universe_forward(snap.decision_id, h):
            continue
        fwd = forward_returns_universe(raw, cutoff, h, ref)
        if len(fwd) < 10:
            continue
        store.save_universe_forward(snap.decision_id, h, fwd)
        rows = [(name, h, *spearman({s: feats[s][2].get(name) for s in ref}, fwd)) for name in weights["signed"]]
        rows.append(("_model_net_score", h, *spearman(net, fwd)))
        store.save_ic(d["decision_date"], rows)
        done += 1
    return done


def _fmt(x, nd=3, pct=True):
    if x is None:
        return "n/a"
    return f"{x:+.{nd}f}%" if pct else f"{x:.{nd}f}"


def daily_report_text(store, snap, cfg: SelectorConfig) -> str:
    d = snap.to_dict()
    outs = store.outcomes(snap.decision_id)
    q = d["data_quality"]["selected"]
    fh = d["feed_health"]
    lines = ["=" * 64, f"PSYGRID 945 DAILY REPORT -- {d['decision_date']}", "=" * 64,
             f"09:45 DECISION     : {d['decision_id']} (published {d['published_at'][11:19]} IST, "
             f"lag {d['publication_lag_seconds']}s)",
             f"SELECTED STOCK     : {d['selected_symbol']}",
             f"DIRECTION          : {d['direction']}",
             f"SCORE              : {d['selection_score']:.1f} / 100 (ranking score, not a probability)",
             f"PROBABILITY        : {d['estimated_probability']:.2f}  {d['probability_definition']}",
             f"PROBABILITY STATUS : {d['probability_status']}",
             f"EXPECTED HORIZON   : {d['expected_horizon_min']} min [{d['horizon_status']}]",
             f"EXPECTED RETURN    : {_fmt(d['expected_return_pct'])} [{d['expected_return_status']}]"]
    for h in cfg.horizons_min:
        o = outs.get(h)
        if not o:
            lines.append(f"+{h}M RESULT{' ' * (9 - len(str(h)))}: not recorded")
            continue
        lines.append(f"+{h}M RESULT{' ' * (9 - len(str(h)))}: {o['outcome']:<16} return {_fmt(o['forward_return_pct'])} | "
                     f"MFE {_fmt(o['mfe_pct'])} @{o.get('mfe_minute')}m | MAE {_fmt(o['mae_pct'])} @{o.get('mae_minute')}m")
    best = max((o for o in outs.values() if o.get("mfe_pct") is not None), key=lambda o: o["horizon_min"], default=None)
    lines += [f"MFE (30m window)   : {_fmt(best['mfe_pct']) if best else 'n/a'}",
              f"MAE (30m window)   : {_fmt(best['mae_pct']) if best else 'n/a'}",
              f"DATA QUALITY       : freshness {d['data_quality']['freshness']} | bars {q['n_bars']} | "
              f"missing minutes {q['missing_minutes']} | missing fields {q['missing_field_count']}",
              f"FEED               : " + (", ".join(f"{n} {'PASS' if p else 'FAIL'}" for n, p, _ in fh['checks'])
                                         if fh.get("checks") else "not validated"),
              f"UNIVERSE           : {d['universe_size']} received | {d['eligible_count']} eligible | "
              f"{d['excluded_count']} excluded {d['exclusion_reasons']}",
              f"MARKET             : {d['market_context']['regime']} ({d['market_context']['market_source']})",
              f"MODEL              : {d['model_id']} | features {d['feature_version']} | config {d['config_hash']} | "
              f"weights {d['weights_hash']}",
              f"FINGERPRINTS       : input {d['input_fingerprint'][:16]} | decision {d['decision_fingerprint'][:16]}",
              "=" * 64]
    return "\n".join(lines)


def write_daily_report(store, snap, cfg: SelectorConfig) -> Path:
    p = Path(cfg.reports_dir) / f"{snap.decision_date}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(daily_report_text(store, snap, cfg), encoding="utf-8")
    return p


SUMMARY_FIELDS = ["date", "mode", "model_id", "symbol", "direction", "score", "probability", "probability_status",
                  "expected_horizon_min", "expected_return_pct", "regime", "eligible", "universe",
                  "ret_5m", "ret_15m", "ret_30m", "outcome_15m", "mfe_30m", "mae_30m", "freshness",
                  "publication_lag_s", "decision_fingerprint"]


def summary_rows(store, mode: str = "live") -> list[dict]:
    rows = []
    for d in store.decisions(mode=mode):
        outs = store.outcomes(d["decision_id"])
        g = lambda h, k: (outs.get(h) or {}).get(k)
        rows.append({"date": d["decision_date"], "mode": d["mode"], "model_id": d["model_id"],
                     "symbol": d["selected_symbol"], "direction": d["direction"], "score": d["selection_score"],
                     "probability": d["estimated_probability"], "probability_status": d["probability_status"].split(" ")[0],
                     "expected_horizon_min": d["expected_horizon_min"], "expected_return_pct": d["expected_return_pct"],
                     "regime": d["market_context"]["regime"], "eligible": d["eligible_count"], "universe": d["universe_size"],
                     "ret_5m": g(5, "forward_return_pct"), "ret_15m": g(15, "forward_return_pct"),
                     "ret_30m": g(30, "forward_return_pct"), "outcome_15m": g(15, "outcome"),
                     "mfe_30m": g(30, "mfe_pct"), "mae_30m": g(30, "mae_pct"),
                     "freshness": d["data_quality"]["freshness"], "publication_lag_s": d["publication_lag_seconds"],
                     "decision_fingerprint": d["decision_fingerprint"]})
    return rows


def write_summary_csv(store, path: Path, mode: str = "live") -> Path:
    """Regenerated from the database every time, so it can never drift or duplicate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(summary_rows(store, mode))
    return path


def _stats(rets, mfes, maes) -> str:
    if not rets:
        return "n=0"
    hits = sum(1 for r in rets if r > 0)
    return (f"n={len(rets):<4} hit {hits / len(rets):6.1%} | mean {mean(rets):+.3f}% | median {median(rets):+.3f}% | "
            f"MFE {mean(mfes):+.3f}% | MAE {mean(maes):+.3f}% | expectancy {mean(rets):+.3f}%/trade")


def _tstat(vals):
    if len(vals) < 2:
        return 0.0
    m = mean(vals)
    sd = sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
    return m / (sd / sqrt(len(vals))) if sd > 0 else 0.0


def calibration_metrics(pairs: list[tuple[float, bool]]) -> dict:
    """pairs = (predicted probability, realized event). Brier, ECE, reliability buckets."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    brier = mean((p - float(y)) ** 2 for p, y in pairs)
    buckets = []
    ece = 0.0
    for lo, hi in ((0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)):
        b = [(p, y) for p, y in pairs if lo <= p < hi]
        if b:
            pm, ym = mean(p for p, _ in b), mean(float(y) for _, y in b)
            ece += len(b) / n * abs(pm - ym)
            buckets.append({"range": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(b), "predicted": pm, "realized": ym})
    base = mean(float(y) for _, y in pairs)
    return {"n": n, "brier": brier, "brier_baseline": base * (1 - base), "ece": ece, "buckets": buckets}


def research_report(store, cfg: SelectorConfig, mode: str, train_fraction: float = 0.6) -> str:
    rows = [(d, store.outcomes(d["decision_id"])) for d in store.decisions(mode=mode)]
    title = "WALK-FORWARD BACKTEST" if mode == "backtest" else "LIVE RESEARCH"
    lines = ["=" * 78, f"PSYGRID 945 -- {title} REPORT ({mode} decisions)", "=" * 78,
             "Statistical status: NOT PROVEN until enough out-of-sample sessions exist. Read every number "
             "below together with its n."]
    if not rows:
        return "\n".join(lines + [f"No {mode} decisions stored yet."])
    days = sorted({d["decision_date"] for d, _ in rows})
    split = days[int(len(days) * train_fraction)] if len(days) > 1 else days[-1]
    lines.append(f"Sessions: {len(days)} ({days[0]} .. {days[-1]}) | early < {split} <= late (out-of-sample half)")

    def collect(fn, h):
        r, mf, ma = [], [], []
        for d, outs in rows:
            o = outs.get(h)
            if fn(d) and o and o.get("complete") and o.get("forward_return_pct") is not None:
                r.append(o["forward_return_pct"]); mf.append(o["mfe_pct"]); ma.append(o["mae_pct"])
        return r, mf, ma

    liq = lambda d: d["feature_snapshot"].get("pct_turnover") or 0.0
    sections = [("ALL", lambda d: True), ("EARLY", lambda d: d["decision_date"] < split),
                ("LATE (out-of-sample)", lambda d: d["decision_date"] >= split),
                ("DIRECTION UP", lambda d: d["direction"] == "UP"), ("DIRECTION DOWN", lambda d: d["direction"] == "DOWN"),
                ("LIQUIDITY TOP THIRD", lambda d: liq(d) >= 2 / 3),
                ("LIQUIDITY MIDDLE THIRD", lambda d: 1 / 3 <= liq(d) < 2 / 3),
                ("LIQUIDITY BOTTOM THIRD", lambda d: liq(d) < 1 / 3)]
    sections += [(f"REGIME {r}", (lambda rr: lambda d: d["market_context"]["regime"] == rr)(r)) for r in ("UP", "DOWN", "MIXED")]
    for h in cfg.horizons_min:
        lines += ["", f"--- HORIZON +{h} min ---"]
        lines += [f"  {name:<26} {_stats(*collect(fn, h))}" for name, fn in sections]

    syms = Counter(d["selected_symbol"] for d, _ in rows)
    n = len(rows)
    hhi = sum((c / n) ** 2 for c in syms.values())
    lines += ["", "SELECTION CONCENTRATION",
              f"  {len(syms)} distinct symbols in {n} decisions | most frequent {syms.most_common(1)[0][0]} "
              f"{syms.most_common(1)[0][1] / n:.0%} | HHI {hhi:.3f} (1/n = {1 / n:.3f} is perfectly spread)"]
    lines += [f"    {s:<14} {c}" for s, c in syms.most_common(8)]

    stab = defaultdict(list)
    for d, _ in rows:
        for t, v in (d.get("stability") or {}).items():
            if isinstance(v, dict) and v.get("rank"):
                stab[t].append(v["rank"])
    lines += ["", "RANK STABILITY OF THE 09:45 PICK (same model on earlier information)"]
    lines += [f"  at {t}: median rank {median(v):.0f}, in top 5 on {sum(1 for x in v if x <= 5) / len(v):.0%} of days (n={len(v)})"
              for t, v in sorted(stab.items())] or ["  n/a"]

    lines += ["", "SCORE vs OUTCOME"]
    pairs = [(d["selection_score"], outs[h]["forward_return_pct"]) for d, outs in rows
             for h in (cfg.default_horizon_min,) if outs.get(h) and outs[h].get("forward_return_pct") is not None]
    if len(pairs) >= 10:
        rho, k = spearman({i: p[0] for i, p in enumerate(pairs)}, {i: p[1] for i, p in enumerate(pairs)})
        lines.append(f"  Spearman(selection score, {cfg.default_horizon_min}m signed return) across days: "
                     f"{'n/a' if rho is None else f'{rho:+.3f}'} (n={k})")
    else:
        lines.append(f"  only {len(pairs)} decisions with outcomes -- need >= 10")

    ic = store.ic_rows()
    by = defaultdict(list)
    for _, feat, h, v, _n in ic:
        if v is not None:
            by[(feat, h)].append(v)
    lines += ["", "FEATURE VALIDATION -- mean daily Spearman IC of the 09:45 value vs forward return (all eligible stocks)",
              "  (|t| < 1 = not yet justified; '_model_net_score' = whole-model cross-sectional IC)"]
    for h in cfg.horizons_min:
        lines.append(f"  horizon +{h} min:")
        for (feat, hh), vals in sorted(by.items()):
            if hh == h:
                t = _tstat(vals)
                lines.append(f"    {feat:<20} IC {mean(vals):+.4f}  t {t:+5.2f}  days {len(vals)}"
                             f"{'' if abs(t) >= 1 else '  <- not yet justified'}")
    if not by:
        lines.append("    no IC observations yet")

    lines += ["", "PROBABILITY CALIBRATION (only decisions whose probability came from EARLIER sessions)"]
    cal = [(d["estimated_probability"], outs[d["expected_horizon_min"]]["forward_return_pct"] > cfg.prob_threshold_pct)
           for d, outs in rows if d["probability_status"].startswith("EMPIRICAL")
           and outs.get(d["expected_horizon_min"]) and outs[d["expected_horizon_min"]].get("forward_return_pct") is not None]
    unc = sum(1 for d, _ in rows if d["probability_status"].startswith("UNCALIBRATED"))
    m = calibration_metrics(cal)
    if m["n"] < 20:
        lines.append(f"  {m['n']} calibrated observations ({unc} decisions were UNCALIBRATED) -- need >= 20 to assess")
    else:
        lines.append(f"  n={m['n']} | Brier {m['brier']:.4f} (always-base-rate baseline {m['brier_baseline']:.4f}) | ECE {m['ece']:.4f}")
        lines += [f"    predicted {b['range']}: n={b['n']:<4} mean predicted {b['predicted']:.2f} -> realized {b['realized']:.2f}"
                  for b in m["buckets"]]
    return "\n".join(lines + ["=" * 78])
