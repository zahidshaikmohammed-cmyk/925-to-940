"""SENSEX expiry engine: execution safety, kill switch, state machine, audit trail,
validation gate, history assembly, Dhan adapter (fake client) and a live replay."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from sensex_expiry.audit import AuditLog, verify
from sensex_expiry.config import EngineConfig
from sensex_expiry.dhan_adapter import DhanBroker, normalize_ltt, parse_scrip_master, tick_from_feed, ws_instruments
from sensex_expiry.execution import OrderRequest, OrderStatus, PaperBroker, PreOrderGuard, correlation_id, protective_stop
from sensex_expiry.history import load_day, merge_fixed, resolve_epochs, save_day, split_rolling
from sensex_expiry.live import LiveRunner
from sensex_expiry.models import IST, Reason, Tick
from sensex_expiry.risk import KillSwitch
from sensex_expiry.state_machine import InvalidTransition, State, TradeStateMachine
from sensex_expiry.synthetic import make_day
from sensex_expiry.validation_gate import CRITERIA, check_stage, live_allowed

from tests.test_sensex_expiry_core import DAY, OPEN, PRIOR, sweep_day

CFG = EngineConfig()
NOW = OPEN + timedelta(hours=1)


def buy_payload(**kw):
    p = {"setup": "S1_SWEEP_RECLAIM", "bar_timestamp": NOW.isoformat(), "direction": "LONG", "qty": 60,
         "outlay": 9000.0, "est_costs": 80.0, "entry": 150.0, "stop_loss": 105.0, "security_id": "123"}
    p.update(kw)
    return p


class FakeQuotes:
    def __init__(self):
        self.q = {}

    def __call__(self, sid):
        return self.q.get(sid)


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.quotes = FakeQuotes()
        self.broker = PaperBroker(CFG, 500_000, self.quotes)
        self.guard = PreOrderGuard(CFG)

    def check(self, **kw):
        args = dict(now=NOW, decision_payload=buy_payload(), broker=self.broker, data_age_ms=200, market_open=True,
                    live_gate_ok=True, kill_switch_tripped=False, engine_flat=True)
        args.update(kw)
        return self.guard.check(**args)

    def test_clean_signal_becomes_marketable_ioc_limit(self):
        reasons, req = self.check()
        self.assertEqual(reasons, [])
        self.assertEqual((req.side, req.order_type, req.validity, req.qty), ("BUY", "LIMIT", "IOC", 60))
        self.assertAlmostEqual(req.price, 150.10)

    def test_each_blocker(self):
        self.assertEqual(self.check(kill_switch_tripped=True)[0], [Reason.KILL_SWITCH])
        self.assertIn(Reason.LIVE_NOT_VALIDATED, self.check(live_gate_ok=False)[0])
        self.assertIn(Reason.DATA_STALE, self.check(data_age_ms=9_000)[0])
        self.assertIn(Reason.DATA_STALE, self.check(data_age_ms=None)[0])
        self.assertIn(Reason.POSITION_OPEN, self.check(engine_flat=False)[0])
        self.assertIn(Reason.OUTSIDE_WINDOW, self.check(market_open=False)[0])
        self.assertIn(Reason.OUTSIDE_WINDOW, self.check(now=OPEN.replace(hour=14, minute=50))[0])
        self.assertIn(Reason.STOP_TOO_WIDE, self.check(decision_payload=buy_payload(stop_loss=160.0))[0])
        self.assertIn(Reason.RISK_TOO_HIGH, self.check(decision_payload=buy_payload(outlay=10_000_000.0))[0])
        self.assertIn(Reason.DATA_BAD, self.check(decision_payload=buy_payload(security_id=None))[0])

    def test_duplicate_signal_cannot_send_twice(self):
        self.assertEqual(self.check()[0], [])
        self.assertIn(Reason.DUPLICATE_SIGNAL, self.check()[0])

    def test_broker_position_blocks_even_if_engine_thinks_flat(self):
        self.broker.pos["999"] = 20
        self.assertIn(Reason.POSITION_OPEN, self.check()[0])

    def test_correlation_ids(self):
        a = correlation_id("2026-10-08|S1|ORL|LONG")
        self.assertEqual(a, correlation_id("2026-10-08|S1|ORL|LONG"))
        self.assertNotEqual(a, correlation_id("2026-10-08|S1|ORL|LONG", 1))
        self.assertLessEqual(len(a), 25)

    def test_protective_stop_shape(self):
        _, req = self.check()
        sl = protective_stop(req, 105.0, 60, 0.05)
        self.assertEqual((sl.side, sl.order_type, sl.trigger_price), ("SELL", "STOP_LOSS", 105.0))
        self.assertLess(sl.price, sl.trigger_price)
        self.assertNotEqual(sl.correlation_id, req.correlation_id)


class TestPaperBroker(unittest.TestCase):
    def setUp(self):
        self.quotes = FakeQuotes()
        self.b = PaperBroker(CFG, 100_000, self.quotes)

    def test_ioc_not_marketable_is_cancelled(self):
        self.quotes.q["1"] = (99.0, 101.0)
        st = self.b.place(OrderRequest("c1", "1", "BUY", 20, "LIMIT", 100.0, validity="IOC"))
        self.assertEqual(st.status, OrderStatus.CANCELLED)
        self.assertEqual(self.b.positions(), [])

    def test_buy_fills_at_ask_plus_slippage_and_stop_fills_on_trade_through(self):
        self.quotes.q["1"] = (99.0, 100.0)
        st = self.b.place(OrderRequest("c1", "1", "BUY", 20, "LIMIT", 100.2, validity="IOC"))
        self.assertEqual((st.status, st.avg_price), (OrderStatus.FILLED, 100.1))
        sl = self.b.place(OrderRequest("c2", "1", "SELL", 20, "STOP_LOSS", 63.0, trigger_price=70.0))
        self.assertEqual(sl.status, OrderStatus.OPEN)
        self.quotes.q["1"] = (69.0, 69.5)
        self.b.poll()
        self.assertEqual((sl.status, sl.avg_price), (OrderStatus.FILLED, 68.9))
        self.assertEqual(self.b.positions(), [])

    def test_stop_gapping_through_limit_stays_open(self):
        self.quotes.q["1"] = (99.0, 100.0)
        self.b.place(OrderRequest("c1", "1", "BUY", 20, "LIMIT", 100.2, validity="IOC"))
        sl = self.b.place(OrderRequest("c2", "1", "SELL", 20, "STOP_LOSS", 63.0, trigger_price=70.0))
        self.quotes.q["1"] = (50.0, 51.0)
        self.b.poll()
        self.assertEqual(sl.status, OrderStatus.OPEN)      # the engine's chase logic must act


class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "kill.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_trips_persist_and_survive_restart(self):
        k = KillSwitch(self.path, 5000, 200)
        k.check_broker_state(NOW, -6000, [], 0, 0, 0)
        self.assertTrue(k.tripped)
        self.assertEqual(KillSwitch(self.path, 5000, 200).reason(), "DAILY_LOSS_LIMIT")

    def test_unexpected_position_and_orders(self):
        k = KillSwitch(self.path, 5000, 200)
        k.check_broker_state(NOW, 0, [{"security_id": "1", "net_qty": 20}], 0, 0, 0)
        self.assertEqual(k.reason(), "UNEXPECTED_POSITION")
        self.path.unlink()
        k.check_broker_state(NOW, 0, [], 0, 3, 1)
        self.assertEqual(k.reason(), "UNEXPECTED_ORDERS")

    def test_max_quantity(self):
        k = KillSwitch(self.path, 5000, 100)
        k.check_broker_state(NOW, 0, [{"net_qty": 120}], 120, 0, 0)
        self.assertEqual(k.reason(), "MAX_QTY_EXCEEDED")

    def test_runaway_order_loop(self):
        k = KillSwitch(self.path, 5000, 200, max_orders_per_minute=3)
        for i in range(4):
            k.note_order(NOW + timedelta(seconds=i))
        self.assertEqual(k.reason(), "RUNAWAY_ORDER_LOOP")


class TestStateMachine(unittest.TestCase):
    def test_full_valid_cycle(self):
        sm = TradeStateMachine()
        for s in (State.DATA_VALID, State.REGIME_IDENTIFIED, State.SETUP_CONFIRMED, State.RISK_APPROVED,
                  State.ORDER_PENDING, State.POSITION_OPEN, State.POSITION_MANAGEMENT, State.EXIT_PENDING,
                  State.COOLDOWN, State.WAITING):
            sm.go(s, NOW)
        self.assertEqual(sm.state, State.WAITING)

    def test_cannot_open_second_position_or_skip_risk(self):
        sm = TradeStateMachine(State.POSITION_MANAGEMENT)
        with self.assertRaises(InvalidTransition):
            sm.go(State.ORDER_PENDING, NOW)
        sm2 = TradeStateMachine(State.SETUP_CONFIRMED)
        with self.assertRaises(InvalidTransition):
            sm2.go(State.ORDER_PENDING, NOW)
        self.assertFalse(TradeStateMachine(State.POSITION_OPEN).accepts_entries)

    def test_killed_is_terminal_and_reachable_from_anywhere(self):
        for s in State:
            sm = TradeStateMachine(s)
            sm.go(State.KILLED, NOW)
            with self.assertRaises(InvalidTransition):
                sm.go(State.WAITING, NOW)


class TestAudit(unittest.TestCase):
    def test_hash_chain_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "audit.jsonl"
            log = AuditLog(p)
            for i in range(3):
                log.write("DECISION", NOW, {"i": i})
            self.assertEqual(verify(p), (True, 3))
            AuditLog(p).write("DECISION", NOW, {"i": 3})          # reopen continues the chain
            self.assertEqual(verify(p), (True, 4))
            lines = p.read_text().splitlines()
            rec = json.loads(lines[1])
            rec["payload"]["i"] = 99
            lines[1] = json.dumps(rec)
            p.write_text("\n".join(lines) + "\n")
            self.assertEqual(verify(p), (False, 1))


class TestValidationGate(unittest.TestCase):
    def passing(self):
        def ok(crit):
            return {k: (v + 1 if k.startswith("min_") else v) for k, v in crit.items()}
        return {st: ok(c) for st, c in CRITERIA.items()}

    def test_missing_report_or_wrong_hash_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            self.assertFalse(live_allowed(p, CFG.config_hash(), "TINY_LIVE").allowed)
            p.write_text(json.dumps({"config_hash": "other", "stages": self.passing()}))
            self.assertFalse(live_allowed(p, CFG.config_hash(), "TINY_LIVE").allowed)
            p.write_text(json.dumps({"config_hash": CFG.config_hash(), "stages": self.passing()}))
            self.assertTrue(live_allowed(p, CFG.config_hash(), "TINY_LIVE").allowed)
            self.assertTrue(live_allowed(p, CFG.config_hash(), "PRODUCTION").allowed)

    def test_single_failed_criterion_blocks(self):
        stages = self.passing()
        stages["WALK_FORWARD"]["min_oos_trades"] = 59
        self.assertTrue(check_stage("WALK_FORWARD", stages["WALK_FORWARD"]))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({"config_hash": CFG.config_hash(), "stages": stages}))
            g = live_allowed(p, CFG.config_hash(), "TINY_LIVE")
            self.assertFalse(g.allowed)
            self.assertTrue(any("min_oos_trades" in f for f in g.failures))

    def test_missing_metric_is_failure_not_pass(self):
        self.assertTrue(any("missing" in f for f in check_stage("PAPER", {})))


class TestHistory(unittest.TestCase):
    def test_rolling_split_and_conflict_detection(self):
        t0 = int(datetime(2026, 10, 8, 9, 15, tzinfo=IST).timestamp())
        resp = {"data": {"ce": {"timestamp": [t0, t0 + 60, t0 + 120], "open": [100, 101, 90], "high": [102, 103, 92],
                                "low": [99, 100, 88], "close": [101, 102, 91], "volume": [5, 6, 7],
                                "strike": [81000, 81000, 81100]}, "pe": None}}
        parts = split_rolling(resp, "CE")
        self.assertEqual(sorted(parts), [81000, 81100])
        self.assertEqual(len(parts[81000]), 2)
        bad = {81000: {k: v.__class__(v.start, v.open, v.high, v.low, v.close + 5) for k, v in parts[81000].items()}}
        with self.assertRaises(ValueError):
            merge_fixed([parts, bad])

    def test_epoch_convention_resolved_or_refused(self):
        utc = int(datetime(2026, 10, 8, 9, 15, tzinfo=IST).timestamp())
        self.assertEqual(resolve_epochs([utc])[0].time().hour, 9)
        ist_encoded = utc + 19800
        self.assertEqual(resolve_epochs([ist_encoded])[0], datetime(2026, 10, 8, 9, 15, tzinfo=IST))
        with self.assertRaises(ValueError):
            resolve_epochs([utc + 4 * 3600])

    def test_day_roundtrip(self):
        d = make_day(DAY, seed=2, strikes_each_side=1)
        with tempfile.TemporaryDirectory() as tmp:
            back = load_day(save_day(d, Path(tmp)))
        def ohlcv(cs):
            return [(c.start, c.open, c.high, c.low, c.close, c.volume) for c in cs]   # tick counts are not stored
        self.assertEqual(ohlcv(back.underlying), ohlcv(d.underlying))
        self.assertEqual({k: ohlcv(v.values()) for k, v in back.options.items()},
                         {k: ohlcv(v.values()) for k, v in d.options.items()})
        self.assertEqual(back.prior, d.prior)


class FakeDhan:
    def __init__(self):
        self.calls = []

    def place_order(self, **kw):
        self.calls.append(("place", kw))
        return {"status": "success", "data": {"orderId": "9001", "orderStatus": "PENDING"}}

    def get_order_list(self):
        return {"status": "success", "data": [{"orderId": "1", "orderStatus": "PENDING"},
                                              {"orderId": "2", "orderStatus": "TRADED"}]}

    def cancel_order(self, oid):
        self.calls.append(("cancel", oid))
        return {"status": "success", "data": {"orderId": oid, "orderStatus": "CANCELLED"}}

    def get_positions(self):
        return {"status": "success", "data": [{"securityId": 777, "netQty": 40, "realizedProfit": -100.0,
                                               "unrealizedProfit": 50.0}]}

    def get_fund_limits(self):
        return {"status": "success", "data": {"availabelBalance": 12345.5}}

    def kill_switch(self, action):
        self.calls.append(("kill", action))
        return {"status": "success", "data": {"killSwitchStatus": "ACTIVATE"}}


class TestDhanAdapter(unittest.TestCase):
    def test_unarmed_broker_refuses_entries_but_allows_exits(self):
        fake = FakeDhan()
        b = DhanBroker("id", "tok", armed=False, client=fake)
        with self.assertRaises(PermissionError):
            b.place(OrderRequest("c", "1", "BUY", 20, "LIMIT", 100.0))
        res = b.square_off_all()
        self.assertEqual([c[0] for c in fake.calls], ["cancel", "place"])
        self.assertEqual(fake.calls[-1][1]["transaction_type"], "SELL")
        self.assertEqual(len(res), 2)

    def test_account_reads(self):
        b = DhanBroker("id", "tok", client=FakeDhan())
        self.assertEqual(b.positions(), [{"security_id": "777", "net_qty": 40, "pnl": -50.0}])
        self.assertEqual(b.available_margin(), 12345.5)
        self.assertEqual([o.order_id for o in b.open_orders()], ["1"])
        self.assertTrue(b.account_kill_switch())

    def test_feed_message_conversion(self):
        recv = datetime(2026, 10, 8, 10, 0, 1, tzinfo=IST)
        msg = {"type": "Full Data", "exchange_segment": 8, "security_id": 123, "LTP": "150.25", "LTT": "10:00:00",
               "volume": 500, "OI": 9000, "depth": [{"bid_quantity": 100, "ask_quantity": 80, "bid_orders": 1,
                                                     "ask_orders": 1, "bid_price": "150.20", "ask_price": "150.30"}]}
        t = tick_from_feed(msg, recv)
        self.assertEqual((t.security_id, t.ltp, t.bid, t.ask, t.ts.hour), ("123", 150.25, 150.2, 150.3, 10))
        utc_str = dict(msg, LTT="04:30:00")          # UTC wall-clock string is resolved to IST
        self.assertEqual(tick_from_feed(utc_str, recv).ts.hour, 10)
        self.assertIsNone(tick_from_feed({"LTP": "x"}, recv))

    def test_normalize_epoch(self):
        recv = datetime(2026, 10, 8, 10, 0, 0, tzinfo=IST)
        self.assertEqual(normalize_ltt(int(recv.timestamp()), recv)[1], "UTC_EPOCH")
        self.assertEqual(normalize_ltt(int(recv.timestamp()) + 19800, recv)[1], "IST_EPOCH")

    def test_scrip_master_and_ws_tuples(self):
        csv_text = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_DATE,"
                    "SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS\n"
                    "BSE,I,51,INDEX,,,,SENSEX,1\n"
                    "BSE,D,8801,OPTIDX,2026-10-08 14:30:00,81000.0,CE,SENSEX-Oct2026-81000-CE,20\n"
                    "BSE,D,8802,OPTIDX,2026-10-15 14:30:00,81000.0,CE,SENSEX-Oct2026-81000-CE,20\n"
                    "BSE,D,8803,OPTIDX,2026-10-08 14:30:00,81000.0,CE,SENSEX50-Oct2026-81000-CE,20\n")
        m = parse_scrip_master(csv_text, date(2026, 10, 8))
        self.assertEqual(m["index_id"], "51")
        self.assertEqual(m["options"], {(81000, "CE"): ("8801", 20)})
        self.assertEqual(ws_instruments("51", ["8801"]), [(0, "51", 15), (8, "8801", 21)])


class TestLiveReplay(unittest.TestCase):
    """Drive LiveRunner with ticks built from the hand-made sweep day and a paper broker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.holder = {}
        self.broker = PaperBroker(CFG, 500_000, lambda sid: self.holder["runner"]._paper_quote(sid))
        self.kill = KillSwitch(root / "kill.lock", 50_000, 1000)
        self.runner = LiveRunner(CFG, DAY, PRIOR, True, DAY, 20, "51", {(81100, "CE"): "CE81100"},
                                 self.broker, self.kill, AuditLog(root / "audit.jsonl"), live_gate_ok=True)
        self.runner._paper_quote = lambda sid: ((self.runner.quotes[sid].bid, self.runner.quotes[sid].ask)
                                                if sid in self.runner.quotes else None)
        self.holder["runner"] = self.runner
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, bars, premium_of):
        for c in bars:
            for k, px in enumerate((c.open, c.high, c.low, c.close)):
                ts = c.start + timedelta(seconds=5 + 15 * k)
                prem = premium_of(px)
                self.runner.on_tick(Tick("CE81100", ts, prem, ts, bid=prem - 0.1, ask=prem + 0.1,
                                         bid_qty=5000, ask_qty=5000))
                self.runner.on_tick(Tick("51", ts, px, ts))
                self.broker.poll()
                self.runner.on_clock(ts)
                self.runner.reconcile(ts)

    def test_sweep_enters_places_stop_and_survives_reconcile(self):
        bars = sweep_day()
        self.feed(bars, lambda px: max(5.0, 150 + 0.5 * (px - 81055)))
        self.assertFalse(self.kill.tripped, self.kill.reason())
        self.assertIsNotNone(self.runner.pos)
        self.assertEqual(self.runner.sm.state, State.POSITION_MANAGEMENT)
        self.assertEqual(sum(p["net_qty"] for p in self.broker.positions()), self.runner.pos.qty)
        self.assertEqual(len(self.broker.open_orders()), 1)                # the protective stop
        ok, n = verify(self.root / "audit.jsonl")
        self.assertTrue(ok)
        kinds = [json.loads(x)["kind"] for x in (self.root / "audit.jsonl").read_text().splitlines()]
        self.assertIn("FILL", kinds)

    def test_kill_switch_squares_off_and_locks(self):
        self.feed(sweep_day(), lambda px: max(5.0, 150 + 0.5 * (px - 81055)))
        self.assertIsNotNone(self.runner.pos)
        self.kill.trip("MANUAL", NOW)
        self.runner.on_clock(NOW)
        self.broker.poll()
        self.assertEqual(self.runner.sm.state, State.KILLED)
        self.assertEqual(self.broker.positions(), [])
        self.assertEqual(self.broker.open_orders(), [])

    def test_stuck_exit_never_doubles_up_and_escalates(self):
        self.feed(sweep_day(), lambda px: max(5.0, 150 + 0.5 * (px - 81055)))
        self.assertIsNotNone(self.runner.pos)
        real_try = self.broker._try_fill
        self.broker._try_fill = lambda st: None if st.request.side == "SELL" else real_try(st)   # book has no buyers
        t = NOW
        self.runner._exit(t, Reason.EXIT_INVALIDATION)
        for i in range(40):
            t += timedelta(seconds=1)
            self.runner.on_clock(t)
            sells = [o for o in self.broker.open_orders() if o.request.side == "SELL" and o.request.order_type == "LIMIT"]
            self.assertLessEqual(len(sells), 1)
            if self.kill.tripped:
                break
        self.assertEqual(self.kill.reason(), "EXIT_NOT_FILLING")
        self.assertEqual(self.runner.sm.state, State.KILLED)

    def test_exit_chase_fills_and_books_trade(self):
        self.feed(sweep_day(), lambda px: max(5.0, 150 + 0.5 * (px - 81055)))
        self.runner._exit(NOW, Reason.EXIT_INVALIDATION)
        self.assertIsNone(self.runner.pos)
        self.assertEqual(self.broker.positions(), [])
        self.assertEqual(self.broker.open_orders(), [])
        self.assertEqual(self.runner.sm.state, State.WAITING)
        self.assertEqual(self.runner.engine.risk_state.trades, 1)

    def test_unexpected_broker_position_trips_kill_switch(self):
        self.feed(sweep_day()[:30], lambda px: 150.0)
        self.broker.pos["ROGUE"] = 20
        self.runner.reconcile(NOW)
        self.assertTrue(self.kill.tripped)
        self.assertEqual(self.kill.reason(), "UNEXPECTED_POSITION")


if __name__ == "__main__":
    unittest.main()
