import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from old import OldConfig, Trade, clock_check, load_trade, manage_step, save_trade
from strategy_930 import IST, Candle
from tests.test_old import CLEAN_LONG, make

# CLEAN_LONG covers 09:15-09:28; the trade is taken at 09:29:05.
BASE = make(CLEAN_LONG)
OPENED = datetime(2026, 9, 30, 9, 29, 5, tzinfo=IST)


def after(rows):
    """Candles continuing from 09:29 onwards."""
    t0 = datetime(2026, 9, 30, 9, 29, tzinfo=IST)
    return BASE + [Candle(t0 + timedelta(minutes=i), o, h, l, c, v) for i, (o, h, l, c, v) in enumerate(rows)]


def new_trade():
    return Trade("CLEAN", "LONG", 101.6, 100.79, 102.1, 103.2,
                 OPENED.isoformat(), BASE[-1].ts.isoformat(), 100.79)


def kinds(events):
    return [k for _, k, _ in events]


class TradeManagerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = OldConfig()

    def test_stop_loss_is_minus_one_r(self):
        tr = new_trade()
        manage_step(tr, after([(101.6, 101.65, 100.7, 100.75, 900)]), self.cfg)
        self.assertEqual(tr.exit_reason, "STOP LOSS HIT")
        self.assertAlmostEqual(tr.realized_r, -1.0)

    def test_candle_touching_stop_and_target_counts_as_stop(self):
        tr = new_trade()
        manage_step(tr, after([(101.6, 102.2, 100.7, 101.6, 900)]), self.cfg)
        self.assertEqual(tr.exit_reason, "STOP LOSS HIT")

    def test_book_half_at_tp1_then_breakeven(self):
        tr = new_trade()
        ev = manage_step(tr, after([(101.6, 102.15, 101.58, 102.1, 900),
                                    (102.1, 102.1, 101.5, 101.55, 900)]), self.cfg)
        self.assertIn("BOOK", kinds(ev))
        self.assertEqual(tr.exit_reason, "BREAKEVEN STOP")
        self.assertAlmostEqual(tr.realized_r, 0.5 * (102.1 - 101.6) / tr.risk)

    def test_time_stop_when_trade_goes_nowhere(self):
        tr = new_trade()
        flat = [(101.65, 101.7, 101.6, 101.65, 900)] * 25
        manage_step(tr, after(flat), self.cfg)
        self.assertEqual(tr.exit_reason, "TIME STOP")
        self.assertEqual(datetime.fromisoformat(tr.last_ts).astimezone(IST).strftime("%H:%M"), "09:49")

    def test_five_minute_close_below_vwap_exits(self):
        tr = new_trade()
        manage_step(tr, after([(101.6, 101.6, 101.0, 101.0, 900)]), self.cfg)  # completes 09:25 bar
        self.assertEqual(tr.exit_reason, "5M CLOSE WRONG SIDE OF VWAP")

    def test_runner_trails_on_five_minute_closes(self):
        tr = new_trade()
        rows = [(101.6, 102.2, 101.58, 102.15, 900)]                  # 09:29 book, bar completes
        rows += [(102.3, 102.5, 102.2, 102.4, 900)] * 5               # 09:30-09:34 higher bar
        rows += [(102.1, 102.15, 101.95, 102.0, 900)] * 5             # 09:35-09:39 closes below 102.2
        ev = manage_step(tr, after(rows), self.cfg)
        self.assertIn("TRAIL", kinds(ev))
        self.assertEqual(tr.exit_reason, "5M TRAIL BROKEN")
        self.assertAlmostEqual(tr.realized_r, 0.5 * 0.5 / tr.risk + 0.5 * 0.4 / tr.risk)

    def test_max_hold(self):
        tr = new_trade()
        cfg = replace(self.cfg, max_hold_minutes=8)
        rows = [(101.6, 102.2, 101.58, 102.15, 900)]
        rows += [(102.2 + i * 0.01, 102.25 + i * 0.01, 102.18 + i * 0.01, 102.22 + i * 0.01, 900) for i in range(12)]
        manage_step(tr, after(rows), cfg)
        self.assertEqual(tr.exit_reason, "MAX HOLD 8 MIN")

    def test_already_processed_candles_are_not_replayed(self):
        tr = new_trade()
        cs = after([(101.65, 101.7, 101.6, 101.65, 900)])
        self.assertEqual(len(manage_step(tr, cs, self.cfg)), 1)
        self.assertEqual(manage_step(tr, cs, self.cfg), [])

    def test_short_stop(self):
        tr = Trade("S", "SHORT", 100.0, 101.0, 99.0, 97.0, OPENED.isoformat(), BASE[-1].ts.isoformat(), 101.0)
        manage_step(tr, after([(100.0, 101.1, 99.9, 100.9, 900)]), self.cfg)
        self.assertAlmostEqual(tr.realized_r, -1.0)

    def test_stalled_feed_still_enforces_max_hold(self):
        tr = new_trade()
        tr.booked_half = True
        tr.current_stop = tr.entry
        self.assertEqual(clock_check(tr, OPENED + timedelta(minutes=30), 102.0, self.cfg), [])
        ev = clock_check(tr, OPENED + timedelta(minutes=63), 102.0, self.cfg)
        self.assertEqual(kinds(ev), ["EXIT"])
        self.assertTrue(tr.closed)

    def test_stalled_feed_time_stop(self):
        tr = new_trade()
        clock_check(tr, OPENED + timedelta(minutes=23), 101.62, self.cfg)
        self.assertEqual(tr.exit_reason, "TIME STOP (FEED STALLED)")

    def test_save_and_resume_roundtrip(self):
        tr = new_trade()
        tr.booked_half = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            save_trade(tr, path)
            self.assertEqual(load_trade(path), tr)


if __name__ == "__main__":
    unittest.main()
