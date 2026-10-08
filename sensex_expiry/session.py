"""Autonomous trading session + browser dashboard (no terminal interaction needed).

    python -m sensex_expiry.session --feed replay --day data/sensex_expiry/days/2026-10-08.json --speed 30
    python -m sensex_expiry.session --feed dhan --mode paper          # live data, paper orders
    python -m sensex_expiry.session --feed dhan --mode live           # live data, real orders once ARMED

Then open http://127.0.0.1:8765 (printed at start, also written to <state-dir>/dashboard_url.txt).

Threads:
  feed thread     Dhan WebSocket (or replay) -> thread-safe tick queue
  engine thread   the ONLY thread that touches the runner: ticks, clock, reconcile,
                  queued control commands, snapshot publishing (every 0.5 s)
  http thread     serves the dashboard; reads published snapshots; enqueues controls

The strategy never waits for the dashboard. Closing the browser, or the dashboard
thread crashing, changes nothing: an ARMED engine keeps trading and managing exits.
Every session starts DISARMED. Arming never survives a restart.
"""
from __future__ import annotations

import argparse
import json
import queue
import secrets
import sys
import threading
import time as _time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .audit import AuditLog
from .config import EngineConfig
from .execution import PaperBroker
from .features import opening_range
from .live import LiveRunner
from .models import IST, Tick
from .risk import KillSwitch
from .session_calendar import at, weekly_expiry_for
from .validation_gate import live_allowed

ARM_PHRASE_LIVE = "ARM {hash}"
ARM_PHRASE_PAPER = "ARM PAPER"
SQUARE_OFF_PHRASE = "SQUARE OFF"


# ------------------------------------------------------------------ feeds

class ReplayFeed:
    """Replays one recorded/built day as ticks (4 index ticks per minute: O, H, L, C order
    matching the bar's direction; options at the same instants with modelled bid/ask).
    Simulated time advances `speed` x faster than the wall clock. For rehearsing the
    dashboard and the controls outside market hours: PAPER ONLY."""

    kind = "REPLAY"

    def __init__(self, day, speed: float = 30.0, spread_pct: float = 0.005, start_at: str | None = None):
        self.day, self.speed, self.spread_pct = day, max(0.1, speed), spread_pct
        self.connected = False
        self.status = {"kind": self.kind, "connected": False, "reconnects": 0, "last_disconnect": None, "detail": ""}
        self._now = at(day.day, (datetime.strptime(start_at, "%H:%M").time() if start_at else
                                 day.underlying[0].start.time())) - timedelta(seconds=1)
        self._stop = threading.Event()
        self.done = False
        self.backlog = lambda: 0           # set by LiveSession to its tick-queue depth

    def now(self) -> datetime:
        """Replay time advances only with emitted ticks (discrete-event clock). A clock that
        ran continuously at `speed` x would turn the engine loop's own few-ms latency into
        seconds of apparent tick age and trip the freshness rules."""
        return self._now

    def start(self, sink) -> None:
        threading.Thread(target=self._run, args=(sink,), name="replay-feed", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, sink) -> None:
        """One index tick per simulated second along O -> first extreme -> second extreme -> C
        (a real feed ticks several times a second, so a sparser replay would be correctly
        judged stale by the data-quality rules). Options tick every 2 s for strikes within
        6 steps of the spot."""
        self.connected = self.status["connected"] = True
        d = self.day
        step = 100
        for bar in d.underlying:
            if bar.start < self._now - timedelta(seconds=59):
                continue
            pts = (bar.open, bar.low, bar.high, bar.close) if bar.close >= bar.open else (bar.open, bar.high, bar.low, bar.close)
            for sec in range(1, 60):
                ts = bar.start + timedelta(seconds=sec)
                wait = (ts - self._now).total_seconds() / self.speed
                if wait > 0 and self._stop.wait(wait):
                    return
                while self.backlog() > 50:             # never run ahead of the engine
                    if self._stop.wait(0.01):
                        return
                self._now = ts
                px = round(_along(pts, sec / 59), 2)
                if sec % 2 == 0:
                    atm = round(px / step) * step
                    for (strike, right), series in d.options.items():
                        if abs(strike - atm) > 6 * step:
                            continue
                        o = series.get(bar.start)
                        if o is None:
                            continue
                        opts = (o.open, o.low, o.high, o.close) if o.close >= o.open else (o.open, o.high, o.low, o.close)
                        p = round(_along(opts, sec / 59) / 0.05) * 0.05
                        half = max(0.05, round(self.spread_pct * p / 2, 2))
                        sink(Tick(f"{strike}{right}", ts, round(p, 2), ts, bid=round(p - half, 2), ask=round(p + half, 2),
                                  bid_qty=5000, ask_qty=5000))
                sink(Tick("SENSEX", ts, px, ts))
        self.done = True
        self._now = d.underlying[-1].start + timedelta(minutes=1, seconds=5)
        self.connected = self.status["connected"] = False
        self.status["detail"] = "replay finished"


