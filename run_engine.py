from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time as dtime, timedelta
from math import ceil
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from config import StrategyConfig
from psygrid_client import ENDPOINT_PATH, EXPECTED_UNIVERSE, Health, PsygridClient, StockData
from precision import Breadth, Pick, market_breadth, rank_picks
from strategy_930 import Candidate, Candle, evaluate_tiers

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = "http://140.245.226.102:10000"
# The two-node PSYGRID Live Core: same /public/live.json contract, 989-stock universe (bbbbb.py uses it).
LIVE_CORE_URL = "http://129.225.112.47:10000"
SESSION_START = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-02-19", "2026-03-03",
    "2026-03-19", "2026-03-26", "2026-03-31", "2026-04-01",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
    "2026-06-26", "2026-08-26", "2026-10-02", "2026-10-20",
    "2026-11-08", "2026-11-10", "2026-11-24", "2026-12-25",
}


class Audit:
    def __init__(self, path: str = "engine_audit.jsonl"):
        self.path = Path(path)

    def event(self, event: str, **fields) -> None:
        record = {"ts": datetime.now(IST).isoformat(), "event": event, **fields}
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        except Exception as exc:
            print(f"[AUDIT-WARN] {exc}", flush=True)


def now_ist() -> datetime:
    return datetime.now(IST)


def load_sector_map() -> dict[str, str]:
    path = Path("sector_map.json")
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        print(f"[WARN] sector_map.json could not be read: {exc}")
        return {}


def is_trading_day(day) -> bool:
    return day.weekday() < 5 and day.isoformat() not in NSE_HOLIDAYS_2026


def print_banner() -> None:
    print("=" * 96)
    print("PSYGRID // 990-STOCK INTRADAY #1 DETERMINISTIC SCANNER")
    print("=" * 96)
    print("Run at ANY market time | scans every AVAILABLE stock | LONG + SHORT | returns one #1")
    print("Target universe: 990 | failed stocks are skipped | remaining healthy stocks are scored")
    print("Completed 1m candles available at runtime | latest completed 1m close | Tier 1 -> Tier 2 -> Tier 3")
    print("Previous close is OPTIONAL metadata and never blocks signal generation")
    print("Rolling impulse/retracement geometry + rolling relative strength are used for rescans")
    print("Precision: live session only | stale/thin stocks skipped | VWAP stretch capped | break-of-candle trigger")
    print("Tier 3 is a labelled forced pick (LOW_CONFIDENCE_FORCED), never presented as a setup")
    print("=" * 96)


TIER_LABELS = {
    1: "(STRICT)",
    2: "(FALLBACK)",
    3: "(FORCED - NO VALID SETUP, LOW CONFIDENCE)",
}


def signal_status(c: Candidate, cfg: StrategyConfig) -> str:
    """SIGNAL_READY only for a Tier 1/2 setup scoring at least ``min_signal_score``.

    Tier 3 "forced" scores come from a looser formula (it credits even negative
    relative strength) and are not comparable with Tier 1/2 scores.
    """
    if c.tier >= 3:
        return "LOW_CONFIDENCE_FORCED"
    if c.score < cfg.min_signal_score:
        return "LOW_CONFIDENCE_WEAK"
    return "SIGNAL_READY"


def print_shortlist(candidates: list[Candidate], limit: int = 5) -> None:
    pool = sorted(
        candidates,
        key=lambda c: (c.tier, -c.score, -c.rs_market, -c.rs_sector, -c.vwap_distance_atr, c.symbol, c.side),
    )[:limit]
    if not pool:
        return
    print("\nSHORTLIST (best tier first):")
    for i, c in enumerate(pool, 1):
        print(
            f"  {i}. {c.symbol:<14} {c.side:<5} T{c.tier} "
            f"{'forced' if c.tier == 3 else 'score '}={c.score:6.2f} "
            f"vwap={c.vwap_distance_atr:+.2f}ATR rs={c.rs_market:+.2f}% entry=₹{c.entry:.2f}"
        )


