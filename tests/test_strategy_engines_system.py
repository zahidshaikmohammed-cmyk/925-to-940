"""Strategy-engine system behaviour: universe accounting, fault isolation, duplicate
suppression, lifecycle, Telegram delivery, history inputs, the daily runner and replay."""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import strategy_engines as se
from psygrid_client import PsygridClient
from strategies.core import (ENGINE_ERROR, INELIGIBLE, INSUFFICIENT_DATA, INVALID_DATA, LONG, SIGNAL, STATUSES,
                             Engine, no)
from strategies.engines import ENGINE_NAMES, build_engines
from strategies.history import DayStats, build_priors, load_psygrid_days, previous_trading_day
from strategies.ledger import SignalLedger
from strategies.scanner import StockInput, run_scan
from strategies.telegram import TelegramNotifier, format_feed_problem, format_signal
from strategy_930 import IST, Candle
from tests import strategy_fixtures as fx

SYM = "TEST"
SWEEP = "Liquidity_Sweep_Reclaim_Engine"


def sweep_report():
    u, priors, at = fx.scenario_universe(SWEEP, SYM)
    return run_scan(u, at, build_engines(), priors), u, at


class UniverseAccounting(unittest.TestCase):
    def test_every_stock_accounted_for_by_every_engine(self):
        n = 60
        u = fx.filler_universe(980, n)
        good = u["F000"].bars
        at = fx.scan_after(n)
        bad = {
            "MALFORMED": good[:-1] + (replace(good[-1], high=good[-1].low - 1),),
            "DUPLICATE": good[:-1] + (replace(good[-1], ts=good[-2].ts),),
            "FUTURE": good + (replace(good[-1], ts=good[-1].ts + timedelta(minutes=1)),),
            "STALE": good[:-10],
            "NO_OPEN": good[3:],
            "ILLIQUID": tuple(replace(b, volume=10) for b in good),
            "PENNY": tuple(replace(b, open=b.open / 100, high=b.high / 100, low=b.low / 100, close=b.close / 100)
                           for b in good),
        }
        for s, bars in bad.items():
            u[s] = StockInput(s, bars, 100.0, None)
        u["UNHEALTHY"] = StockInput("UNHEALTHY", good, 100.0, None, False, "parse_exception:boom")
        universe = set(u) | {"MISSING"}                        # seen earlier today, absent now
        self.assertEqual(len(universe), 989)
        rep = run_scan(u, at, build_engines(), {}, universe)
        expect = {"MALFORMED": INVALID_DATA, "DUPLICATE": INVALID_DATA, "FUTURE": INVALID_DATA,
                  "STALE": INSUFFICIENT_DATA, "NO_OPEN": INSUFFICIENT_DATA, "ILLIQUID": INELIGIBLE,
                  "PENNY": INELIGIBLE, "UNHEALTHY": INVALID_DATA, "MISSING": INVALID_DATA}
        for e in ENGINE_NAMES:
            ev = rep.evaluations[e]
            self.assertEqual(len(ev), 989)
            self.assertEqual(sum(rep.counts[e].values()), 989)
            self.assertTrue(set(rep.counts[e]) <= set(STATUSES))
            for s, st in expect.items():
                self.assertEqual(ev[s].status, st, (e, s, ev[s].reason))
        self.assertEqual(rep.evaluations[ENGINE_NAMES[0]]["MISSING"].reason, "missing_from_feed")
        self.assertEqual(rep.valid, 980)
        self.assertTrue(rep.integrity_ok)


class MarketContextValidity(unittest.TestCase):
    def test_many_illiquid_stocks_are_not_a_broken_feed(self):
        n = 60
        u = fx.filler_universe(400, n)
        for i, (s, si) in enumerate(sorted(u.items())):
            if i >= 150:
                u[s] = replace(si, bars=tuple(replace(b, volume=50) for b in si.bars))
        rep = run_scan(u, fx.scan_after(n), build_engines(["Trend_Pullback_Continuation_Engine"]))
        self.assertEqual(rep.quality_counts[INELIGIBLE], 250)
        self.assertTrue(rep.context.valid, rep.context.reason)

    def test_broken_feed_invalidates_the_market(self):
        n = 60
        u = fx.filler_universe(400, n)
        for i, (s, si) in enumerate(sorted(u.items())):
            if i >= 100:
                u[s] = replace(si, bars=si.bars[:-10])               # 300 stale stocks
        rep = run_scan(u, fx.scan_after(n), [])
        self.assertFalse(rep.context.valid)
        self.assertIn("usable_data", rep.context.reason)