def _along(pts, f: float) -> float:
    """Piecewise-linear position f in [0, 1] along the 4-point O/extreme/extreme/C path."""
    seg = min(2, int(f * 3))
    t = f * 3 - seg
    return pts[seg] + (pts[seg + 1] - pts[seg]) * t


class DhanFeed:
    """Dhan market-feed v2 WebSocket in its own thread, with reconnect and backoff.
    Index as Ticker, options as Full (depth + OI), per dhan_adapter.ws_instruments."""

    kind = "DHAN"

    def __init__(self, client_id: str, token: str, index_id: str, option_sids: list[str]):
        self.client_id, self.token = client_id, token
        self.index_id, self.option_sids = index_id, option_sids
        self.connected = False
        self.status = {"kind": self.kind, "connected": False, "reconnects": 0, "last_disconnect": None,
                       "detail": "", "instruments": 1 + len(option_sids)}
        self._stop = threading.Event()

    def now(self) -> datetime:
        return datetime.now(IST)

    def start(self, sink) -> None:
        threading.Thread(target=self._run, args=(sink,), name="dhan-feed", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, sink) -> None:
        from .dhan_adapter import tick_from_feed, ws_instruments
        backoff = 1.0
        while not self._stop.is_set():
            try:
                from dhanhq import DhanContext
                from dhanhq.marketfeed import MarketFeed
                feed = MarketFeed(DhanContext(self.client_id, self.token),
                                  ws_instruments(self.index_id, self.option_sids), version="v2")
                feed.run_forever()
                self.connected = self.status["connected"] = True
                self.status["detail"] = "streaming"
                backoff = 1.0
                while not self._stop.is_set():
                    msg = feed.get_data()
                    if isinstance(msg, dict) and "LTP" in msg:
                        t = tick_from_feed(msg, datetime.now(IST))
                        if t is not None:
                            sink(t)
            except Exception as e:      # any failure: mark down, back off, reconnect
                self.connected = self.status["connected"] = False
                self.status["last_disconnect"] = {"at": datetime.now(IST).isoformat(), "error": repr(e)[:300]}
                self.status["reconnects"] += 1
                self._stop.wait(backoff)
                backoff = min(30.0, backoff * 2)


# ------------------------------------------------------------------ session

@dataclass
class Command:
    action: str
    confirm: str
    client: str
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None


