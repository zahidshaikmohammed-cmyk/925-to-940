"""research.py -- look for a strategy with a real edge in the downloaded 60-day history.

    python 945.py --bootstrap        (once: 60 days of 5-minute candles into data/history)
    python research.py               (a few minutes; prints the table and saves data/research.txt)

Every idea has FIXED rules written down before testing (no tuning to the data). The
days are split in time order: the first two thirds are the DESIGN period, the last
third is the TEST period the rules have never been looked at on. A strategy only
PASSES when it makes money after costs in BOTH periods, with enough trades.

Fills are realistic: a signal on a completed 5-minute bar is entered at the NEXT bar's
open, stops fill at the stop (or the open when price gaps through it), and the stop is
checked before the target inside a bar. Intraday trades cost 0.142% round trip; swing
(delivery) trades 0.25%. Liquidity and every reference value use only earlier data.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, time
from pathlib import Path
from statistics import median

from intelligence.history import HISTORY_FILE, load_history

INTRADAY_COST = 0.142        # % round trip
SWING_COST = 0.25            # % round trip (delivery: STT both sides)
SLOTS = 75                   # 5-minute bars 09:15 .. 15:25
EXIT_SLOT = 71               # the 15:10 bar: its close is the 15:15 square-off
UNIVERSE = 300               # most traded stocks (prior 10 days' median turnover)
MIN_PRICE = 50.0
PER_DAY = 5                  # at most 5 trades per strategy per day (what one person can take)


# --------------------------------------------------------------------------- data

class Day:
    __slots__ = ("o", "h", "l", "c", "v", "open", "high", "low", "close", "turnover")

    def __init__(self, bars):
        self.o = [None] * SLOTS
        self.h = [None] * SLOTS
        self.l = [None] * SLOTS
        self.c = [None] * SLOTS
        self.v = [0] * SLOTS
        for ts, o, h, l, c, v in bars:
            s = ((ts.hour * 60 + ts.minute) - (9 * 60 + 15)) // 5
            if 0 <= s < SLOTS:
                self.o[s], self.h[s], self.l[s], self.c[s], self.v[s] = o, h, l, c, v or 0
        # carry the last price over empty bars (illiquid minutes)
        last = None
        for s in range(SLOTS):
            if self.c[s] is None and last is not None:
                self.o[s] = self.h[s] = self.l[s] = self.c[s] = last
            if self.c[s] is not None:
                last = self.c[s]
        got = [s for s in range(SLOTS) if self.c[s] is not None]
        self.open = self.o[got[0]] if got else None
        self.close = self.c[EXIT_SLOT] if got and self.c[EXIT_SLOT] is not None else (self.c[got[-1]] if got else None)
        self.high = max(self.h[s] for s in got) if got else None
        self.low = min(self.l[s] for s in got) if got else None
        self.turnover = sum((self.c[s] or 0) * self.v[s] for s in range(SLOTS))

    def complete(self) -> bool:
        return self.o[0] is not None and self.c[EXIT_SLOT] is not None

    def vwap(self, upto: int) -> float | None:
        pv = vv = 0.0
        for s in range(upto + 1):
            if self.c[s] is None:
                continue
            pv += (self.h[s] + self.l[s] + self.c[s]) / 3 * self.v[s]
            vv += self.v[s]
        return pv / vv if vv else None


def load(path: Path):
    raw = load_history(path)
    days: dict[str, dict[date, Day]] = {}
    for sym, bars in raw.items():
        per: dict[date, list] = defaultdict(list)
        for b in bars:
            per[b[0].date()].append(b)
        days[sym] = {d: Day(sorted(bs)) for d, bs in per.items()}
    all_days = sorted({d for per in days.values() for d in per})
    # a trading day counts only when most stocks have the full session (drops today's partial day)
    full = [d for d in all_days if sum(1 for per in days.values() if d in per and per[d].complete()) > 0.5 * len(days)]
    return days, full


# --------------------------------------------------------------------------- simulation

def simulate(day: Day, entry_slot: int, side: int, stop: float, target: float | None = None,
             cost: float = INTRADAY_COST):
    """Enter at the open of entry_slot; exit at stop / target / 15:15. Returns (net %, R) or None."""
    if entry_slot > EXIT_SLOT or day.o[entry_slot] is None:
        return None
    entry = day.o[entry_slot]
    risk = side * (entry - stop)
    if risk <= 0 or risk / entry > 0.04:          # stop on the wrong side, or wider than 4%
        return None
    exit_price = None
    for s in range(entry_slot, EXIT_SLOT + 1):
        o, h, l, c = day.o[s], day.h[s], day.l[s], day.c[s]
        if (side > 0 and l <= stop) or (side < 0 and h >= stop):
            exit_price = min(o, stop) if side > 0 else max(o, stop)
            break
        if target is not None and ((side > 0 and h >= target) or (side < 0 and l <= target)):
            exit_price = max(o, target) if side > 0 else min(o, target)
            break
    if exit_price is None:
        exit_price = day.c[EXIT_SLOT]
    gross = side * (exit_price / entry - 1) * 100
    net = gross - cost
    return net, net / (risk / entry * 100)


# --------------------------------------------------------------------------- strategies
# Each returns candidate trades for one day: (score, symbol, entry_slot, side, stop, target).

def market_move(ctx, slot):
    vals = [ctx.today[s].c[slot] / ctx.today[s].open - 1 for s in ctx.universe
            if ctx.today[s].c[slot] is not None and ctx.today[s].open]
    return median(vals) if vals else 0.0


def gap_and_go(ctx):
    """Gap >= 2% that holds: the first 15 minutes close beyond the open in the gap's direction.
    Enter at 09:30 with the gap, stop the other side of the first 15 minutes, hold to 15:15."""
    out = []
    for s in ctx.universe:
        d, pc = ctx.today[s], ctx.prev[s].close
        gap = d.open / pc - 1
        if abs(gap) < 0.02:
            continue
        side = 1 if gap > 0 else -1
        hi, lo = max(d.h[0:3]), min(d.l[0:3])
        if side * (d.c[2] - d.open) <= 0:
            continue
        out.append((abs(gap), s, 3, side, lo if side > 0 else hi, None))
    return out


def gap_fade(ctx):
    """Gap >= 2% that fails: the first 15 minutes close back against the gap. Enter against the
    gap at 09:30, stop beyond the first 15 minutes' extreme, target the previous close."""
    out = []
    for s in ctx.universe:
        d, pc = ctx.today[s], ctx.prev[s].close
        gap = d.open / pc - 1
        if abs(gap) < 0.02:
            continue
        side = -1 if gap > 0 else 1
        hi, lo = max(d.h[0:3]), min(d.l[0:3])
        if side * (d.c[2] - d.open) <= 0:
            continue
        out.append((abs(gap), s, 3, side, hi if side < 0 else lo, pc))
    return out


