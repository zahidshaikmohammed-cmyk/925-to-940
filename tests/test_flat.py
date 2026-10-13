"""flat.py: only stocks that opened flat and stayed within ±1% qualify; a tight, squeezing box scores high."""

from __future__ import annotations

import contextlib
import io
import unittest
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


if __name__ == "__main__":
    unittest.main()
