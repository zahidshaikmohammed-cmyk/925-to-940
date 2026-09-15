from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from config import StrategyConfig
from psygrid_client import PsygridClient
from strategy_930 import Candidate, evaluate, rank

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = "http://140.245.226.102:10000"
SESSION_START = dtime(9, 15)
SIGNAL_TIME = dtime(9, 30)
ENTRY_TIME = dtime(9, 31)


def now_ist() -> datetime:
    return datetime.now(IST)


def sleep_until(target: datetime) -> None:
    while True:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(0.50, max(0.02, remaining / 5.0)))


def beep() -> None:
    # Console bell first; Windows speaker beep second. A sound failure never
    # stops the strategy engine.
    print("\a", end="", flush=True)
    try:
        import winsound
        winsound.Beep(1600, 500)
        winsound.Beep(2000, 500)
    except Exception:
        pass


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


def print_candidate(title: str, candidate: Candidate | None) -> None:
    if candidate is None:
        print(f"\n{title}: NO QUALIFIED CANDIDATE")
        return
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    print(f"SYMBOL             : {candidate.symbol}")
    print(f"DIRECTION          : {candidate.side}")
    print(f"SCORE              : {candidate.score:.2f}/100")
    print(f"ENTRY               : ₹{candidate.entry:.4f}")
    print(f"STOP LOSS           : ₹{candidate.stop:.4f}")
    print(f"TARGET              : ₹{candidate.target:.4f}")
    print(f"GAP                 : {candidate.gap_pct:+.3f}%")
    print(f"IMPULSE             : {candidate.impulse_pct:+.3f}%")
    print(f"IMPULSE / ATR       : {candidate.impulse_atr:.2f}")
    print(f"RETRACEMENT         : {candidate.retracement_depth * 100:.1f}%")
    print(f"RETRACE VOL RATIO   : {candidate.retracement_volume_ratio:.2f}x")
    print(f"RS VS MARKET        : {candidate.rs_market:+.3f}%")
    print(f"RS VS SECTOR        : {candidate.rs_sector:+.3f}%")
    print(f"VWAP EXTENSION      : {candidate.vwap_distance_atr:.2f} ATR")
    print(f"STRUCTURE SCORE     : {candidate.structure:.3f}")
    print("=" * 72)


def market_return(data) -> float:
    values = [
        100.0 * (d.candles[-1].close / d.candles[0].open - 1.0)
        for d in data.values()
        if d.health.healthy and len(d.candles) == 15
    ]
    return median(values) if values else 0.0


