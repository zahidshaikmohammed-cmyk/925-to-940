"""945 continuous scan: cost model, signal confirmation, no look-ahead, trade management,
journal/restart, discipline limits and the CLI (live loop on a fake clock + replay)."""
import contextlib
import gzip
import importlib.util
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from intelligence.selector_config import load_weights
from intelligence.selector_data import IST, Series, parse_payload
from intelligence.selector_scan import (Event, Journal, ScanConfig, Scanner, Trade, render_day, render_signal,
                                        render_update, replay, round_trip_cost_pct, stats)
from intelligence.selector_synthetic import make_index_payload, make_payload

ROOT = Path(__file__).resolve().parents[1]
W = load_weights()
DAY = date(2026, 10, 1)
PLANTED = {"STK0010": 0.08, "STK0020": -0.08}
# Loose thresholds so the small synthetic universe produces signals to manage.
TEST_CFG = replace(ScanConfig(), tier1_score=75, stop_scanning=time(12, 0), last_entry=time(11, 45))


def at(h, m, s=0):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, tzinfo=IST)


def session(n=120, seed=5):
    return make_payload(n, session=DAY, minutes=375, seed=seed, planted=PLANTED), make_index_payload(DAY, 375)


def truncate(payload, index, end: datetime):
    stamp = end.strftime("%Y-%m-%d %H:%M")
    out = dict(payload)
    out["stocks"] = {}
    for sym, st in payload["stocks"].items():
        if isinstance(st, dict):
            st = dict(st)
            st["candles_1m"] = [r for r in st["candles_1m"]
                                if not isinstance(r, dict) or str(r.get("timestamp", ""))[:16] < stamp
                                or str(r.get("timestamp", ""))[:4] != "2026"]
        out["stocks"][sym] = st
    out["session"] = {"date": DAY.isoformat(), "status": "LIVE",
                      "current_time_ist": end.strftime("%Y-%m-%d %H:%M:%S IST")}
    return out, {"symbol": "NIFTY", "1m": [r for r in index["1m"] if r["timestamp"][:16] < stamp]}


def series(sym, start: datetime, rows):
    """rows: (o, h, l, c) per minute from `start`."""
    ts = tuple(start + timedelta(minutes=i) for i in range(len(rows)))
    o, h, l, c = (tuple(r[k] for r in rows) for k in range(4))
    return Series(sym, ts, o, h, l, c, tuple(1000.0 for _ in rows))


def pending_long(signal_at: datetime, **kw) -> Trade:
    base = dict(signal_id="x", day=DAY.isoformat(), number=1, symbol="AAA", direction="LONG", side=1, tier="TIER 1",
                score=90.0, rank=1, confirmed_scans=3, signal_time=signal_at.isoformat(), signal_bar="10:00",
                trigger=100.0, stop=99.0, target=102.0, risk=1.0, atr=1.0, cost_pct=0.14, net_rr=1.7,
                expires_at=(signal_at + timedelta(minutes=2)).isoformat(), next_bar=signal_at.isoformat())
    base.update(kw)
    return Trade(**base)


