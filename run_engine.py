from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, time as dtime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from config import StrategyConfig
from psygrid_client import PsygridClient, StockData
from strategy_930 import Candidate, evaluate_tiers

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = "http://140.245.226.102:10000"
SESSION_START = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-02-19", "2026-03-03",
    "2026-03-19", "2026-03-26", "2026-03-31", "2026-04-01",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
    "2026-06-26", "2026-08-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-08", "2026-11-10", "2026-11-24",
    "2026-12-25",
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
    print("=" * 92)
    print("PSYGRID // 450-STOCK INTRADAY #1 DETERMINISTIC SCANNER")
    print("=" * 92)
    print("Run at ANY market time | scans all 450 | LONG + SHORT hypotheses | returns one #1")
    print("Completed 1m candles available at runtime | live LTP | Tier 1 -> Tier 2 -> Tier 3")
    print("Tier 3 prevents strategic NO SIGNAL; feed integrity still cannot be bypassed")
    print("=" * 92)


def print_candidate(title: str, c: Candidate | None) -> None:
    print("\n" + "-" * 92)
    print(title)
    print("-" * 92)
    if c is None:
        print("NONE")
        return
    print(f"SYMBOL       : {c.symbol}")
    print(f"DIRECTION    : {c.side}")
    print(f"TIER         : {c.tier} {'(STRICT)' if c.tier == 1 else '(FALLBACK)'}")
    print(f"SCORE        : {c.score:.2f}/100")
    print(f"ENTRY/LTP    : ₹{c.entry:.4f}")
    print(f"STOP LOSS    : ₹{c.stop:.4f}")
    print(f"TAKE PROFIT  : ₹{c.target:.4f}")
    print(f"RISK/SHARE   : ₹{abs(c.entry - c.stop):.4f}")
    print(f"RR           : {abs(c.target-c.entry)/max(abs(c.entry-c.stop),1e-12):.2f}R")
    print(f"IMPULSE      : {c.impulse_pct:+.3f}% / {c.impulse_atr:.2f} ATR")
    print(f"RETRACEMENT  : {c.retracement_depth * 100:.1f}%")
    print(f"RETRACE VOL  : {c.retracement_volume_ratio:.2f}x")
    print(f"RS MARKET    : {c.rs_market:+.3f}%")
    print(f"RS SECTOR    : {c.rs_sector:+.3f}%")
    print(f"VWAP DIST    : {c.vwap_distance_atr:+.3f} ATR")
    print(f"ATR          : {c.atr_value:.4f}")
    print(f"STRUCTURE    : {c.structure:.3f}")
    print(f"REASONS      : {' | '.join(c.reasons)}")


def market_return(data: dict[str, StockData]) -> float:
    values = [
        100.0 * (d.candles[-1].close / d.candles[0].open - 1.0)
        for d in data.values()
        if d.health.healthy and len(d.candles) >= 5
    ]
    return median(values) if values else 0.0


def build_candidates(
    data: dict[str, StockData],
    sectors: dict[str, str],
    cfg: StrategyConfig,
) -> list[Candidate]:
    healthy = {
        symbol: d for symbol, d in data.items()
        if d.health.healthy
        and len(d.candles) >= cfg.min_completed_1m
        and d.previous_close
        and d.ltp > 0
    }
    mkt = market_return(healthy)
    gap_history = [
        100.0 * (d.candles[0].open / d.previous_close - 1.0)
        for d in healthy.values() if d.previous_close
    ]

    peer_returns: dict[str, list[float]] = {}
    for symbol, d in healthy.items():
        sector = sectors.get(symbol)
        if sector:
            peer_returns.setdefault(sector, []).append(
                100.0 * (d.candles[-1].close / d.candles[0].open - 1.0)
            )

    candidates: list[Candidate] = []
    for symbol, d in healthy.items():
        sector = sectors.get(symbol)
        sector_return = median(peer_returns[sector]) if sector and peer_returns.get(sector) else mkt
        try:
            candidates.extend(evaluate_tiers(
                symbol, d.candles, d.ltp, d.previous_close,
                mkt, sector_return, gap_history, cfg,
            ))
        except Exception as exc:
            print(f"[EVAL-WARN] {symbol}: {exc}", flush=True)
    return candidates


def select_global_best(candidates: list[Candidate]) -> Candidate | None:
    """Select #1 with tier quality first, then deterministic score ordering.

    A Tier 1 pattern is preferred over Tier 2, and Tier 2 over Tier 3. Within
    the best available tier, score is the primary ranking criterion. Therefore
    Tier 3 only becomes the global #1 when no Tier 1/2 candidate exists.
    """
    if not candidates:
        return None
    best_tier = min(c.tier for c in candidates)
    pool = [c for c in candidates if c.tier == best_tier]
    return sorted(
        pool,
        key=lambda c: (-c.score, -c.rs_market, -c.rs_sector, -c.vwap_distance_atr, c.symbol, c.side),
    )[0]


