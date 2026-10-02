"""Feed parsing and the 09:45 information-set cut.

Two objects, deliberately separate:

    RawSession     everything the feed contains for one day (may include data
                   after 09:45). ONLY the outcome evaluator and the backtest
                   harness may read candles after the cutoff from it.
    SessionInput   the frozen 09:45 information set: per-stock candles with
                   timestamp < cutoff, plus pre-open metadata (previous close,
                   today's open). This is the ONLY input to features/scoring.

Parsing reuses the existing PSYGRID contract (PsygridClient.candles): only genuine,
finite, geometrically valid 1-minute OHLCV rows are accepted; nothing is synthesized.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from psygrid_client import PsygridClient

IST = ZoneInfo("Asia/Kolkata")
SESSION_START = time(9, 15)
MARKET_CLOSE = time(15, 30)


@dataclass(frozen=True)
class Series:
    """Columnar 1-minute series for one instrument (sorted, one bar per minute)."""
    symbol: str
    ts: tuple[datetime, ...]
    o: tuple[float, ...]
    h: tuple[float, ...]
    l: tuple[float, ...]
    c: tuple[float, ...]
    v: tuple[float, ...]
    previous_close: float | None = None
    today_open: float | None = None

    def __len__(self) -> int:
        return len(self.c)


@dataclass(frozen=True)
class RawSession:
    session_date: date
    stocks: dict[str, Series]
    unparseable: dict[str, str]
    index: Series | None
    feed_meta: dict
    source: str


@dataclass(frozen=True)
class SessionInput:
    session_date: date
    cutoff: datetime
    stocks: dict[str, Series]          # candles strictly before cutoff only
    received_symbols: tuple[str, ...]
    unparseable: dict[str, str]
    index: Series | None               # NIFTY, cut at the same cutoff (None if unavailable)
    feed_meta: dict
    source: str
    fingerprint: str = field(default="")


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) and v > 0 else None


_TS_CACHE: dict = {}


def _ts_cached(value) -> datetime:
    """PsygridClient._ts with a cache: a full-day feed repeats only ~375 distinct
    timestamp strings across ~370k rows."""
    try:
        return _TS_CACHE[value]
    except KeyError:
        pass
    dt = PsygridClient._ts(value)
    if len(_TS_CACHE) > 50_000:
        _TS_CACHE.clear()
    _TS_CACHE[value] = dt
    return dt


def fast_candles(rows) -> list[tuple]:
    """Same acceptance contract as PsygridClient.candles (genuine, finite, positive,
    geometrically valid 1m OHLCV; rows marked complete=false dropped), but returns
    plain tuples (ts, o, h, l, c, v) and caches timestamp parsing. Equivalence with
    PsygridClient.candles is enforced by tests."""
    out = []
    if not isinstance(rows, list):
        return out
    inf = float("inf")
    for row in rows:
        if not isinstance(row, dict) or row.get("complete", True) is False:
            continue
        try:
            o, h, l, c, v = (float(row["open"]), float(row["high"]), float(row["low"]),
                             float(row["close"]), float(row["volume"]))
            if not (-inf < o < inf and -inf < h < inf and -inf < l < inf and -inf < c < inf and -inf < v < inf):
                continue
            t = _ts_cached(row["timestamp"])
        except Exception:
            continue
        if o <= 0 or h <= 0 or l <= 0 or c <= 0 or v < 0:
            continue
        if h < (o if o > c else c) or l > (o if o < c else c):
            continue
        out.append((t, o, h, l, c, v))
    out.sort(key=lambda r: r[0])
    return out


def _series(symbol: str, rows: list[tuple], previous_close=None, today_open=None) -> Series:
    by_minute: dict = {}
    for r in rows:
        t = r[0]
        if t.second or t.microsecond or t.tzinfo is not IST:
            t = t.astimezone(IST).replace(second=0, microsecond=0)
        by_minute[t] = r                                   # later duplicate wins
    ordered = [by_minute[t] for t in sorted(by_minute)]
    ts = tuple(t for t in sorted(by_minute))
    return Series(symbol, ts,
                  tuple(r[1] for r in ordered), tuple(r[2] for r in ordered), tuple(r[3] for r in ordered),
                  tuple(r[4] for r in ordered), tuple(r[5] for r in ordered),
                  _num(previous_close), _num(today_open))


def infer_session_date(payload: dict, stocks: dict[str, Series]) -> date:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    if session.get("date"):
        try:
            return date.fromisoformat(str(session["date"])[:10])
        except ValueError:
            pass
    counts = Counter(t.date() for s in stocks.values() for t in s.ts[-1:])
    if not counts:
        raise ValueError("cannot infer session date: no candles in payload")
    return counts.most_common(1)[0][0]


def parse_payload(payload: dict, index_payload: dict | None = None, source: str = "live",
                  session_date: date | None = None) -> RawSession:
    """Parse a PSYGRID /public/live.json payload (atomic 990-stock snapshot)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("stocks"), dict):
        raise ValueError("payload is not a PSYGRID stock snapshot (missing 'stocks' object)")
    stocks: dict[str, Series] = {}
    unparseable: dict[str, str] = {}
    for symbol, row in payload["stocks"].items():
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        if not isinstance(row, dict):
            unparseable[symbol] = "PAYLOAD_NOT_OBJECT"
            continue
        try:
            rows = row.get("candles_1m")
            if rows is None:
                rows = row.get("1m", [])
            stocks[symbol] = _series(symbol, fast_candles(rows), row.get("previous_close"), row.get("today_open"))
        except Exception as exc:                      # one bad stock never stops the run
            unparseable[symbol] = f"PARSE_ERROR:{type(exc).__name__}"
    day = session_date or infer_session_date(payload, stocks)
    index = None
    if isinstance(index_payload, dict):
        try:
            index = _series(str(index_payload.get("symbol", "NIFTY")),
                            fast_candles(index_payload.get("1m") or index_payload.get("candles_1m") or []))
        except Exception:
            index = None
    meta = {k: payload.get(k) for k in ("universe_size", "stock_count", "status") if k in payload}
    if isinstance(payload.get("session"), dict):
        meta["session"] = payload["session"]
    return RawSession(day, stocks, unparseable, index, meta, source)