class CostTests(unittest.TestCase):
    def test_round_trip_cost_is_realistic_and_capped(self):
        cfg = ScanConfig()
        pct = round_trip_cost_pct(cfg)
        self.assertTrue(0.10 < pct < 0.20, pct)            # fees + STT + stamp + GST + slippage
        # brokerage is capped at Rs 20 an order, so a bigger order costs less in %
        self.assertLess(round_trip_cost_pct(replace(cfg, order_value=1_000_000)), pct)
        self.assertAlmostEqual(round_trip_cost_pct(replace(cfg, slippage_pct_per_side=0.0)), pct - 0.06, places=6)


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload, cls.index = session()
        cls.raw = parse_payload(cls.payload, cls.index)
        cls.events = []
        cls.trades = replay(cls.raw, Scanner(TEST_CFG, W), on_event=cls.events.append)

    def test_signals_need_confirmation_and_respect_the_window(self):
        self.assertTrue(self.trades, "the planted session must produce at least one signal")
        for t in self.trades:
            self.assertGreaterEqual(t.confirmed_scans, TEST_CFG.confirm_scans)
            self.assertGreaterEqual(t.score, TEST_CFG.tier1_score)
            sig = datetime.fromisoformat(t.signal_time)
            self.assertGreaterEqual(sig.time(), time(9, 32))    # 09:30 + two more confirming scans
            self.assertLess(sig.time(), TEST_CFG.last_entry)
            self.assertGreaterEqual(t.net_rr, TEST_CFG.min_net_rr)
            self.assertEqual(t.side * (t.target - t.trigger) > 0, True)
            self.assertEqual(t.side * (t.trigger - t.stop) > 0, True)

    def test_one_trade_at_a_time_and_no_repeats(self):
        live = [(datetime.fromisoformat(t.signal_time), datetime.fromisoformat(t.exit_time or t.expires_at))
                for t in self.trades]
        for (a0, a1), (b0, _) in zip(live, live[1:]):
            self.assertGreaterEqual(b0, a1)
        keys = [(t.symbol, t.direction) for t in self.trades]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertLessEqual(len(self.trades), TEST_CFG.max_signals_per_day)

    def test_no_look_ahead(self):
        """The first signal is identical when the feed physically ends at the signal minute."""
        first = self.trades[0]
        cut = datetime.fromisoformat(first.signal_time)
        p, i = truncate(self.payload, self.index, cut)
        sc = Scanner(TEST_CFG, W)
        raw = parse_payload(p, i)
        t = at(9, 30)
        got = None
        while t <= cut and got is None:
            events, _ = sc.step(raw, t)
            got = next((e.trade for e in events if e.kind == "SIGNAL"), None)
            t += timedelta(minutes=1)
        self.assertIsNotNone(got)
        for k in ("symbol", "direction", "signal_time", "trigger", "stop", "target", "score"):
            self.assertEqual(got[k], getattr(first, k), k)

    def test_results_are_net_of_costs(self):
        for t in self.trades:
            if t.state == "CLOSED":
                self.assertLess(t.net_r, t.gross_r)
        st = stats(self.trades)
        self.assertEqual(st["signals"], len(self.trades))
        self.assertIn("No signal today", render_day([]))
        out = render_signal(self.trades[0].to_dict())
        for word in ("ENTRY", "STOP LOSS", "TARGET", "COSTS", "not a win probability"):
            self.assertIn(word, out)
        out.encode("ascii")                                   # Windows consoles: plain ASCII only

    def test_noise_day_with_default_thresholds_stays_quiet(self):
        noise = parse_payload(make_payload(120, session=DAY, minutes=375, seed=9), make_index_payload(DAY, 375))
        trades = replay(noise, Scanner(replace(ScanConfig(), stop_scanning=time(12, 0)), W))
        self.assertEqual(trades, [])


class ManageTests(unittest.TestCase):
    def setUp(self):
        self.sc = Scanner(ScanConfig(), W)
        self.sig = at(10, 1)

    def run_bars(self, trade, rows, until=None):
        s = series("AAA", self.sig, rows)
        return self.sc._manage(trade, s, until or self.sig + timedelta(minutes=len(rows)))

    def test_fill_then_target(self):
        t = pending_long(self.sig)
        ev = self.run_bars(t, [(99.8, 100.2, 99.7, 100.1), (100.1, 101.0, 100.0, 100.9), (100.9, 102.3, 100.8, 102.1)])
        self.assertEqual([e.kind for e in ev], ["FILLED", "TARGET"])
        self.assertEqual((t.entry, t.exit, t.state), (100.0, 102.0, "CLOSED"))
        self.assertAlmostEqual(t.gross_r, 2.0)
        self.assertAlmostEqual(t.net_r, 2.0 - 0.14, places=3)

    def test_stop_counts_first_when_one_candle_touches_both(self):
        t = pending_long(self.sig)
        ev = self.run_bars(t, [(99.8, 100.3, 99.9, 100.2), (100.2, 102.5, 98.9, 100.0)])
        self.assertEqual([e.kind for e in ev], ["FILLED", "STOP"])
        self.assertEqual(t.exit, 99.0)

    def test_gap_through_stop_exits_at_the_open(self):
        t = pending_long(self.sig)
        self.run_bars(t, [(99.8, 100.3, 99.9, 100.2), (98.5, 98.8, 98.2, 98.6)])
        self.assertEqual((t.exit_reason, t.exit), ("STOP", 98.5))
        self.assertLess(t.net_r, -1.4)

    def test_unfilled_order_expires_after_two_candles(self):
        t = pending_long(self.sig)
        ev = self.run_bars(t, [(99.5, 99.9, 99.4, 99.8), (99.8, 99.95, 99.6, 99.7), (99.7, 100.5, 99.6, 100.4)])
        self.assertEqual([e.kind for e in ev], ["EXPIRED"])
        self.assertIsNone(t.entry)

    def test_time_stop(self):
        sc = Scanner(replace(ScanConfig(), max_hold_minutes=3), W)
        t = pending_long(self.sig)
        rows = [(99.9, 100.1, 99.8, 100.05)] + [(100.05, 100.3, 99.95, 100.2)] * 5
        ev = sc._manage(t, series("AAA", self.sig, rows), self.sig + timedelta(minutes=6))
        self.assertEqual(ev[-1].kind, "TIME_STOP")
        self.assertEqual(t.exit, 100.2)
        self.assertIn("TIME_STOP", render_update(ev[-1]))

    def test_square_off_at_1515(self):
        sig = at(15, 10)
        t = pending_long(sig)
        rows = [(99.9, 100.1, 99.8, 100.05)] + [(100.05, 100.3, 99.95, 100.2)] * 8
        ev = self.sc._manage(t, series("AAA", sig, rows), sig + timedelta(minutes=9))
        self.assertEqual(ev[-1].kind, "SQUARE_OFF")
        self.assertEqual(datetime.fromisoformat(t.exit_time).time(), time(15, 14))

    def test_short_side(self):
        t = pending_long(self.sig, direction="SHORT", side=-1, trigger=100.0, stop=101.0, target=98.0)
        ev = self.run_bars(t, [(100.2, 100.3, 99.9, 100.0), (99.9, 100.0, 97.8, 98.1)])
        self.assertEqual([e.kind for e in ev], ["FILLED", "TARGET"])
        self.assertAlmostEqual(t.gross_r, 2.0)


