"""opt.py -- forced #1 index option trade (NIFTY / BANKNIFTY / SENSEX).

One command, like bbbbb.py: scans every index in both directions, scores
each CALL and PUT 0-100, and ALWAYS prints the single best option trade with
strike, premium SL/TP, the index levels behind them, then beeps.

The score says how good the forced pick is:
    75-100 STRONG    direction, flow and structure agree
    55-75  MODERATE  mostly agree
    0-55   WEAK      best of a poor lot -- smallest size or skip

Score parts (direction evidence):
    25% market breadth from the 400-stock universe agrees
    15% index trend since the 09:15 open
    15% position vs the session average price (near it on the right side is best)
    15% last 15-minute momentum
    15% option OI flow near the money (put writing = bullish, call writing = bearish)
    15% room to the next key level vs the risk
  +15   a clean breakout-retest / rejection at a key zone (from index.py)
  -15   entering right under resistance / right above support
  -15   badly stretched from the session average

Stop = recent 5-minute swing (clamped to 0.6-1.5 x 5m ATR).
Targets = next key zones (PDH/PDL, opening range, OI walls, round numbers),
or 1.5R / 2.5R when no zone is in reach.

Usage:
    python opt.py              all three indices, one #1
    python opt.py NIFTY        one index only
    python opt.py --self-test
"""
from __future__ import annotations

import argparse
import contextlib
import io
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time as dtime

from index import (
    INDEXES,
    OR_END,
    IndexConfig,
    IndexSignal,
    OptionPick,
    Zone,
    build_zones,
    completed_candles,
    completed_five_minute,
    find_signals,
    key_zones,
    load_store,
    market_bias,
    missing_minutes,
    oi_levels,
    pick_option,
    previous_day,
    price_levels,
    update_store,
)
from old import atr5, beep
from psygrid_client import PsygridClient
from run_engine import BASE_URL, Audit, is_trading_day, now_ist
from strategy_930 import IST, Candle, clamp, scale

SESSION_START = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


@dataclass(frozen=True)
class ForcedTrade:
    index: str
    side: str                  # CALL / PUT
    score: float
    grade: str
    entry: float
    stop: float
    target1: float
    target2: float
    risk: float
    target_basis: str
    setup: str
    option: OptionPick | None
    expiry: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


def grade(score: float) -> str:
    return "STRONG" if score >= 75 else ("MODERATE" if score >= 55 else "WEAK")


def session_average(cs: list[Candle]) -> float:
    """Equal-weight average price (index 'volume' is not real traded volume)."""
    return sum((c.high + c.low + c.close) / 3.0 for c in cs) / len(cs)


def oi_flow(chain: dict, spot: float, band_pct: float = 1.0) -> float:
    """+1 = only put OI added near the money (bullish), -1 = only call OI added."""
    put_add = call_add = 0.0
    for s in chain.get("strikes", []):
        try:
            if abs(float(s["strike"]) - spot) / spot * 100.0 > band_pct:
                continue
            put_add += (s["pe"].get("oi") or 0) - (s["pe"].get("previous_oi") or 0)
            call_add += (s["ce"].get("oi") or 0) - (s["ce"].get("previous_oi") or 0)
        except (KeyError, TypeError, AttributeError):
            continue
    total = abs(put_add) + abs(call_add)
    return 0.0 if total == 0 else (put_add - call_add) / total


def _zone_targets(entry: float, side: int, risk: float, keys: list[Zone]) -> tuple[float, float, str]:
    if side == 1:
        ahead = sorted((z.low for z in keys if z.low > entry))
    else:
        ahead = sorted((z.high for z in keys if z.high < entry), reverse=True)
    ahead = [p for p in ahead if side * (p - entry) >= 1.0 * risk]
    if ahead:
        t1 = ahead[0]
        t2 = ahead[1] if len(ahead) > 1 else entry + side * max(2.5 * risk, abs(t1 - entry) + risk)
        return t1, t2, "KEY LEVELS"
    return entry + side * 1.5 * risk, entry + side * 2.5 * risk, "R-MULTIPLE (no key level in reach)"


