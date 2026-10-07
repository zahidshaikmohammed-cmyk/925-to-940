import contextlib
import io
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta

import bbbbb_plus
import run_engine
from psygrid_client import PsygridClient
from strategy_930 import IST, Candle, Candidate

NOW = datetime(2026, 9, 30, 10, 5, 5, tzinfo=IST)


def payload(seed):
    rnd = random.Random(seed)
    t0 = datetime(2026, 9, 30, 9, 15, tzinfo=IST)
    p, rows = 100.0 + seed, []
    drift = rnd.uniform(-0.002, 0.002)
    for k in range(50):
        o = p
        c = p * (1 + drift + rnd.gauss(0, 0.002))
        rows.append({"timestamp": (t0 + timedelta(minutes=k)).isoformat(), "open": o,
                     "high": max(o, c) * 1.001, "low": min(o, c) * 0.999, "close": c,
                     "volume": rnd.randint(1000, 9000)})
        p = c
    return {"candles_1m": rows, "previous_close": 100.0 + seed}


class FakeClient(PsygridClient):
    def market(self):
        self.last_market_errors = ()
        self.last_market_duplicates = ()
        self.last_market_meta = {"endpoint": "public/live.json", "status": "OK", "session": {}}
        return {f"S{i:03d}": payload(i) for i in range(60)}


class SameEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.saved = (run_engine.PsygridClient, run_engine.now_ist)
        run_engine.PsygridClient = FakeClient
        run_engine.now_ist = lambda: NOW

    def tearDown(self):
        run_engine.PsygridClient, run_engine.now_ist = self.saved
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def run_quiet(self, fn, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(argv)
        return code, out.getvalue()

    def test_output_starts_with_exact_bbbbb_output(self):
        code_a, plain = self.run_quiet(run_engine.main, [])
        code_b, plus = self.run_quiet(bbbbb_plus.main, ["--no-manage"])
        self.assertEqual((code_a, code_b), (0, 0))
        strip = lambda text: [l for l in text.splitlines() if not l.startswith("SCAN TIME")]
        self.assertEqual(strip(plus)[:len(strip(plain))], strip(plain))
        self.assertIn("EXIT PLAN (bbbbb_plus)", plus)

    def test_selected_trade_is_identical(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with bbbbb_plus._Spy() as spy:
                run_engine.main([])
        with contextlib.redirect_stdout(io.StringIO()):
            cfg = run_engine.StrategyConfig()
            parsed = run_engine.parse_universe(FakeClient("x"), FakeClient("x").market(), NOW)
            parsed = run_engine.precision_screen(parsed, cfg, NOW)
            direct, *_ = run_engine.choose(parsed, run_engine.build_candidates(parsed, {}, cfg), cfg, NOW)
        self.assertEqual((spy.best.symbol, spy.best.side, spy.best.entry, spy.best.stop),
                         (direct.symbol, direct.side, direct.entry, direct.stop))

    def test_engine_functions_are_restored(self):
        before = (run_engine.select_global_best, run_engine.parse_universe)
        with contextlib.redirect_stdout(io.StringIO()):
            bbbbb_plus.main(["--no-manage"])
        self.assertEqual((run_engine.select_global_best, run_engine.parse_universe), before)


def cand(side, entry, stop):
    d = 1 if side == "LONG" else -1
    risk = abs(entry - stop)
    return Candidate("X", side, 80.0, entry, stop, entry + d * 2 * risk, 0, 0, 0, 0.5, 0.5,
                     0, 0, 0, 0, 1, stop, 1, ())


def bars(highs_lows):
    t0 = datetime(2026, 9, 30, 9, 15, tzinfo=IST)
    return [Candle(t0 + timedelta(minutes=i), (h + l) / 2, h, l, (h + l) / 2, 1) for i, (h, l) in enumerate(highs_lows)]


class ExitPlanTests(unittest.TestCase):
    def test_entry_and_stop_never_change(self):
        c = cand("LONG", 100.0, 98.0)
        plan = bbbbb_plus.exit_plan(c, bars([(103, 99)]))
        self.assertEqual(plan.risk, 2.0)
        self.assertEqual((c.entry, c.stop), (100.0, 98.0))

    def test_day_high_between_1r_and_2r_is_tp2(self):
        plan = bbbbb_plus.exit_plan(cand("LONG", 100.0, 98.0), bars([(103, 99)]))       # high = 1.5R
        self.assertEqual((plan.tp1, plan.tp2), (102.0, 103.0))

    def test_day_high_closer_than_1r_becomes_tp1(self):
        plan = bbbbb_plus.exit_plan(cand("LONG", 100.0, 98.0), bars([(101.5, 99)]))     # high = 0.75R
        self.assertEqual(plan.tp1, 101.5)
        self.assertEqual(plan.tp2, 104.0)
        self.assertIn("stall", plan.note)

    def test_entry_at_the_high_warns(self):
        plan = bbbbb_plus.exit_plan(cand("LONG", 100.0, 98.0), bars([(100.2, 99)]))
        self.assertEqual((plan.tp1, plan.tp2), (102.0, 104.0))
        self.assertIn("breakout", plan.note)

    def test_short_side(self):
        plan = bbbbb_plus.exit_plan(cand("SHORT", 100.0, 102.0), bars([(101, 97)]))     # low = 1.5R
        self.assertEqual((plan.tp1, plan.tp2), (98.0, 97.0))


if __name__ == "__main__":
    unittest.main()
