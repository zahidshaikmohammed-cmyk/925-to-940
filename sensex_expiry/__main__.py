"""Command line for research and validation (spec section 40).

  python -m sensex_expiry --self-test
  python -m sensex_expiry --null-test 60                  # random-walk days: must show no edge
  python -m sensex_expiry --backtest DATA_DIR [--report out.json]
  python -m sensex_expiry --walk-forward DATA_DIR --train 40 --validate 15 --test 15
  python -m sensex_expiry --ablation DATA_DIR
  python -m sensex_expiry --gate REPORT.json --stage TINY_LIVE
  python -m sensex_expiry --baseline DATA_DIR --out reports/baseline [--workers 4] [--n-null 200]
  python -m sensex_expiry --validate-synthetic 40 --out reports/synthetic_pipeline_check

DATA_DIR holds one JSON file per day in history.py's format. There is no live-order
command here on purpose: live trading needs a passing gate and the separate runner.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from . import backtest as bt
from .audit import AuditLog, verify
from .config import ABLATIONS, EngineConfig, ablate
from .history import load_folder
from .validation_gate import live_allowed


def _summary(trades) -> dict:
    rs = [t.r_net for t in trades]
    lo, hi, p0 = bt.bootstrap_mean_ci(rs)
    return {"metrics_net": bt.metrics(trades), "metrics_gross": bt.metrics(trades, "r_gross"),
            "bootstrap_ci_mean_r": [lo, hi], "prob_mean_le_zero": p0,
            "by_setup": bt.breakdown(trades, lambda t: t.setup),
            "by_hour": bt.breakdown(trades, lambda t: t.entry_ts.strftime("%H:00")),
            "by_direction": bt.breakdown(trades, lambda t: t.direction),
            "by_exit": bt.breakdown(trades, lambda t: t.exit_reason),
            "monte_carlo": bt.monte_carlo(rs) if rs else {}}


def cmd_backtest(cfg: EngineConfig, folder: Path) -> dict:
    days = load_folder(folder)
    res = bt.run(cfg, days)
    base = bt.random_baseline(cfg, days, res.trades)
    out = {"config_hash": cfg.config_hash(), "days": len(days), "expiry_days": sum(d.is_expiry for d in days),
           "decisions": res.decisions, "no_trade_reasons": res.no_trade_reasons, **_summary(res.trades),
           "random_baseline_expectancy_r": bt.metrics(base).get("expectancy_r"),
           "perm_p_vs_random": bt.permutation_vs_baseline([t.r_net for t in res.trades], [t.r_net for t in base]),
           "sensitivity": {k: v.get("expectancy_r") for k, v in bt.sensitivity(cfg, days).items()}}
    return out


EXIT_CHOICES = ("TRAIL", "FIXED_2R", "FIXED_3R", "TIME_ONLY")


def cmd_walk_forward(cfg: EngineConfig, folder: Path, train: int, validate: int, test: int) -> dict:
    """The ONLY thing selected on validation data is the exit policy, from 4 pre-registered
    choices. Entry rules are fixed. Train windows are reported for drift, never fitted."""
    days = load_folder(folder)
    by_day = {d.day: d for d in days}
    folds = bt.walk_forward_folds([d.day for d in days if d.is_expiry], train, validate, test)
    oos, fold_rows = [], []
    for f in folds:
        vdays = [by_day[x] for x in f["validate"]]
        scores = {}
        for pol in EXIT_CHOICES:
            c2 = replace(cfg, exits=replace(cfg.exits, policy=pol))
            m = bt.metrics(bt.run(c2, vdays).trades)
            scores[pol] = m.get("expectancy_r", float("-inf")) if m.get("n", 0) >= 5 else float("-inf")
        best = max(scores, key=scores.get) if max(scores.values()) > float("-inf") else cfg.exits.policy
        c_best = replace(cfg, exits=replace(cfg.exits, policy=best))
        tr = bt.run(c_best, [by_day[x] for x in f["test"]]).trades
        oos += tr
        fold_rows.append({"test_from": str(f["test"][0]), "test_to": str(f["test"][-1]), "exit_policy": best,
                          "n": len(tr), "expectancy_r": statistics.mean([t.r_net for t in tr]) if tr else None})
    pos_share = (sum(1 for r in fold_rows if (r["expectancy_r"] or 0) > 0) / len(fold_rows)) if fold_rows else 0.0
    return {"config_hash": cfg.config_hash(), "folds": fold_rows, "positive_folds_share": pos_share,
            "oos": _summary(oos)}


def cmd_ablation(cfg: EngineConfig, folder: Path) -> dict:
    days = load_folder(folder)
    full = bt.metrics(bt.run(cfg, days).trades)
    rows = {"FULL": full}
    for name in ABLATIONS:
        rows[name] = bt.metrics(bt.run(ablate(cfg, name), days).trades)
    return {"config_hash": cfg.config_hash(),
            "table": {k: {"n": v.get("n"), "expectancy_r": v.get("expectancy_r"), "profit_factor": v.get("profit_factor"),
                          "max_drawdown_r": v.get("max_drawdown_r")} for k, v in rows.items()}}


def cmd_null(cfg: EngineConfig, n: int) -> dict:
    from .synthetic import make_days
    days = make_days(n, seed=99)
    res = bt.run(cfg, days)
    base = bt.random_baseline(cfg, days, res.trades)
    out = _summary(res.trades)
    out["random_baseline_expectancy_r"] = bt.metrics(base).get("expectancy_r")
    out["perm_p_vs_random"] = bt.permutation_vs_baseline([t.r_net for t in res.trades], [t.r_net for t in base])
    out["note"] = "SYNTHETIC random walk with fairly priced options: any apparent edge here is noise or a bug"
    return out


def self_test() -> int:
    from datetime import datetime
    from .models import IST
    from .synthetic import make_days
    cfg = EngineConfig()
    cfg.validate()
    res = bt.run(cfg, make_days(2, seed=3))
    assert res.decisions > 0
    with tempfile.TemporaryDirectory() as tmp:
        log = AuditLog(Path(tmp) / "a.jsonl")
        log.write("TEST", datetime(2026, 10, 8, 9, 30, tzinfo=IST), {"x": 1})
        assert verify(Path(tmp) / "a.jsonl") == (True, 1)
        g = live_allowed(Path(tmp) / "missing.json", cfg.config_hash(), "TINY_LIVE")
        assert not g.allowed
    print(f"sensex_expiry self-test OK (config {cfg.config_hash()}, {res.decisions} decisions, "
          f"{len(res.trades)} synthetic trades)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sensex_expiry")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--null-test", type=int)
    ap.add_argument("--backtest", type=Path)
    ap.add_argument("--walk-forward", type=Path)
    ap.add_argument("--train", type=int, default=40)
    ap.add_argument("--validate", type=int, default=15)
    ap.add_argument("--test", type=int, default=15)
    ap.add_argument("--ablation", type=Path)
    ap.add_argument("--gate", type=Path)
    ap.add_argument("--stage", default="TINY_LIVE")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--baseline", type=Path, help="baseline validation on a real dataset (realdata.py build)")
    ap.add_argument("--validate-synthetic", type=int, help="run the validation pipeline on N synthetic days")
    ap.add_argument("--out", type=Path, default=Path("reports/baseline"))
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--n-null", type=int, default=200)
    a = ap.parse_args(argv)
    cfg = EngineConfig()
    if a.self_test:
        return self_test()
    if a.gate:
        g = live_allowed(a.gate, cfg.config_hash(), a.stage)
        print(json.dumps({"allowed": g.allowed, "stage": g.stage, "failures": g.failures}, indent=2))
        return 0 if g.allowed else 3
    if a.baseline or a.validate_synthetic:
        from .validation import ValidationRun
        if a.baseline:
            days, label = load_folder(a.baseline), "REAL DHAN DATA"
            if not days:
                print(f"no day files in {a.baseline}", file=sys.stderr)
                return 2
        else:
            from .synthetic import make_days, make_day
            from datetime import timedelta
            exp = make_days(a.validate_synthetic, seed=77)
            extra = []
            for i, d in enumerate(exp):                     # add non-expiry days for Test A
                for k in (1, 2):
                    nd = make_day(d.day - timedelta(days=k), seed=9000 + 10 * i + k, strikes_each_side=0, is_expiry=False)
                    extra.append(nd)
            days = sorted(exp + extra, key=lambda d: d.day)
            from .models import PriorDay
            for prev, cur in zip(days, days[1:]):           # chain each prior day to the real previous session
                u = prev.underlying
                cur.prior = PriorDay(prev.day, max(x.high for x in u), min(x.low for x in u), u[-1].close)
            label = "SYNTHETIC RANDOM WALK - PIPELINE CHECK ONLY, NOT EVIDENCE"
        r = ValidationRun(cfg, days, a.out, n_null=a.n_null, workers=a.workers, label=label).run()
        print(json.dumps({"verdict": r["verdict"], "lookahead_audit_pass": r["lookahead_audit"].get("ALL_PASS"),
                          "report": str(a.out / "VALIDATION_REPORT.md")}, indent=2))
        return 0
    if a.null_test:
        out = cmd_null(cfg, a.null_test)
    elif a.backtest:
        out = cmd_backtest(cfg, a.backtest)
    elif a.walk_forward:
        out = cmd_walk_forward(cfg, a.walk_forward, a.train, a.validate, a.test)
    elif a.ablation:
        out = cmd_ablation(cfg, a.ablation)
    else:
        ap.print_help()
        return 2
    text = json.dumps(out, indent=2, default=str)
    if a.report:
        a.report.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