def cutoff_datetime(session_date: date, cutoff: time) -> datetime:
    return datetime.combine(session_date, cutoff, IST)


def _cut(s: Series, session_date: date, start: datetime, end: datetime) -> Series:
    keep = [i for i, t in enumerate(s.ts) if t.date() == session_date and start <= t < end]
    pick = lambda col: tuple(col[i] for i in keep)
    return Series(s.symbol, pick(s.ts), pick(s.o), pick(s.h), pick(s.l), pick(s.c), pick(s.v),
                  s.previous_close, s.today_open)


def freeze_information_set(raw: RawSession, cutoff: time = time(9, 45)) -> SessionInput:
    """THE no-look-ahead gate: drop every candle that starts at or after the cutoff.

    A 1-minute candle stamped 09:44 covers 09:44:00-09:44:59 and is complete at
    09:45:00, so it is included; the 09:45 candle is not.
    """
    end = cutoff_datetime(raw.session_date, cutoff)
    start = datetime.combine(raw.session_date, SESSION_START, IST)
    stocks = {sym: _cut(s, raw.session_date, start, end) for sym, s in raw.stocks.items()}
    index = _cut(raw.index, raw.session_date, start, end) if raw.index is not None else None
    if index is not None and len(index) == 0:
        index = None
    received = tuple(sorted(set(raw.stocks) | set(raw.unparseable)))
    si = SessionInput(raw.session_date, end, stocks, received, dict(raw.unparseable), index,
                      dict(raw.feed_meta), raw.source)
    return SessionInput(**{**si.__dict__, "fingerprint": input_fingerprint(si)})


def input_fingerprint(si: SessionInput) -> str:
    """sha256 of the canonical frozen information set (identical input -> identical hash)."""
    h = hashlib.sha256()
    h.update(f"{si.session_date.isoformat()}|{si.cutoff.isoformat()}".encode())
    for sym in sorted(si.stocks):
        s = si.stocks[sym]
        h.update(f"|{sym}|{s.previous_close!r}|{s.today_open!r}".encode())
        for i in range(len(s)):
            h.update(f"{s.ts[i].strftime('%H%M')},{s.o[i]!r},{s.h[i]!r},{s.l[i]!r},{s.c[i]!r},{s.v[i]!r};".encode())
    if si.index is not None:
        h.update(b"|INDEX")
        for i in range(len(si.index)):
            h.update(f"{si.index.ts[i].strftime('%H%M')},{si.index.c[i]!r};".encode())
    for sym in sorted(si.unparseable):
        h.update(f"|X{sym}:{si.unparseable[sym]}".encode())
    return h.hexdigest()


def future_bars(raw: RawSession, symbol: str, cutoff_dt: datetime, minutes: int) -> Series | None:
    """Candles in [cutoff, cutoff + minutes). For outcome evaluation ONLY."""
    s = raw.stocks.get(symbol)
    if s is None:
        return None
    return _cut(s, raw.session_date, cutoff_dt, cutoff_dt + timedelta(minutes=minutes))


def load_json_file(path: str | Path) -> dict:
    p = Path(path)
    data = gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()
    return json.loads(data.decode("utf-8"))


def load_session_file(path: str | Path) -> RawSession:
    """A saved session: either a raw live.json payload, or an archive written by
    `945.py --archive` ({"stocks_payload": ..., "index_payload": ...})."""
    data = load_json_file(path)
    if isinstance(data, dict) and "stocks_payload" in data:
        return parse_payload(data["stocks_payload"], data.get("index_payload"), source=str(path))
    return parse_payload(data, None, source=str(path))


def recut(si: SessionInput, cutoff: time) -> SessionInput:
    """An EARLIER information set derived from an already-frozen one (used for the rank
    stability diagnostic). It can only remove data, never add any."""
    end = cutoff_datetime(si.session_date, cutoff)
    if end > si.cutoff:
        raise ValueError("recut can only move the cutoff earlier")
    start = datetime.combine(si.session_date, SESSION_START, IST)
    stocks = {sym: _cut(s, si.session_date, start, end) for sym, s in si.stocks.items()}
    index = _cut(si.index, si.session_date, start, end) if si.index is not None else None
    return SessionInput(si.session_date, end, stocks, si.received_symbols, dict(si.unparseable),
                        index if index is not None and len(index) else None, dict(si.feed_meta), si.source, "")
