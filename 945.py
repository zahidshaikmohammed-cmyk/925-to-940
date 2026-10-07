"""945.py -- PSYGRID 945 intraday engine (model 945-V1).

SCAN MODE (default -- start it in PowerShell before 09:30 and leave it running):
    python 945.py                 rescans every stock on the feed once a minute from 09:30,
                                  keeps a live shortlist, and BEEPS only when a Tier 1 setup
                                  stays at the top for 3 scans in a row and passes the market,
                                  over-extension and after-cost checks. It then prints a
                                  stop-entry plan, tracks the fill, stop, target and time stop,
                                  and records everything in data/scan/. One trade at a time,
                                  at most 3 signals a day, stops after 2 losers.
    python 945.py --allow-tier2   also alert on Tier 2 setups
    python 945.py --risk-rupees 1000    print a share quantity for that rupee risk
    python 945.py --scan-replay FILE... run the same scan minute by minute over saved sessions
                                  (data/sessions/*.json.gz) and report results net of costs

09:45 RESEARCH DECISION (the original single-pick lifecycle, used by the scheduler):
    python 945.py --daemon        full day: 09:45 decision -> +5/+15/+30 min outcomes ->
                                  daily report -> full-session archive after 15:31. Safe to
                                  restart at any time; it resumes and never duplicates.

Reads the existing PSYGRID public endpoints only (no writes, no listening port); it
cannot affect the live data plane, and it never places orders.
TOOLS:
    python 945.py --status        today's lifecycle state (exit 2 if a step is overdue)
    python 945.py --decide-only   publish today's decision and exit
    python 945.py --evaluate      record any outcomes that are due now
    python 945.py --verify [--date D]   re-decide from the stored frozen input; fingerprints must match
    python 945.py --show [--date D]     print a stored decision and its outcomes
    python 945.py --research      cumulative live research report
    python 945.py --archive       save today's full feed now (normally automatic)
    python 945.py --replay FILE   decide from a saved session file
    python 945.py --backtest PATH...    walk-forward replay of archived sessions + report
    python 945.py --report        backtest report again
    python 945.py --benchmark     989-stock benchmark incl. the integrated persistence path
    python 945.py --self-test     945 test suite
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time as systime
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path

from intelligence.selector_945 import decide, render
from intelligence.selector_backtest import report, run_backtest
from intelligence.selector_config import MODEL_ID, SelectorConfig, load_weights
from intelligence.selector_data import IST, freeze_information_set, load_session_file, parse_payload
from intelligence.selector_feed import fetch_payloads, validate_feed
from intelligence.selector_lifecycle import Clock, Lifecycle
from intelligence.selector_outcomes import evaluate_decision
from intelligence.selector_research import research_report
from intelligence.selector_scan import (Journal, ScanConfig, Scanner, information_set, render_day,
                                        render_heartbeat, render_signal, render_update, replay, stats)
from intelligence.selector_store import DecisionExists, Store

BASE_URL = "http://140.245.226.102:10000"


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


def alert_beep() -> None:
    """A new signal: three loud beeps, so it is heard from across the room."""
    print("\a", end="", flush=True)
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1500, 350)
            systime.sleep(0.12)
    except Exception:
        pass


def fetch(base_url: str):
    return fetch_payloads(base_url)


def load_sectors(path: str = "sector_map.json") -> tuple[dict, str]:
    """Sector classification is pluggable: any {symbol: sector} JSON. Unlisted symbols stay
    unclassified (sector features null). Returns (map, 'file@sha256-prefix')."""
    p = Path(path)
    try:
        raw = p.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return data, f"{p.name}@{hashlib.sha256(raw).hexdigest()[:12]}"
    except Exception:
        pass
    return {}, "none"


def is_trading_day(day) -> bool:
    from run_engine import is_trading_day as rule
    return rule(day)


def lifecycle(args, cfg, weights) -> Lifecycle:
    sectors, source = load_sectors(args.sector_map)
    return Lifecycle(cfg, weights, args.db, args.base_url, sectors, source, is_trading_day,
                     Clock(lambda: now(), lambda s: sleep(s)), fetch=lambda b: fetch(b),
                     beep=lambda: beep(), out=print, calibration_db=args.calibration_db)


def print_outcomes(store: Store, snap) -> None:
    outs = store.outcomes(snap.decision_id)
    if not outs:
        print("OUTCOMES     : none recorded yet (recorded automatically after 09:51 / 10:01 / 10:16)")
        return
    print("OUTCOMES (appended after the decision -- the decision itself never changes)")
    for h in sorted(outs):
        o = outs[h]
        fr = "n/a" if o["forward_return_pct"] is None else f"{o['forward_return_pct']:+.3f}%"
        print(f"  +{h:>2} min: {o['outcome']:<16} return {fr:>9} | MFE {o['mfe_pct'] or 0:+.3f}% @{o.get('mfe_minute')}m | "
              f"MAE {o['mae_pct'] or 0:+.3f}% @{o.get('mae_minute')}m | bars {o['bars']}")


def cmd_replay(args, cfg, weights) -> int:
    raw = load_session_file(args.replay)
    sectors, source = load_sectors(args.sector_map)
    store = None if args.no_store else Store(args.db)
    si = freeze_information_set(raw, cfg.cutoff)
    day = raw.session_date.isoformat()
    baselines = store.baselines(day, cfg.baseline_lookback_days, cfg.baseline_min_days) if store else {}
    history = store.history(before=day) if store else []
    snap, inner = decide(si, cfg, weights, "replay", sectors, baselines, history, sector_source=source)
    if store:
        try:
            store.save_decision(snap, inner["feature_rows"], inner["ranking_rows"],
                                {"path": str(args.replay), "sha256": None, "bytes": Path(args.replay).stat().st_size})
        except DecisionExists:
            snap = store.get_decision(day, "replay", MODEL_ID)
            print("(replay decision for this date already stored -- showing the immutable original)\n")
    print(render(snap))
    outs = evaluate_decision(snap.to_dict(), raw, cfg.horizons_min, cfg.move_threshold_pct, cfg.prob_threshold_pct,
                             final=True)
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
    sectors, source = load_sectors(args.sector_map)
    ok = True
    for label, minutes in (("LIVE 09:45 FEED", 30), ("FULL-DAY FEED (backtest / late run)", 375)):
        payload_bytes = json.dumps(make_payload(n, minutes=minutes, seed=3, session=now().date())).encode()
        index = make_index_payload(minutes=minutes, session=now().date())
        print(f"\nBENCHMARK {label}: {n} stocks x {minutes} one-minute candles ({len(payload_bytes) / 1e6:.1f} MB JSON)")
        t0 = systime.perf_counter()
        stocks = json.loads(payload_bytes)
        raw = parse_payload(stocks, index)
        t1 = systime.perf_counter()
        si = freeze_information_set(raw, cfg.cutoff)
        t2 = systime.perf_counter()
        health = validate_feed(raw, si, si.cutoff + timedelta(seconds=3), cfg, lambda d: True)
        t3 = systime.perf_counter()
        table = build_feature_table(si, cfg, sectors, {})
        t4 = systime.perf_counter()
        ranked = LinearEvidenceModel(weights).rank(table)
        t5 = systime.perf_counter()
        snap, inner = decide(si, cfg, weights, "live", sectors, {}, [], feed_health=health.to_dict(),
                             sector_source=source)
        t6 = systime.perf_counter()
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(str(Path(tmp) / "bench.sqlite"))
            import gzip
            blob = gzip.compress(json.dumps({"stocks_payload": stocks, "index_payload": index}).encode(), compresslevel=5)
            (Path(tmp) / "in.json.gz").write_bytes(blob)
            st.save_decision(snap, inner["feature_rows"], inner["ranking_rows"],
                             {"path": "in.json.gz", "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)})
            t7 = systime.perf_counter()
            st.close()
        tracemalloc.start()
        snap2, _ = decide(freeze_information_set(parse_payload(json.loads(payload_bytes), index), cfg.cutoff),
                          cfg, weights, "live", sectors, {}, [], feed_health=health.to_dict(), sector_source=source)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        same = snap.decision_fingerprint == snap2.decision_fingerprint
        ok &= same and health.ok
        print(f"  JSON decode + parse          : {t1 - t0:7.3f} s")
        print(f"  09:45 freeze + fingerprint   : {t2 - t1:7.3f} s")
        print(f"  feed validation              : {t3 - t2:7.3f} s  ({'PASS' if health.ok else 'FAIL: ' + '; '.join(health.failures())})")
        print(f"  feature matrix               : {t4 - t3:7.3f} s")
        print(f"  ranking ({len(ranked):>4} directional) : {t5 - t4:7.3f} s")
        print(f"  decide() incl. stability     : {t6 - t5:7.3f} s")
        print(f"  persistence (decision + {len(inner['feature_rows'])} feature rows + {len(inner['ranking_rows'])} ranks + input file): {t7 - t6:6.3f} s")
        print(f"  TOTAL integrated path        : {(t3 - t0) + (t7 - t5):7.3f} s  (parse -> validate -> decide -> persist)")
        print(f"  peak traced memory           : {peak / 1e6:7.1f} MB")
        print(f"  universe/eligible/excluded   : {snap.universe_size} / {snap.eligible_count} / {snap.excluded_count} "
              f"{snap.to_dict()['exclusion_reasons']}")
        print(f"  selected                     : {snap.selected_symbol} {snap.direction} score {snap.selection_score:.1f}")
        print(f"  deterministic re-run         : {'IDENTICAL' if same else 'DIFFERENT'} ({snap.decision_fingerprint[:16]})")
    return 0 if ok else 1


def scan_config(args) -> ScanConfig:
    from dataclasses import replace
    cfg = ScanConfig()
    changes = {"allow_tier2": args.allow_tier2, "risk_rupees": args.risk_rupees}
    if args.tier1 is not None:
        changes["tier1_score"] = args.tier1
    if args.order_value is not None:
        changes["order_value"] = args.order_value
    return replace(cfg, **changes)


def _scan_banner(cfg: ScanConfig, scanner: Scanner) -> None:
    print("=" * 72)
    print("PSYGRID 945 -- CONTINUOUS SCAN")
    print("=" * 72)
    print(f"Scans every minute {cfg.start:%H:%M}-{cfg.stop_scanning:%H:%M} | new entries until {cfg.last_entry:%H:%M} | "
          f"square-off {cfg.square_off:%H:%M}")
    print(f"Alert       : {'Tier 1 or Tier 2' if cfg.allow_tier2 else 'Tier 1 only'} (score >= "
          f"{cfg.tier2_score if cfg.allow_tier2 else cfg.tier1_score:.0f}, +{cfg.lunch_extra_score:.0f} at lunch), "
          f"top {cfg.shortlist_size} for {cfg.confirm_scans} scans in a row, with the market")
    print(f"Trade plan  : stop-entry on the candle break, {cfg.reward_r:.0f}R target, "
          f"{cfg.max_hold_minutes} min time stop, net reward/risk >= {cfg.min_net_rr} after costs")
    print(f"Costs       : ~{scanner.cost_pct:.3f}% per round trip (fees + taxes + slippage, "
          f"Rs {cfg.order_value:,.0f} order)")
    print(f"Discipline  : one trade at a time | max {cfg.max_signals_per_day} signals | "
          f"stop after {cfg.max_losses_per_day} losses")
    print("Signal only -- this program never places orders.")
    print("=" * 72)


def cmd_scan(args, weights) -> int:
    from dataclasses import replace
    cfg = scan_config(args)
    sectors, _ = load_sectors(args.sector_map)
    scanner = Scanner(cfg, weights, sectors)
    journal = Journal(Path(args.data_dir) / "scan")
    today = now().date()
    if not is_trading_day(today):
        print(f"NOT A NORMAL NSE TRADING DAY: {today}")
        return 20
    _scan_banner(cfg, scanner)
    done = journal.trades(today.isoformat())
    if done:
        scanner.restore(today, done)
        print(f"Resumed today's journal: {len(done)} signal(s) already recorded -- they will not repeat.")
        if scanner.active:
            print(f"Still tracking #{scanner.active.number} {scanner.active.symbol} {scanner.active.direction} "
                  f"({scanner.active.state}).")

    def at(t):
        return datetime.combine(today, t, IST)

    first, last = at(cfg.start), at(cfg.stop_scanning)
    if now() < first:
        print(f"Waiting for {cfg.start:%H:%M} IST (the first scan uses the 09:15-09:29 candles)...")
    scanned: set = set()
    warned = None
    try:
        while True:
            t = now()
            if t < first + timedelta(seconds=3):
                sleep(min(30.0, (first + timedelta(seconds=3) - t).total_seconds()))
                continue
            cutoff = t.replace(second=0, microsecond=0)
            if cutoff > last:
                break
            if cutoff in scanned:
                sleep(max(0.5, (cutoff + timedelta(minutes=1, seconds=3) - t).total_seconds()))
                continue
            try:
                stocks, index = fetch(args.base_url)
                raw = parse_payload(stocks, index, source=f"{args.base_url}/public/live.json")
                health = validate_feed(raw, information_set(raw, cutoff), t, scanner.sel, is_trading_day)
                problem = None if health.ok else "; ".join(health.failures())
            except Exception as exc:
                problem = f"feed error: {type(exc).__name__}: {exc}"
            if problem:
                if t.second < 45:                       # the feed may publish the candle late
                    sleep(5)
                    continue
                if warned != problem:
                    print(f"[{cutoff:%H:%M}] FEED NOT READY -- scan skipped: {problem}")
                    warned = problem
                scanned.add(cutoff)
                continue
            warned = None
            events, summary = scanner.step(raw, cutoff)
            scanned.add(cutoff)
            for ev in events:                          # trade updates first, then the scan line
                journal.write(ev)
                if ev.kind != "SIGNAL":
                    beep()
                    print(render_update(ev), flush=True)
            print(render_heartbeat(summary, cfg), flush=True)
            for ev in events:
                if ev.kind == "SIGNAL":
                    alert_beep()
                    print(render_signal(ev.trade), flush=True)
    except KeyboardInterrupt:
        print("\nStopped by you (Ctrl+C). Everything so far is saved in the journal.")
        print(render_day(scanner.trades))
        return 0
    print(render_day(scanner.trades))
    print(f"Journal: {journal.path(today.isoformat())} | all trades: {journal.root / 'trades.csv'}")
    # Save the full session so it can be replayed with --scan-replay.
    sel = replace(SelectorConfig(), sessions_dir=str(Path(args.data_dir) / "sessions"))
    target = Path(sel.sessions_dir) / f"{today.isoformat()}.json.gz"
    if target.exists() or args.no_archive:
        return 0
    print("Saving today's full session for replay after 15:31 (Ctrl+C to skip)...")
    try:
        while now() < at(sel.archive_after):
            sleep(min(30.0, (at(sel.archive_after) - now()).total_seconds() + 0.5))
        for _ in range(5):
            try:
                print(f"ARCHIVED {lifecycle_for_archive(args, sel, weights).archive_session()}")
                break
            except Exception as exc:
                print(f"archive failed ({exc}); retrying in 60 s")
                sleep(60)
    except KeyboardInterrupt:
        print("Archive skipped.")
    return 0


def lifecycle_for_archive(args, sel, weights) -> Lifecycle:
    return Lifecycle(sel, weights, str(Path(args.data_dir) / "psygrid_945.sqlite"), args.base_url, {}, "none",
                     is_trading_day, Clock(lambda: now(), lambda s: sleep(s)), fetch=lambda b: fetch(b),
                     out=print)


def cmd_scan_replay(args, weights) -> int:
    cfg = scan_config(args)
    sectors, _ = load_sectors(args.sector_map)
    files: list[Path] = []
    for item in args.scan_replay:
        p = Path(item)
        files += sorted(p.glob("*.json*")) if p.is_dir() else ([p] if p.is_file() else [])
    if not files:
        print("No session files found. The scanner and `--daemon` save one per day in data/sessions/.")
        return 50
    scanner = Scanner(cfg, weights, sectors)
    _scan_banner(cfg, scanner)
    everything = []
    for f in files:
        try:
            raw = load_session_file(f)
        except Exception as exc:
            print(f"{f}: unreadable ({exc}) -- skipped")
            continue
        print(f"\nREPLAY {raw.session_date} ({f.name})")
        show = (lambda ev: print(render_signal(ev.trade) if ev.kind == "SIGNAL" else render_update(ev))) \
            if args.verbose else None
        trades = replay(raw, scanner, on_event=show,
                        on_scan=(lambda s: print(render_heartbeat(s, cfg))) if args.verbose else None)
        print(render_day(trades, f"{raw.session_date} SUMMARY"))
        everything += trades
    st = stats(everything)
    wr = "n/a" if st["win_rate"] is None else f"{st['win_rate']:.0%}"
    print("\n" + "=" * 72)
    print(f"REPLAY TOTAL over {len(files)} session(s)")
    print(f"Signals {st['signals']} | filled {st['filled']} | expired {st['expired']} | win rate {wr}")
    print(f"Net {st['net_r']:+.2f}R after costs (gross {st['gross_r']:+.2f}R) | avg {st['avg_net_r'] if st['avg_net_r'] is not None else 'n/a'}R per trade "
          f"| exits {st['by_exit']}")
    if st["filled"] < 50:
        print(f"Only {st['filled']} filled trades: too few to judge. Keep archiving sessions and replay again at 50+.")
    print("=" * 72)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="945.py", description="PSYGRID 09:45 intraday selector (945-V1)",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    g = p.add_mutually_exclusive_group()
    for flag, hlp in (("--daemon", "run the 09:45 research-decision day (the scheduler uses this)"),
                      ("--scan", "continuous scan from 09:30 with beep alerts (same as no flag)"),
                      ("--decide-only", "publish today's decision and exit"),
                      ("--status", "today's lifecycle state"), ("--evaluate", "record outcomes that are due now"),
                      ("--verify", "reproduce a stored decision from its frozen input"),
                      ("--show", "print a stored decision and its outcomes"), ("--research", "live research report"),
                      ("--archive", "save today's full feed now"), ("--report", "backtest report"),
                      ("--benchmark", "989-stock benchmark"), ("--self-test", "run the 945 test suite")):
        g.add_argument(flag, action="store_true", help=hlp)
    g.add_argument("--replay", metavar="FILE", help="decide from a saved session file")
    g.add_argument("--scan-replay", nargs="+", metavar="PATH", help="run the scan over saved session files/dirs")
    g.add_argument("--backtest", nargs="+", metavar="PATH", help="session files/dirs for walk-forward replay")
    p.add_argument("--date", help="YYYY-MM-DD for --show / --verify (default today)")
    p.add_argument("--mode", default="live", choices=("live", "replay", "backtest"), help="decision mode for --show")
    p.add_argument("--data-dir", default="data", help="root for the store, frozen inputs, archives and reports")
    p.add_argument("--db", default=None, help="SQLite store (default DATA_DIR/psygrid_945.sqlite; backtests: DATA_DIR/backtest_945.sqlite)")
    p.add_argument("--calibration-db", help="use another store's EARLIER decisions for walk-forward probability")
    p.add_argument("--weights", help="model weights JSON (default intelligence/selector_weights_v1.json)")
    p.add_argument("--sector-map", default="sector_map.json", help="symbol->sector JSON (pluggable classification)")
    p.add_argument("--no-store", action="store_true", help="with --replay: do not persist")
    p.add_argument("--stocks", type=int, default=989, help="with --benchmark: universe size")
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--allow-tier2", action="store_true", help="scan: also alert on Tier 2 setups")
    p.add_argument("--tier1", type=float, help="scan: Tier 1 score threshold (default 85)")
    p.add_argument("--risk-rupees", type=float, help="scan: print a share quantity for this rupee risk")
    p.add_argument("--order-value", type=float, help="scan: typical order value in rupees for the cost model (default 100000)")
    p.add_argument("--verbose", action="store_true", help="scan replay: print every scan and signal")
    p.add_argument("--no-archive", action="store_true", help="scan: do not wait to save the session after the close")
    args = p.parse_args(argv)

    from dataclasses import replace
    d = Path(args.data_dir)
    cfg = replace(SelectorConfig(), db_path=str(d / "psygrid_945.sqlite"), sessions_dir=str(d / "sessions"),
                  reports_dir=str(d / "reports"))
    weights = load_weights(args.weights)
    if args.db is None:
        args.db = str(d / "backtest_945.sqlite") if (args.backtest or args.report) else cfg.db_path

    if args.scan_replay:
        return cmd_scan_replay(args, weights)
    if not any((args.daemon, args.decide_only, args.status, args.evaluate, args.verify, args.show, args.research,
                args.archive, args.report, args.benchmark, args.self_test, args.replay, args.backtest)):
        return cmd_scan(args, weights)
    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromNames(["tests.test_945", "tests.test_945_production",
                                                               "tests.test_945_scan"])
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 10
    if args.benchmark:
        return cmd_benchmark(args, cfg, weights)
    if args.backtest:
        sectors, source = load_sectors(args.sector_map)
        results = run_backtest(args.backtest, args.db, cfg, weights, sectors, sector_source=source)
        print(report(args.db, cfg))
        return 0 if results else 50
    if args.report:
        print(report(args.db, cfg))
        return 0
    if args.replay:
        return cmd_replay(args, cfg, weights)
    if args.research:
        print(research_report(Store(args.db), cfg, "live"))
        return 0
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

    lc = lifecycle(args, cfg, weights)
    if args.status:
        code, text = lc.status()
        print(text)
        return code
    if args.verify:
        ok, text = lc.verify(args.date or now().date().isoformat())
        print(("VERIFIED: " if ok else "VERIFY FAILED: ") + text)
        return 0 if ok else 60
    if args.archive:
        print(f"ARCHIVED {lc.archive_session()}")
        return 0
    if args.evaluate:
        snap = lc.decision()
        if not snap:
            print("No live decision stored for today.")
            return 40
        added = lc.record_outcomes(snap, final=now().time() >= cfg.archive_after)
        print(f"{added} new outcome horizon(s) recorded.")
        print_outcomes(lc.store, snap)
        return 0
    if args.decide_only:
        if not is_trading_day(now().date()):
            print(f"NOT A NORMAL NSE TRADING DAY: {now().date()}")
            return 20
        existing = lc.decision()
        if existing:
            print("Today's 09:45 decision is already published and immutable:\n")
            print(render(existing))
            print_outcomes(lc.store, existing)
            return 0
        snap = lc.publish_decision()
        return 0 if snap else 31
    existing = lc.decision()
    if existing:
        print("Today's 09:45 decision is already published and immutable:\n")
        print(render(existing))
        print_outcomes(lc.store, existing)
        print("\nResuming the daily lifecycle (outcomes / report / archive)...")
    return lc.run_day()


if __name__ == "__main__":
    sys.exit(main())
