from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from config import StrategyConfig
from psygrid_client import PsygridClient, StockData
from strategy_930 import Candidate, evaluate_tiers, rank

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = "http://140.245.226.102:10000"
SESSION_START = dtime(9, 15)
SIGNAL_TIME = dtime(9, 30)
ENTRY_TIME = dtime(9, 31)
ENTRY_GRACE_SECONDS = 5.0

# NSE 2026 full-day equity holidays relevant to the engine. The exchange
# calendar is also checked for weekends. Keep this list editable if NSE issues
# a special-session notice.
NSE_HOLIDAYS_2026 = {
    "2026-01-26", "2026-02-19", "2026-03-03", "2026-03-19",
    "2026-03-26", "2026-03-31", "2026-04-01", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-08-26", "2026-09-14", "2026-10-02", "2026-10-20",
    "2026-11-08", "2026-11-10", "2026-11-24", "2026-12-25",
}


class Audit:
    def __init__(self, path: str = "engine_audit.jsonl"):
        self.path = Path(path)

    def event(self, event: str, **fields) -> None:
        record = {
            "ts": datetime.now(IST).isoformat(),
            "event": event,
            **fields,
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        except Exception as exc:
            print(f"[AUDIT-WARN] {exc}", flush=True)


def now_ist() -> datetime:
    return datetime.now(IST)


def sleep_until(target: datetime) -> None:
    while True:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(0.50, max(0.02, remaining / 4.0)))


def beep(pattern: str = "signal") -> None:
    """Best-effort physical beep. Audio failure never kills the engine."""
    print("\a", end="", flush=True)
    try:
        import winsound
        if pattern == "freeze":
            winsound.Beep(1200, 350)
            winsound.Beep(1700, 350)
        else:
            winsound.Beep(1600, 400)
            winsound.Beep(2100, 500)
    except Exception as exc:
        print(f"[BEEP-WARN] Windows speaker beep unavailable: {exc}", flush=True)


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
    print("=" * 88)
    print("PSYGRID // 09:31 DETERMINISTIC OPENING-MOMENTUM ENGINE")
    print("=" * 88)
    print("450 stocks | 10 shards x 45 | completed 09:15-09:29 candles | freeze 09:30 | entry 09:31")
    print("NORMAL -> FALLBACK TIER 2 -> FALLBACK TIER 3")
    print("Hard exclusions always active: extreme gap / extreme extension / exhausted impulse")
    print("=" * 88)


def print_candidate(title: str, c: Candidate | None) -> None:
    print("\n" + "-" * 88)
    print(title)
    print("-" * 88)
    if c is None:
        print("NONE")
        return
    print(f"SYMBOL       : {c.symbol}")
    print(f"DIRECTION    : {c.side}")
    print(f"TIER         : {c.tier} {'(STRICT)' if c.tier == 1 else '(FALLBACK)'}")
    print(f"SCORE        : {c.score:.2f}/100")
    print(f"ENTRY        : {c.entry:.4f}")
    print(f"STOP         : {c.stop:.4f}")
    print(f"TARGET       : {c.target:.4f}")
    print(f"GAP          : {c.gap_pct:+.3f}%")
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
        if d.health.healthy and len(d.candles) == 15
    ]
    return median(values) if values else 0.0


def build_candidates(
    data: dict[str, StockData],
    sectors: dict[str, str],
    cfg: StrategyConfig,
) -> list[Candidate]:
    healthy = {
        symbol: d for symbol, d in data.items()
        if d.health.healthy and len(d.candles) == 15 and d.previous_close and d.ltp > 0
    }
    mkt = market_return(healthy)
    gap_history = [
        100.0 * (d.candles[0].open / d.previous_close - 1.0)
        for d in healthy.values()
        if d.previous_close
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
        stock_return = 100.0 * (d.candles[-1].close / d.candles[0].open - 1.0)
        sector = sectors.get(symbol)
        sector_return = median(peer_returns[sector]) if sector and peer_returns.get(sector) else mkt
        try:
            candidates.extend(
                evaluate_tiers(
                    symbol=symbol,
                    candles=d.candles,
                    entry=d.ltp,
                    previous_close=d.previous_close,
                    market_return=mkt,
                    sector_return=sector_return,
                    gap_history=gap_history,
                    cfg=cfg,
                )
            )
        except Exception as exc:
            print(f"[EVAL-WARN] {symbol}: {exc}", flush=True)
    return candidates


def fetch_selected_ltp(client: PsygridClient, symbol: str) -> tuple[float, str]:
    """Try the single-stock endpoint repeatedly for a fresh executable price."""
    deadline = time.monotonic() + ENTRY_GRACE_SECONDS
    last_error = "unknown"
    while time.monotonic() <= deadline:
        try:
            payload = client._get(f"public/stock/{symbol}.json")
            ltp = float(payload.get("ltp") or 0.0)
            stamp = payload.get("ltp_timestamp")
            if ltp > 0 and stamp:
                age = (now_ist() - client._ts(stamp)).total_seconds()
                if -5 <= age <= 10:
                    return ltp, "LIVE_09_31_LTP"
                last_error = f"stale_ltp_age={age:.2f}s"
            else:
                last_error = "missing_ltp_or_timestamp"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.15)
    return 0.0, f"FAILED:{last_error}"


