"""Strategy-engine mathematics checked against hand-computed / textbook values."""
from __future__ import annotations

import math
import unittest
from datetime import timedelta

from strategies import indicators as ind
from strategies.core import (CAPPED_SCORE, LONG, SHORT, Component, RiskConfig, band, build_trade, ramp,
                             round_to_tick, safe_tick, score_of, tick_size)
from strategies.features import Features, Side, five_minute_bars
from strategy_930 import Candle
from tests.strategy_fixtures import ts


class Indicators(unittest.TestCase):
    def test_true_range_excludes_overnight_gap_on_first_bar(self):
        tr = ind.true_ranges([10, 11, 12], [9, 10, 9.5], [9.5, 10.8, 11])
        self.assertEqual(tr, (1, 1.5, 2.5))

    def test_rolling_mean(self):
        self.assertEqual(ind.rolling_mean([1, 2, 3, 4], 3), (None, None, 2.0, 3.0))

    def test_vwap_and_volume_weighted_sd_by_hand(self):
        vw, sd = ind.vwap_series([10, 11], [9, 10], [9.5, 10.8], [100, 300])
        self.assertAlmostEqual(vw[0], 9.5)
        self.assertAlmostEqual(sd[0], 0.0)
        # TP = 9.5 (w 100), 10.6 (w 300): mean 10.325, var = (100*0.825^2 + 300*0.275^2)/400
        self.assertAlmostEqual(vw[1], 10.325)
        self.assertAlmostEqual(sd[1], math.sqrt(0.226875))

    def test_vwap_undefined_without_volume(self):
        vw, sd = ind.vwap_series([10, 11], [9, 10], [9.5, 10.8], [0, 0])
        self.assertEqual(vw, (None, None))
        self.assertEqual(sd, (None, None))

    def test_ema_seed_and_recursion(self):
        self.assertEqual(ind.ema([1, 2, 3, 4, 5], 3), (None, None, 2.0, 3.0, 4.0))

    def test_parkinson(self):
        h = [math.exp(0.01) * 100] * 5
        l = [100.0] * 5
        self.assertAlmostEqual(ind.parkinson_sigma(h, l), 0.01 / math.sqrt(4 * math.log(2)))
        self.assertIsNone(ind.parkinson_sigma([1.0], [2.0]))     # high < low is invalid

    def test_bvc(self):
        self.assertAlmostEqual(ind.bvc_buy_fraction(0.5, 0.5), 0.8413447460685429)
        self.assertAlmostEqual(ind.bvc_buy_fraction(-0.5, 0.5), 1 - 0.8413447460685429)
        self.assertEqual(ind.bvc_buy_fraction(0.0, 0.0), 0.5)
        self.assertEqual(ind.bvc_buy_fraction(1.0, 0.0), 1.0)

    def test_stdev_sample(self):
        self.assertAlmostEqual(ind.stdev([2, 4, 4, 4, 5, 5, 7, 9]), math.sqrt(32 / 7))
        self.assertIsNone(ind.stdev([1.0]))

    def test_efficiency_ratio(self):
        self.assertAlmostEqual(ind.efficiency_ratio([1, 2, 1, 2]), 1 / 3)
        self.assertEqual(ind.efficiency_ratio([5, 5, 5]), 0.0)

    def test_rsi_wilder_textbook_example(self):
        # Wilder RSI worked example (StockCharts data). Gains sum 3.34, losses 1.40 over 14
        # changes -> RSI = 100 - 100/(1 + 3.34/1.40) = 70.464 (StockCharts prints 70.53 because it
        # rounds the average loss to 0.0995).
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03,
                  45.61, 46.28, 46.28]
        self.assertAlmostEqual(ind.rsi_wilder(closes, 14), 100 - 100 / (1 + 3.34 / 1.40), places=9)
        # one Wilder smoothing step: next change +0.29 (46.28 -> 46.57)
        ag, al = (3.34 / 14 * 13 + 0.29) / 14, (1.40 / 14 * 13) / 14
        self.assertAlmostEqual(ind.rsi_wilder(closes + [46.57], 14), 100 - 100 / (1 + ag / al), places=9)
        self.assertEqual(ind.rsi_wilder(list(range(20)), 14), 100.0)
        self.assertEqual(ind.rsi_wilder([5.0] * 20, 14), 50.0)
        self.assertIsNone(ind.rsi_wilder([1, 2, 3], 14))

    def test_ols_slope_t_by_hand(self):
        b, t = ind.ols_slope_t([1, 3, 2, 5])
        self.assertAlmostEqual(b, 1.1)
        self.assertAlmostEqual(t, 1.1 / math.sqrt(0.27))
        b, t = ind.ols_slope_t([1, 2, 3, 4])
        self.assertEqual((b, t), (1.0, 1e9))

    def test_swing_points_never_use_unconfirmed_bars(self):
        h = [1, 2, 5, 2, 1, 3, 1]
        l = [0.5, 1, 4, 1, 0.2, 2, 0.5]
        highs, lows = ind.swing_points(h, l, 1, 6)
        self.assertEqual(highs, [(2, 5), (5, 3)])
        self.assertEqual(lows, [(4, 0.2)])
        highs, _ = ind.swing_points(h, l, 1, 5)       # bar 6 not known yet: pivot at 5 unconfirmed
        self.assertEqual(highs, [(2, 5)])

    def test_bar_shape_ratios(self):
        self.assertAlmostEqual(ind.clv(10, 12, 10, 11.5), 0.5)
        self.assertEqual(ind.clv(10, 10, 10, 10), 0.0)
        self.assertAlmostEqual(ind.body_ratio(10, 12, 9, 11.5), 0.5)
        self.assertAlmostEqual(ind.upper_wick_ratio(10, 12, 9, 11.5), 0.5 / 3)
        self.assertAlmostEqual(ind.lower_wick_ratio(10, 12, 9, 11.5), 1 / 3)