def print_candidate(title: str, c: Candidate | None) -> None:
    print("\n" + "-" * 96)
    print(title)
    print("-" * 96)
    if c is None:
        print("NONE")
        return
    print(f"SYMBOL       : {c.symbol}")
    print(f"DIRECTION    : {c.side}")
    print(f"TIER         : {c.tier} {TIER_LABELS.get(c.tier, '(FALLBACK)')}")
    print(f"SCORE        : {c.score:.2f}/100")
    print(f"ENTRY/LTP    : ₹{c.entry:.4f}")
    print(f"STOP LOSS    : ₹{c.stop:.4f}")
    print(f"TAKE PROFIT  : ₹{c.target:.4f}")
    print(f"RISK/SHARE   : ₹{abs(c.entry - c.stop):.4f}")
    print(f"RR           : {abs(c.target-c.entry)/max(abs(c.entry-c.stop),1e-12):.2f}R")
    print(f"IMPULSE      : {c.impulse_pct:+.3f}% / {c.impulse_atr:.2f} ATR")
    print(f"RETRACEMENT  : {c.retracement_depth * 100:.1f}%")
    print(f"RETRACE VOL  : {c.retracement_volume_ratio:.2f}x")
    print(f"RS MARKET    : {c.rs_market:+.3f}% (in the trade's direction; + = trading with relative strength)")
    print(f"RS SECTOR    : {c.rs_sector:+.3f}%")
    print(f"VWAP DIST    : {c.vwap_distance_atr:+.3f} ATR")
    print(f"ATR          : {c.atr_value:.4f}")
    print(f"STRUCTURE    : {c.structure:.3f}")
    print(f"REASONS      : {' | '.join(c.reasons)}")


def rolling_return(d: StockData, bars: int = 12) -> float:
    cs = d.candles[-bars:]
    if len(cs) < 2 or cs[0].open <= 0:
        return 0.0
    return 100.0 * (cs[-1].close / cs[0].open - 1.0)


def market_return(data: dict[str, StockData]) -> float:
    values = [
        rolling_return(d)
        for d in data.values()
        if d.health.healthy and len(d.candles) >= 5
    ]
    return median(values) if values else 0.0


def parse_universe(client: PsygridClient, raw: dict[str, dict], scan_time: datetime) -> dict[str, StockData]:
    """Parse every raw stock payload into a StockData, symbol by symbol.

    A single malformed payload must never abort the snapshot: any parsing
    exception is caught per-symbol, that symbol is recorded unhealthy with
    the exception as its reason, and every other symbol is still parsed.
    """
    parsed: dict[str, StockData] = {}
    for symbol, payload in raw.items():
        try:
            parsed[symbol] = client.stock(symbol, payload, scan_time)
        except Exception as exc:
            print(f"[PARSE-WARN] {symbol}: {exc}", flush=True)
            parsed[symbol] = StockData(
                symbol=symbol,
                candles=(),
                ltp=0.0,
                previous_close=None,
                health=Health(symbol, False, f"parse_exception:{exc}"),
            )
    return parsed


