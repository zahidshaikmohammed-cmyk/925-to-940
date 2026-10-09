"""Tests for edge/diagnose.py: forward returns, excursions, strength without look-ahead, stats."""
import random
import unittest
from datetime import date

from edge import families as F
from edge.data import SLOTS
from edge.diagnose import (boot_mean, bucket, capital_drawdown, forward, quantile_edges, strength)
from edge.sim import make_trade
from tests.test_edge import day_from, random_day, scramble_after


class Fwd:
    def __init__(self, e, side, entry):
        self.entry_slot, self.side, self.entry = e, side, entry


class ForwardReturns(unittest.TestCase):
    def test_return_mfe_mae_and_direction(self):
        d = day_from([100 + s for s in range(SLOTS)], spread=0.0)       # rises 1 per bar
        r, fav, adv, status = forward(Fwd(11, 1, d.o[11]), d, 6)        # bars 11..16
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(r, (d.c[16] / d.o[11] - 1) * 100)
        self.assertGreater(fav, 0)
        self.assertEqual(adv, 0.0)
        r_short, *_ = forward(Fwd(11, -1, d.o[11]), d, 6)
        self.assertAlmostEqual(r_short, -r)

    def test_truncated_past_1515_and_missing_end_candle(self):
        d = day_from([100.0] * SLOTS)
        self.assertEqual(forward(Fwd(60, 1, 100.0), d, 24)[3], "truncated")   # would end after 15:10
        d.c[16] = None
        self.assertEqual(forward(Fwd(11, 1, 100.0), d, 6)[3], "missing")      # never invented

    def test_sparse_window_gives_no_excursion(self):
        d = day_from([100.0] * SLOTS)
        for s in range(11, 16):
            d.c[s] = None                        # 1 of 6 candles present
        r, fav, adv, status = forward(Fwd(11, 1, 100.0), d, 6)
        self.assertEqual(status, "ok")
        self.assertIsNone(fav)


class StrengthHasNoLookAhead(unittest.TestCase):
    def test_strength_ignores_bars_after_the_signal(self):
        rnd = random.Random(4)
        checked = 0
        for _ in range(40):
            day = random_day(rnd, rnd.uniform(60, 3000))
            base_v = [1500.0] * SLOTS
            base_r6 = [10.0] * SLOTS
            for name, (fam, kind, _p, tr, hold) in F.VARIANTS.items():
                if kind != "single":
                    continue
                for k in range(F.K_MIN, F.K_MAX + 1):
                    sig = F.single_signal(name, day, k, base_v, base_r6)
                    if not sig:
                        continue
                    t = make_trade(fam, name, "X", date(2026, 1, 1), day, k, sig[0], sig[1], tr, hold)
                    if not t:
                        continue
                    a = strength(t, day, base_v, base_r6)
                    b = strength(t, scramble_after(day, k, rnd), base_v, base_r6)
                    self.assertEqual(a, b, f"{name} strength at k={k} used future bars")
                    checked += 1
                    break
        self.assertGreater(checked, 30)


class Stats(unittest.TestCase):
    def test_quantiles_and_buckets(self):
        edges = quantile_edges(list(range(100)))
        self.assertEqual(edges, [20, 40, 60, 80])
        self.assertEqual([bucket(x, edges) for x in (0, 20, 59, 99)], [0, 1, 2, 4])
        self.assertIsNone(quantile_edges([1, 2, 3]))
        self.assertIsNone(bucket(None, edges))

    def test_boot_mean_is_day_clustered_and_deterministic(self):
        data = {date(2026, 1, d): [1.0, 1.0] if d % 2 else [-1.0] for d in range(1, 21)}
        a, b = boot_mean(data), boot_mean(data)
        self.assertEqual(a, b)
        mean, lo, hi, p, n, days = a
        self.assertEqual((n, days), (30, 20))
        self.assertAlmostEqual(mean, (20 - 10) / 30)
        self.assertLess(lo, mean)
        self.assertGreater(hi, mean)

    def test_capital_limit_caps_open_positions(self):
        class T:
            def __init__(self, e, x, g):
                self.day, self.entry_slot, self.exit_slot, self.symbol, self.gross_pct = date(2026, 1, 1), e, x, "S", g
        trades = [T(10, 20, 1.0) for _ in range(30)]          # 30 overlapping signals
        taken, eq, dd = capital_drawdown(trades, 0.0, positions=19, size=5000)
        self.assertEqual(taken, 19)
        self.assertAlmostEqual(eq, 19 * 50.0)


if __name__ == "__main__":
    unittest.main()