class Boom(Engine):
    NAME = "Boom_Engine"
    TITLE = "raises"
    EARLIEST, MIN_BARS = time(9, 16), 1

    def side(self, stock, sv, ctx):
        if stock.symbol == SYM:
            raise ZeroDivisionError("synthetic failure")
        return no("nothing")


class PrepBoom(Boom):
    NAME = "PrepBoom_Engine"

    def prepare(self, views, feats, ctx):
        raise RuntimeError("prepare failed")


class Mutator(Boom):
    NAME = "Mutator_Engine"

    def side(self, stock, sv, ctx):
        sv.base.c = tuple(x + 1 for x in sv.base.c)          # violates the read-only contract
        return no("mutated")


class FaultIsolation(unittest.TestCase):
    def test_a_failing_engine_cannot_affect_the_others(self):
        u, priors, at = fx.scenario_universe(SWEEP, SYM)
        clean = run_scan(u, at, build_engines(), priors)
        mixed = run_scan(u, at, [Boom(), PrepBoom()] + build_engines(), priors)
        self.assertEqual(mixed.evaluations["Boom_Engine"][SYM].status, ENGINE_ERROR)
        self.assertEqual(mixed.counts["PrepBoom_Engine"][ENGINE_ERROR], clean.valid)
        self.assertIn("Boom_Engine", mixed.engine_errors)
        self.assertEqual(clean.signals, mixed.signals)
        for e in ENGINE_NAMES:
            self.assertEqual({s: v.status for s, v in clean.evaluations[e].items()},
                             {s: v.status for s, v in mixed.evaluations[e].items()})

    def test_feature_mutation_is_detected(self):
        u, priors, at = fx.scenario_universe(SWEEP, SYM, n_filler=5)
        rep = run_scan(u, at, [Mutator()], priors)
        self.assertFalse(rep.integrity_ok)


