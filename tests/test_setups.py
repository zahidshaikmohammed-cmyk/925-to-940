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
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--setup-backtest"])
            self.assertEqual(code, 50)
            self.assertIn("--bootstrap", out)
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
        mod.scan_config = lambda args: replace(TEST_CFG, stop_scanning=time(9, 31))
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--no-archive"])
            self.assertIn("SETUPS OFF", out)
            code, out = self.run_cli(mod, ["--data-dir", tmp, "--no-archive", "--no-setups"])
            self.assertNotIn("SETUPS", out)


if __name__ == "__main__":
    unittest.main()
