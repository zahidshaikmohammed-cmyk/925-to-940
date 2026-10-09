"""edge_research.py -- six-family intraday edge research (15-30 minute holds).

    python 945.py --bootstrap     (once: data/history/yahoo_5m.json.gz)
    python edge_research.py       (prints the report; saves data/edge_report.txt and data/edge_trades.csv)

Research only. Rules, costs and the validation design are fixed in edge/ and documented in
docs/EDGE_RESEARCH.md. The original research.py and the dataset are not modified.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from edge import families as F
from edge.costs import SCENARIOS, describe, scenario_cost
from edge.data import load, slot_time
from edge.engine import WARMUP, random_baseline, run
from edge.validate import final_holdout, holm, split, summary, walk_forward

BASE = scenario_cost("BASE")


def fmt(st, cost_label=""):
    if not st or not st.get("n"):
        return "   0 trades"
    lo, hi = st["ci"]
    return (f"{st['n']:>5} {st['days']:>4} {st['win']:>5.0%} {st['gross']:>+7.3f} {st['net']:>+7.3f} "
            f"[{lo:+.3f},{hi:+.3f}] {st['pf']:>5.2f} {st['max_dd']:>+8.1f}")


HEAD = f"{'trades':>5} {'days':>4} {'win':>5} {'gross%':>7} {'net%':>7} {'95% CI net':>17} {'PF':>5} {'maxDD%':>8}"


def audit_lines(ds):
    a = ds.audit
    L = ["1. DATA AUDIT", "-" * 100,
         f"File fetched at {ds.fetched_at}. Symbols with data {a['symbols_loaded']}, failed downloads "
         f"{a['symbols_failed']} (intended universe {a['symbols_loaded'] + a['symbols_failed']}).",
         "Failed: " + (", ".join(f"{s} ({r[:40]})" for s, r in sorted(a["failed"].items())) or "none"),
         f"Candles kept {a['bars']:,}; off the 5-min grid {a['off_grid']}; duplicates {a['duplicates']}; "
         f"invalid OHLC {a['invalid_ohlc']}; zero-volume {a['zero_volume']:,}; stale runs (6+ flat bars) "
         f"{a['stale_runs']}.",
         "NOTE: the downloader (intelligence/history.parse_chart) already dropped candles with missing or "
         "invalid OHLC and de-duplicated timestamps, so those counts here are after that filtering; "
         "dropped candles show up as missing intervals below.",
         f"Dates seen {a['dates_seen']}, full sessions used {a['sessions']} "
         f"({ds.sessions[0] if ds.sessions else '-'} .. {ds.sessions[-1] if ds.sessions else '-'}). "
         f"Dropped dates: {a['dropped_dates'] or 'none'}",
         f"Symbol slot coverage over the sessions: {a['symbol_slot_coverage']}",
         f"Most-missing time slots: {a['missing_by_slot_top']}",
         f"Possible corporate actions (overnight gap > 15%): {len(a['possible_corporate_actions'])} "
         f"{a['possible_corporate_actions'][:10]}",
         "Survivorship: the symbol list is TODAY's 989-stock universe; stocks delisted, merged or renamed in "
         "the period are absent, and prices are as served by Yahoo (no corporate-action audit trail). "
         "No bid/ask or order-book data exists: spreads and depth are assumed, never observed."]
    by = a["coverage_by_date"]
    if by:
        worst = sorted(by.items(), key=lambda kv: kv[1][1])[:3]
        L.append(f"Coverage by date (symbols present, symbols with all 75 candles): worst {worst}; "
                 f"median full-day symbols {median(v[1] for v in by.values()):.0f}")
    return L


def regime_of(ds, universe_days, d):
    moves = []
    for s, per in ds.days.items():
        day = per.get(d)
        if day and day.first_open() and day.last_close():
            moves.append(day.last_close() / day.first_open() - 1)
    m = median(moves) if moves else 0
    return "UP" if m > 0.003 else ("DOWN" if m < -0.003 else "FLAT")


def breakdown(trades, key, cost):
    groups = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    return {g: summary(rows, cost, boot=200) for g, rows in sorted(groups.items())}


def top_symbol_share(trades, cost):
    by = defaultdict(float)
    for t in trades:
        by[t.symbol] += t.gross_pct - cost
    pos = sum(x for x in by.values() if x > 0)
    return (max(by.values()) / pos) if pos > 0 else 0.0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--embargo", type=int, default=0, help="sessions dropped between train and test")
    args = p.parse_args(argv)
    path = Path(args.data_dir) / "history" / "yahoo_5m.json.gz"
    if not path.exists():
        print(f"{path} not found: run  python 945.py --bootstrap  first")
        return 1
    print(f"Loading {path} ...", flush=True)
    ds = load(path)
    out = ["=" * 100, "SIX-FAMILY INTRADAY EDGE RESEARCH (15-30 minute holds) -- research only, no orders", "=" * 100]
    out += audit_lines(ds)
    if len(ds.sessions) < WARMUP + 20:
        out.append(f"Only {len(ds.sessions)} sessions: not enough for warm-up plus validation. Stopping.")
        print("\n".join(out))
        return 2

    out += ["", "2. COSTS (round trip, % of position)", "-" * 100] + describe()
    out.append("Old vs new: the base model is built from statutory rates and Dhan's brokerage on a Rs 5,000 "
               "position plus 2 bps/side slippage; OLD_FLAT is the previous 0.142%. Both are reported.")

    out += ["", "3. STRATEGIES: 6 families, 15 pre-registered variants (full rules in edge/families.py and "
            "docs/EDGE_RESEARCH.md)", "-" * 100]
    for name, (fam, kind, params, tr, hold) in F.VARIANTS.items():
        out.append(f"  {name:<16} {fam:<8} params {params}  target {tr or '-'}R  hold {hold * 5} min")

    print("Generating trades (warm-up 10 sessions, then every session) ...", flush=True)
    trades, info = run(ds, say=lambda m: print(m, flush=True))
    sessions_eval = ds.sessions[WARMUP:]
    folds, holdout, adequate = split(sessions_eval)
    wf_days = [d for f in folds for d in f]
    out += ["", "4. VALIDATION DESIGN", "-" * 100,
            f"Evaluation sessions {len(sessions_eval)} after a {WARMUP}-session warm-up. Walk-forward folds: "
            + ", ".join(f"F{i} {f[0]}..{f[-1]} ({len(f)})" for i, f in enumerate(folds) if f)
            + f". Final holdout {holdout[0]}..{holdout[-1]} ({len(holdout)} sessions). Embargo {args.embargo}.",
            f"Universe per session: top {300} by prior-10-session median turnover, price >= Rs 50, 8+ prior "
            f"sessions; median eligible {median(v['universe'] for v in info.values()):.0f}. Exclusions on the "
            f"first evaluated session: {next(iter(info.values()))['excluded']}",
            "ADEQUACY: " + ("adequate." if adequate else
                            "INADEQUATE -- the holdout has fewer than 15 sessions and/or the walk-forward fewer "
                            "than 40. Any result is a hypothesis for more data, not evidence of a durable edge.")]

    out += ["", "5. EVERY VARIANT on the walk-forward sessions (in-sample sensitivity, BASE costs)", "-" * 100,
            f"  {'variant':<16} {HEAD}"]
    for name in F.VARIANTS:
        rows = [t for t in trades if t.variant == name and t.day in set(wf_days)]
        out.append(f"  {name:<16} {fmt(summary(rows, BASE, boot=300))}")

    wf = walk_forward(trades, folds, BASE, args.embargo)
    baseline = {fam: random_baseline(ds, r["oos"]) for fam, r in wf.items()}
    stats = {fam: summary(r["oos"], BASE) for fam, r in wf.items()}
    adj = holm({fam: (s["p"] if s.get("n") else 1.0) for fam, s in stats.items()})
    hold = final_holdout(trades, folds, holdout, BASE)

    out += ["", "6. WALK-FORWARD OUT-OF-SAMPLE (folds 1-3; variant chosen on earlier folds only)", "-" * 100,
            f"  {'family':<9} {HEAD} {'p':>6} {'Holm':>6}"]
    for fam in F.FAMILIES:
        s = stats[fam]
        extra = f" {s['p']:>6.3f} {adj[fam]:>6.3f}" if s.get("n") else ""
        out.append(f"  {fam:<9} {fmt(s)}{extra}")
        for f, variant, fs in wf[fam]["folds"]:
            out.append(f"      fold {f}: {variant:<16} n {fs.get('n', 0):>4}  net {fs.get('net', 0):+.3f}%")

    out += ["", "7. GROSS vs NET, ADVERSE COSTS, OHLC AMBIGUITY, BASELINE (walk-forward OOS)", "-" * 100,
            f"  {'family':<9} {'gross%':>7} " + " ".join(f"{k:>9}" for k in SCENARIOS)
            + f" {'both-hit':>8} {'optimistic':>10} {'random':>8} {'top sym':>8}"]
    for fam in F.FAMILIES:
        rows = wf[fam]["oos"]
        if not rows:
            out.append(f"  {fam:<9} no trades")
            continue
        nets = " ".join(f"{summary(rows, scenario_cost(k), boot=1)['net']:>+9.3f}" for k in SCENARIOS)
        rb = summary(baseline[fam], BASE, boot=1) if baseline[fam] else {"net": float('nan')}
        out.append(f"  {fam:<9} {stats[fam]['gross']:>+7.3f} {nets} {stats[fam]['ambiguous']:>8} "
                   f"{stats[fam]['net_optimistic']:>+10.3f} {rb['net']:>+8.3f} {top_symbol_share(rows, BASE):>8.0%}")
    out.append("  both-hit = trades whose exit bar touched stop and target (base assumes the stop); optimistic = "
               "net if the target came first; random = matched random entries; top sym = largest single symbol's "
               "share of positive net.")

    out += ["", "8. OOS BREAKDOWNS (BASE costs): time of day and market regime", "-" * 100]
    regimes = {d: regime_of(ds, None, d) for d in sessions_eval}
    for fam in F.FAMILIES:
        rows = wf[fam]["oos"]
        if not rows:
            continue
        tod = breakdown(rows, lambda t: "09:30-11:00" if t.signal_slot < 21 else
                        ("11:00-13:30" if t.signal_slot < 51 else "13:30-14:45"), BASE)
        reg = breakdown(rows, lambda t: regimes.get(t.day, "?"), BASE)
        out.append(f"  {fam:<9} time: " + "  ".join(f"{k} n{v['n']} {v['net']:+.3f}" for k, v in tod.items()))
        out.append(f"  {'':<9} regime: " + "  ".join(f"{k} n{v['n']} {v['net']:+.3f}" for k, v in reg.items()))
    out.append("  Regime = the universe's median open-to-close move that day (known only after the close: used "
               "for reporting, never for signals). Per-year results are impossible: the data covers one year.")

    out += ["", "9. FINAL HOLDOUT (variant frozen on all walk-forward sessions; evaluated once)", "-" * 100,
            f"  {'family':<9} {'variant':<16} {HEAD} {'adverse net':>11}"]
    for fam in F.FAMILIES:
        variant, rows = hold[fam]
        s = summary(rows, BASE)
        adv = summary(rows, scenario_cost("ADVERSE"), boot=1).get("net", 0) if rows else 0
        out.append(f"  {fam:<9} {variant:<16} {fmt(s)} {adv:>+11.3f}")

    out += ["", "10. PROMOTION CRITERIA (all must hold) and RANKING", "-" * 100,
            "  C1 OOS net > 0 with Holm-adjusted p < 0.05   C2 PF >= 1.2   C3 >= 100 trades on >= 15 sessions",
            "  C4 positive in >= 2 of 3 folds   C5 holdout net > 0 on >= 20 trades   C6 OOS net > 0 at ADVERSE cost",
            "  C7 OOS net beats the matched random baseline   C8 no single symbol > 25% of positive net"]
    ranking = []
    for fam in F.FAMILIES:
        s, rows = stats[fam], wf[fam]["oos"]
        if not s.get("n"):
            ranking.append((float("-inf"), fam, "no OOS trades", False))
            continue
        hv, hrows = hold[fam]
        hs = summary(hrows, BASE, boot=1)
        rb = summary(baseline[fam], BASE, boot=1) if baseline[fam] else {"net": float("inf")}
        checks = {
            "C1": s["net"] > 0 and adj[fam] < 0.05,
            "C2": s["pf"] >= 1.2,
            "C3": s["n"] >= 100 and s["days"] >= 15,
            "C4": sum(1 for _, _, fs in wf[fam]["folds"] if fs.get("n") and fs["net"] > 0) >= 2,
            "C5": hs.get("n", 0) >= 20 and hs.get("net", -1) > 0,
            "C6": summary(rows, scenario_cost("ADVERSE"), boot=1)["net"] > 0,
            "C7": s["net"] > rb["net"],
            "C8": top_symbol_share(rows, BASE) <= 0.25,
        }
        failed = [k for k, ok in checks.items() if not ok]
        ranking.append((s["net"], fam, "PASS (candidate only)" if not failed else "fail: " + " ".join(failed),
                        not failed))
    ranking.sort(key=lambda x: -x[0])
    for i, (net, fam, verdict, _) in enumerate(ranking, 1):
        out.append(f"  #{i} {fam:<9} OOS net {net:+.3f}%/trade   {verdict}")
    passed = [fam for _, fam, _, ok in ranking if ok]
    out += ["", "11. CONCLUSION", "-" * 100]
    if passed:
        out.append(f"  Passed every criterion: {', '.join(passed)}. With {len(sessions_eval)} sessions this is a "
                   "CANDIDATE, not a proven edge: confirm on a longer, untouched history before any real money.")
    else:
        out.append("  No family passed. No robust intraday edge was demonstrated in this data after costs.")
    out.append("  " + ("" if adequate else "The final holdout is too short to validate anything durable. ")
               + "Recommended next step: a multi-year 5-minute history (e.g. the broker's historical API -- "
               "ask before downloading) and rerun this exact frozen pipeline; do not add rules to fit this sample.")

    text = "\n".join(out)
    print(text)
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    (Path(args.data_dir) / "edge_report.txt").write_text(text + "\n", encoding="utf-8")
    with open(Path(args.data_dir) / "edge_trades.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["family", "variant", "symbol", "day", "signal", "entry_time", "side", "entry", "stop", "target",
                    "exit", "exit_time", "reason", "ambiguous", "gross_pct", "net_pct_base", "risk_pct", "split"])
        hold_set, wf_set = set(holdout), set(wf_days)
        for t in trades:
            w.writerow([t.family, t.variant, t.symbol, t.day, slot_time(t.signal_slot), slot_time(t.entry_slot),
                        t.side, round(t.entry, 2), round(t.stop, 2), round(t.target, 2) if t.target else "",
                        round(t.exit, 2), slot_time(t.exit_slot), t.reason, int(t.ambiguous),
                        round(t.gross_pct, 4), round(t.gross_pct - BASE, 4), round(t.risk_pct, 3),
                        "holdout" if t.day in hold_set else ("walkforward" if t.day in wf_set else "warmup")])
    print(f"\nSaved {Path(args.data_dir) / 'edge_report.txt'} and edge_trades.csv ({len(trades)} trades)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