def preflight(client: PsygridClient, cfg: StrategyConfig) -> tuple[bool, str]:
    try:
        shard = client.ping()
        return True, f"A-shard OK ({shard.get('stock_count')}/45)"
    except Exception as exc:
        return False, f"A-shard FAILED: {exc}"


def run_self_test() -> int:
    import unittest
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PSYGRID any-time 450-stock scanner")
    parser.add_argument("--self-test", action="store_true", help="run offline tests and exit")
    parser.add_argument("--preflight-only", action="store_true", help="validate the live 450-stock feed and exit")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    cfg = StrategyConfig()
    cfg.validate()
    audit = Audit()
    client = PsygridClient(args.base_url, timeout=cfg.http_timeout_seconds)
    sectors = load_sector_map()

    print_banner()
    now = now_ist()
    today = now.date()
    audit.event("ENGINE_START", base_url=args.base_url, mode="ANY_TIME_SCAN")

    if not is_trading_day(today):
        print(f"NOT A NORMAL NSE EQUITY TRADING DAY: {today.isoformat()}")
        return 20
    if now.time() < SESSION_START:
        print(f"Market has not opened. Run again at/after {SESSION_START.strftime('%H:%M')} IST.")
        return 22
    if now.time() >= MARKET_CLOSE:
        print(f"Market session is closed. Last live scan window ended at {MARKET_CLOSE.strftime('%H:%M')} IST.")
        return 23

    ok, message = preflight(client, cfg)
    print(f"PREFLIGHT: {message}")
    audit.event("PREFLIGHT", ok=ok, message=message)
    if args.preflight_only:
        if not ok:
            return 21
        try:
            raw = client.market()
            print(f"FULL UNIVERSE OK ({len(raw)}/450)")
            return 0 if len(raw) == cfg.universe_size else 21
        except Exception as exc:
            print(f"FULL UNIVERSE FAILED: {exc}")
            return 21
    if not ok:
        print("FATAL: live feed preflight failed. No fabricated 450-stock scan will be produced.")
        audit.event("FATAL_PREFLIGHT", message=message)
        return 21

    scan_time = now_ist()
    try:
        raw = client.market()
    except Exception as exc:
        print(f"FATAL: 450-stock universe assembly failed: {exc}")
        print("The scanner will not silently score a partial or duplicated universe.")
        audit.event("FATAL_UNIVERSE", error=str(exc))
        return 30

    if len(raw) != cfg.universe_size:
        print(f"FATAL: expected 450 unique stocks, received {len(raw)}")
        audit.event("FATAL_UNIVERSE_SIZE", received=len(raw))
        return 30

    parsed = {symbol: client.stock(symbol, payload, scan_time) for symbol, payload in raw.items()}
    healthy = {s: d for s, d in parsed.items() if d.health.healthy}
    insufficient = cfg.min_completed_1m
    print(f"SCAN TIME    : {scan_time:%Y-%m-%d %H:%M:%S.%f} IST")
    print(f"UNIVERSE     : {len(parsed)}/450 unique stocks")
    print(f"HEALTHY      : {len(healthy)}/450")
    print(f"MIN CANDLES  : {insufficient} completed 1m candles")
    if not sectors:
        print("SECTOR MAP   : absent -> sector RS benchmark uses healthy-universe median")

    if not healthy:
        print("FATAL: zero healthy stocks. No fake signal will be invented.")
        audit.event("FATAL_NO_HEALTHY_STOCKS")
        return 31

    candidates = build_candidates(parsed, sectors, cfg)
    strict = [c for c in candidates if c.tier == 1]
    tier2 = [c for c in candidates if c.tier == 2]
    tier3 = [c for c in candidates if c.tier == 3]
    print(f"HYPOTHESES   : {len(candidates)} total | T1={len(strict)} | T2={len(tier2)} | T3={len(tier3)}")

    selected = select_global_best(candidates)
    if selected is None:
        print("FATAL: healthy feed produced no directional hypothesis. This should only occur if the feed is incomplete or invalid.")
        audit.event("FATAL_NO_CANDIDATE", healthy=len(healthy))
        return 32

    print_candidate("🏆 #1 BEST SIGNAL ACROSS ALL 450", selected)
    print("\nSTATUS: SIGNAL_READY")
    print("MODE: ONE-SHOT INTRADAY SCAN — rerun bbbbb.py whenever you want a fresh #1")
    print("NOTE: deterministic research signal; not a guarantee of profit.")
    audit.event(
        "SIGNAL_READY",
        scan_time=scan_time.isoformat(),
        universe=len(parsed),
        healthy=len(healthy),
        candidate_count=len(candidates),
        selected=asdict(selected),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
