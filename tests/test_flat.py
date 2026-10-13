"""flat.py: only stocks that opened flat and stayed within ±1% qualify; a tight, squeezing box scores high."""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

import flat
from psygrid_client import Health, StockData
from strategy_930 import IST, Candle

NOON = datetime(2026, 10, 13, 10, 0, tzinfo=IST)
PREV = 100.0


def day(prices: list[tuple[float, float]], volumes: list[float] | None = None) -> tuple[Candle, ...]:
    """1-minute candles from 09:15, each given as (close, half-range)."""
    start = NOON.replace(hour=9, minute=15)
    volumes = volumes or [10_000] * len(prices)
    out, prev = [], prices[0][0]
    for i, ((close, half), vol) in enumerate(zip(prices, volumes)):
        out.append(Candle(start + timedelta(minutes=i), prev, max(prev, close) + half, min(prev, close) - half, close, vol))
        prev = close
    return tuple(out)


def coil(rising_volume: bool = True) -> tuple[Candle, ...]:
    """30 drifting minutes, then a 15-minute box at 100.1-100.4 that squeezes up into its high."""
    early = [(100.0 + 0.05 * ((i % 4) - 1.5), 0.08) for i in range(30)]
    box = [(100.15 + 0.01 * i, 0.10 if i < 10 else 0.02) for i in range(15)]
    vols = [10_000] * 40 + ([25_000] * 5 if rising_volume else [6_000] * 5)
    return day(early + box, vols)


def stock(symbol: str, candles, previous_close: float | None = PREV) -> StockData:
    return StockData(symbol, candles, candles[-1].close, previous_close, Health(symbol, True))


class FlatFilterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = flat.FlatConfig()

    def test_flat_coil_qualifies_with_a_bracket(self):
        picks, dropped = flat.scan({"COIL": stock("COIL", coil())}, {"COIL": {"today_open": 100.0}}, self.cfg)
        self.assertEqual([p.symbol for p in picks], ["COIL"], dropped)
        p = picks[0]
        self.assertEqual(p.bias, "LONG")
        self.assertGreater(p.long_trigger, p.box_high)
        self.assertLess(p.short_trigger, p.box_low)
        self.assertAlmostEqual(p.long_target - p.long_trigger, 2 * (p.long_trigger - p.long_stop))
        self.assertLess(p.contraction, 1.0)

    def test_gap_of_one_percent_is_rejected(self):
        d = stock("GAP", coil())
        self.assertEqual(flat.flat_reject(d, 101.0, self.cfg), "gapped")
        self.assertEqual(flat.flat_reject(d, 98.9, self.cfg), "gapped")

    def test_stock_that_touched_one_percent_and_came_back_is_rejected(self):
        cs = list(coil())
        cs[5] = Candle(cs[5].ts, cs[5].open, 101.05, cs[5].low, cs[5].close, cs[5].volume)
        self.assertEqual(flat.flat_reject(stock("SPIKE", tuple(cs)), 100.0, self.cfg), "moved_up")
        cs[5] = Candle(cs[5].ts, cs[5].open, 100.2, 98.95, cs[5].close, cs[5].volume)
        self.assertEqual(flat.flat_reject(stock("DIP", tuple(cs)), 100.0, self.cfg), "moved_down")

    def test_max_move_is_configurable(self):
        tight = flat.FlatConfig(max_move_pct=0.3)
        self.assertEqual(flat.flat_reject(stock("COIL", coil()), 100.0, tight), "moved_up")

    def test_previous_close_is_required(self):
        self.assertEqual(flat.flat_reject(stock("X", coil(), None), 100.0, self.cfg), "no_previous_close")

    def test_open_falls_back_only_to_the_0915_candle(self):
        d = stock("X", coil())
        self.assertEqual(flat.opening_price(d, {}), d.candles[0].open)
        late = stock("Y", coil()[3:])
        self.assertIsNone(flat.opening_price(late, {}))
        self.assertEqual(flat.flat_reject(late, None, self.cfg), "no_today_open")

    def test_dead_box_is_too_tight_to_pay_costs(self):
        cs = day([(100.0, 0.01)] * 30)
        pick, reason = flat.evaluate(stock("DEAD", cs), 100.0, self.cfg)
        self.assertIsNone(pick)
        self.assertEqual(reason, "box_too_tight")

    def test_rising_volume_scores_higher(self):
        hot, _ = flat.evaluate(stock("A", coil(True)), 100.0, self.cfg)
        cold, _ = flat.evaluate(stock("B", coil(False)), 100.0, self.cfg)
        self.assertGreater(hot.score, cold.score)

    def test_mirror_coil_leans_short(self):
        mirror = tuple(Candle(c.ts, 200 - c.open, 200 - c.low, 200 - c.high, 200 - c.close, c.volume) for c in coil())
        pick, _ = flat.evaluate(stock("M", mirror), 100.0, self.cfg)
        self.assertEqual(pick.bias, "SHORT")

    def test_unhealthy_stocks_are_ignored(self):
        d = StockData("BAD", coil(), 100.0, PREV, Health("BAD", False, "thin_turnover"))
        picks, dropped = flat.scan({"BAD": d}, {}, self.cfg)
        self.assertEqual((picks, dropped), ([], {}))

    def test_printing_a_pick_does_not_crash(self):
        p, _ = flat.evaluate(stock("COIL", coil()), 100.0, self.cfg)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            flat.print_pick(p, self.cfg, NOON, 500.0, coil())
            flat.print_table([p], self.cfg)
        self.assertIn("BUY >", out.getvalue())
        self.assertIn("stop-entry ABOVE", out.getvalue())


