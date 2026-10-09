"""Tests for the six-family edge research: timing, leakage, costs, candles, exits, splits."""
import copy
import gzip
import json
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from edge import families as F
from edge.costs import OLD_FLAT_PCT, CostModel, scenario_cost
from edge.data import SLOTS, DayBars, build, load, range6, slot_baseline, slot_of, universe_for
from edge.engine import run
from edge.sim import simulate
from edge.validate import holm, split, summary, walk_forward

IST = ZoneInfo("Asia/Kolkata")


def day_from(prices, vol=1000, spread=0.002):
    """DayBars from a list of closes; open = previous close."""
    d = DayBars()
    prev = prices[0]
    for s, c in enumerate(prices[:SLOTS]):
        o = prev
        d.o[s], d.c[s] = o, c
        d.h[s], d.l[s] = max(o, c) * (1 + spread), min(o, c) * (1 - spread)
        d.v[s] = vol
        prev = c
    return d


def random_day(rnd, p=100.0):
    prices = []
    for _ in range(SLOTS):
        p *= 1 + rnd.gauss(0, 0.004)
        prices.append(p)
    d = day_from(prices, spread=0.0015)
    for s in range(SLOTS):
        d.v[s] = rnd.randint(500, 5000) * (8 if rnd.random() < 0.05 else 1)
    return d


def scramble_after(day, k, rnd):
    z = copy.deepcopy(day)
    for s in range(k + 1, SLOTS):
        base = rnd.uniform(1, 10_000)
        z.o[s], z.c[s] = base, base * rnd.uniform(0.5, 1.5)
        z.h[s], z.l[s] = max(z.o[s], z.c[s]) * 1.5, min(z.o[s], z.c[s]) * 0.5
        z.v[s] = rnd.randint(0, 10 ** 7)
    return z


class TimingAndLeakage(unittest.TestCase):
    def test_signals_never_depend_on_bars_after_k(self):
        rnd = random.Random(1)
        checked = 0
        for _ in range(60):
            day = random_day(rnd, rnd.uniform(60, 3000))
            base_v = [1500.0] * SLOTS
            base_r6 = [x * 1.5 if x else None for x in range6(day)]
            for k in range(F.K_MIN, F.K_MAX + 1):
                fut = scramble_after(day, k, rnd)
                for name, (fam, kind, *_rest) in F.VARIANTS.items():
                    if kind != "single":
                        continue
                    a = F.single_signal(name, day, k, base_v, base_r6)
                    b = F.single_signal(name, fut, k, base_v, base_r6)
                    self.assertEqual(a, b, f"{name} at k={k} changed when future bars changed")
                    checked += 1 if a else 0
        self.assertGreater(checked, 50)            # the test exercised real signals

    def test_cross_sectional_ranking_never_sees_the_future(self):
        rnd = random.Random(2)
        bars = {f"S{i}": random_day(rnd, rnd.uniform(60, 3000)) for i in range(40)}
        for k in F.XS_SLOTS:
            future = {s: scramble_after(d, k, rnd) for s, d in bars.items()}
            for sign in (1, -1):
                self.assertEqual(F.xsrs(bars, list(bars), k, sign=sign), F.xsrs(future, list(bars), k, sign=sign),
                                 f"xsrs at k={k} used future data (e.g. a negative index wrapping to the close)")

    def test_entry_is_the_next_bars_open(self):
        d = day_from([100 + 0.1 * s for s in range(SLOTS)])
        r = simulate(d, 10, 1, d.c[10] - 1.0, None, 3)
        self.assertEqual((r[0], r[1]), (11, d.o[11]))

    def test_no_trade_when_the_next_bar_is_missing(self):
        d = day_from([100.0] * SLOTS)
        d.o[11] = d.h[11] = d.l[11] = d.c[11] = None
        self.assertIsNone(simulate(d, 10, 1, 99.0, 1.5, 6))

    def test_no_entry_after_the_last_exit_bar(self):
        d = day_from([100.0] * SLOTS)
        self.assertIsNone(simulate(d, 71, 1, 99.0, 1.5, 6))


