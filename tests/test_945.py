"""Tests for 945.py and the intelligence/ selector package."""
import contextlib
import copy
import dataclasses
import gzip
import importlib.util
import io
import json
import random
import sqlite3
import tempfile
import time as systime
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

from intelligence import selector_backtest, selector_scoring
from intelligence.selector_calibration import Calibrator
from intelligence.selector_config import SelectorConfig, load_weights
from intelligence.selector_data import (IST, Series, fast_candles, freeze_information_set, future_bars,
                                        parse_payload)
from intelligence.selector_945 import decide, render
from intelligence.selector_features import _percentiles, build_feature_table, stock_features
from intelligence.selector_outcomes import evaluate_decision, evaluate_horizon
from intelligence.selector_snapshot import verify_fingerprint
from intelligence.selector_store import DecisionExists, Store
from intelligence.selector_synthetic import make_index_payload, make_payload
from psygrid_client import PsygridClient

ROOT = Path(__file__).resolve().parents[1]
CFG = SelectorConfig()
W = load_weights()
DAY = date(2026, 9, 30)
T0 = datetime(2026, 9, 30, 9, 15, tzinfo=IST)


def load_945():
    spec = importlib.util.spec_from_file_location("psygrid_945", ROOT / "945.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def series(closes, prev_close=100.0, start=T0, vols=None, spread=0.1):
    o, h, l, c, v, ts = [], [], [], [], [], []
    prev = closes[0]
    for i, x in enumerate(closes):
        op = prev if i else x
        o.append(op); c.append(x)
        h.append(max(op, x) + spread); l.append(min(op, x) - spread)
        v.append((vols or [1000] * len(closes))[i]); ts.append(start + timedelta(minutes=i))
        prev = x
    return Series("X", tuple(ts), tuple(o), tuple(h), tuple(l), tuple(c), tuple(v), prev_close, None)


def decide_payload(payload, index=None, mode="replay", **kw):
    raw = parse_payload(payload, index)
    si = freeze_information_set(raw, CFG.cutoff)
    snap, inner = decide(si, CFG, W, mode, {}, {}, [], published_at=datetime(2026, 9, 30, 9, 45, 3, tzinfo=IST), **kw)
    return snap, inner, raw, si


class FeatureMathTests(unittest.TestCase):
    def test_gap_previous_close_and_returns(self):
        s = series([102.0 + 0.1 * i for i in range(30)], prev_close=100.0)
        f = stock_features(s, CFG, datetime(2026, 9, 30, 9, 45, tzinfo=IST), None)
        self.assertEqual(f["previous_close"], 100.0)
        self.assertAlmostEqual(f["gap_pct"], 2.0)
        self.assertAlmostEqual(f["return_since_open"], (104.9 / 102.0 - 1) * 100)
        self.assertAlmostEqual(f["return_1m"], (104.9 / 104.8 - 1) * 100)
        self.assertAlmostEqual(f["return_5m"], (104.9 / 104.4 - 1) * 100)
        self.assertAlmostEqual(f["return_15m"], (104.9 / 103.4 - 1) * 100)
        self.assertAlmostEqual(f["return_30m"], (104.9 / 102.0 - 1) * 100)
        self.assertAlmostEqual(f["dist_prev_close_pct"], 4.9)

    def test_opening_range(self):
        closes = [100, 101, 99, 100, 102, 100, 100, 101, 100, 100, 100, 100, 100, 100, 100] + [103] * 15
        f = stock_features(series(closes, spread=0.0), CFG, datetime(2026, 9, 30, 9, 45, tzinfo=IST), None)
        self.assertEqual((f["opening_high"], f["opening_low"]), (102, 99))
        self.assertEqual(f["or_break"], 1)
        self.assertAlmostEqual(f["dist_or_high_pct"], (103 / 102 - 1) * 100)
        self.assertGreater(f["or_position"], 0.5)

    def test_volume_features(self):
        vols = [100] * 15 + [100] * 10 + [400] * 5
        closes = [100 + (0.1 if i % 2 else -0.05) * i for i in range(30)]
        f = stock_features(series(closes, vols=vols), CFG, datetime(2026, 9, 30, 9, 45, tzinfo=IST), (1000.0, None))
        self.assertEqual(f["cumulative_volume"], sum(vols))
        self.assertEqual(f["volume_5m"], 2000)
        self.assertAlmostEqual(f["volume_acceleration"], 4.0)
        self.assertAlmostEqual(f["relative_volume"], sum(vols) / 1000.0)
        self.assertTrue(-1 <= f["volume_imbalance"] <= 1)

    def test_relative_volume_null_without_baseline(self):
        f = stock_features(series([100.0] * 30), CFG, datetime(2026, 9, 30, 9, 45, tzinfo=IST), None)
        self.assertIsNone(f["relative_volume"])
        self.assertEqual(f["relative_volume_source"], "UNAVAILABLE_NO_BASELINE")

    def test_missing_previous_close_is_null_not_fabricated(self):
        f = stock_features(series([100.0] * 30, prev_close=None), CFG, datetime(2026, 9, 30, 9, 45, tzinfo=IST), None)
        self.assertIsNone(f["gap_pct"])
        self.assertIsNone(f["previous_close"])
        self.assertGreaterEqual(f["missing_field_count"], 1)

    def test_percentiles_ties_deterministic(self):
        p = _percentiles({"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0, "e": None})
        self.assertEqual(p, {"a": 0.0, "b": 0.5, "c": 0.5, "d": 1.0, "e": None})


class DataContractTests(unittest.TestCase):
    def test_fast_parser_matches_psygrid_client_contract(self):
        rnd = random.Random(5)
        rows = []
        for i in range(3000):
            r = {"timestamp": (T0 + timedelta(minutes=i % 375)).strftime("%Y-%m-%d %H:%M:%S IST"),
                 "open": rnd.choice([100, 0, -1, "NaN", "inf", "x", 101.5]), "high": rnd.choice([102, 99, 101.5, "NaN"]),
                 "low": rnd.choice([98, 100.5, 0, 99]), "close": rnd.choice([101, 97, 103, 100]),
                 "volume": rnd.choice([10, -5, 0, "Infinity"])}
            if i % 50 == 0:
                r["complete"] = False
            if i % 77 == 0:
                del r["volume"]
            rows.append(r)
        rows += ["junk", None, {"timestamp": "garbage", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        ref = [(c.ts, c.open, c.high, c.low, c.close, c.volume) for c in PsygridClient.candles(rows)]
        self.assertEqual(fast_candles(rows), ref)

    def test_cutoff_includes_0944_excludes_0945(self):
        raw = parse_payload(make_payload(5, minutes=60, broken=False))
        si = freeze_information_set(raw, CFG.cutoff)
        for s in si.stocks.values():
            self.assertEqual(s.ts[-1].strftime("%H:%M"), "09:44")
            self.assertTrue(all(t < si.cutoff for t in s.ts))
            self.assertEqual(len(s), 30)

    def test_previous_day_candles_are_ignored(self):
        p = make_payload(3, minutes=40, broken=False)
        for st in p["stocks"].values():
            st["candles_1m"].insert(0, {"timestamp": "2026-09-29 15:29:00 IST", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
        si = freeze_information_set(parse_payload(p), CFG.cutoff)
        self.assertTrue(all(s.ts[0].date() == DAY for s in si.stocks.values()))


class DecisionTests(unittest.TestCase):
    def test_exactly_one_selection_and_accounting(self):
        snap, inner, raw, si = decide_payload(make_payload(989, minutes=30, seed=4))
        self.assertEqual(snap.universe_size, 989)
        self.assertEqual(snap.eligible_count + snap.excluded_count, snap.universe_size)
        self.assertEqual(sum(snap.to_dict()["exclusion_reasons"].values()), snap.excluded_count)
        self.assertIn(snap.selected_symbol, inner["table"].eligible)
        self.assertIn(snap.direction, ("UP", "DOWN"))
        self.assertEqual(inner["ranked"][0].symbol, snap.selected_symbol)

    def test_score_bounds(self):
        _, inner, _, _ = decide_payload(make_payload(300, minutes=30, seed=9))
        self.assertTrue(all(0.0 <= c.score <= 100.0 for c in inner["ranked"]))

    def test_planted_up_trend_selected_up(self):
        snap, *_ = decide_payload(make_payload(400, minutes=30, seed=2, planted={"STK0123": 0.15}))
        self.assertEqual((snap.selected_symbol, snap.direction), ("STK0123", "UP"))

    def test_planted_down_trend_selected_down(self):
        snap, *_ = decide_payload(make_payload(400, minutes=30, seed=2, planted={"STK0077": -0.15}))
        self.assertEqual((snap.selected_symbol, snap.direction), ("STK0077", "DOWN"))

    def test_deterministic_and_order_independent(self):
        p = make_payload(500, minutes=30, seed=6)
        a, *_ = decide_payload(p)
        shuffled = dict(p)
        items = list(p["stocks"].items())
        random.Random(1).shuffle(items)
        shuffled["stocks"] = dict(items)
        b, *_ = decide_payload(shuffled)
        self.assertEqual(a.decision_fingerprint, b.decision_fingerprint)
        self.assertEqual(a.input_fingerprint, b.input_fingerprint)

    def test_stale_and_bad_stocks_never_crash_and_are_reported(self):
        snap, inner, *_ = decide_payload(make_payload(989, minutes=30, seed=4))
        reasons = snap.to_dict()["exclusion_reasons"]
        self.assertIn("STALE_DATA", reasons)
        self.assertIn("TOO_FEW_BARS", reasons)
        self.assertIn("PAYLOAD_NOT_OBJECT", reasons)

    def test_still_selects_one_when_almost_everything_is_unusable(self):
        p = make_payload(50, minutes=30, seed=3, broken=False)
        for i, (sym, st) in enumerate(p["stocks"].items()):
            if i:
                st["candles_1m"] = st["candles_1m"][:3]
        snap, *_ = decide_payload(p)
        self.assertEqual(snap.selected_symbol, "STK0000")
        self.assertEqual(snap.eligible_count, 1)

    def test_nothing_usable_refuses_to_fabricate(self):
        p = make_payload(10, minutes=30, broken=False)
        for st in p["stocks"].values():
            st["candles_1m"] = []
        with self.assertRaises(RuntimeError):
            decide_payload(p)

    def test_uses_nifty_when_available(self):
        snap, *_ = decide_payload(make_payload(200, minutes=30, seed=3), make_index_payload(minutes=30))
        self.assertEqual(snap.to_dict()["market_context"]["market_source"], "NIFTY")
        snap2, *_ = decide_payload(make_payload(200, minutes=30, seed=3))
        self.assertEqual(snap2.to_dict()["market_context"]["market_source"], "UNIVERSE_MEDIAN")

    def test_probability_is_labelled_uncalibrated_without_history(self):
        snap, *_ = decide_payload(make_payload(200, minutes=30, seed=3))
        self.assertTrue(snap.probability_status.startswith("UNCALIBRATED"))
        self.assertIsNone(snap.expected_return_pct)
        self.assertTrue(0 < snap.estimated_probability < 1)
        self.assertIn("PRIOR", snap.horizon_status)


class NoLookAheadTests(unittest.TestCase):
    def test_full_day_feed_equals_feed_cut_at_0945(self):
        full = make_payload(989, minutes=375, seed=8)
        cut = copy.deepcopy(full)
        for st in cut["stocks"].values():
            if isinstance(st, dict):
                st["candles_1m"] = [r for r in st["candles_1m"] if isinstance(r, dict) and r.get("timestamp", "") < "2026-09-30 09:45"]
        a, *_ = decide_payload(full, make_index_payload(minutes=375))
        b, *_ = decide_payload(cut, make_index_payload(minutes=30))
        self.assertEqual(a.input_fingerprint, b.input_fingerprint)
        self.assertEqual(a.decision_fingerprint, b.decision_fingerprint)

    def test_poisoned_future_does_not_change_the_decision(self):
        clean = make_payload(989, minutes=375, seed=12)
        first, *_ = decide_payload(clean, make_index_payload(minutes=375))
        poisoned = copy.deepcopy(clean)
        rnd = random.Random(99)
        for st in poisoned["stocks"].values():
            if not isinstance(st, dict):
                continue
            for r in st["candles_1m"]:
                if (isinstance(r, dict) and r.get("timestamp", "") >= "2026-09-30 09:45"
                        and isinstance(r.get("open"), (int, float))):
                    k = rnd.choice([0.2, 5.0, 50.0])
                    r.update(open=r["open"] * k, high=r["high"] * k * 1.5, low=r["low"] * k * 0.5,
                             close=r["close"] * k, volume=r["volume"] * 10_000)
        idx = make_index_payload(minutes=375, drift_pct=0.9)
        idx["1m"] = make_index_payload(minutes=375)["1m"][:30] + idx["1m"][30:]
        second, *_ = decide_payload(poisoned, idx)
        self.assertEqual(second.to_dict()["selected_symbol"], first.selected_symbol)
        self.assertEqual(second.direction, first.direction)
        self.assertEqual(second.selection_score, first.selection_score)
        self.assertEqual(second.input_fingerprint, first.input_fingerprint)
        self.assertEqual(second.decision_fingerprint, first.decision_fingerprint)

    def test_decision_code_never_imports_outcome_evaluator(self):
        for mod in ("selector_features.py", "selector_scoring.py", "selector_945.py", "selector_calibration.py",
                    "selector_data.py"):
            imports = [ln for ln in (ROOT / "intelligence" / mod).read_text().splitlines()
                       if ln.lstrip().startswith(("import ", "from "))]
            self.assertFalse([ln for ln in imports if "selector_outcomes" in ln or "future_bars" in ln], mod)


class OutcomeTests(unittest.TestCase):
    def test_outcome_metrics(self):
        bars = series([100.5, 101.0, 99.6, 100.8, 101.2], start=datetime(2026, 9, 30, 9, 45, tzinfo=IST), spread=0.0)
        o = evaluate_horizon(100.0, "UP", bars, 5, 0.25, 0.05)
        self.assertAlmostEqual(o.forward_return_pct, 1.2)
        self.assertAlmostEqual(o.mfe_pct, 1.2)
        self.assertAlmostEqual(o.mae_pct, -0.4)
        self.assertEqual((o.minutes_to_favorable, o.minutes_to_adverse, o.outcome), (1, 3, "WIN"))
        d = evaluate_horizon(100.0, "DOWN", bars, 5, 0.25, 0.05)
        self.assertAlmostEqual(d.forward_return_pct, -1.2)
        self.assertEqual(d.outcome, "LOSS")

    def test_incomplete_window(self):
        bars = series([100.5, 101.0], start=datetime(2026, 9, 30, 9, 45, tzinfo=IST))
        self.assertEqual(evaluate_horizon(100.0, "UP", bars, 15, 0.25, 0.05).outcome, "INCOMPLETE")

    def test_outcomes_only_read_after_cutoff(self):
        snap, _, raw, _ = decide_payload(make_payload(200, minutes=375, seed=5))
        cut = datetime.fromisoformat(snap.cutoff)
        fb = future_bars(raw, snap.selected_symbol, cut, 15)
        self.assertEqual(len(fb), 15)
        self.assertTrue(all(cut <= t < cut + timedelta(minutes=15) for t in fb.ts))
        outs = evaluate_decision(snap.to_dict(), raw, CFG.horizons_min, 0.25, 0.05)
        self.assertEqual([o.horizon_min for o in outs], [5, 15, 30])
        self.assertTrue(all(o.complete for o in outs))


class ImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.snap, *_ = decide_payload(make_payload(200, minutes=30, seed=3), mode="live")

    def test_snapshot_cannot_be_mutated(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.snap.selected_symbol = "OTHER"
        with self.assertRaises(TypeError):
            self.snap.feature_snapshot["current_price"] = 1.0
        with self.assertRaises(TypeError):
            self.snap.market_context["regime"] = "UP"
        self.assertTrue(verify_fingerprint(self.snap))

    def test_store_is_insert_only(self):
        st = Store(":memory:")
        st.save_decision(self.snap)
        with self.assertRaises(DecisionExists):
            st.save_decision(self.snap)
        with self.assertRaises(sqlite3.DatabaseError):
            with st.db:
                st.db.execute("UPDATE decisions SET selected_symbol='HACK'")
        with self.assertRaises(sqlite3.DatabaseError):
            with st.db:
                st.db.execute("DELETE FROM decisions")
        back = st.get_decision(self.snap.decision_date, "live")
        self.assertEqual(back.decision_fingerprint, self.snap.decision_fingerprint)
        self.assertTrue(verify_fingerprint(back))

    def test_outcomes_update_without_touching_decision(self):
        st = Store(":memory:")
        st.save_decision(self.snap)
        raw = parse_payload(make_payload(200, minutes=375, seed=3))
        for _ in range(2):
            st.save_outcomes(self.snap.decision_id, evaluate_decision(self.snap.to_dict(), raw, (5, 15), 0.25, 0.05))
        self.assertEqual(sorted(st.outcomes(self.snap.decision_id)), [5, 15])
        self.assertEqual(st.get_decision(self.snap.decision_date, "live").to_json(), self.snap.to_json())


class CalibrationTests(unittest.TestCase):
    def test_uncalibrated_then_empirical(self):
        fake = [({"selection_score": 80.0}, {15: {"complete": True, "forward_return_pct": 0.3 if i % 3 else -0.2}})
                for i in range(70)]
        self.assertTrue(Calibrator(fake[:10], CFG).estimate(80, 2.0).probability_status.startswith("UNCALIBRATED"))
        est = Calibrator(fake, CFG).estimate(80, 2.0)
        self.assertTrue(est.probability_status.startswith("EMPIRICAL_WALK_FORWARD"))
        self.assertAlmostEqual(est.probability, (sum(1 for i in range(70) if i % 3) + 1) / 72)
        self.assertIsNotNone(est.expected_return_pct)


class BacktestTests(unittest.TestCase):
    def test_walk_forward_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            for k, day in enumerate((date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1))):
                blob = {"stocks_payload": make_payload(300, session=day, minutes=120, seed=20 + k),
                        "index_payload": make_index_payload(session=day, minutes=120, seed=k)}
                (sessions / f"{day}.json.gz").write_bytes(gzip.compress(json.dumps(blob).encode()))
            db = str(Path(tmp) / "bt.sqlite")
            res = selector_backtest.run_backtest([str(sessions)], db, CFG, W, {}, log=lambda *_: None)
            self.assertEqual([r.session_date for r in res], ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"])
            st = Store(db)
            # walk-forward: each day's history and baselines come only from earlier days
            self.assertEqual(len(st.history(before="2026-09-30", modes=("backtest",))), 2)
            self.assertEqual(st.baselines("2026-09-28", 10, 1), {})
            self.assertTrue(st.baselines("2026-10-01", 10, 3))           # 3 prior sessions -> relative volume
            last = st.get_decision("2026-10-01", "backtest").to_dict()
            self.assertEqual(last["feature_snapshot"]["relative_volume_source"], "PRIOR_SESSIONS_MEDIAN")
            self.assertTrue(all(st.outcomes(d["decision_id"]) for d in st.decisions(mode="backtest")))
            st.close()
            rep = selector_backtest.report(db, CFG)
            for text in ("HORIZON +5", "HORIZON +15", "HORIZON +30", "LATE (out-of-sample)", "DIRECTION UP",
                         "LIQUIDITY", "REGIME", "SELECTION CONCENTRATION", "FEATURE VALIDATION"):
                self.assertIn(text, rep)
            # re-running never overwrites the stored decisions
            again = selector_backtest.run_backtest([str(sessions)], db, CFG, W, {}, log=lambda *_: None)
            self.assertEqual([r.symbol for r in again], [r.symbol for r in res])


class CliTests(unittest.TestCase):
    def test_replay_cli_and_terminal_publication(self):
        mod = load_945()
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.json"
            f.write_text(json.dumps(make_payload(150, minutes=60, seed=3)))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mod.main(["--replay", str(f), "--db", str(Path(tmp) / "x.sqlite")])
            text = out.getvalue()
            self.assertEqual(code, 0)
            for s in ("PSYGRID 945 -- 09:45 INTRADAY SELECTION", "Symbol", "Direction", "Score", "Probability",
                      "UNCALIBRATED", "WHY SELECTED", "DATA QUALITY", "Input hash"):
                self.assertIn(s, text)

    def test_live_waits_for_0945_then_publishes_once_and_beeps(self):
        mod = load_945()
        clock = [datetime(2026, 9, 30, 9, 40, 0, tzinfo=IST)]
        slept = []
        mod.now = lambda: clock[0]

        def fake_sleep(s):
            slept.append(s)
            clock[0] += timedelta(seconds=max(s, 1))
        mod.sleep = fake_sleep
        mod.fetch = lambda base: (make_payload(200, minutes=31, seed=3), make_index_payload(minutes=31))
        beeps = []
        mod.beep = lambda: beeps.append(clock[0])
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "psygrid_945.sqlite")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(mod.main(["--decide-only", "--data-dir", tmp]), 0)
            self.assertGreaterEqual(clock[0], datetime(2026, 9, 30, 9, 45, 3, tzinfo=IST))
            self.assertEqual(len(beeps), 1)
            first = Store(db).get_decision("2026-09-30", "live")
            # later run: different (later) feed -> stored decision is shown, never recomputed
            mod.fetch = lambda base: (make_payload(200, minutes=375, seed=4), None)
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                mod.main(["--decide-only", "--data-dir", tmp])
            self.assertIn("already published and immutable", out2.getvalue())
            self.assertEqual(Store(db).get_decision("2026-09-30", "live").decision_fingerprint,
                             first.decision_fingerprint)


    def test_live_refuses_a_previous_day_feed(self):
        mod = load_945()
        clock = [datetime(2026, 10, 1, 15, 20, 0, tzinfo=IST)]      # keeps retrying until 15:25, then gives up
        mod.now = lambda: clock[0]
        mod.sleep = lambda s: clock.__setitem__(0, clock[0] + timedelta(seconds=max(s, 1)))
        mod.fetch = lambda base: (make_payload(100, session=date(2026, 9, 30), minutes=31, seed=3), None)
        mod.beep = lambda: None
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "psygrid_945.sqlite")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mod.main(["--decide-only", "--data-dir", tmp]), 31)
            self.assertEqual(Store(db).decisions(), [])


class BenchmarkTests(unittest.TestCase):
    def test_989_stock_decision_is_fast_and_reproducible(self):
        blob = json.dumps(make_payload(989, minutes=30, seed=3))
        t = systime.perf_counter()
        a, *_ = decide_payload(json.loads(blob), make_index_payload(minutes=30))
        elapsed = systime.perf_counter() - t
        b, *_ = decide_payload(json.loads(blob), make_index_payload(minutes=30))
        self.assertLess(elapsed, 5.0)
        self.assertEqual(a.universe_size, 989)
        self.assertEqual(a.decision_fingerprint, b.decision_fingerprint)


if __name__ == "__main__":
    unittest.main()