def forced_trade(index: str, cs: list[Candle], keys: list[Zone], bias: int, chain: dict,
                 side: int, cfg: IndexConfig, now: datetime) -> ForcedTrade | None:
    bars = completed_five_minute(cs)
    a5 = atr5(cs, 10)
    if len(cs) < 5 or a5 <= 0:
        return None
    entry = cs[-1].close
    name = "CALL" if side == 1 else "PUT"

    # Stop: recent 5m swing, clamped so it is neither noise-tight nor huge.
    recent = bars[-3:] if bars else cs[-15:]
    swing = min(c.low for c in recent) if side == 1 else max(c.high for c in recent)
    raw_risk = side * (entry - (swing - side * cfg.stop_buffer_a5 * a5))
    risk = min(max(raw_risk, 0.6 * a5), 1.5 * a5)
    stop = entry - side * risk
    t1, t2, basis = _zone_targets(entry, side, risk, keys)

    avg = session_average(cs)
    avg_pos = side * (entry - avg) / a5
    trend = side * (entry - cs[0].open) / a5
    back = cs[-16].close if len(cs) >= 16 else cs[0].open
    momentum = side * (entry - back) / a5
    flow = side * oi_flow(chain, entry)
    room_r = side * (t1 - entry) / risk if basis == "KEY LEVELS" else 2.0   # open space: neutral
    align = 1.0 if bias == side else (0.5 if bias == 0 else 0.0)

    setups = [s for s in find_signals(index, cs, keys, 0, cfg) if s.side == name]
    bonus = 15.0 if setups else 0.0
    blocking = [z for z in keys if 0 <= side * ((z.low if side == 1 else z.high) - entry) <= 0.3 * a5]
    penalty = (15.0 if blocking else 0.0) + 15.0 * scale(avg_pos, 2.5, 4.0)

    parts = (
        (0.25, align),
        (0.15, scale(trend, -0.5, 2.0)),
        (0.15, clamp(1.0 - abs(avg_pos - 0.6) / 1.6) if avg_pos > -0.3 else 0.0),
        (0.15, scale(momentum, -0.3, 1.5)),
        (0.15, scale(flow, -0.5, 0.5)),
        (0.15, scale(room_r, 1.0, 3.0)),
    )
    score = max(0.0, min(100.0, 100.0 * sum(w * s for w, s in parts) + bonus - penalty))

    signal = IndexSignal(index, setups[0].setup if setups else "FORCED", name, "", "", entry, stop,
                         t1, t2, risk, side * (t1 - entry) / risk, ())
    option = pick_option(chain, signal, cfg)
    if option is None:                                   # relax once, still liquid
        option = pick_option(chain, signal, replace(cfg, option_delta=(0.35, 0.85), max_spread_pct=2.0))

    warnings = []
    t = now.astimezone(IST).time()
    if t < OR_END:
        warnings.append("before 09:30 -- opening range still forming")
    if t >= dtime(14, 30):
        warnings.append("after 14:30 -- option time decay is heavy")
    if chain.get("expiry") == now.date().isoformat() and t >= dtime(13, 0):
        warnings.append("expiry day after 13:00 -- premium can collapse fast")
    gaps = missing_minutes(cs)
    if gaps:
        warnings.append(f"{len(gaps)} missing 1m candles in the feed")
    if option is None:
        warnings.append("no liquid strike found -- do not trade this one")

    reasons = (
        f"breadth {'agrees' if align == 1 else ('mixed' if align == 0.5 else 'AGAINST')}",
        f"trend {trend:+.2f} ATR5",
        f"vs avg {avg_pos:+.2f} ATR5",
        f"15m momentum {momentum:+.2f} ATR5",
        f"OI flow {flow:+.2f}",
        f"room {room_r:.1f}R",
    ) + ((f"setup {setups[0].setup} at {setups[0].zone}",) if setups else ()) \
      + (("entering right at a key level",) if blocking else ())
    return ForcedTrade(index, name, score, grade(score), entry, stop, t1, t2, risk, basis,
                       setups[0].setup if setups else "FORCED", option, str(chain.get("expiry")),
                       reasons, tuple(warnings))


def scan_index(index: str, client: PsygridClient, cfg: IndexConfig, bias: int,
               now: datetime) -> tuple[list[ForcedTrade], str]:
    spec = INDEXES[index]
    try:
        payload = client._get(spec.candles_path)
        chain = client._get(spec.options_path)
    except Exception as exc:
        return [], f"{index}: feed error ({exc})"
    cs = completed_candles(payload, now)
    if len(cs) < 5:
        return [], f"{index}: not enough candles yet"
    day = now.date().isoformat()
    store = load_store()
    update_store(store, index, day, cs)
    prev = previous_day(store, index, day)
    opening_complete = not [m for m in missing_minutes(cs) if m < "09:30"] and cs[-1].ts.astimezone(IST).time() >= OR_END
    spot = cs[-1].close
    zones = build_zones(price_levels(cs, prev, spec, cfg, opening_complete) + oi_levels(chain, spot, cfg), spot, cfg)
    keys = key_zones(zones, cfg)
    trades = [t for side in (1, -1) if (t := forced_trade(index, cs, keys, bias, chain, side, cfg, now))]
    return trades, f"{index}: spot {spot:,.2f} | {len(keys)} key zones"


