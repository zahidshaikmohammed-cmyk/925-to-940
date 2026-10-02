"""Production-hardening tests for 945: feed validation, automated lifecycle, restart
recovery, duplicate protection, database immutability, outcome isolation, malformed
feeds, reproducibility audit, research storage and leakage (look-ahead) audits."""
import contextlib
import copy
import gzip
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from intelligence import selector_backtest
from intelligence.selector_945 import decide
from intelligence.selector_config import SelectorConfig, load_weights, weights_hash
from intelligence.selector_data import IST, freeze_information_set, parse_payload
from intelligence.selector_feed import validate_feed
from intelligence.selector_features import build_feature_table
from intelligence.selector_lifecycle import Clock, Lifecycle
from intelligence.selector_outcomes import evaluate_decision
from intelligence.selector_research import calibration_metrics, research_report
from intelligence.selector_scoring import LinearEvidenceModel
from intelligence.selector_store import IMMUTABLE, DecisionExists, Store
from intelligence.selector_synthetic import make_index_payload, make_payload

ROOT = Path(__file__).resolve().parents[1]
W = load_weights()
DAY = date(2026, 10, 1)          # a Thursday, NSE trading day


def at(h, m, s=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=IST)


def truncate(payload, end: datetime, index=None):
    """The feed as it would look at wall-clock `end`: only candles that have started."""
    stamp = end.strftime("%Y-%m-%d %H:%M")
    out = dict(payload)
    out["stocks"] = {}
    for sym, st in payload["stocks"].items():
        if isinstance(st, dict):
            st = dict(st)
            st["candles_1m"] = [r for r in st["candles_1m"] if isinstance(r, dict) and str(r.get("timestamp", ""))[:16] < stamp
                                or not isinstance(r, dict) or str(r.get("timestamp", ""))[:4] != "2026"]
        out["stocks"][sym] = st
    out["session"] = {"date": end.date().isoformat(), "status": "LIVE",
                      "current_time_ist": end.strftime("%Y-%m-%d %H:%M:%S IST")}
    idx = None
    if index:
        idx = {"symbol": "NIFTY", "1m": [r for r in index["1m"] if r["timestamp"][:16] < stamp]}
    return out, idx


class FakeMarket:
    """A whole trading day; fetch() returns what PSYGRID would serve at the fake clock time."""

    def __init__(self, n=250, seed=5, day=DAY):
        self.full = make_payload(n, session=day, minutes=375, seed=seed)
        self.index = make_index_payload(session=day, minutes=375)
        self.t = at(9, 0, day=day)
        self.calls = 0
        self.fail_until = None
        self.override = None

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=max(s, 0.5))

    def fetch(self, base):
        self.calls += 1
        if self.override is not None:
            return self.override(self.t)
        if self.fail_until and self.t < self.fail_until:
            raise ConnectionError("simulated outage")
        return truncate(self.full, self.t, self.index)


def make_lifecycle(tmp, market, cfg=None, out=None, beeps=None, calibration_db=None):
    base = cfg or SelectorConfig()
    cfg = replace(base, db_path=str(Path(tmp) / "live.sqlite"), sessions_dir=str(Path(tmp) / "sessions"),
                  reports_dir=str(Path(tmp) / "reports"))
    return Lifecycle(cfg, W, cfg.db_path, "http://test", {}, "none", lambda d: d.weekday() < 5,
                     Clock(market.now, market.sleep), fetch=market.fetch,
                     beep=(lambda: beeps.append(market.t)) if beeps is not None else (lambda: None),
                     out=out or (lambda *a: None), calibration_db=calibration_db)


class FeedValidationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SelectorConfig()
        self.full = make_payload(200, session=DAY, minutes=375, seed=4)

    def health(self, now, payload=None):
        raw = parse_payload(payload or truncate(self.full, now)[0])
        return validate_feed(raw, freeze_information_set(raw, self.cfg.cutoff), now, self.cfg, lambda d: d.weekday() < 5)

    def test_good_feed_at_0945_passes(self):
        h = self.health(at(9, 45, 3))
        self.assertTrue(h.ok, h.failures())
        self.assertEqual(h.universe_count, 200)

    def test_previous_day_feed_rejected(self):
        y = make_payload(200, session=date(2026, 9, 30), minutes=375, seed=4)
        h = self.health(at(9, 45, 3), truncate(y, at(15, 30, day=date(2026, 9, 30)))[0])
        self.assertFalse(h.ok)
        self.assertIn("session_date", " ".join(h.failures()))

    def test_stale_feed_clock_rejected(self):
        payload, _ = truncate(self.full, at(9, 45, 3))
        payload["session"]["current_time_ist"] = "2026-10-01 09:30:00 IST"
        self.assertIn("feed_fresh", " ".join(self.health(at(9, 45, 3), payload).failures()))

    def test_missing_0944_history_rejected(self):
        payload, _ = truncate(self.full, at(9, 40))
        payload["session"]["current_time_ist"] = "2026-10-01 09:45:03 IST"
        self.assertIn("cutoff_history", " ".join(self.health(at(9, 45, 3), payload).failures()))

    def test_mostly_empty_universe_rejected(self):
        payload, _ = truncate(self.full, at(9, 45, 3))
        for i, st in enumerate(payload["stocks"].values()):
            if isinstance(st, dict) and i % 10:
                st["candles_1m"] = st["candles_1m"][:3]
        self.assertIn("valid_universe", " ".join(self.health(at(9, 45, 3), payload).failures()))

    def test_non_trading_day_rejected(self):
        sat = date(2026, 10, 3)
        full = make_payload(100, session=sat, minutes=60, seed=1)
        raw = parse_payload(truncate(full, at(9, 50, day=sat))[0])
        h = validate_feed(raw, freeze_information_set(raw), at(9, 50, day=sat), self.cfg, lambda d: d.weekday() < 5)
        self.assertIn("trading_day", " ".join(h.failures()))

    def test_after_close_full_session_accepted(self):
        payload, _ = truncate(self.full, at(15, 31))
        payload["session"]["current_time_ist"] = "2026-10-01 15:30:00 IST"
        self.assertTrue(self.health(at(18, 0), payload).ok)