class JournalAndDisciplineTests(unittest.TestCase):
    def test_journal_restore_prevents_duplicate_signals(self):
        payload, index = session()
        raw = parse_payload(payload, index)
        with tempfile.TemporaryDirectory() as tmp:
            j = Journal(tmp)
            sc = Scanner(TEST_CFG, W)
            t = at(9, 30)
            first = None
            while first is None and t <= at(12, 0):
                events, _ = sc.step(raw, t)
                for e in events:
                    j.write(e)
                first = next((e for e in events if e.kind == "SIGNAL"), None)
                t += timedelta(minutes=1)
            self.assertIsNotNone(first)
            restored = j.trades(DAY.isoformat())
            self.assertEqual([x.signal_id for x in restored], [first.trade["signal_id"]])
            sc2 = Scanner(TEST_CFG, W)
            sc2.restore(DAY, restored)
            self.assertIsNotNone(sc2.active)
            events, _ = sc2.step(raw, t)
            self.assertNotIn("SIGNAL", [e.kind for e in events])        # still tracking, no repeat

    def test_loss_limit_locks_the_day(self):
        sc = Scanner(replace(TEST_CFG, max_losses_per_day=1), W)
        payload, index = session()
        raw = parse_payload(payload, index)
        sc.step(raw, at(9, 30))
        lost = pending_long(at(9, 31), state="CLOSED", net_r=-1.1, symbol="LOSER")
        sc.trades.append(lost)
        sc.locked = sc.losses() >= sc.cfg.max_losses_per_day
        _, summary = sc.step(raw, at(10, 0))
        self.assertIn("day locked", summary.status)

    def test_signal_limit(self):
        sc = Scanner(replace(TEST_CFG, max_signals_per_day=1), W)
        raw = parse_payload(*session())
        sc.step(raw, at(9, 30))
        sc.trades.append(pending_long(at(9, 31), state="EXPIRED"))
        _, summary = sc.step(raw, at(9, 45))
        self.assertIn("signal limit", summary.status)


class FakeFeed:
    def __init__(self, payload, index):
        self.payload, self.index = payload, index
        self.t = at(9, 25)

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=max(s, 0.5))

    def fetch(self, base):
        return truncate(self.payload, self.index, self.t)


