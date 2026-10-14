"""Run every engine over every stock of one snapshot, with full accounting.

Guarantees:
* every symbol in the universe gets exactly one Evaluation from every engine -- a stock that
  is missing from the feed, malformed, stale or illiquid is reported under that status, so
  "no signal" is never confused with "no data";
* an exception inside one engine (in ``prepare`` or for one stock) is recorded as
  ENGINE_ERROR for that engine only; other engines and other stocks are unaffected;
* engines only read immutable feature tuples; an integrity fingerprint taken before and
  after the engines run proves no engine altered another engine's inputs;
* stocks are processed in sorted symbol order, so results do not depend on feed order.
"""
from __future__ import annotations

import re
import time as systime
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .context import ContextConfig, MarketContext, build_context
from .core import ENGINE_ERROR, INELIGIBLE, INVALID_DATA, SIGNAL, STATUSES, Evaluation, Signal
from .features import Features, PriorLevels, StockView
from .quality import QualityConfig, check_stock


@dataclass(frozen=True)
class StockInput:
    symbol: str
    bars: tuple
    previous_close: float | None
    today_open: float | None
    healthy: bool = True
    health_reason: str = ""


@dataclass
class ScanReport:
    scan_time: datetime
    universe: tuple[str, ...]
    received: int
    valid: int
    context: MarketContext
    evaluations: dict = field(default_factory=dict)      # engine -> {symbol: Evaluation}
    counts: dict = field(default_factory=dict)           # engine -> Counter(status)
    quality_counts: Counter = field(default_factory=Counter)
    quality_reasons: Counter = field(default_factory=Counter)
    engine_errors: dict = field(default_factory=dict)    # engine -> [messages]
    signals: list = field(default_factory=list)          # list[Signal], engine order then score
    integrity_ok: bool = True
    seconds: float = 0.0
    views: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)


def reason_kind(reason: str) -> str:
    """'stale_5m' -> 'stale', 'feed_unhealthy:x' -> 'feed_unhealthy' (for counting)."""
    head = reason.split(":")[0]
    return re.sub(r"_?\d.*$", "", head) or head


def run_scan(inputs: dict[str, StockInput], scan_time: datetime, engines: list,
             priors: dict[str, PriorLevels] | None = None, universe: set[str] | None = None,
             qcfg: QualityConfig = QualityConfig(), ccfg: ContextConfig = ContextConfig()) -> ScanReport:
    started = systime.perf_counter()
    priors = priors or {}
    symbols = tuple(sorted(set(inputs) | set(universe or ())))
    failed: dict[str, Evaluation] = {}       # template per failed symbol (engine filled later)
    views: dict[str, StockView] = {}
    feats: dict[str, Features] = {}
    qc, qr = Counter(), Counter()
    for sym in symbols:
        si = inputs.get(sym)
        if si is None:
            failed[sym] = Evaluation("", sym, INVALID_DATA, "missing_from_feed")
            qc[INVALID_DATA] += 1
            qr["missing_from_feed"] += 1
            continue
        try:
            q = check_stock(sym, si.bars, si.previous_close, si.today_open, si.healthy, si.health_reason,
                            scan_time, qcfg, priors.get(sym))
            if q.status is None:
                feats[sym] = Features(q.view.bars)
                views[sym] = q.view
                qc["VALID"] += 1
                continue
            failed[sym] = Evaluation("", sym, q.status, q.reason)
            qc[q.status] += 1
            qr[reason_kind(q.reason)] += 1
        except Exception as exc:          # a bad record must never stop the scan
            failed[sym] = Evaluation("", sym, INVALID_DATA, f"quality_exception:{type(exc).__name__}")
            qc[INVALID_DATA] += 1
            qr["quality_exception"] += 1
            views.pop(sym, None)
            feats.pop(sym, None)
    try:
        ctx = build_context(views, feats, len(inputs), ccfg, n_data_ok=len(views) + qc.get(INELIGIBLE, 0))
    except Exception as exc:
        ctx = MarketContext(False, f"context_exception:{type(exc).__name__}", len(views), len(inputs))
    before = {s: f.fingerprint() for s, f in feats.items()}

    report = ScanReport(scan_time, symbols, len(inputs), len(views), ctx, quality_counts=qc, quality_reasons=qr,
                        views=views, features=feats)
    for eng in engines:
        evals: dict[str, Evaluation] = {}
        errors: list[str] = []
        prep_error = None
        try:
            eng.prepare(views, feats, ctx)
        except Exception as exc:
            prep_error = f"prepare:{type(exc).__name__}:{exc}"
            errors.append(prep_error + "\n" + traceback.format_exc(limit=3))
        for sym in symbols:
            if sym in failed:
                f = failed[sym]
                evals[sym] = Evaluation(eng.NAME, sym, f.status, f.reason)
                continue
            if prep_error:
                evals[sym] = Evaluation(eng.NAME, sym, ENGINE_ERROR, prep_error)
                continue
            try:
                ev = eng.evaluate(views[sym], feats[sym], ctx)
                if ev.engine != eng.NAME or ev.symbol != sym:
                    raise RuntimeError("engine returned an evaluation for another engine/symbol")
            except Exception as exc:
                ev = Evaluation(eng.NAME, sym, ENGINE_ERROR, f"{type(exc).__name__}:{exc}")
                if len(errors) < 5:
                    errors.append(f"{sym}: {type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=3))
            evals[sym] = ev
        counts = Counter(ev.status for ev in evals.values())
        assert sum(counts.values()) == len(symbols) and set(counts) <= set(STATUSES)
        report.evaluations[eng.NAME] = evals
        report.counts[eng.NAME] = counts
        if errors:
            report.engine_errors[eng.NAME] = errors
        sigs = sorted((ev.signal for ev in evals.values() if ev.status == SIGNAL), key=lambda s: (-s.score, s.symbol))
        report.signals.extend(sigs)
    report.integrity_ok = all(feats[s].fingerprint() == fp for s, fp in before.items())
    report.seconds = systime.perf_counter() - started
    return report


def signals_by_engine(report: ScanReport) -> dict[str, list[Signal]]:
    out: dict[str, list[Signal]] = {}
    for s in report.signals:
        out.setdefault(s.engine, []).append(s)
    return out