class LifecycleTests(unittest.TestCase):
    def test_full_automatic_day(self):
        m = FakeMarket()
        beeps, printed = [], []
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_lifecycle(tmp, m, out=printed.append, beeps=beeps)
            self.assertEqual(lc.run_day(), 0)
            snap = lc.decision()
            self.assertIsNotNone(snap)
            self.assertEqual(len(beeps), 1)
            self.assertEqual(beeps[0].strftime("%H:%M:%S"), "09:45:03")
            self.assertEqual(snap.publication_lag_seconds, 3.0)
            outs = lc.store.outcomes(snap.decision_id)
            self.assertEqual(sorted(outs), [5, 15, 30])
            self.assertTrue(all(o["complete"] for o in outs.values()))
            self.assertTrue((Path(tmp) / "reports" / "2026-10-01.txt").exists())
            csv_text = (Path(tmp) / "reports" / "daily_summary.csv").read_text()
            self.assertIn(snap.selected_symbol, csv_text)
            self.assertTrue((Path(tmp) / "sessions" / "2026-10-01.json.gz").exists())
            self.assertTrue((Path(tmp) / "sessions" / "2026-10-01.0945-input.json.gz").exists())
            events = [e for _, e, _ in lc.store.events("2026-10-01")]
            for e in ("decision_published", "outcomes_recorded", "report_written", "session_archived", "lifecycle_complete"):
                self.assertIn(e, events)
            self.assertEqual(len(lc.store.decision_features(snap.decision_id)), 250)
            self.assertGreater(len(lc.store.decision_rankings(snap.decision_id)), 100)
            self.assertTrue(lc.store.universe_forward(snap.decision_id, 15))
            self.assertTrue(lc.store.ic_rows())
            report = (Path(tmp) / "reports" / "2026-10-01.txt").read_text()
            for k in ("SELECTED STOCK", "DIRECTION", "SCORE", "PROBABILITY STATUS", "EXPECTED HORIZON",
                      "+5M RESULT", "+15M RESULT", "+30M RESULT", "MFE", "MAE", "DATA QUALITY"):
                self.assertIn(k, report)
            ok, text = lc.verify("2026-10-01")
            self.assertTrue(ok, text)
            code, status = lc.status()
            self.assertEqual(code, 0, status)

    def test_restart_after_crash_resumes_without_duplicating(self):
        m = FakeMarket()
        with tempfile.TemporaryDirectory() as tmp:
            first = make_lifecycle(tmp, m).publish_decision()          # "crash" right after publishing
            m.t = at(10, 40)                                            # restarted much later
            beeps = []
            lc2 = make_lifecycle(tmp, m, beeps=beeps)
            self.assertEqual(lc2.decision().decision_fingerprint, first.decision_fingerprint)
            self.assertEqual(lc2.run_day(), 0)
            self.assertEqual(beeps, [])                                 # nothing republished
            self.assertEqual(len(lc2.store.decisions()), 1)
            self.assertEqual(sorted(lc2.store.outcomes(first.decision_id)), [5, 15, 30])

    def test_late_start_publishes_from_0945_information_only(self):
        early, late = FakeMarket(seed=8), FakeMarket(seed=8)
        late.t = at(11, 30)
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            s1 = make_lifecycle(a, early).publish_decision()
            s2 = make_lifecycle(b, late).publish_decision()
        self.assertEqual(s1.decision_fingerprint, s2.decision_fingerprint)
        self.assertGreater(s2.publication_lag_seconds, 6000)

    def test_feed_outage_then_recovery(self):
        m = FakeMarket()
        m.fail_until = at(9, 50)
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_lifecycle(tmp, m)
            snap = lc.publish_decision()
            self.assertIsNotNone(snap)
            self.assertGreaterEqual(datetime.fromisoformat(snap.published_at), at(9, 50))
            self.assertTrue(any(not ok for ok, in lc.store.db.execute("SELECT ok FROM feed_checks")))

    def test_stale_previous_day_feed_never_publishes(self):
        m = FakeMarket()
        y = make_payload(150, session=date(2026, 9, 30), minutes=375, seed=2)
        m.override = lambda t: truncate(y, at(15, 30, day=date(2026, 9, 30)))
        m.t = at(15, 20)
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_lifecycle(tmp, m)
            self.assertIsNone(lc.publish_decision())
            self.assertEqual(lc.store.decisions(), [])
            self.assertIn("decision_blocked", [e for _, e, _ in lc.store.events("2026-10-01")])

    def test_malformed_feeds_fail_safely(self):
        for bad in ("garbage", {"no_stocks": 1}, {"stocks": "x"}, {"stocks": {}}):
            m = FakeMarket()
            m.override = lambda t, bad=bad: (bad, None)
            m.t = at(15, 22)
            with tempfile.TemporaryDirectory() as tmp:
                lc = make_lifecycle(tmp, m)
                self.assertIsNone(lc.publish_decision())
                self.assertEqual(lc.store.decisions(), [])

    def test_concurrent_duplicate_session_protection(self):
        m = FakeMarket()
        with tempfile.TemporaryDirectory() as tmp:
            a = make_lifecycle(tmp, m).publish_decision()
            b = make_lifecycle(tmp, m).publish_decision()              # second process, same day
            self.assertEqual(a.decision_fingerprint, b.decision_fingerprint)
            self.assertEqual(len(Store(str(Path(tmp) / "live.sqlite")).decisions()), 1)

    def test_tampered_input_fails_verification(self):
        m = FakeMarket()
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_lifecycle(tmp, m)
            snap = lc.publish_decision()
            p = Path(lc.store.decision_input(snap.decision_id)["path"])
            p.write_bytes(p.read_bytes() + b"x")
            ok, text = lc.verify("2026-10-01")
            self.assertFalse(ok)
            self.assertIn("sha256", text)

    def test_non_trading_day_does_nothing(self):
        m = FakeMarket(day=date(2026, 10, 3))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(make_lifecycle(tmp, m).run_day(), 0)
            self.assertEqual(m.calls, 0)