def orb30_with_market(ctx):
    """30-minute opening range breakout, only in the market's direction at 09:45, on stocks
    trading 2x their usual opening volume. Stop the other side of the range, hold to 15:15."""
    mkt = market_move(ctx, 5)
    side = 1 if mkt > 0.002 else (-1 if mkt < -0.002 else 0)
    if not side:
        return []
    out = []
    for s in ctx.universe:
        d = ctx.today[s]
        hi, lo = max(d.h[0:6]), min(d.l[0:6])
        base = ctx.open_volume.get(s)
        if not base or sum(d.v[0:6]) < 2 * base:
            continue
        for k in range(6, 30):                    # breakout before 11:45
            if (side > 0 and d.c[k] > hi) or (side < 0 and d.c[k] < lo):
                out.append((sum(d.v[0:6]) / base, s, k + 1, side, lo if side > 0 else hi, None))
                break
    return out


def relative_strength_1015(ctx):
    """At 10:15 take the stocks moving most WITH the market (vs the market since the open)
    and above/below VWAP. Stop the morning extreme, hold to 15:15."""
    slot = 11                                     # the 10:10 bar is complete at 10:15
    mkt = market_move(ctx, slot)
    side = 1 if mkt > 0.002 else (-1 if mkt < -0.002 else 0)
    if not side:
        return []
    out = []
    for s in ctx.universe:
        d = ctx.today[s]
        rel = (d.c[slot] / d.open - 1) - mkt
        vw = d.vwap(slot)
        if side * rel < 0.01 or vw is None or side * (d.c[slot] - vw) <= 0:
            continue
        stop = min(d.l[0:slot + 1]) if side > 0 else max(d.h[0:slot + 1])
        out.append((side * rel, s, slot + 1, side, stop, None))
    return out


