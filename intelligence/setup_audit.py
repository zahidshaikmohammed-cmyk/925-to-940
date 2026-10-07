"""Daily audit: did the engine see everything, on time, with the right numbers?

`python 945.py --audit [--date D]` re-runs the setup engine minute by minute over the
day's saved feed (data/sessions/D.json.gz) exactly as the live scan runs it, then
compares with what the live scan journaled (data/setups/D.jsonl):

  1. coverage      feed stocks, stocks with history, liquid stocks, median RVOL
  2. setups        every setup armed / triggered / closed, with R after costs
  3. live vs replay  every setup the replay found that the live run did not (and
                   the reverse) -- a mismatch means a feed gap or the scan was off
  4. alert delay   for each live trigger: seconds from the candle that crossed the
                   level to the moment the scan logged it
  5. near misses   stocks that failed exactly one condition, and which one
  6. big movers    the day's largest moves and what the engine said about each
"""
from __future__ import annotations

import bisect
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median

from .history import IST, Baseline, HistoryIndex
from .setups import NAMES, SetupConfig, SetupEngine, SetupTrade, build_day, to_five_minute

ONE = timedelta(minutes=1)


def replay_day(raw, index: HistoryIndex, cfg: SetupConfig, cost_pct: float):
    """The live path on a saved day: 1-minute triggers, 5-minute arming, every minute."""
    day = raw.session_date
    base: dict[str, Baseline] = index.baselines(day)
    for sym, s in raw.stocks.items():
        if sym in base and s.previous_close:
            base[sym] = replace(base[sym], prev_close=s.previous_close)
    end = datetime.combine(day, time(15, 31), IST)
    one = {sym: [(s.ts[i], s.o[i], s.h[i], s.l[i], s.c[i], s.v[i]) for i in range(len(s))]
           for sym, s in raw.stocks.items() if len(s)}
    stamps = {sym: [r[0] for r in rows] for sym, rows in one.items()}
    bars = {sym: build_day(sym, five, cfg.atr_bars) for sym, rows in one.items()
            if (five := to_five_minute(rows, end))}
    eng = SetupEngine(cfg, cost_pct)
    t = datetime.combine(day, time(9, 20), IST)
    stop = datetime.combine(day, time(15, 20), IST)
    while t <= stop:
        fine = {tr.symbol: one[tr.symbol][:bisect.bisect_left(stamps[tr.symbol], t)]
                for tr in eng.trades if tr.live and tr.symbol in one}
        eng.step(day, bars, base, t, fine)
        t += ONE
    return eng, base, bars


def read_live(path: Path) -> tuple[dict, list[dict]]:
    """Latest state per live setup + every TRIGGERED row (with its wall-clock stamp)."""
    latest, triggers = {}, []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            latest[row["trade"]["id"]] = row["trade"]
            if row["kind"] == "TRIGGERED":
                triggers.append(row)
    return latest, triggers


def _hm(iso):
    return datetime.fromisoformat(iso).astimezone(IST).strftime("%H:%M") if iso else "--:--"