class ImmutabilityTests(unittest.TestCase):
    def test_every_research_table_rejects_update_and_delete(self):
        m = FakeMarket(n=120)
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_lifecycle(tmp, m)
            self.assertEqual(lc.run_day(), 0)
            before = lc.decision().to_json()
            for table in IMMUTABLE:
                n = lc.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertGreater(n, 0, table)
                with self.assertRaises(sqlite3.DatabaseError, msg=table):
                    with lc.store.db:
                        lc.store.db.execute(f"UPDATE {table} SET rowid = rowid")
                with self.assertRaises(sqlite3.DatabaseError, msg=table):
                    with lc.store.db:
                        lc.store.db.execute(f"DELETE FROM {table}")
            self.assertEqual(lc.decision().to_json(), before)

    def test_outcome_recording_never_changes_the_decision(self):
        m = FakeMarket(n=120)
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_lifecycle(tmp, m)
            snap = lc.publish_decision()
            row = lc.store.db.execute("SELECT * FROM decisions").fetchone()
            feats = lc.store.decision_features(snap.decision_id)
            m.t = at(11, 0)
            lc.record_outcomes(snap)
            lc.record_outcomes(snap)                                   # idempotent
            self.assertEqual(lc.store.db.execute("SELECT * FROM decisions").fetchone(), row)
            self.assertEqual(lc.store.decision_features(snap.decision_id), feats)
            self.assertEqual(len(lc.store.outcomes(snap.decision_id)), 3)

    def test_incomplete_outcomes_are_not_stored(self):
        m = FakeMarket(n=120)
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_lifecycle(tmp, m)
            snap = lc.publish_decision()
            m.t = at(9, 55)                                            # only +5 is complete
            lc.record_outcomes(snap)
            self.assertEqual(sorted(lc.store.outcomes(snap.decision_id)), [5])

    def test_v1_database_is_refused_not_silently_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "old.sqlite"
            con = sqlite3.connect(p)
            con.execute("CREATE TABLE decisions (decision_id TEXT, decision_date TEXT)")
            con.commit()
            con.close()
            with self.assertRaises(RuntimeError):
                Store(p)


def _decide(payload, index=None, **kw):
    raw = parse_payload(payload, index)
    si = freeze_information_set(raw)
    return decide(si, SelectorConfig(), W, "replay", {}, {}, [], published_at=at(9, 45, 3), **kw)