def late_trend(ctx):
    """At 14:00, stocks up/down >= 2% on the day, on the right side of VWAP and within 0.5% of
    the day's extreme: ride the last hour. Stop the 13:00-14:00 extreme, hold to 15:15."""
    slot = 56                                     # the 13:55 bar is complete at 14:00
    out = []
    for s in ctx.universe:
        d = ctx.today[s]
        move = d.c[slot] / ctx.prev[s].close - 1
        if abs(move) < 0.02:
            continue
        side = 1 if move > 0 else -1
        vw = d.vwap(slot)
        hi, lo = max(d.h[0:slot + 1]), min(d.l[0:slot + 1])
        near = (hi - d.c[slot]) / hi if side > 0 else (d.c[slot] - lo) / lo
        if vw is None or side * (d.c[slot] - vw) <= 0 or near > 0.005:
            continue
        stop = min(d.l[44:slot + 1]) if side > 0 else max(d.h[44:slot + 1])
        out.append((abs(move), s, slot + 1, side, stop, None))
    return out


def stretch_reversion(ctx):
    """At 11:00, stocks 4%+ away from the open while the market moved < 0.5%, stretched > 2%
    from VWAP: bet on a move back to VWAP. Stop 1% beyond the day's extreme, target VWAP."""
    slot = 20                                     # the 10:55 bar is complete at 11:00
    mkt = market_move(ctx, slot)
    if abs(mkt) > 0.005:
        return []
    out = []
    for s in ctx.universe:
        d = ctx.today[s]
        move = d.c[slot] / d.open - 1
        vw = d.vwap(slot)
        if abs(move) < 0.04 or vw is None or abs(d.c[slot] / vw - 1) < 0.02:
            continue
        side = -1 if move > 0 else 1
        ext = max(d.h[0:slot + 1]) if side < 0 else min(d.l[0:slot + 1])
        out.append((abs(move), s, slot + 1, side, ext * (1.01 if side < 0 else 0.99), vw))
    return out


INTRADAY = {
    "GAP_GO": gap_and_go,
    "GAP_FADE": gap_fade,
    "ORB30_MKT": orb30_with_market,
    "RS_1015": relative_strength_1015,
    "LATE_TREND": late_trend,
    "STRETCH_REV": stretch_reversion,
}


class Ctx:
    pass


def run_intraday(days, full):
    results = {name: [] for name in INTRADAY}      # name -> [(date, net%, R)]
    for i in range(10, len(full)):
        d, prev_d, hist = full[i], full[i - 1], full[i - 10:i]
        liquid = []
        for s, per in days.items():
            if d not in per or prev_d not in per or not per[d].complete() or not per[prev_d].close:
                continue
            past = [per[x].turnover for x in hist if x in per]
            if len(past) < 8 or per[prev_d].close < MIN_PRICE:
                continue
            liquid.append((median(past), s))
        liquid.sort(reverse=True)
        ctx = Ctx()
        ctx.universe = [s for _, s in liquid[:UNIVERSE]]
        ctx.today = {s: days[s][d] for s in ctx.universe}
        ctx.prev = {s: days[s][prev_d] for s in ctx.universe}
        ctx.open_volume = {}
        for s in ctx.universe:
            vols = [sum(days[s][x].v[0:6]) for x in hist if x in days[s]]
            if len(vols) >= 8:
                ctx.open_volume[s] = median(vols)
        for name, rule in INTRADAY.items():
            cands = sorted(rule(ctx), key=lambda x: -x[0])[:PER_DAY]
            for _, s, slot, side, stop, target in cands:
                r = simulate(ctx.today[s], slot, side, stop, target)
                if r:
                    results[name].append((d, r[0], r[1]))
    return results


