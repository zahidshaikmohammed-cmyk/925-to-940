"""Historical dataset: Dhan responses -> fixed-strike minute series -> DayData files.

Dhan's expired-options endpoint (/charts/rollingoption) returns ROLLING moneyness
series ("ATM", "ATM+1", ...): the contract behind "ATM" changes as the index moves.
A backtest must trade ONE contract, so each response is split by its per-minute
`strike` field and re-assembled into fixed-strike series. Fetch enough offsets
(ATM-10 .. ATM+10) that every strike the engine could pick is fully covered; any
minute that is missing for the traded strike cancels the entry (no fill assumed).

Response shape assumed (VERIFY on the first real fetch):
  {"data": {"ce": {"timestamp": [...], "open": [...], "high": [...], "low": [...],
                   "close": [...], "volume": [...], "oi": [...], "iv": [...],
                   "strike": [...], "spot": [...]}, "pe": null}}
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .backtest import DayData
from .models import IST, Candle, PriorDay


def resolve_epochs(ts: list[int]) -> list[datetime]:
    """Interpret epoch seconds as UTC, or as IST wall-clock if that is what puts the first
    bar of the day near 09:15. Raises if neither does: never guess."""
    if not ts:
        return []
    as_utc = [datetime.fromtimestamp(x, IST) for x in ts]
    if time(9, 0) <= as_utc[0].time() <= time(9, 31):
        return as_utc
    as_ist = [x - timedelta(hours=5, minutes=30) for x in as_utc]
    if time(9, 0) <= as_ist[0].time() <= time(9, 31):
        return as_ist
    raise ValueError(f"cannot resolve epoch convention: first bar at {as_utc[0]}")


def split_rolling(payload: dict, right: str) -> dict[int, dict[datetime, Candle]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    side = data.get("ce" if right == "CE" else "pe") or {}
    need = ("timestamp", "open", "high", "low", "close", "strike")
    if not all(isinstance(side.get(k), list) for k in need):
        return {}
    n = len(side["timestamp"])
    if any(len(side[k]) != n for k in need):
        raise ValueError("rolling option arrays have different lengths")
    out: dict[int, dict[datetime, Candle]] = {}
    days: dict[date, list[int]] = {}
    for i, x in enumerate(side["timestamp"]):
        days.setdefault(datetime.fromtimestamp(x, IST).date(), []).append(i)
    vol = side.get("volume") or [0] * n
    for idx in days.values():
        times = resolve_epochs([side["timestamp"][i] for i in idx])
        for i, ts in zip(idx, times):
            strike = int(round(float(side["strike"][i])))
            c = Candle(ts.replace(second=0, microsecond=0), float(side["open"][i]), float(side["high"][i]),
                       float(side["low"][i]), float(side["close"][i]), int(vol[i] or 0))
            if c.is_valid():
                out.setdefault(strike, {})[c.start] = c
    return out


def merge_fixed(parts: list[dict[int, dict[datetime, Candle]]]) -> dict[int, dict[datetime, Candle]]:
    """Several rolling offsets can contain the same contract-minute; they must agree."""
    out: dict[int, dict[datetime, Candle]] = {}
    for part in parts:
        for strike, series in part.items():
            dst = out.setdefault(strike, {})
            for ts, c in series.items():
                if ts in dst and abs(dst[ts].close - c.close) > 0.051:
                    raise ValueError(f"conflicting prints for {strike} at {ts}: {dst[ts].close} vs {c.close}")
                dst[ts] = c
    return out


# ------------------------------------------------------------- on-disk format

def save_day(d: DayData, folder: Path) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    obj = {"day": d.day.isoformat(), "is_expiry": d.is_expiry, "expiry": d.expiry.isoformat() if d.expiry else None,
           "lot_size": d.lot_size, "tags": d.tags,
           "prior": None if d.prior is None else {"day": d.prior.day.isoformat(), "high": d.prior.high,
                                                  "low": d.prior.low, "close": d.prior.close},
           "underlying": [[c.start.isoformat(), c.open, c.high, c.low, c.close, c.volume] for c in d.underlying],
           "options": {f"{k}{r}": [[c.start.isoformat(), c.open, c.high, c.low, c.close, c.volume] for c in s.values()]
                       for (k, r), s in d.options.items()}}
    p = folder / f"{d.day.isoformat()}.json"
    p.write_text(json.dumps(obj))
    return p


def load_day(path: Path) -> DayData:
    o = json.loads(Path(path).read_text())

    def candles(rows):
        return [Candle(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]
    opts = {}
    for key, rows in o["options"].items():
        opts[(int(key[:-2]), key[-2:])] = {c.start: c for c in candles(rows)}
    pr = o.get("prior")
    return DayData(date.fromisoformat(o["day"]),
                   None if pr is None else PriorDay(date.fromisoformat(pr["day"]), pr["high"], pr["low"], pr["close"]),
                   o["is_expiry"], date.fromisoformat(o["expiry"]) if o.get("expiry") else None,
                   candles(o["underlying"]), opts, o.get("lot_size", 20), o.get("tags", {}))


def load_folder(folder: Path) -> list[DayData]:
    return [load_day(p) for p in sorted(Path(folder).glob("*.json")) if not p.name.startswith("_")]
