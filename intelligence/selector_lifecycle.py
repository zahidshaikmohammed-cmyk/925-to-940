"""Automated daily lifecycle for 945 (restartable, idempotent).

    09:45:03  DECIDE   fetch -> validate feed -> freeze 09:45 -> decide -> persist decision,
                       full feature matrix, full ranking and the exact frozen input file in
                       ONE transaction -> publish -> beep
    09:51+    OUTCOME  +5m  (each horizon once its window is complete and published)
    10:01+    OUTCOME  +15m
    10:16+    OUTCOME  +30m -> universe forward returns + feature/score IC -> daily report
    15:31+    ARCHIVE  full-session feed saved for future backtests; day complete

Every phase first checks the database, so a crash/restart/reboot resumes where it left
off and never duplicates or alters anything already stored. The decision row is never
touched after insertion (database triggers forbid it).
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from .selector_945 import decide, render
from .selector_config import MODEL_ID, SelectorConfig
from .selector_data import IST, freeze_information_set, load_session_file, parse_payload
from .selector_feed import fetch_payloads, render_health, validate_feed
from .selector_outcomes import evaluate_decision
from .selector_research import record_universe_research, write_daily_report, write_summary_csv
from .selector_store import DecisionExists, Store

GIVE_UP_DECISION = time(15, 25)


@dataclass
class Clock:
    now: object
    sleep: object


class Lifecycle:
    def __init__(self, cfg: SelectorConfig, weights: dict, db_path: str, base_url: str,
                 sectors: dict, sector_source: str, is_trading_day, clock: Clock,
                 fetch=fetch_payloads, beep=lambda: None, out=print, calibration_db: str | None = None):
        self.cfg, self.weights, self.base_url = cfg, weights, base_url
        self.store = Store(db_path)
        self.history_store = Store(calibration_db) if calibration_db else self.store
        self.sectors, self.sector_source = sectors, sector_source
        self.is_trading_day, self.clock, self.fetch, self.beep, self.out = is_trading_day, clock, fetch, beep, out

    # ------------------------------------------------------------ helpers
    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _cutoff(self) -> datetime:
        return datetime.combine(self.clock.now().date(), self.cfg.cutoff, IST)

    def _wait_until(self, t: datetime) -> None:
        while self.clock.now() < t:
            self.clock.sleep(min(30.0, max(0.05, (t - self.clock.now()).total_seconds())))

    def decision(self):
        return self.store.get_decision(self._today(), "live", MODEL_ID)

    def _fetch_raw(self):
        stocks, index = self.fetch(self.base_url)
        return stocks, index, parse_payload(stocks, index, source=f"{self.base_url}/public/live.json")

    # ------------------------------------------------------------ DECIDE
    def publish_decision(self, give_up: time = GIVE_UP_DECISION):
        day = self._today()
        existing = self.decision()
        if existing:
            return existing
        self._wait_until(self._cutoff() + timedelta(seconds=3))
        attempt_until = self.clock.now() + timedelta(seconds=self.cfg.fetch_retry_window_seconds)
        last_problem = None
        while True:
            now = self.clock.now()
            try:
                stocks, index, raw = self._fetch_raw()
                si = freeze_information_set(raw, self.cfg.cutoff)
                health = validate_feed(raw, si, now, self.cfg, self.is_trading_day)
                self.store.log_feed_check(day, health.ok, health.to_dict())
                if health.ok:
                    break
                last_problem = "; ".join(health.failures())
                diag = render_health(health)
            except Exception as exc:
                last_problem, diag = f"feed unavailable: {exc}", None
                self.store.log_feed_check(day, False, {"error": str(exc), "local_time": now.isoformat()})
            if now >= attempt_until:
                self.out(f"[{now:%H:%M:%S}] NO DECISION PUBLISHED -- feed failed validation: {last_problem}")
                if diag:
                    self.out(diag)
                self.store.log_event(day, "decision_blocked", last_problem)
                if now.time() >= give_up:
                    return None
                attempt_until = now + timedelta(seconds=self.cfg.fetch_retry_window_seconds)
                self.clock.sleep(30)
                continue
            self.clock.sleep(self.cfg.fetch_retry_seconds)

        # Exact frozen input preserved for reproducibility audits.
        sess = Path(self.cfg.sessions_dir)
        sess.mkdir(parents=True, exist_ok=True)
        blob = gzip.compress(json.dumps({"captured_at": now.isoformat(), "stocks_payload": stocks,
                                         "index_payload": index}, separators=(",", ":")).encode(), compresslevel=5)
        in_path = sess / f"{day}.0945-input.json.gz"
        if not in_path.exists():
            in_path.write_bytes(blob)
        input_record = {"path": str(in_path), "sha256": hashlib.sha256(in_path.read_bytes()).hexdigest(),
                        "bytes": in_path.stat().st_size}

        baselines = self.store.baselines(day, self.cfg.baseline_lookback_days, self.cfg.baseline_min_days)
        history = self.history_store.history(before=day)
        snap, inner = decide(si, self.cfg, self.weights, "live", self.sectors, baselines, history,
                             published_at=self.clock.now(), feed_health=health.to_dict(),
                             sector_source=self.sector_source)
        try:
            self.store.save_decision(snap, inner["feature_rows"], inner["ranking_rows"], input_record)
        except DecisionExists:
            return self.decision()
        self.store.save_baselines(day, {s: (f.get("cumulative_volume"), f.get("realized_vol_pct"))
                                        for s, f in inner["table"].features.items() if f})
        self.store.log_event(day, "decision_published",
                             f"{snap.selected_symbol} {snap.direction} score {snap.selection_score} "
                             f"lag {snap.publication_lag_seconds}s fp {snap.decision_fingerprint[:16]}")
        self.out(render(snap))
        self.beep()
        return snap

    # ------------------------------------------------------------ OUTCOMES
    def pending_horizons(self, snap) -> list[int]:
        done = self.store.outcomes(snap.decision_id)
        return [h for h in self.cfg.horizons_min if h not in done]

    def due_time(self, h: int) -> datetime:
        return self._cutoff() + timedelta(minutes=h, seconds=self.cfg.outcome_buffer_seconds)

    def record_outcomes(self, snap, raw=None, final: bool = False) -> int:
        day = snap.decision_date
        if raw is None:
            _, _, raw = self._fetch_raw()
        if raw.session_date.isoformat() != day:
            raise RuntimeError(f"feed session {raw.session_date} != decision date {day}")
        d = snap.to_dict()
        outs = evaluate_decision(d, raw, self.cfg.horizons_min, self.cfg.move_threshold_pct,
                                 self.cfg.prob_threshold_pct, final=final)
        added = self.store.save_outcomes(snap.decision_id, outs)
        record_universe_research(self.store, snap, raw, self.cfg, self.weights)
        if added:
            self.store.log_event(day, "outcomes_recorded", f"{added} horizon(s); "
                                 + ", ".join(f"{o.horizon_min}m {o.outcome}" for o in outs if o.complete))
        if not self.pending_horizons(snap):
            self._report(snap)
        return added

    def _report(self, snap) -> None:
        path = write_daily_report(self.store, snap, self.cfg)
        write_summary_csv(self.store, Path(self.cfg.reports_dir) / "daily_summary.csv")
        if not any(e[1] == "report_written" for e in self.store.events(snap.decision_date)):
            self.store.log_event(snap.decision_date, "report_written", str(path))
            self.out(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ ARCHIVE
    def archive_path(self, day: str) -> Path:
        return Path(self.cfg.sessions_dir) / f"{day}.json.gz"

    def archive_session(self, stocks=None, index=None) -> Path:
        day = self._today()
        if stocks is None:
            stocks, index = self.fetch(self.base_url)
        raw = parse_payload(stocks, index)
        if raw.session_date.isoformat() != day:
            raise RuntimeError(f"feed session {raw.session_date} is not today ({day}); archive skipped")
        p = self.archive_path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(gzip.compress(json.dumps({"archived_at": self.clock.now().isoformat(),
                                                "stocks_payload": stocks, "index_payload": index},
                                               separators=(",", ":")).encode(), compresslevel=5))
        bars = max((len(s) for s in raw.stocks.values()), default=0)
        self.store.log_event(day, "session_archived", f"{p} | {len(raw.stocks)} stocks | {bars} bars")
        return p

    # ------------------------------------------------------------ the whole day
    def run_day(self) -> int:
        now = self.clock.now()
        day = self._today()
        if not self.is_trading_day(now.date()):
            self.out(f"{day} is not an NSE trading day -- nothing to do.")
            return 0
        self.store.log_event(day, "lifecycle_start", f"pid-start {now.isoformat()}")
        snap = self.publish_decision()
        if snap is None:
            return 31
        # Outcomes: wake at each due time; retry feed problems every 30 s.
        close = datetime.combine(now.date(), self.cfg.archive_after, IST)
        while self.pending_horizons(snap):
            pending = self.pending_horizons(snap)
            due = min(self.due_time(h) for h in pending)
            if self.clock.now() < due:
                self._wait_until(due)
            final = self.clock.now() >= close
            try:
                self.record_outcomes(snap, final=final)
            except Exception as exc:
                self.store.log_event(day, "outcome_error", str(exc))
                if final:
                    break
                self.clock.sleep(30)
                continue
            if self.pending_horizons(snap) and not final:
                nxt = min(self.due_time(h) for h in self.pending_horizons(snap))
                if self.clock.now() >= nxt:
                    self.clock.sleep(30)       # window due but not yet complete in the feed
        # Archive the full session once the market has closed.
        if not self.archive_path(day).exists():
            self._wait_until(close)
            for _ in range(10):
                try:
                    self.out(f"ARCHIVED {self.archive_session()}")
                    break
                except Exception as exc:
                    self.store.log_event(day, "archive_error", str(exc))
                    self.clock.sleep(60)
        self.store.log_event(day, "lifecycle_complete", "")
        return 0

    # ------------------------------------------------------------ status / health
    def status(self) -> tuple[int, str]:
        day = self._today()
        now = self.clock.now()
        snap = self.decision()
        lines = [f"945 STATUS {day} {now:%H:%M:%S} IST"]
        code = 0
        if not self.is_trading_day(now.date()):
            return 0, "\n".join(lines + ["not a trading day"])
        if snap:
            lines.append(f"decision : {snap.selected_symbol} {snap.direction} score {snap.selection_score:.1f} "
                         f"({snap.probability_status.split(' ')[0]}) published {snap.published_at}")
            outs = self.store.outcomes(snap.decision_id)
            for h in self.cfg.horizons_min:
                o = outs.get(h)
                due = self.due_time(h)
                state = (f"{o['outcome']} {o['forward_return_pct']:+.3f}%" if o and o["forward_return_pct"] is not None
                         else (o["outcome"] if o else ("OVERDUE" if now > due + timedelta(minutes=10) else "pending")))
                if state == "OVERDUE":
                    code = 2
                lines.append(f"+{h:>2}m     : {state}")
        else:
            late = now > self._cutoff() + timedelta(minutes=5)
            lines.append("decision : " + ("MISSING (overdue)" if late else "not yet (publishes 09:45:03)"))
            code = 2 if late else 0
        archive = self.archive_path(day)
        lines.append(f"archive  : {'saved ' + str(archive) if archive.exists() else 'pending (after 15:31)'}")
        last = self.store.db.execute("SELECT checked_at, ok FROM feed_checks ORDER BY id DESC LIMIT 1").fetchone()
        lines.append(f"feed     : last check {last[0]} {'OK' if last[1] else 'FAILED'}" if last else "feed     : never checked")
        for at, ev, detail in self.store.events(day)[-6:]:
            lines.append(f"event    : {at[11:19]} {ev} {detail[:90]}")
        return code, "\n".join(lines)

    def verify(self, day: str) -> tuple[bool, str]:
        """Reproducibility audit: re-decide from the stored frozen input and compare fingerprints."""
        snap = self.store.get_decision(day, "live", MODEL_ID)
        if not snap:
            return False, f"no live decision for {day}"
        rec = self.store.decision_input(snap.decision_id)
        if not rec or not rec["path"] or not Path(rec["path"]).exists():
            return False, "frozen input file missing"
        if hashlib.sha256(Path(rec["path"]).read_bytes()).hexdigest() != rec["sha256"]:
            return False, "frozen input file was modified (sha256 mismatch)"
        raw = load_session_file(rec["path"])
        si = freeze_information_set(raw, self.cfg.cutoff)
        baselines = self.store.baselines(day, self.cfg.baseline_lookback_days, self.cfg.baseline_min_days)
        again, _ = decide(si, self.cfg, self.weights, "live", self.sectors, baselines,
                          self.history_store.history(before=day), sector_source=self.sector_source)
        ok = (again.input_fingerprint == snap.input_fingerprint and again.decision_fingerprint == snap.decision_fingerprint)
        return ok, (f"input {'MATCH' if again.input_fingerprint == snap.input_fingerprint else 'DIFFERENT'}, "
                    f"decision {'MATCH' if again.decision_fingerprint == snap.decision_fingerprint else 'DIFFERENT'} "
                    f"({snap.decision_fingerprint[:16]})")
