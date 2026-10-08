"""Fetch real SENSEX history from Dhan and build the point-in-time dataset.

    export DHAN_CLIENT_ID=...  DHAN_ACCESS_TOKEN=...      # needs the Dhan Data API add-on
    python -m sensex_expiry.realdata fetch --from 2023-05-15 --to 2026-10-07 --raw data/sensex_expiry/raw
    python -m sensex_expiry.realdata build --raw data/sensex_expiry/raw --out data/sensex_expiry/days
    python -m sensex_expiry --baseline data/sensex_expiry/days --out reports/baseline --workers 4

Rules:
  * Every API response is stored verbatim under raw/ (one JSON per request, keyed by the
    request). The build reads only raw/, so it is reproducible and auditable offline.
  * Nothing is invented. A minute Dhan did not return stays missing; a day whose option
    prints conflict between rolling offsets loses its option data, with the reason
    logged. Days with < 300 index minutes are excluded, with the reason logged.
  * Historical bid/ask does not exist in Dhan's history: the backtest models spread
    (0.5% of premium round trip, stressed x2) and says so in every report.
  * Expiry days come from the exchange convention calendar with holidays taken from
    Dhan's own daily index history (a weekday with no daily bar was not a session), and
    are then CONFIRMED from the option data itself: on a real expiry day the ATM
    option's time value at the last minute is ~0. Calendar/data disagreement is logged
    and the day is excluded from Test B.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .backtest import DayData
from .history import merge_fixed, resolve_epochs, save_day, split_rolling
from .models import IST, Candle, PriorDay
from .session_calendar import weekly_expiry_for

INDEX_SEG, FNO_SEG = "IDX_I", "BSE_FNO"
OFFSETS = range(-10, 11)
FIELDS_REQ = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
# SENSEX lot size history. VERIFY the change date against the BSE circular; it only affects
# rupee sizing and fixed brokerage per trade, not R multiples of the signal.
LOT_SCHEDULE = ((date(2023, 5, 15), 10), (date(2025, 1, 1), 20))


def lot_for(d: date) -> int:
    lot = LOT_SCHEDULE[0][1]
    for start, n in LOT_SCHEDULE:
        if d >= start:
            lot = n
    return lot


# ------------------------------------------------------------------ fetch

class RawStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, kind: str, params: dict) -> Path:
        key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        return self.root / kind / f"{key}.json"

    def get(self, kind: str, params: dict):
        p = self.path(kind, params)
        return json.loads(p.read_text())["response"] if p.exists() else None

    def put(self, kind: str, params: dict, response) -> None:
        p = self.path(kind, params)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"params": params, "fetched_at": datetime.now(IST).isoformat(), "response": response}))

    def all(self, kind: str):
        for p in sorted((self.root / kind).glob("*.json")):
            o = json.loads(p.read_text())
            yield o["params"], o["response"]


def _windows(start: date, end: date, days: int):
    a = start
    while a <= end:
        b = min(end, a + timedelta(days=days - 1))
        yield a, b
        a = b + timedelta(days=1)


def fetch(client_id: str, token: str, start: date, end: date, raw: Path, index_id: str | None = None,
          expiry_code: int | None = None, log=print) -> dict:
    from .dhan_adapter import SCRIP_MASTER_URL, _client, parse_scrip_master
    import urllib.request

    c = _client(client_id, token)
    store = RawStore(raw)
    manifest = {"start": str(start), "end": str(end), "requests": 0, "failures": []}
    last = [0.0]

    def call(kind, params, fn):
        cached = store.get(kind, params)
        if cached is not None:
            return cached
        gap = _time.monotonic() - last[0]
        if gap < 0.25:                      # Dhan data APIs: 5 requests/second
            _time.sleep(0.25 - gap)
        last[0] = _time.monotonic()
        try:
            resp = fn()
        except Exception as e:              # network errors are recorded, never papered over
            resp = {"status": "failure", "remarks": repr(e)}
        manifest["requests"] += 1
        if not (isinstance(resp, dict) and str(resp.get("status", "")).lower() == "success"):
            manifest["failures"].append({"kind": kind, "params": params, "remarks": str(resp)[:300]})
        store.put(kind, params, resp)
        return resp

    if index_id is None:
        csv_path = Path(raw) / "scrip_master.csv"
        if not csv_path.exists():
            with urllib.request.urlopen(SCRIP_MASTER_URL, timeout=60) as r:
                csv_path.write_bytes(r.read())
        index_id = parse_scrip_master(csv_path.read_text(errors="replace"), end)["index_id"]
        if not index_id:
            raise SystemExit("SENSEX index id not found in scrip master: refusing to guess")
    manifest["index_id"] = index_id
    log(f"SENSEX index security id = {index_id}")

    for a, b in _windows(start - timedelta(days=10), end, 365):
        p = {"sid": index_id, "from": str(a), "to": str(b)}
        call("index_daily", p, lambda: c.historical_daily_data(index_id, INDEX_SEG, "INDEX", str(a), str(b)))
    for a, b in _windows(start - timedelta(days=10), end, 60):
        p = {"sid": index_id, "from": str(a), "to": str(b)}
        call("index_1m", p, lambda: c.intraday_minute_data(index_id, INDEX_SEG, "INDEX", str(a), str(b), 1))
    log("index history fetched")

    if expiry_code is None:
        expiry_code = detect_expiry_code(call, c, index_id, end, log)
    manifest["expiry_code"] = expiry_code
    if expiry_code is None:
        log("could not confirm which expiry_code returns the expiring weekly contract: option fetch skipped")
    else:
        for a, b in _windows(start, end, 28):
            for k in OFFSETS:
                label = "ATM" if k == 0 else f"ATM{k:+d}"
                for right in ("CALL", "PUT"):
                    p = {"sid": index_id, "code": expiry_code, "strike": label, "right": right, "from": str(a), "to": str(b)}
                    call("options_1m", p, lambda: c.expired_options_data(
                        int(index_id), FNO_SEG, "OPTIDX", "WEEK", expiry_code, label, right, FIELDS_REQ,
                        str(a), str(b), 1))
            log(f"options {a}..{b} fetched")
    (Path(raw) / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return manifest


def _last_time_value(resp: dict, right: str, day: date) -> float | None:
    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    side = (data or {}).get("ce" if right == "CE" else "pe") or {}
    try:
        idx = [i for i, ts in enumerate(side["timestamp"]) if datetime.fromtimestamp(ts, IST).date() == day
               or (datetime.fromtimestamp(ts, IST) - timedelta(hours=5, minutes=30)).date() == day]
        if not idx:
            return None
        i = idx[-1]
        spot, strike, close = float(side["spot"][i]), float(side["strike"][i]), float(side["close"][i])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    intrinsic = max(0.0, spot - strike) if right == "CE" else max(0.0, strike - spot)
    return close - intrinsic


def detect_expiry_code(call, c, index_id: str, end: date, log) -> int | None:
    """Pick the expiry_code whose ATM contract has ~zero time value at the close of a recent
    calendar expiry and material time value the day before. No assumption is made."""
    exp = weekly_expiry_for(end - timedelta(days=7))
    if exp is None:
        return None
    for code in (1, 0, 2):
        p = {"sid": index_id, "code": code, "strike": "ATM", "right": "CALL", "from": str(exp - timedelta(days=3)), "to": str(exp)}
        resp = call("options_probe", p, lambda: c.expired_options_data(int(index_id), FNO_SEG, "OPTIDX", "WEEK", code, "ATM",
                                                                      "CALL", FIELDS_REQ, p["from"], p["to"], 1))
        tv_exp = _last_time_value(resp, "CE", exp)
        tv_before = _last_time_value(resp, "CE", exp - timedelta(days=1))
        log(f"expiry_code {code}: time value at close on {exp} = {tv_exp}, day before = {tv_before}")
        if tv_exp is not None and tv_exp < 15 and (tv_before is None or tv_before > 30):
            return code
    return None


# ------------------------------------------------------------------ build

def _arrays_to_candles(resp) -> dict[date, list[Candle]]:
    data = resp.get("data", resp) if isinstance(resp, dict) else {}
    need = ("timestamp", "open", "high", "low", "close")
    if not isinstance(data, dict) or not all(isinstance(data.get(k), list) for k in need):
        return {}
    by_day: dict[date, list[int]] = {}
    for i, ts in enumerate(data["timestamp"]):
        by_day.setdefault(datetime.fromtimestamp(ts, IST).date(), []).append(i)
    vol = data.get("volume") or [0] * len(data["timestamp"])
    out: dict[date, list[Candle]] = {}
    for d, idx in by_day.items():
        times = resolve_epochs([data["timestamp"][i] for i in idx])
        cs = []
        for i, ts in zip(idx, times):
            cdl = Candle(ts.replace(second=0, microsecond=0), float(data["open"][i]), float(data["high"][i]),
                         float(data["low"][i]), float(data["close"][i]), int(vol[i] or 0))
            if cdl.is_valid() and time(9, 15) <= cdl.start.time() < time(15, 30):
                cs.append(cdl)
        if cs:
            out[cs[0].start.date()] = sorted({x.start: x for x in cs}.values(), key=lambda x: x.start)
    return out


def _daily(resp) -> dict[date, tuple[float, float, float]]:
    data = resp.get("data", resp) if isinstance(resp, dict) else {}
    if not isinstance(data, dict) or not isinstance(data.get("timestamp"), list):
        return {}
    out = {}
    for i, ts in enumerate(data["timestamp"]):
        dt = datetime.fromtimestamp(ts, IST)
        d = dt.date() if dt.hour < 12 else (dt + timedelta(hours=6)).date()
        out[d] = (float(data["high"][i]), float(data["low"][i]), float(data["close"][i]))
    return out


def build(raw: Path, out: Path, log=print) -> dict:
    store = RawStore(raw)
    daily: dict = {}
    for _, resp in store.all("index_daily"):
        daily.update(_daily(resp))
    minutes: dict[date, list[Candle]] = {}
    for _, resp in store.all("index_1m"):
        for d, cs in _arrays_to_candles(resp).items():
            minutes.setdefault(d, [])
            minutes[d] = sorted({x.start: x for x in minutes[d] + cs}.values(), key=lambda x: x.start)
    sessions = sorted(daily)
    if not sessions:
        raise SystemExit("no daily index history in raw/: nothing to build")
    holidays = frozenset(d for d in (sessions[0] + timedelta(days=i) for i in range((sessions[-1] - sessions[0]).days + 1))
                         if d.weekday() < 5 and d not in daily)

    parts: dict[date, list] = {}
    for params, resp in store.all("options_1m"):
        right = "CE" if params["right"] == "CALL" else "PE"
        try:
            split = split_rolling(resp, right)
        except ValueError as e:
            log(f"options {params}: {e}")
            continue
        for strike, series in split.items():
            for ts, cdl in series.items():
                parts.setdefault(ts.date(), []).append({(strike, right): {ts: cdl}})

    report = {"sessions": len(sessions), "holidays_inferred": len(holidays), "excluded": [], "built": 0,
              "expiry_days": 0, "expiry_days_with_options": 0, "expiry_label_conflicts": []}
    out = Path(out)
    for i, d in enumerate(sessions):
        cs = minutes.get(d, [])
        if len(cs) < 300:
            report["excluded"].append({"day": str(d), "reason": f"only {len(cs)} index minutes"})
            continue
        prev = sessions[i - 1] if i > 0 else None
        prior = PriorDay(prev, *daily[prev]) if prev else None
        is_exp = weekly_expiry_for(d, holidays) == d
        opts: dict = {}
        tags = {"source": "dhan", "lot_size_assumed": lot_for(d), "spread_model": "0.5% of premium round trip (modelled)"}
        if d in parts:
            try:
                fixed = merge_fixed([{k: v for k, v in p.items()} for p in _regroup(parts[d])])
                opts = {(k, r): s for (k, r), s in fixed.items()}
            except ValueError as e:
                report["excluded"].append({"day": str(d), "reason": f"option prints conflict: {e}"})
                opts = {}
        if is_exp and opts:
            ok = _confirm_expiry(opts, cs)
            tags["expiry_confirmed_by_data"] = ok
            if ok is False:
                report["expiry_label_conflicts"].append(str(d))
                opts = {}
        if prev and prior:
            tags["gap_pct"] = round((cs[0].open / prior.close - 1) * 100, 3)
        tags["day_range_pct"] = round((max(x.high for x in cs) / min(x.low for x in cs) - 1) * 100, 3)
        tags["convention"] = {4: "FRI", 1: "TUE", 3: "THU"}.get(d.weekday(), "OTHER") if is_exp else None
        save_day(DayData(d, prior, is_exp, d if is_exp else None, cs, opts, lot_for(d), tags), out)
        report["built"] += 1
        report["expiry_days"] += is_exp
        report["expiry_days_with_options"] += bool(is_exp and opts)
    (out / "_build_report.json").write_text(json.dumps(report, indent=2))
    log(json.dumps({k: v for k, v in report.items() if k != "excluded"}, indent=2))
    return report


def _regroup(chunks: list[dict]) -> list[dict]:
    """[{(strike,right): {ts: c}}, ...] -> one dict per contract so merge_fixed can check conflicts."""
    acc: dict = {}
    for ch in chunks:
        for (k, r), s in ch.items():
            for ts, cdl in s.items():
                cur = acc.setdefault((k, r), {})
                if ts in cur and abs(cur[ts].close - cdl.close) > 0.051:
                    raise ValueError(f"{k}{r} {ts}: {cur[ts].close} vs {cdl.close}")
                cur[ts] = cdl
    return [{key: series} for key, series in acc.items()]


def _confirm_expiry(opts: dict, und: list[Candle]) -> bool | None:
    """True if the ATM option at the last minute has < Rs 15 time value (expiring today)."""
    last = und[-1]
    atm = int(round(last.close / 100) * 100)
    vals = []
    for right in ("CE", "PE"):
        s = opts.get((atm, right), {})
        if not s:
            continue
        ts = max(s)
        if (last.start - ts).total_seconds() > 600:
            continue
        intrinsic = max(0.0, last.close - atm) if right == "CE" else max(0.0, atm - last.close)
        vals.append(s[ts].close - intrinsic)
    if not vals:
        return None
    return statistics.median(vals) < 15


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sensex_expiry.realdata")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    f.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    f.add_argument("--raw", type=Path, required=True)
    f.add_argument("--index-id")
    f.add_argument("--expiry-code", type=int)
    b = sub.add_parser("build")
    b.add_argument("--raw", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        cid, tok = os.environ.get("DHAN_CLIENT_ID"), os.environ.get("DHAN_ACCESS_TOKEN")
        if not cid or not tok:
            print("set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN", file=sys.stderr)
            return 2
        m = fetch(cid, tok, a.start, a.end, a.raw, a.index_id, a.expiry_code)
        print(json.dumps({k: (v if k != "failures" else len(v)) for k, v in m.items()}, indent=2, default=str))
        return 0
    build(a.raw, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
