"""Price history for the setup engine: 60 days of 5-minute bars from Yahoo Finance.

The PSYGRID feed only serves today. The research setups need earlier days for
relative volume (RVOL), daily ATR and the size of a normal first half-hour, so
`python 945.py --bootstrap` downloads the last 60 days of 5-minute candles for
every symbol (SYMBOL.NS on Yahoo's public chart API, standard library only) into
data/history/yahoo_5m.json.gz. The same file is the backtest data set.

Baselines for a day D only ever use days strictly before D (no look-ahead).
"""
from __future__ import annotations

import gzip
import json
import time as systime
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median, pstdev
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=5m&range=60d&includePrePost=false"
HISTORY_FILE = "yahoo_5m.json.gz"

Bar = tuple  # (ts: datetime, o, h, l, c, v)


FIVE_MIN = timedelta(minutes=5)


def to_five_minute(one_minute: list[Bar], now: datetime) -> list[Bar]:
    """Aggregate 1-minute bars into 5-minute bars that are complete at `now`."""
    out, cur, key = [], None, None
    for b in one_minute:
        t = b[0]
        mins = (t.hour * 60 + t.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
        if mins < 0:
            continue
        start = t.replace(second=0, microsecond=0) - timedelta(minutes=mins % 5)
        if start + FIVE_MIN > now:
            break
        if start != key:
            if cur:
                out.append(tuple(cur))
            key, cur = start, [start, b[1], b[2], b[3], b[4], b[5]]
        else:
            cur[2], cur[3], cur[4], cur[5] = max(cur[2], b[2]), min(cur[3], b[3]), b[4], cur[5] + b[5]
    if cur:
        out.append(tuple(cur))
    return out


# --------------------------------------------------------------------------- own history

def sessions_history(sessions_dir: str | Path, last: int | None = None, before: date | None = None,
                     say=None) -> dict[str, list[Bar]]:
    """History built from the scanner's own saved PSYGRID sessions (data/sessions/*.json.gz):
    the same feed, so volume is on exactly the same scale as live."""
    from .selector_data import load_session_file
    files = sorted(Path(sessions_dir).glob("*.json.gz"))
    if before is not None:
        files = [f for f in files if f.name[:10] < before.isoformat()]
    if last:
        files = files[-last:]
    out: dict[str, list[Bar]] = {}
    for f in files:
        try:
            raw = load_session_file(f)
        except Exception as exc:
            if say:
                say(f"  skipped {f.name}: {exc}")
            continue
        end = datetime.combine(raw.session_date, SESSION_CLOSE, IST) + FIVE_MIN
        for sym, ser in raw.stocks.items():
            rows = [(ser.ts[i], ser.o[i], ser.h[i], ser.l[i], ser.c[i], ser.v[i]) for i in range(len(ser))
                    if ser.ts[i].date() == raw.session_date]
            five = to_five_minute(rows, end)
            if five:
                out.setdefault(sym, []).extend(five)
    for sym in out:
        out[sym].sort(key=lambda b: b[0])
    return out


def merge_history(*sources: dict[str, list[Bar]]) -> dict[str, list[Bar]]:
    """Combine histories day by day; a later source wins for a day both have."""
    merged: dict[str, dict] = {}
    for src in sources:
        for sym, bars in src.items():
            days = merged.setdefault(sym, {})
            for d, rows in by_day(bars).items():
                days[d] = rows
    return {s: [b for d in sorted(days) for b in days[d]] for s, days in merged.items() if days}


def probe_yahoo(timeout: float = 8.0) -> bool:
    """One quick request: is Yahoo reachable from this machine at all?"""
    try:
        return bool(fetch_symbol("RELIANCE", timeout=timeout, tries=1))
    except Exception:
        return False


# --------------------------------------------------------------------------- fetch

def yahoo_symbol(symbol: str) -> str:
    return f"{symbol}.NS"


NSE_EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


def nse_equity_symbols(timeout: float = 30.0) -> list[str]:
    """Every NSE equity in the EQ series (fallback universe when the feed is closed)."""
    import csv
    import io
    req = urllib.request.Request(NSE_EQUITY_LIST, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")
    rows = csv.DictReader(io.StringIO(text))
    out = []
    for r in rows:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SERIES") == "EQ" and r.get("SYMBOL"):
            out.append(r["SYMBOL"])
    return sorted(set(out))


def parse_chart(payload: dict) -> list[Bar]:
    """Yahoo chart JSON -> 5-minute session bars in IST (incomplete rows dropped)."""
    result = ((payload or {}).get("chart") or {}).get("result") or []
    if not result:
        return []
    r = result[0]
    stamps = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    out = []
    for i, s in enumerate(stamps):
        try:
            o, h, l, c, v = (float(col[i]) for col in cols)
        except (TypeError, ValueError, IndexError):
            continue
        t = datetime.fromtimestamp(int(s), IST).replace(second=0, microsecond=0)
        if not (SESSION_OPEN <= t.time() < SESSION_CLOSE) or min(o, h, l, c) <= 0 or v < 0:
            continue
        if h < max(o, c) or l > min(o, c):
            continue
        out.append((t, o, h, l, c, v))
    out.sort(key=lambda b: b[0])
    dedup = {b[0]: b for b in out}
    return [dedup[t] for t in sorted(dedup)]


def fetch_symbol(symbol: str, timeout: float = 20.0, tries: int = 3) -> list[Bar]:
    url = CHART_URL.format(sym=urllib.parse.quote(yahoo_symbol(symbol)))
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return parse_chart(json.loads(resp.read().decode("utf-8")))
        except Exception as exc:              # 404 = unknown symbol, 429 = slow down
            last = exc
            if "404" in str(exc):
                break
            systime.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last}")


def bootstrap(symbols: list[str], out_dir: str | Path, fetch=fetch_symbol, workers: int = 8,
              say=print) -> Path:
    """Download every symbol's 60-day 5-minute history into one file."""
    data: dict = {}
    failed: dict = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            done += 1
            try:
                bars = fut.result()
                if bars:
                    data[sym] = bars
                else:
                    failed[sym] = "no bars"
            except Exception as exc:
                failed[sym] = str(exc)[:120]
            if done % 100 == 0 or done == len(symbols):
                say(f"  history: {done}/{len(symbols)} symbols ({len(data)} ok, {len(failed)} missing)")
    path = Path(out_dir) / HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    save_history(path, data, failed)
    return path


def save_history(path: Path, data: dict, failed: dict | None = None) -> None:
    body = {"fetched_at": datetime.now(IST).isoformat(), "interval": "5m", "failed": failed or {},
            "symbols": {s: [[b[0].isoformat(), b[1], b[2], b[3], b[4], b[5]] for b in bars]
                        for s, bars in data.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(json.dumps(body, separators=(",", ":")).encode(), compresslevel=5))
    tmp.replace(path)


def load_history(path: str | Path) -> dict[str, list[Bar]]:
    raw = json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))
    return {s: [(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]
            for s, rows in raw.get("symbols", {}).items()}


def by_day(bars: list[Bar]) -> dict[date, list[Bar]]:
    out: dict = {}
    for b in bars:
        out.setdefault(b[0].date(), []).append(b)
    return out


def last_day(history: dict[str, list[Bar]]) -> date | None:
    days = [bars[-1][0].date() for bars in history.values() if bars]
    return max(days) if days else None


# ------------------------------------------------------------------------ baselines

@dataclass(frozen=True)
class Baseline:
    days: int                         # prior sessions used
    prev_close: float | None
    atr_daily: float | None           # rupees, mean true range of the last 14 sessions
    rvol_base: float | None           # median volume of the 09:15 5-minute bar
    fh_sd: float | None               # s.d. of the 09:15-09:45 return incl. the gap (fraction)
    turnover_5m: float | None         # median rupee turnover per 5-minute bar


@dataclass(frozen=True)
class DaySummary:
    day: date
    o: float
    h: float
    l: float
    c: float
    first_vol: float | None          # volume of the 09:15 5-minute bar
    close_0940: float | None         # close of the 09:40 bar (= price at 09:45)
    turnover_5m: float               # median rupee turnover per 5-minute bar that day


def summarize(bars: list[Bar]) -> list[DaySummary]:
    out = []
    for d, rows in sorted(by_day(bars).items()):
        first = rows[0][5] if rows[0][0].time() == SESSION_OPEN else None
        b940 = next((x[4] for x in rows if x[0].time() == time(9, 40)), None)
        out.append(DaySummary(d, rows[0][1], max(x[2] for x in rows), min(x[3] for x in rows), rows[-1][4],
                              first, b940, median(x[4] * x[5] for x in rows)))
    return out


class HistoryIndex:
    """Per-symbol daily summaries, built once; baselines for any day in O(lookback)."""

    def __init__(self, history: dict[str, list[Bar]]):
        self.summaries = {s: summarize(b) for s, b in history.items() if b}
        self.daily_bars = {s: by_day(b) for s, b in history.items() if b}

    def days(self) -> list[date]:
        return sorted({x.day for rows in self.summaries.values() for x in rows})

    def bars_on(self, symbol: str, day: date) -> list[Bar]:
        return self.daily_bars.get(symbol, {}).get(day, [])

    def baselines(self, day: date, lookback: int = 14) -> dict[str, Baseline]:
        out = {}
        for sym, rows in self.summaries.items():
            prior = [x for x in rows if x.day < day][-(lookback + 1):]
            if not prior:
                continue
            trs = [max(x.h - x.l, abs(x.h - prior[i - 1].c), abs(x.l - prior[i - 1].c))
                   for i, x in enumerate(prior) if i > 0][-lookback:]
            recent = prior[-lookback:]
            firsts = [x.first_vol for x in recent if x.first_vol is not None]
            fh = [x.close_0940 / prior[i - 1].c - 1 for i, x in enumerate(prior)
                  if i > 0 and x.close_0940 is not None and prior[i - 1].c > 0][-lookback:]
            base = median(firsts) if len(firsts) >= 5 else None
            out[sym] = Baseline(
                days=len(recent), prev_close=prior[-1].c,
                atr_daily=(sum(trs) / len(trs)) if len(trs) >= 5 else None,
                rvol_base=base if base else None,
                fh_sd=pstdev(fh) if len(fh) >= 10 else None,
                turnover_5m=median(x.turnover_5m for x in prior[-5:]))
        return out


def baselines_for(history: dict[str, list[Bar]], day: date, lookback: int = 14) -> dict[str, Baseline]:
    return HistoryIndex(history).baselines(day, lookback)
