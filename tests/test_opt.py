import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from index import IndexConfig, Zone
from opt import ForcedTrade, _zone_targets, forced_trade, grade, oi_flow, rank
from strategy_930 import IST, Candle

CHAIN = json.loads(Path(__file__).with_name("fixtures").joinpath("nifty_chain_small.json").read_text())
NOW = datetime(2026, 10, 1, 10, 30, 5, tzinfo=IST)


def ones(closes, start=22500.0):
    t0 = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
    out, prev = [], start
    for i, c in enumerate(closes):
        out.append(Candle(t0 + timedelta(minutes=i), prev, max(prev, c) + 3, min(prev, c) - 3, c, 1000.0))
        prev = c
    return out


UPTREND = ones([22500 + 2.0 * i for i in range(75)])       # steady rise to ~22650
DOWNTREND = ones([22650 - 2.0 * i for i in range(75)], start=22650.0)


class ForcedTests(unittest.TestCase):
    def setUp(self):
        self.cfg = IndexConfig()

    def test_always_returns_both_sides(self):
        trades = [forced_trade("NIFTY", UPTREND, [], 0, CHAIN, side, self.cfg, NOW) for side in (1, -1)]
        self.assertTrue(all(t is not None for t in trades))

    def test_uptrend_with_up_market_prefers_call(self):
        call = forced_trade("NIFTY", UPTREND, [], 1, CHAIN, 1, self.cfg, NOW)
        put = forced_trade("NIFTY", UPTREND, [], 1, CHAIN, -1, self.cfg, NOW)
        self.assertGreater(call.score, put.score + 20)
        self.assertEqual(rank([put, call])[0].side, "CALL")

    def test_downtrend_with_down_market_prefers_put(self):
        call = forced_trade("NIFTY", DOWNTREND, [], -1, CHAIN, 1, self.cfg, NOW)
        put = forced_trade("NIFTY", DOWNTREND, [], -1, CHAIN, -1, self.cfg, NOW)
        self.assertGreater(put.score, call.score + 20)

    def test_stop_is_clamped_and_on_the_right_side(self):
        t = forced_trade("NIFTY", UPTREND, [], 1, CHAIN, 1, self.cfg, NOW)
        self.assertLess(t.stop, t.entry)
        self.assertGreater(t.target1, t.entry)
        self.assertGreater(t.target2, t.target1)

    def test_targets_use_key_levels_when_in_reach(self):
        keys = [Zone(22700, 22705, 5.0, ("PDH", "CALL WALL")), Zone(22800, 22800, 5.0, ("ROUND", "CALL WALL"))]
        t1, t2, basis = _zone_targets(22650.0, 1, 20.0, keys)
        self.assertEqual((t1, t2, basis), (22700, 22710.0, "KEY LEVELS"))   # 22800 is 7.5R: capped
        t1, t2, basis = _zone_targets(22650.0, 1, 20.0, [])
        self.assertEqual((t1, t2), (22680.0, 22700.0))
        self.assertTrue(basis.startswith("R-MULTIPLE"))

    def test_far_key_level_is_not_used_as_target(self):
        far = [Zone(23700, 23700, 6.0, ("ROUND", "CALL WALL"))]          # ~52R away
        t1, t2, basis = _zone_targets(22650.0, 1, 20.0, far)
        self.assertEqual((t1, t2), (22680.0, 22700.0))
        self.assertIn("too far", basis)

    def test_second_target_capped(self):
        keys = [Zone(22700, 22700, 5.0, ("PDH", "CALL WALL")), Zone(23000, 23000, 5.0, ("ROUND", "CALL WALL"))]
        t1, t2, _ = _zone_targets(22650.0, 1, 20.0, keys)
        self.assertEqual(t1, 22700)
        self.assertLessEqual(t2, 22650.0 + 4.0 * 20.0)

    def test_entering_under_resistance_is_penalised(self):
        spot = UPTREND[-1].close
        free = forced_trade("NIFTY", UPTREND, [], 1, CHAIN, 1, self.cfg, NOW)
        wall = [Zone(spot + 2, spot + 4, 6.0, ("PDH", "CALL WALL"))]
        blocked = forced_trade("NIFTY", UPTREND, wall, 1, CHAIN, 1, self.cfg, NOW)
        self.assertLess(blocked.score, free.score - 10)
        self.assertIn("entering right at a key level", blocked.reasons)

    def test_option_strike_is_picked_from_chain(self):
        t = forced_trade("NIFTY", UPTREND, [], 1, CHAIN, 1, self.cfg, NOW)
        self.assertIsNotNone(t.option)
        self.assertEqual(t.option.kind, "CE")
        self.assertLess(t.option.premium_stop, t.option.ask)

    def test_oi_flow_sign(self):
        chain = {"strikes": [{"strike": 100.0, "ce": {"oi": 10, "previous_oi": 10},
                              "pe": {"oi": 50, "previous_oi": 10}}]}
        self.assertEqual(oi_flow(chain, 100.0), 1.0)

    def test_grades(self):
        self.assertEqual((grade(80), grade(60), grade(30)), ("STRONG", "MODERATE", "WEAK"))

    def test_rank_puts_tradeable_options_first(self):
        base = forced_trade("NIFTY", UPTREND, [], 1, CHAIN, 1, self.cfg, NOW)
        no_strike = ForcedTrade(**{**base.__dict__, "option": None, "score": 99.0})
        self.assertIs(rank([no_strike, base])[0], base)


if __name__ == "__main__":
    unittest.main()
