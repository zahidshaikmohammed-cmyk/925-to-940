"""Dashboard server security, session controls (ARM / DISARM / PAUSE / SQUARE-OFF), the
read-only snapshot, and the live option-candle boundary regression."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

from sensex_expiry.audit import AuditLog
from sensex_expiry.config import EngineConfig
from sensex_expiry.dashboard import DashboardServer
from sensex_expiry.dhan_adapter import DhanBroker
from sensex_expiry.execution import OrderRequest, PaperBroker
from sensex_expiry.live import LiveRunner
from sensex_expiry.models import Reason, Tick
from sensex_expiry.risk import KillSwitch
from sensex_expiry.session import SQUARE_OFF_PHRASE, LiveSession

from tests.test_sensex_expiry_core import DAY, OPEN, PRIOR, sweep_day

CFG = EngineConfig()


class FakeSession:
    def __init__(self):
        self.snapshot_cond = threading.Condition()
        self.snapshot_seq = 1
        self.snapshot_json = json.dumps({"meta": {"seq": 1}})
        self.heartbeat = time.monotonic()
        self.calls = []

    def submit(self, action, confirm, client, timeout=5.0):
        self.calls.append((action, confirm))
        return {"ok": True, "message": "done"}


class TestServerSecurity(unittest.TestCase):
    def setUp(self):
        self.sess = FakeSession()
        self.srv = DashboardServer(self.sess, "127.0.0.1", 0, "tok123", embed_token=True)
        self.srv.start()
        self.port = self.srv.port

    def tearDown(self):
        self.srv.stop()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"} | (headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, data, hdrs

    def good_headers(self):
        return {"Origin": f"http://127.0.0.1:{self.port}", "X-Control-Token": "tok123", "Content-Type": "application/json"}

    def test_page_embeds_token_locally_and_sets_csp(self):
        st, body, h = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b'content="tok123"', body)
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", h)
        self.assertEqual(self.req("GET", "/favicon.ico")[0], 204)

    def test_state_is_readable(self):
        st, body, _ = self.req("GET", "/api/state")
        self.assertEqual((st, json.loads(body)["meta"]["seq"]), (200, 1))

    def test_bad_host_rejected(self):
        st, _, _ = self.req("GET", "/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(st, 421)

    def test_control_requires_origin_token_and_json(self):
        h = self.good_headers()
        self.assertEqual(self.req("POST", "/api/control", {"action": "PAUSE"}, h | {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/control", {"action": "PAUSE"}, {k: v for k, v in h.items() if k != "Origin"})[0], 403)
        self.assertEqual(self.req("POST", "/api/control", {"action": "PAUSE"}, h | {"X-Control-Token": "nope"})[0], 401)
        self.assertEqual(self.req("POST", "/api/control", {"action": "PAUSE"}, h | {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("POST", "/api/control", {"action": "BUY_NOW"}, h)[0], 400)
        self.assertEqual(self.sess.calls, [])
        st, body, _ = self.req("POST", "/api/control", {"action": "pause", "confirm": ""}, h)
        self.assertEqual(st, 200)
        self.assertEqual(self.sess.calls, [("PAUSE", "")])

    def test_rate_limit(self):
        h = self.good_headers()
        codes = [self.req("POST", "/api/control", {"action": "PAUSE"}, h)[0] for _ in range(12)]
        self.assertEqual(codes.count(200), 10)
        self.assertEqual(codes[-1], 429)

    def test_remote_mode_does_not_embed_token(self):
        srv = DashboardServer(FakeSession(), "127.0.0.1", 0, "secret", embed_token=False)
        srv.start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            c.request("GET", "/", headers={"Host": f"127.0.0.1:{srv.port}"})
            body = c.getresponse().read()
            self.assertNotIn(b"secret", body)
        finally:
            srv.stop()


class ManualFeed:
    """A feed the test drives by hand: ticks are pushed straight into the session queue."""
    kind = "TEST"

    def __init__(self):
        self.connected = True
        self.status = {"kind": "TEST", "connected": True, "reconnects": 0, "last_disconnect": None}
        self.t = OPEN

    def now(self):
        return self.t

    def start(self, sink):
        pass

    def stop(self):
        pass


def make_session(tmp: Path, mode="PAPER", broker=None):
    holder = {}
    broker = broker or PaperBroker(CFG, 500_000, lambda sid: holder["r"]._q(sid))
    runner = LiveRunner(CFG, DAY, PRIOR, True, DAY, 20, "51", {(81100, "CE"): "CE81100"}, broker,
                        KillSwitch(tmp / "kill.lock", 50_000, 1000), AuditLog(tmp / "audit.jsonl"))
    runner._q = lambda sid: (runner.quotes[sid].bid, runner.quotes[sid].ask) if sid in runner.quotes else None
    holder["r"] = runner
    feed = ManualFeed()
    sess = LiveSession(runner, feed, mode, tmp / "no_report.json", tmp, "TestBroker")
    return sess, runner, feed, broker


def feed_bars(sess, feed, bars, premium_of):
    for c in bars:
        for k, px in enumerate((c.open, c.high, c.low, c.close)):
            ts = c.start + timedelta(seconds=5 + 15 * k)
            prem = premium_of(px)
            sess.ticks.put(Tick("CE81100", ts, prem, ts, bid=prem - 0.1, ask=prem + 0.1, bid_qty=5000, ask_qty=5000))
            sess.ticks.put(Tick("51", ts, px, ts))
            feed.t = ts
            sess.step()
            sess.runner.broker.poll()


PREM = lambda px: max(5.0, 150 + 0.5 * (px - 81055))   # noqa: E731


class TestSessionControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def do(self, sess, action, confirm=""):
        from sensex_expiry.session import Command
        cmd = Command(action, confirm, "test")
        sess.commands.put(cmd)
        sess.step()
        return cmd.result

    def test_starts_disarmed_and_never_trades_unarmed(self):
        sess, runner, feed, broker = make_session(self.root)
        self.assertFalse(sess.armed)
        feed_bars(sess, feed, sweep_day(), PREM)
        self.assertIsNone(runner.pos)                       # the signal fired but the guard blocked it
        kinds = [json.loads(x) for x in (self.root / "audit.jsonl").read_text().splitlines()]
        blocks = [k["payload"]["reasons"] for k in kinds if k["kind"] == "GUARD_BLOCK"]
        self.assertTrue(any("LIVE_NOT_VALIDATED" in b for b in blocks))

    def test_arm_paper_then_trade(self):
        sess, runner, feed, broker = make_session(self.root)
        feed_bars(sess, feed, sweep_day()[:30], PREM)
        self.assertFalse(self.do(sess, "ARM", "arm please")["ok"])
        res = self.do(sess, "ARM", "ARM PAPER")
        self.assertTrue(res["ok"], res)
        self.assertTrue(runner.live_gate_ok)
        feed_bars(sess, feed, sweep_day()[30:], PREM)
        self.assertIsNotNone(runner.pos)

    def test_live_arm_refused_without_validation_report(self):
        sess, runner, feed, broker = make_session(self.root, mode="LIVE")
        feed_bars(sess, feed, sweep_day()[:30], PREM)
        res = self.do(sess, "ARM", f"ARM {CFG.config_hash()}")
        self.assertFalse(res["ok"])
        self.assertTrue(any("validation gate" in b for b in res["blockers"]))
        self.assertFalse(runner.live_gate_ok)

    def test_arm_refused_on_stale_data(self):
        sess, runner, feed, broker = make_session(self.root)
        feed_bars(sess, feed, sweep_day()[:30], PREM)
        feed.t = feed.t + timedelta(seconds=30)
        res = self.do(sess, "ARM", "ARM PAPER")
        self.assertFalse(res["ok"])
        self.assertIn("no fresh SENSEX tick in the last 5 s", res["blockers"])

    def test_pause_blocks_entries_but_keeps_managing(self):
        sess, runner, feed, broker = make_session(self.root)
        feed_bars(sess, feed, sweep_day()[:30], PREM)
        self.do(sess, "ARM", "ARM PAPER")
        self.assertTrue(self.do(sess, "PAUSE")["ok"])
        feed_bars(sess, feed, sweep_day()[30:], PREM)
        self.assertIsNone(runner.pos)
        self.assertEqual(runner.reason_counts.get("ENTRIES_PAUSED"), 1)
        self.assertTrue(self.do(sess, "RESUME")["ok"])
        self.assertFalse(runner.entries_paused)

    def test_squareoff_needs_phrase_trips_kill_flattens_and_disarms(self):
        sess, runner, feed, broker = make_session(self.root)
        feed_bars(sess, feed, sweep_day()[:30], PREM)
        self.do(sess, "ARM", "ARM PAPER")
        feed_bars(sess, feed, sweep_day()[30:], PREM)
        self.assertIsNotNone(runner.pos)
        self.assertFalse(self.do(sess, "SQUAREOFF", "yes")["ok"])
        self.assertTrue(self.do(sess, "SQUAREOFF", SQUARE_OFF_PHRASE)["ok"])
        broker.poll()
        self.assertTrue(runner.kill.tripped)
        self.assertFalse(sess.armed)
        self.assertEqual(broker.positions(), [])
        self.assertFalse(self.do(sess, "ARM", "ARM PAPER")["ok"])       # locked

    def test_squareoff_falls_back_to_broker_when_engine_hung(self):
        sess, runner, feed, broker = make_session(self.root)
        broker.pos["X"] = 20
        sess.heartbeat = time.monotonic() - 10
        res = sess.submit("SQUAREOFF", SQUARE_OFF_PHRASE, "test", timeout=0.5)
        self.assertTrue(res["ok"])
        self.assertIn("unresponsive", res["message"])
        self.assertTrue(runner.kill.tripped)

    def test_snapshot_has_every_panel_and_is_json(self):
        sess, runner, feed, broker = make_session(self.root)
        feed_bars(sess, feed, sweep_day()[:30], PREM)
        sess._publish(force=True)
        snap = json.loads(sess.snapshot_json)
        for k in ("meta", "control", "engine", "market", "decision", "position", "pnl", "risk", "data", "connection",
                  "orders", "reasons", "trades", "audit"):
            self.assertIn(k, snap)
        self.assertEqual(snap["control"]["armed"], False)
        self.assertEqual(snap["meta"]["config_hash"], CFG.config_hash())
        self.assertEqual(len(snap["market"]["bars"]), 29)

    def test_controls_are_audited(self):
        sess, runner, feed, broker = make_session(self.root)
        feed_bars(sess, feed, sweep_day()[:30], PREM)
        self.do(sess, "ARM", "ARM PAPER")
        self.do(sess, "PAUSE")
        self.do(sess, "DISARM")
        recs = [json.loads(x) for x in (self.root / "audit.jsonl").read_text().splitlines()]
        actions = [r["payload"]["action"] for r in recs if r["kind"] == "CONTROL"]
        self.assertEqual(actions, ["ARM", "PAUSE", "DISARM"])


class TestDhanExitsAfterDisarm(unittest.TestCase):
    def test_unarmed_dhan_broker_allows_sells_only(self):
        class C:
            def place_order(self, **kw):
                return {"status": "success", "data": {"orderId": "1", "orderStatus": "PENDING"}}
        b = DhanBroker("x", "y", armed=False, client=C())
        with self.assertRaises(PermissionError):
            b.place(OrderRequest("c", "1", "BUY", 20, "LIMIT", 100.0))
        self.assertEqual(b.place(OrderRequest("c", "1", "SELL", 20, "LIMIT", 90.0)).order_id, "1")


class TestOptionCandleBoundary(unittest.TestCase):
    """Regression: the index tick that closes minute M can arrive before the option's next tick.
    The position must not be force-exited for a 'missing' option bar that was really just open."""

    def test_no_false_data_failure_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, runner, feed, broker = make_session(Path(tmp))
            feed_bars(sess, feed, sweep_day()[:30], PREM)
            sess.commands.put(__import__("sensex_expiry.session", fromlist=["Command"]).Command("ARM", "ARM PAPER", "t"))
            sess.step()
            feed_bars(sess, feed, sweep_day()[30:], PREM)
            self.assertIsNotNone(runner.pos)
            start = sweep_day()[-1].start + timedelta(minutes=1)
            ticks = []
            for m in range(4):
                t0 = start + timedelta(minutes=m)
                for sec, px in ((20, 81070.0), (40, 81072.0)):          # option prints mid-minute only
                    ts = t0 + timedelta(seconds=sec)
                    ticks.append(Tick("CE81100", ts, PREM(px), ts, bid=PREM(px) - 0.1, ask=PREM(px) + 0.1,
                                      bid_qty=5000, ask_qty=5000))
                for sec, px in ((10, 81068.0), (30, 81070.0), (50, 81072.0)):
                    ts = t0 + timedelta(seconds=sec)
                    ticks.append(Tick("51", ts, px, ts))
            # true arrival order: the index tick at M+1:10 closes minute M BEFORE the option's M+1:20 print
            for t in sorted(ticks, key=lambda t: t.ts):
                sess.ticks.put(t)
                feed.t = t.ts
                sess.step()
            self.assertIsNotNone(runner.pos)
            self.assertNotEqual(runner.pos.pending_exit, Reason.EXIT_DATA_FAILURE)


if __name__ == "__main__":
    unittest.main()