class LiveSession:
    def __init__(self, runner: LiveRunner, feed, mode: str, report_path: Path, state_dir: Path,
                 broker_name: str, publish_every: float = 0.5, reconcile_every: float = 5.0):
        assert mode in ("PAPER", "LIVE")
        self.runner, self.feed, self.mode = runner, feed, mode
        self.report_path, self.state_dir = Path(report_path), Path(state_dir)
        self.broker_name = broker_name
        self.publish_every, self.reconcile_every = publish_every, reconcile_every
        self.ticks: queue.Queue = queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.armed = False
        self.armed_at: str | None = None
        self.started_at = datetime.now(IST).isoformat()
        self.heartbeat = _time.monotonic()
        self.snapshot_json = "{}"
        self.snapshot_seq = 0
        self.snapshot_cond = threading.Condition()
        self.engine_error: str | None = None
        self._stop = threading.Event()
        self._last_reconcile = 0.0
        self._last_publish = 0.0
        self._last_tick_ts: datetime | None = None
        self.control_log: list[dict] = []
        self._set_arm(False)

    # ---------------- engine thread
    def run(self, until: datetime | None = None) -> None:
        if hasattr(self.feed, "backlog"):
            self.feed.backlog = self.ticks.qsize
        self.feed.start(self.ticks.put)
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                # the engine must never die silently: record, trip the kill switch, keep publishing
                self.engine_error = traceback.format_exc()[-2000:]
                now = self.now()
                self.runner.kill.trip("ENGINE_EXCEPTION", now, {"trace": self.engine_error[-500:]})
                try:
                    self.runner._emergency(now, "ENGINE_EXCEPTION")
                except Exception:
                    pass
                self._publish(force=True)
            now = self.now()
            if until is not None and now >= until:
                break
            if getattr(self.feed, "done", False) and self.ticks.empty():
                self._publish(force=True)
                if until is None:
                    _time.sleep(0.2)
                    continue
                break
            _time.sleep(0.05)
        self.feed.stop()

    def stop(self) -> None:
        self._stop.set()

    def now(self) -> datetime:
        """Live: the wall clock. Replay: the time of the last tick the ENGINE has processed,
        so a replay never shows data as older than it is to the engine."""
        if self.feed.kind == "REPLAY" and self._last_tick_ts is not None and not getattr(self.feed, "done", False):
            return self._last_tick_ts
        return self.feed.now()

    def step(self) -> None:
        r = self.runner
        self.heartbeat = _time.monotonic()
        while True:
            try:
                cmd = self.commands.get_nowait()
            except queue.Empty:
                break
            cmd.result = self._execute(cmd)
            cmd.done.set()
        n = 0
        while n < 5000:
            try:
                t = self.ticks.get_nowait()
            except queue.Empty:
                break
            r.on_tick(t)
            self._last_tick_ts = t.ts
            n += 1
        r.feed_connected = bool(self.feed.connected)
        now = self.now()
        r.on_clock(now)
        if self.armed and (r.kill.tripped or r.sm.state.value in ("KILLED", "DAY_DONE")):
            self._set_arm(False, reason="auto-disarm: " + (r.kill.reason() or r.sm.state.value), now=now)
        mono = _time.monotonic()
        if mono - self._last_reconcile >= self.reconcile_every:
            self._last_reconcile = mono
            r.reconcile(now)
        if mono - self._last_publish >= self.publish_every:
            self._publish()

    # ---------------- controls (executed on the engine thread only)
    def gate(self):
        return live_allowed(self.report_path, self.runner.cfg.config_hash(), "TINY_LIVE")

    def arm_phrase(self) -> str:
        return ARM_PHRASE_LIVE.format(hash=self.runner.cfg.config_hash()) if self.mode == "LIVE" else ARM_PHRASE_PAPER

    def arm_blockers(self, now: datetime) -> list[str]:
        r = self.runner
        out = []
        if self.mode == "LIVE":
            g = self.gate()
            if not g.allowed:
                out += [f"validation gate: {f}" for f in g.failures]
        if r.kill.tripped:
            out.append(f"kill switch tripped ({r.kill.reason()}); delete the lock file to reset")
        if r.sm.state.value in ("KILLED", "DAY_DONE"):
            out.append(f"engine state is {r.sm.state.value}")
        if not self.feed.connected:
            out.append("market feed not connected")
        if r.last_index_tick is None or (now - r.last_index_tick).total_seconds() > 5:
            out.append("no fresh SENSEX tick in the last 5 s")
        if now.time() >= r.cfg.session.last_entry:
            out.append("past the last entry time")
        if not r.is_expiry:
            out.append("today is not a SENSEX weekly expiry day (the engine would not trade anyway)")
        return out

    def _set_arm(self, on: bool, reason: str = "", now: datetime | None = None) -> None:
        self.armed = on
        self.armed_at = (now or datetime.now(IST)).isoformat() if on else None
        self.runner.live_gate_ok = on
        if hasattr(self.runner.broker, "armed"):
            self.runner.broker.armed = on
        if reason:
            self.runner.audit.write("CONTROL", now or datetime.now(IST), {"action": "ARM" if on else "DISARM",
                                                                         "reason": reason})

    def _execute(self, cmd: Command) -> dict:
        r = self.runner
        now = self.now()
        act = cmd.action.upper()
        entry = {"ts": now.isoformat(), "action": act, "client": cmd.client}
        if act == "ARM":
            if self.armed:
                res = {"ok": True, "message": "already armed"}
            elif cmd.confirm.strip() != self.arm_phrase():
                res = {"ok": False, "message": f"type exactly: {self.arm_phrase()}"}
            else:
                blockers = self.arm_blockers(now)
                if blockers:
                    res = {"ok": False, "message": "ARM refused", "blockers": blockers}
                else:
                    self._set_arm(True, reason=f"armed from dashboard ({cmd.client})", now=now)
                    res = {"ok": True, "message": f"ARMED ({self.mode})"}
        elif act == "DISARM":
            self._set_arm(False, reason=f"disarmed from dashboard ({cmd.client})", now=now)
            res = {"ok": True, "message": "DISARMED: no new entries; open positions are still managed and exited"}
        elif act == "PAUSE":
            r.entries_paused = True
            r.audit.write("CONTROL", now, {"action": "PAUSE", "client": cmd.client})
            res = {"ok": True, "message": "new entries paused; exits continue"}
        elif act == "RESUME":
            r.entries_paused = False
            r.audit.write("CONTROL", now, {"action": "RESUME", "client": cmd.client})
            res = {"ok": True, "message": "entries resumed"}
        elif act == "SQUAREOFF":
            if cmd.confirm.strip() != SQUARE_OFF_PHRASE:
                res = {"ok": False, "message": f"type exactly: {SQUARE_OFF_PHRASE}"}
            else:
                r.audit.write("CONTROL", now, {"action": "EMERGENCY_SQUARE_OFF", "client": cmd.client})
                r.kill.trip("MANUAL_SQUARE_OFF", now, {"client": cmd.client})
                r._emergency(now, "MANUAL_SQUARE_OFF")
                self._set_arm(False, reason="square-off", now=now)
                res = {"ok": True, "message": "kill switch tripped: orders cancelled, positions exiting, engine locked"}
        else:
            res = {"ok": False, "message": f"unknown action {act}"}
        self.control_log.append(entry | {"result": res})
        self._publish(force=True)
        return res

    def submit(self, action: str, confirm: str, client: str, timeout: float = 5.0) -> dict:
        """Called from the HTTP thread. Commands run on the engine thread; if the engine
        loop is hung, SQUARE-OFF falls back to acting on the broker directly."""
        cmd = Command(action, confirm or "", client)
        stale = _time.monotonic() - self.heartbeat > 3.0
        if stale and action.upper() == "SQUAREOFF" and (confirm or "").strip() == SQUARE_OFF_PHRASE:
            now = datetime.now(IST)
            self.runner.kill.trip("MANUAL_SQUARE_OFF_ENGINE_HUNG", now, {"client": client})
            res = [x.status.value for x in self.runner.broker.square_off_all()]
            return {"ok": True, "message": "engine loop unresponsive: broker square-off sent directly", "orders": res}
        self.commands.put(cmd)
        if not cmd.done.wait(timeout):
            return {"ok": False, "message": "engine did not respond within timeout"}
        return cmd.result or {"ok": False, "message": "no result"}

    # ---------------- snapshot
    def _publish(self, force: bool = False) -> None:
        self._last_publish = _time.monotonic()
        try:
            snap = self.snapshot()
        except Exception:
            snap = {"error": traceback.format_exc()[-1500:]}
        with self.snapshot_cond:
            self.snapshot_seq += 1
            snap["meta"] = snap.get("meta", {}) | {"seq": self.snapshot_seq}
            self.snapshot_json = json.dumps(snap, default=str)
            self.snapshot_cond.notify_all()

    def snapshot(self) -> dict:
        r, cfg = self.runner, self.runner.cfg
        now = self.now()
        bars = r.builder.closed
        spot_tick = r.validator.last.get(r.index_id)
        orng = opening_range(bars, r.session_open, cfg.features.or_minutes) if bars else None
        levels = {}
        if r.prior:
            levels |= {"PDH": r.prior.high, "PDL": r.prior.low, "PDC": r.prior.close}
        if orng:
            levels |= {"ORH": orng[0], "ORL": orng[1]}
        rs = r.engine.risk_state
        risk_budget = (r.capital or cfg.risk.capital) * cfg.risk.risk_per_trade_pct
        opt_ages = [(now - t.recv_ts).total_seconds() * 1000 for t in r.quotes.values()]
        pos = None
        unreal = 0.0
        if r.pos is not None:
            p = r.pos
            sid = r.option_ids.get((p.strike, p.right))
            q = r.quotes.get(sid) if sid else None
            mark = (q.bid or q.ltp) if q else None
            unreal = ((mark - p.entry_price) * p.qty) if mark else 0.0
            pos = {"instrument": f"SENSEX {r.expiry} {p.strike} {p.right}", "setup": p.setup, "direction": p.direction.value,
                   "qty": p.qty, "entry": p.entry_price, "entry_ts": p.entry_ts.isoformat(), "mark": mark,
                   "ltp": q.ltp if q else None, "stop": p.stop, "initial_stop": p.initial_stop,
                   "invalidation": round(p.invalidation, 2), "target": None if cfg.exits.policy in ("TRAIL", "TIME_ONLY")
                   else cfg.exits.policy, "exit_policy": cfg.exits.policy, "one_r": round(p.one_r, 2),
                   "upnl_rs": round(unreal, 2), "upnl_r": round(unreal / p.one_r, 3) if p.one_r else None,
                   "mfe_r": round(p.r_at(p.peak_premium), 3), "bars_held": p.bars_held,
                   "pending_exit": p.pending_exit.value if p.pending_exit else None}
        realized = sum(t["net"] for t in r.closed_trades)
        open_orders = []
        try:
            open_orders = [{"order_id": o.order_id, "side": o.request.side if o.request else None,
                            "type": o.request.order_type if o.request else None,
                            "price": o.request.price if o.request else None,
                            "trigger": o.request.trigger_price if o.request else None,
                            "qty": o.request.qty if o.request else None, "status": o.status.value}
                           for o in r.broker.open_orders()]
            broker_ok, broker_err = True, None
        except Exception as e:
            broker_ok, broker_err = False, repr(e)[:300]
        sl = None
        if r.sl_order_id:
            try:
                st = r.broker.status(r.sl_order_id)
                sl = {"order_id": r.sl_order_id, "status": st.status.value}
            except Exception as e:
                sl = {"order_id": r.sl_order_id, "status": "UNKNOWN", "error": repr(e)[:200]}
        g = self.gate() if self.mode == "LIVE" else None
        dec = r.last_decision or {}
        return {
            "meta": {"engine_version": cfg.version, "config_hash": cfg.config_hash(), "mode": self.mode,
                     "feed": self.feed.kind, "broker": self.broker_name, "day": str(r.day), "expiry": str(r.expiry),
                     "is_expiry": r.is_expiry, "lot_size": r.lot_size, "now": now.isoformat(),
                     "started_at": self.started_at, "engine_error": self.engine_error,
                     "replay_speed": getattr(self.feed, "speed", None)},
            "control": {"armed": self.armed, "armed_at": self.armed_at, "paused": r.entries_paused,
                        "kill": {"tripped": r.kill.tripped, "reason": r.kill.reason(), "lock_file": str(r.kill.lock_path)},
                        "arm_phrase": self.arm_phrase(), "squareoff_phrase": SQUARE_OFF_PHRASE,
                        "arm_blockers": self.arm_blockers(now),
                        "gate": None if g is None else {"allowed": g.allowed, "failures": list(g.failures)},
                        "log": self.control_log[-20:]},
            "engine": {"state": r.sm.state.value,
                       "transitions": [{"ts": t.isoformat(), "from": a.value, "to": b.value, "why": w}
                                       for t, a, b, w in r.sm.history[-12:]]},
            "market": {"spot": spot_tick.ltp if spot_tick else None,
                       "spot_ts": spot_tick.ts.isoformat() if spot_tick else None,
                       "bars": [[c.start.strftime("%H:%M"), c.close] for c in bars], "levels": levels,
                       "regime": dec.get("regime"), "atr": dec.get("atr")},
            "decision": dec, "last_buy": r.last_buy_decision,
            "position": pos,
            "pnl": {"realized_rs": round(realized, 2), "realized_r": round(rs.realized_r, 3),
                    "unrealized_rs": round(unreal, 2), "total_rs": round(realized + unreal, 2),
                    "trades": len(r.closed_trades)},
            "risk": {"trades_used": rs.trades, "max_trades": cfg.risk.max_trades_per_day,
                     "daily_r": round(rs.realized_r, 3), "max_daily_loss_r": cfg.risk.max_daily_loss_r,
                     "consecutive_losses": rs.consecutive_losses, "max_consecutive": cfg.risk.max_consecutive_losses,
                     "open_positions": rs.open_positions, "max_open": cfg.risk.max_open_positions,
                     "risk_per_trade_rs": round(risk_budget, 2), "capital": r.capital or cfg.risk.capital,
                     "kill_loss_rs": r.kill.max_daily_loss,
                     "cooldown_until": (rs.last_exit + timedelta(minutes=cfg.risk.cooldown_minutes)).isoformat()
                     if rs.last_exit else None},
            "data": {"quality": (r.last_quality or {}).get("data_quality"),
                     "quality_reasons": (r.last_quality or {}).get("quality_reasons"),
                     "index_tick_age_ms": int((now - r.last_index_tick).total_seconds() * 1000) if r.last_index_tick else None,
                     "option_quote_age_ms": int(min(opt_ages)) if opt_ages else None,
                     "option_quotes": len(r.quotes), "candles": len(bars),
                     "missing_minutes": len(r.builder.gaps), "late_ticks": r.builder.late_ticks,
                     "duplicates": r.validator.duplicates, "out_of_order": r.validator.out_of_order,
                     "rejected_jumps": r.validator.rejected_jumps, "invalid": r.validator.invalid,
                     "max_underlying_age_ms": cfg.data.max_underlying_age_ms},
            "connection": dict(self.feed.status) | {"broker_ok": broker_ok, "broker_error": broker_err},
            "orders": {"sl": sl, "exit": {"order_id": r.exit_order_id, "attempts": r.exit_attempts,
                                          "reason": r.exit_reason.value if r.exit_reason else None},
                       "open": open_orders, "last_event": r.last_order_event},
            "reasons": {"last": dec.get("reason_codes", []),
                        "counts": dict(sorted(r.reason_counts.items(), key=lambda kv: -kv[1]))},
            "trades": r.closed_trades,
            "audit": {"path": str(r.audit.path)},
        }


