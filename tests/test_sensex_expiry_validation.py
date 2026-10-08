"""Real-data validation pipeline: Dhan fetch/build with a fake client, the underlying exit
engine used by Test A and the controls, statistics and multiple-testing corrections, and a
complete (tiny, synthetic) validation run."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from sensex_expiry.config import EngineConfig
from sensex_expiry.history import load_folder
from sensex_expiry.models import IST, Candle, Direction
from sensex_expiry.realdata import build, fetch
from sensex_expiry.synthetic import make_day
from sensex_expiry.validation import (ValidationRun, benjamini_hochberg, holm, randomization_p, significance,
                                      simulate_underlying)

CFG = EngineConfig()
OPEN = datetime(2026, 10, 8, 9, 15, tzinfo=IST)


def bars(closes, start=OPEN, spread=5.0):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Candle(start + timedelta(minutes=i), prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c
    return out


class TestUnderlyingExitEngine(unittest.TestCase):
    def test_intrabar_stop_is_1_2_x_distance_and_fills_at_open_on_gap(self):
        c = bars([81000] * 5 + [80900] * 5)            # gap down through the stop
        sim = simulate_underlying(CFG, c, 1, Direction.LONG, 50.0, 80950.0, None)
        self.assertEqual(sim["reason"], "EXIT_STOP")
        self.assertLessEqual(sim["r"], -1.2)

    def test_invalidation_close_exits_next_open(self):
        closes = [81000, 81000, 81000, 80985, 80970, 80970, 80970]     # stop at 80940 is never touched
        c = bars(closes, spread=1.0)
        sim = simulate_underlying(CFG, c, 1, Direction.LONG, 50.0, 80975.0, None)
        self.assertEqual((sim["reason"], sim["exit_ts"]), ("EXIT_INVALIDATION", c[5].start))

    def test_hold_bars_mode_has_no_stop(self):
        c = bars([81000 - 10 * i for i in range(30)], spread=1.0)
        sim = simulate_underlying(CFG, c, 1, Direction.LONG, 20.0, None, None, rules=False, hold_bars=10)
        self.assertEqual(sim["reason"], "EXIT_HOLD_DONE")
        self.assertLess(sim["r"], -3)

    def test_short_is_mirror_of_long(self):
        up = bars([81000 + 8 * i for i in range(40)], spread=1.0)
        dn = [Candle(x.start, 162000 - x.open, 162000 - x.low, 162000 - x.high, 162000 - x.close) for x in up]
        a = simulate_underlying(CFG, up, 1, Direction.LONG, 20.0, 80980.0, None)
        b = simulate_underlying(CFG, dn, 1, Direction.SHORT, 20.0, 162000 - 80980.0, None)
        self.assertAlmostEqual(a["r"], b["r"])
        self.assertEqual(a["reason"], b["reason"])


class TestStatistics(unittest.TestCase):
    def test_holm_and_bh(self):
        p = {"a": 0.01, "b": 0.04, "c": 0.03}
        h = holm(p)
        self.assertAlmostEqual(h["a"], 0.03)
        self.assertAlmostEqual(h["c"], 0.06)
        self.assertAlmostEqual(h["b"], 0.06)            # monotone
        bh = benjamini_hochberg(p)
        self.assertAlmostEqual(bh["a"], 0.03)
        self.assertAlmostEqual(bh["b"], 0.04)
        self.assertAlmostEqual(bh["c"], 0.04)

    def test_randomization_p_never_zero(self):
        self.assertAlmostEqual(randomization_p(10.0, [0.0] * 99), 0.01)
        self.assertAlmostEqual(randomization_p(-10.0, [0.0] * 99), 1.0)

    def test_significance_small_sample(self):
        s = significance([1.0, -1.0, 2.0, -1.0, -1.0])
        self.assertTrue(s["bootstrap_ci95"][0] < 0 < s["bootstrap_ci95"][1])
        self.assertGreater(s["n_needed_for_0.15R_80pct_power"], 100)
        self.assertEqual(significance([1.0])["n"], 1)


class FakeDhan:
    """Serves synthetic days in Dhan's array response shape, with true-UTC epochs and
    ROLLING moneyness option series (the contract behind ATM+k changes with spot)."""

    def __init__(self, days):
        self.days = {d.day: d for d in days}
        self.calls = 0

    @staticmethod
    def _arr(cs, extra=None):
        out = {"timestamp": [int(x.start.timestamp()) for x in cs], "open": [x.open for x in cs],
               "high": [x.high for x in cs], "low": [x.low for x in cs], "close": [x.close for x in cs],
               "volume": [x.volume for x in cs]}
        for k, v in (extra or {}).items():
            out[k] = v
        return out

    def _in(self, a, b):
        a, b = date.fromisoformat(a), date.fromisoformat(b)
        return [d for k, d in sorted(self.days.items()) if a <= k <= b]

    def historical_daily_data(self, sid, seg, inst, a, b):
        self.calls += 1
        ds = self._in(a, b)
        daily = [Candle(datetime(d.day.year, d.day.month, d.day.day, tzinfo=IST), d.underlying[0].open,
                        max(x.high for x in d.underlying), min(x.low for x in d.underlying), d.underlying[-1].close)
                 for d in ds]
        return {"status": "success", "data": self._arr(daily)}

    def intraday_minute_data(self, sid, seg, inst, a, b, interval):
        self.calls += 1
        return {"status": "success", "data": self._arr([x for d in self._in(a, b) for x in d.underlying])}

    def expired_options_data(self, sid, seg, inst, flag, code, strike_rel, right, fields, a, b, interval):
        self.calls += 1
        if code != 1:
            return {"status": "success", "data": {"ce": None, "pe": None}}
        k = 0 if strike_rel == "ATM" else int(strike_rel[3:])
        r = "CE" if right == "CALL" else "PE"
        cs, strikes, spots = [], [], []
        for d in self._in(a, b):
            if not d.options:
                continue
            for u in d.underlying:
                strike = int(round(u.close / 100) * 100) + 100 * k
                o = d.options.get((strike, r), {}).get(u.start)
                if o is not None:
                    cs.append(o)
                    strikes.append(strike)
                    spots.append(u.close)
        arr = self._arr(cs, {"strike": strikes, "spot": spots, "iv": [0.0] * len(cs), "oi": [0] * len(cs)})
        return {"status": "success", "data": {"ce" if r == "CE" else "pe": arr}}


class TestRealDataPipeline(unittest.TestCase):
    def test_fetch_build_roundtrip_reconstructs_fixed_strikes(self):
        thu = date(2026, 9, 24)
        days = [make_day(thu - timedelta(days=2), seed=1, is_expiry=False, strikes_each_side=0),
                make_day(thu - timedelta(days=1), seed=2, is_expiry=False, strikes_each_side=0),
                make_day(thu, seed=3, strikes_each_side=10)]
        days[0].options, days[1].options = {}, {}
        fake = FakeDhan(days)
        import sensex_expiry.dhan_adapter as da
        orig = da._client
        da._client = lambda cid, tok: fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                raw, out = Path(tmp) / "raw", Path(tmp) / "days"
                m = fetch("id", "tok", thu - timedelta(days=2), thu, raw, index_id="51", expiry_code=1, log=lambda *a: None)
                self.assertEqual(m["failures"], [])
                n_calls = fake.calls
                fetch("id", "tok", thu - timedelta(days=2), thu, raw, index_id="51", expiry_code=1, log=lambda *a: None)
                self.assertEqual(fake.calls, n_calls)          # second run served entirely from raw/
                rep = build(raw, out, log=lambda *a: None)
                built = {d.day: d for d in load_folder(out)}
        finally:
            da._client = orig
        self.assertEqual(rep["built"], 3)
        self.assertEqual(rep["expiry_days"], 1)
        exp = built[thu]
        self.assertTrue(exp.is_expiry)
        self.assertTrue(exp.tags["expiry_confirmed_by_data"])
        self.assertFalse(built[thu - timedelta(days=1)].is_expiry)
        # prior day comes from the previous session's daily bar, never from the same day
        self.assertEqual(exp.prior.day, thu - timedelta(days=1))
        self.assertAlmostEqual(exp.prior.high, max(x.high for x in days[1].underlying))
        # every reconstructed fixed-strike print equals the true contract's print
        src = days[2].options
        n = 0
        for key, series in exp.options.items():
            for ts, cdl in series.items():
                self.assertAlmostEqual(cdl.close, src[key][ts].close)
                n += 1
        self.assertGreater(n, 300)
        self.assertEqual([x.close for x in exp.underlying], [x.close for x in days[2].underlying])


class TestValidationRun(unittest.TestCase):
    def test_tiny_synthetic_run_completes_audits_and_refuses_yes(self):
        exp = [make_day(date(2025, 9, 4) + timedelta(days=7 * i), seed=50 + i, inject_sweep=(i % 2 == 0)) for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            r = ValidationRun(CFG, exp, Path(tmp), n_null=5, label="SYNTHETIC TEST").run()
            self.assertTrue((Path(tmp) / "VALIDATION_REPORT.md").exists())
            self.assertTrue((Path(tmp) / "trades_testB_options.csv").exists())
            saved = json.loads((Path(tmp) / "validation_report.json").read_text())
        self.assertEqual(saved["config_hash"], CFG.config_hash())
        self.assertTrue(r["lookahead_audit"]["truncation_equality"]["ok"])
        self.assertNotEqual(r["verdict"]["answer"], "YES")        # 3 days can never be evidence
        for key in ("TEST_A_all_days_underlying", "TEST_B_expiry_options", "TEST_C_setups", "TEST_D_windows",
                    "TEST_E_regimes", "controls_A", "controls_B", "multiple_testing"):
            self.assertIn(key, r)
        self.assertEqual(len(r["TEST_D_windows"]["A"]), 8)


if __name__ == "__main__":
    unittest.main()