class LiveCoreTests(unittest.TestCase):
    def test_default_feed_is_the_live_core_and_its_payload_scans(self):
        spec = importlib.util.spec_from_file_location("psygrid_945_url", ROOT / "945.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.BASE_URL, "http://129.225.112.47:10000")
        payload, index = session()
        p, _ = truncate(payload, index, at(9, 40))
        p.update({"schema_version": "4.0", "status": "OK", "universe_size": 989,
                  "coverage": {"complete": True, "expected_stock_count": 989}})
        raw = parse_payload(p, None)                       # Live Core serves no NIFTY file
        events, summary = Scanner(TEST_CFG, W).step(raw, at(9, 40))
        self.assertGreater(summary.eligible, 50)
        self.assertEqual(summary.market_source, "UNIVERSE_MEDIAN")
        closed = {"service": "PSYGRID", "schema_version": "4.0", "status": "CLOSED",
                  "session": {"status": "CLOSED", "date": None}, "stocks": {}}
        self.assertIsNone(mod.save_session(tempfile.mkdtemp(), DAY, closed, None))


class CliTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("psygrid_945_scan_cli", ROOT / "945.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def run_cli(self, mod, argv):
        if "--scan-replay" not in argv:
            argv = [*argv, "--no-setups"]          # these tests cover the 945 score scan alone
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod.main(argv)
        return code, buf.getvalue()

    def test_live_scan_beeps_records_and_resumes(self):
        mod = self.load()
        feed = FakeFeed(*session())
        beeps = []
        mod.now, mod.sleep, mod.fetch = feed.now, feed.sleep, feed.fetch
        mod.is_trading_day = lambda d: d.weekday() < 5
        mod.beep, mod.alert_beep = (lambda: beeps.append("update")), (lambda: beeps.append("SIGNAL"))
        short = replace(TEST_CFG, stop_scanning=time(10, 30))
        mod.scan_config = lambda args: replace(short, risk_rupees=args.risk_rupees)
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--no-archive", "--risk-rupees", "1000"])
            self.assertEqual(code, 0, out)
            self.assertIn("CONTINUOUS SCAN", out)
            self.assertIn("Waiting for 09:30", out)
            self.assertIn("[09:30]", out)
            self.assertIn("945 SCAN SIGNAL #1", out)
            self.assertIn("QUANTITY", out)
            self.assertIn("DAY SUMMARY", out)
            self.assertEqual(beeps[0], "SIGNAL")
            day_file = Path(tmp) / "scan" / f"{DAY.isoformat()}.jsonl"
            kinds = [json.loads(x)["kind"] for x in day_file.read_text().splitlines()]
            self.assertEqual(kinds[0], "SIGNAL")
            # every minute 09:30..10:30 scanned exactly once
            stamps = [line[1:6] for line in out.splitlines() if line.startswith("[") and "scanned" in line]
            self.assertEqual(len(stamps), len(set(stamps)))
            self.assertEqual((stamps[0], stamps[-1]), ("09:30", "10:30"))
            # restart later the same day: the journal is restored, nothing is repeated
            feed.t = at(10, 0)
            code, out2 = self.run_cli(mod, ["--data-dir", tmp, "--no-archive"])
            self.assertIn("Resumed today's journal", out2)
            kinds2 = [json.loads(x)["kind"] for x in day_file.read_text().splitlines()]
            self.assertEqual(kinds2.count("SIGNAL"), kinds.count("SIGNAL") + out2.count("945 SCAN SIGNAL"))
            sigs = [json.loads(x)["trade"]["symbol"] for x in day_file.read_text().splitlines()
                    if json.loads(x)["kind"] == "SIGNAL"]
            self.assertEqual(len(sigs), len(set(sigs)))

    def test_live_scan_saves_the_session_and_an_empty_feed_cannot_overwrite_it(self):
        mod = self.load()
        feed = FakeFeed(*session())
        mod.now, mod.sleep, mod.fetch = feed.now, feed.sleep, feed.fetch
        mod.is_trading_day = lambda d: True
        mod.beep = mod.alert_beep = lambda: None
        mod.scan_config = lambda args: replace(TEST_CFG, stop_scanning=time(9, 50))
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli(mod, ["--data-dir", tmp])
            self.assertEqual(code, 0, out)
            saved = Path(tmp) / "sessions" / f"{DAY.isoformat()}.json.gz"
            self.assertTrue(saved.exists(), out)
            self.assertIn("Session saved for replay", out)
            before = saved.read_bytes()
            from intelligence.selector_data import load_session_file
            raw = load_session_file(saved)
            self.assertEqual(max(s.ts[-1] for s in raw.stocks.values() if len(s)), at(9, 49))
            # after the close the feed is empty: nothing is overwritten
            empty = {"stocks": {}, "session": {"date": DAY.isoformat()}}
            self.assertIsNone(mod.save_session(tmp, DAY, empty, None))
            self.assertEqual(saved.read_bytes(), before)
            code, out = self.run_cli(mod, ["--scan-replay", str(saved)])
            self.assertEqual(code, 0, out)

    def test_stale_feed_is_never_scanned(self):
        mod = self.load()
        feed = FakeFeed(*session())
        frozen = truncate(feed.payload, feed.index, at(9, 20))
        mod.now, mod.sleep, mod.fetch = feed.now, feed.sleep, (lambda base: frozen)
        mod.is_trading_day = lambda d: True
        mod.beep = mod.alert_beep = lambda: None
        mod.scan_config = lambda args: replace(TEST_CFG, stop_scanning=time(9, 35))
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--no-archive"])
        self.assertEqual(code, 0)
        self.assertIn("FEED NOT READY", out)
        self.assertNotIn("scanned |", out)
        self.assertNotIn("SIGNAL #", out)

    def test_scan_replay_cli(self):
        mod = self.load()
        payload, index = session()
        mod.scan_config = lambda args: TEST_CFG
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / f"{DAY.isoformat()}.json.gz"
            f.write_bytes(gzip.compress(json.dumps({"stocks_payload": payload, "index_payload": index}).encode()))
            code, out = self.run_cli(mod, ["--scan-replay", tmp])
            self.assertEqual(code, 0, out)
            self.assertIn(f"REPLAY {DAY}", out)
            self.assertIn("REPLAY TOTAL over 1 session(s)", out)
            self.assertIn("too few to judge", out)
            code, out = self.run_cli(mod, ["--scan-replay", str(Path(tmp) / "missing")])
            self.assertEqual(code, 50)


if __name__ == "__main__":
    unittest.main()
