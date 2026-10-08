"""Research setups: Yahoo history parsing and bootstrap, causal baselines, 5-minute
aggregation, ORB/FHM/VWT arming and management, no look-ahead on a random walk,
live == backtest arming, and the CLI (--bootstrap, --setup-backtest, live scan)."""
import contextlib
import importlib.util
import io
import json
import random
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from intelligence.history import (IST, HistoryIndex, bootstrap, load_history, parse_chart, save_history)
from intelligence.selector_data import parse_payload
from intelligence.setups import (LiveSetups, SetupConfig, SetupEngine, SetupJournal, backtest, build_day,
                                 render_trigger, setup_stats, to_five_minute)
from intelligence.selector_synthetic import make_payload

ROOT = Path(__file__).resolve().parents[1]
COST = 0.142


def trading_days(n, end=date(2026, 10, 1)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def at(day, h, m):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def flat_day(day, price, vol, rnd, sigma=0.002, first_mult=1.0, drift=0.0):
    bars, p = [], price
    for k in range(75):
        o = p
        c = o * (1 + drift + rnd.gauss(0, sigma))
        h = max(o, c) * (1 + abs(rnd.gauss(0, sigma / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, sigma / 2)))
        v = vol * (first_mult if k == 0 else 1.0) * rnd.uniform(0.8, 1.2)
        bars.append((at(day, 9, 15) + timedelta(minutes=5 * k), o, h, l, c, v))
        p = c
    return bars


def random_history(nsym=60, ndays=16, seed=3, sigma=0.002):
    rnd = random.Random(seed)
    hist = {}
    for i in range(nsym):
        p = rnd.uniform(100, 1500)
        rows = []
        for d in trading_days(ndays):
            day = flat_day(d, p, 50_000, rnd, sigma)
            rows += day
            p = day[-1][4]
        hist[f"S{i:03d}"] = rows
    return hist


class HistoryTests(unittest.TestCase):
    def test_parse_chart_keeps_session_bars_in_ist(self):
        base = int(datetime(2026, 10, 1, 9, 15, tzinfo=IST).timestamp())
        payload = {"chart": {"result": [{"timestamp": [base - 300, base, base + 300, base + 600],
                                         "indicators": {"quote": [{"open": [1, 100, None, 101], "high": [1, 101, 102, 102],
                                                                   "low": [1, 99, 100, 100], "close": [1, 100.5, 101, 101.5],
                                                                   "volume": [1, 5000, 6000, 7000]}]}}]}}
        bars = parse_chart(payload)
        self.assertEqual([b[0].strftime("%H:%M") for b in bars], ["09:15", "09:25"])   # pre-open + null row dropped
        self.assertEqual(bars[0][1:], (100.0, 101.0, 99.0, 100.5, 5000.0))
        self.assertEqual(parse_chart({}), [])

    def test_bootstrap_saves_and_reports_missing_symbols(self):
        hist = random_history(3, 2)
        def fake(sym):
            if sym == "BAD":
                raise RuntimeError("404")
            return hist.get(sym, [])
        with tempfile.TemporaryDirectory() as tmp:
            path = bootstrap(["S000", "S001", "BAD", "EMPTY"], tmp, fetch=fake, workers=2, say=lambda *_: None)
            back = load_history(path)
            self.assertEqual(set(back), {"S000", "S001"})
            self.assertEqual(back["S000"][0], hist["S000"][0])
            meta = json.loads(__import__("gzip").decompress(path.read_bytes()))
            self.assertEqual(set(meta["failed"]), {"BAD", "EMPTY"})

    def test_baselines_use_only_earlier_days(self):
        hist = random_history(5, 16)
        days = HistoryIndex(hist).days()
        d = days[-1]
        before = HistoryIndex(hist).baselines(d)
        tampered = {s: [(b[0], b[1], b[2] * 3, b[3], b[4] * 3, b[5] * 100) if b[0].date() >= d else b for b in rows]
                    for s, rows in hist.items()}
        self.assertEqual(before, HistoryIndex(tampered).baselines(d))
        b = before["S000"]
        firsts = sorted(r[5] for r in hist["S000"] if r[0].time() == time(9, 15) and r[0].date() < d)[-14:]
        self.assertIsNotNone(b.rvol_base)
        self.assertEqual(b.days, 14)
        self.assertIsNotNone(b.atr_daily)
        self.assertIsNotNone(b.fh_sd)
        self.assertGreater(b.rvol_base, 0)
        self.assertLessEqual(min(firsts), b.rvol_base)


class BarTests(unittest.TestCase):
    def test_five_minute_aggregation_only_complete_bars(self):
        d = date(2026, 10, 1)
        one = [(at(d, 9, 15) + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(12)]
        five = to_five_minute(one, at(d, 9, 27))
        self.assertEqual(len(five), 2)                       # 09:25 bucket not complete at 09:27
        self.assertEqual(five[0], (at(d, 9, 15), 100, 105, 99, 104.5, 50))
        self.assertEqual(len(to_five_minute(one, at(d, 9, 30))), 3)   # a bucket with missing minutes still closes
        b = build_day("X", five)
        self.assertEqual(len(b.vwap), 2)
        self.assertAlmostEqual(b.vwap[0], (105 + 99 + 104.5) / 3)
        self.assertEqual(b.done(at(d, 9, 24)), 1)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.hist = random_history(40, 16)
        self.index = HistoryIndex(self.hist)
        self.day = self.index.days()[-1]
        self.cfg = replace(SetupConfig(), regime_long=1.1, regime_short=-0.1)     # regime always MIXED

    def day_bars(self, overrides=None):
        rows = {s: self.index.bars_on(s, self.day) for s in self.index.daily_bars}
        rows.update(overrides or {})
        return {s: build_day(s, r) for s, r in rows.items()}

    def test_orb_arms_the_stock_in_play_and_holds_to_square_off(self):
        rnd = random.Random(1)
        base = self.index.baselines(self.day)
        p = base["S005"].prev_close
        planted = flat_day(self.day, p, 50_000, rnd, sigma=0.001, first_mult=6.0, drift=0.0015)
        o, h, l, c, v = planted[0][1:]
        planted[0] = (planted[0][0], o, o * 1.006, o * 0.999, o * 1.005, v)    # strong green opening bar
        bars = self.day_bars({"S005": planted})
        eng = SetupEngine(self.cfg, COST)
        events = []
        for k in range(1, 76):
            events += eng.step(self.day, bars, base, at(self.day, 9, 15) + timedelta(minutes=5 * k))
        orb = [t for t in eng.trades if t.setup == "ORB" and t.symbol == "S005"]
        self.assertEqual(len(orb), 1)
        t = orb[0]
        self.assertEqual((t.direction, t.trigger, t.stop), ("LONG", round(o * 1.006, 2), round(o * 0.999, 2)))
        self.assertIn("RVOL", t.facts[0])
        self.assertEqual(t.state, "CLOSED")
        self.assertEqual(t.exit_reason, "SQUARE_OFF")
        self.assertGreater(t.net_r, 0)
        self.assertLess(t.net_r, t.gross_r)
        kinds = [e.kind for e in events if e.trade["symbol"] == "S005" and e.trade["setup"] == "ORB"]
        self.assertEqual(kinds, ["ARMED", "TRIGGERED", "SQUARE_OFF"])
        text = render_trigger(next(e.trade for e in events if e.kind == "TRIGGERED" and e.trade["symbol"] == "S005"),
                              {"ORB": {"status": "ACTIVE", "trades": 40, "win_rate": 0.3, "avg_net_r": 0.2}}, 1000)
        for word in ("STOCKS-IN-PLAY", "WHY THIS SETUP", "TRACK RECORD", "ACTIVE: 40 backtest trades", "QUANTITY"):
            self.assertIn(word, text)
        text.encode("ascii")

    def test_stop_first_and_expiry(self):
        base = self.index.baselines(self.day)
        eng = SetupEngine(self.cfg, COST)
        eng.reset(self.day)
        now = at(self.day, 10, 0)
        eng._arm("ORB", "S001", 1, 100.0, 99.0, now, now + timedelta(minutes=10), [])
        eng._arm("ORB", "S002", 1, 100.0, 99.0, now, now + timedelta(minutes=10), [])
        fine = {"S001": [(now, 99.9, 100.2, 99.95, 100.1, 1), (now + timedelta(minutes=1), 100.1, 102.0, 98.5, 100, 1)],
                "S002": [(now + timedelta(minutes=i), 99.5, 99.8, 99.4, 99.6, 1) for i in range(12)]}
        events = eng.step(self.day, {}, base, now + timedelta(minutes=12), fine)
        kinds = {(e.trade["symbol"], e.kind) for e in events}
        self.assertIn(("S001", "STOP"), kinds)
        self.assertIn(("S002", "EXPIRED"), kinds)
        s1 = next(t for t in eng.trades if t.symbol == "S001")
        self.assertEqual(s1.exit, 99.0)

    def test_costs_gate_tiny_stops(self):
        eng = SetupEngine(self.cfg, COST)
        now = at(self.day, 10, 0)
        self.assertIsNone(eng._arm("ORB", "S001", 1, 1000.0, 999.9, now, now, []))      # 0.01% risk
        self.assertIsNotNone(eng._arm("ORB", "S002", 1, 1000.0, 990.0, now, now, []))

    def test_paytm_2026_10_08_is_never_signalled(self):
        """Late start at 09:26: PAYTM SHORT, trigger 1582, stop 1656, previous close 1732, price 1565."""
        from intelligence.setups import EMPTY_BASE
        eng = SetupEngine(self.cfg, COST)
        eng.reset(self.day)
        now = at(self.day, 9, 26)
        base = {"PAYTM": replace(EMPTY_BASE, prev_close=1732.0), "OK": replace(EMPTY_BASE, prev_close=100.0)}
        eng._ctx = ({}, base, {}, {"PAYTM": [(now - timedelta(minutes=1), 1570, 1572, 1564, 1565.1, 1)],
                                   "OK": [(now - timedelta(minutes=1), 100.5, 100.6, 100.4, 100.5, 1)]})
        self.assertIsNone(eng._arm("ORB", "PAYTM", -1, 1582.0, 1656.0, now, now, []))   # stop 4.7% away
        self.assertIsNone(eng._arm("ORB", "PAYTM", -1, 1582.0, 1600.0, now, now, []))   # 8.7% down already
        base["PAYTM"] = replace(EMPTY_BASE, prev_close=1640.0)
        self.assertIsNone(eng._arm("ORB", "PAYTM", -1, 1582.0, 1600.0, now, now, []))   # break already happened
        self.assertIsNotNone(eng._arm("ORB", "OK", -1, 100.0, 101.0, now, now + timedelta(minutes=30), []))

    def test_a_bar_opening_well_past_the_trigger_is_a_missed_trade(self):
        eng = SetupEngine(self.cfg, COST)
        eng.reset(self.day)
        now = at(self.day, 10, 0)
        eng._arm("ORB", "S001", -1, 100.0, 101.0, now, now + timedelta(minutes=30), [])
        fine = {"S001": [(now, 99.0, 99.2, 98.8, 99.0, 1)]}                           # opened 1% below
        events = eng.step(self.day, {}, {}, now + timedelta(minutes=2), fine)
        self.assertEqual([(e.kind, e.trade["exit_reason"]) for e in events], [("EXPIRED", "MISSED")])
        self.assertIsNone(eng.trades[0].entry)

    def test_optional_r_target_books_the_profit(self):
        from intelligence.setups import EMPTY_BASE
        for target_r, expect in ((None, None), (2.0, ("TARGET", 98.0))):
            eng = SetupEngine(replace(self.cfg, target_r=target_r), COST)
            eng.reset(self.day)
            now = at(self.day, 10, 0)
            eng._ctx = ({}, {"S001": replace(EMPTY_BASE, prev_close=101.0)}, {},
                        {"S001": [(now - timedelta(minutes=1), 100.5, 100.6, 100.4, 100.5, 1)]})
            eng._arm("ORB", "S001", -1, 100.0, 101.0, now, now + timedelta(minutes=30), [])
            fine = {"S001": [(now, 100.1, 100.2, 99.9, 100.0, 1),                 # sell stop 100 fills
                             (now + timedelta(minutes=1), 99.5, 99.6, 97.5, 98.2, 1)]}   # through 98 = 2R
            events = eng.step(self.day, {}, {}, now + timedelta(minutes=3), fine)
            closed = [(e.kind, e.trade["exit"]) for e in events if e.kind not in ("ARMED", "TRIGGERED")]
            self.assertEqual(closed, [expect] if expect else [])

    def test_position_size_for_a_small_account(self):
        from intelligence.setups import position_size, quantity_line
        self.assertEqual(position_size(250.30, 2.06, 0.0, 25), 12)                  # BUILDPRO
        self.assertEqual(position_size(80.47, 0.42, 0.0, 25, 5000), 59)            # INA
        self.assertEqual(position_size(80.47, 0.05, 0.0, 25, 5000), 62)            # capped by Rs 5,000
        self.assertEqual(position_size(1565.10, 90.90, 0.1424, 25, 5000), 0)       # PAYTM: skip
        self.assertIn("SKIP", quantity_line(0, 1565.1, 90.9, 25))
        self.assertIn("12 shares", quantity_line(12, 250.3, 2.06, 25))

    def test_vwap_trend_pullback_arms_and_exits_on_vwap_close(self):
        base = self.index.baselines(self.day)
        p = base["S007"].prev_close
        rows, price = [], p
        for k in range(75):
            t = at(self.day, 9, 15) + timedelta(minutes=5 * k)
            if k < 20:
                o, c = price, price * 1.002                      # steady climb above VWAP
                v = 60_000
            elif k == 20:
                o, c = price, price * 0.999                      # pullback towards VWAP, light volume
                v = 20_000
            elif k < 30:
                o, c = price, price * 1.002
                v = 60_000
            else:
                o, c = price, price * 0.985 if k == 30 else price * 0.999   # breaks back through VWAP
                v = 60_000
            h, l = max(o, c) * 1.0005, min(o, c) * 0.9995
            if k == 20:
                l = rows[-1][4] * 0.985                          # wick down to VWAP
            rows.append((t, o, h, l, c, v))
            price = c
        bars = self.day_bars({"S007": rows})
        eng = SetupEngine(replace(self.cfg, vwt_pull_atr=50.0, vwt_top=1000, vwt_max_day=1000), COST)
        for k in range(1, 76):
            eng.step(self.day, bars, base, at(self.day, 9, 15) + timedelta(minutes=5 * k))
        vwt = [t for t in eng.trades if t.setup == "VWT" and t.symbol == "S007"]
        self.assertEqual(len(vwt), 1, [t.symbol for t in eng.trades if t.setup == "VWT"])
        t = vwt[0]
        self.assertEqual(t.direction, "LONG")
        self.assertEqual(t.armed_at, at(self.day, 11, 0).isoformat())    # the pullback bar (10:55) completes at 11:00
        self.assertEqual(t.exit_reason, "EXIT_VWAP")

    def test_random_walk_has_no_edge_after_costs(self):
        hist = random_history(80, 16, seed=11)
        trades = backtest(HistoryIndex(hist), SetupConfig(), COST, min_prior=10)
        stats = setup_stats(trades, min_trades=1)
        closed = [t for t in trades if t.state == "CLOSED"]
        self.assertTrue(closed)
        self.assertLess(sum(t.net_r for t in closed) / len(closed), 0.05)
        for s in stats.values():
            if s["trades"]:
                self.assertNotEqual(s["status"], "ACTIVE")

    def test_live_one_minute_feed_arms_like_the_backtest(self):
        """Same day as 1-minute bars (live) and 5-minute bars (backtest): same ORB arms."""
        rnd = random.Random(4)
        base = self.index.baselines(self.day)
        one_min = {}
        for s in list(self.index.daily_bars)[:25]:
            p, rows = base[s].prev_close, []
            mult = 8.0 if s in ("S003", "S004") else 1.0
            for i in range(375):
                o = p
                c = o * (1 + (0.001 if s == "S003" else -0.001 if s == "S004" else 0) + rnd.gauss(0, 0.0008))
                v = 10_000 * (mult if i < 5 else 1.0)
                rows.append((at(self.day, 9, 15) + timedelta(minutes=i), o, max(o, c) * 1.0002, min(o, c) * 0.9998, c, v))
                p = c
            one_min[s] = rows
        end = at(self.day, 15, 30)
        bt = SetupEngine(self.cfg, COST)
        five = {s: build_day(s, to_five_minute(r, end)) for s, r in one_min.items()}
        for k in range(1, 76):
            bt.step(self.day, five, base, at(self.day, 9, 15) + timedelta(minutes=5 * k))
        from intelligence.selector_data import Series
        with tempfile.TemporaryDirectory() as tmp:
            live = LiveSetups(SetupEngine(self.cfg, COST), base, {}, SetupJournal(tmp), self.day)
            for m in range(5, 375):
                cut = at(self.day, 9, 15) + timedelta(minutes=m)
                series = {s: Series(s, *zip(*[r for r in rows if r[0] < cut])) for s, rows in one_min.items()}
                live.on_scan(series, cut)
            self.assertTrue((Path(tmp) / f"{self.day.isoformat()}.jsonl").exists())
        key = lambda e: sorted((t.setup, t.symbol, t.direction, t.trigger, t.stop) for t in e.trades if t.setup == "ORB")
        self.assertEqual(key(bt), key(live.engine))
        self.assertTrue(key(bt))


class CliTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("psygrid_945_setups_cli", ROOT / "945.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def run_cli(self, mod, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod.main(argv)
        return code, buf.getvalue()

    def test_bootstrap_and_setup_backtest(self):
        mod = self.load()
        hist = random_history(40, 14)
        mod.fetch = lambda base: ({"stocks": {s: {} for s in hist}}, None)
        mod.bootstrap = lambda syms, out, say=print: (save_history(Path(out) / "yahoo_5m.json.gz",
                                                                   {s: hist[s] for s in syms}, {}),
                                                      Path(out) / "yahoo_5m.json.gz")[1]
        mod.probe_yahoo = lambda: True
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--setup-backtest"])
            self.assertEqual(code, 50)
            self.assertIn("needs 11+", out)
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--bootstrap"])
            self.assertEqual(code, 0, out)
            self.assertIn("40 symbols", out)
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--setup-backtest"])
            self.assertEqual(code, 0, out)
            self.assertIn("SETUP BACKTEST", out)
            stats = json.loads((Path(tmp) / "history" / "setup_stats.json").read_text())["setups"]
            self.assertEqual(set(stats), {"ORB", "FHM", "VWT"})
            self.assertTrue((Path(tmp) / "history" / "setup_trades.csv").exists())

    def test_live_scan_runs_the_setups(self):
        mod = self.load()
        from tests.test_945_scan import FakeFeed, TEST_CFG, truncate
        day = date(2026, 10, 1)
        payload = make_payload(80, session=day, minutes=375, seed=5)
        from intelligence.selector_synthetic import make_index_payload
        feed = FakeFeed(payload, make_index_payload(day, 375))
        feed.t = at(day, 9, 18)
        # history: 14 earlier sessions per symbol with a much smaller opening volume
        rnd = random.Random(2)
        hist = {}
        raw = parse_payload(payload)
        for sym, s in raw.stocks.items():
            if len(s) < 30:
                continue
            rows = []
            for d in trading_days(15, day - timedelta(days=1)):
                rows += flat_day(d, s.c[0], sum(s.v[5:10]) / 2, rnd)
            hist[sym] = rows
        mod.now, mod.sleep, mod.fetch = feed.now, feed.sleep, feed.fetch
        mod.is_trading_day = lambda d: d.weekday() < 5
        beeps = []
        mod.beep, mod.alert_beep = (lambda: beeps.append("b")), (lambda: beeps.append("B"))
        mod.scan_config = lambda args: replace(TEST_CFG, stop_scanning=time(9, 40))
        mod.bootstrap = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download in tests"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "history").mkdir()
            save_history(Path(tmp) / "history" / "yahoo_5m.json.gz", hist, {})
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--no-archive"])
            self.assertEqual(code, 0, out)
            self.assertIn("Setups      :", out)
            self.assertIn("setups watching the open", out)
            self.assertIn("[09:30]", out)
            self.assertIn("SETUPS TODAY", out)
            self.assertTrue((Path(tmp) / "universe.json").exists())
            j = Path(tmp) / "setups" / f"{day.isoformat()}.jsonl"
            if j.exists():
                self.assertIn("ARMED", out)

    def test_no_setups_flag_and_missing_history(self):
        mod = self.load()
        from tests.test_945_scan import FakeFeed, TEST_CFG, session
        feed = FakeFeed(*session())
        feed.t = at(date(2026, 10, 1), 9, 26)
        mod.now, mod.sleep, mod.fetch = feed.now, feed.sleep, feed.fetch
        mod.is_trading_day = lambda d: True
        mod.beep = mod.alert_beep = lambda: None
        mod.probe_yahoo = lambda: False
        mod.scan_config = lambda args: replace(TEST_CFG, stop_scanning=time(9, 31))
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--no-archive"])
            self.assertIn("DAY-ONE MODE", out)
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--no-archive", "--no-setups"])
            self.assertNotIn("SETUPS", out)


if __name__ == "__main__":
    unittest.main()


class EndToEndAuditTests(unittest.TestCase):
    """Run the live scan with setups on a simulated morning, then --audit the saved day:
    the replay must find exactly the setups the live run armed, at the same levels."""

    def test_live_run_and_audit_agree(self):
        from tests.test_945_scan import FakeFeed, TEST_CFG
        from intelligence.selector_synthetic import make_index_payload
        spec = importlib.util.spec_from_file_location("psygrid_945_e2e", ROOT / "945.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        day = date(2026, 10, 1)
        payload = make_payload(90, session=day, minutes=375, seed=5, planted={"STK0010": 0.06, "STK0020": -0.06})
        feed = FakeFeed(payload, make_index_payload(day, 375))
        feed.t = at(day, 9, 18)
        rnd = random.Random(2)
        hist = {}
        for sym, s in parse_payload(payload).stocks.items():
            if len(s) >= 30:
                rows = []
                for d in trading_days(15, day - timedelta(days=1)):
                    rows += flat_day(d, s.c[0], sum(s.v[20:25]) * 0.8, rnd)
                hist[sym] = rows
        mod.now, mod.sleep, mod.fetch = feed.now, feed.sleep, feed.fetch
        mod.is_trading_day = lambda d: d.weekday() < 5
        mod.beep = mod.alert_beep = lambda: None
        mod.scan_config = lambda args: replace(TEST_CFG, stop_scanning=time(11, 0))
        with tempfile.TemporaryDirectory() as tmp:
            save_history(Path(tmp) / "history" / "yahoo_5m.json.gz", hist, {})
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(mod.main(["--data-dir", tmp]), 0)
            live_out = buf.getvalue()
            self.assertIn("ARMED", live_out)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = mod.main(["--data-dir", tmp, "--audit", "--date", day.isoformat()])
            out = buf.getvalue()
            self.assertEqual(code, 0, out)
            self.assertIn("identical: all", out, out)
            self.assertNotIn("MISSED LIVE", out)
            self.assertIn("after the crossing candle closed", out)
            self.assertIn("NEAR MISSES", out)
            self.assertIn("BIGGEST MOVES", out)
            self.assertTrue((Path(tmp) / "audit" / f"{day.isoformat()}.txt").exists())
            out.encode("ascii")

    def test_audit_flags_a_setup_the_live_run_missed(self):
        """Delete one ARMED setup from the live journal: the audit must name it."""
        from intelligence.history import HistoryIndex as HI
        from intelligence.setup_audit import audit, replay_day
        idx = HI(random_history(40, 16))
        day = idx.days()[-1]
        rnd = random.Random(1)
        base = idx.baselines(day)
        from intelligence.selector_data import RawSession, Series
        stocks = {}
        for s in list(idx.daily_bars)[:40]:
            p = base[s].prev_close
            rows = []
            for i in range(375):
                o = p
                c = o * (1 + (0.001 if s == "S003" else 0) + rnd.gauss(0, 0.0008))
                rows.append((at(day, 9, 15) + timedelta(minutes=i), o, max(o, c) * 1.0002, min(o, c) * 0.9998, c,
                             10_000 * (12.0 if (s == "S003" and i < 5) else 1.0)))
                p = c
            stocks[s] = Series(s, *zip(*rows))
        raw = RawSession(day, stocks, {}, None, {}, "test")
        eng, _, _ = replay_day(raw, idx, SetupConfig(), COST)
        self.assertTrue(any(t.setup == "ORB" and t.symbol == "S003" for t in eng.trades))
        with tempfile.TemporaryDirectory() as tmp:
            j = Path(tmp) / "j.jsonl"
            for t in eng.trades:
                if t.symbol != "S003":
                    j.open("a").write(json.dumps({"kind": "ARMED", "at": t.armed_at, "trade": t.to_dict()}) + "\n")
            text = audit(raw, idx, SetupConfig(), COST, j)
        self.assertIn("MISSED LIVE", text)
        self.assertIn("S003", text.split("MISSED LIVE")[1].splitlines()[0])


class ReviewFixTests(unittest.TestCase):
    """One test per bug found in the correctness review."""

    def setUp(self):
        self.idx = HistoryIndex(random_history(30, 16))
        self.day = self.idx.days()[-1]
        self.base = self.idx.baselines(self.day)
        self.cfg = replace(SetupConfig(), regime_long=1.1, regime_short=-0.1)

    def vwt_open(self, eng, entry_ts):
        eng.reset(self.day)
        eng._arm("VWT", "S001", 1, 100.0, 98.0, entry_ts, entry_ts + timedelta(minutes=10), [])
        t = eng.trades[-1]
        return t

    def test_vwap_exit_is_taken_in_time_order_before_a_later_stop(self):
        """A 5-min close below VWAP at 10:10 must exit there, not at a stop hit at 10:12."""
        eng = SetupEngine(self.cfg, COST)
        t0 = at(self.day, 10, 0)
        t = self.vwt_open(eng, t0)
        five = [(at(self.day, 9, 15) + timedelta(minutes=5 * k), 100, 100.2, 99.9, 100.1, 1000) for k in range(9)]
        five += [(t0, 99.95, 100.4, 99.9, 100.3, 1000), (t0 + timedelta(minutes=5), 100.3, 100.35, 99.0, 99.2, 50_000)]
        b = build_day("S001", five)
        self.assertLess(b.c[-1], b.vwap[-1])                     # the 10:05 bar closes below VWAP
        fine = [(t0 + timedelta(minutes=i), 100, 100.3, 99.95, 100.1, 1) for i in range(10)]
        fine += [(t0 + timedelta(minutes=10), 99.2, 99.3, 97.5, 97.6, 1)]   # stop hit at 10:10
        events = eng.step(self.day, {"S001": b}, self.base, t0 + timedelta(minutes=11), {"S001": fine})
        self.assertEqual([e.kind for e in events], ["TRIGGERED", "EXIT_VWAP"])
        self.assertEqual(t.exit, 99.2)

    def test_open_trade_is_squared_off_even_without_a_1515_candle(self):
        eng = SetupEngine(self.cfg, COST)
        eng.reset(self.day)
        t1 = at(self.day, 14, 50)
        eng._arm("FHM", "S002", 1, 100.0, 99.0, t1, t1 + timedelta(minutes=15), [])
        fine = [(t1, 99.9, 100.2, 99.8, 100.1, 1), (t1 + timedelta(minutes=1), 100.1, 100.6, 100.0, 100.5, 1)]
        eng.step(self.day, {}, self.base, t1 + timedelta(minutes=2), {"S002": fine})       # filled, halted after
        events = eng.step(self.day, {}, self.base, at(self.day, 15, 17), {"S002": fine})
        self.assertEqual([e.kind for e in events], ["SQUARE_OFF"])
        self.assertEqual(eng.trades[0].exit, 100.5)

    def test_orb_takes_the_top_20_by_rvol_before_the_body_filter(self):
        """Paper order: a doji in the top 20 is skipped, it does NOT pull in stock #21."""
        cfg = replace(self.cfg, orb_top=1)
        rnd = random.Random(3)
        bars = {}
        for s in list(self.idx.daily_bars)[:3]:
            rows = flat_day(self.day, self.base[s].prev_close, 50_000, rnd)
            o = rows[0][1]
            if s == "S000":    # highest RVOL but a doji
                rows[0] = (rows[0][0], o, o * 1.005, o * 0.995, o * 1.0001, 50_000 * 9)
            if s == "S001":    # second-highest RVOL, clean candle
                rows[0] = (rows[0][0], o, o * 1.006, o * 0.999, o * 1.005, 50_000 * 5)
            bars[s] = build_day(s, rows)
        eng = SetupEngine(cfg, COST)
        eng.step(self.day, bars, self.base, at(self.day, 9, 20))
        self.assertEqual([t.symbol for t in eng.trades if t.setup == "ORB"], [])
        miss = {n["symbol"]: n["failed"] for n in eng.near if n["setup"] == "ORB"}
        self.assertEqual(miss.get("S000"), "candle body")
        self.assertEqual(miss.get("S001"), "top rank")

    def test_live_uses_the_feeds_previous_close_and_warns_on_volume_scale(self):
        from intelligence.selector_data import Series
        rnd = random.Random(5)
        stale = {s: replace(b, prev_close=1.0) for s, b in self.base.items()}
        with tempfile.TemporaryDirectory() as tmp:
            live = LiveSetups(SetupEngine(self.cfg, COST), stale, {}, SetupJournal(tmp), self.day)
            stocks = {}
            for s in list(self.idx.daily_bars)[:20]:
                p = self.base[s].prev_close
                rows = [(at(self.day, 9, 15) + timedelta(minutes=i), p, p * 1.001, p * 0.999, p, 50_000.0) for i in range(6)]
                stocks[s] = Series(s, *zip(*rows), previous_close=p)
            live.on_scan(stocks, at(self.day, 9, 20))
            self.assertEqual(live.base["S000"].prev_close, self.base["S000"].prev_close)
            self.assertGreater(live.engine.diag["orb_median_rvol"], 2.5)     # 5 x 50k vs ~50k history
            self.assertIn("VOLUME CHECK", live.rvol_warning())
            self.assertIsNone(live.rvol_warning())                            # said once


class FeedOnlyTests(unittest.TestCase):
    """Only the PSYGRID feed is reachable: history comes from saved sessions, and the
    setups run on day-one proxies until it exists."""

    def load(self):
        spec = importlib.util.spec_from_file_location("psygrid_945_feedonly", ROOT / "945.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def write_sessions(self, folder, days, n=40):
        import gzip
        folder.mkdir(parents=True, exist_ok=True)
        for i, d in enumerate(days):
            p = make_payload(n, session=d, minutes=375, seed=100 + i, broken=False)
            (folder / f"{d.isoformat()}.json.gz").write_bytes(
                gzip.compress(json.dumps({"stocks_payload": p, "index_payload": None}).encode()))

    def test_history_is_built_from_saved_sessions(self):
        from intelligence.history import sessions_history, merge_history
        days = trading_days(3)
        with tempfile.TemporaryDirectory() as tmp:
            self.write_sessions(Path(tmp), days, n=5)
            h = sessions_history(tmp)
            self.assertEqual(len(h), 5)
            bars = h["STK0000"]
            self.assertEqual(len(bars), 3 * 75)
            self.assertEqual(bars[0][0].time(), time(9, 15))
            self.assertEqual(sessions_history(tmp, before=days[-1])["STK0000"][-1][0].date(), days[-2])
            # a later source wins for a day both have
            other = {"STK0000": [(bars[0][0], 1, 1, 1, 1, 1)]}
            merged = merge_history(other, h)
            self.assertEqual(len(merged["STK0000"]), 3 * 75)

    def test_bootstrap_fails_fast_when_yahoo_is_blocked(self):
        mod = self.load()
        mod.probe_yahoo = lambda: False
        mod.bootstrap = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download"))
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = mod.main(["--data-dir", tmp, "--bootstrap"])
        self.assertEqual(code, 70)
        self.assertIn("not reachable", buf.getvalue())

    def test_backtest_runs_on_saved_sessions_alone(self):
        mod = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_sessions(Path(tmp) / "sessions", trading_days(12), n=30)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = mod.main(["--data-dir", tmp, "--setup-backtest"])
            self.assertEqual(code, 0, buf.getvalue())
            self.assertIn("SETUP BACKTEST", buf.getvalue())

    def test_day_one_fhm_uses_todays_volatility(self):
        cfg = replace(SetupConfig(), regime_long=1.1, regime_short=-0.1, enabled=("FHM",))
        rnd = random.Random(8)
        d = date(2026, 10, 1)
        bars, base = {}, {}
        for i in range(30):
            s = f"S{i:03d}"
            drift = 0.004 if i < 3 else 0.0
            rows = flat_day(d, 100.0, 2_000_000 / 100.0, rnd, sigma=0.001, drift=drift)
            bars[s] = build_day(s, rows)
            from intelligence.setups import EMPTY_BASE
            base[s] = replace(EMPTY_BASE, prev_close=100.0)
        eng = SetupEngine(cfg, COST)
        eng.step(d, bars, base, at(d, 14, 45))
        fhm = [t for t in eng.trades if t.setup == "FHM"]
        self.assertTrue(fhm)
        self.assertTrue(all(t.direction == "LONG" for t in fhm))
        self.assertIn("day-one proxy", fhm[0].facts[0])