class Exits(unittest.TestCase):
    def setUp(self):
        self.d = day_from([100.0] * SLOTS, spread=0.0)

    def bar(self, s, o, h, lo, c):
        self.d.o[s], self.d.h[s], self.d.l[s], self.d.c[s] = o, h, lo, c

    def test_stop_before_target_when_one_bar_touches_both(self):
        self.bar(11, 100, 100, 100, 100)
        self.bar(12, 100, 103, 98, 100)               # stop 99 and target 101.5 in one bar
        e, entry, target, exit_price, slot, reason, amb, optimistic = simulate(self.d, 10, 1, 99.0, 1.5, 6)
        self.assertEqual((exit_price, reason, amb, optimistic), (99.0, "STOP", True, target))

    def test_gap_through_the_stop_fills_at_the_open(self):
        self.bar(12, 97, 97.5, 96, 97)
        r = simulate(self.d, 10, 1, 99.0, 1.5, 6)
        self.assertEqual((r[3], r[5]), (97, "STOP"))

    def test_target_and_time_exit(self):
        self.bar(13, 100.5, 101.6, 100.4, 101.5)
        r = simulate(self.d, 10, 1, 99.0, 1.5, 6)
        self.assertEqual((r[3], r[5]), (101.5, "TARGET"))
        d = day_from([100 + 0.01 * s for s in range(SLOTS)], spread=0.0)
        r = simulate(d, 10, 1, 99.0, None, 3)
        self.assertEqual((r[4], r[5], r[3]), (13, "TIME", d.c[13]))   # 3 bars: 11, 12, 13

    def test_the_last_exit_is_the_1510_bar(self):
        d = day_from([100.0] * SLOTS, spread=0.0)
        r = simulate(d, 68, 1, 99.0, None, 6)
        self.assertEqual(r[4], 71)

    def test_risk_limits(self):
        self.assertIsNone(simulate(self.d, 10, 1, 95.0, 1.5, 6))      # 5% > 3%
        self.assertIsNone(simulate(self.d, 10, 1, 99.95, 1.5, 6))     # 0.05% < 0.10%
        self.assertIsNone(simulate(self.d, 10, 1, 100.5, 1.5, 6))     # stop above a long entry


class Costs(unittest.TestCase):
    def test_round_trip_components_for_a_5000_position(self):
        m = CostModel(order_value=5000, slippage_bps_per_side=0)
        # brokerage 0.03% x2 = 0.06; STT 0.025; exchange 0.00594; SEBI 0.0002; stamp 0.003;
        # GST 18% of (0.06 + 0.00594 + 0.0002) = 0.0119052
        self.assertAlmostEqual(m.fees_pct(), 0.06 + 0.025 + 0.00594 + 0.0002 + 0.003 + 0.0119052, places=6)

    def test_brokerage_cap_on_large_orders(self):
        m = CostModel(order_value=200000, slippage_bps_per_side=0)
        brokerage = 2 * 20 / 200000 * 100            # Rs 20 cap per order = 0.02% round trip
        expected = brokerage + 0.025 + 0.00594 + 0.0002 + 0.003 + 0.18 * (brokerage + 0.00594 + 0.0002)
        self.assertAlmostEqual(m.fees_pct(), expected, places=9)

    def test_scenarios_are_ordered_and_old_model_kept(self):
        self.assertEqual(scenario_cost("OLD_FLAT"), OLD_FLAT_PCT)
        self.assertLess(scenario_cost("BASE"), scenario_cost("ADVERSE"))
        self.assertLess(scenario_cost("ADVERSE"), scenario_cost("SEVERE"))
        self.assertAlmostEqual(scenario_cost("ADVERSE") - scenario_cost("BASE"), 0.06, places=9)


def history_file(tmp, ndays=30, nsym=30, seed=3, mutate=None):
    rnd = random.Random(seed)
    sessions, d = [], date(2026, 7, 1)
    while len(sessions) < ndays:
        if d.weekday() < 5:
            sessions.append(d)
        d += timedelta(days=1)
    symbols = {}
    for i in range(nsym):
        p, rows = rnd.uniform(60, 2000), []
        for dd in sessions:
            t0 = datetime(dd.year, dd.month, dd.day, 9, 15, tzinfo=IST)
            for s in range(SLOTS):
                o = p
                p *= 1 + rnd.gauss(0, 0.003)
                rows.append([(t0 + timedelta(minutes=5 * s)).isoformat(), o, max(o, p) * 1.001, min(o, p) * 0.999, p,
                             rnd.randint(100, 9000)])
        symbols[f"S{i:02d}"] = rows
    if mutate:
        mutate(symbols)
    body = {"fetched_at": "2026-10-09T12:00:00+05:30", "failed": {"GONE": "404"}, "symbols": symbols}
    path = Path(tmp) / "history" / "yahoo_5m.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(body).encode()))
    return path