def rank(trades: list[ForcedTrade]) -> list[ForcedTrade]:
    return sorted(trades, key=lambda t: (t.option is None, -t.score, t.index, t.side))


def print_trade(t: ForcedTrade) -> None:
    d = 1 if t.side == "CALL" else -1
    o = t.option
    print("\n" + "-" * 96)
    print("#1 OPTION TRADE RIGHT NOW")
    print("-" * 96)
    if o:
        print(f"BUY          : {t.index} {t.expiry} {o.strike:,.0f} {o.kind}")
        print(f"ENTRY (ask)  : Rs {o.ask:.2f}   (bid {o.bid:.2f} | delta {o.delta:.2f} | OI {o.oi / 1e6:.1f}M)")
        print(f"STOP LOSS    : Rs {o.premium_stop:.2f}   (or exit if {t.index} hits {t.stop:,.2f})")
        print(f"TP1          : Rs ~{o.premium_t1:.2f}   ({t.index} {t.target1:,.2f}) -> book half, SL to entry")
        if o.premium_t2:
            print(f"TP2          : Rs ~{o.premium_t2:.2f}   ({t.index} {t.target2:,.2f})")
        print(f"RISK / UNIT  : Rs {o.ask - o.premium_stop:.2f}  -> x your lot size = risk per lot")
    else:
        print(f"DIRECTION    : {t.index} {t.side} -- no liquid strike found")
    print(f"SCORE        : {t.score:.1f}/100  ({t.grade})")
    print(f"SETUP        : {t.setup}")
    print(f"INDEX        : entry {t.entry:,.2f} | SL {t.stop:,.2f} ({t.risk:,.1f} pts) | "
          f"T1 {t.target1:,.2f} ({d * (t.target1 - t.entry) / t.risk:.1f}R) | T2 {t.target2:,.2f}")
    print(f"TARGET BASIS : {t.target_basis}")
    print(f"WHY          : {' | '.join(t.reasons)}")
    print("RULES        : 15-min time stop | book half at TP1, SL to entry | exit by 15:15")
    for w in t.warnings:
        print(f"WARNING      : {w}")
    if t.grade == "WEAK":
        print("NOTE         : WEAK forced pick -- smallest size or skip.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="forced #1 index option trade")
    parser.add_argument("index", nargs="?", choices=sorted(INDEXES))
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_opt")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 10

    now = now_ist()
    print("=" * 96)
    print("OPT.PY // FORCED #1 INDEX OPTION TRADE | NIFTY + BANKNIFTY + SENSEX | CALL + PUT")
    print("=" * 96)
    if not is_trading_day(now.date()):
        print(f"NOT A NORMAL NSE TRADING DAY: {now.date().isoformat()}")
        return 20
    if not SESSION_START <= now.time() < MARKET_CLOSE:
        print("Market is closed.")
        return 22

    cfg = IndexConfig()
    client = PsygridClient(args.base_url, timeout=10.0)
    bias, bias_text = market_bias(client, now)
    print(f"MARKET       : {bias_text}")
    trades: list[ForcedTrade] = []
    for index in ([args.index] if args.index else list(INDEXES)):
        found, status = scan_index(index, client, cfg, bias, now)
        print(f"SCANNED      : {status}")
        trades.extend(found)
    ranked = rank(trades)
    if not ranked:
        print("FATAL: no index data available right now.")
        return 30
    best = ranked[0]
    print_trade(best)
    print("\nALL CANDIDATES:")
    for t in ranked:
        strike = f"{t.option.strike:,.0f} {t.option.kind}" if t.option else "no strike"
        print(f"  {t.index:<10} {t.side:<4} score {t.score:5.1f} {t.grade:<8} | {strike} | {t.setup}")
    print("\nNOTE: deterministic research signal; not a guarantee of profit.")
    Audit().event("OPT_SIGNAL", scan_time=now.isoformat(),
                  selected={k: v for k, v in asdict(best).items()})
    beep()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