class LeakageTests(unittest.TestCase):
    """Each test contaminates ONE kind of post-09:45 information and proves the 09:45
    decision is unchanged."""

    def setUp(self):
        self.full = make_payload(400, session=DAY, minutes=375, seed=31)
        self.index = make_index_payload(session=DAY, minutes=375)
        self.base, self.inner = _decide(self.full, self.index)

    def _mutate_future(self, fn):
        p = copy.deepcopy(self.full)
        for st in p["stocks"].values():
            if isinstance(st, dict):
                for r in st["candles_1m"]:
                    if isinstance(r, dict) and r.get("timestamp", "")[:16] >= "2026-10-01 09:45" \
                            and isinstance(r.get("open"), (int, float)):
                        fn(r)
        return p

    def assertSameDecision(self, snap):
        self.assertEqual(snap.decision_fingerprint, self.base.decision_fingerprint)
        self.assertEqual(snap.input_fingerprint, self.base.input_fingerprint)

    def test_future_volume_contamination(self):
        snap, _ = _decide(self._mutate_future(lambda r: r.update(volume=r["volume"] * 1000 + 7)), self.index)
        self.assertSameDecision(snap)

    def test_future_price_contamination(self):
        def spike(r):
            k = 3.0
            r.update(open=r["open"] * k, high=r["high"] * k, low=r["low"] * k, close=r["close"] * k)
        snap, _ = _decide(self._mutate_future(spike), self.index)
        self.assertSameDecision(snap)

    def test_future_nifty_contamination(self):
        idx = copy.deepcopy(self.index)
        for r in idx["1m"][30:]:
            r.update(open=r["open"] * 1.05, high=r["high"] * 1.06, low=r["low"] * 1.04, close=r["close"] * 1.05)
        snap, _ = _decide(self.full, idx)
        self.assertSameDecision(snap)
        self.assertEqual(snap.to_dict()["market_context"]["market_source"], "NIFTY")

    def test_future_cross_sectional_normalization(self):
        p = copy.deepcopy(self.full)
        extra = make_payload(300, session=DAY, minutes=375, seed=99)["stocks"]
        added = sum(1 for st in extra.values() if isinstance(st, dict))
        for sym, st in extra.items():
            if isinstance(st, dict):
                st["candles_1m"] = [r for r in st["candles_1m"] if r["timestamp"][:16] >= "2026-10-01 09:45"]
                p["stocks"]["LATE" + sym] = st                          # stocks that only exist after 09:45
        snap, inner = _decide(p, self.index)
        self.assertEqual((snap.selected_symbol, snap.direction, snap.selection_score, snap.raw_score),
                         (self.base.selected_symbol, self.base.direction, self.base.selection_score, self.base.raw_score))
        self.assertEqual(inner["table"].eligible, self.inner["table"].eligible)
        self.assertEqual([(c.symbol, c.direction, c.raw) for c in inner["ranked"]],
                         [(c.symbol, c.direction, c.raw) for c in self.inner["ranked"]])
        self.assertEqual(snap.to_dict()["exclusion_reasons"].get("NO_PRE_CUTOFF_CANDLES"), added)

    def _sessions(self, tmp, days):
        d = Path(tmp) / "s"
        d.mkdir(exist_ok=True)
        for k, day in enumerate(days):
            blob = {"stocks_payload": make_payload(250, session=day, minutes=60, seed=40 + k),
                    "index_payload": make_index_payload(session=day, minutes=60, seed=k)}
            (d / f"{day}.json.gz").write_bytes(gzip.compress(json.dumps(blob).encode()))
        return d

    def test_future_sessions_never_change_earlier_backtest_decisions(self):
        """Covers future volatility/volume baselines and future calibration history."""
        days = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25),
                date(2026, 9, 28)]
        cfg = replace(SelectorConfig(), min_calibration_obs=2, baseline_min_days=1)
        with tempfile.TemporaryDirectory() as tmp:
            src = self._sessions(tmp, days)
            only_first = Path(tmp) / "first"
            only_first.mkdir()
            for day in days[:3]:
                (only_first / f"{day}.json.gz").write_bytes((src / f"{day}.json.gz").read_bytes())
            selector_backtest.run_backtest([str(only_first)], str(Path(tmp) / "a.sqlite"), cfg, W, {}, log=lambda *_: None)
            selector_backtest.run_backtest([str(src)], str(Path(tmp) / "b.sqlite"), cfg, W, {}, log=lambda *_: None)
            a = {d["decision_date"]: d for d in Store(Path(tmp) / "a.sqlite").decisions()}
            b = {d["decision_date"]: d for d in Store(Path(tmp) / "b.sqlite").decisions()}
            for day in a:
                self.assertEqual(a[day]["decision_fingerprint"], b[day]["decision_fingerprint"], day)
            # the later days really did use earlier-only calibration (empirical once >= 2 obs)
            self.assertTrue(b["2026-09-28"]["probability_status"].startswith("EMPIRICAL"))
            self.assertEqual(b["2026-09-28"]["feature_snapshot"]["relative_volume_source"], "PRIOR_SESSIONS_MEDIAN")

    def test_model_fit_only_sees_earlier_sessions(self):
        seen = []

        class SpyModel(LinearEvidenceModel):
            model_id = "945-SPY"

            def fit(self, history):
                seen.append([d["decision_date"] for d, _ in history])
                return self

        days = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)]
        with tempfile.TemporaryDirectory() as tmp:
            src = self._sessions(tmp, days)
            res = selector_backtest.run_backtest([str(src)], str(Path(tmp) / "c.sqlite"), SelectorConfig(), W, {},
                                                 log=lambda *_: None, model_factory=lambda: SpyModel(W))
        self.assertEqual(len(res), 3)
        self.assertEqual(len(seen), 3)                                  # fit once per day, in date order
        for hist, day in zip(seen, [d.isoformat() for d in days]):
            self.assertTrue(all(h < day for h in hist), (hist, day))
        self.assertEqual([len(h) for h in seen], [0, 1, 2])

    def test_weights_are_fixed_and_hashed(self):
        before = weights_hash(load_weights())
        with tempfile.TemporaryDirectory() as tmp:
            src = self._sessions(tmp, [date(2026, 9, 21), date(2026, 9, 22)])
            selector_backtest.run_backtest([str(src)], str(Path(tmp) / "d.sqlite"), SelectorConfig(), W, {},
                                           log=lambda *_: None)
            stored = {d["weights_hash"] for d in Store(Path(tmp) / "d.sqlite").decisions()}
        self.assertEqual(stored, {before})
        self.assertEqual(weights_hash(load_weights()), before)

    def test_calibration_history_excludes_same_and_later_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._sessions(tmp, [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)])
            db = str(Path(tmp) / "e.sqlite")
            selector_backtest.run_backtest([str(src)], db, SelectorConfig(), W, {}, log=lambda *_: None)
            st = Store(db)
            self.assertEqual([d["decision_date"] for d, _ in st.history(before="2026-09-22")], ["2026-09-21"])


