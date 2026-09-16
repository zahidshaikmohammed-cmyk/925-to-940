from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import StrategyConfig
from psygrid_client import PsygridClient
from strategy_930 import Candle, atr, efficiency, evaluate, evaluate_tiers, session_candles, vwap

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


def late_session_retrace_fixture():
    start = datetime(2026, 9, 15, 9, 15, tzinfo=IST)
    rows = []
    price = 100.0
    for i in range(20):
        rows.append((price, price + 0.15, price - 0.10, price + 0.05, 1000))
        price += 0.05
    for i in range(5):
        o = price
        c = price + 0.8
        rows.append((o, c + 0.2, o - 0.1, c, 1800))
        price = c
    for i in range(5):
        o = price
        c = price - 0.5
        rows.append((o, o + 0.1, c - 0.1, c, 500))
        price = c
    for i in range(5):
        o = price
        c = price + 0.45
        rows.append((o, c + 0.1, o - 0.1, c, 900))
        price = c
    return tuple(
        Candle(start + timedelta(minutes=i), o, h, l, c, v)
        for i, (o, h, l, c, v) in enumerate(rows)
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

    def test_preopen_candle_never_becomes_session_candle(self):
        pre = Candle(datetime(2026, 9, 15, 9, 9, tzinfo=IST), 50, 50, 50, 50, 1)
        cs = session_candles((pre,) + valid_long_fixture())
        self.assertEqual(cs[0].ts.time().strftime("%H:%M"), "09:15")
        self.assertEqual(cs[0].open, 100.0)

    def test_strict_impulse_retracement_candidate_exists(self):
        cs = valid_long_fixture()
        candidate = evaluate("TEST", cs, 104.2, 101.0, 1.0, 1.0, [0.0] * 20, self.cfg, 1)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.side, "LONG")
        self.assertEqual(candidate.tier, 1)
        self.assertGreaterEqual(candidate.retracement_depth, self.cfg.min_retracement_depth)
        self.assertLessEqual(candidate.retracement_depth, self.cfg.max_retracement_depth)
        self.assertGreaterEqual(candidate.target - candidate.entry, 2 * abs(candidate.entry - candidate.stop))

    def test_missing_previous_close_does_not_change_signal(self):
        cs = valid_long_fixture()
        with_close = evaluate("TEST", cs, 104.2, 101.0, 1.0, 1.0, [0.0] * 20, self.cfg, 1)
        without_close = evaluate("TEST", cs, 104.2, None, 1.0, 1.0, [], self.cfg, 1)
        self.assertIsNotNone(with_close)
        self.assertIsNotNone(without_close)
        self.assertEqual(with_close.side, without_close.side)
        self.assertAlmostEqual(with_close.score, without_close.score, places=9)
        self.assertAlmostEqual(with_close.entry, without_close.entry, places=9)
        self.assertAlmostEqual(with_close.stop, without_close.stop, places=9)
        self.assertAlmostEqual(with_close.target, without_close.target, places=9)

    def test_psygrid_canonical_1m_ohlcv_is_healthy_without_ltp_fields(self):
        rows = []
        start = datetime(2026, 9, 16, 9, 15, tzinfo=IST)
        for i in range(6):
            price = 100.0 + i
            rows.append({
                "timestamp": (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S IST"),
                "open": price,
                "high": price + 0.5,
                "low": price - 0.2,
                "close": price + 0.3,
                "volume": 1000,
            })
        payload = {
            "symbol": "TEST",
            "security_id": "1",
            "candles_1m": rows,
        }
        d = PsygridClient("http://example.invalid").stock(
            "TEST", payload, datetime(2026, 9, 16, 9, 20, 5, tzinfo=IST)
        )
        self.assertTrue(d.health.healthy, d.health.reason)
        self.assertIsNone(d.previous_close)
        self.assertEqual(d.ltp, 105.3)
        self.assertEqual(len(d.candles), 5)
        self.assertNotIn("stale", d.health.reason)

    def test_old_ltp_timestamp_is_ignored_when_endpoint_has_1m_ohlcv(self):
        start = datetime(2026, 9, 16, 9, 15, tzinfo=IST)
        rows = [
            {
                "timestamp": (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S IST"),
                "open": 100 + i,
                "high": 101 + i,
                "low": 99 + i,
                "close": 100.5 + i,
                "volume": 1000,
            }
            for i in range(6)
        ]
        payload = {
            "ltp": 999.0,
            "ltp_timestamp": "2020-01-01 09:15:00 IST",
            "candles_1m": rows,
        }
        d = PsygridClient("http://example.invalid").stock(
            "TEST", payload, datetime(2026, 9, 16, 9, 20, 5, tzinfo=IST)
        )
        self.assertTrue(d.health.healthy, d.health.reason)
        self.assertEqual(d.ltp, 105.5)

    def test_tier3_forces_signal_even_when_gap_breaks_pattern_gates(self):
        cs = valid_long_fixture()
        candidate = evaluate("GAPTEST", cs, 104.2, 95.0, 1.0, 1.0, [0.0] * 20, self.cfg, 3)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.tier, 3)
        self.assertIn("FORCED_ENTRY_TIER_3", candidate.reasons)
        self.assertGreater(abs(candidate.entry - candidate.stop), 0)

    def test_tier3_no_retracement_is_not_allowed_high_score(self):
        start = datetime(2026, 9, 15, 9, 15, tzinfo=IST)
        cs = tuple(
            Candle(start + timedelta(minutes=i), 100 + i * 0.25, 100.3 + i * 0.25,
                   99.9 + i * 0.25, 100.25 + i * 0.25, 1000)
            for i in range(40)
        )
        candidate = evaluate("TREND", cs, cs[-1].close, None, 0.0, 0.0, [], self.cfg, 3)
        self.assertIsNotNone(candidate)
        self.assertIn("setup=NO_QUALIFYING_RETRACEMENT", candidate.reasons)
        self.assertLessEqual(candidate.score, 58.0)
        self.assertEqual(candidate.retracement_depth, 0.0)

    def test_late_session_impulse_is_not_killed_by_old_bar_gate(self):
        cs = late_session_retrace_fixture()
        gate_only_cfg = replace(
            self.cfg,
            max_impulse_atr=5.0,
            max_retracement_volume_ratio=2.0,
            min_persistence=0.40,
        )
        candidates = evaluate_tiers("LATE", cs, cs[-1].close, None, 0.0, 0.0, [], gate_only_cfg)
        self.assertTrue(candidates)
        self.assertTrue(any(c.tier == 1 for c in candidates), [c.reasons for c in candidates])
        strict = next(c for c in candidates if c.tier == 1)
        self.assertGreaterEqual(strict.retracement_depth, self.cfg.min_retracement_depth)
        self.assertLessEqual(strict.retracement_depth, self.cfg.max_retracement_depth)
        self.assertGreater(strict.reasons.index("setup=IMPULSE_RETRACEMENT"), -1)

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