# ------------------------------------------------------------------ bootstrap

class BootHolder:
    """Stands in for a session until bootstrap succeeds, so the dashboard can say why."""

    def __init__(self, cfg: EngineConfig, mode: str):
        self.snapshot_cond = threading.Condition()
        self.snapshot_seq = 0
        self.heartbeat = _time.monotonic()
        self.cfg, self.mode = cfg, mode
        self.fail("starting...", 0)

    def fail(self, error: str, retry_in: int) -> None:
        with self.snapshot_cond:
            self.snapshot_seq += 1
            self.snapshot_json = json.dumps({"boot": {"error": error, "retry_in_s": retry_in,
                                                      "at": datetime.now(IST).isoformat()},
                                             "meta": {"seq": self.snapshot_seq, "mode": self.mode,
                                                      "config_hash": self.cfg.config_hash(),
                                                      "engine_version": self.cfg.version}})
            self.snapshot_cond.notify_all()

    def submit(self, action, confirm, client, timeout=5.0):
        return {"ok": False, "message": "engine not running (bootstrap failed or in progress)"}

def build_replay(cfg: EngineConfig, day_file: Path, state_dir: Path, speed: float, start_at: str | None, capital: float):
    from .history import load_day
    d = load_day(day_file)
    option_ids = {(k, r): f"{k}{r}" for (k, r) in d.options}
    feed = ReplayFeed(d, speed=speed, start_at=start_at)
    holder = {}
    broker = PaperBroker(cfg, capital, lambda sid: holder["q"](sid))
    runner = _runner(cfg, d.day, d.prior, d.is_expiry, d.expiry, d.lot_size, "SENSEX", option_ids, broker,
                     state_dir, capital)
    holder["q"] = lambda sid: ((runner.quotes[sid].bid, runner.quotes[sid].ask) if sid in runner.quotes else None)
    return runner, feed, "PaperBroker"


