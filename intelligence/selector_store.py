"""SQLite persistence for decisions, outcomes, baselines and feature-validation stats.

Decisions are INSERT-ONLY: SQLite triggers abort any UPDATE or DELETE, and a unique
(decision_date, mode, model_version) key stops a second decision for the same day.
Outcomes live in their own table and may be refreshed as more post-decision data
arrives; they never touch the decision row.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .selector_snapshot import DecisionSnapshot, snapshot_from_dict

IST = ZoneInfo("Asia/Kolkata")

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    decision_date TEXT NOT NULL,
    mode TEXT NOT NULL,
    model_version TEXT NOT NULL,
    selected_symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    selection_score REAL NOT NULL,
    estimated_probability REAL NOT NULL,
    decision_fingerprint TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    UNIQUE (decision_date, mode, model_version)
);
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;
CREATE TABLE IF NOT EXISTS outcomes (
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    horizon_min INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    forward_return_pct REAL,
    mfe_pct REAL,
    mae_pct REAL,
    direction_hit INTEGER,
    outcome TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    PRIMARY KEY (decision_id, horizon_min)
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
"""


class DecisionExists(Exception):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- decisions (insert-only) --
    def save_decision(self, snap: DecisionSnapshot) -> None:
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (snap.decision_id, snap.decision_date, snap.mode, snap.model_version, snap.selected_symbol,
                     snap.direction, snap.selection_score, snap.estimated_probability, snap.decision_fingerprint,
                     snap.input_fingerprint, datetime.now(IST).isoformat(), snap.to_json()))
        except sqlite3.IntegrityError as exc:
            raise DecisionExists(f"a {snap.mode} decision for {snap.decision_date} (model "
                                 f"{snap.model_version}) is already stored and is immutable") from exc

    def get_decision(self, decision_date: str, mode: str, model_version: str | None = None) -> DecisionSnapshot | None:
        q = "SELECT snapshot_json FROM decisions WHERE decision_date=? AND mode=?"
        args: list = [decision_date, mode]
        if model_version:
            q += " AND model_version=?"
            args.append(model_version)
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
        return [json.loads(r[0]) for r in self.db.execute(q + " ORDER BY decision_date", args)]

    # -- outcomes (refreshable, separate) --
    def save_outcomes(self, decision_id: str, outcomes) -> None:
        now = datetime.now(IST).isoformat()
        with self.db:
            for o in outcomes:
                d = o.to_dict()
                self.db.execute(
                    "INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, d["horizon_min"], int(d["complete"]), d["forward_return_pct"], d["mfe_pct"],
                     d["mae_pct"], None if d["direction_hit"] is None else int(d["direction_hit"]),
                     d["outcome"], now, json.dumps(d)))

    def outcomes(self, decision_id: str) -> dict:
        rows = self.db.execute("SELECT horizon_min, outcome_json FROM outcomes WHERE decision_id=?", (decision_id,))
        return {h: json.loads(j) for h, j in rows}

    def history(self, before: str, modes=("live", "backtest", "replay")) -> list[tuple[dict, dict]]:
        """(decision, {horizon: outcome}) for decisions strictly before `before` (walk-forward)."""
        out = []
        for d in self.decisions(before=before):
            if d["mode"] in modes:
                out.append((d, self.outcomes(d["decision_id"])))
        return out

    # -- baselines from prior sessions (pre-cutoff values only) --
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
        q = "SELECT symbol, cumulative_volume, realized_vol_pct FROM baselines WHERE session_date IN (%s)" % ",".join("?" * len(days))
        acc: dict = {}
        for sym, v, rv in self.db.execute(q, days):
            acc.setdefault(sym, ([], []))
            if v:
                acc[sym][0].append(v)
            if rv:
                acc[sym][1].append(rv)
        med = lambda xs: sorted(xs)[len(xs) // 2] if len(xs) % 2 else sum(sorted(xs)[len(xs) // 2 - 1:len(xs) // 2 + 1]) / 2
        return {s: (med(v) if len(v) >= min_days else None, med(rv) if len(rv) >= min_days else None)
                for s, (v, rv) in acc.items()}

    # -- feature validation --
    def save_ic(self, session_date: str, rows: list[tuple[str, int, float | None, int]]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO feature_ic VALUES (?,?,?,?,?)",
                                [(session_date, f, h, ic, n) for f, h, ic, n in rows])

    def ic_rows(self) -> list[tuple]:
        return list(self.db.execute("SELECT session_date, feature, horizon_min, ic, n FROM feature_ic ORDER BY session_date"))