def finalize_order(c: Candidate, entry: float, cfg: StrategyConfig) -> Candidate:
    """Re-engineer the order around the actual 09:31 price without changing direction."""
    stop = c.stop
    reason = list(c.reasons)
    valid = entry > stop if c.side == "LONG" else entry < stop
    if not valid:
        # Price crossed the frozen structural stop between 09:30 and 09:31.
        # User requested a signal rather than a dead engine. Rebase the stop to
        # a deterministic 0.35 ATR emergency distance and label it explicitly.
        emergency_distance = max(0.35 * c.atr_value, entry * cfg.minimum_risk_pct / 100.0)
        stop = entry - emergency_distance if c.side == "LONG" else entry + emergency_distance
        reason.append("EMERGENCY_STOP_REBASED_AT_09_31")
    risk = abs(entry - stop)
    target_distance = max(cfg.minimum_rr * risk, cfg.minimum_target_atr * c.atr_value)
    target = entry + target_distance if c.side == "LONG" else entry - target_distance
    return Candidate(
        symbol=c.symbol, side=c.side, score=c.score,
        entry=entry, stop=stop, target=target,
        gap_pct=c.gap_pct, impulse_pct=c.impulse_pct,
        impulse_atr=c.impulse_atr, retracement_depth=c.retracement_depth,
        retracement_volume_ratio=c.retracement_volume_ratio,
        rs_market=c.rs_market, rs_sector=c.rs_sector,
        vwap_distance_atr=c.vwap_distance_atr, structure=c.structure,
        atr_value=c.atr_value, retracement_level=c.retracement_level,
        tier=c.tier, reasons=tuple(reason),
    )


def preflight(client: PsygridClient, cfg: StrategyConfig) -> tuple[bool, str]:
    checks: list[str] = []
    try:
        shard = client.ping()
        checks.append(f"A-shard OK ({shard.get('stock_count')}/45)")
    except Exception as exc:
        return False, f"A-shard FAILED: {exc}"
    try:
        market = client.market()
        checks.append(f"FULL UNIVERSE OK ({len(market)}/450)")
        if len(market) != cfg.universe_size:
            return False, "; ".join(checks + ["universe size mismatch"])
    except Exception as exc:
        return False, "; ".join(checks + [f"FULL UNIVERSE FAILED: {exc}"])
    return True, " | ".join(checks)


