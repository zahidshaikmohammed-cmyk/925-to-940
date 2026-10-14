"""Prior-session inputs: previous-day high/low/close and the opening-range volume baseline.

Sources (both already produced by this repository; nothing is downloaded here):
  * data/sessions/YYYY-MM-DD.json.gz -- PSYGRID 1-minute sessions saved by 945.py and by the
    strategy-engine runner itself (same feed, so volumes are on the live scale);
  * data/history/yahoo_5m.json.gz    -- optional 60-day 5-minute history (945.py --bootstrap).

Rules (no look-ahead, no silent repair):
  * only sessions strictly before ``today`` are read;
  * PDH/PDL/PDC come only from the IMMEDIATELY previous NSE trading day, and only from a
    complete session (last bar >= 15:25); otherwise they are absent (None), never guessed;
  * PSYGRID sessions win over Yahoo for the same day;
  * the opening-volume baseline (median 09:15-09:29 volume) uses one source only -- PSYGRID
    if it has >= 5 sessions, else Yahoo -- because their volume scales can differ;
  * if the previous close from history differs from the feed's previous_close by > 0.5%,
    the prior levels are dropped for that stock (likely corporate action or bad data).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from pathlib import Path
from statistics import median

from .features import PriorLevels

FULL_SESSION_LAST_BAR = time(15, 25)
OR_END = time(9, 30)


@dataclass(frozen=True)
class DayStats:
    high: float
    low: float
    close: float
    or_volume: float | None      # None unless the whole 09:15-09:29 window is present
    complete: bool


def previous_trading_day(today: date, is_trading_day) -> date:
    d = today - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d


def _stats_from_rows(rows, minutes_per_bar: int) -> DayStats | None:
    """rows: [(ts, o, h, l, c, v)] for one day, ts-sorted."""
    rows = [r for r in rows if time(9, 15) <= r[0].time() < time(15, 30)]
    if not rows:
        return None
    or_rows = [r for r in rows if r[0].time() < OR_END]
    need = 15 // minutes_per_bar
    or_vol = sum(r[5] for r in or_rows) if len(or_rows) == need else None
    last_bar_end = (rows[-1][0] + timedelta(minutes=minutes_per_bar)).time()
    complete = last_bar_end >= time(15, 25) and rows[0][0].time() == time(9, 15)
    return DayStats(max(r[2] for r in rows), min(r[3] for r in rows), rows[-1][4], or_vol, complete)


def load_psygrid_days(sessions_dir: str | Path, today: date, last: int = 15) -> dict[str, dict[date, DayStats]]:
    from intelligence.selector_data import load_session_file
    files = sorted(p for p in Path(sessions_dir).glob("*.json.gz") if p.name[:10] < today.isoformat())[-last:]
    out: dict[str, dict[date, DayStats]] = {}
    for f in files:
        try:
            raw = load_session_file(f)
        except Exception as exc:
            print(f"[HISTORY-WARN] skipped {f.name}: {exc}")
            continue
        if raw.session_date >= today:
            continue
        for sym, ser in raw.stocks.items():
            rows = [(ser.ts[i], ser.o[i], ser.h[i], ser.l[i], ser.c[i], ser.v[i]) for i in range(len(ser))
                    if ser.ts[i].date() == raw.session_date]
            st = _stats_from_rows(rows, 1)
            if st:
                out.setdefault(sym, {})[raw.session_date] = st
    return out


def load_yahoo_days(path: str | Path, today: date) -> dict[str, dict[date, DayStats]]:
    p = Path(path)
    if not p.exists():
        return {}
    from intelligence.history import by_day, load_history
    try:
        hist = load_history(p)
    except Exception as exc:
        print(f"[HISTORY-WARN] could not read {p}: {exc}")
        return {}
    out: dict[str, dict[date, DayStats]] = {}
    for sym, bars in hist.items():
        for d, rows in by_day(bars).items():
            if d < today:
                st = _stats_from_rows(rows, 5)
                if st:
                    out.setdefault(sym, {})[d] = st
    return out


def build_priors(today: date, is_trading_day, psygrid: dict, yahoo: dict,
                 feed_prev_close: dict[str, float | None], lookback: int = 14,
                 min_or_days: int = 5) -> tuple[dict[str, PriorLevels], dict]:
    prev = previous_trading_day(today, is_trading_day)
    priors: dict[str, PriorLevels] = {}
    report = {"previous_trading_day": prev.isoformat(), "with_levels": 0, "with_or_baseline": 0,
              "pdc_mismatch": 0, "no_previous_session": 0}
    for sym in sorted(set(psygrid) | set(yahoo) | set(feed_prev_close)):
        ps, yh = psygrid.get(sym, {}), yahoo.get(sym, {})
        day = ps.get(prev) or yh.get(prev)
        pdh = pdl = pdc = None
        if day is not None and day.complete:
            pdh, pdl, pdc = day.high, day.low, day.close
            fpc = feed_prev_close.get(sym)
            if fpc and abs(pdc / fpc - 1.0) > 0.005:
                pdh = pdl = pdc = None
                report["pdc_mismatch"] += 1
        else:
            report["no_previous_session"] += 1
        base, ndays, source = None, 0, ""
        for name, src in (("psygrid", ps), ("yahoo", yh)):
            vols = [src[d].or_volume for d in sorted(src) if d < today and src[d].or_volume][-lookback:]
            if len(vols) >= min_or_days:
                base, ndays, source = median(vols), len(vols), name
                break
        if pdh is None and base is None:
            continue
        priors[sym] = PriorLevels(prev.isoformat(), pdh, pdl, pdc, base, ndays, source)
        report["with_levels"] += pdh is not None
        report["with_or_baseline"] += base is not None
    return priors, report


def load_priors(today: date, data_dir: str | Path, is_trading_day, feed_prev_close: dict) -> tuple[dict, dict]:
    d = Path(data_dir)
    psy = load_psygrid_days(d / "sessions", today)
    yah = load_yahoo_days(d / "history" / "yahoo_5m.json.gz", today)
    return build_priors(today, is_trading_day, psy, yah, feed_prev_close)
