"""SQLite research store (schema v2).

Immutability is enforced by the DATABASE, not by convention:

    decisions, decision_features, decision_rankings, decision_inputs, outcomes,
    universe_forward  ->  BEFORE UPDATE / BEFORE DELETE triggers RAISE(ABORT).

A decision and its full feature matrix, full ranking and frozen-input record are
written in ONE transaction (all or nothing). UNIQUE(decision_date, mode, model_id)
prevents a second decision for the same session. Outcomes are appended later and only
once per horizon (only final values are stored). Monitoring tables (feed_checks,
lifecycle_events) are append-only logs.

Research questions this schema answers after N sessions: per-feature IC
(decision_features x universe_forward), score/outcome relation (decision_rankings x
universe_forward), regime / liquidity / direction / horizon splits (decisions JSON),
selection concentration and stability (decisions), calibration (decisions x outcomes).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .selector_config import SCHEMA_VERSION
from .selector_snapshot import DecisionSnapshot, snapshot_from_dict

IST = ZoneInfo("Asia/Kolkata")

IMMUTABLE = ("decisions", "decision_features", "decision_rankings", "decision_inputs", "outcomes",
             "universe_forward")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    decision_date TEXT NOT NULL,
    mode TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    weights_hash TEXT NOT NULL,
    selected_symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('UP', 'DOWN')),
    selection_score REAL NOT NULL CHECK (selection_score >= 0 AND selection_score <= 100),
    estimated_probability REAL NOT NULL CHECK (estimated_probability > 0 AND estimated_probability < 1),
    probability_status TEXT NOT NULL,
    expected_horizon_min INTEGER NOT NULL,
    decision_fingerprint TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    published_at TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    UNIQUE (decision_date, mode, model_id)
);
CREATE TABLE IF NOT EXISTS decision_features (
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    symbol TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    exclusion_reason TEXT,
    features_json TEXT NOT NULL,
    PRIMARY KEY (decision_id, symbol)
);
CREATE TABLE IF NOT EXISTS decision_rankings (
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    rank INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    raw REAL NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (decision_id, rank)
);
CREATE TABLE IF NOT EXISTS decision_inputs (
    decision_id TEXT PRIMARY KEY REFERENCES decisions(decision_id),
    path TEXT,
    sha256 TEXT,
    bytes INTEGER
);
CREATE TABLE IF NOT EXISTS outcomes (
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    horizon_min INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    forward_return_pct REAL,
    mfe_pct REAL,
    mae_pct REAL,
    mfe_minute INTEGER,
    mae_minute INTEGER,
    direction_hit INTEGER,
    outcome TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    PRIMARY KEY (decision_id, horizon_min)
);
CREATE TABLE IF NOT EXISTS universe_forward (
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    horizon_min INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    forward_return_pct REAL NOT NULL,
    PRIMARY KEY (decision_id, horizon_min, symbol)
);
CREATE TABLE IF NOT EXISTS baselines (
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    cumulative_volume REAL,
    realized_vol_pct REAL,
    PRIMARY KEY (session_date, symbol)
);
CREATE TABLE IF NOT EXISTS feature_ic (
    session_date TEXT NOT NULL,
    feature TEXT NOT NULL,
    horizon_min INTEGER NOT NULL,
    ic REAL,
    n INTEGER,
    PRIMARY KEY (session_date, feature, horizon_min)
);
CREATE TABLE IF NOT EXISTS feed_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    session_date TEXT,
    ok INTEGER NOT NULL,
    health_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    session_date TEXT,
    event TEXT NOT NULL,
    detail TEXT
);
"""


