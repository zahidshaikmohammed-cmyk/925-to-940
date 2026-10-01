"""index.py -- key-level engine for NIFTY / BANKNIFTY / SENSEX options.

It maps the levels that matter first, and only signals when price does
something meaningful AT one of them. Most of the time the right output is
"wait -- here is where to act".

Key levels (clustered into zones, each zone scored by how many independent
sources agree):
    PDH / PDL / PDC   previous day high / low / close (saved locally each day)
    ORH / ORL         09:15-09:29 opening range
    HOD / LOD         today's high / low so far
    CALL / PUT WALL   strikes with the largest call / put open interest
    ROUND             round numbers (100s on NIFTY, 500s on BANKNIFTY/SENSEX)

Setups (on completed 5-minute candles, only at KEY zones):
    BREAKOUT-RETEST   5m close through a zone, pullback holds the zone, 5m close
                      away again -> trade the breakout direction.
    REJECTION         5m candle pokes into a zone and closes back out with a long
                      wick -> trade away from the zone.

Every trade needs: target = next key zone, at least 1.5R away; direction not
against the 400-stock market breadth; clean data.

Usage:
    python index.py NIFTY            one look: level map + signal or plan
    python index.py NIFTY --watch    re-checks every 30 s, beeps on a signal
    python index.py --self-test
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import time as systime
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path

from old import Context, OldConfig, atr5, beep, five_minute_bars, liquid_universe, since_open_return, vwap
from psygrid_client import PsygridClient
from run_engine import BASE_URL, Audit, is_trading_day, now_ist, parse_universe
from strategy_930 import IST, Candle

SESSION_START = dtime(9, 15)
OR_END = dtime(9, 30)
MARKET_CLOSE = dtime(15, 30)


@dataclass(frozen=True)
class IndexSpec:
    candles_path: str
    options_path: str
    round_step: float


INDEXES = {
    "NIFTY": IndexSpec("public/nifty.json", "public/nifty-options.json", 100.0),
    "BANKNIFTY": IndexSpec("public/banknifty.json", "public/banknifty-options.json", 500.0),
    "SENSEX": IndexSpec("public/sensex.json", "public/sensex-options.json", 500.0),
}


@dataclass(frozen=True)
class IndexConfig:
    zone_tolerance_pct: float = 0.10     # levels within 0.10% merge into one zone
    key_zone_strength: float = 4.0       # needs at least two independent sources
    oi_max_distance_pct: float = 3.0
    oi_walls_per_side: int = 3
    breakout_lookback_bars: int = 8      # 5m bars
    breakout_clearance_a5: float = 0.10
    retest_touch_a5: float = 0.15
    stop_buffer_a5: float = 0.25
    min_rejection_wick: float = 0.50     # wick share of the 5m candle range
    min_target_r: float = 1.5
    max_signals_per_day: int = 2
    no_signals_after: str = "14:30"
    max_missing_recent: int = 3          # missing 1m candles in the last 30 min
    option_delta: tuple[float, float] = (0.45, 0.75)
    option_target_delta: float = 0.60
    max_spread_pct: float = 1.0
    max_premium_loss_pct: float = 30.0
    weights: dict = field(default_factory=lambda: {
        "PDH": 3.0, "PDL": 3.0, "PDC": 1.5, "ORH": 2.0, "ORL": 2.0,
        "HOD": 2.0, "LOD": 2.0, "ROUND": 1.0,
    })


@dataclass(frozen=True)
class Level:
    price: float
    label: str
    weight: float


@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    strength: float
    labels: tuple[str, ...]

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def describe(self) -> str:
        span = f"{self.low:,.2f}" if self.high - self.low < 0.01 else f"{self.low:,.2f}-{self.high:,.2f}"
        return f"{span}  [{' + '.join(self.labels)}]  strength {self.strength:.1f}"


@dataclass(frozen=True)
class IndexSignal:
    index: str
    setup: str
    side: str                 # CALL (long index) or PUT (short index)
    zone: str
    bar_time: str
    entry: float
    stop: float
    target1: float
    target2: float | None
    risk: float
    t1_r: float
    reasons: tuple[str, ...]


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def completed_candles(payload: dict, now: datetime) -> list[Candle]:
    """Today's completed 1m candles (the still-forming minute is dropped)."""
    rows = payload.get("1m") or payload.get("candles_1m") or []
    cutoff = now.astimezone(IST).replace(second=0, microsecond=0)
    return [
        c for c in PsygridClient.candles(rows)
        if c.ts.astimezone(IST).date() == cutoff.date()
        and SESSION_START <= c.ts.astimezone(IST).time() < MARKET_CLOSE
        and c.ts < cutoff
    ]