class Ledger(unittest.TestCase):
    def setUp(self):
        self.rep, self.u, self.at = sweep_report()
        self.sig = [s for s in self.rep.signals if s.engine == SWEEP][0]

    def test_duplicate_continuing_and_cooldown(self):
        led = SignalLedger(None, {SWEEP: 15})
        self.assertEqual(led.decide(self.sig), (True, "new"))
        led.record(self.sig)
        self.assertEqual(led.decide(self.sig)[1], "duplicate_same_setup")
        later = replace(self.sig, anchor="other", signal_time=self.sig.signal_time + timedelta(minutes=5))
        self.assertEqual(led.decide(later)[1], "continuing_condition_active_signal")
        rec = led.records[self.sig.key]
        rec["state"] = "STOPPED"
        self.assertEqual(led.decide(later)[1], "within_cooldown")
        after_cooldown = replace(later, signal_time=self.sig.signal_time + timedelta(minutes=16))
        self.assertTrue(led.decide(after_cooldown)[1].startswith("symbol_alerted_recently"))  # 30-min gap
        much_later = replace(later, signal_time=self.sig.signal_time + timedelta(minutes=31))
        self.assertEqual(led.decide(much_later), (True, "new"))
        other_side = replace(later, direction="SHORT")
        self.assertEqual(led.decide(other_side)[1], "opposite_direction_alerted_recently")
        other_engine = replace(later, engine="VWAP_Reclaim_Rejection_Engine")
        self.assertTrue(led.decide(other_engine)[1].startswith("symbol_alerted_recently"))
        other_stock = replace(later, symbol="OTHER")
        self.assertEqual(led.decide(other_stock), (True, "new"))

    def _bars(self, *ohlc, start=None):
        start = start or self.sig.signal_time
        return [Candle(start + timedelta(minutes=i), *x, 1000) for i, x in enumerate(ohlc)]

    def _life(self, *ohlc):
        led = SignalLedger(None)
        rec = led.record(self.sig)
        led.update({SYM: self._bars(*ohlc)})
        return rec

    def test_lifecycle_paths(self):
        e, s, t = self.sig.entry, self.sig.stop, self.sig.target
        mid = (e + s) / 2
        self.assertEqual(self._life((mid, mid, mid, mid))["state"], "PENDING")
        self.assertEqual(self._life((mid, mid, s - 0.01, mid))["state"], "INVALIDATED")
        r = self._life((mid, e + 0.01, mid, e), (e, t + 0.01, e, t))
        self.assertEqual((r["state"], r["r_multiple"]), ("TARGET", round((t - e) / (e - s), 3)))
        self.assertAlmostEqual(r["r_net"], r["r_multiple"] - self.sig.cost_r, places=3)
        r = self._life((mid, e + 0.01, mid, e), (e, e, s - 0.01, s))
        self.assertEqual((r["state"], r["r_multiple"]), ("STOPPED", -1.0))
        r = self._life((mid, e + 0.01, s - 0.01, mid))            # both in one bar: stop assumed
        self.assertEqual(r["state"], "STOPPED")
        r = self._life((mid, mid, mid, mid), (mid, mid, mid, mid), (mid, mid, mid, mid), (mid, e + 1, mid, mid))
        self.assertEqual(r["state"], "EXPIRED")                     # valid for 3 minutes only
        r = self._life((e + 0.5, e + 0.6, e + 0.4, e + 0.5), (e, e, e - 0.01, e))  # gap fill at open
        self.assertEqual(r["fill"], e + 0.5)

    def test_time_exit_at_square_off(self):
        led = SignalLedger(None)
        rec = led.record(self.sig)
        e = self.sig.entry
        bars = self._bars((e, e + 0.01, e, e))
        sq = datetime.combine(self.sig.signal_time.date(), time(15, 15), IST)
        bars.append(Candle(sq, e + 0.1, e + 0.2, e, e + 0.1, 1000))
        led.update({SYM: bars})
        self.assertEqual((rec["state"], rec["exit_price"]), ("TIME_EXIT", e + 0.1))

    def test_persistence_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.json"
            led = SignalLedger(p)
            led.record(self.sig)
            led.save()
            again = SignalLedger(p)
            self.assertEqual(again.decide(self.sig)[1], "duplicate_same_setup")   # survives a restart
            p.write_text("{not json", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                fresh = SignalLedger(p)
            self.assertEqual(fresh.records, {})
            self.assertTrue((Path(d) / "ledger.corrupt").exists())
            self.assertIn("LEDGER-WARN", out.getvalue())


class GovernorRules(unittest.TestCase):
    def state(self):
        rep, _, _ = sweep_report()
        sig = [x for x in rep.signals if x.engine == SWEEP][0]
        st = se.new_day_state(fx.DAY.date(), Path(tempfile.mkdtemp()), [], TelegramNotifier("", ""), persist=False)
        return st, sig

    def closed(self, st, sig, i, r_net, minute):
        rec = st.ledger.record(replace(sig, symbol=f"S{i}", anchor=f"a{i}"))
        rec.update(state="STOPPED" if r_net < 0 else "TARGET", r_multiple=r_net, r_net=r_net,
                   state_time=(sig.signal_time + timedelta(minutes=minute)).isoformat())

    def test_daily_loss_limit_and_losing_streak(self):
        st, sig = self.state()
        self.assertIsNone(se.governor_block(st, LONG, 0))
        self.closed(st, sig, 1, -1.1, 1)
        self.closed(st, sig, 2, -1.1, 2)
        self.assertIsNone(se.governor_block(st, LONG, 0))
        self.closed(st, sig, 3, +2.0, 3)
        self.closed(st, sig, 4, -1.1, 4)
        self.assertIsNone(se.governor_block(st, LONG, 0))     # streak broken by the winner, -1.3R total
        self.closed(st, sig, 5, -1.1, 5)
        self.closed(st, sig, 6, -1.1, 6)
        self.assertIn("daily_loss_limit", se.governor_block(st, LONG, 0))   # -3.5R

    def test_streak_and_caps(self):
        st, sig = self.state()
        for i in range(3):
            self.closed(st, sig, i, -0.5, i)
        self.assertEqual(se.governor_block(st, LONG, 0), "losing_streak_3")
        st, sig = self.state()
        self.assertEqual(se.governor_block(st, LONG, 2), "max_new_alerts_this_minute")
        for i in range(3):
            st.ledger.record(replace(sig, symbol=f"O{i}", anchor=f"o{i}"))
        self.assertEqual(se.governor_block(st, LONG, 0), "max_open_long_trades")
        self.assertIsNone(se.governor_block(st, "SHORT", 0))

    def test_handle_report_caps_alerts_per_minute_highest_score_first(self):
        st, sig = self.state()
        rep, _, _ = sweep_report()
        many = [replace(sig, symbol=f"M{i}", anchor=f"m{i}", score=90 + i) for i in range(5)]
        rep.signals = many
        with contextlib.redirect_stdout(io.StringIO()):
            new = se.handle_report(st, rep, sig.signal_time, send=False)
        self.assertEqual([r["symbol"] for r in new], ["M4", "M3"])


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, timeout):
        self.calls.append((url, json.loads(body)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Telegram(unittest.TestCase):
    def notifier(self, *responses):
        self.sleeps = []
        self.tr = FakeTransport(*responses)
        return TelegramNotifier("123:SECRET", "-100", transport=self.tr, sleep=self.sleeps.append,
                                clock=lambda: 0.0)

    def test_not_configured(self):
        n = TelegramNotifier("", "")
        self.assertFalse(n.enabled)
        self.assertEqual(n.send("x")[1], "telegram_not_configured")

    def test_sends_html(self):
        n = self.notifier((200, {"ok": True, "result": {"message_id": 7}}))
        self.assertEqual(n.send("<b>x</b>"), (True, "sent", 7))
        url, body = self.tr.calls[0]
        self.assertTrue(url.endswith("/bot123:SECRET/sendMessage"))
        self.assertEqual((body["chat_id"], body["parse_mode"]), ("-100", "HTML"))

    def test_429_honours_retry_after(self):
        n = self.notifier((429, {"ok": False, "parameters": {"retry_after": 7}}), (200, {"ok": True, "result": {}}))
        self.assertTrue(n.send("x")[0])
        self.assertIn(7.0, self.sleeps)

    def test_4xx_not_retried_and_token_redacted(self):
        n = self.notifier((401, {"ok": False, "description": "Unauthorized for 123:SECRET"}))
        ok, info, _ = n.send("x")
        self.assertFalse(ok)
        self.assertNotIn("SECRET", info)
        self.assertEqual(len(self.tr.calls), 1)

    def test_network_errors_retried_then_reported(self):
        n = self.notifier(OSError("down"), OSError("down"), OSError("down"), OSError("down"))
        ok, info, _ = n.send("x")
        self.assertFalse(ok)
        self.assertIn("network", info)
        self.assertEqual(len(self.tr.calls), 4)

    def test_alert_text_has_every_required_field_and_escapes(self):
        rep, _, _ = sweep_report()
        sig = [s for s in rep.signals if s.engine == SWEEP][0]
        led = SignalLedger(None)
        rec = led.record(replace(sig, conditions=sig.conditions + ("a <b> & c",)))
        text = format_signal(rec)
        for needle in (SYM, "LONG", SWEEP, "Liquidity Sweep + Reclaim", f"{sig.score:.1f}",
                       f"₹{sig.entry:,.2f}", f"₹{sig.stop:,.2f}", f"₹{sig.target:,.2f}",
                       sig.signal_time.strftime("%Y-%m-%d %H:%M"), "a &lt;b&gt; &amp; c", "not a probability"):
            self.assertIn(needle, text)
        self.assertIn("Silence does NOT mean no opportunity", format_feed_problem(sig.signal_time, "x"))


class UnattendedRun(unittest.TestCase):
    def test_credentials_file_used_when_no_environment(self):
        import os
        from unittest import mock
        from strategies.telegram import load_credentials
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "telegram.env"
            f.write_text('# comment\nTELEGRAM_BOT_TOKEN = "123:ABC"\nTELEGRAM_CHAT_ID=-1001\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_credentials(f), ("123:ABC", "-1001"))
            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "9:Z", "TELEGRAM_CHAT_ID": "7"}, clear=True):
                self.assertEqual(load_credentials(f), ("9:Z", "7"))           # environment wins
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_credentials(Path(d) / "missing.env"), ("", ""))

    def test_credentials_file_is_git_ignored(self):
        self.assertIn("telegram.env", Path(".gitignore").read_text().split())

    def test_keep_awake_is_a_no_op_off_windows(self):
        self.assertFalse(se.keep_awake())


