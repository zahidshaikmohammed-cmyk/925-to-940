"""FLAT: stocks that stayed flat all day, alerted only when one actually breaks out.

A stock is flat only if all of these hold against its previous close:
  - it OPENED within ±1% (no gap),
  - its day HIGH and day LOW so far are both within ±1% (it never moved 1% either way),
  - it is still within ±1% now.

Flat stocks sitting in a tight 15-minute box form a silent WATCHLIST. Nothing is traded
while they stay inside: most sideways stocks simply stay sideways. The alert fires only
when the newest completed candle CLOSES outside the box with at least 3x normal volume
and a real body. Entry: stop order just beyond that candle; stop at the box middle
(back there = failed breakout); 2R target.

    python flat.py                    # rescan every minute, beep on a breakout (Ctrl+C stops)
    python flat.py --once             # one scan, then exit
    python flat.py --min-volume 4     # fewer, stronger breakouts
    python flat.py --risk-rupees 500  # also print share quantity
    python flat.py --self-test
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from statistics import median

from bbbbb import live_core_argv
from config import StrategyConfig
from psygrid_client import ENDPOINT_PATH, EXPECTED_UNIVERSE, PsygridClient, StockData
from run_engine import (
    BASE_URL, IST, MARKET_CLOSE, SESSION_START, Audit, feed_is_live, health_failure_report, is_trading_day,
    now_ist, order_minutes_of_turnover, parse_universe, precision_screen, print_preflight,
)
from strategy_930 import Candle, clamp, vwap


@dataclass(frozen=True)
class FlatConfig:
    max_move_pct: float = 1.0      # gap, day high, day low and LTP must all stay within this of previous close
    box_bars: int = 15             # the coil: last N completed 1-minute candles
    min_box_pct: float = 0.15      # a tighter box cannot pay costs on a 2R trade
    max_box_pct: float = 0.80      # a wider box is not a coil
    recent_bars: int = 5           # "now" inside the box (contraction and volume pickup)
    volume_baseline_bars: int = 30
    reward_r: float = 2.0          # target = trigger ± reward_r × box width
    # Alerts fire on a confirmed BREAKOUT, never on the coil alone: predicting which sideways
    # stock will move picked stocks that simply stayed sideways (2026-10-13). The newest
    # completed candle must close outside the box of the 15 candles before it, with:
    breakout_volume_ratio: float = 3.0   # its volume >= this x the median of the 30 candles before it
    breakout_body_ratio: float = 0.6     # a real body, not a wick poking out: |close-open| / range
    breakout_clear_pct: float = 0.05     # close at least this far (%) beyond the box edge
    max_risk_pct: float = 1.0            # skip when the stop (box middle) is further than this from entry
    entry_valid_minutes: int = 3         # cancel the entry order if not filled in this many minutes
    alert_cooldown_minutes: int = 5      # one alert at a time
    symbol_realert_minutes: int = 60     # the same stock alerts again only after this long
    show: int = 10

    def validate(self) -> None:
        if self.max_move_pct <= 0 or self.box_bars < 6 or self.recent_bars < 2:
            raise ValueError("invalid FlatConfig")
        if not 0 < self.min_box_pct < self.max_box_pct:
            raise ValueError("min_box_pct must be > 0 and below max_box_pct")
        if self.recent_bars >= self.box_bars:
            raise ValueError("recent_bars must be shorter than box_bars")
        if (self.breakout_volume_ratio <= 0 or not 0 <= self.breakout_body_ratio <= 1 or self.max_risk_pct <= 0
                or self.alert_cooldown_minutes < 0 or self.symbol_realert_minutes < 0):
            raise ValueError("invalid breakout/alert settings")


@dataclass(frozen=True)
class FlatPick:
    symbol: str
    bias: str                # LONG, SHORT or EITHER
    score: float
    previous_close: float
    today_open: float
    ltp: float
    gap_pct: float
    day_high_pct: float      # day high vs previous close
    day_low_pct: float       # day low vs previous close
    box_high: float
    box_low: float
    box_pct: float
    contraction: float       # recent mean candle range / earlier mean candle range in the box (<1 = shrinking)
    volume_ratio: float      # recent mean volume / baseline median volume
    box_position: float      # 0 = at the box low, 1 = at the box high
    vwap: float


def pct(x: float, base: float) -> float:
    return (x / base - 1.0) * 100.0


def opening_price(d: StockData, raw: dict) -> float | None:
    """The feed's today_open, else the 09:15 candle's open (never a later candle's)."""
    try:
        value = float(raw.get("today_open"))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    first = d.candles[0] if d.candles else None
    if first and first.ts.astimezone(IST).time() == SESSION_START:
        return first.open
    return None


def flat_reject(d: StockData, today_open: float | None, cfg: FlatConfig) -> str | None:
    """Why this stock is not flat, or None if it opened flat and stayed flat."""
    pc = d.previous_close
    if not pc:
        return "no_previous_close"
    if today_open is None:
        return "no_today_open"
    limit = cfg.max_move_pct
    if abs(pct(today_open, pc)) >= limit:
        return "gapped"
    if pct(max(c.high for c in d.candles), pc) >= limit:
        return "moved_up"
    if pct(min(c.low for c in d.candles), pc) <= -limit:
        return "moved_down"
    if len(d.candles) < cfg.box_bars:
        return "too_few_candles"
    return None


def _mean_range(cs) -> float:
    return sum(c.high - c.low for c in cs) / len(cs)


def evaluate(d: StockData, today_open: float, cfg: FlatConfig) -> tuple[FlatPick | None, str]:
    """Score a flat stock's coil. (pick, "") or (None, reason)."""
    box = d.candles[-cfg.box_bars:]
    hi = max(c.high for c in box)
    lo = min(c.low for c in box)
    width = hi - lo
    last = box[-1].close
    box_pct = width / last * 100.0
    if box_pct < cfg.min_box_pct:
        return None, "box_too_tight"
    if box_pct > cfg.max_box_pct:
        return None, "box_too_wide"

    recent, earlier = box[-cfg.recent_bars:], box[:-cfg.recent_bars]
    early_range = _mean_range(earlier)
    contraction = _mean_range(recent) / early_range if early_range > 0 else 1.0
    baseline = median(c.volume for c in d.candles[-cfg.volume_baseline_bars:])
    volume_ratio = (sum(c.volume for c in recent) / len(recent)) / baseline if baseline > 0 else 0.0
    position = (last - lo) / width
    vw = vwap(d.candles)

    half = len(box) // 2
    higher_lows = (min(c.low for c in box[half:]) - min(c.low for c in box[:half])) / width
    lower_highs = (max(c.high for c in box[:half]) - max(c.high for c in box[half:])) / width
    long_votes = (position > 0.5) + (last > vw) + (higher_lows > 0)
    short_votes = (position < 0.5) + (last < vw) + (lower_highs > 0)
    bias = "LONG" if long_votes >= 2 and long_votes > short_votes else (
        "SHORT" if short_votes >= 2 and short_votes > long_votes else "EITHER")

    tight = clamp(1.0 - box_pct / cfg.max_box_pct)
    coil = clamp((1.2 - contraction) / 0.7)        # recent candles half the earlier size or less -> 1
    volume = clamp((volume_ratio - 0.8) / 1.2)     # 2x baseline volume or more -> 1
    edge = clamp(abs(position - 0.5) * 2.0)        # pressed against one side of the box
    lean = clamp(max(higher_lows, 0.0) if bias == "LONG" else max(lower_highs, 0.0) if bias == "SHORT" else 0.0)
    agree = 1.0 if bias != "EITHER" else 0.0
    score = 100.0 * (0.25 * tight + 0.20 * coil + 0.20 * volume + 0.15 * edge + 0.10 * lean + 0.10 * agree)

    return FlatPick(
        symbol=d.symbol, bias=bias, score=round(score, 2),
        previous_close=d.previous_close, today_open=today_open, ltp=last,
        gap_pct=pct(today_open, d.previous_close),
        day_high_pct=pct(max(c.high for c in d.candles), d.previous_close),
        day_low_pct=pct(min(c.low for c in d.candles), d.previous_close),
        box_high=hi, box_low=lo, box_pct=box_pct, contraction=contraction, volume_ratio=volume_ratio,
        box_position=position, vwap=vw,
    ), ""


def scan(parsed: dict[str, StockData], raw: dict[str, dict], cfg: FlatConfig) -> tuple[list[FlatPick], dict[str, int]]:
    """Rank every healthy flat stock by coil score; also count why the rest were dropped."""
    picks: list[FlatPick] = []
    dropped: dict[str, int] = {}
    for symbol, d in parsed.items():
        if not d.health.healthy or not d.candles:
            continue
        today_open = opening_price(d, raw.get(symbol) or {})
        reason = flat_reject(d, today_open, cfg)
        if reason is None:
            pick, reason = evaluate(d, today_open, cfg)
            if pick:
                picks.append(pick)
                continue
        dropped[reason] = dropped.get(reason, 0) + 1
    picks.sort(key=lambda p: (-p.score, p.box_pct, p.symbol))
    return picks, dropped


@dataclass(frozen=True)
class Breakout:
    symbol: str
    side: str                # LONG (up through the box high) or SHORT (down through the box low)
    candle_time: datetime    # the breakout candle's start
    previous_close: float
    today_open: float
    close: float             # the breakout candle's close
    box_high: float
    box_low: float
    box_pct: float
    volume_ratio: float      # breakout candle volume / median of the 30 candles before it
    body_ratio: float
    entry: float             # stop-entry just beyond the breakout candle's extreme
    stop: float              # the box middle: back there, the breakout has failed
    target: float
    risk_pct: float


def detect_breakout(d: StockData, today_open: float | None, cfg: FlatConfig) -> Breakout | None:
    """A flat stock whose newest completed candle broke its box on volume, else None.

    Everything before that candle must pass the flat rules; the breakout candle itself
    may take the stock past ±max_move_pct — that is the move.
    """
    if len(d.candles) < cfg.box_bars + 1:
        return None
    setup, bar = d.candles[:-1], d.candles[-1]
    if flat_reject(replace(d, candles=setup), today_open, cfg) is not None:
        return None
    box = setup[-cfg.box_bars:]
    hi, lo = max(c.high for c in box), min(c.low for c in box)
    box_pct = (hi - lo) / box[-1].close * 100.0
    if not cfg.min_box_pct <= box_pct <= cfg.max_box_pct:
        return None
    baseline = median(c.volume for c in setup[-cfg.volume_baseline_bars:])
    volume_ratio = bar.volume / baseline if baseline > 0 else 0.0
    rng = bar.high - bar.low
    body_ratio = abs(bar.close - bar.open) / rng if rng > 0 else 0.0
    if volume_ratio < cfg.breakout_volume_ratio or body_ratio < cfg.breakout_body_ratio:
        return None
    clear = cfg.breakout_clear_pct / 100.0
    if bar.close >= hi * (1 + clear) and bar.close > bar.open:
        side, entry = "LONG", bar.high + 0.05
    elif bar.close <= lo * (1 - clear) and bar.close < bar.open:
        side, entry = "SHORT", bar.low - 0.05
    else:
        return None
    stop = (hi + lo) / 2.0
    risk = abs(entry - stop)
    risk_pct = risk / entry * 100.0
    if risk_pct > cfg.max_risk_pct:
        return None
    sign = 1 if side == "LONG" else -1
    return Breakout(
        symbol=d.symbol, side=side, candle_time=bar.ts, previous_close=d.previous_close, today_open=today_open,
        close=bar.close, box_high=hi, box_low=lo, box_pct=box_pct, volume_ratio=volume_ratio, body_ratio=body_ratio,
        entry=entry, stop=stop, target=entry + sign * cfg.reward_r * risk, risk_pct=risk_pct,
    )


def find_breakouts(parsed: dict[str, StockData], raw: dict[str, dict], cfg: FlatConfig) -> list[Breakout]:
    """Every healthy flat stock breaking out on its newest candle, strongest volume first."""
    out = []
    for symbol, d in parsed.items():
        if d.health.healthy and d.candles:
            b = detect_breakout(d, opening_price(d, raw.get(symbol) or {}), cfg)
            if b:
                out.append(b)
    out.sort(key=lambda b: (-b.volume_ratio, b.symbol))
    return out


def print_banner(cfg: FlatConfig) -> None:
    print("=" * 96)
    print("PSYGRID // FLAT — FLAT ALL DAY, THEN A CONFIRMED BREAKOUT")
    print("=" * 96)
    print(f"Flat = open, day high, day low and LTP all within ±{cfg.max_move_pct:g}% of previous close")
    print(f"Watchlist = flat stocks in a {cfg.min_box_pct:g}–{cfg.max_box_pct:g}% box over the last {cfg.box_bars} min "
          f"(no trade while they stay inside)")
    print(f"Signal = a candle CLOSES outside the box with ≥ {cfg.breakout_volume_ratio:g}x volume and a "
          f"≥ {cfg.breakout_body_ratio:.0%} body; stop at the box middle, {cfg.reward_r:g}R target")
    print("=" * 96)


def print_breakout(b: Breakout, cfg: FlatConfig, scan_time: datetime, risk_rupees: float | None, candles) -> None:
    word = "ABOVE" if b.side == "LONG" else "BELOW"
    edge = b.box_high if b.side == "LONG" else b.box_low
    print("\n" + "-" * 96)
    print(f"🚀 BREAKOUT: {b.symbol} {'UP' if b.side == 'LONG' else 'DOWN'} -> {b.side}")
    print("-" * 96)
    print(f"PREV CLOSE   : ₹{b.previous_close:.2f} | OPEN ₹{b.today_open:.2f} ({pct(b.today_open, b.previous_close):+.2f}%)"
          f" | flat until the {b.candle_time.astimezone(IST):%H:%M} candle")
    print(f"BOX ({cfg.box_bars}m)    : ₹{b.box_low:.2f} – ₹{b.box_high:.2f} ({b.box_pct:.2f}% wide)")
    print(f"BREAK CANDLE : {b.candle_time.astimezone(IST):%H:%M} closed ₹{b.close:.2f} "
          f"({pct(b.close, edge):+.2f}% past the box) | volume {b.volume_ratio:.1f}x | body {b.body_ratio:.0%}")
    print(f"ENTRY        : stop-entry {word} ₹{b.entry:.2f} (only if the move continues)")
    print(f"STOP LOSS    : ₹{b.stop:.2f} (box middle: back here = failed breakout) | risk {b.risk_pct:.2f}%")
    print(f"TARGET       : ₹{b.target:.2f} ({cfg.reward_r:g}R)")
    if risk_rupees:
        per_share = abs(b.entry - b.stop)
        qty = int(risk_rupees // per_share) if per_share > 0 else 0
        minutes = order_minutes_of_turnover(qty * b.entry, candles, StrategyConfig())
        print(f"QUANTITY     : {qty} shares = ₹{risk_rupees:.0f} risk at ₹{per_share:.2f}/share "
              f"({minutes:.1f} min of median turnover)")
    expiry = (scan_time + timedelta(minutes=cfg.entry_valid_minutes)).strftime("%H:%M")
    print(f"RULES        : cancel the entry if not filled by {expiry} | place the SL with the entry, never after")


def print_table(picks: list[FlatPick], cfg: FlatConfig) -> None:
    if not picks:
        return
    print(f"\nWATCHLIST — TOP {min(cfg.show, len(picks))} FLAT BOXES (no trade until one breaks out):")
    print(f"  {'#':>2} {'SYMBOL':<14}{'BIAS':<7}{'SCORE':>6}{'GAP':>8}{'DAY LO/HI %':>16}{'BOX%':>7}"
          f"{'SQZ':>6}{'VOL':>6}{'BOX HI':>11}{'BOX LO':>11}")
    for i, p in enumerate(picks[:cfg.show], 1):
        print(f"  {i:>2} {p.symbol:<14}{p.bias:<7}{p.score:>6.1f}{p.gap_pct:>+7.2f}%"
              f"{p.day_low_pct:>+8.2f}/{p.day_high_pct:+.2f}{p.box_pct:>7.2f}{p.contraction:>6.2f}"
              f"{p.volume_ratio:>6.2f}{p.box_high:>11.2f}{p.box_low:>11.2f}")


def run_self_test() -> int:
    import unittest
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName("tests.test_flat"))
    return 0 if result.wasSuccessful() else 10


class FeedError(RuntimeError):
    """No usable live snapshot this minute; ``code`` is the one-shot exit code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ScanResult:
    scan_time: datetime
    parsed: dict[str, StockData]
    picks: list[FlatPick]
    dropped: dict[str, int]
    healthy: int
    preflight: dict[str, dict]
    breakouts: list[Breakout] = field(default_factory=list)


def fetch_scan(client: PsygridClient, engine_cfg: StrategyConfig, cfg: FlatConfig) -> ScanResult:
    """Fetch one live snapshot and rank its flat coils. Raises FeedError when the feed is unusable."""
    try:
        raw = client.market()
    except Exception as exc:
        raise FeedError(30, f"no usable stock feed returned: {exc}") from exc
    meta = client.last_market_meta
    live, why = feed_is_live(meta)
    if not live:
        raise FeedError(33, f"FEED NOT LIVE: {why}. No signal from non-live data.")
    scan_time = now_ist()
    parsed = precision_screen(parse_universe(client, raw, scan_time), engine_cfg, scan_time)
    picks, dropped = scan(parsed, raw, cfg)
    return ScanResult(
        scan_time=scan_time, parsed=parsed, picks=picks, dropped=dropped,
        breakouts=find_breakouts(parsed, raw, cfg),
        healthy=sum(1 for d in parsed.values() if d.health.healthy),
        preflight={ENDPOINT_PATH: {
            "ok": bool(raw), "count": len(raw), "declared": meta.get("declared_stock_count"),
            "universe_size": meta.get("universe_size"),
            "error": "; ".join(client.last_market_errors) or None,
        }},
    )


def status_of(r: ScanResult) -> str:
    if r.breakouts:
        return "FLAT_BREAKOUT"
    return "WATCHING" if r.picks else "NO_FLAT_STOCKS"


def print_report(r: ScanResult, cfg: FlatConfig, risk_rupees: float | None, best: Breakout | None = None) -> str:
    """The full scan report; ``best`` is the breakout to show first. Returns the status."""
    print_preflight(r.preflight)
    print(f"SCAN TIME    : {r.scan_time:%Y-%m-%d %H:%M:%S} IST")
    print(f"UNIVERSE     : {len(r.parsed)} | HEALTHY {r.healthy} | skipped {len(r.parsed) - r.healthy} (stale/thin feed)")
    print(f"FLAT BOXES   : {len(r.picks)} of {r.healthy} healthy stocks | breakouts this minute: {len(r.breakouts)}")
    if r.dropped:
        print("NOT FLAT / NO BOX:")
        for reason, count in sorted(r.dropped.items(), key=lambda kv: -kv[1]):
            print(f"  {reason:<20}: {count}")
    failure_counts, _ = health_failure_report(r.parsed)
    if failure_counts:
        top = ", ".join(f"{reason}={n}" for reason, n in failure_counts.most_common(3))
        print(f"HEALTH FAILS : {top}")
    shown = ([best] if best else []) + [b for b in r.breakouts if b is not best]
    for b in shown:
        print_breakout(b, cfg, r.scan_time, risk_rupees, r.parsed[b.symbol].candles)
    broke = {b.symbol for b in r.breakouts}
    print_table([p for p in r.picks if p.symbol not in broke], cfg)
    status = status_of(r)
    print(f"\nSTATUS: {status}" + ("" if r.breakouts else " — no flat stock has broken out on volume this minute"))
    print("NOTE: deterministic research signal; breakouts can fail. Not a guarantee of profit.")
    return status


def alert_beep() -> None:
    """A new setup: three loud beeps, so it is heard from across the room."""
    print("\a", end="", flush=True)
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1500, 350)
            time.sleep(0.12)
    except Exception:
        pass


@dataclass
class AlertState:
    """What the watch loop remembers between minutes."""
    alerted: dict[str, datetime] = field(default_factory=dict)    # symbol -> last alert time
    last_alert: datetime | None = None


def next_alert(breakouts: list[Breakout], state: AlertState, now: datetime, cfg: FlatConfig) -> Breakout | None:
    """The strongest breakout not alerted in ``symbol_realert_minutes``, outside the global cooldown."""
    if state.last_alert and now - state.last_alert < timedelta(minutes=cfg.alert_cooldown_minutes):
        return None
    realert = timedelta(minutes=cfg.symbol_realert_minutes)
    for b in breakouts:  # strongest volume first
        if b.symbol not in state.alerted or now - state.alerted[b.symbol] >= realert:
            return b
    return None


def watch_note(r: ScanResult, state: AlertState, cfg: FlatConfig) -> str:
    """One quiet line: what is being watched, and why nothing alerted."""
    if r.breakouts and state.last_alert and r.scan_time - state.last_alert < timedelta(minutes=cfg.alert_cooldown_minutes):
        quiet = state.last_alert + timedelta(minutes=cfg.alert_cooldown_minutes)
        return f"breakout {r.breakouts[0].symbol} held back: one alert at a time until {quiet:%H:%M}"
    if r.breakouts:
        return f"breakout {r.breakouts[0].symbol} already alerted"
    if not r.picks:
        return "no flat stock to watch"
    return "watching " + ", ".join(p.symbol for p in r.picks[:3])


def seconds_to_next_scan(now: datetime, offset_seconds: int) -> float:
    """Seconds until ``offset_seconds`` past the next minute, when that minute's candle has closed."""
    target = now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=offset_seconds)
    return max(1.0, (target - now).total_seconds())


