"""Precision gates added after the 2026-10-07 RICOAUTO short (+2.34 ATR stretched from VWAP)."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

import run_engine
from config import StrategyConfig
from psygrid_client import Health, PsygridClient, StockData
from strategy_930 import IST, Candidate, Candle, evaluate_tiers, vwap_alignment_score
from tests.test_run_engine import mirror_short
from tests.test_strategy_930 import late_session_retrace_fixture

NOW = datetime(2026, 10, 7, 10, 0, 20, tzinfo=IST)


def bars(count: int, price: float = 120.0, volume: float = 20_000, end: datetime = NOW) -> tuple[Candle, ...]:
    last = end.replace(second=0, microsecond=0) - timedelta(minutes=1)
    return tuple(
        Candle(last - timedelta(minutes=count - 1 - i), price, price + 0.2, price - 0.2, price, volume)
        for i in range(count)
    )


def stock(symbol: str, candles: tuple[Candle, ...]) -> StockData:
    return StockData(symbol, candles, candles[-1].close, None, Health(symbol, True))


class VwapStretchTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.cfg.validate()

    def test_vwap_score_peaks_near_vwap_and_decays_when_stretched(self):
        score = lambda d: vwap_alignment_score(d, self.cfg)
        self.assertEqual(score(-0.5), 0.0)
        self.assertEqual(score(0.0), 0.0)
        self.assertEqual(score(1.0), 1.0)
        self.assertGreater(score(1.0), score(1.8))
        self.assertEqual(score(2.34), 0.0)  # the RICOAUTO stretch earns nothing

    def test_a_stretched_short_is_not_a_strict_setup(self):
        cs = mirror_short(late_session_retrace_fixture())  # closes ~4.4 ATR below VWAP
        loose = replace(
            self.cfg, max_impulse_atr=5.0, max_retracement_volume_ratio=2.0, min_persistence=0.40,
            max_extension_from_vwap_atr=5.0, fallback_extension_atr_max=5.0,
        )
        self.assertTrue(any(c.tier == 1 and c.side == "SHORT" for c in evaluate_tiers("X", cs, cs[-1].close, None, 0, 0, [], loose)))
        capped = replace(loose, max_extension_from_vwap_atr=2.0, fallback_extension_atr_max=2.0)
        self.assertFalse(any(c.tier in (1, 2) for c in evaluate_tiers("X", cs, cs[-1].close, None, 0, 0, [], capped)))


class PrecisionScreenTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()

    def test_fresh_liquid_stock_passes(self):
        out = run_engine.precision_screen({"OK": stock("OK", bars(30))}, self.cfg, NOW)
        self.assertTrue(out["OK"].health.healthy, out["OK"].health.reason)

    def test_stock_without_recent_candles_is_skipped(self):
        stale = bars(30, end=NOW - timedelta(minutes=6))
        out = run_engine.precision_screen({"OLD": stock("OLD", stale)}, self.cfg, NOW)
        self.assertFalse(out["OLD"].health.healthy)
        self.assertIn("stale_last_candle_6m", out["OLD"].health.reason)

    def test_thin_turnover_stock_is_skipped(self):
        out = run_engine.precision_screen({"THIN": stock("THIN", bars(30, volume=1_000))}, self.cfg, NOW)
        self.assertFalse(out["THIN"].health.healthy)
        self.assertIn("thin_turnover", out["THIN"].health.reason)

    def test_checks_can_be_disabled(self):
        cfg = replace(self.cfg, max_candle_age_minutes=0, min_median_turnover_rupees=0.0)
        stale_thin = bars(30, volume=10, end=NOW - timedelta(minutes=30))
        self.assertTrue(run_engine.precision_screen({"X": stock("X", stale_thin)}, cfg, NOW)["X"].health.healthy)


class FeedAndTriggerTests(unittest.TestCase):
    def test_only_a_live_snapshot_is_tradable(self):
        self.assertTrue(run_engine.feed_is_live({"status": "OK", "session": {"status": "LIVE"}})[0])
        self.assertFalse(run_engine.feed_is_live({"status": "AUTH_ERROR", "session": {"status": "AUTH_ERROR"}})[0])
        self.assertFalse(run_engine.feed_is_live({"status": "OK", "session": {"status": "CLOSED"}})[0])

    def test_short_triggers_below_the_last_candle_low(self):
        c = Candidate("RICOAUTO", "SHORT", 95.0, 120.91, 121.83, 119.07, 0, 1.9, 2.15, 0.53, 0.24,
                      1.4, 1.4, 1.0, 0.85, 0.17, 121.5, 1, ())
        last = Candle(NOW, 120.95, 121.0, 120.80, 120.91, 5000)
        trigger, target = run_engine.entry_trigger(c, last, StrategyConfig())
        self.assertEqual(trigger, 120.80)
        # Same stop, so the target moves with the trigger and keeps 2R.
        self.assertAlmostEqual(target, 120.80 - 2.0 * (121.83 - 120.80))

    def test_long_trigger_keeps_two_r_like_the_ltm_signal(self):
        c = Candidate("LTM", "LONG", 64.9, 3988.0, 3980.1425, 4003.715, 0, 0.36, 3.43, 0.51, 0.25,
                      0.65, 0.5, 0.89, 0.67, 2.45, 3980.5, 2, ())
        last = Candle(NOW, 3986.0, 3989.40, 3985.0, 3988.0, 5000)
        trigger, target = run_engine.entry_trigger(c, last, StrategyConfig())
        self.assertEqual(trigger, 3989.40)
        self.assertAlmostEqual((target - trigger) / (trigger - c.stop), 2.0)


class SignalStatusTests(unittest.TestCase):
    def make(self, tier, score):
        return Candidate("X", "LONG", score, 100, 99, 102, 0, 0, 0, 0.5, 0.5, 0, 0, 0, 0, 1, 99, tier, ())

    def test_status_depends_on_tier_and_score(self):
        cfg = StrategyConfig()
        self.assertEqual(run_engine.signal_status(self.make(1, 80), cfg), "SIGNAL_READY")
        self.assertEqual(run_engine.signal_status(self.make(2, 39.75), cfg), "LOW_CONFIDENCE_WEAK")  # BHARTIARTL 11:38
        self.assertEqual(run_engine.signal_status(self.make(3, 73.3), cfg), "LOW_CONFIDENCE_FORCED")  # MTARTECH 11:38


class NotLiveFeedClient(PsygridClient):
    def market(self):
        self.last_market_errors = ()
        self.last_market_duplicates = ()
        self.last_market_meta = {"endpoint": "public/live.json", "status": "AUTH_ERROR",
                                 "session": {"status": "AUTH_ERROR"}}
        return {"X": {"candles_1m": []}}


class MainRefusesNonLiveDataTests(unittest.TestCase):
    def test_no_signal_is_printed_from_a_non_live_feed(self):
        saved = (run_engine.PsygridClient, run_engine.now_ist)
        tmp = tempfile.TemporaryDirectory()
        cwd = os.getcwd()
        os.chdir(tmp.name)
        try:
            run_engine.PsygridClient = NotLiveFeedClient
            run_engine.now_ist = lambda: NOW
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_engine.main([])
        finally:
            run_engine.PsygridClient, run_engine.now_ist = saved
            os.chdir(cwd)
            tmp.cleanup()
        self.assertEqual(code, 33)
        self.assertIn("FEED NOT LIVE", out.getvalue())
        self.assertNotIn("SIGNAL_READY", out.getvalue())


if __name__ == "__main__":
    unittest.main()