def audit(raw, index: HistoryIndex, cfg: SetupConfig, cost_pct: float, live_journal: Path) -> str:
    day = raw.session_date
    eng, base, bars = replay_day(raw, index, cfg, cost_pct)
    liquid = [s for s in bars if s in base and (base[s].turnover_5m or 0) >= cfg.min_turnover_5m]
    L = ["=" * 88, f"945 SETUP AUDIT  {day}", "=" * 88]

    # 1. coverage
    no_hist = sorted(s for s in raw.stocks if s not in base)
    L += ["", "1. COVERAGE",
          f"   feed stocks {len(raw.stocks)} | with 14-day history {len(raw.stocks) - len(no_hist)} | "
          f"liquid enough (>= Rs {cfg.min_turnover_5m / 1e5:.0f} lakh per 5-min bar) {len(liquid)}",
          f"   median RVOL at 09:20: {eng.diag.get('orb_median_rvol', 'n/a')} "
          f"(near 1.0 = Yahoo history and the feed count volume the same way)"]
    if no_hist:
        L.append(f"   no history, never scanned for setups ({len(no_hist)}): {', '.join(no_hist[:15])}"
                 f"{' ...' if len(no_hist) > 15 else ''}")

    # 2. setups found by the replay
    L += ["", "2. SETUPS THE ENGINE FOUND (replay of the full day)"]
    if not eng.trades:
        L.append("   none")
    for t in sorted(eng.trades, key=lambda x: (x.armed_at, x.setup, x.symbol)):
        res = (f"triggered {_hm(t.entry_time)} @ {t.entry:.2f}, {t.exit_reason} {_hm(t.exit_time)} @ {t.exit:.2f}, "
               f"{t.net_r:+.2f}R net" if t.state == "CLOSED" else t.state.lower())
        L.append(f"   {_hm(t.armed_at)} {t.setup} {t.symbol:<12} {t.direction:<5} trigger {t.trigger:.2f} "
                 f"stop {t.stop:.2f} -> {res}")
    closed = [t for t in eng.trades if t.state == "CLOSED"]
    for k in NAMES:
        mine = [t for t in closed if t.setup == k]
        if mine:
            L.append(f"   {k}: {len(mine)} traded, {sum(1 for t in mine if t.net_r > 0)} won, "
                     f"{sum(t.net_r for t in mine):+.2f}R net")

    # 3. live vs replay
    live, triggers = read_live(live_journal)
    L += ["", "3. LIVE RUN vs REPLAY"]
    if not live:
        L.append(f"   no live journal ({live_journal}) -- the scan was not running with setups that day")
    else:
        rep = {t.id: t for t in eng.trades}
        missed = sorted(set(rep) - set(live))
        extra = sorted(set(live) - set(rep))
        differ = [i for i in set(rep) & set(live)
                  if (rep[i].trigger, rep[i].stop) != (live[i]["trigger"], live[i]["stop"])]
        if not (missed or extra or differ):
            L.append(f"   identical: all {len(rep)} setups were armed live with the same levels")
        for i in missed:
            L.append(f"   MISSED LIVE  {_hm(rep[i].armed_at)} {rep[i].setup} {rep[i].symbol} -- the scan was not "
                     f"running or the feed had a gap at that minute")
        for i in extra:
            L.append(f"   ONLY LIVE    {live[i]['setup']} {live[i]['symbol']} -- the saved session differs from "
                     f"what the live feed showed then (late or corrected candles)")
        for i in differ:
            L.append(f"   LEVELS DIFFER {rep[i].setup} {rep[i].symbol}: live {live[i]['trigger']}/{live[i]['stop']} "
                     f"vs replay {rep[i].trigger}/{rep[i].stop}")

    # 4. alert delay
    L += ["", "4. ALERT DELAY (live triggers)"]
    delays = []
    for row in triggers:
        if not row.get("logged_at"):
            continue
        crossed = datetime.fromisoformat(row["at"])
        told = datetime.fromisoformat(row["logged_at"])
        delays.append((told - crossed).total_seconds())
        L.append(f"   {row['trade']['setup']} {row['trade']['symbol']:<12} crossed in the {_hm(row['at'])} candle, "
                 f"you were told at {told.astimezone(IST):%H:%M:%S} ({(told - crossed).total_seconds() - 60:.0f} s "
                 f"after that candle closed)")
    if delays:
        L.append(f"   median {median(delays) - 60:.0f} s after the crossing candle closed. A stop-entry order "
                 f"placed at the ARMED level fills at the level with no delay.")
    elif triggers:
        L.append("   (older journal without timestamps)")
    else:
        L.append("   no live triggers")

    # 5. near misses
    L += ["", "5. NEAR MISSES (failed exactly one condition)"]
    seen = set()
    shown = {k: 0 for k in NAMES}
    for n in eng.near:
        key = (n["setup"], n["symbol"])
        if key in seen or shown[n["setup"]] >= 8:
            continue
        seen.add(key)
        shown[n["setup"]] += 1
        L.append(f"   {_hm(n['at'])} {n['setup']} {n['symbol']:<12} {n['direction']:<5} failed {n['failed']}: {n['detail']}")
    if not seen:
        L.append("   none")

    # 6. biggest movers
    L += ["", "6. THE DAY'S BIGGEST MOVES (liquid stocks) and what the engine said"]
    moves = []
    for s in liquid:
        b = bars[s]
        if len(b.c) > 1:
            moves.append((abs(b.c[-1] / b.o[0] - 1), s, b.c[-1] / b.o[0] - 1))
    moves.sort(reverse=True)
    for _, s, r in moves[:10]:
        said = [f"{t.setup} {t.direction} {t.state.lower()}" + (f" {t.net_r:+.2f}R" if t.state == "CLOSED" else "")
                for t in eng.trades if t.symbol == s]
        near = next((f"near miss {n['setup']}: {n['failed']} ({n['detail']})" for n in eng.near if n["symbol"] == s), None)
        L.append(f"   {s:<12} {r * 100:+6.2f}% open->close | " + ("; ".join(said) if said else
                 (near or "no setup pattern formed (no opening volume surge, no VWAP pullback, not a top first half-hour)")))
    L += ["", "=" * 88]
    return "\n".join(L)