class History(unittest.TestCase):
    def test_previous_trading_day_skips_weekend_and_holiday(self):
        from run_engine import is_trading_day
        self.assertEqual(previous_trading_day(date(2026, 10, 19), is_trading_day), date(2026, 10, 16))
        self.assertEqual(previous_trading_day(date(2026, 10, 21), is_trading_day), date(2026, 10, 19))  # 20th holiday

    def test_build_priors_rules(self):
        td = lambda d: d.weekday() < 5
        prev = date(2026, 10, 12)
        full = DayStats(105.0, 95.0, 100.0, 1000.0, True)
        ps = {"A": {prev: full, **{prev - timedelta(days=k): full for k in range(1, 8)}},
              "B": {prev: DayStats(105, 95, 100, 1000, False)},             # incomplete session
              "C": {prev: full}}
        yh = {"A": {prev - timedelta(days=k): DayStats(1, 1, 1, 9e9, True) for k in range(1, 20)}}
        pri, rep = build_priors(date(2026, 10, 13), td, ps, yh, {"A": 100.2, "C": 110.0})
        self.assertEqual((pri["A"].pdh, pri["A"].pdl, pri["A"].pdc), (105.0, 95.0, 100.0))
        self.assertEqual((pri["A"].or_volume_base, pri["A"].or_source), (1000.0, "psygrid"))  # never mixed
        self.assertNotIn("B", pri)                                            # no levels from a partial day
        self.assertNotIn("C", pri)                                            # 10% close mismatch: dropped
        self.assertEqual(rep["pdc_mismatch"], 1)

    def test_archive_round_trip_feeds_tomorrows_levels(self):
        bars, pc, _ = fx.sc_structural()
        full = list(bars)
        while full[-1].ts.time() < time(15, 29):
            full.append(Candle(full[-1].ts + timedelta(minutes=1), 101.5, 101.6, 101.4, 101.5, 1000))
        raw = {SYM: payload(full, pc)}
        with tempfile.TemporaryDirectory() as d:
            se.archive_session(Path(d), fx.DAY.date(), raw, {"status": "OK"})
            days = load_psygrid_days(Path(d) / "sessions", fx.DAY.date() + timedelta(days=1))
        st = days[SYM][fx.DAY.date()]
        self.assertEqual((st.high, st.low, st.close, st.complete), (max(b.high for b in full), min(b.low for b in full),
                                                                    101.5, True))
        self.assertEqual(st.or_volume, sum(b.volume for b in full[:15]))


