"""sr_eval.py -- measure sr.py on recorded sessions (no look-ahead in the engine).

Record real sessions first:   python sr.py --record snaps --lot-size NIFTY=<n> ...
Then evaluate them:           python sr_eval.py snaps --lot-size NIFTY=<n> ...
                              python sr_eval.py snaps --split 2026-11-01   (later days = out-of-sample)

How it works
  1. ENGINE   every snapshot of a day is replayed through sr.Engine in time order, exactly as it was
              fetched; the engine only ever sees data that existed at that moment.
  2. OUTCOME  each alert is then followed on the day's final 1-minute candles (hindsight is used only
              here, for scoring): stop and target touched in the same candle counts as the stop
              (conservative); T1 moves the stop to entry; whatever is open is closed at the 15:00
              hard exit. R = result / initial risk. The premium P&L uses the contract's recorded BID
              in the first snapshot at or after the exit, minus round-trip costs; if that snapshot or
              bid does not exist the alert is "unpriced" and left out of the rupee figures.
  3. ORACLE   an opportunity minute is a completed candle, inside an entry window, where sr.py's own
              causal regime (computed from candles up to that minute only) is BULLISH or BEARISH and an
              entry at its close with a stop beyond the last confirmed swing (0.6-2.5 ATR) would have
              reached +1.5R before the stop and before 15:00. Consecutive minutes (gap <= 5) form one
              episode. Opportunity minutes are also counted ignoring the regime, to show what the strict
              trend gate gives up.
  4. METRICS  precision is reported two ways: alerts reaching T1 before the stop, and alerts that ended
              profitable (in R) under the exit rule; false positives = alerts that ended with a loss;
              recall = episodes with an alert in the same direction from 2 minutes
              before the episode to its end; expectancy = mean R and mean net rupees per alert.
              Missed episodes list what the audit log says the engine decided at those minutes.

The oracle is a definition, not ground truth. Do not tune parameters on the sessions you report as
out-of-sample.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import sr
from sr import IST, SNAP_RE, Candle, Config, Engine, classify_regime, index_blocks, parse_snapshot, session_window

EPISODE_GAP_MIN = 5
ORACLE_R = 1.5


@dataclass
class Outcome:
    index: str
    side: str
    setup: str
    trigger: str
    result: str                       # STOP / T1->BE / T2 / TIME / OPEN
    r: float
    t1_first: bool
    exit_time: str
    net_rs: float | None = None


@dataclass
class DayReport:
    day: str
    alerts: list = field(default_factory=list)
    episodes: list = field(default_factory=list)      # dicts with detected flag
    minutes_trend: int = 0
    minutes_any: int = 0
    missed: list = field(default_factory=list)
    decisions: Counter = field(default_factory=Counter)


def day_of(f: Path) -> str:
    m = SNAP_RE.search(f.name)
    return m.group(1) if m else "unknown"


def follow(sig: sr.Signal, cs: list[Candle], square_off) -> Outcome:
    d = 1 if sig.side == "CALL" else -1
    trig = sr.parse_ts(sig.trigger_ts)
    risk = abs(sig.entry - sig.stop) or 1e-9
    stop, t1_hit, last = sig.stop, False, None
    for c in cs:
        if c.ts <= trig:
            continue
        if (c.ts + timedelta(minutes=1)).time() > square_off:
            break
        lo, hi = (c.low, c.high) if d == 1 else (-c.high, -c.low)
        e, st_, t1, t2 = sig.entry * d, stop * d, sig.target1 * d, sig.target2 * d
        if lo <= st_:
            res = "T1->BE" if t1_hit else "STOP"
            return Outcome(sig.index, sig.side, sig.setup, sig.trigger_ts, res, (st_ - e) / risk, t1_hit,
                           (c.ts + timedelta(minutes=1)).isoformat())
        if hi >= t2:
            return Outcome(sig.index, sig.side, sig.setup, sig.trigger_ts, "T2", (t2 - e) / risk, True,
                           (c.ts + timedelta(minutes=1)).isoformat())
        if not t1_hit and hi >= t1:
            t1_hit, stop = True, sig.entry
        last = c
    if last is None or last.ts <= trig:
        return Outcome(sig.index, sig.side, sig.setup, sig.trigger_ts, "OPEN", 0.0, t1_hit, "")
    return Outcome(sig.index, sig.side, sig.setup, sig.trigger_ts, "TIME", (last.close - sig.entry) * d / risk, t1_hit,
                   (last.ts + timedelta(minutes=1)).isoformat())


def oracle_minutes(cs: list[Candle], cfg: Config, use_regime: bool) -> list[tuple[datetime, int]]:
    out = []
    for i in range(cfg.min_candles, len(cs)):
        c = cs[i]
        decide = c.ts + timedelta(minutes=1)
        if not session_window(decide).entries:
            continue
        hist = cs[:i + 1]
        if use_regime:
            d = classify_regime(hist, cfg).direction
            dirs = [d] if d else []
        else:
            dirs = [1, -1]
        atr = sr.atr_wilder(hist, 14)[-1] or (sum(sr.true_ranges(hist)) / len(hist))
        highs, lows = sr.swings(hist, cfg.swing_k)
        for d in dirs:
            piv = [p for _, p in (lows if d == 1 else highs) if (c.close - p) * d > 0]
            if not piv:
                continue
            stop = piv[-1] - d * cfg.stop_buffer_atr * atr
            risk = (c.close - stop) * d
            if not cfg.min_stop_atr * atr <= risk <= cfg.max_stop_atr * atr:
                continue
            goal = c.close + d * ORACLE_R * risk
            for x in cs[i + 1:]:
                if (x.ts + timedelta(minutes=1)).time() > cfg.square_off:
                    break
                lo, hi = (x.low, x.high) if d == 1 else (-x.high, -x.low)
                if lo <= stop * d:
                    break
                if hi >= goal * d:
                    out.append((c.ts, d))
                    break
    return out


def episodes(minutes: list[tuple[datetime, int]]) -> list[dict]:
    eps: list[dict] = []
    for ts, d in sorted(minutes):
        e = next((e for e in eps if e["side"] == d and (ts - e["end"]).total_seconds() <= EPISODE_GAP_MIN * 60), None)
        if e:
            e["end"] = ts
        else:
            eps.append({"side": d, "start": ts, "end": ts})
    return eps


def bid_at(files: list[tuple[datetime, Path]], when: datetime, index: str, strike: float, kind: str) -> float | None:
    for t, f in files:
        if t < when:
            continue
        try:
            blk = index_blocks(sr.load_snapshot(f)).get(index)
        except (OSError, ValueError):
            return None
        if not blk:
            return None
        s = parse_snapshot(index, blk, t.date())
        bk = s.depth.get((strike, kind))
        if bk and bk.bids:
            return bk.bids[0][0]
        leg = s.chain.leg(strike, kind) if s.chain else None
        return leg.bid if leg and leg.bid > 0 else None
    return None


def evaluate_day(day: str, files: list[Path], cfg: Config, indices: list[str]) -> DayReport:
    rep = DayReport(day)
    eng = Engine(cfg, indices, journal=None, sound=False, explain=False, out=io.StringIO(), keep_audit=True)
    timed = []
    last_payload = None
    for f in files:
        try:
            payload = sr.load_snapshot(f)
        except (OSError, ValueError):
            continue
        t = sr.snapshot_time(f, payload)
        timed.append((t, f))
        eng.process(payload, t, live=False)
        last_payload = payload
    if last_payload is None:
        return rep
    rep.decisions = Counter(r["event"] for r in eng.audit_rows)
    blocks = index_blocks(last_payload)
    for name in indices:
        if name not in blocks:
            continue
        cs = sr.parse_candles(sr.pick(blocks[name], "candles_1m", "candles", "1m", default=[]))
        sigs = eng.state[name].signals
        for s in sigs:
            o = follow(s, cs, cfg.square_off)
            if o.exit_time:
                bid = bid_at(timed, sr.parse_ts(o.exit_time), name, s.option["strike"], s.option["kind"])
                if bid is not None:
                    qty = s.lot_size * s.lots
                    o.net_rs = (bid - s.option["entry"]) * qty - sr.round_trip_costs(s.option["entry"], bid, qty, name, cfg)
            rep.alerts.append(o)
        mins = oracle_minutes(cs, cfg, use_regime=True)
        eps = episodes(mins)
        rep.minutes_trend += len(mins)
        rep.minutes_any += len(oracle_minutes(cs, cfg, use_regime=False))
        for e in eps:
            side = "CALL" if e["side"] == 1 else "PUT"
            hit = any(s.side == side and e["start"] - timedelta(minutes=2) <= sr.parse_ts(s.trigger_ts) <= e["end"]
                      for s in sigs)
            rep.episodes.append({"index": name, "side": side, "start": e["start"].isoformat(),
                                 "end": e["end"].isoformat(), "detected": hit})
            if not hit:
                lo, hi = e["start"], e["end"] + timedelta(minutes=1)
                why = Counter()
                for r in eng.audit_rows:
                    if r["index"] != name or "candle" not in r or r["event"] in ("state", "regime", "data_health"):
                        continue
                    ts = sr.parse_ts(r["candle"]["ts"])
                    if ts and lo <= ts <= hi and r["event"] != "detected":
                        why[f"{r['event']}: {r['reason'][:90]}"] += 1
                rep.missed.append({"index": name, "side": side, "start": f"{e['start']:%H:%M}",
                                   "end": f"{e['end']:%H:%M}",
                                   "engine": [k for k, _ in why.most_common(3)] or ["no setup detected"]})
    return rep


def summarise(reps: list[DayReport]) -> dict:
    al = [a for r in reps for a in r.alerts]
    resolved = [a for a in al if a.result != "OPEN"]
    t1 = [a for a in resolved if a.t1_first]
    priced = [a.net_rs for a in al if a.net_rs is not None]
    eps = [e for r in reps for e in r.episodes]
    det = [e for e in eps if e["detected"]]
    return {
        "sessions": len(reps), "alerts": len(al), "resolved": len(resolved),
        "precision_t1_before_stop": round(len(t1) / len(resolved), 3) if resolved else None,
        "precision_profitable": round(sum(1 for a in resolved if a.r > 0) / len(resolved), 3) if resolved else None,
        "false_positives": sum(1 for a in resolved if a.r < 0),
        "stopped_before_t1": sum(1 for a in resolved if a.result == "STOP"),
        "expectancy_r": round(sum(a.r for a in resolved) / len(resolved), 3) if resolved else None,
        "priced_alerts": len(priced),
        "net_expectancy_rs": round(sum(priced) / len(priced), 2) if priced else None,
        "net_total_rs": round(sum(priced), 2) if priced else None,
        "episodes_trend": len(eps), "episodes_detected": len(det),
        "recall": round(len(det) / len(eps), 3) if eps else None,
        "oracle_minutes_trend_gated": sum(r.minutes_trend for r in reps),
        "oracle_minutes_any_direction": sum(r.minutes_any for r in reps),
        "by_setup": dict(Counter(a.setup for a in al)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay recorded sessions through sr.py and measure it.")
    ap.add_argument("path", type=Path, help="folder of snapshots recorded with sr.py --record")
    ap.add_argument("indices", nargs="*")
    ap.add_argument("--split", help="YYYY-MM-DD: sessions before = development, on/after = out-of-sample")
    ap.add_argument("--min-score", type=float, default=Config.min_score)
    ap.add_argument("--lot-size", action="append", metavar="INDEX=N")
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--json", type=Path, help="write the full report as JSON")
    a = ap.parse_args(argv)
    cfg = Config(min_score=a.min_score, lots=max(1, a.lots))
    cfg.lot_size = sr.parse_lot_sizes(a.lot_size, {})
    indices = [x for x in (sr._norm_index(i) for i in a.indices) if x] or list(sr.INDICES)
    by_day: dict[str, list[Path]] = defaultdict(list)
    for f in sr.snapshot_files(a.path):
        by_day[day_of(f)].append(f)
    if not by_day:
        print(f"no snapshots under {a.path}")
        return 1
    reps = []
    for day in sorted(by_day):
        rep = evaluate_day(day, by_day[day], cfg, indices)
        reps.append(rep)
        s = summarise([rep])
        print(f"\n=== {day}: {len(by_day[day])} snapshots | alerts {s['alerts']} | T1-before-stop "
              f"{s['precision_t1_before_stop']} | profitable {s['precision_profitable']} | E[R] {s['expectancy_r']} | "
              f"net Rs/alert {s['net_expectancy_rs']} | recall {s['recall']} ({s['episodes_detected']}/"
              f"{s['episodes_trend']} trend episodes) | oracle minutes {s['oracle_minutes_trend_gated']} trend-gated vs "
              f"{s['oracle_minutes_any_direction']} any direction")
        for o in rep.alerts:
            print(f"   {o.trigger[11:16]} {o.index:9} {o.side:4} {o.setup:9} -> {o.result:6} {o.r:+.2f}R"
                  + (f"  net Rs {o.net_rs:,.0f}" if o.net_rs is not None else "  (unpriced)"))
        for m in rep.missed:
            print(f"   MISSED {m['index']} {m['side']} {m['start']}-{m['end']}: {' | '.join(m['engine'])}")
        print("   decisions: " + ", ".join(f"{k} {v}" for k, v in rep.decisions.most_common() if k not in ("state", "regime")))
    groups = {"all sessions": reps}
    if a.split:
        groups = {"development (before split)": [r for r in reps if r.day < a.split.replace("-", "")],
                  "OUT-OF-SAMPLE (on/after split)": [r for r in reps if r.day >= a.split.replace("-", "")]}
    report = {}
    for name, rs in groups.items():
        report[name] = summarise(rs)
        print(f"\n### {name}: " + json.dumps(report[name]))
    if a.json:
        report["days"] = [{"day": r.day, "alerts": [o.__dict__ for o in r.alerts], "episodes": r.episodes,
                           "missed": r.missed, "decisions": dict(r.decisions)} for r in reps]
        a.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("\nThe oracle is a hindsight definition; treat recall as a relative measure. Costs are Config rates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