class DecisionExists(Exception):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")       # readers never block the writer
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(decisions)")}
        if cols and "model_id" not in cols:
            raise RuntimeError(f"{self.path} uses the v1 schema; move it aside (e.g. rename to *.v1.sqlite) "
                               f"-- v1 decisions cannot be migrated without rewriting immutable rows")
        self.db.executescript(SCHEMA)
        for t in IMMUTABLE:
            for op in ("UPDATE", "DELETE"):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                                f"BEGIN SELECT RAISE(ABORT, '{t} rows are immutable'); END")
        self.db.execute("INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---------------- decisions (insert-only, atomic) ----------------
    def save_decision(self, snap: DecisionSnapshot, feature_rows: list | None = None,
                      ranking_rows: list | None = None, input_record: dict | None = None) -> None:
        d = snap.to_dict()
        now = datetime.now(IST).isoformat()
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (snap.decision_id, snap.decision_date, snap.mode, d["model_id"], snap.model_version,
                     d["feature_version"], d["config_hash"], snap.weights_hash, snap.selected_symbol,
                     snap.direction, snap.selection_score, snap.estimated_probability, snap.probability_status,
                     snap.expected_horizon_min, snap.decision_fingerprint, snap.input_fingerprint,
                     snap.published_at, now, snap.to_json()))
                if feature_rows:
                    self.db.executemany("INSERT INTO decision_features VALUES (?,?,?,?,?)",
                                        [(snap.decision_id, s, int(e), r, json.dumps(f, default=str))
                                         for s, e, r, f in feature_rows])
                if ranking_rows:
                    self.db.executemany("INSERT INTO decision_rankings VALUES (?,?,?,?,?,?)",
                                        [(snap.decision_id, i + 1, s, dr, raw, sc)
                                         for i, (s, dr, raw, sc) in enumerate(ranking_rows)])
                if input_record:
                    self.db.execute("INSERT INTO decision_inputs VALUES (?,?,?,?)",
                                    (snap.decision_id, input_record.get("path"), input_record.get("sha256"),
                                     input_record.get("bytes")))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc) or "PRIMARY KEY" in str(exc):
                raise DecisionExists(f"a {snap.mode} {d['model_id']} decision for {snap.decision_date} is "
                                     f"already stored and is immutable") from exc
            raise

    def get_decision(self, decision_date: str, mode: str, model_id: str | None = None) -> DecisionSnapshot | None:
        q, args = "SELECT snapshot_json FROM decisions WHERE decision_date=? AND mode=?", [decision_date, mode]
        if model_id:
            q += " AND model_id=?"
            args.append(model_id)
        row = self.db.execute(q + " ORDER BY stored_at LIMIT 1", args).fetchone()
        return snapshot_from_dict(json.loads(row[0])) if row else None

    def decisions(self, mode: str | None = None, before: str | None = None) -> list[dict]:
        q, args = "SELECT snapshot_json FROM decisions WHERE 1=1", []
        if mode:
            q += " AND mode=?"
            args.append(mode)
        if before:
            q += " AND decision_date < ?"
            args.append(before)
        return [json.loads(r[0]) for r in self.db.execute(q + " ORDER BY decision_date, mode", args)]

    def decision_features(self, decision_id: str) -> dict:
        return {s: (bool(e), r, json.loads(f)) for s, e, r, f in self.db.execute(
            "SELECT symbol, eligible, exclusion_reason, features_json FROM decision_features WHERE decision_id=?",
            (decision_id,))}

    def decision_rankings(self, decision_id: str) -> list[tuple]:
        return list(self.db.execute("SELECT rank, symbol, direction, raw, score FROM decision_rankings "
                                    "WHERE decision_id=? ORDER BY rank", (decision_id,)))

    def decision_input(self, decision_id: str) -> dict | None:
        r = self.db.execute("SELECT path, sha256, bytes FROM decision_inputs WHERE decision_id=?",
                            (decision_id,)).fetchone()
        return {"path": r[0], "sha256": r[1], "bytes": r[2]} if r else None

    # ---------------- outcomes (insert-only, final values only) ----------------
    def save_outcomes(self, decision_id: str, outcomes) -> int:
        """Stores each FINAL horizon once. Incomplete windows are not stored. Returns rows added."""
        now = datetime.now(IST).isoformat()
        added = 0
        with self.db:
            for o in outcomes:
                d = o.to_dict()
                if not d["complete"] and not d["outcome"].endswith("_FINAL"):
                    continue
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, d["horizon_min"], int(d["complete"]), d["forward_return_pct"], d["mfe_pct"],
                     d["mae_pct"], d.get("mfe_minute"), d.get("mae_minute"),
                     None if d["direction_hit"] is None else int(d["direction_hit"]), d["outcome"], now, json.dumps(d)))
                added += cur.rowcount
        return added

    def outcomes(self, decision_id: str) -> dict:
        rows = self.db.execute("SELECT horizon_min, outcome_json FROM outcomes WHERE decision_id=?", (decision_id,))
        return {h: json.loads(j) for h, j in rows}

    def save_universe_forward(self, decision_id: str, horizon: int, returns: dict) -> None:
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO universe_forward VALUES (?,?,?,?)",
                                [(decision_id, horizon, s, r) for s, r in sorted(returns.items())])

    def universe_forward(self, decision_id: str, horizon: int) -> dict:
        return dict(self.db.execute("SELECT symbol, forward_return_pct FROM universe_forward "
                                    "WHERE decision_id=? AND horizon_min=?", (decision_id, horizon)))

    def history(self, before: str, modes=("live", "backtest", "replay")) -> list[tuple[dict, dict]]:
        """(decision, {horizon: outcome}) for decisions strictly before `before` (walk-forward)."""
        return [(d, self.outcomes(d["decision_id"])) for d in self.decisions(before=before) if d["mode"] in modes]

    # ---------------- baselines from prior sessions (pre-cutoff values only) ----------------
    def save_baselines(self, session_date: str, rows: dict) -> None:
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO baselines VALUES (?,?,?,?)",
                                [(session_date, s, v, rv) for s, (v, rv) in rows.items()])

    def baselines(self, before: str, lookback: int, min_days: int) -> dict:
        days = [r[0] for r in self.db.execute(
            "SELECT DISTINCT session_date FROM baselines WHERE session_date < ? ORDER BY session_date DESC LIMIT ?",
            (before, lookback))]
        if not days:
            return {}
        q = ("SELECT symbol, cumulative_volume, realized_vol_pct FROM baselines WHERE session_date IN (%s)"
             % ",".join("?" * len(days)))
        acc: dict = {}
        for sym, v, rv in self.db.execute(q, days):
            a = acc.setdefault(sym, ([], []))
            if v:
                a[0].append(v)
            if rv:
                a[1].append(rv)

        def med(xs):
            xs = sorted(xs)
            n = len(xs)
            return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

        return {s: (med(v) if len(v) >= min_days else None, med(rv) if len(rv) >= min_days else None)
                for s, (v, rv) in acc.items()}

    # ---------------- feature validation ----------------
    def save_ic(self, session_date: str, rows: list[tuple]) -> None:
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO feature_ic VALUES (?,?,?,?,?)",
                                [(session_date, f, h, ic, n) for f, h, ic, n in rows])

    def ic_rows(self, before: str | None = None) -> list[tuple]:
        q, a = "SELECT session_date, feature, horizon_min, ic, n FROM feature_ic", []
        if before:
            q += " WHERE session_date < ?"
            a.append(before)
        return list(self.db.execute(q + " ORDER BY session_date", a))

    # ---------------- monitoring logs (append-only) ----------------
    def log_feed_check(self, session_date: str | None, ok: bool, health: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO feed_checks (checked_at, session_date, ok, health_json) VALUES (?,?,?,?)",
                            (datetime.now(IST).isoformat(), session_date, int(ok), json.dumps(health, default=str)))

    def log_event(self, session_date: str | None, event: str, detail: str = "") -> None:
        with self.db:
            self.db.execute("INSERT INTO lifecycle_events (at, session_date, event, detail) VALUES (?,?,?,?)",
                            (datetime.now(IST).isoformat(), session_date, event, detail))

    def events(self, session_date: str) -> list[tuple]:
        return list(self.db.execute("SELECT at, event, detail FROM lifecycle_events WHERE session_date=? ORDER BY id",
                                    (session_date,)))
