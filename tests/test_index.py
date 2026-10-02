import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from index import (
    INDEXES,
    IndexConfig,
    IndexSignal,
    Level,
    Zone,
    breakout_retest,
    build_zones,
    completed_candles,
    completed_five_minute,
    find_signals,
    key_zones,
    max_pain,
    missing_minutes,
    oi_levels,
    pick_option,
    previous_day,
    rejection,
    update_store,
)
from strategy_930 import IST, Candle

T0 = datetime(2026, 10, 1, 9, 15, tzinfo=IST)


def bars5(rows):
    """5-minute candles from (o, h, l, c) tuples starting 09:15."""
    return [Candle(T0 + timedelta(minutes=5 * i), o, h, l, c, 0.0) for i, (o, h, l, c) in enumerate(rows)]


def ones(rows, start=T0):
    return [Candle(start + timedelta(minutes=i), o, h, l, c, 1000.0) for i, (o, h, l, c) in enumerate(rows)]


RESISTANCE = Zone(22600.0, 22610.0, 6.0, ("PDH", "CALL WALL"))
SUPPORT = Zone(22400.0, 22410.0, 6.0, ("PDL", "PUT WALL"))
TARGET_UP = Zone(22800.0, 22800.0, 5.0, ("ROUND", "CALL WALL"))
KEYS = [SUPPORT, RESISTANCE, TARGET_UP]


class LevelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = IndexConfig()

    def test_levels_within_tolerance_merge_and_add_strength(self):
        levels = [Level(22600, "ROUND", 1.0), Level(22610, "HOD", 2.0), Level(22605, "CALL WALL", 3.0),
                  Level(22800, "ROUND", 1.0)]
        zones = build_zones(levels, 22550, self.cfg)
        self.assertEqual(len(zones), 2)
        self.assertEqual((zones[0].low, zones[0].high, zones[0].strength), (22600, 22610, 6.0))
        self.assertEqual([z.low for z in key_zones(zones, self.cfg)], [22600])

    def test_oi_walls_from_real_chain(self):
        chain = json.loads(Path(__file__).with_name("fixtures").joinpath("nifty_chain_small.json").read_text())
        walls = oi_levels(chain, 22620.0, self.cfg)
        calls = [lv for lv in walls if lv.label.startswith("CALL")]
        puts = [lv for lv in walls if lv.label.startswith("PUT")]
        self.assertEqual(calls[0].price, 23000.0)
        self.assertEqual(puts[0].price, 22000.0)
        self.assertTrue(all(lv.price >= 22620 for lv in calls))
        self.assertTrue(all(lv.price <= 22620 for lv in puts))

    def test_biggest_wall_is_key_on_its_own(self):
        chain = json.loads(Path(__file__).with_name("fixtures").joinpath("nifty_chain_small.json").read_text())
        walls = [lv for lv in oi_levels(chain, 22620.0, self.cfg) if lv.label.startswith(("CALL WALL", "PUT WALL"))]
        self.assertEqual(max(lv.weight for lv in walls), 4.0)
        self.assertGreaterEqual(4.0, self.cfg.key_zone_strength)

    def test_max_pain_matches_server(self):
        chain = json.loads(Path(__file__).with_name("fixtures").joinpath("nifty_chain_small.json").read_text())
        self.assertEqual(max_pain(chain), 22700.0)      # PsyGrid's own analytics say 22700 too

    def test_chain_extras(self):
        chain = json.loads(Path(__file__).with_name("fixtures").joinpath("nifty_chain_small.json").read_text())
        labels = [lv.label for lv in oi_levels(chain, 22620.0, self.cfg)]
        self.assertTrue(any(l.startswith("PIVOT") for l in labels))
        self.assertIn("MAX PAIN", labels)
        self.assertTrue(any(l.startswith("EXPIRY RANGE HIGH") for l in labels))
        self.assertTrue(any(l.startswith("EXPIRY RANGE LOW") for l in labels))

    def test_fresh_writing_level(self):
        mk = lambda k, ce, ce_prev, pe, pe_prev: {"strike": k, "ce": {"oi": ce, "previous_oi": ce_prev},
                                                  "pe": {"oi": pe, "previous_oi": pe_prev}}
        chain = {"strikes": [mk(100.0, 10, 10, 10, 10), mk(101.0, 100, 100, 1, 1), mk(102.0, 40, 5, 1, 1)]}
        labels = [lv.label for lv in oi_levels(chain, 100.0, self.cfg)]
        self.assertIn("FRESH CALL WRITING +35.0M"[:18], " ".join(labels))

    def test_previous_day_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "levels.json"
            store = {}
            update_store(store, "NIFTY", "2026-09-30", ones([(100, 110, 90, 105)]), path)
            update_store(store, "NIFTY", "2026-10-01", ones([(105, 106, 104, 105)]), path)
            prev = previous_day(json.loads(path.read_text()), "NIFTY", "2026-10-01")
            self.assertEqual((prev["high"], prev["low"], prev["close"]), (110, 90, 105))


