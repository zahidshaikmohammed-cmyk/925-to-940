import unittest
from datetime import datetime, time as dtime, timedelta

from old import (
    Context,
    _forced,
    OldConfig,
    evaluate_stock,
    rank_signals,
    scan,
    structural_leg,
    time_window,
)
from psygrid_client import Health, StockData
from strategy_930 import IST, Candle


def make(rows, start=(9, 15)):
    t0 = datetime(2026, 9, 30, start[0], start[1], tzinfo=IST)
    return [Candle(t0 + timedelta(minutes=i), o, h, l, c, v) for i, (o, h, l, c, v) in enumerate(rows)]


# 09:15 opening drive 100 -> 102.1, light-volume pullback to 101.0 (50%),
# bullish reclaim to 101.6 with room left to the high.
CLEAN_LONG = [
    (100.0, 100.35, 99.9, 100.3, 3000), (100.3, 100.65, 100.2, 100.6, 2800),
    (100.6, 100.95, 100.5, 100.9, 2700), (100.9, 101.25, 100.8, 101.2, 2600),
    (101.2, 101.55, 101.1, 101.5, 2500), (101.5, 101.85, 101.4, 101.8, 2400),
    (101.8, 102.1, 101.7, 102.0, 2300), (102.0, 102.05, 101.75, 101.8, 900),
    (101.8, 101.85, 101.5, 101.55, 900), (101.55, 101.6, 101.25, 101.3, 800),
    (101.3, 101.35, 101.0, 101.1, 800), (101.1, 101.3, 101.02, 101.25, 900),
    (101.25, 101.45, 101.2, 101.4, 1000), (101.4, 101.62, 101.35, 101.6, 1200),
]

CTX = Context(market_return=0.1, sector_return=None, breadth=0.55)


def mirror(rows, pivot=200.0):
    """Reflect a LONG fixture into the equivalent SHORT fixture."""
    return [(pivot - o, pivot - l, pivot - h, pivot - c, v) for o, h, l, c, v in rows]


class OldEngineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = OldConfig()

    def test_clean_opening_drive_is_a_plus_with_structural_targets(self):
        sigs = evaluate_stock("CLEAN", make(CLEAN_LONG), None, CTX, self.cfg)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual((s.tier, s.side), (1, "LONG"))
        self.assertAlmostEqual(s.tp1, 102.1)                 # retest of the high
        self.assertAlmostEqual(s.tp2, 101.0 + (102.1 - 99.9))  # AB=CD measured move
        self.assertLess(s.stop, 101.0)                       # below the pullback low
        self.assertGreaterEqual(s.tp1_r, self.cfg.min_tp1_r)

    def test_short_mirror_is_a_plus(self):
        sigs = evaluate_stock("MIRROR", make(mirror(CLEAN_LONG)), None, Context(-0.1, None, 0.45), self.cfg)
        self.assertEqual((sigs[0].tier, sigs[0].side), (1, "SHORT"))

    def test_entry_glued_to_extreme_is_not_a_setup(self):
        rows = CLEAN_LONG[:-1] + [(101.4, 102.05, 101.35, 102.0, 1200)]  # reclaim ~0.9
        sigs = evaluate_stock("GLUED", make(rows), None, CTX, self.cfg)
        self.assertTrue(all(s.tier == 3 for s in sigs))

    def test_price_past_the_extreme_is_not_a_setup(self):
        # JUBLPHARMA 30-Sep pattern: drop, bounce, then a fresh low far below VWAP.
        rows = [(1045.0 - i * 0.8, 1045.3 - i * 0.8, 1044.0 - i * 0.8, 1044.2 - i * 0.8, 2000) for i in range(10)]
        rows += [(1037.2, 1038.5, 1037.0, 1038.3, 800), (1038.3, 1040.0, 1038.2, 1039.6, 700),
                 (1039.6, 1039.7, 1037.5, 1037.8, 1500), (1037.8, 1037.9, 1035.3, 1035.4, 2500)]
        sigs = evaluate_stock("JUBL", make(rows), None, CTX, self.cfg)
        self.assertTrue(sigs)
        self.assertTrue(all(s.tier == 3 for s in sigs))

    def test_large_gap_blocks_setup(self):
        sigs = evaluate_stock("GAP", make(CLEAN_LONG), 95.0, CTX, self.cfg)  # +5.3% gap
        self.assertTrue(all(s.tier == 3 for s in sigs))

    def test_forced_prefers_pullback_near_vwap_over_stretched_chase(self):
        chop = [(100.0 + (0.05 if i % 2 else -0.05), 100.2, 99.8, 100.0 + (0.05 if i % 2 else -0.05), 1000) for i in range(20)]
        # A: steady uptrend that pulled back near VWAP.  B: vertical spike, closing at the high.
        a = [(100 + i * 0.1, 100.15 + i * 0.1, 99.95 + i * 0.1, 100.1 + i * 0.1, 1000) for i in range(14)]
        a += [(101.4, 101.45, 101.0, 101.05, 600), (101.05, 101.1, 100.9, 101.0, 600),
              (101.0, 101.15, 100.95, 101.1, 700), (101.1, 101.2, 101.05, 101.15, 700)]
        b = [(100.0, 100.1, 99.95, 100.0, 1000)] * 14
        b += [(100.0, 101.0, 100.0, 101.0, 5000), (101.0, 102.0, 101.0, 102.0, 5000),
              (102.0, 103.0, 102.0, 103.0, 5000), (103.0, 104.0, 103.0, 104.0, 5000)]
        data = {
            name: StockData(name, tuple(make(rows)), rows[-1][3], None, Health(name, True))
            for name, rows in (("A", a), ("B", b), ("C", chop), ("D", chop), ("E", chop))
        }
        signals, _ = scan(data, {}, self.cfg)
        best = rank_signals(signals)[0]
        self.assertEqual((best.symbol, best.side), ("A", "LONG"))
        spike = [s for s in signals if s.symbol == "B" and s.side == "LONG"][0]
        self.assertEqual(spike.tier, 3)
        forced_a = _forced("A", make(a), 1, CTX, self.cfg)
        forced_b = _forced("B", make(b), 1, CTX, self.cfg)
        self.assertLess(forced_b.score, forced_a.score)

    def test_forced_penalises_chasing_near_the_high(self):
        # HCL 30-Sep: steady rally 1225 -> 1247, tiny dip, long offered at 1245.
        rally, p = [], 1225.0
        for _ in range(20):
            rally.append((p, p + 1.7, p - 0.5, p + 1.1, 20000))
            p += 1.1
        chase = rally + [(1247, 1247.3, 1244.5, 1245, 9000), (1245, 1245.5, 1243, 1243.5, 8000),
                         (1243.5, 1244.2, 1243, 1244, 8000), (1244, 1245.4, 1243.8, 1245, 9000)]
        pullback = rally + [(1247, 1247.2, 1244, 1244.5, 9000), (1244.5, 1245, 1241, 1241.5, 8000),
                            (1241.5, 1242, 1238.5, 1239, 8000), (1239, 1239.5, 1236.5, 1237, 7000),
                            (1237, 1238.5, 1236.2, 1238, 6000), (1238, 1240, 1237.5, 1239.8, 7000),
                            (1239.8, 1241.3, 1239.5, 1241, 8000)]
        self.assertTrue(all(s.tier == 3 for s in evaluate_stock("HCL", make(chase), None, CTX, self.cfg)))
        chase_score = _forced("HCL", make(chase), 1, CTX, self.cfg).score
        pullback_score = _forced("HCL", make(pullback), 1, CTX, self.cfg).score
        self.assertLess(chase_score, pullback_score - 15.0)

    def test_market_bias(self):
        self.assertEqual(Context(0.3, None, 0.70).bias(self.cfg), 1)
        self.assertEqual(Context(-0.3, None, 0.30).bias(self.cfg), -1)
        self.assertEqual(Context(0.3, None, 0.50).bias(self.cfg), 0)
        self.assertEqual(Context(-0.1, None, 0.65).bias(self.cfg), 0)   # breadth and median disagree

    def test_no_short_setup_in_an_up_market(self):
        short_rows = make(mirror(CLEAN_LONG))
        up = Context(0.4, None, 0.70)
        self.assertTrue(all(s.tier == 3 for s in evaluate_stock("M", short_rows, None, up, self.cfg)))
        mixed = Context(0.1, None, 0.50)
        self.assertEqual(evaluate_stock("M", short_rows, None, mixed, self.cfg)[0].tier, 1)

    def test_forced_counter_market_pick_is_penalised(self):
        short_rows = make(mirror(CLEAN_LONG))
        up = _forced("M", short_rows, -1, Context(0.4, None, 0.70), self.cfg)
        mixed = _forced("M", short_rows, -1, Context(0.1, None, 0.50), self.cfg)
        self.assertAlmostEqual(mixed.score - up.score, self.cfg.counter_market_penalty, delta=8.0)

    def test_up_market_universe_never_picks_a_short(self):
        trend = [(100 + i * 0.1, 100.15 + i * 0.1, 99.95 + i * 0.1, 100.1 + i * 0.1, 1000) for i in range(16)]
        data = {f"U{i}": StockData(f"U{i}", tuple(make(trend)), trend[-1][3], None, Health(f"U{i}", True))
                for i in range(8)}
        short_rows = mirror(CLEAN_LONG)
        data["SHORTY"] = StockData("SHORTY", tuple(make(short_rows)), short_rows[-1][3], None, Health("SHORTY", True))
        signals, stats = scan(data, {}, self.cfg)
        self.assertEqual(Context(stats["market_return"], None, stats["breadth"]).bias(self.cfg), 1)
        self.assertEqual(rank_signals(signals)[0].side, "LONG")

    def test_forced_always_returns_a_pick_for_healthy_data(self):
        chop = [(100.0, 100.2, 99.8, 100.0 + (0.1 if i % 2 else -0.1), 1000) for i in range(30)]
        data = {"C": StockData("C", tuple(make(chop)), chop[-1][3], None, Health("C", True))}
        signals, _ = scan(data, {}, self.cfg)
        self.assertTrue(rank_signals(signals))

    def test_structural_leg_uses_session_extreme(self):
        leg = structural_leg(make(CLEAN_LONG), 1, self.cfg)
        self.assertEqual((leg.start, leg.extreme, leg.pullback), (99.9, 102.1, 101.0))
        self.assertLessEqual(leg.reclaim, 1.0)

    def test_time_windows(self):
        self.assertEqual(time_window(dtime(9, 20))[0], "TOO EARLY")
        self.assertEqual(time_window(dtime(9, 32))[0], "PRIME")
        self.assertEqual(time_window(dtime(10, 0))[0], "GOOD")
        self.assertEqual(time_window(dtime(12, 30))[0], "MIDDAY CHOP")
        self.assertEqual(time_window(dtime(15, 5)), ("NO NEW ENTRIES", 0, time_window(dtime(15, 5))[2]))
        self.assertEqual(time_window(dtime(15, 45))[0], "CLOSED")


if __name__ == "__main__":
    unittest.main()
