"""flat.py: flat stocks form a watchlist; only a volume breakout out of the box is a signal."""

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


def breakout(up: bool = True, volume: float = 50_000, close: float | None = None) -> tuple[Candle, ...]:
    """coil() plus one 10:00 candle that closes through the box (99.88-100.34) on volume."""
    cs = coil()
    t = cs[-1].ts + timedelta(minutes=1)
    if up:
        c = close if close is not None else 100.60
        return cs + (Candle(t, 100.30, c + 0.02, 100.28, c, volume),)
    c = close if close is not None else 99.60
    return cs + (Candle(t, 99.92, 99.94, c - 0.02, c, volume),)


def stock(symbol: str, candles, previous_close: float | None = PREV) -> StockData:
    return StockData(symbol, candles, candles[-1].close, previous_close, Health(symbol, True))


class FlatFilterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = flat.FlatConfig()

    def test_flat_coil_joins_the_watchlist(self):
        picks, dropped = flat.scan({"COIL": stock("COIL", coil())}, {"COIL": {"today_open": 100.0}}, self.cfg)
        self.assertEqual([p.symbol for p in picks], ["COIL"], dropped)
        self.assertEqual(picks[0].bias, "LONG")
        self.assertLess(picks[0].contraction, 1.0)

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



class BreakoutTests(unittest.TestCase):
    """The only signal: a flat stock's newest candle closes through its box on volume."""

    def setUp(self):
        self.cfg = flat.FlatConfig()

    def detect(self, candles, today_open=100.0):
        return flat.detect_breakout(stock("X", candles), today_open, self.cfg)

    def test_sideways_coil_is_not_a_signal(self):
        self.assertIsNone(self.detect(coil()))  # the 2026-10-13 complaint: no move, no alert

    def test_volume_breakout_up(self):
        b = self.detect(breakout())
        self.assertEqual(b.side, "LONG")
        self.assertAlmostEqual(b.entry, 100.67)
        self.assertAlmostEqual(b.stop, (b.box_high + b.box_low) / 2)
        self.assertAlmostEqual(b.target - b.entry, 2 * (b.entry - b.stop))
        self.assertAlmostEqual(b.volume_ratio, 5.0)

    def test_volume_breakout_down(self):
        b = self.detect(breakout(up=False))
        self.assertEqual(b.side, "SHORT")
        self.assertLess(b.entry, b.box_low)
        self.assertGreater(b.stop, b.entry)
        self.assertLess(b.target, b.entry)

    def test_breakout_without_volume_is_ignored(self):
        self.assertIsNone(self.detect(breakout(volume=20_000)))  # 2x, needs 3x

    def test_close_back_inside_the_box_is_ignored(self):
        self.assertIsNone(self.detect(breakout(close=100.33)))

    def test_wick_without_a_body_is_ignored(self):
        cs = coil()
        wick = Candle(cs[-1].ts + timedelta(minutes=1), 100.30, 100.80, 100.25, 100.42, 50_000)
        self.assertIsNone(self.detect(cs + (wick,)))

    def test_stock_that_already_moved_before_the_break_is_not_flat(self):
        cs = list(breakout())
        cs[5] = Candle(cs[5].ts, cs[5].open, 101.05, cs[5].low, cs[5].close, cs[5].volume)
        self.assertIsNone(self.detect(tuple(cs)))

    def test_the_breakout_candle_itself_may_pass_one_percent(self):
        b = self.detect(breakout(close=101.02, volume=80_000))
        self.assertIsNotNone(b)
        self.assertGreater(flat.pct(b.close, PREV), 1.0)

    def test_stop_too_far_is_skipped(self):
        self.assertIsNone(flat.detect_breakout(stock("X", breakout(close=101.10, volume=80_000)), 100.0,
                                               flat.FlatConfig(max_risk_pct=0.5)))

    def test_find_breakouts_ranks_by_volume(self):
        parsed = {"A": stock("A", breakout(volume=40_000)), "B": stock("B", breakout(volume=90_000)),
                  "C": stock("C", coil())}
        found = flat.find_breakouts(parsed, {s: {"today_open": 100.0} for s in parsed}, self.cfg)
        self.assertEqual([b.symbol for b in found], ["B", "A"])

    def test_printing_a_breakout_does_not_crash(self):
        b = self.detect(breakout())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            flat.print_breakout(b, self.cfg, NOON, 500.0, breakout())
            flat.print_table(flat.scan({"C": stock("C", coil())}, {}, self.cfg)[0], self.cfg)
        self.assertIn("BREAKOUT: X UP", out.getvalue())
        self.assertIn("stop-entry ABOVE ₹100.67", out.getvalue())
        self.assertIn("WATCHLIST", out.getvalue())


