"""The 09:45 decision pipeline and its terminal publication.

decide() is a pure function of (frozen SessionInput, sectors, prior-session baselines,
prior-session calibration history, model). It never imports selector_outcomes.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from .selector_calibration import Calibrator
from .selector_config import (FEATURE_VERSION, MODEL_NAME, MODEL_VERSION, SelectorConfig, config_hash,
                              weights_hash)
from .selector_data import SessionInput, recut
from .selector_features import build_feature_table
from .selector_scoring import Candidate, LinearEvidenceModel, RankingModel, select
from .selector_snapshot import DecisionSnapshot, build_snapshot

IST = ZoneInfo("Asia/Kolkata")

FEATURE_TEXT = {
    "rel_market": lambda v: f"{v:+.2f}% vs the market since 09:15",
    "residual_return": lambda v: f"{v:+.2f}% beta-adjusted move not explained by the market",
    "sector_rel": lambda v: f"{v:+.2f}% vs its sector peers",
    "return_15m": lambda v: f"{v:+.2f}% over the last 15 minutes",
    "return_5m": lambda v: f"{v:+.2f}% over the last 5 minutes",
    "ema_spread_pct": lambda v: f"EMA9 {'above' if v > 0 else 'below'} EMA20 by {abs(v):.2f}% of price",
    "slope_10_pct": lambda v: f"10-minute trend slope {v:+.3f}% per minute",
    "or_position": lambda v: f"price at {v:+.2f} opening-range widths from the range midpoint",
    "volume_imbalance": lambda v: f"{v:+.0%} net volume on {'up' if v > 0 else 'down'} candles",
    "persistence_10": lambda v: f"{v:+.0%} net candle direction over the last 10 minutes",
    "structure": lambda v: {1: "higher highs and higher lows", -1: "lower highs and lower lows"}.get(int(v), "no clear swing structure"),
    "efficiency_ratio": lambda v: f"clean move: {abs(v):.0%} of all price travel since 09:15 went {'up' if v > 0 else 'down'}",
    "day_range_position": lambda v: f"trading in the {'upper' if v > 0 else 'lower'} part of today's range ({v + 0.5:.0%})",
}


def explain(c: Candidate, top: int = 5) -> list[str]:
    ranked = sorted((x for x in c.contributions if x.contribution > 0 and x.value is not None),
                    key=lambda x: (-x.contribution, x.feature))[:top]
    lines = []
    for x in ranked:
        text = FEATURE_TEXT.get(x.feature, lambda v: f"{x.feature} = {v:.4g}")(x.value)
        lines.append(f"{text} (z {x.z:+.2f}, weight {x.weight:.2f}, +{x.contribution:.3f})")
    for name, p in c.penalties:
        lines.append(f"penalty: {name.replace('_', ' ')} (-{p:.3f})")
    if c.activity < 0.75:
        lines.append(f"activity multiplier only {c.activity:.2f} (low turnover / volume pace vs universe)")
    return lines


def _feature_stats(table) -> dict:
    """Distribution of every numeric feature over the eligible universe (research record)."""
    out = {}
    names = sorted({k for s in table.eligible for k, v in table.features[s].items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)})
    for name in names:
        vals = sorted(table.features[s][name] for s in table.eligible
                      if isinstance(table.features[s].get(name), (int, float))
                      and not isinstance(table.features[s].get(name), bool))
        n = len(vals)
        nulls = len(table.eligible) - n
        if not n:
            out[name] = {"n": 0, "nulls": nulls}
            continue
        q = lambda p: vals[min(n - 1, int(p * (n - 1) + 0.5))]
        out[name] = {"n": n, "nulls": nulls, "mean": round(sum(vals) / n, 6), "p10": round(q(0.1), 6),
                     "median": round(q(0.5), 6), "p90": round(q(0.9), 6), "min": round(vals[0], 6),
                     "max": round(vals[-1], 6)}
    return out


def decide(si: SessionInput, cfg: SelectorConfig, weights: dict, mode: str,
           sectors: dict | None = None, baselines: dict | None = None,
           history: list | None = None, model: RankingModel | None = None,
           published_at: datetime | None = None, feed_health: dict | None = None,
           sector_source: str = "none") -> tuple[DecisionSnapshot, dict]:
    """Returns (immutable snapshot, internals: table, ranking, store rows).

    Inputs are ONLY: the frozen 09:45 information set, prior-session baselines and
    prior-session history (calibration / model fit). Nothing after the cutoff exists here.
    """
    sectors = sectors or {}
    table = build_feature_table(si, cfg, sectors, baselines)
    model = model or LinearEvidenceModel(weights)
    ranked = model.rank(table)
    best = select(ranked)                      # exactly one; raises only if nothing is scorable
    est = Calibrator(history or [], cfg).estimate(best.score, best.raw)
    f = table.features[best.symbol]
    reasons = Counter(table.exclusions.values())
    m = table.market

    stability = {}
    for t in cfg.stability_cutoffs:            # same model on EARLIER information only
        try:
            early = build_feature_table(recut(si, t), cfg, sectors, baselines)
            r = model.rank(early)
            pos = next((i + 1 for i, c in enumerate(r) if (c.symbol, c.direction) == (best.symbol, best.direction)), None)
            stability[t.strftime("%H:%M")] = {"rank": pos, "of": len(r), "top_then": r[0].symbol if r else None}
        except Exception as exc:
            stability[t.strftime("%H:%M")] = {"error": type(exc).__name__}

    published = published_at or datetime.now(IST)
    universe_sectored = sum(1 for s in si.received_symbols if sectors.get(s))
    eligible_sectored = sum(1 for s in table.eligible if table.features[s].get("sector_rel") is not None)
    snap = build_snapshot(
        decision_id="", decision_date=si.session_date.isoformat(), cutoff=si.cutoff.isoformat(),
        published_at=published.isoformat(), mode=mode,
        universe_size=len(si.received_symbols), eligible_count=len(table.eligible),
        excluded_count=len(table.exclusions), exclusion_reasons=dict(sorted(reasons.items())),
        selected_symbol=best.symbol, direction=best.direction,
        selection_score=round(best.score, 4), raw_score=round(best.raw, 6),
        reference_price=f["current_price"],
        estimated_probability=round(est.probability, 4), probability_status=est.probability_status,
        probability_definition=(f"P(signed return over {est.horizon_min} min > {cfg.prob_threshold_pct}% "
                                f"| 09:45 state)"),
        expected_return_pct=None if est.expected_return_pct is None else round(est.expected_return_pct, 4),
        expected_return_status=est.expected_return_status,
        expected_horizon_min=est.horizon_min, horizon_status=est.horizon_status,
        explanation=explain(best),
        feature_snapshot={k: (round(v, 6) if isinstance(v, float) else v) for k, v in sorted(f.items())},
        contributions=[{"feature": x.feature, "value": x.value, "z": x.z, "weight": x.weight,
                        "contribution": round(x.contribution, 6)} for x in best.contributions],
        ranking_snapshot=[{"rank": i + 1, "symbol": c.symbol, "direction": c.direction,
                           "score": round(c.score, 4), "raw": round(c.raw, 6)}
                          for i, c in enumerate(ranked[:cfg.ranking_snapshot_size])],
        market_context={"market_return_pct": m.market_return, "market_source": m.market_source,
                        "breadth": m.breadth, "regime": m.regime,
                        # only time-invariant feed metadata may enter the fingerprinted body;
                        # the feed clock / live status live in feed_health (not fingerprinted)
                        "declared_universe_size": si.feed_meta.get("universe_size")},
        data_quality={"selected": {k: f.get(k) for k in ("n_bars", "missing_minutes", "last_bar_age_min", "stale",
                                                        "missing_field_count", "data_quality_score",
                                                        "spread_available", "opening_price_source",
                                                        "relative_volume_source")},
                      "universe_unparseable": len(si.unparseable),
                      "freshness": "PASS" if not f.get("stale") else "FAIL"},
        model_id=getattr(model, "model_id", "unknown"),
        model_name=f"{MODEL_NAME}/{model.name}", model_version=f"{MODEL_VERSION}+{model.version}",
        feature_version=FEATURE_VERSION, config_hash=config_hash(cfg), weights_hash=weights_hash(weights),
        sector_coverage={"source": sector_source, "universe_classified": universe_sectored,
                         "universe": len(si.received_symbols), "eligible_with_sector_feature": eligible_sectored,
                         "eligible": len(table.eligible),
                         "note": "unclassified stocks keep sector features null -- never inferred"},
        feature_stats=_feature_stats(table),
        stability=stability,
        feed_health=feed_health or {"validated": False, "note": "offline/replay input -- no live feed checks"},
        publication_lag_seconds=round((published - si.cutoff).total_seconds(), 3),
        input_fingerprint=si.fingerprint, decision_fingerprint="",
    )
    feature_rows = [(s, s in table.eligible, table.exclusions.get(s),
                     {k: v for k, v in table.features.get(s, {}).items()})
                    for s in si.received_symbols]
    ranking_rows = [(c.symbol, c.direction, round(c.raw, 6), round(c.score, 4)) for c in ranked]
    return snap, {"table": table, "ranked": ranked, "estimate": est,
                  "feature_rows": feature_rows, "ranking_rows": ranking_rows}


def render(snap: DecisionSnapshot) -> str:
    d = snap.to_dict()
    pub = datetime.fromisoformat(d["published_at"]).astimezone(IST)
    cut = datetime.fromisoformat(d["cutoff"]).astimezone(IST)
    exp = ("UNAVAILABLE (needs walk-forward history)" if d["expected_return_pct"] is None
           else f"{d['expected_return_pct']:+.3f}%  [{d['expected_return_status']}]")
    lines = [
        "=" * 70,
        "PSYGRID 945 -- 09:45 INTRADAY SELECTION",
        "=" * 70,
        "",
        f"Decision Time    : {cut:%H:%M:%S} IST information cutoff | published {pub:%Y-%m-%d %H:%M:%S} IST",
        f"Universe         : {d['universe_size']}",
        f"Eligible         : {d['eligible_count']}",
        f"Excluded         : {d['excluded_count']}  {d['exclusion_reasons'] if d['exclusion_reasons'] else ''}",
        "",
        "SELECTED STOCK",
        f"Symbol           : {d['selected_symbol']}",
        f"Direction        : {d['direction']}",
        f"Reference price  : Rs {d['reference_price']:.2f} (last completed 1m close before 09:45)",
        f"Score            : {d['selection_score']:.1f} / 100   (ranking score -- NOT a probability)",
        f"Probability      : {d['estimated_probability']:.2f}   {d['probability_definition']}",
        f"Status           : {d['probability_status']}",
        "",
        f"Expected Horizon : {d['expected_horizon_min']} MIN  [{d['horizon_status']}]",
        f"Expected Return  : {exp}",
        "",
        "WHY SELECTED (from actual feature contributions)",
    ]
    lines += [f"{i}. {t}" for i, t in enumerate(d["explanation"], 1)] or ["(no positive contributions)"]
    q = d["data_quality"]["selected"]
    mc = d["market_context"]
    sc = d["sector_coverage"]
    lines += [
        "",
        "MARKET CONTEXT",
        f"Market           : {mc['regime']} | return {mc['market_return_pct'] if mc['market_return_pct'] is None else round(mc['market_return_pct'], 3)}% "
        f"({mc['market_source']}) | breadth {mc['breadth'] if mc['breadth'] is None else round(mc['breadth'] * 100)}%",
        "",
        "DATA QUALITY",
        f"Freshness        : {d['data_quality']['freshness']} (last bar age {q['last_bar_age_min']:.0f} min)",
        f"Missing fields   : {q['missing_field_count']} | missing minutes {q['missing_minutes']} | bars {q['n_bars']}",
        f"Input timestamp  : {d['cutoff']}",
        f"Input hash       : {d['input_fingerprint'][:16]}",
        f"Decision hash    : {d['decision_fingerprint'][:16]}  ({d['decision_id']})",
        f"Model            : {d['model_id']} ({d['model_name']} v{d['model_version']}) | features {d['feature_version']}",
        f"Config / weights : {d['config_hash']} / {d['weights_hash']}",
        f"Sector coverage  : {sc['universe_classified']}/{sc['universe']} classified ({sc['source']}); "
        f"{sc['eligible_with_sector_feature']}/{sc['eligible']} eligible have a sector feature, rest null",
        f"Rank stability   : " + " | ".join(f"{t}: rank {v.get('rank')} of {v.get('of')}" for t, v in sorted(d['stability'].items())),
    ]
    fh = d["feed_health"]
    if fh.get("checks"):
        lines += ["", "FEED", f"Feed clock       : {fh.get('feed_clock') or 'not provided'} | age "
                  f"{'n/a' if fh.get('feed_age_seconds') is None else round(fh['feed_age_seconds'])}s | local {fh.get('local_time')}",
                  f"Session / status : {fh.get('session_date')} / {fh.get('market_status')}",
                  f"Universe / valid : {fh.get('universe_count')} / {fh.get('valid_count')} (missing {fh.get('missing_count')})",
                  "Checks           : " + ", ".join(f"{n} {'PASS' if p else 'FAIL'}" for n, p, _ in fh["checks"]),
                  f"Publication lag  : {d['publication_lag_seconds']:.1f}s after the 09:45 cutoff"]
    else:
        lines += ["", "FEED             : not validated (offline replay/backtest input)"]
    lines += [
        "",
        "TOP 5 OF RANKING",
    ]
    lines += [f"  #{r['rank']} {r['symbol']:<14} {r['direction']:<4} score {r['score']:.1f}"
              for r in d["ranking_snapshot"][:5]]
    lines += ["", "NOTE: research selection, not an order. Not a guarantee of profit.", "=" * 70]
    return "\n".join(lines)