class ResearchTests(unittest.TestCase):
    def test_calibration_metrics(self):
        m = calibration_metrics([(0.8, True)] * 8 + [(0.8, False)] * 2 + [(0.3, False)] * 10)
        self.assertEqual(m["n"], 20)
        self.assertAlmostEqual(m["ece"], 0.15, places=6)
        self.assertLess(m["brier"], m["brier_baseline"] + 0.25)

    def test_research_report_on_live_store(self):
        m = FakeMarket(n=150)
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_lifecycle(tmp, m)
            lc.run_day()
            text = research_report(lc.store, lc.cfg, "live")
        for k in ("HORIZON +5", "HORIZON +15", "HORIZON +30", "SELECTION CONCENTRATION", "RANK STABILITY",
                  "SCORE vs OUTCOME", "FEATURE VALIDATION", "_model_net_score", "PROBABILITY CALIBRATION",
                  "NOT PROVEN"):
            self.assertIn(k, text)

    def test_sector_coverage_is_explicit(self):
        p = make_payload(200, session=DAY, minutes=30, seed=3)
        sectors = {f"STK{i:04d}": ("A" if i % 2 else "B") for i in range(60)}
        raw = parse_payload(p)
        snap, inner = decide(freeze_information_set(raw), SelectorConfig(), W, "replay", sectors, {}, [],
                             sector_source="test-map")
        sc = snap.to_dict()["sector_coverage"]
        self.assertEqual((sc["source"], sc["universe_classified"], sc["universe"]), ("test-map", 60, 200))
        unclassified = [s for s in inner["table"].eligible if s not in sectors]
        self.assertTrue(unclassified)
        self.assertTrue(all(inner["table"].features[s]["sector_rel"] is None for s in unclassified))


class CliTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("psygrid_945_cli", ROOT / "945.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_decide_only_status_verify_research_cli(self):
        mod = self.load()
        m = FakeMarket(n=150)
        m.t = at(9, 40)
        mod.now, mod.sleep, mod.fetch = m.now, m.sleep, m.fetch
        mod.beep = lambda: None
        mod.is_trading_day = lambda d: d.weekday() < 5
        with tempfile.TemporaryDirectory() as tmp:
            run = lambda *a: self._run(mod, ["--data-dir", tmp, *a])
            code, out = run("--decide-only")
            self.assertEqual(code, 0)
            self.assertIn("PSYGRID 945 -- 09:45 INTRADAY SELECTION", out)
            self.assertIn("FEED", out)
            code, out = run("--decide-only")
            self.assertIn("already published and immutable", out)
            m.t = at(10, 30)
            code, out = run("--evaluate")
            self.assertIn("3 new outcome horizon(s) recorded", out)
            code, out = run("--status")
            self.assertEqual(code, 0, out)
            code, out = run("--verify")
            self.assertEqual(code, 0, out)
            self.assertIn("VERIFIED", out)
            code, out = run("--research")
            self.assertIn("LIVE RESEARCH REPORT", out)

    def _run(self, mod, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod.main(argv)
        return code, buf.getvalue()


if __name__ == "__main__":
    unittest.main()