def _runner(cfg, day, prior, is_exp, expiry, lot, index_id, option_ids, broker, state_dir: Path, capital):
    """state_dir holds the kill-switch lock (persists across days until a human deletes it)
    and one hash-chained audit file per day."""
    state_dir.mkdir(parents=True, exist_ok=True)
    kill = KillSwitch(state_dir / "KILL_SWITCH.lock", max_daily_loss_rupees=3 * capital * cfg.risk.risk_per_trade_pct,
                      max_qty=cfg.risk.max_lots * max(lot, 1))
    audit = AuditLog(state_dir / "audit" / f"audit_{day}.jsonl")
    return LiveRunner(cfg, day, prior, is_exp, expiry, lot, index_id, option_ids, broker, kill, audit,
                      live_gate_ok=False, capital=capital)


def build_dhan(cfg: EngineConfig, mode: str, state_dir: Path, capital: float, strikes_each_side: int = 15):
    """Live bootstrap from Dhan. Any failure raises with a plain reason (shown on the dashboard)."""
    import os
    import urllib.request
    from .dhan_adapter import SCRIP_MASTER_URL, DhanBroker, _client, _ok, parse_scrip_master
    cid, tok = os.environ.get("DHAN_CLIENT_ID"), os.environ.get("DHAN_ACCESS_TOKEN")
    if not cid or not tok:
        raise RuntimeError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN are not set")
    today = datetime.now(IST).date()
    c = _client(cid, tok)
    dhan = DhanBroker(cid, tok, armed=False, client=c)
    expiries = set(dhan.expiries())
    exp = min((e for e in expiries if e >= today), default=None)
    holidays = frozenset()
    rule_exp = weekly_expiry_for(today, holidays)
    is_exp = exp == today and rule_exp == today
    csv_path = state_dir / "scrip_master.csv"
    state_dir.mkdir(parents=True, exist_ok=True)
    if not csv_path.exists() or datetime.fromtimestamp(csv_path.stat().st_mtime).date() != today:
        with urllib.request.urlopen(SCRIP_MASTER_URL, timeout=60) as resp:
            csv_path.write_bytes(resp.read())
    master = parse_scrip_master(csv_path.read_text(errors="replace"), exp or today)
    index_id = master["index_id"]
    if not index_id:
        raise RuntimeError("SENSEX index id not found in the scrip master")
    lots = {lot for _, lot in master["options"].values()}
    if len(lots) > 1:
        raise RuntimeError(f"inconsistent lot sizes in scrip master: {lots}")
    lot = lots.pop() if lots else 0
    if exp and not master["options"]:
        raise RuntimeError(f"no SENSEX option contracts found for expiry {exp}")
    q = _ok(c.ticker_data({"IDX_I": [int(index_id)]})) or {}
    spot = None
    try:
        spot = float(q["IDX_I"][str(index_id)]["last_price"])
    except (KeyError, TypeError, ValueError):
        pass
    if spot is None:
        raise RuntimeError("could not read SENSEX LTP to centre the option band")
    atm = int(round(spot / 100) * 100)
    band = {k: v for k, v in master["options"].items() if abs(k[0] - atm) <= strikes_each_side * 100}
    option_ids = {k: str(v[0]) for k, v in band.items()}
    daily = _ok(c.historical_daily_data(index_id, "IDX_I", "INDEX", str(today - timedelta(days=10)),
                                        str(today - timedelta(days=1)))) or {}
    prior = None
    if daily.get("timestamp"):
        from .models import PriorDay
        prior = PriorDay(datetime.fromtimestamp(daily["timestamp"][-1], IST).date(), float(daily["high"][-1]),
                         float(daily["low"][-1]), float(daily["close"][-1]))
    if mode == "LIVE":
        broker, name = dhan, "DhanBroker"
    else:
        holder = {}
        broker, name = PaperBroker(cfg, capital, lambda sid: holder["q"](sid)), "PaperBroker (Dhan market data)"
    runner = _runner(cfg, today, prior, is_exp, exp, lot, str(index_id), option_ids, broker, state_dir, capital)
    if mode != "LIVE":
        holder["q"] = lambda sid: ((runner.quotes[sid].bid, runner.quotes[sid].ask) if sid in runner.quotes else None)
    feed = DhanFeed(cid, tok, str(index_id), list(option_ids.values()))
    return runner, feed, name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sensex_expiry.session")
    ap.add_argument("--feed", choices=("dhan", "replay"), required=True)
    ap.add_argument("--mode", choices=("paper", "live"), default="paper")
    ap.add_argument("--day", type=Path, help="replay: one day file from realdata build")
    ap.add_argument("--speed", type=float, default=30.0, help="replay speed multiple")
    ap.add_argument("--start-at", help="replay: start time HH:MM")
    ap.add_argument("--capital", type=float, default=EngineConfig().risk.capital)
    ap.add_argument("--state-dir", type=Path, default=Path("data/sensex_expiry/live"))
    ap.add_argument("--report", type=Path, default=Path("reports/validation_gate_report.json"),
                    help="validation report the live gate checks before ARM")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-remote", action="store_true",
                    help="bind beyond localhost; the control token is then NOT embedded in the page")
    ap.add_argument("--open-browser", action="store_true")
    ap.add_argument("--exit-after", default="15:40",
                    help="live feeds: stop the process at this IST time (positions are flat by 15:10)")
    a = ap.parse_args(argv)
    if a.feed == "replay" and a.mode == "live":
        print("replay feeds are paper-only", file=sys.stderr)
        return 2
    if a.host not in ("127.0.0.1", "localhost") and not a.allow_remote:
        print("binding beyond localhost needs --allow-remote", file=sys.stderr)
        return 2
    cfg = EngineConfig()
    if a.feed == "replay" and not a.day:
        print("--day is required for replay", file=sys.stderr)
        return 2
    from .dashboard import DashboardServer
    token = secrets.token_urlsafe(24)
    holder = BootHolder(cfg, a.mode.upper())
    srv = DashboardServer(holder, a.host, a.port, token, embed_token=not a.allow_remote)
    srv.start()
    a.state_dir.mkdir(parents=True, exist_ok=True)
    (a.state_dir / "dashboard_url.txt").write_text(srv.url + "\n")
    tok_file = a.state_dir / "dashboard_token.txt"
    tok_file.write_text(token + "\n")
    tok_file.chmod(0o600)
    print(f"dashboard: {srv.url}   (control token in {tok_file})", flush=True)
    if a.open_browser:
        import webbrowser
        webbrowser.open(srv.url)
    try:
        while True:                      # bootstrap failures are shown on the dashboard and retried
            try:
                if a.feed == "replay":
                    rdir = a.state_dir / "replay" / f"{a.day.stem}_{datetime.now(IST):%H%M%S}"
                    runner, feed, bname = build_replay(cfg, a.day, rdir, a.speed, a.start_at, a.capital)
                else:
                    runner, feed, bname = build_dhan(cfg, a.mode.upper(), a.state_dir, a.capital)
                break
            except Exception as e:
                holder.fail(repr(e)[:500], retry_in=60)
                _time.sleep(60)
        sess = LiveSession(runner, feed, a.mode.upper(), a.report, a.state_dir, bname)
        srv.session = sess
        until = None
        if a.feed == "dhan":
            hh, mm = (int(x) for x in a.exit_after.split(":"))
            until = datetime.now(IST).replace(hour=hh, minute=mm, second=0, microsecond=0)
        sess.run(until=until)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
