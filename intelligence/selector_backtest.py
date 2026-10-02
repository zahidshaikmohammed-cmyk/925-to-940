"""Historical walk-forward replay.

For each archived session, in date order:
  1. freeze the 09:45 information set (selector_data.freeze_information_set)
  2. load baselines, calibration history and model-fit history from STRICTLY EARLIER
     sessions only (store queries with `before=day`)
  3. model.fit(earlier history) -> decide() -> immutable snapshot + feature matrix +
     ranking stored in one transaction
  4. only now reveal post-cutoff candles to the outcome evaluator -> outcomes stored
  5. universe forward returns + per-feature / whole-model IC stored for research
  6. today's pre-cutoff baselines stored for FUTURE sessions

945-V1 has no fitted parameters (fit() is a no-op). A future trained model receives in
fit() only what was stored for earlier days -- the leakage tests enforce this.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .selector_945 import decide
from .selector_config import MODEL_ID, SelectorConfig
from .selector_data import freeze_information_set, load_session_file
from .selector_outcomes import evaluate_decision
from .selector_research import record_universe_research, research_report, spearman
from .selector_scoring import LinearEvidenceModel
from .selector_store import DecisionExists, Store

_spearman = spearman          # backwards-compatible name


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
            files += sorted(f for f in list(p.glob("*.json")) + list(p.glob("*.json.gz"))
                            if "0945-input" not in f.name)        # full sessions only
        elif p.exists():
            files.append(p)
    return files


def run_backtest(paths: list[str], db_path: str, cfg: SelectorConfig, weights: dict,
                 sectors: dict | None = None, log=print, model_factory=None,
                 sector_source: str = "none") -> list[DayResult]:
    store = Store(db_path)
    model = model_factory() if model_factory else LinearEvidenceModel(weights)
    results: list[DayResult] = []
    raws = []
    for path in session_files(paths):
        try:
            raws.append((load_session_file(path), path))
        except Exception as exc:
            log(f"[SKIP] {path}: {exc}")
    raws.sort(key=lambda r: r[0].session_date)
    model_id = getattr(model, "model_id", MODEL_ID)
    for raw, path in raws:
        day = raw.session_date.isoformat()
        si = freeze_information_set(raw, cfg.cutoff)                                          # 1
        baselines = store.baselines(day, cfg.baseline_lookback_days, cfg.baseline_min_days)   # 2
        history = store.history(before=day, modes=("backtest",))
        existing = store.get_decision(day, "backtest", model_id)
        if existing:
            snap = existing
            log(f"[KEEP] {day}: decision already stored (immutable) -- only missing outcomes are added")
        else:
            model.fit(history)                                                               # 3
            try:
                snap, inner = decide(si, cfg, weights, "backtest", sectors, baselines, history, model=model,
                                     sector_source=sector_source)
                store.save_decision(snap, inner["feature_rows"], inner["ranking_rows"],
                                    {"path": str(path), "sha256": None, "bytes": Path(path).stat().st_size})
            except DecisionExists:
                snap = store.get_decision(day, "backtest", model_id)
            except RuntimeError as exc:
                log(f"[SKIP] {day}: {exc}")
                continue
            store.save_baselines(day, {s: (f.get("cumulative_volume"), f.get("realized_vol_pct"))  # 6
                                       for s, f in inner["table"].features.items() if f})
        d = snap.to_dict()
        outs = evaluate_decision(d, raw, cfg.horizons_min, cfg.move_threshold_pct, cfg.prob_threshold_pct,
                                 final=True)                                                  # 4
        store.save_outcomes(snap.decision_id, outs)
        record_universe_research(store, snap, raw, cfg, weights)                              # 5
        results.append(DayResult(day, d["selected_symbol"], d["direction"], d["selection_score"],
                                 {o.horizon_min: o.to_dict() for o in outs}))
        h15 = next((o for o in outs if o.horizon_min == cfg.default_horizon_min), outs[0])
        fr = "n/a" if h15.forward_return_pct is None else f"{h15.forward_return_pct:+.3f}%"
        log(f"{day}  {d['selected_symbol']:<12} {d['direction']:<4} score {d['selection_score']:5.1f} | "
            f"{h15.horizon_min}m {fr} {h15.outcome}")
    store.close()
    return results


def report(db_path: str, cfg: SelectorConfig, train_fraction: float = 0.6) -> str:
    store = Store(db_path)
    try:
        return research_report(store, cfg, "backtest", train_fraction)
    finally:
        store.close()