class WatchModeTests(unittest.TestCase):
    """The minute loop: beep once per new setup, not again while its bracket is alive."""

    def setUp(self):
        self.cfg = flat.FlatConfig()
        self.pick, _ = flat.evaluate(stock("COIL", coil()), 100.0, self.cfg)

    def run_watch(self, start: datetime, scans, until: str = "10:10"):
        clock = [start]
        beeps, calls = [], []

        def fetch(client, engine_cfg, cfg):
            item = scans[min(len(calls), len(scans) - 1)]
            calls.append(clock[0])
            if isinstance(item, Exception):
                raise item
            return flat.ScanResult(clock[0], {"COIL": stock("COIL", coil())}, item, {}, 1, {})

        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

        args = type("Args", (), {"until": until, "scan_offset": 8, "risk_rupees": None})()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = flat.watch(None, None, self.cfg, run_audit(), args,
                              clock=lambda: clock[0], sleep=sleep, beep=lambda: beeps.append(clock[0]), fetch=fetch)
        return code, beeps, calls, out.getvalue()

    def test_beeps_once_per_new_setup_and_scans_every_minute(self):
        start = NOON.replace(minute=0, second=8)
        code, beeps, calls, out = self.run_watch(start, [[], [self.pick], [self.pick], [self.pick]])
        self.assertEqual(code, 0)
        self.assertEqual(len(beeps), 1)
        self.assertEqual(beeps[0], start + timedelta(minutes=1))
        self.assertEqual(len(calls), 10)  # 10:00 .. 10:09
        self.assertTrue(all(c.second == 8 for c in calls))
        self.assertIn("NEW FLAT SETUP", out)
        self.assertIn("already alerted", out)

    def test_realerts_after_the_bracket_expires(self):
        start = NOON.replace(minute=0, second=8)
        _, beeps, _, _ = self.run_watch(start, [[self.pick]], until="10:20")
        self.assertEqual([b - start for b in beeps], [timedelta(0), timedelta(minutes=15)])

    def test_watch_picks_below_setup_score_never_beep(self):
        weak = replace(self.pick, score=self.cfg.min_score - 1)
        _, beeps, _, _ = self.run_watch(NOON.replace(second=8), [[weak]])
        self.assertEqual(beeps, [])

    def test_feed_error_is_retried_not_fatal(self):
        _, beeps, calls, out = self.run_watch(NOON.replace(second=8), [flat.FeedError(30, "down"), [self.pick]])
        self.assertGreater(len(calls), 2)
        self.assertEqual(len(beeps), 1)
        self.assertIn("retrying next minute", out)

    def test_waits_for_the_box_before_the_first_scan(self):
        _, _, calls, out = self.run_watch(NOON.replace(hour=9, minute=20, second=0), [[]], until="09:33")
        self.assertEqual(calls[0], NOON.replace(hour=9, minute=30, second=8))
        self.assertIn("waiting for 15 candles", out)

    def test_next_scan_is_offset_past_the_next_minute(self):
        self.assertEqual(flat.seconds_to_next_scan(NOON.replace(second=50), 8), 18)
        self.assertEqual(flat.seconds_to_next_scan(NOON.replace(second=8, microsecond=500_000), 8), 59.5)


def run_audit():
    import tempfile
    return flat.Audit(os.path.join(tempfile.mkdtemp(), "flat_audit.jsonl"))


if __name__ == "__main__":
    unittest.main()