class Candles(unittest.TestCase):
    def test_audit_counts_and_missing_candles_stay_missing(self):
        def mutate(symbols):
            rows = symbols["S00"]
            rows.append(list(rows[5]))                                   # duplicate
            rows.append([rows[6][0].replace(":45:", ":47:"), 1, 1, 1, 1, 1])  # off grid
            rows[7] = [rows[7][0], 10, 5, 4, 6, 100]                     # high < open: invalid
            del rows[20]                                                 # missing candle
        with tempfile.TemporaryDirectory() as tmp:
            ds = load(history_file(tmp, mutate=mutate))
        a = ds.audit
        self.assertEqual((a["duplicates"], a["off_grid"], a["invalid_ohlc"]), (1, 1, 1))
        first = ds.days["S00"][ds.sessions[0]]
        self.assertIsNone(first.c[20])             # never filled with a price or a zero
        self.assertIsNone(first.v[20])
        self.assertIsNone(first.c[7])
        self.assertEqual(ds.failed, {"GONE": "404"})

    def test_partial_last_day_is_dropped(self):
        def mutate(symbols):
            for rows in symbols.values():
                del rows[-40:]                     # the download day stops at 12:00
        with tempfile.TemporaryDirectory() as tmp:
            ds = load(history_file(tmp, ndays=12, mutate=mutate))
        self.assertEqual(len(ds.sessions), 11)
        self.assertEqual(len(ds.audit["dropped_dates"]), 1)

    def test_slot_mapping(self):
        self.assertEqual(slot_of(datetime(2026, 1, 1, 9, 15)), 0)
        self.assertEqual(slot_of(datetime(2026, 1, 1, 15, 25)), 74)
        self.assertIsNone(slot_of(datetime(2026, 1, 1, 15, 30)))
        self.assertIsNone(slot_of(datetime(2026, 1, 1, 9, 17)))

    def test_universe_and_baselines_use_only_earlier_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = load(history_file(tmp, ndays=15))
        i = 12
        before = universe_for(ds, i)
        base = slot_baseline(ds, "S01", i, lambda d: d.v)
        for per in ds.days.values():               # wreck today and later sessions
            for d in ds.sessions[i:]:
                day = per[d]
                for s in range(SLOTS):
                    day.v[s] = 10 ** 9
                    day.c[s] = (day.c[s] or 1) * 50
        self.assertEqual(universe_for(ds, i), before)
        self.assertEqual(slot_baseline(ds, "S01", i, lambda d: d.v), base)


class Validation(unittest.TestCase):
    def test_split_is_chronological_with_an_untouched_holdout(self):
        days = [date(2026, 7, 1) + timedelta(days=i) for i in range(50)]
        folds, hold, adequate = split(days)
        flat = [d for f in folds for d in f]
        self.assertEqual(flat + hold, days)
        self.assertEqual(len(hold), 10)
        self.assertTrue(all(a < b for a, b in zip(flat, flat[1:])))
        self.assertLess(flat[-1], hold[0])
        self.assertFalse(adequate)                 # 10-session holdout < 15: flagged inadequate
        folds, hold, adequate = split([date(2025, 1, 1) + timedelta(days=i) for i in range(250)])
        self.assertTrue(adequate)

    def test_walk_forward_never_trains_on_its_test_fold_or_the_holdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds = load(history_file(tmp, ndays=40, nsym=25))
        trades, _ = run(ds, variants=["SWEEP_L6", "SWEEP_L12", "FAILBO_N3"])
        folds, hold, _ = split(ds.sessions[10:])
        import edge.validate as V
        seen = []
        real = V.select_variant

        def spy(family, tr, train_days, cost):
            seen.append(set(train_days))
            return real(family, tr, train_days, cost)
        V.select_variant = spy
        try:
            res = walk_forward(trades, folds, 0.1)
        finally:
            V.select_variant = real
        hold_set = set(hold)
        for i, train in enumerate(seen):
            f = i % 3 + 1
            self.assertFalse(train & set(folds[f]))
            self.assertFalse(train & hold_set)
        for fam, r in res.items():
            self.assertFalse({t.day for t in r["oos"]} & hold_set)

    def test_holm_and_bootstrap(self):
        adj = holm({"a": 0.01, "b": 0.02, "c": 0.5})
        self.assertAlmostEqual(adj["a"], 0.03)
        self.assertAlmostEqual(adj["b"], 0.04)
        self.assertAlmostEqual(adj["c"], 0.5)
        with tempfile.TemporaryDirectory() as tmp:
            ds = load(history_file(tmp, ndays=25, nsym=20))
        trades, _ = run(ds, variants=["SWEEP_L6"])
        s1, s2 = summary(trades, 0.1), summary(trades, 0.1)
        self.assertEqual(s1, s2)                   # deterministic
        self.assertLessEqual(s1["ci"][0], s1["net"])
        self.assertGreaterEqual(s1["ci"][1], s1["net"])


if __name__ == "__main__":
    unittest.main()