class Score(unittest.TestCase):
    def test_ramp_and_band(self):
        self.assertEqual(ramp(5, 0, 10), 0.5)
        self.assertEqual(ramp(5, 10, 0), 0.5)
        self.assertEqual(ramp(-1, 0, 10), 0.0)
        self.assertEqual(ramp(float("nan"), 0, 1), 0.0)
        self.assertEqual(band(0.3, 0.0, 0.1, 0.5, 1.0), 1.0)
        self.assertAlmostEqual(band(0.75, 0.0, 0.1, 0.5, 1.0), 0.5)
        with self.assertRaises(ValueError):
            band(0.3, 0.1, 0.0, 0.5, 1.0)

    def test_weighted_score(self):
        s, capped = score_of([Component("a", 0.5, 1.0, 0, ""), Component("b", 0.5, 0.8, 0, "")])
        self.assertEqual((s, capped), (90.0, False))

    def test_q90_rule_caps_a_hidden_weak_leg(self):
        s, capped = score_of([Component("a", 0.9, 1.0, 0, ""), Component("b", 0.1, 0.45, 0, "")])
        self.assertEqual((s, capped), (CAPPED_SCORE, True))
        s, capped = score_of([Component("a", 0.9, 0.5, 0, ""), Component("b", 0.1, 0.45, 0, "")])
        self.assertEqual((s, capped), (49.5, False))       # below 90: nothing to cap

    def test_weights_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            score_of([Component("a", 0.9, 1.0, 0, "")])
        with self.assertRaises(ValueError):
            score_of([Component("a", 1.0, 1.2, 0, "")])