def run_self_test() -> int:
    """Fast offline integrity test. No network and no market data required."""
    import unittest

    loader = unittest.TestLoader()
    suite = loader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PSYGRID 09:31 engine")
    parser.add_argument("--self-test", action="store_true", help="run offline tests and exit")
    parser.add_argument("--preflight-only", action="store_true", help="test all 450 endpoints and exit")
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
    audit.event("ENGINE_START", base_url=args.base_url)

    today = now_ist().date()
    if not is_trading_day(today):
        print(f"NOT A NORMAL NSE EQUITY TRADING DAY: {today.isoformat()}")
        audit.event("NON_TRADING_DAY", date=today.isoformat())
        return 20

    ok, message = preflight(client, cfg)
    print(f"PREFLIGHT: {message}")
    audit.event("PREFLIGHT", ok=ok, message=message)
    if args.preflight_only:
        return 0 if ok else 21
    if not ok:
        print("PREFLIGHT FAILED. The engine will retry before the live session starts.")

    now = now_ist()
    start = datetime.combine(today, SESSION_START, IST)
    freeze = datetime.combine(today, SIGNAL_TIME, IST)
    entry_time = datetime.combine(today, ENTRY_TIME, IST)

    if now >= entry_time:
        print("09:31 has already passed. Start this program before the opening session.")
        return 22

    if now < start:
        print(f"Waiting for {start:%H:%M:%S} IST ...")
        sleep_until(start)

    last_full_snapshot: dict[str, StockData] | None = None
    last_snapshot_at: datetime | None = None
    last_status = 0.0
    cycle = 0

    print("LIVE ACQUISITION STARTED — scanning all 450 symbols continuously")
    audit.event("ACQUISITION_START")

    while now_ist() < freeze:
        cycle += 1
        try:
            raw = client.market()
            parsed = {
                symbol: client.stock(symbol, payload, now_ist())
                for symbol, payload in raw.items()
            }
            # Keep a complete universe snapshot even when individual stocks are
            # unhealthy. The engine must see all 450 and exclude bad records.
            if len(parsed) == cfg.universe_size:
                last_full_snapshot = parsed
                last_snapshot_at = now_ist()
            healthy = sum(1 for d in parsed.values() if d.health.healthy)
            if time.monotonic() - last_status > 5:
                print(f"[{now_ist():%H:%M:%S}] cycle={cycle:03d} universe=450 healthy={healthy}", flush=True)
                last_status = time.monotonic()
            audit.event("ACQUISITION_CYCLE", cycle=cycle, healthy=healthy, total=len(parsed))
        except Exception as exc:
            if time.monotonic() - last_status > 5:
                print(f"[{now_ist():%H:%M:%S}] FEED RETRY: {exc}", flush=True)
                last_status = time.monotonic()
            audit.event("ACQUISITION_ERROR", error=str(exc))
        time.sleep(cfg.poll_seconds)

    # 09:30 is an unconditional event.
    beep("freeze")
    print(f"\n🔔 09:30 FREEZE BELL — {now_ist():%H:%M:%S.%f} IST")
    audit.event("FREEZE_BELL")

    # One immediate final full-universe fetch gives us the freshest completed
    # 09:15-09:29 dataset. If it fails, use the most recent verified full snapshot.
    try:
        raw = client.market()
        parsed = {s: client.stock(s, p, now_ist()) for s, p in raw.items()}
        if len(parsed) == cfg.universe_size:
            last_full_snapshot = parsed
            last_snapshot_at = now_ist()
            print("FINAL FREEZE FETCH: 450/450 shards assembled")
    except Exception as exc:
        print(f"FINAL FREEZE FETCH FAILED — using last verified snapshot: {exc}")
        audit.event("FINAL_FREEZE_FETCH_ERROR", error=str(exc))

    if not last_full_snapshot:
        print("FATAL: no complete 450-stock snapshot exists. No fake signal will be invented from missing data.")
        audit.event("FATAL_NO_SNAPSHOT")
        return 30

    frozen = {
        s: d for s, d in last_full_snapshot.items()
        if d.health.healthy and len(d.candles) == 15
    }
    print(f"FROZEN: {len(frozen)}/450 stocks passed feed integrity")
    print(f"SNAPSHOT: {last_snapshot_at.isoformat() if last_snapshot_at else 'unknown'}")
    audit.event("SNAPSHOT_FROZEN", healthy=len(frozen), total=450)

    candidates = build_candidates(frozen, sectors, cfg)
    strict_count = sum(c.tier == 1 for c in candidates)
    fallback_count = sum(c.tier > 1 for c in candidates)
    print(f"CANDIDATES: total={len(candidates)} strict={strict_count} fallback={fallback_count}")
    if not sectors:
        print("SECTOR MAP: absent -> sector RS benchmark uses healthy-universe median")

    best_long = rank(candidates, "LONG")
    best_short = rank(candidates, "SHORT")
    print_candidate("BEST LONG", best_long)
    print_candidate("BEST SHORT", best_short)

    options = [c for c in (best_long, best_short) if c is not None]
    if not options:
        print("FATAL: even the emergency tiers found no mathematically valid candidate while hard exclusions were respected.")
        audit.event("FATAL_NO_CANDIDATE")
        return 31

    selected = sorted(
        options,
        key=lambda c: (-c.score, c.tier, -c.rs_market, -c.rs_sector, c.symbol),
    )[0]
    print_candidate("🔒 LOCKED SINGLE 09:31 CANDIDATE", selected)
    audit.event("CANDIDATE_LOCKED", candidate=asdict(selected))

    print(f"Waiting for exact 09:31:00 IST ...")
    sleep_until(entry_time)

    live_ltp, source = fetch_selected_ltp(client, selected.symbol)
    if live_ltp <= 0:
        # Availability of the stock endpoint is a data problem, not a reason to
        # kill the decision. Use the verified 09:30 LTP as the final deterministic
        # fallback and mark the source explicitly.
        frozen_ltp = frozen[selected.symbol].ltp
        if frozen_ltp <= 0:
            print("FATAL: selected symbol has no valid 09:30 fallback LTP.")
            audit.event("FATAL_NO_ENTRY_PRICE", symbol=selected.symbol, source=source)
            return 32
        live_ltp = frozen_ltp
        source = "FROZEN_09_30_LTP_FALLBACK"
        print(f"[ENTRY-WARN] live 09:31 LTP unavailable: {source}")

    final = finalize_order(selected, live_ltp, cfg)
    beep("signal")

    print("\n" + "#" * 88)
    print("🚨 09:31 EXECUTION SIGNAL — SIGNAL READY")
    print("#" * 88)
    print(f"TIME         : {now_ist():%Y-%m-%d %H:%M:%S.%f} IST")
    print(f"SYMBOL       : {final.symbol}")
    print(f"DIRECTION    : {final.side}")
    print(f"ENTRY        : ₹{final.entry:.4f}")
    print(f"STOP LOSS    : ₹{final.stop:.4f}")
    print(f"TAKE PROFIT  : ₹{final.target:.4f}")
    print(f"RISK / SHARE : ₹{abs(final.entry - final.stop):.4f}")
    print(f"RR FLOOR     : {abs(final.target-final.entry)/max(abs(final.entry-final.stop),1e-12):.2f}R")
    print(f"TIER         : {final.tier}")
    print(f"PRICE SOURCE : {source}")
    print(f"SCORE        : {final.score:.2f}/100")
    print(f"WHY          : {' | '.join(final.reasons)}")
    print("#" * 88)
    print("STATUS: SIGNAL_READY")
    print("NOTE: this is a deterministic research signal; it is not a guarantee of profit.")
    audit.event("SIGNAL_READY", candidate=asdict(final), price_source=source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