class DataTests(unittest.TestCase):
    def test_forming_candle_is_dropped_and_gaps_found(self):
        rows = [{"timestamp": "2026-10-01 09:15:00 IST", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                {"timestamp": "2026-10-01 09:18:00 IST", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                {"timestamp": "2026-10-01 09:19:00 IST", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}]
        cs = completed_candles({"1m": rows}, datetime(2026, 10, 1, 9, 19, 30, tzinfo=IST))
        self.assertEqual(len(cs), 2)
        self.assertEqual(missing_minutes(cs), ["09:16", "09:17"])

    def test_incomplete_last_five_minute_bar_is_dropped(self):
        cs = ones([(1, 1, 1, 1)] * 7)              # 09:15-09:21: one full 5m bar + a partial one
        self.assertEqual(len(completed_five_minute(cs)), 1)

    def test_index_specs(self):
        self.assertEqual(INDEXES["NIFTY"].options_path, "public/nifty-options.json")


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.cfg = IndexConfig()

    def test_breakout_retest_call(self):
        bars = bars5([
            (22560, 22590, 22550, 22585), (22585, 22598, 22575, 22595),     # below the zone
            (22595, 22640, 22590, 22635),                                   # 5m close through 22610
            (22635, 22640, 22606, 22612),                                   # pullback tests the zone
            (22612, 22650, 22610, 22645),                                   # holds, bullish close away
        ])
        sigs = breakout_retest("NIFTY", bars, KEYS, 20.0, self.cfg)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual((s.side, s.setup), ("CALL", "BREAKOUT-RETEST"))
        self.assertAlmostEqual(s.stop, 22600 - 0.25 * 20.0)
        self.assertEqual(s.target1, 22800.0)
        self.assertGreaterEqual(s.t1_r, self.cfg.min_target_r)

    def test_failed_breakout_back_inside_is_not_a_signal(self):
        bars = bars5([
            (22585, 22598, 22575, 22595), (22595, 22640, 22590, 22635),
            (22635, 22640, 22560, 22570),                                   # falls back through the zone
            (22570, 22625, 22565, 22620),
        ])
        self.assertEqual(breakout_retest("NIFTY", bars, KEYS, 20.0, self.cfg), [])

    def test_rejection_put_at_resistance(self):
        bars = bars5([(22560, 22580, 22550, 22575), (22575, 22612, 22570, 22578)])  # wick into zone, close below
        keys = [Zone(22300, 22300, 5.0, ("PDL", "PUT WALL")), RESISTANCE]
        sigs = rejection("NIFTY", bars, keys, 20.0, self.cfg)
        self.assertEqual([(s.side, s.setup) for s in sigs], [("PUT", "REJECTION")])
        self.assertEqual(sigs[0].target1, 22300)
        self.assertAlmostEqual(sigs[0].stop, 22612 + 0.25 * 20.0)

    def test_rejection_needs_room_to_next_level(self):
        bars = bars5([(22560, 22580, 22550, 22575), (22575, 22612, 22570, 22578)])
        near = [Zone(22560, 22560, 5.0, ("ROUND", "PUT WALL")), RESISTANCE]      # T1 only ~0.9R away
        self.assertEqual(rejection("NIFTY", bars, near, 20.0, self.cfg), [])

    def test_small_wick_is_not_a_rejection(self):
        bars = bars5([(22560, 22580, 22550, 22575), (22600, 22602, 22570, 22572)])
        self.assertEqual(rejection("NIFTY", bars, [SUPPORT, RESISTANCE], 20.0, self.cfg), [])

    def test_market_bias_blocks_counter_trend_signal(self):
        # A clean resistance rejection PUT built from 1m candles, then an UP market blocks it.
        ones_rows = [(22560, 22562, 22558, 22561)] * 5 + [(22575, 22612, 22570, 22578)] * 1 + \
                    [(22578, 22580, 22574, 22578)] * 4
        cs = ones(ones_rows)
        keys = [Zone(22300, 22300, 5.0, ("PDL", "PUT WALL")), RESISTANCE]
        self.assertTrue(find_signals("NIFTY", cs, keys, 0, self.cfg))
        self.assertEqual(find_signals("NIFTY", cs, keys, 1, self.cfg), [])


class OptionTests(unittest.TestCase):
    def test_pick_option_prefers_delta_near_0_6_with_tight_spread(self):
        chain = json.loads(Path(__file__).with_name("fixtures").joinpath("nifty_chain_small.json").read_text())
        sig = IndexSignal("NIFTY", "REJECTION", "CALL", "z", "10:00", 22620.0, 22590.0, 22700.0, None, 30.0, 2.67, ())
        pick = pick_option(chain, sig, IndexConfig())
        self.assertIsNotNone(pick)
        self.assertEqual(pick.kind, "CE")
        self.assertTrue(0.45 <= pick.delta <= 0.75)
        self.assertLess(pick.premium_stop, pick.ask)
        self.assertGreaterEqual(pick.premium_stop, pick.ask * 0.7)
        self.assertGreater(pick.premium_t1, pick.ask)


if __name__ == "__main__":
    unittest.main()
