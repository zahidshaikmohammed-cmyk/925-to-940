"""Append-only, hash-chained JSONL audit log (spec section 38).

Each record carries the SHA-256 of the previous record, so any edit, deletion or
reordering after the fact is detectable with `verify()`. Every decision (including
every NO_TRADE), order event, fill, exit and kill-switch event is written here; a trade
must be reconstructable from this file alone.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


def _digest(prev: str, body: dict) -> str:
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev = "GENESIS"
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    self._prev = json.loads(line)["hash"]

    def write(self, kind: str, ts: datetime, payload: dict) -> str:
        body = {"kind": kind, "ts": ts.isoformat(), "payload": payload, "prev": self._prev}
        h = _digest(self._prev, body)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(body | {"hash": h}, default=str) + "\n")
            f.flush()
        self._prev = h
        return h


def verify(path: Path) -> tuple[bool, int]:
    """(ok, number of records verified before the first break)."""
    prev = "GENESIS"
    n = 0
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        h = rec.pop("hash")
        if rec.get("prev") != prev or _digest(prev, rec) != h:
            return False, n
        prev = h
        n += 1
    return True, n