def missing_minutes(cs: list[Candle]) -> list[str]:
    """Minutes absent between 09:15 and the last candle."""
    if not cs:
        return []
    have = {c.ts.astimezone(IST).strftime("%H:%M") for c in cs}
    last = cs[-1].ts.astimezone(IST)
    out = []
    minute = 9 * 60 + 15
    end = last.hour * 60 + last.minute
    while minute <= end:
        stamp = f"{minute // 60:02d}:{minute % 60:02d}"
        if stamp not in have:
            out.append(stamp)
        minute += 1
    return out


def completed_five_minute(cs: list[Candle]) -> list[Candle]:
    bars = five_minute_bars(cs)
    if not bars:
        return []
    last = cs[-1].ts.astimezone(IST)
    if (last.hour * 60 + last.minute - (9 * 60 + 15)) % 5 != 4:
        bars = bars[:-1]                 # last 5m bucket still forming
    return bars


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------

LEVELS_FILE = Path("index_levels.json")


def load_store(path: Path = LEVELS_FILE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def update_store(store: dict, index: str, day: str, cs: list[Candle], path: Path = LEVELS_FILE) -> None:
    if not cs:
        return
    store.setdefault(index, {})[day] = {
        "high": max(c.high for c in cs),
        "low": min(c.low for c in cs),
        "close": cs[-1].close,
        "last_candle": cs[-1].ts.astimezone(IST).strftime("%H:%M"),
    }
    try:
        path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[LEVELS-WARN] could not save {path}: {exc}")


def previous_day(store: dict, index: str, day: str) -> dict | None:
    days = sorted(d for d in store.get(index, {}) if d < day)
    return store[index][days[-1]] if days else None


def price_levels(cs: list[Candle], prev: dict | None, spec: IndexSpec, cfg: IndexConfig,
                 opening_complete: bool) -> list[Level]:
    w = cfg.weights
    out: list[Level] = []
    if prev:
        out += [Level(prev["high"], "PDH", w["PDH"]), Level(prev["low"], "PDL", w["PDL"]),
                Level(prev["close"], "PDC", w["PDC"])]
    opening = [c for c in cs if c.ts.astimezone(IST).time() < OR_END]
    if opening_complete and opening:
        out += [Level(max(c.high for c in opening), "ORH", w["ORH"]),
                Level(min(c.low for c in opening), "ORL", w["ORL"])]
    if cs:
        out += [Level(max(c.high for c in cs), "HOD", w["HOD"]),
                Level(min(c.low for c in cs), "LOD", w["LOD"])]
        spot = cs[-1].close
        base = round(spot / spec.round_step) * spec.round_step
        out += [Level(base + k * spec.round_step, "ROUND", w["ROUND"]) for k in (-2, -1, 0, 1, 2)]
    return out


def oi_levels(chain: dict, spot: float, cfg: IndexConfig) -> list[Level]:
    rows = [
        s for s in chain.get("strikes", [])
        if isinstance(s, dict) and abs(float(s["strike"]) - spot) / spot * 100.0 <= cfg.oi_max_distance_pct
    ]
    out: list[Level] = []
    for side, key, label in ((1, "ce", "CALL WALL"), (-1, "pe", "PUT WALL")):
        pool = [s for s in rows if (s["strike"] >= spot if side == 1 else s["strike"] <= spot)]
        pool = [s for s in pool if (s.get(key) or {}).get("oi", 0) > 0]
        if not pool:
            continue
        top = sorted(pool, key=lambda s: -s[key]["oi"])[:cfg.oi_walls_per_side]
        biggest = top[0][key]["oi"]
        for s in top:
            oi = s[key]["oi"]
            change = oi - (s[key].get("previous_oi") or 0)
            out.append(Level(float(s["strike"]), f"{label} {oi / 1e6:.1f}M ({change / 1e6:+.1f}M)",
                             1.0 + 2.0 * oi / biggest))
    return out


def build_zones(levels: list[Level], spot: float, cfg: IndexConfig) -> list[Zone]:
    tol = spot * cfg.zone_tolerance_pct / 100.0
    zones: list[Zone] = []
    group: list[Level] = []
    for lv in sorted(levels, key=lambda x: x.price):
        if group and lv.price - group[0].price > tol:
            zones.append(_zone(group))
            group = []
        group.append(lv)
    if group:
        zones.append(_zone(group))
    return zones


def _zone(group: list[Level]) -> Zone:
    labels = []
    for lv in group:
        if lv.label not in labels:
            labels.append(lv.label)
    return Zone(min(lv.price for lv in group), max(lv.price for lv in group),
                sum(lv.weight for lv in group), tuple(labels))


def key_zones(zones: list[Zone], cfg: IndexConfig) -> list[Zone]:
    return [z for z in zones if z.strength >= cfg.key_zone_strength]


# --------------------------------------------------------------------------
# Setups
# --------------------------------------------------------------------------

def _targets(entry: float, side: int, keys: list[Zone], exclude: Zone) -> tuple[float | None, float | None]:
    if side == 1:
        ahead = sorted((z for z in keys if z is not exclude and z.low > entry), key=lambda z: z.low)
        prices = [z.low for z in ahead]
    else:
        ahead = sorted((z for z in keys if z is not exclude and z.high < entry), key=lambda z: -z.high)
        prices = [z.high for z in ahead]
    return (prices[0] if prices else None, prices[1] if len(prices) > 1 else None)


def _finish(index: str, setup: str, side: int, zone: Zone, bar: Candle, entry: float, stop: float,
            keys: list[Zone], cfg: IndexConfig, reasons: tuple[str, ...]) -> IndexSignal | None:
    risk = side * (entry - stop)
    if risk <= 0:
        return None
    t1, t2 = _targets(entry, side, keys, zone)
    if t1 is None or side * (t1 - entry) < cfg.min_target_r * risk:
        return None
    return IndexSignal(
        index, setup, "CALL" if side == 1 else "PUT", zone.describe(),
        bar.ts.astimezone(IST).strftime("%H:%M"), entry, stop, t1, t2, risk,
        side * (t1 - entry) / risk, reasons,
    )


def breakout_retest(index: str, bars: list[Candle], keys: list[Zone], a5: float,
                    cfg: IndexConfig) -> list[IndexSignal]:
    out: list[IndexSignal] = []
    if len(bars) < 3 or a5 <= 0:
        return out
    last = bars[-1]
    window_start = max(1, len(bars) - cfg.breakout_lookback_bars)
    for z in keys:
        for side in (1, -1):
            edge, back = (z.high, z.low) if side == 1 else (z.low, z.high)
            for b in range(window_start, len(bars) - 1):
                broke = side * (bars[b].close - edge) > cfg.breakout_clearance_a5 * a5
                was_inside = side * (bars[b - 1].close - edge) <= 0
                if not (broke and was_inside):
                    continue
                after = bars[b + 1:]
                touch = min(c.low for c in after) if side == 1 else max(c.high for c in after)
                retested = side * (touch - edge) <= cfg.retest_touch_a5 * a5
                held = side * (touch - back) > -cfg.breakout_clearance_a5 * a5
                resumed = side * (last.close - edge) > 0 and side * (last.close - last.open) > 0
                if retested and held and resumed:
                    stop = back - side * cfg.stop_buffer_a5 * a5
                    sig = _finish(index, "BREAKOUT-RETEST", side, z, last, last.close, stop, keys, cfg, (
                        f"5m close through zone at {bars[b].ts.astimezone(IST):%H:%M}",
                        f"retest held at {touch:,.2f}",
                        "last 5m candle closed away from the zone",
                    ))
                    if sig:
                        out.append(sig)
                    break
    return out


def rejection(index: str, bars: list[Candle], keys: list[Zone], a5: float,
              cfg: IndexConfig) -> list[IndexSignal]:
    out: list[IndexSignal] = []
    if not bars or a5 <= 0:
        return out
    bar = bars[-1]
    rng = bar.high - bar.low
    if rng <= 0:
        return out
    upper_wick = bar.high - max(bar.open, bar.close)
    lower_wick = min(bar.open, bar.close) - bar.low
    for z in keys:
        # Resistance rejection -> PUT
        if bar.high >= z.low and bar.close < z.low and upper_wick / rng >= cfg.min_rejection_wick:
            stop = max(bar.high, z.high) + cfg.stop_buffer_a5 * a5
            sig = _finish(index, "REJECTION", -1, z, bar, bar.close, stop, keys, cfg, (
                f"5m candle poked to {bar.high:,.2f} and closed back below the zone",
                f"upper wick {upper_wick / rng:.0%} of the candle",
            ))
            if sig:
                out.append(sig)
        # Support rejection -> CALL
        if bar.low <= z.high and bar.close > z.high and lower_wick / rng >= cfg.min_rejection_wick:
            stop = min(bar.low, z.low) - cfg.stop_buffer_a5 * a5
            sig = _finish(index, "REJECTION", 1, z, bar, bar.close, stop, keys, cfg, (
                f"5m candle dipped to {bar.low:,.2f} and closed back above the zone",
                f"lower wick {lower_wick / rng:.0%} of the candle",
            ))
            if sig:
                out.append(sig)
    return out


def find_signals(index: str, cs: list[Candle], keys: list[Zone], bias: int,
                 cfg: IndexConfig) -> list[IndexSignal]:
    bars = completed_five_minute(cs)
    a5 = atr5(cs, 10)
    signals = breakout_retest(index, bars, keys, a5, cfg) + rejection(index, bars, keys, a5, cfg)
    # Never against the 400-stock market.
    allowed = [s for s in signals if not (bias == 1 and s.side == "PUT") and not (bias == -1 and s.side == "CALL")]
    return sorted(allowed, key=lambda s: -s.t1_r)


# --------------------------------------------------------------------------
# Option choice
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionPick:
    strike: float
    kind: str
    ask: float
    bid: float
    delta: float
    oi: float
    premium_stop: float
    premium_t1: float
    premium_t2: float | None


def pick_option(chain: dict, sig: IndexSignal, cfg: IndexConfig) -> OptionPick | None:
    key = "ce" if sig.side == "CALL" else "pe"
    best = None
    for s in chain.get("strikes", []):
        o = s.get(key) or {}
        delta = abs((o.get("greeks") or {}).get("delta") or 0.0)
        bid, ask = o.get("top_bid_price") or 0.0, o.get("top_ask_price") or 0.0
        if not cfg.option_delta[0] <= delta <= cfg.option_delta[1] or bid <= 0 or ask < bid:
            continue
        if (ask - bid) > max(1.0, ask * cfg.max_spread_pct / 100.0):
            continue
        rank = (abs(delta - cfg.option_target_delta), ask - bid)
        if best is None or rank < best[0]:
            best = (rank, s, o, delta, bid, ask)
    if best is None:
        return None
    _, s, o, delta, bid, ask = best
    move = lambda target: abs(target - sig.entry) * delta
    stop = max(ask - move(sig.stop), ask * (1.0 - cfg.max_premium_loss_pct / 100.0))
    return OptionPick(float(s["strike"]), key.upper(), ask, bid, delta, o.get("oi", 0.0), stop,
                      ask + move(sig.target1), ask + move(sig.target2) if sig.target2 else None)


# --------------------------------------------------------------------------
# Market breadth from the 400-stock universe
# --------------------------------------------------------------------------

def market_bias(client: PsygridClient, now: datetime) -> tuple[int, str]:
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            raw = client.market()
            parsed = parse_universe(client, raw, now)
        ocfg = OldConfig()
        liquid = liquid_universe({s: d for s, d in parsed.items()
                                  if d.health.healthy and len(d.candles) >= ocfg.min_bars}, ocfg)
        if not liquid:
            return 0, "breadth unavailable"
        breadth = sum(1 for d in liquid.values() if d.candles[-1].close > vwap(d.candles)) / len(liquid)
        returns = sorted(since_open_return(d.candles) for d in liquid.values())
        med = returns[len(returns) // 2]
        bias = Context(med, None, breadth).bias(ocfg)
        name = {1: "UP -> CALLS ONLY", -1: "DOWN -> PUTS ONLY", 0: "MIXED -> both"}[bias]
        return bias, f"{name} | {breadth:.0%} of {len(liquid)} liquid stocks above VWAP | median {med:+.2f}%"
    except Exception as exc:
        return 0, f"breadth unavailable ({exc})"


# --------------------------------------------------------------------------
# Daily signal cap
# --------------------------------------------------------------------------

STATE_FILE = Path("index_state.json")


def signals_today(day: str, index: str, path: Path = STATE_FILE) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(day, {}).get(index, [])
    except Exception:
        return []


def record_signal(day: str, sig: IndexSignal, path: Path = STATE_FILE) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data = {day: data.get(day, {})}                    # keep today only
    data[day].setdefault(sig.index, []).append(asdict(sig))
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[STATE-WARN] {exc}")


# --------------------------------------------------------------------------
# One look
# --------------------------------------------------------------------------

def analyse(index: str, client: PsygridClient, cfg: IndexConfig, now: datetime, show: bool = True):
    spec = INDEXES[index]
    day = now.date().isoformat()
    try:
        payload = client._get(spec.candles_path)
        chain = client._get(spec.options_path)
    except Exception as exc:
        print(f"FEED ERROR: {exc}")
        return None, []
    cs = completed_candles(payload, now)
    if len(cs) < 5:
        print("Not enough completed 1m candles yet. Run at/after 09:30.")
        return None, []

    store = load_store()
    update_store(store, index, day, cs)
    prev = previous_day(store, index, day)
    missing = missing_minutes(cs)
    opening_missing = [m for m in missing if m < "09:30"]
    last = cs[-1].ts.astimezone(IST)
    last_minute = last.hour * 60 + last.minute
    last_30 = {f"{(last_minute - k) // 60:02d}:{(last_minute - k) % 60:02d}" for k in range(30)}
    recent_missing = [m for m in missing if m in last_30]
    spot = cs[-1].close
    opening_complete = not opening_missing and last.time() >= OR_END
    levels = price_levels(cs, prev, spec, cfg, opening_complete)
    levels += oi_levels(chain, spot, cfg)
    zones = build_zones(levels, spot, cfg)
    keys = key_zones(zones, cfg)
    bias, bias_text = market_bias(client, now)

    if show:
        print("=" * 96)
        print(f"INDEX.PY // {index} KEY LEVELS | {now:%Y-%m-%d %H:%M:%S} IST | spot {spot:,.2f}")
        print("=" * 96)
        print(f"MARKET       : {bias_text}")
        print(f"DATA         : {len(cs)} completed 1m candles | missing {len(missing)}"
              + (f" (opening {opening_missing[0]}-{opening_missing[-1]} -> no ORH/ORL)" if opening_missing else ""))
        if not prev:
            print("PREV DAY     : not stored yet -- PDH/PDL/PDC appear from the next trading day")
        print(f"OPTION CHAIN : expiry {chain.get('expiry')} | PCR(OI) "
              f"{(chain.get('analytics') or {}).get('pcr_oi', float('nan')):.2f}")
        print("\nKEY ZONES (strength >= %.1f), top to bottom:" % cfg.key_zone_strength)
        for z in sorted(keys, key=lambda z: -z.mid):
            marker = "  <-- price" if z.low <= spot <= z.high else ""
            arrow = "above" if z.low > spot else ("below" if z.high < spot else "AT")
            print(f"  {arrow:>5}  {z.describe()}{marker}")
        if not keys:
            print("  none -- no confluence today; nothing to trade")

    blockers = []
    if recent_missing and len(recent_missing) > cfg.max_missing_recent:
        blockers.append(f"{len(recent_missing)} missing candles in the last 30 min")
    h, m = (int(x) for x in cfg.no_signals_after.split(":"))
    if now.astimezone(IST).time() >= dtime(h, m):
        blockers.append(f"no new option trades after {cfg.no_signals_after} (time decay)")
    if now.astimezone(IST).time() < OR_END:
        blockers.append("opening range not complete -- wait for 09:30")
    taken = signals_today(day, index)
    if len(taken) >= cfg.max_signals_per_day:
        blockers.append(f"daily cap reached ({cfg.max_signals_per_day} signals)")
    if chain.get("expiry") == day and now.astimezone(IST).time() >= dtime(13, 0):
        blockers.append("expiry day after 13:00 -- this chain decays too fast")

    signals = [] if blockers else find_signals(index, cs, keys, bias, cfg)
    seen = {(t["setup"], t["side"], t["bar_time"]) for t in taken}
    signals = [s for s in signals if (s.setup, s.side, s.bar_time) not in seen]

    if show:
        if blockers:
            print("\nSTATUS       : NO TRADE -- " + "; ".join(blockers))
        elif not signals:
            print_plan(spot, keys, bias)
        else:
            print_signal(signals[0], pick_option(chain, signals[0], cfg))
    return chain, signals


def print_plan(spot: float, keys: list[Zone], bias: int) -> None:
    above = sorted((z for z in keys if z.low > spot), key=lambda z: z.low)
    below = sorted((z for z in keys if z.high < spot), key=lambda z: -z.high)
    print("\nSTATUS       : WAIT -- price is between levels, not at one. Plan:")
    if above:
        z = above[0]
        if bias != -1:
            print(f"  CALL if a 5m candle closes above {z.high:,.2f} and a pullback holds it (breakout-retest)")
        if bias != 1:
            print(f"  PUT  if price pokes into {z.low:,.2f}-{z.high:,.2f} and a 5m candle closes back below with a long upper wick")
    if below:
        z = below[0]
        if bias != 1:
            print(f"  PUT  if a 5m candle closes below {z.low:,.2f} and a pullback holds it (breakout-retest)")
        if bias != -1:
            print(f"  CALL if price dips into {z.low:,.2f}-{z.high:,.2f} and a 5m candle closes back above with a long lower wick")
    if not above and not below:
        print("  no key zones on either side")


def print_signal(sig: IndexSignal, opt: OptionPick | None) -> None:
    d = 1 if sig.side == "CALL" else -1
    print("\n" + "-" * 96)
    print(f"SIGNAL       : {sig.index} {sig.side} | {sig.setup} | 5m candle {sig.bar_time}")
    print("-" * 96)
    print(f"ZONE         : {sig.zone}")
    print(f"INDEX ENTRY  : {sig.entry:,.2f}")
    print(f"INDEX STOP   : {sig.stop:,.2f}  ({sig.risk:,.2f} pts)")
    print(f"INDEX T1     : {sig.target1:,.2f}  ({sig.t1_r:.2f}R, next key zone)")
    if sig.target2:
        print(f"INDEX T2     : {sig.target2:,.2f}  ({d * (sig.target2 - sig.entry) / sig.risk:.2f}R)")
    print(f"WHY          : {' | '.join(sig.reasons)}")
    if opt:
        print(f"OPTION       : {opt.strike:,.0f} {opt.kind} | ask {opt.ask:.2f} / bid {opt.bid:.2f} | delta {opt.delta:.2f}")
        print(f"PREMIUM STOP : {opt.premium_stop:.2f}  (exit the option if the INDEX hits {sig.stop:,.2f}, whichever first)")
        print(f"PREMIUM T1   : ~{opt.premium_t1:.2f}" + (f" | T2 ~{opt.premium_t2:.2f}" if opt.premium_t2 else "")
              + "  (approx: delta only)")
        print(f"RISK PER UNIT: Rs {opt.ask - opt.premium_stop:.2f} -> per lot = this x your lot size")
    else:
        print("OPTION       : no liquid strike with delta 0.45-0.75 and a tight spread -- skip")
    print("RULES        : 15-min time stop | book half at T1, stop to entry | max 2 index trades/day")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="key-level index options engine")
    parser.add_argument("index", nargs="?", default="NIFTY", choices=sorted(INDEXES))
    parser.add_argument("--watch", action="store_true", help="re-check every 30 s and beep on a signal")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_index")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 10

    cfg = IndexConfig()
    client = PsygridClient(args.base_url, timeout=10.0)
    audit = Audit()
    now = now_ist()
    if not is_trading_day(now.date()):
        print(f"NOT A NORMAL NSE TRADING DAY: {now.date().isoformat()}")
        return 20
    if not SESSION_START <= now.time() < MARKET_CLOSE:
        print("Market is closed.")
        return 22

    _, signals = analyse(args.index, client, cfg, now)
    if signals:
        record_signal(now.date().isoformat(), signals[0])
        audit.event("INDEX_SIGNAL", **asdict(signals[0]))
        beep()
    if not args.watch:
        return 0

    print("\nWATCHING -- re-checking every 30 s. Ctrl+C to stop.", flush=True)
    last_minute = None
    try:
        while True:
            systime.sleep(30)
            now = now_ist()
            if now.time() >= MARKET_CLOSE:
                break
            with contextlib.redirect_stdout(io.StringIO()):
                _, signals = analyse(args.index, client, cfg, now, show=False)
            if signals:
                print()
                analyse(args.index, client, cfg, now)
                record_signal(now.date().isoformat(), signals[0])
                audit.event("INDEX_SIGNAL", **asdict(signals[0]))
                beep()
            elif now.minute != last_minute and now.minute % 5 == 0:
                print(f"[{now:%H:%M}] no setup at a key level -- still watching", flush=True)
            last_minute = now.minute
            if len(signals_today(now.date().isoformat(), args.index)) >= cfg.max_signals_per_day:
                print("Daily signal cap reached -- stopping.")
                break
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