def run_swing(days, full):
    """SWING_BREAKOUT (long only, delivery): a liquid stock (>= Rs 5 crore a day) closes at a
    20-day closing high on above-median turnover. Buy the next day's open, stop 2 x 10-day
    average range below the entry, exit at the 5th day's close."""
    out = []
    for i in range(21, len(full) - 6):
        d = full[i]
        cands = []
        for s, per in days.items():
            win = [per.get(x) for x in full[i - 20:i + 1]]
            if any(w is None or w.close is None for w in win) or win[-1].close < MIN_PRICE:
                continue
            closes = [w.close for w in win]
            if closes[-1] < max(closes[:-1]):
                continue
            turn = [w.turnover for w in win]
            if turn[-1] < median(turn[:-1]) or median(turn[:-1]) < 5e7:   # >= Rs 5 crore a day
                continue
            atr = sum(w.high - w.low for w in win[-10:]) / 10
            cands.append((turn[-1] / median(turn[:-1]), s, atr))
        for _, s, atr in sorted(cands, reverse=True)[:PER_DAY]:
            per = days[s]
            nxt = [per.get(x) for x in full[i + 1:i + 6]]
            if any(n is None or n.open is None for n in nxt):
                continue
            entry = nxt[0].open
            stop = entry - 2 * atr
            exit_price = nxt[-1].close
            for n in nxt:
                if n.low <= stop:
                    exit_price = min(n.open, stop)
                    break
            net = (exit_price / entry - 1) * 100 - SWING_COST
            out.append((d, net, net / (2 * atr / entry * 100)))
    return out


# --------------------------------------------------------------------------- report

def stats(rows):
    if not rows:
        return None
    nets = [r[1] for r in rows]
    wins = [x for x in nets if x > 0]
    gain, loss = sum(wins), -sum(x for x in nets if x <= 0)
    return {"n": len(nets), "win": len(wins) / len(nets), "avg": sum(nets) / len(nets),
            "avg_r": sum(r[2] for r in rows) / len(rows), "total": sum(nets),
            "pf": gain / loss if loss > 0 else float("inf")}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default="data")
    args = p.parse_args(argv)
    path = Path(args.data_dir) / "history" / HISTORY_FILE
    if not path.exists():
        print(f"{path} not found: run  python 945.py --bootstrap  first")
        return 1
    print(f"Loading {path} ...", flush=True)
    days, full = load(path)
    split = full[10 + (len(full) - 10) * 2 // 3] if len(full) > 15 else full[-1]
    print(f"{len(days)} stocks, {len(full)} full trading days {full[0]} .. {full[-1]}; "
          f"TEST period starts {split}", flush=True)
    print("Testing intraday strategies ...", flush=True)
    results = run_intraday(days, full)
    print("Testing the swing strategy ...", flush=True)
    results["SWING_BREAKOUT"] = run_swing(days, full)

    lines = ["", "=" * 104,
             f"STRATEGY RESEARCH  {full[0]} .. {full[-1]}  |  DESIGN before {split}, TEST from {split} (unseen)",
             "Net of costs (intraday 0.142%, swing 0.25%), next-bar entries, stop before target, max 5 trades/day",
             "=" * 104,
             f"{'Strategy':<16}{'Period':<8}{'Trades':>7}{'Win%':>7}{'Avg%':>8}{'AvgR':>7}{'Total%':>9}{'PF':>6}"]
    verdicts = []
    for name, rows in results.items():
        parts = {"DESIGN": [r for r in rows if r[0] < split], "TEST": [r for r in rows if r[0] >= split]}
        ok = True
        for label, part in parts.items():
            st = stats(part)
            if not st:
                lines.append(f"{name:<16}{label:<8}{0:>7}")
                ok = False
                continue
            lines.append(f"{name:<16}{label:<8}{st['n']:>7}{st['win']:>7.0%}{st['avg']:>+8.3f}{st['avg_r']:>+7.2f}"
                         f"{st['total']:>+9.1f}{st['pf']:>6.2f}")
            ok &= st["avg"] > 0 and st["pf"] > 1.1 and st["n"] >= 20
        verdicts.append((name, ok))
        lines.append("-" * 104)
    lines.append("VERDICT (profitable after costs in BOTH periods, PF > 1.1, 20+ trades each):")
    for name, ok in verdicts:
        lines.append(f"  {name:<16} {'PASS' if ok else 'fail'}")
    if not any(ok for _, ok in verdicts):
        lines.append("  Nothing passed: none of these ideas shows an edge after costs on this data.")
    lines.append("Avg% is the average profit per trade as % of the position (Rs 5,000 x Avg%/100 per trade).")
    text = "\n".join(lines)
    print(text)
    out = Path(args.data_dir) / "research.txt"
    out.write_text(text + "\n", encoding="utf-8")
    print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