def payload(bars, pc):
    return {"previous_close": pc, "today_open": bars[0].open,
            "candles_1m": [{"timestamp": b.ts.isoformat(), "open": b.open, "high": b.high, "low": b.low,
                            "close": b.close, "volume": b.volume} for b in bars]}


class FakeClient(PsygridClient):
    def __init__(self, raw, mode="ok"):
        super().__init__("http://fake", expected_universe=len(raw))
        self.raw, self.mode = raw, mode

    def market(self):
        if self.mode == "down":
            raise RuntimeError("connection refused")
        self.last_market_meta = {"status": "OK", "session": {"status": "CLOSED" if self.mode == "closed" else "LIVE"}}
        return self.raw


class FakeNotifier(TelegramNotifier):
    def __init__(self):
        super().__init__("t", "c", transport=lambda *a: (200, {"ok": True, "result": {"message_id": 1}}),
                         sleep=lambda s: None)
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True, "sent", len(self.sent)


class DailyRunner(unittest.TestCase):
    def run_day(self, mode="ok", start=time(9, 59, 30), scans=4):
        u, priors, _ = fx.scenario_universe(SWEEP, SYM)
        raw = {s: payload(si.bars, si.previous_close) for s, si in u.items()}
        times = [datetime.combine(fx.DAY.date(), start, IST)]
        for _ in range(scans):
            nxt = times[-1].replace(second=0) + timedelta(minutes=1, seconds=8)
            times.append(nxt)
        times.append(datetime.combine(fx.DAY.date(), time(15, 17), IST))
        state = {"i": 0}
        clock = lambda: times[min(state["i"], len(times) - 1)]

        def sleep(_):
            state["i"] += 1
        with tempfile.TemporaryDirectory() as d:
            notifier = FakeNotifier()
            st = se.new_day_state(fx.DAY.date(), Path(d), build_engines(), notifier)
            st.priors = priors
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = se.run_day(st, FakeClient(raw, mode), 8, True, clock, sleep)
            journal = (st.day_dir / "scans.jsonl").read_text(encoding="utf-8").splitlines()
            archived = (Path(d) / "sessions" / f"{fx.DAY.date()}.json.gz").exists()
            ledger = json.loads((st.day_dir / "ledger.json").read_text()) if (st.day_dir / "ledger.json").exists() else {}
        return code, notifier.sent, out.getvalue(), journal, archived, ledger, st

    def test_signal_alerted_once_across_repeated_scans(self):
        code, sent, out, journal, archived, ledger, st = self.run_day()
        self.assertEqual(code, 0)
        alerts = [m for m in sent if SWEEP in m and "TEST" in m and "score" in m]
        self.assertEqual(len(alerts), 1)                 # 10:01 scan alerts; later scans do not repeat it
        self.assertTrue(any("Strategy engines" in m for m in sent))      # daily summary
        self.assertIn("Strategy engines running", sent[0])                # started message first
        self.assertTrue(archived)
        self.assertEqual(sum(1 for line in journal if "\"SCAN\"" in line), 5)   # start + 4 minutes
        rec = list(ledger["signals"].values())[0]
        self.assertTrue(rec["telegram"]["sent"])
        self.assertEqual(st.scans, 5)

    def test_feed_down_is_reported_not_silent(self):
        code, sent, out, journal, archived, ledger, st = self.run_day(mode="down")
        self.assertEqual(code, 0)
        self.assertIn("DATA FEED PROBLEM", out)
        self.assertEqual(sum(1 for m in sent if "DATA FEED PROBLEM" in m), 1)   # rate-limited to one per 15 min
        self.assertEqual(st.feed_failures, 5)
        self.assertEqual(st.scans, 0)

    def test_feed_not_live_produces_no_signals(self):
        code, sent, out, journal, archived, ledger, st = self.run_day(mode="closed")
        self.assertIn("feed not live", out)
        self.assertFalse(any(SWEEP in m and "score" in m for m in sent))

    def test_no_false_partial_feed_alarm_before_0921(self):
        code, sent, out, journal, archived, ledger, st = self.run_day(start=time(9, 17, 8), scans=2)
        self.assertNotIn("partial feed", out)

    def test_waits_for_first_completed_candle(self):
        code, sent, out, journal, archived, ledger, st = self.run_day(start=time(9, 10), scans=1)
        self.assertIn("waiting for the first completed candle", out)


