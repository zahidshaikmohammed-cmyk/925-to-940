from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import StrategyConfig
from strategy_930 import Candle, atr, efficiency, evaluate, evaluate_tiers, vwap

IST = ZoneInfo("Asia/Kolkata")


def make_candles(opens_highs_lows_closes, volume=1000):
    start = datetime(2026, 9, 15, 9, 15, tzinfo=IST)
    return tuple(
        Candle(start + timedelta(minutes=i), o, h, l, c, v)
        for i, (o, h, l, c, v) in enumerate(
            [(o, h, l, c, volume) for o, h, l, c in opens_highs_lows_closes]
        )
    )


def valid_long_fixture(retrace_volume=400):
    rows = [
        (100.0, 100.8, 99.8, 100.6), (100.6, 101.8, 100.4, 101.6),
        (101.6, 103.0, 101.3, 102.8), (102.8, 104.2, 102.5, 104.0),
        (104.0, 105.0, 103.7, 104.8), (104.8, 104.9, 103.0, 103.5),
        (103.5, 104.0, 102.6, 103.0), (103.0, 103.8, 102.4, 102.8),
        (102.8, 103.5, 102.2, 102.9), (102.9, 103.8, 102.3, 103.1),
        (103.1, 104.0, 102.5, 103.4), (103.4, 104.2, 102.6, 103.6),
        (103.6, 104.4, 102.7, 103.8), (103.8, 104.5, 102.8, 104.0),
        (104.0, 104.7, 103.0, 104.2),
    ]
    volumes = [1500] * 5 + [retrace_volume] * 10
    start = datetime(2026, 9, 15, 9, 15, tzinfo=IST)
    return tuple(
        Candle(start + timedelta(minutes=i), o, h, l, c, v)
        for i, ((o, h, l, c), v) in enumerate(zip(rows, volumes))
    )


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.cfg.validate()

    def test_vwap_and_atr_are_positive(self):
        cs = make_candles([(100, 101, 99, 100.5)] * 15)
        self.assertGreater(vwap(cs), 0)
        self.assertGreater(atr(cs, 10), 0)

    def test_efficiency_monotonic_is_one(self):
        rows = [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(15)]
        self.assertAlmostEqual(efficiency(make_candles(rows)), 1.0)

    def test_exact_09_15_to_09_29_grid(self):
        cs = valid_long_fixture()
        self.assertEqual(len(cs), 15)
        self.assertEqual(cs[0].ts.time().strftime("%H:%M"), "09:15")
        self.assertEqual(cs[-1].ts.time().strftime("%H:%M"), "09:29")

    def test_strict_impulse_retracement_candidate_exists(self):
        cs = valid_long_fixture()
        candidate = evaluate("TEST", cs, 104.2, 101.0, 1.0, 1.0, [0.0] * 20, self.cfg, 1)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.side, "LONG")
        self.assertEqual(candidate.tier, 1)
        self.assertGreaterEqual(candidate.retracement_depth, self.cfg.min_retracement_depth)
        self.assertLessEqual(candidate.retracement_depth, self.cfg.max_retracement_depth)
        self.assertGreaterEqual(candidate.target - candidate.entry, 2 * abs(candidate.entry - candidate.stop))

    def test_extreme_gap_is_hard_excluded_from_all_tiers(self):
        cs = valid_long_fixture()
        for tier in (1, 2, 3):
            candidate = evaluate("GAPTEST", cs, 104.2, 95.0, 1.0, 1.0, [0.0] * 20, self.cfg, tier)
            self.assertIsNone(candidate)

    def test_high_retrace_volume_can_drop_to_fallback(self):
        cs = valid_long_fixture(retrace_volume=1300)
        candidates = evaluate_tiers("FALLBACK", cs, 104.2, 101.0, 1.0, 1.0, [0.0] * 20, self.cfg)
        self.assertTrue(candidates)
        self.assertTrue(any(c.tier > 1 for c in candidates))

    def test_candidate_risk_is_positive_and_target_is_directional(self):
        cs = valid_long_fixture()
        candidate = evaluate("RISK", cs, 104.2, 101.0, 0.5, 0.5, [0.0] * 20, self.cfg)
        self.assertIsNotNone(candidate)
        self.assertGreater(abs(candidate.entry - candidate.stop), 0)
        if candidate.side == "LONG":
            self.assertGreater(candidate.target, candidate.entry)
        else:
            self.assertLess(candidate.target, candidate.entry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
