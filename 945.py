"""945.py -- PSYGRID 09:45 IST intraday selector (executable / orchestrator).

Evaluates the whole PSYGRID stock universe using ONLY information available by 09:45,
ranks every stock in both directions and publishes EXACTLY ONE stock + direction as an
immutable, fingerprinted decision. Reads the existing PSYGRID public endpoints only;
it never touches the live data plane.

    python 945.py                         live: wait for 09:45, decide, store, beep
    python 945.py --show [--date D]       print a stored decision
    python 945.py --evaluate [--date D]   compute +5/+15/+30 min outcomes (after 10:15)
    python 945.py --archive               save today's full feed for future backtests
    python 945.py --replay FILE           decide from a saved session file
    python 945.py --backtest PATH...      walk-forward replay of archived sessions + report
    python 945.py --report                print the backtest report again
    python 945.py --benchmark             989-stock production-scale benchmark
    python 945.py --self-test             run the test suite
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import sys
import time as systime
import tracemalloc
from datetime import date, datetime, time, timedelta
from pathlib import Path

from intelligence.selector_945 import decide, render
from intelligence.selector_backtest import report, run_backtest
from intelligence.selector_config import SelectorConfig, load_weights
from intelligence.selector_data import IST, freeze_information_set, load_session_file, parse_payload
from intelligence.selector_outcomes import evaluate_decision
from intelligence.selector_store import DecisionExists, Store

BASE_URL = "http://140.245.226.102:10000"
STOCKS_PATH = "public/live.json"
INDEX_PATH = "public/nifty.json"


def now() -> datetime:
    return datetime.now(IST)


def sleep(seconds: float) -> None:
    systime.sleep(seconds)


def beep() -> None:
    print("\a", end="", flush=True)
    try:
        import winsound
        winsound.Beep(1200, 400)
    except Exception:
        pass


def load_sectors() -> dict:
    p = Path("sector_map.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def fetch(base_url: str):
    """One atomic stock snapshot + (optional) NIFTY candles from the existing PSYGRID API."""
    from psygrid_client import PsygridClient
    client = PsygridClient(base_url, timeout=15.0)
    with contextlib.redirect_stdout(io.StringIO()):
        stocks = client._get(STOCKS_PATH)
    try:
        index = client._get(INDEX_PATH)
    except Exception:
        index = None
    return stocks, index


def print_outcomes(store: Store, snap) -> None:
    outs = store.outcomes(snap.decision_id)
    if not outs:
        print("OUTCOMES     : not evaluated yet (python 945.py --evaluate after 10:15)")
        return
    print("OUTCOMES (after the decision -- the decision itself never changes)")
    for h in sorted(outs):
        o = outs[h]
        fr = "n/a" if o["forward_return_pct"] is None else f"{o['forward_return_pct']:+.3f}%"
        print(f"  +{h:>2} min: {o['outcome']:<10} return {fr:>9} | MFE {o['mfe_pct'] or 0:+.3f}% | "
              f"MAE {o['mae_pct'] or 0:+.3f}% | to +move {o['minutes_to_favorable']} min | "
              f"to -move {o['minutes_to_adverse']} min | bars {o['bars']}")


def run_decision(raw, cfg, weights, mode, store: Store | None, history_store: Store | None):
    si = freeze_information_set(raw, cfg.cutoff)
    day = si.session_date.isoformat()
    baselines = store.baselines(day, cfg.baseline_lookback_days, cfg.baseline_min_days) if store else {}
    history = history_store.history(before=day) if history_store else []
    snap, inner = decide(si, cfg, weights, mode, load_sectors(), baselines, history)
    if store:
        store.save_decision(snap)
        store.save_baselines(day, {s: (f.get("cumulative_volume"), f.get("realized_vol_pct"))
                                   for s, f in inner["table"].features.items() if f})
    return snap


def cmd_live(args, cfg, weights) -> int:
    from run_engine import is_trading_day
    current = now()
    if not is_trading_day(current.date()):
        print(f"NOT A NORMAL NSE TRADING DAY: {current.date()}")
        return 20
    store = Store(args.db)
    existing = store.get_decision(current.date().isoformat(), "live")
    if existing:
        print("Today's 09:45 decision is already published and immutable:\n")
        print(render(existing))
        print_outcomes(store, existing)
        return 0
    publish_at = datetime.combine(current.date(), cfg.cutoff, IST) + timedelta(seconds=3)
    if current < publish_at:
        print(f"PSYGRID 945 waiting for the 09:45 freeze ({publish_at:%H:%M:%S} IST)...", flush=True)
        while now() < publish_at:
            sleep(min(1.0, max(0.05, (publish_at - now()).total_seconds())))
    deadline = now() + timedelta(seconds=30)
    while True:
        try:
            stocks, index = fetch(args.base_url)
            raw = parse_payload(stocks, index, source=f"{args.base_url}/{STOCKS_PATH}")
        except Exception as exc:
            if now() > deadline:
                print(f"FATAL: PSYGRID feed unavailable: {exc}")
                return 30
            sleep(3)
            continue
        if raw.session_date != current.date():
            if now() > deadline:
                print(f"FATAL: feed is serving session {raw.session_date}, not today ({current.date()}). "
                      f"No decision published.")
                return 31
            sleep(3)
            continue
        last = max((s.ts[-1] for s in raw.stocks.values() if len(s)), default=None)
        need = datetime.combine(raw.session_date, cfg.cutoff, IST) - timedelta(minutes=1)
        if (last is not None and last >= need) or now() > deadline:
            break
        sleep(3)                  # 09:44 candle not published yet -- retry briefly
    history_store = Store(args.calibration_db) if args.calibration_db else store
    try:
        snap = run_decision(raw, cfg, weights, "live", store, history_store)
    except DecisionExists:
        snap = store.get_decision(raw.session_date.isoformat(), "live")
    print(render(snap))
    beep()
    return 0


def cmd_evaluate(args, cfg) -> int:
    store = Store(args.db)
    day = args.date or now().date().isoformat()
    snap = store.get_decision(day, args.mode)
    if not snap:
        print(f"No {args.mode} decision stored for {day}.")
        return 40
    raw = load_session_file(args.file) if args.file else parse_payload(*fetch(args.base_url))
    outs = evaluate_decision(snap.to_dict(), raw, cfg.horizons_min, cfg.move_threshold_pct, cfg.prob_threshold_pct)
    store.save_outcomes(snap.decision_id, outs)
    print(f"{snap.decision_date} {snap.selected_symbol} {snap.direction} (score {snap.selection_score:.1f})")
    print_outcomes(store, snap)
    return 0


def cmd_archive(args, cfg) -> int:
    stocks, index = fetch(args.base_url)
    raw = parse_payload(stocks, index)
    out_dir = Path(cfg.sessions_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{raw.session_date.isoformat()}.json.gz"
    blob = {"archived_at": now().isoformat(), "stocks_payload": stocks, "index_payload": index}
    path.write_bytes(gzip.compress(json.dumps(blob, separators=(",", ":")).encode()))
    bars = max((len(s) for s in raw.stocks.values()), default=0)
    print(f"ARCHIVED {path} | {len(raw.stocks)} stocks | up to {bars} candles each | "
          f"NIFTY {'yes' if raw.index is not None else 'no'}")
    if now().time() < time(15, 30):
        print("NOTE: archive again after 15:30 to capture the full session for outcome evaluation.")
    return 0


def cmd_replay(args, cfg, weights) -> int:
    raw = load_session_file(args.replay)
    store = None if args.no_store else Store(args.db)
    try:
        snap = run_decision(raw, cfg, weights, "replay", store, store)
    except DecisionExists:
        snap = store.get_decision(raw.session_date.isoformat(), "replay")
        print("(replay decision for this date already stored -- showing the immutable original)\n")
    print(render(snap))
    outs = evaluate_decision(snap.to_dict(), raw, cfg.horizons_min, cfg.move_threshold_pct, cfg.prob_threshold_pct)
    if store:
        store.save_outcomes(snap.decision_id, outs)
        print_outcomes(store, snap)
    else:
        for o in outs:
            print(f"  +{o.horizon_min} min: {o.outcome} {o.forward_return_pct}")
    return 0


def cmd_benchmark(args, cfg, weights) -> int:
    from intelligence.selector_features import build_feature_table
    from intelligence.selector_scoring import LinearEvidenceModel
    from intelligence.selector_synthetic import make_index_payload, make_payload

    n = args.stocks
    sectors = load_sectors()
    ok = True
    for label, minutes in (("LIVE 09:45 FEED", 30), ("FULL-DAY FEED (backtest / late run)", 375)):
        blob = json.dumps(make_payload(n, minutes=minutes, seed=3)).encode()
        index = make_index_payload(minutes=minutes)
        print(f"\nBENCHMARK {label}: {n} stocks x {minutes} one-minute candles ({len(blob) / 1e6:.1f} MB JSON)")
        t0 = systime.perf_counter()
        raw = parse_payload(json.loads(blob), index)
        t1 = systime.perf_counter()
        si = freeze_information_set(raw, cfg.cutoff)
        t2 = systime.perf_counter()
        table = build_feature_table(si, cfg, sectors, {})
        t3 = systime.perf_counter()
        ranked = LinearEvidenceModel(weights).rank(table)
        t4 = systime.perf_counter()
        snap, _ = decide(si, cfg, weights, "replay", sectors, {}, [])
        t5 = systime.perf_counter()
        print(f"  JSON decode + parse          : {t1 - t0:7.3f} s")
        print(f"  09:45 freeze + fingerprint   : {t2 - t1:7.3f} s")
        print(f"  feature matrix               : {t3 - t2:7.3f} s")
        print(f"  ranking ({len(ranked):>4} directional) : {t4 - t3:7.3f} s")
        print(f"  decide() end-to-end          : {t5 - t4:7.3f} s  (features + ranking + snapshot)")
        print(f"  TOTAL feed bytes -> decision : {(t2 - t0) + (t5 - t4):7.3f} s")
        tracemalloc.start()
        snap2, _ = decide(freeze_information_set(parse_payload(json.loads(blob), index), cfg.cutoff),
                          cfg, weights, "replay", sectors, {}, [])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        same = snap.decision_fingerprint == snap2.decision_fingerprint
        ok &= same
        print(f"  peak traced memory           : {peak / 1e6:7.1f} MB")
        print(f"  universe/eligible/excluded   : {snap.universe_size} / {snap.eligible_count} / {snap.excluded_count} "
              f"{snap.to_dict()['exclusion_reasons']}")
        print(f"  selected                     : {snap.selected_symbol} {snap.direction} score {snap.selection_score:.1f}")
        print(f"  deterministic re-run         : {'IDENTICAL' if same else 'DIFFERENT'} ({snap.decision_fingerprint[:16]})")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="945.py", description="PSYGRID 09:45 intraday selector",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true", help="print a stored decision (and outcomes)")
    g.add_argument("--evaluate", action="store_true", help="evaluate +5/+15/+30 min outcomes")
    g.add_argument("--archive", action="store_true", help="save today's full feed to data/sessions/")
    g.add_argument("--replay", metavar="FILE", help="decide from a saved session file")
    g.add_argument("--backtest", nargs="+", metavar="PATH", help="session files/dirs for walk-forward replay")
    g.add_argument("--report", action="store_true", help="print the stored backtest report")
    g.add_argument("--benchmark", action="store_true", help="production-scale synthetic benchmark")
    g.add_argument("--self-test", action="store_true", help="run the 945 test suite")
    p.add_argument("--date", help="YYYY-MM-DD for --show/--evaluate (default today)")
    p.add_argument("--mode", default="live", choices=("live", "replay", "backtest"), help="decision mode for --show/--evaluate")
    p.add_argument("--file", help="with --evaluate: use a saved session file instead of the live feed")
    p.add_argument("--db", default=None, help="SQLite store (default data/psygrid_945.sqlite; backtests use data/backtest_945.sqlite)")
    p.add_argument("--calibration-db", help="use another store's earlier decisions for walk-forward probability")
    p.add_argument("--weights", help="model weights JSON (default intelligence/selector_weights_v1.json)")
    p.add_argument("--no-store", action="store_true", help="with --replay: do not persist")
    p.add_argument("--stocks", type=int, default=989, help="with --benchmark: universe size")
    p.add_argument("--base-url", default=BASE_URL)
    args = p.parse_args(argv)

    cfg = SelectorConfig()
    weights = load_weights(args.weights)
    if args.db is None:
        args.db = "data/backtest_945.sqlite" if (args.backtest or args.report) else cfg.db_path

    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_945")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 10
    if args.benchmark:
        return cmd_benchmark(args, cfg, weights)
    if args.backtest:
        results = run_backtest(args.backtest, args.db, cfg, weights, load_sectors())
        print(report(args.db, cfg))
        return 0 if results else 50
    if args.report:
        print(report(args.db, cfg))
        return 0
    if args.replay:
        return cmd_replay(args, cfg, weights)
    if args.archive:
        return cmd_archive(args, cfg)
    if args.evaluate:
        return cmd_evaluate(args, cfg)
    if args.show:
        store = Store(args.db)
        day = args.date or now().date().isoformat()
        snap = store.get_decision(day, args.mode)
        if not snap:
            print(f"No {args.mode} decision stored for {day}.")
            return 40
        print(render(snap))
        print_outcomes(store, snap)
        return 0
    return cmd_live(args, cfg, weights)


if __name__ == "__main__":
    sys.exit(main())
