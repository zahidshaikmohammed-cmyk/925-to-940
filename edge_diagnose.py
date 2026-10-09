"""edge_diagnose.py -- diagnostic investigation of the frozen six-family signals.

    python edge_diagnose.py        (prints the report; saves data/edge_diagnostics.txt)

Uses the same data, signals, walk-forward folds and holdout as edge_research.py. No strategy
rule is changed; every threshold below is chosen on earlier training sessions only, and the
final holdout is evaluated once with choices frozen on the walk-forward sessions.
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from pathlib import Path

from edge import families as F
from edge.costs import scenario_cost
from edge.data import load
from edge.diagnose import (HORIZONS, boot_mean, bucket, build_events, by_day, capital_drawdown, concurrency,
                           matched_random, quantile_edges)
from edge.engine import WARMUP, run
from edge.validate import final_holdout, holm, split, walk_forward

BASE = scenario_cost("BASE")
ADV = scenario_cost("ADVERSE")
H_ALL = list(HORIZONS) + ["EOD"]


def ci(r, fmt="{:+.3f}"):
    if not r:
        return "      n/a"
    m, lo, hi, p, n, d = r
    return f"{fmt.format(m)} [{lo:+.3f},{hi:+.3f}] n{n} d{d}"


def spread_boot(events, edges_of, value, draws=1000, seed=9):
    """Top-quintile minus bottom-quintile mean, day-clustered bootstrap."""
    days = defaultdict(lambda: ([], []))
    for ev in events:
        b = bucket(ev.strength, edges_of(ev))
        v = value(ev)
        if b is None or v is None:
            continue
        if b == 4:
            days[ev.t.day][0].append(v)
        elif b == 0:
            days[ev.t.day][1].append(v)
    keys = list(days)
    if not keys:
        return None

    def stat(sample):
        top = [x for d in sample for x in days[d][0]]
        bot = [x for d in sample for x in days[d][1]]
        return (sum(top) / len(top) - sum(bot) / len(bot)) if top and bot else None
    obs = stat(keys)
    if obs is None:
        return None
    rnd = random.Random(seed)
    sims = sorted(s for s in (stat([keys[rnd.randrange(len(keys))] for _ in keys]) for _ in range(draws))
                  if s is not None)
    if not sims:
        return None
    return obs, sims[int(0.025 * len(sims))], sims[int(0.975 * len(sims)) - 1], \
        sum(1 for s in sims if s <= 0) / len(sims)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default="data")
    args = p.parse_args(argv)
    path = Path(args.data_dir) / "history" / "yahoo_5m.json.gz"
    if not path.exists():
        print(f"{path} not found: run  python 945.py --bootstrap  first")
        return 1
    print(f"Loading {path} ...", flush=True)
    ds = load(path)
    print("Regenerating the frozen trades ...", flush=True)
    trades, _ = run(ds)
    folds, holdout, adequate = split(ds.sessions[WARMUP:])
    wf = walk_forward(trades, folds, BASE)
    frozen = final_holdout(trades, folds, holdout, BASE)
    print(f"Building diagnostics for {len(trades)} signals ...", flush=True)
    events = build_events(ds, trades)
    ev_of = {id(e.t): e for e in events}
    hold_set = set(holdout)
    fold_of = {d: i for i, f in enumerate(folds) for d in f}

    def fold_plan(fam):
        """[(fold, variant, train_days, test_days)] exactly as the frozen walk-forward chose."""
        return [(f, v, {d for fold in folds[:f] for d in fold}, set(folds[f])) for f, v, _ in wf[fam]["folds"]]

    def oos_events(fam):
        return [ev_of[id(t)] for t in wf[fam]["oos"]]

    out = ["=" * 104, "SIX-FAMILY DIAGNOSTICS -- frozen signals, walk-forward folds 1-3 unless stated, gross % "
           "per trade before costs", "=" * 104,
           f"Sessions {len(ds.sessions)}; walk-forward " + ", ".join(f"F{i} {f[0]}..{f[-1]}" for i, f in enumerate(folds))
           + f"; holdout {holdout[0]}..{holdout[-1]} ({len(holdout)}). Base cost {BASE:.3f}%, adverse {ADV:.3f}%.",
           "CI = 95% day-clustered bootstrap; n = signals, d = sessions. " + ("" if adequate else
           "The holdout (10 sessions) is INADEQUATE for confirming anything.")]

    # ------------------------------------------------------------------ 0 pipeline audit
    out += ["", "0. PIPELINE AUDIT (tests: python -m pytest tests/test_edge.py)", "-" * 104,
            "  D1 max_dd in edge_research.py sums % of every trade incl. simultaneous ones: NOT a capital drawdown.",
            "  D2 no capital/concurrency limit: totals assume every signal is taken (per-trade averages unaffected).",
            "  D3 stops touched inside a bar fill exactly at the stop (optimistic); gaps fill at the open.",
            "  D4 only the first signal per symbol/variant/day is taken (favours early-session signals).",
            "  D5 costs: Rs 5,000 position, brokerage min(Rs20, 0.03%)/order, STT 0.025% sell, exch 0.00297%, SEBI,",
            "     GST 18%, stamp 0.003% buy, slippage 2 bps/side (BASE). Arithmetic is unit-tested.",
            f"  {'family':<9} {'trades/day med':>14} {'max':>5} {'open at once med':>16} {'max':>5}  "
            f"{'Rs19k x5 margin, 19 slots of Rs5,000: taken  net Rs  maxDD Rs':>44}"]
    conc = concurrency([t for fam in F.FAMILIES for t in wf[fam]["oos"]])
    for fam in F.FAMILIES:
        if fam not in conc:
            continue
        a, b, c, d = conc[fam]
        taken, eq, dd = capital_drawdown(wf[fam]["oos"], BASE)
        out.append(f"  {fam:<9} {a:>14.0f} {b:>5} {c:>16.0f} {d:>5}  {taken:>14} {eq:>+9.0f} {dd:>+9.0f}")
    gap = [abs(t.entry / ds.days[t.symbol][t.day].c[t.signal_slot] - 1) * 100 for t in trades]
    stops = [t for t in trades if t.reason == "STOP"]
    gapped = sum(1 for t in stops if abs(t.exit - t.stop) > 1e-9)
    out.append(f"  Entry vs signal close: median |gap| {sorted(gap)[len(gap) // 2]:.3f}%. Stops: {len(stops)}, "
               f"{gapped} filled at a worse open (gap), the rest exactly at the stop (D3).")

    # ------------------------------------------------------------------ 1 forward returns
    out += ["", "1. FORWARD GROSS RETURN by horizon (minutes from entry; truncated = would pass 15:15)", "-" * 104]
    for fam in F.FAMILIES:
        evs = oos_events(fam)
        out.append(f"  {fam}  ({len(evs)} OOS signals)")
        for h in H_ALL:
            r = boot_mean(by_day(evs, lambda e, h=h: e.fwd.get(h)))
            st = defaultdict(int)
            for e in evs:
                st[e.status.get(h, "ok") if h != "EOD" else ("ok" if e.fwd["EOD"] is not None else "missing")] += 1
            absm = [abs(e.fwd[h]) for e in evs if e.fwd.get(h) is not None]
            label = f"{h}m" if h != "EOD" else "EOD"
            out.append(f"    {label:>5} {ci(r)}  mean|move| {sum(absm) / max(1, len(absm)):.3f}%  "
                       f"truncated {st['truncated']} missing {st['missing']}")
    rnd = matched_random(ds, [e for fam in F.FAMILIES for e in oos_events(fam)])
    line = []
    for h in HORIZONS:
        xs = [r[h][0] for r in rnd if r[h][0] is not None]
        line.append(f"{h}m {sum(xs) / max(1, len(xs)):+.3f} (|{sum(abs(x) for x in xs) / max(1, len(xs)):.3f}|)")
    out.append("  RANDOM entries (same symbols/days): " + "  ".join(line))

    # ------------------------------------------------------------------ 2 MFE / MAE
    out += ["", "2. EXCURSIONS (mean MFE / mean MAE %, share of signals whose MFE ever exceeds the base cost)",
            "-" * 104]
    for fam in F.FAMILIES:
        evs = oos_events(fam)
        cells = []
        for h in (15, 30, 60, 120):
            fav = [e.mfe[h] for e in evs if e.mfe.get(h) is not None]
            adv = [e.mae[h] for e in evs if e.mae.get(h) is not None]
            if fav:
                cells.append(f"{h}m {sum(fav) / len(fav):.3f}/{sum(adv) / len(adv):+.3f} "
                             f"({sum(1 for x in fav if x > BASE) / len(fav):.0%})")
        out.append(f"  {fam:<9} " + "  ".join(cells))

    # ------------------------------------------------------------------ 3 strength buckets
    out += ["", "3. SIGNAL STRENGTH -> 30-min forward return (quintiles fixed on each fold's TRAINING signals)",
            "-" * 104, f"  {'family':<9} {'Q1 (weakest)':>13} {'Q2':>8} {'Q3':>8} {'Q4':>8} {'Q5 (strongest)':>15}"
            f"  {'Q5-Q1 [95% CI]':>24} {'p':>6} {'Holm':>6}"]
    spreads, rows = {}, {}
    for fam in F.FAMILIES:
        tagged, edges_map = [], {}
        for f, v, train, test in fold_plan(fam):
            tr = [ev_of[id(t)].strength for t in trades if t.variant == v and t.day in train]
            edges_map[f] = quantile_edges(tr)
            tagged += [ev_of[id(t)] for t in trades if t.variant == v and t.day in test]
        means = []
        for b in range(5):
            xs = [e.fwd[30] for e in tagged if e.fwd.get(30) is not None
                  and bucket(e.strength, edges_map.get(fold_of.get(e.t.day))) == b]
            means.append(f"{sum(xs) / len(xs):+.3f}" if xs else "  n/a")
        sp = spread_boot(tagged, lambda e: edges_map.get(fold_of.get(e.t.day)), lambda e: e.fwd.get(30))
        spreads[fam] = sp[3] if sp else 1.0
        rows[fam] = (means, sp)
    adj = holm(spreads)
    for fam in F.FAMILIES:
        means, sp = rows[fam]
        tail = f"{sp[0]:+.3f} [{sp[1]:+.3f},{sp[2]:+.3f}] {sp[3]:>6.3f} {adj[fam]:>6.3f}" if sp else "n/a"
        out.append(f"  {fam:<9} {means[0]:>13} {means[1]:>8} {means[2]:>8} {means[3]:>8} {means[4]:>15}  {tail:>40}")

    # ------------------------------------------------------------------ 4 breakdowns
    out += ["", "4. BREAKDOWNS at 30 min (gross); H0: gross <= base cost. Holm across ALL cells; * = n<200 or d<10",
            "-" * 104]
    cells, pv = [], {}
    for fam in F.FAMILIES:
        evs = oos_events(fam)
        vol_edges = {}
        for f, v, train, test in fold_plan(fam):
            vol_edges[f] = quantile_edges([ev_of[id(t)].atr_pct for t in trades if t.variant == v and t.day in train], 3)
        groups = {
            "long": lambda e: e.t.side > 0, "short": lambda e: e.t.side < 0,
            "09:30-11:00": lambda e: e.tod == "09:30-11:00", "11:00-13:30": lambda e: e.tod == "11:00-13:30",
            "13:30-14:45": lambda e: e.tod == "13:30-14:45",
            "vol low": lambda e: bucket(e.atr_pct, vol_edges.get(fold_of.get(e.t.day))) == 0,
            "vol mid": lambda e: bucket(e.atr_pct, vol_edges.get(fold_of.get(e.t.day))) == 1,
            "vol high": lambda e: bucket(e.atr_pct, vol_edges.get(fold_of.get(e.t.day))) == 2,
            "liq top100": lambda e: (e.liq_rank or 999) <= 100,
            "liq 101-200": lambda e: 100 < (e.liq_rank or 999) <= 200,
            "liq 201-300": lambda e: 200 < (e.liq_rank or 999) <= 300}
        line = []
        for gname, fn in groups.items():
            r = boot_mean(by_day([e for e in evs if fn(e)], lambda e: e.fwd.get(30)), threshold=BASE, draws=500)
            if not r:
                continue
            key = (fam, gname)
            pv[key] = r[3]
            cells.append((key, r))
            small = "*" if r[4] < 200 or r[5] < 10 else ""
            line.append(f"{gname} {r[0]:+.3f}{small}")
        out.append(f"  {fam:<9} " + " | ".join(line))
    adjc = holm(pv)
    survivors = [(k, r) for k, r in cells if adjc[k] < 0.05 and r[0] > BASE]
    out.append(f"  Cells tested {len(cells)}; cells with gross above the base cost after Holm: "
               + (", ".join(f"{k[0]}/{k[1]} {r[0]:+.3f}" for k, r in survivors) if survivors else "NONE"))

    # ------------------------------------------------------------------ 5 selectivity
    out += ["", "5. SELECTIVITY: only the strongest signals (threshold = training top 20% / top 10% of strength)",
            "-" * 104, f"  {'family':<9} {'all 30m net':>12} {'top20% 30m net':>26} {'top10% 30m net':>26}"]
    sel_thresholds = {}
    for fam in F.FAMILIES:
        res = {}
        for share in (0.2, 0.1):
            picked = []
            for f, v, train, test in fold_plan(fam):
                tr = sorted(s for s in (ev_of[id(t)].strength for t in trades if t.variant == v and t.day in train)
                            if s is not None)
                if len(tr) < 50:
                    continue
                cut = tr[int(len(tr) * (1 - share))]
                picked += [ev_of[id(t)] for t in trades if t.variant == v and t.day in test
                           and ev_of[id(t)].strength is not None and ev_of[id(t)].strength >= cut]
            res[share] = boot_mean(by_day(picked, lambda e: (e.fwd[30] - BASE) if e.fwd.get(30) is not None else None))
        allr = boot_mean(by_day(oos_events(fam), lambda e: (e.fwd[30] - BASE) if e.fwd.get(30) is not None else None))
        out.append(f"  {fam:<9} {allr[0] if allr else float('nan'):>+12.3f} {ci(res[0.2]):>26} {ci(res[0.1]):>26}")

    # ------------------------------------------------------------------ 6 holding period
    out += ["", "6. HOLDING PERIOD: net = gross - base cost by horizon (WF OOS). Best horizon chosen here, then",
            "   tested ONCE on the holdout below together with the frozen variant and the top-20% filter.", "-" * 104]
    best_h = {}
    for fam in F.FAMILIES:
        evs = oos_events(fam)
        cells, best = [], None
        for h in H_ALL:
            r = boot_mean(by_day(evs, lambda e, h=h: (e.fwd[h] - BASE) if e.fwd.get(h) is not None else None),
                          draws=500)
            if r:
                cells.append(f"{h}{'m' if h != 'EOD' else ''} {r[0]:+.3f}")
                if best is None or r[0] > best[1]:
                    best = (h, r[0])
        best_h[fam] = best[0] if best else 30
        out.append(f"  {fam:<9} " + "  ".join(cells) + f"   best: {best_h[fam]}")

    # ------------------------------------------------------------------ 7 holdout (once)
    out += ["", "7. FINAL HOLDOUT (evaluated once; variant, horizon and top-20% threshold frozen on walk-forward)",
            "-" * 104]
    wf_days = {d for f in folds for d in f}
    any_pass = False
    for fam in F.FAMILIES:
        variant, _ = frozen[fam]
        h = best_h[fam]
        tr = sorted(s for s in (ev_of[id(t)].strength for t in trades if t.variant == variant and t.day in wf_days)
                    if s is not None)
        cut = tr[int(len(tr) * 0.8)] if len(tr) >= 50 else None
        hevs = [ev_of[id(t)] for t in trades if t.variant == variant and t.day in hold_set]
        allr = boot_mean(by_day(hevs, lambda e: (e.fwd[h] - BASE) if e.fwd.get(h) is not None else None))
        top = [e for e in hevs if cut is not None and e.strength is not None and e.strength >= cut]
        topr = boot_mean(by_day(top, lambda e: (e.fwd[h] - BASE) if e.fwd.get(h) is not None else None))
        ok = bool(allr and allr[1] > 0) or bool(topr and topr[1] > 0)
        any_pass |= ok
        out.append(f"  {fam:<9} {variant:<16} horizon {h}: all {ci(allr)}  top20% {ci(topr)}")

    out += ["", "8. CONCLUSION (computed)", "-" * 104]
    wf_pos = []
    for fam in F.FAMILIES:
        for h in H_ALL:
            r = boot_mean(by_day(oos_events(fam), lambda e, h=h: (e.fwd[h] - BASE) if e.fwd.get(h) is not None
                                 else None), draws=500)
            if r and r[1] > 0:
                wf_pos.append(f"{fam}@{h}")
    out.append("  Walk-forward family/horizon combinations with net CI entirely above 0: "
               + (", ".join(wf_pos) if wf_pos else "NONE"))
    out.append("  Holdout combinations with net CI entirely above 0: " + ("some (see 7)" if any_pass else "NONE"))
    out.append("  Read with the 10-session holdout and one year of data in mind (section 0 limitations).")
    text = "\n".join(out)
    print(text)
    (Path(args.data_dir) / "edge_diagnostics.txt").write_text(text + "\n", encoding="utf-8")
    print(f"\nSaved {Path(args.data_dir) / 'edge_diagnostics.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
