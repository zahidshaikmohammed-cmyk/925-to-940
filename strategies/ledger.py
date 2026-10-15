"""Duplicate suppression and signal lifecycle, persisted per day.

A signal is NEW (alert-worthy) only if all hold:
  1. its key engine|symbol|direction|anchor was never recorded today (same setup = same
     anchor: e.g. the same swept level and sweep bar, the same ORB day, the same climax bar);
  2. no earlier signal of the same engine, symbol and direction is still ACTIVE (PENDING
     entry or TRIGGERED trade) -- a continuing condition is not a new signal;
  3. the last signal of that engine, symbol and direction is older than the engine's
     COOLDOWN_MINUTES;
  4. no engine alerted the same stock in the last 30 minutes (one alert per stock; an
     opposite-direction alert inside that window is a conflict and is suppressed).
Closed trades carry r_multiple (gross, at modelled prices) and r_net = r_multiple - cost_r.

Lifecycle (evaluated only on bars that START at or after the signal time; for a stop-entry,
LONG shown, SHORT mirrored; where one bar touches both levels the stop is assumed first):
  PENDING  -> TRIGGERED when High >= entry          (same bar Low <= stop -> STOPPED)
  PENDING  -> INVALIDATED when Low <= stop first    (the setup broke before entry)
  PENDING  -> EXPIRED when the bar starts at/after valid_until without a trigger
  TRIGGERED-> STOPPED (Low <= stop) | TARGET (High >= target) | TIME_EXIT (at the open of the first
           bar starting >= trigger + max_hold_minutes (30), or >= 15:15, whichever is first)
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, time, timedelta
from pathlib import Path

from strategy_930 import IST

from .core import LONG, Signal

ACTIVE = ("PENDING", "TRIGGERED")
SYMBOL_GAP = timedelta(minutes=30)
SQUARE_OFF = time(15, 15)
MAX_HOLD_MINUTES = 30


def _iso(x: datetime) -> str:
    return x.astimezone(IST).isoformat()


class SignalLedger:
    def __init__(self, path: str | Path | None, cooldowns: dict[str, int] | None = None):
        self.path = Path(path) if path else None
        self.cooldowns = cooldowns or {}
        self.records: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self.records = json.loads(self.path.read_text(encoding="utf-8")).get("signals", {})
            except Exception as exc:
                # a corrupt ledger must not silently re-enable duplicates: keep it aside, start clean, say so
                bad = self.path.with_suffix(".corrupt")
                os.replace(self.path, bad)
                print(f"[LEDGER-WARN] unreadable ledger moved to {bad}: {exc}")

    # ------------------------------------------------------------------ dedup
    def decide(self, sig: Signal) -> tuple[bool, str]:
        if sig.key in self.records:
            return False, "duplicate_same_setup"
        same = [r for r in self.records.values()
                if r["engine"] == sig.engine and r["symbol"] == sig.symbol and r["direction"] == sig.direction]
        if any(r["state"] in ACTIVE for r in same):
            return False, "continuing_condition_active_signal"
        cd = timedelta(minutes=self.cooldowns.get(sig.engine, 30))
        for r in same:
            if sig.signal_time - datetime.fromisoformat(r["signal_time"]) < cd:
                return False, "within_cooldown"
        # across engines: never against an open trade, one alert per stock per SYMBOL_GAP,
        # never a flip inside it
        for r in self.records.values():
            if r["symbol"] != sig.symbol:
                continue
            if r["state"] in ACTIVE and r["direction"] != sig.direction:
                return False, "opposite_direction_trade_open"
            if sig.signal_time - datetime.fromisoformat(r["signal_time"]) < SYMBOL_GAP:
                if r["direction"] != sig.direction:
                    return False, "opposite_direction_alerted_recently"
                return False, "symbol_alerted_recently_by_" + r["engine"]
        return True, "new"

    # --------------------------------------------------------------- governor
    def open_count(self, direction: str) -> int:
        return sum(1 for r in self.records.values() if r["state"] in ACTIVE and r["direction"] == direction)

    def closed(self) -> list[dict]:
        done = [r for r in self.records.values() if r.get("r_net") is not None]
        return sorted(done, key=lambda r: (r["state_time"], r["key"]))

    def realised_r(self) -> float:
        return round(sum(r["r_net"] for r in self.closed()), 3)

    def losing_streak(self) -> int:
        n = 0
        for r in reversed(self.closed()):
            if r["r_net"] >= 0:
                break
            n += 1
        return n

    def record(self, sig: Signal) -> dict:
        rec = {
            "key": sig.key, "engine": sig.engine, "strategy": sig.strategy, "symbol": sig.symbol,
            "direction": sig.direction, "score": sig.score, "entry": sig.entry, "stop": sig.stop,
            "target": sig.target, "risk_pct": sig.risk_pct, "reward_r": sig.reward_r, "cost_r": sig.cost_r,
            "bar_time": _iso(sig.bar_time), "signal_time": _iso(sig.signal_time),
            "valid_until": _iso(sig.valid_until), "anchor": sig.anchor, "conditions": list(sig.conditions),
            "components": [asdict(c) for c in sig.components],
            "max_hold_minutes": MAX_HOLD_MINUTES, "triggered_at": None,
            "state": "PENDING", "state_time": None, "exit_price": None, "r_multiple": None, "r_net": None,
            "last_bar": None, "telegram": {"sent": False, "attempts": 0, "error": None, "message_id": None},
        }
        self.records[sig.key] = rec
        return rec

    # -------------------------------------------------------------- lifecycle
    def update(self, bars_by_symbol: dict) -> list[dict]:
        """Advance every active signal with newly completed bars. Returns changed records."""
        changed = []
        for rec in self.records.values():
            if rec["state"] not in ACTIVE:
                continue
            bars = bars_by_symbol.get(rec["symbol"])
            if not bars:
                continue
            start = datetime.fromisoformat(rec["signal_time"])
            last = datetime.fromisoformat(rec["last_bar"]) if rec["last_bar"] else None
            before = rec["state"]
            for b in bars:
                if b.ts < start or (last is not None and b.ts <= last):
                    continue
                self._step(rec, b)
                rec["last_bar"] = _iso(b.ts)
                if rec["state"] not in ACTIVE:
                    break
            if rec["state"] != before:
                changed.append(rec)
        return changed

    @staticmethod
    def _step(rec: dict, b) -> None:
        long = rec["direction"] == LONG
        entry, stop, target = rec["entry"], rec["stop"], rec["target"]
        risk = abs(entry - stop)
        hit_entry = b.high >= entry if long else b.low <= entry
        hit_stop = b.low <= stop if long else b.high >= stop
        hit_target = b.high >= target if long else b.low <= target

        def close(state, price):
            rec["state"], rec["state_time"], rec["exit_price"] = state, _iso(b.ts), price
            if state in ("STOPPED", "TARGET", "TIME_EXIT"):
                fill = rec.get("fill", entry)
                rec["r_multiple"] = round(((price - fill) if long else (fill - price)) / risk, 3)
                # net of the modelled round-trip cost (fees + slippage), already expressed in R
                rec["r_net"] = round(rec["r_multiple"] - rec.get("cost_r", 0.0), 3)

        if rec["state"] == "PENDING":
            if b.ts >= datetime.fromisoformat(rec["valid_until"]):
                close("EXPIRED", None)
                return
            if hit_entry:
                rec["state"], rec["state_time"] = "TRIGGERED", _iso(b.ts)
                rec["triggered_at"] = _iso(b.ts)
                # a gap through the entry fills at the open, not at the trigger
                fill = max(entry, b.open) if long else min(entry, b.open)
                rec["fill"] = fill
                if hit_stop:
                    close("STOPPED", stop)          # both touched in one bar: stop assumed first
                elif hit_target:
                    # the bar traded through entry and target without touching the stop
                    close("TARGET", max(target, b.open) if long else min(target, b.open))
                return
            if hit_stop:
                close("INVALIDATED", None)
            return
        # TRIGGERED
        hold = timedelta(minutes=rec.get("max_hold_minutes", MAX_HOLD_MINUTES))
        trig = datetime.fromisoformat(rec.get("triggered_at") or rec["state_time"])
        if b.ts.astimezone(IST).time() >= SQUARE_OFF or b.ts >= trig + hold:
            close("TIME_EXIT", b.open)
        elif hit_stop:
            close("STOPPED", min(stop, b.open) if long else max(stop, b.open))
        elif hit_target:
            close("TARGET", max(target, b.open) if long else min(target, b.open))   # gap fills at the open

    # ---------------------------------------------------------------- storage
    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"signals": self.records}, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    def summary(self) -> dict:
        out: dict = {}
        for r in self.records.values():
            e = out.setdefault(r["engine"], {"signals": 0, "states": {}, "r": []})
            e["signals"] += 1
            e["states"][r["state"]] = e["states"].get(r["state"], 0) + 1
            if r.get("r_net") is not None:
                e["r"].append(r["r_net"])
        return out