def watch(client: PsygridClient, engine_cfg: StrategyConfig, cfg: FlatConfig, audit: Audit, args,
          clock=now_ist, sleep=time.sleep, beep=alert_beep, fetch=fetch_scan) -> int:
    """Rescan once a minute until ``args.until``; beep and print the card when a flat stock breaks out."""
    until = datetime.strptime(args.until, "%H:%M").time()
    earliest = (datetime.combine(clock().date(), SESSION_START, IST) + timedelta(minutes=cfg.box_bars)).time()
    state = AlertState()
    print(f"WATCH MODE   : rescanning every minute until {until:%H:%M} IST | Ctrl+C to stop")
    print(f"ALERTS       : beep only when a flat stock BREAKS its box on ≥ {cfg.breakout_volume_ratio:g}x volume | "
          f"one alert per {cfg.alert_cooldown_minutes} min | same stock again after {cfg.symbol_realert_minutes} min")
    while True:
        now = clock()
        if now.time() >= until:
            print(f"\n{now:%H:%M:%S} watch window over ({until:%H:%M} IST). {len(state.alerted)} setup(s) alerted today.")
            return 0
        if now.time() < earliest:
            wait = (datetime.combine(now.date(), earliest, IST) - now).total_seconds() + args.scan_offset
            print(f"{now:%H:%M:%S} waiting for {cfg.box_bars} candles: first scan at "
                  f"{earliest:%H:%M}:{args.scan_offset:02d} IST", flush=True)
            sleep(max(1.0, wait))
            continue
        try:
            r = fetch(client, engine_cfg, cfg)
        except FeedError as exc:
            print(f"{now:%H:%M:%S} {exc} (retrying next minute)", flush=True)
        else:
            b = next_alert(r.breakouts, state, r.scan_time, cfg)
            if b:
                beep()
                state.alerted[b.symbol] = state.last_alert = r.scan_time
                print("\n" + "=" * 96)
                print(f"🔔 FLAT BREAKOUT @ {r.scan_time:%H:%M:%S} IST: {b.symbol} {b.side}")
                print_report(r, cfg, args.risk_rupees, best=b)
                audit.event("FLAT_BREAKOUT", symbol=b.symbol, breakout=asdict(b),
                            healthy=r.healthy, flat=len(r.picks), scan_time=r.scan_time.isoformat())
            else:
                print(f"{r.scan_time:%H:%M:%S} no breakout | healthy {r.healthy} | flat {len(r.picks)} | "
                      f"{watch_note(r, state, cfg)}", flush=True)
        sleep(seconds_to_next_scan(clock(), args.scan_offset))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PSYGRID FLAT: flat all day, then a confirmed breakout on volume")
    parser.add_argument("--self-test", action="store_true", help="run offline tests and exit")
    parser.add_argument("--once", action="store_true", help="scan once and exit instead of rescanning every minute")
    parser.add_argument("--until", default="15:00", help="watch mode: stop rescanning at this IST time (default 15:00)")
    parser.add_argument("--scan-offset", type=int, default=8,
                        help="watch mode: seconds past each minute to scan, so the candle has closed (default 8)")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--expected-universe", type=int, default=EXPECTED_UNIVERSE)
    parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds")
    parser.add_argument("--max-move-pct", type=float, default=FlatConfig.max_move_pct,
                        help=f"max %% move from previous close for gap/day high/day low (default {FlatConfig.max_move_pct:g})")
    parser.add_argument("--min-turnover", type=float, default=None,
                        help=f"minimum median rupee turnover per minute (default {StrategyConfig.min_median_turnover_rupees:,.0f}; 0 = off)")
    parser.add_argument("--risk-rupees", type=float, default=None, help="rupees you accept losing at the stop; prints share quantity")
    parser.add_argument("--min-volume", type=float, default=FlatConfig.breakout_volume_ratio,
                        help=f"breakout candle volume vs normal (default {FlatConfig.breakout_volume_ratio:g}x)")
    parser.add_argument("--show", type=int, default=FlatConfig.show, help="how many flat coils to list")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    cfg = FlatConfig(max_move_pct=args.max_move_pct, show=args.show, breakout_volume_ratio=args.min_volume)
    cfg.validate()
    engine_cfg = StrategyConfig()
    if args.min_turnover is not None:
        engine_cfg = replace(engine_cfg, min_median_turnover_rupees=max(0.0, args.min_turnover))
    engine_cfg.validate()
    try:
        datetime.strptime(args.until, "%H:%M")
    except ValueError:
        parser.error("--until must be HH:MM")
    if not 0 <= args.scan_offset < 60:
        parser.error("--scan-offset must be 0-59")
    audit = Audit("flat_audit.jsonl")
    timeout = args.timeout if args.timeout is not None else engine_cfg.http_timeout_seconds
    client = PsygridClient(args.base_url, timeout=timeout, expected_universe=args.expected_universe)

    print_banner(cfg)
    now = now_ist()
    if not is_trading_day(now.date()):
        print(f"NOT A NORMAL NSE EQUITY TRADING DAY: {now.date().isoformat()}")
        return 20
    if now.time() >= MARKET_CLOSE:
        print(f"Market session is closed ({MARKET_CLOSE.strftime('%H:%M')} IST).")
        return 23

    if not args.once:
        try:
            return watch(client, engine_cfg, cfg, audit, args)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0

    earliest = (datetime.combine(now.date(), SESSION_START, IST) + timedelta(minutes=cfg.box_bars)).time()
    if now.time() < earliest:
        print(f"Need {cfg.box_bars} completed candles for the box. Run at/after {earliest.strftime('%H:%M')} IST.")
        return 22
    try:
        r = fetch_scan(client, engine_cfg, cfg)
    except FeedError as exc:
        print(f"FATAL: {exc}")
        audit.event("FEED_ERROR", code=exc.code, error=str(exc))
        return exc.code
    status = print_report(r, cfg, args.risk_rupees)
    audit.event(status, breakouts=[asdict(b) for b in r.breakouts], healthy=r.healthy, flat=len(r.picks),
                dropped=r.dropped, scan_time=r.scan_time.isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(live_core_argv(sys.argv[1:])))