class PriorsAtFirstScan(unittest.TestCase):
    def test_yesterdays_archive_becomes_todays_levels(self):
        u, _, _ = fx.scenario_universe(SWEEP, SYM, n_filler=120)
        yday = fx.DAY.date() - timedelta(days=1)
        shift = lambda bars: tuple(replace(b, ts=b.ts - timedelta(days=1)) for b in bars)
        full = list(u[SYM].bars)
        while full[-1].ts.time() < time(15, 29):
            full.append(Candle(full[-1].ts + timedelta(minutes=1), 100.0, 100.1, 99.9, 100.0, 20_000))
        yraw = {SYM: payload(shift(full), 99.0)}
        raw = {s: payload(si.bars, 100.0 if s == SYM else si.previous_close) for s, si in u.items()}
        times = [datetime.combine(fx.DAY.date(), time(10, 1, 8), IST), datetime.combine(fx.DAY.date(), time(15, 17), IST)]
        state = {"i": 0}
        with tempfile.TemporaryDirectory() as d:
            se.archive_session(Path(d), yday, yraw, {"status": "OK"})
            st = se.new_day_state(fx.DAY.date(), Path(d), build_engines([SWEEP]), FakeNotifier())
            self.assertIsNone(st.priors)
            with contextlib.redirect_stdout(io.StringIO()):
                se.run_day(st, FakeClient(raw), 8, True, lambda: times[min(state["i"], 1)],
                           lambda _: state.__setitem__("i", state["i"] + 1))
        p = st.priors[SYM]
        self.assertEqual(p.day, yday.isoformat())
        self.assertEqual((p.pdh, p.pdl, p.pdc), (max(b.high for b in full), min(b.low for b in full), 100.0))


class Replay(unittest.TestCase):
    def test_replay_saved_session(self):
        u, priors, _ = fx.scenario_universe(SWEEP, SYM)
        raw = {s: payload(si.bars, si.previous_close) for s, si in u.items()}
        with tempfile.TemporaryDirectory() as d:
            se.archive_session(Path(d), fx.DAY.date(), raw, {"status": "OK"})
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = se.replay([str(Path(d) / "sessions")], [SWEEP], Path(d), False)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("REPLAY 2026-10-13", text)
        line = [x for x in text.splitlines() if x.strip().startswith(SWEEP)][0]
        self.assertNotIn(" 0 signals", line)


if __name__ == "__main__":
    unittest.main()