def build_candidates(data, sectors, cfg: StrategyConfig) -> list[Candidate]:
    healthy = {s: d for s, d in data.items() if d.health.healthy}
    mkt = market_return(healthy)
    gaps = [
        100.0 * (d.candles[0].open / d.previous_close - 1.0)
        for d in healthy.values()
        if d.previous_close and d.candles
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
        peers = peer_returns.get(sector, []) if sector else []
        sector_return = median(peers) if peers else mkt
        try:
            candidate = evaluate(
                symbol,
                list(d.candles),
                d.ltp,
                d.previous_close,
                mkt,
                sector_return,
                gaps,
                cfg,
            )
        except Exception as exc:
            print(f"[WARN] {symbol} evaluation failed: {exc}")
            continue
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def refresh_selected(client: PsygridClient, selected: list[Candidate], data) -> list[Candidate]:
    if not selected:
        return []

    def fetch(c: Candidate):
        try:
            payload = client._get(f"public/stock/{c.symbol}.json")
            ltp = float(payload.get("ltp") or 0.0)
            return c, ltp, None
        except Exception as exc:
            fallback = data.get(c.symbol)
            return c, (fallback.ltp if fallback else 0.0), exc

    refreshed: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = [pool.submit(fetch, c) for c in selected]
        for future in as_completed(futures):
            c, entry, error = future.result()
            if error:
                print(f"[WARN] {c.symbol} 09:31 refresh failed: {error}; using last verified LTP")
            if entry <= 0:
                print(f"[ERROR] {c.symbol}: no valid entry price")
                continue
            # Preserve the frozen structural stop. Recalculate target from the
            # actual 09:31 entry so the printed order levels are internally
            # consistent with the executable entry price.
            risk = abs(entry - c.stop)
            if risk <= 0:
                print(f"[ERROR] {c.symbol}: entry is on/through structural stop")
                continue
            target_distance = max(2.0 * risk, 1.50 * (risk / max(2.0, 2.0)))
            target = entry + (target_distance if c.side == "LONG" else -target_distance)
            refreshed.append(
                Candidate(
                    symbol=c.symbol,
                    side=c.side,
                    score=c.score,
                    entry=entry,
                    stop=c.stop,
                    target=target,
                    gap_pct=c.gap_pct,
                    impulse_pct=c.impulse_pct,
                    impulse_atr=c.impulse_atr,
                    retracement_depth=c.retracement_depth,
                    retracement_volume_ratio=c.retracement_volume_ratio,
                    rs_market=c.rs_market,
                    rs_sector=c.rs_sector,
                    vwap_distance_atr=c.vwap_distance_atr,
                    structure=c.structure,
                    reasons=c.reasons,
                )
            )
    return refreshed


def main() -> int:
    cfg = StrategyConfig()
    cfg.validate()
    client = PsygridClient(BASE_URL)
    sectors = load_sector_map()

    today = now_ist().date()
    start = datetime.combine(today, SESSION_START, IST)
    signal = datetime.combine(today, SIGNAL_TIME, IST)
    entry_time = datetime.combine(today, ENTRY_TIME, IST)

    if now_ist() >= signal:
        print("09:30 has already passed. Start run_engine.py before 09:30 IST.")
        return 2

    print("=" * 72)
    print("PSYGRID 09:31 FORCED-ENTRY ENGINE")
    print("=" * 72)
    print("Source : PSYGRID A-J / 450 NSE stocks")
    print("Freeze : 09:30:00 IST using completed 09:15-09:29 candles")
    print("Entry  : 09:31:00 IST selected-stock live LTP")
    print("" )

    if now_ist() < start:
        print(f"Waiting for {start:%H:%M:%S} IST ...")
        sleep_until(start)

    # Keep the most recent verified full snapshot. This prevents a transient
    # endpoint failure exactly at 09:30 from destroying the decision.
    last_good_data = None
    last_good_at = None
    last_report = 0.0

    while now_ist() < signal:
        try:
            raw = client.market()
            parsed = {symbol: client.stock(symbol, payload, now_ist()) for symbol, payload in raw.items()}
            healthy_count = sum(d.health.healthy for d in parsed.values())
            if healthy_count >= 400:
                last_good_data = parsed
                last_good_at = now_ist()
            elif time.time() - last_report > 10:
                print(f"[{now_ist():%H:%M:%S}] feed received; only {healthy_count}/450 healthy")
                last_report = time.time()
        except Exception as exc:
            if time.time() - last_report > 10:
                print(f"[{now_ist():%H:%M:%S}] feed retry: {exc}")
                last_report = time.time()
        time.sleep(1.0)

    # The 09:30 event is unconditional. On Windows winsound.Beep is the
    # primary mechanism and the console bell is a fallback.
    beep()
    print(f"\n🔔 SIGNAL BELL — {now_ist():%H:%M:%S} IST")
    print("FREEZING THE LATEST VERIFIED 09:15-09:29 DATASET ...")

    # Give the endpoint a few rapid attempts if the last snapshot is missing.
    if last_good_data is None:
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            try:
                raw = client.market()
                parsed = {symbol: client.stock(symbol, payload, now_ist()) for symbol, payload in raw.items()}
                if sum(d.health.healthy for d in parsed.values()) >= 400:
                    last_good_data = parsed
                    last_good_at = now_ist()
                    break
            except Exception:
                pass

    if last_good_data is None:
        print("FATAL: no verified 450-stock snapshot was acquired before 09:30.")
        print("No fabricated signal will be produced from missing data.")
        return 3

    frozen = {s: d for s, d in last_good_data.items() if d.health.healthy and len(d.candles) == 15}
    print(f"Frozen snapshot: {len(frozen)}/450 healthy stocks")
    if last_good_at:
        print(f"Snapshot captured: {last_good_at:%H:%M:%S.%f} IST")

    candidates = build_candidates(frozen, sectors, cfg)
    long_candidate = rank(candidates, "LONG")
    short_candidate = rank(candidates, "SHORT")

    print(f"Strategy-qualified directional candidates: {len(candidates)}")
    if not sectors:
        print("[INFO] sector_map.json absent: sector-relative strength uses the healthy-universe median proxy.")

    print_candidate("🥇 BEST LONG — FROZEN", long_candidate)
    print_candidate("🥇 BEST SHORT — FROZEN", short_candidate)

    selected = [c for c in (long_candidate, short_candidate) if c is not None]
    print("\n🔒 SELECTION LOCKED. No re-ranking after 09:30:00.")
    print(f"Waiting for exact 09:31:00 IST ...")
    sleep_until(entry_time)

    final = refresh_selected(client, selected, frozen)
    if not final:
        print("FATAL: selected-stock LTP could not be obtained at 09:31.")
        return 4

    beep()
    print("\n" + "#" * 72)
    print("09:31 EXECUTION SIGNAL")
    print("#" * 72)
    for candidate in sorted(final, key=lambda x: (x.side != "LONG", -x.score)):
        print_candidate("🚨 EXECUTE " + candidate.side, candidate)
    print("IMPORTANT: these are strategy-generated research levels, not a guarantee of profit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