class TicksAndTrades(unittest.TestCase):
    def test_nse_tick_bands(self):
        self.assertEqual([tick_size(p) for p in (249.99, 250, 999.99, 1000, 4999, 5000, 10000, 20000)],
                         [0.01, 0.05, 0.05, 0.10, 0.10, 0.50, 1.00, 5.00])
        self.assertEqual(safe_tick(100), 0.01)
        self.assertEqual(safe_tick(240), 0.05)     # within 10% of the 250 boundary
        self.assertEqual(safe_tick(1050), 0.10)

    def test_round_to_tick(self):
        self.assertEqual(round_to_tick(100.123, 0.01, up=True), 100.13)
        self.assertEqual(round_to_tick(100.123, 0.01, up=False), 100.12)
        self.assertEqual(round_to_tick(100.12, 0.01, up=True), 100.12)
        self.assertEqual(round_to_tick(1003.03, 0.10, up=True), 1003.1)

    def test_round_trip_cost_by_hand(self):
        # brokerage 2x20 on 1 lakh = 0.04%; STT 0.025; exchange 2x0.00297; SEBI 2x0.0001;
        # GST 18% of (0.04 + 0.00594 + 0.0002); stamp 0.003; slippage 2 x 2 bps
        fees = 0.04 + 0.025 + 0.00594 + 0.0002 + 0.18 * (0.04 + 0.00594 + 0.0002) + 0.003
        self.assertAlmostEqual(RiskConfig().round_trip_cost_pct(), fees + 0.04)

    def test_build_trade_long_and_short_mirror(self):
        cfg = RiskConfig()
        tr, why = build_trade(LONG, 100.0, 99.30, cfg)
        self.assertEqual(why, "")
        self.assertEqual((tr.entry, tr.stop, tr.target), (100.01, 99.30, 101.43))
        self.assertAlmostEqual(tr.risk_pct, 0.71 / 100.01 * 100, places=3)
        self.assertAlmostEqual(tr.cost_r, cfg.round_trip_cost_pct() / (0.71 / 100.01 * 100), places=3)
        sh, _ = build_trade(SHORT, 100.0, 100.70, cfg)
        self.assertEqual((sh.entry, sh.stop, sh.target), (99.99, 100.70, 98.57))

    def test_build_trade_gates(self):
        cfg = RiskConfig()
        self.assertIn("risk_too_small", build_trade(LONG, 100.0, 99.95, cfg)[1])
        self.assertIn("costs", build_trade(LONG, 100.0, 99.70, cfg)[1])
        self.assertIn("risk_too_large", build_trade(LONG, 100.0, 97.0, cfg)[1])
        self.assertIn("stop_not_beyond_entry", build_trade(LONG, 100.0, 100.5, cfg)[1])
        self.assertIn("reward", build_trade(LONG, 100.0, 99.0, cfg, target=101.0)[1])
        ok, _ = build_trade(LONG, 100.0, 99.0, cfg, target=101.6)
        self.assertEqual(ok.target, 101.6)
        self.assertEqual(build_trade(LONG, float("nan"), 99.0, cfg)[1], "non_finite_trade_inputs")


class FeatureViews(unittest.TestCase):
    def bars(self):
        return tuple(Candle(ts(i), 100 + i, 101 + i, 99 + i, 100.5 + i, 1000 + i) for i in range(12))

    def test_side_mirror_is_exact(self):
        f = Features(self.bars())
        s = Side(f, -1)
        for i in range(f.n):
            self.assertEqual(s.h[i], -f.l[i])
            self.assertEqual(s.l[i], -f.h[i])
            self.assertEqual(s.c[i], -f.c[i])
            self.assertAlmostEqual(s.vwap[i], -f.vwap[i])
        self.assertIs(f.side(-1), f.side(-1))               # cached

    def test_five_minute_bars_only_complete_buckets(self):
        bars = self.bars()                                    # 09:15..09:26
        five = five_minute_bars(bars)
        self.assertEqual(len(five), 2)                        # 09:25 bucket not complete at 09:26
        self.assertEqual(five[0].open, bars[0].open)
        self.assertEqual(five[0].close, bars[4].close)
        self.assertEqual(five[0].high, max(b.high for b in bars[:5]))
        self.assertEqual(five[0].volume, sum(b.volume for b in bars[:5]))
        self.assertEqual(five[1].ts, ts(5))
        self.assertEqual(five_minute_bars(bars[:5])[-1].ts, ts(0))
        self.assertEqual(ts(5) - ts(0), timedelta(minutes=5))


if __name__ == "__main__":
    unittest.main()