class WatchModeTests(unittest.TestCase):
    """The minute loop beeps on breakouts only, one at a time."""

    def setUp(self):
        self.cfg = flat.FlatConfig()
        self.watch_pick, _ = flat.evaluate(stock("COIL", coil()), 100.0, self.cfg)
        self.brk = flat.detect_breakout(stock("COIL", breakout()), 100.0, self.cfg)

    def b(self, symbol: str, volume_ratio: float = 5.0):
        return replace(self.brk, symbol=symbol, volume_ratio=volume_ratio)

    def run_watch(self, start: datetime, scans, until: str = "10:10"):
        """``scans``: per minute, a list of breakouts (or an exception); the watchlist is always COIL."""
        clock = [start]
        beeps, calls = [], []

        def fetch(client, engine_cfg, cfg):
            item = scans[min(len(calls), len(scans) - 1)]
            calls.append(clock[0])
            if isinstance(item, Exception):
                raise item
            parsed = {s: stock(s, breakout()) for s in {b.symbol for b in item} | {"COIL"}}
            return flat.ScanResult(clock[0], parsed, [self.watch_pick], {}, 1, {},
                                   breakouts=sorted(item, key=lambda b: -b.volume_ratio))

        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

        args = type("Args", (), {"until": until, "scan_offset": 8, "risk_rupees": None})()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = flat.watch(None, None, self.cfg, run_audit(), args,
                              clock=lambda: clock[0], sleep=sleep, beep=lambda: beeps.append(clock[0]), fetch=fetch)
        return code, beeps, calls, out.getvalue()

    def test_a_watchlist_alone_never_beeps(self):
        _, beeps, calls, out = self.run_watch(NOON.replace(second=8), [[]], until="11:00")
        self.assertEqual(len(calls), 60)
        self.assertEqual(beeps, [])
        self.assertIn("no breakout", out)
        self.assertIn("watching COIL", out)

    def test_breakout_beeps_at_once_and_scans_every_minute(self):
        start = NOON.replace(second=8)
        code, beeps, calls, out = self.run_watch(start, [[], [self.b("COIL")], []])
        self.assertEqual(code, 0)
        self.assertEqual(beeps, [start + timedelta(minutes=1)])
        self.assertEqual(len(calls), 10)
        self.assertTrue(all(c.second == 8 for c in calls))
        self.assertIn("FLAT BREAKOUT", out)
        self.assertIn("BREAKOUT: COIL UP", out)

    def test_one_alert_at_a_time(self):
        start = NOON.replace(second=8)
        scans = [[self.b("A")], [self.b("B")], [self.b("C")], [], [], [self.b("D")]]
        _, beeps, _, out = self.run_watch(start, scans)
        self.assertEqual([b - start for b in beeps], [timedelta(0), timedelta(minutes=5)])
        self.assertIn("held back", out)

    def test_strongest_breakout_wins_the_minute(self):
        _, beeps, _, out = self.run_watch(NOON.replace(second=8), [[self.b("WEAK", 3.2), self.b("STRONG", 8.0)], []])
        self.assertEqual(len(beeps), 1)
        self.assertIn("FLAT BREAKOUT @ 10:00:08 IST: STRONG", out)

    def test_same_stock_realerts_only_after_an_hour(self):
        start = NOON.replace(second=8)
        _, beeps, _, _ = self.run_watch(start, [[self.b("COIL")]], until="11:10")
        self.assertEqual([b - start for b in beeps], [timedelta(0), timedelta(minutes=60)])

    def test_feed_error_is_retried_not_fatal(self):
        _, beeps, calls, out = self.run_watch(NOON.replace(second=8), [flat.FeedError(30, "down"), [self.b("COIL")]])
        self.assertGreater(len(calls), 3)
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