def precision_screen(
    parsed: dict[str, StockData], cfg: StrategyConfig, scan_time: datetime
) -> dict[str, StockData]:
    """Mark stale or thinly traded stocks unhealthy before anything is scored.

    Stale: the newest completed candle closed more than ``max_candle_age_minutes``
    ago, so the stock is not trading now (or its feed lags).
    Thin: the median rupee turnover of the last ``turnover_lookback_bars`` minutes
    is below ``min_median_turnover_rupees``, so 1-minute geometry is mostly noise
    and a fill at the printed price is unlikely.
    """
    minute = scan_time.astimezone(IST).replace(second=0, microsecond=0)
    out: dict[str, StockData] = {}
    for symbol, d in parsed.items():
        if not d.health.healthy or not d.candles:
            out[symbol] = d
            continue
        reasons: list[str] = []
        if cfg.max_candle_age_minutes:
            # Minutes since the newest candle closed: 0 when it is the minute just completed.
            age = int((minute - d.candles[-1].ts.astimezone(IST)).total_seconds() // 60) - 1
            if age > cfg.max_candle_age_minutes:
                reasons.append(f"stale_last_candle_{age}m")
        if cfg.min_median_turnover_rupees:
            recent = d.candles[-cfg.turnover_lookback_bars:]
            turnover = median(c.close * c.volume for c in recent)
            if turnover < cfg.min_median_turnover_rupees:
                reasons.append("thin_turnover")
        out[symbol] = d if not reasons else StockData(
            d.symbol, d.candles, d.ltp, d.previous_close, Health(symbol, False, ";".join(reasons))
        )
    return out


def feed_is_live(meta: dict) -> tuple[bool, str]:
    """The snapshot must come from a LIVE session; anything else is not tradable data."""
    status = meta.get("status")
    session = meta.get("session") if isinstance(meta.get("session"), dict) else {}
    session_status = session.get("status")
    if status not in (None, "OK"):
        return False, f"feed status={status}"
    if session_status not in (None, "LIVE"):
        return False, f"session status={session_status}"
    return True, ""


def entry_trigger(c: Candidate, last: Candle, cfg: StrategyConfig) -> tuple[float, float]:
    """(trigger, target from the trigger): enter only on a break of the last candle.

    SHORT arms below the last completed candle's low, LONG above its high, so
    the order fills only if price is still moving the signal's way. The stop is
    unchanged, so the target is re-measured from the trigger to keep the same
    minimum reward (``minimum_rr`` x risk, at least ``minimum_target_atr`` x ATR).
    """
    side = -1 if c.side == "SHORT" else 1
    trigger = last.low if side == -1 else last.high
    risk = abs(trigger - c.stop)
    target = trigger + side * max(cfg.minimum_rr * risk, cfg.minimum_target_atr * c.atr_value)
    return trigger, target


def directional_pace(candles, side: int, cfg: StrategyConfig) -> float:
    """Rupees per minute of the stock's fastest sustained move in the trade's direction.

    The best 5-15 bar net close-to-close move (per bar) over the last
    ``pace_lookback_bars``: the momentum the setup is betting continues.
    0.0 when the stock has no move in that direction.
    """
    closes = [c.close for c in candles[-cfg.pace_lookback_bars:]]
    best = 0.0
    for k in range(5, 16):
        for j in range(k, len(closes)):
            best = max(best, side * (closes[j] - closes[j - k]) / k)
    return best


@dataclass(frozen=True)
class ExitTiming:
    pace: float              # expected rupees/minute toward the target
    checkpoint_minutes: int  # by now price should be at +0.5R, else momentum failed
    target_minutes: int      # expected minutes to reach the target at that pace
    time_stop_minutes: int   # exit at market if neither SL nor TP has traded
    checkpoint_price: float
    capped_by_close: bool


def exit_timing(c: Candidate, candles, entry: float, target: float, start: datetime,
                cfg: StrategyConfig) -> ExitTiming | None:
    """Time the trade from the stock's own momentum, not a fixed 30/60 minutes.

    At the expected pace (``continuation_pace_ratio`` x its fastest recent run):
    the +0.5R checkpoint is due after ``checkpoint_slack`` x the minutes to cover
    0.5R, the target after distance / pace, and the time stop at
    ``time_stop_multiple`` x that. Nothing runs past ``intraday_exit_time``.
    None when the stock has no measurable move in the trade's direction.
    """
    side = -1 if c.side == "SHORT" else 1
    pace = cfg.continuation_pace_ratio * directional_pace(candles, side, cfg)
    risk = abs(entry - c.stop)
    if pace <= 0 or risk <= 0:
        return None
    half_r = 0.5 * risk
    checkpoint = max(2, ceil(cfg.checkpoint_slack * half_r / pace))
    to_target = max(3, ceil(abs(target - entry) / pace))
    time_stop = max(to_target + 1, ceil(cfg.time_stop_multiple * to_target))
    hh, mm = (int(x) for x in cfg.intraday_exit_time.split(":"))
    close = start.replace(hour=hh, minute=mm, second=0, microsecond=0)
    left = max(0, int((close - start).total_seconds() // 60))
    capped = time_stop > left
    return ExitTiming(
        pace=pace,
        checkpoint_minutes=min(checkpoint, left),
        target_minutes=min(to_target, left),
        time_stop_minutes=min(time_stop, left),
        checkpoint_price=entry + side * half_r,
        capped_by_close=capped,
    )


def print_exit_timing(timing: ExitTiming | None, start: datetime) -> None:
    if timing is None:
        print("EXIT TIMING  : no measurable momentum in the trade's direction -> skip this trade")
        return
    at = lambda m: (start + timedelta(minutes=m)).strftime("%H:%M")
    print(f"EXIT TIMING  : pace ≈ ₹{timing.pace:.2f}/min (half of this stock's fastest recent run)")
    print(f"  CHECKPOINT : by +{timing.checkpoint_minutes} min (~{at(timing.checkpoint_minutes)}) price must reach "
          f"₹{timing.checkpoint_price:.2f} (+0.5R), else exit: momentum failed")
    print(f"  TARGET ETA : ~{timing.target_minutes} min (~{at(timing.target_minutes)})")
    print(f"  TIME STOP  : exit at market after {timing.time_stop_minutes} min (~{at(timing.time_stop_minutes)}) "
          f"if neither SL nor TP traded" + (" [capped at the intraday exit time]" if timing.capped_by_close else ""))
    print("  (minutes count from the fill; clock times assume a fill now)")


def build_candidates(data: dict[str, StockData], sectors: dict[str, str], cfg: StrategyConfig) -> list[Candidate]:
    healthy = {
        symbol: d for symbol, d in data.items()
        if d.health.healthy
        and len(d.candles) >= cfg.min_completed_1m
        and d.ltp > 0
    }
    if not healthy:
        return []

    mkt = market_return(healthy)
    gap_history: list[float] = []

    peer_returns: dict[str, list[float]] = {}
    for symbol, d in healthy.items():
        sector = sectors.get(symbol)
        if sector:
            peer_returns.setdefault(sector, []).append(rolling_return(d))

    candidates: list[Candidate] = []
    for symbol, d in healthy.items():
        sector = sectors.get(symbol)
        has_sector = bool(sector and peer_returns.get(sector))
        sector_return = median(peer_returns[sector]) if has_sector else mkt
        try:
            candidates.extend(evaluate_tiers(
                symbol, d.candles, d.ltp, d.previous_close,
                mkt, sector_return, gap_history, cfg, has_sector=has_sector,
            ))
        except Exception as exc:
            print(f"[EVAL-WARN] {symbol}: {exc}", flush=True)
    return candidates


def select_global_best(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    best_tier = min(c.tier for c in candidates)
    pool = [c for c in candidates if c.tier == best_tier]
    return sorted(
        pool,
        key=lambda c: (-c.score, -c.rs_market, -c.rs_sector, -c.vwap_distance_atr, c.symbol, c.side),
    )[0]


SECTOR_ABSENT = "sector_data=ABSENT_WEIGHT_REDISTRIBUTED"


def choose(
    parsed: dict[str, StockData], candidates: list[Candidate], cfg: StrategyConfig, now: datetime
) -> tuple[Candidate | None, Pick | None, list[Pick], Breadth]:
    """The engine's #1: the highest-conviction Tier 1/2 pick that passes every precision rule.

    With no tradable pick, the highest-conviction near-miss is shown with its
    blockers; with no Tier 1/2 setup at all, the old tier/score #1 (Tier 3).
    """
    breadth = market_breadth((d.candles for d in parsed.values() if d.health.healthy), cfg)
    candles = {symbol: d.candles for symbol, d in parsed.items()}
    has_sector = {c.symbol: SECTOR_ABSENT not in c.reasons for c in candidates}
    picks = rank_picks(candidates, candles, has_sector, breadth, now, cfg)
    selected = select_global_best([picks[0].candidate] if picks else candidates)
    pick = picks[0] if picks and selected is picks[0].candidate else None
    return selected, pick, picks, breadth


def final_status(selected: Candidate, pick: Pick | None) -> str:
    if pick is None:
        return "LOW_CONFIDENCE_FORCED"
    if pick.tradable:
        return "SIGNAL_READY"
    if any(b.startswith("no ") for b in pick.blockers):
        return "NO_NEW_ENTRIES"
    return "NO_PRECISION_SETUP"


def print_breadth(b: Breadth) -> None:
    print(f"MARKET       : {b.above_vwap * 100:.0f}% of {b.counted} liquid stocks above VWAP -> {b.regime}")


def print_persistence(pick: Pick) -> None:
    p = pick.persistence
    print(f"PERSISTENCE  : {p.score:.1f}/100 | CONVICTION {pick.conviction:.1f}/100")
    print(f"  VWAP HOLD  : {p.vwap_hold * 100:.0f}% of recent candles on the trade's side | {p.vwap_crosses} crosses / 30 min")
    print(f"  STRUCTURE  : {p.structure * 100:.0f}% of 5-min bars making {'HH/HL' if pick.candidate.side == 'LONG' else 'LH/LL'}")
    print(f"  VOLUME     : {p.volume_agreement * 100:.0f}% of directional volume moving the trade's way")
    print(f"  OPEN RANGE : {p.opening_range * 100:.0f}% (100 = held beyond the 09:15-09:29 range)")
    print(f"  RS AGREE   : {p.rs_agreement * 100:.0f}%")
    if p.warnings:
        print(f"  WARNINGS   : {', '.join(p.warnings)} (-{p.penalty:.0f})")
    if pick.blockers:
        print(f"  BLOCKED BY : {' | '.join(pick.blockers)}")


def print_picks(picks: list[Pick], limit: int = 5) -> None:
    if not picks:
        return
    print("\nRANKING (tradable first, by conviction):")
    for i, k in enumerate(picks[:limit], 1):
        c = k.candidate
        tag = "TRADE" if k.tradable else "skip: " + ", ".join(k.blockers)
        print(f"  {i}. {c.symbol:<14} {c.side:<5} T{c.tier} conviction={k.conviction:5.1f} "
              f"persist={k.persistence.score:5.1f} setup={c.score:5.1f} -> {tag}")


def preflight(client: PsygridClient) -> tuple[bool, dict[str, dict]]:
    """Validate the single atomic 990-stock public endpoint."""
    results = client.preflight_all()
    return all(item.get("ok", False) for item in results.values()), results


def print_preflight(results: dict[str, dict]) -> None:
    print("PREFLIGHT — ATOMIC 990-STOCK ENDPOINT:")
    for endpoint, result in results.items():
        print(
            f"  {endpoint}: "
            f"{'OK' if result.get('ok') else 'FAILED'} | "
            f"records={result.get('count', 0)} | "
            f"declared={result.get('declared')} | "
            f"universe={result.get('universe_size', EXPECTED_UNIVERSE)}"
        )
        if result.get("error"):
            print(f"  ERROR: {result.get('error')}")


def health_failure_report(parsed: dict[str, StockData]) -> tuple[Counter, dict[str, list[str]]]:
    counts: Counter = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for symbol, d in parsed.items():
        if d.health.healthy:
            continue
        reasons = [r for r in d.health.reason.split(";") if r]
        for reason in reasons or ["unknown"]:
            counts[reason] += 1
            if len(examples[reason]) < 5:
                examples[reason].append(symbol)
    return counts, examples


def run_self_test() -> int:
    import unittest
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PSYGRID any-time 990-stock scanner")
    parser.add_argument("--self-test", action="store_true", help="run offline tests and exit")
    parser.add_argument("--preflight-only", action="store_true", help="inspect the atomic 990-stock public endpoint and exit")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--expected-universe", type=int, default=EXPECTED_UNIVERSE, help="universe_size the feed must declare")
    parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds (default: strategy config)")
    parser.add_argument("--min-turnover", type=float, default=None,
                        help="minimum median rupee turnover per minute (default 500000; 0 = off)")
    parser.add_argument("--risk-rupees", type=float, default=None, help="rupees you accept losing at the stop; prints the share quantity")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    cfg = StrategyConfig()
    if args.min_turnover is not None:
        cfg = replace(cfg, min_median_turnover_rupees=max(0.0, args.min_turnover))
    cfg.validate()
    audit = Audit()
    timeout = args.timeout if args.timeout is not None else cfg.http_timeout_seconds
    client = PsygridClient(args.base_url, timeout=timeout, expected_universe=args.expected_universe)
    sectors = load_sector_map()

    print_banner()
    now = now_ist()
    today = now.date()
    audit.event("ENGINE_START", base_url=args.base_url, mode="ANY_TIME_SCAN_INTRADAY_NO_PREVIOUS_CLOSE")

    if not is_trading_day(today):
        print(f"NOT A NORMAL NSE EQUITY TRADING DAY: {today.isoformat()}")
        return 20
    if now.time() < SESSION_START:
        print(f"Market has not opened. Run again at/after {SESSION_START.strftime('%H:%M')} IST.")
        return 22
    if now.time() >= MARKET_CLOSE:
        print(f"Market session is closed. Last live scan window ended at {MARKET_CLOSE.strftime('%H:%M')} IST.")
        return 23

    if args.preflight_only:
        preflight_ok, preflight_results = preflight(client)
        print_preflight(preflight_results)
        audit.event("PREFLIGHT", ok=preflight_ok, endpoint=ENDPOINT_PATH, results=preflight_results)
        return 0 if preflight_ok else 21

    # Normal scans fetch exactly one atomic 990-stock snapshot. This avoids
    # scoring one snapshot after preflighting a different snapshot.
    try:
        raw = client.market()
    except Exception as exc:
        print(f"FATAL: no usable stock feed returned: {exc}")
        audit.event("FATAL_NO_USABLE_FEED", error=str(exc))
        return 30

    meta = client.last_market_meta
    live, why = feed_is_live(meta)
    if not live:
        print(f"FEED NOT LIVE: {why}. No signal from non-live data.")
        audit.event("FEED_NOT_LIVE", reason=why)
        return 33

    preflight_results = {
        ENDPOINT_PATH: {
            "ok": bool(raw),
            "count": len(raw),
            "declared": meta.get("declared_stock_count"),
            "universe_size": meta.get("universe_size"),
            "error": None if not client.last_market_errors else "; ".join(client.last_market_errors),
        }
    }
    print_preflight(preflight_results)
    audit.event("PREFLIGHT", ok=bool(raw), endpoint=ENDPOINT_PATH, results=preflight_results)

    scan_time = now_ist()
    parsed = precision_screen(parse_universe(client, raw, scan_time), cfg, scan_time)
    healthy = {s: d for s, d in parsed.items() if d.health.healthy}
    unhealthy = {s: d for s, d in parsed.items() if not d.health.healthy}

    print(f"SCAN TIME    : {scan_time:%Y-%m-%d %H:%M:%S.%f} IST")
    print(f"UNIVERSE     : {len(parsed)}/{args.expected_universe} unique stocks received")
    print(f"HEALTHY      : {len(healthy)}")
    print(f"SKIPPED      : {len(unhealthy)} stock(s) failed per-stock feed checks")
    print(f"SHARD ISSUES : {len(client.last_market_errors)}")
    print(f"DUPLICATES   : {len(client.last_market_duplicates)}")
    meta = client.last_market_meta
    if meta:
        session_meta = meta.get("session", {}) if isinstance(meta.get("session"), dict) else {}
        print(f"FEED ENDPOINT: {meta.get('endpoint', ENDPOINT_PATH)}")
        print(f"FEED STATUS  : {meta.get('status', 'UNKNOWN')}")
        if session_meta.get("current_time_ist"):
            print(f"FEED CLOCK   : {session_meta.get('current_time_ist')}")
    if client.last_market_errors:
        for item in client.last_market_errors[:10]:
            print(f"  - {item}")
    if client.last_market_duplicates:
        print(f"  duplicate symbols (first 10): {', '.join(client.last_market_duplicates[:10])}")
    if not sectors:
        print("SECTOR MAP   : absent -> sector RS benchmark uses healthy-universe median")

    failure_counts, failure_examples = health_failure_report(parsed)
    if failure_counts:
        print("HEALTH FAILURES:")
        for reason, count in failure_counts.most_common():
            print(f"  {reason:<35}: {count} | examples={', '.join(failure_examples[reason])}")

    if not healthy:
        print("FATAL: zero healthy stocks. No fake signal will be invented.")
        audit.event("FATAL_NO_HEALTHY_STOCKS", failure_reasons=dict(failure_counts))
        return 31

    candidates = build_candidates(parsed, sectors, cfg)
    strict = [c for c in candidates if c.tier == 1]
    tier2 = [c for c in candidates if c.tier == 2]
    tier3 = [c for c in candidates if c.tier == 3]
    print(f"HYPOTHESES   : {len(candidates)} total | T1={len(strict)} | T2={len(tier2)} | T3={len(tier3)}")

    selected, pick, picks, breadth = choose(parsed, candidates, cfg, scan_time)
    if selected is None:
        print("FATAL: no directional hypothesis could be calculated from the available data.")
        audit.event("FATAL_NO_CANDIDATE", healthy=len(healthy), received=len(parsed))
        return 32
    status = final_status(selected, pick)

    print_breadth(breadth)
    title = {
        "SIGNAL_READY": "🏆 #1 PRECISION SIGNAL",
        "NO_PRECISION_SETUP": "⛔ NO PRECISION SETUP NOW - closest candidate (DO NOT TRADE)",
        "NO_NEW_ENTRIES": "⛔ OUTSIDE THE ENTRY WINDOW - closest candidate (DO NOT TRADE)",
        "LOW_CONFIDENCE_FORCED": "⚠ FORCED PICK - no Tier 1/2 setup anywhere (DO NOT TRADE at size)",
    }[status]
    print_candidate(title, selected)
    if pick is not None:
        print_persistence(pick)
    selected_data = parsed.get(selected.symbol)
    trigger = trigger_target = None
    timing = None
    if selected_data and selected_data.candles:
        latest_bar = selected_data.candles[-1]
        print(f"LATEST CANDLE: {latest_bar.ts:%Y-%m-%d %H:%M:%S %Z}")
        print(f"CANDLE COUNT : {len(selected_data.candles)}")
        print(f"LATEST CLOSE : ₹{latest_bar.close:.4f}")
        trigger, trigger_target = entry_trigger(selected, latest_bar, cfg)
        word = "BELOW" if selected.side == "SHORT" else "ABOVE"
        print(f"TRIGGER      : enter only if price trades {word} ₹{trigger:.2f} (stop-entry order)")
        print(f"TRIGGER PLAN : SL ₹{selected.stop:.2f} | TP ₹{trigger_target:.2f} "
              f"({abs(trigger_target - trigger) / max(abs(trigger - selected.stop), 1e-12):.2f}R from the trigger)")
        print(f"               cancel if not filled within {cfg.trigger_valid_candles} candle(s) "
              f"or if ₹{selected.stop:.2f} trades first")
        if args.risk_rupees:
            per_share = abs(trigger - selected.stop)
            qty = int(args.risk_rupees // per_share) if per_share > 0 else 0
            print(f"QUANTITY     : {qty} shares = ₹{args.risk_rupees:.0f} risk at ₹{per_share:.2f}/share")
        fill_time = max(scan_time, latest_bar.ts.astimezone(IST) + timedelta(minutes=1))
        timing = exit_timing(selected, selected_data.candles, trigger, trigger_target, fill_time, cfg)
        print_exit_timing(timing, fill_time)
    print(f"STOP ORDER   : place the SL at ₹{selected.stop:.2f} together with the entry, never after")
    print_picks(picks)
    if not picks:
        print_shortlist(candidates)
    print(f"\nSTATUS: {status}")
    if status != "SIGNAL_READY":
        print("ACTION: no trade. Rerun in a few minutes; a precision signal needs a Tier 1/2 setup,")
        print("        the market on its side, and a trend that is holding (persistence).")
    print("MODE: ONE-SHOT INTRADAY SCAN — rerun bbbbb.py whenever you want a fresh #1")
    print("NOTE: deterministic research signal; not a guarantee of profit.")
    audit.event(
        status,
        trigger=trigger,
        trigger_target=trigger_target,
        exit_timing=asdict(timing) if timing else None,
        breadth=asdict(breadth),
        persistence=asdict(pick.persistence) if pick else None,
        conviction=pick.conviction if pick else None,
        blockers=list(pick.blockers) if pick else None,
        scan_time=scan_time.isoformat(),
        universe=len(parsed),
        healthy=len(healthy),
        skipped=len(unhealthy),
        shard_issues=len(client.last_market_errors),
        duplicates=len(client.last_market_duplicates),
        candidate_count=len(candidates),
        selected=asdict(selected),
        feed_endpoint=meta.get("endpoint") if isinstance(meta, dict) else None,
        feed_status=meta.get("status") if isinstance(meta, dict) else None,
        feed_clock=(meta.get("session") or {}).get("current_time_ist") if isinstance(meta.get("session"), dict) else None,
        selected_latest_candle=selected_data.candles[-1].ts.isoformat() if selected_data and selected_data.candles else None,
        selected_candle_count=len(selected_data.candles) if selected_data else 0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
