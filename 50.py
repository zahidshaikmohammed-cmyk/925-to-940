"""PSYGRID NIFTY 50 ENGINE — isolated A/B universe variant of the 990-stock engine.

This file adds a NEW, standalone entry point. It does not modify, refactor,
or reimplement anything in strategy_930.py, config.py, psygrid_client.py, or
run_engine.py. Every strategy computation is reused by direct import from
run_engine (parse_universe, build_candidates, select_global_best,
health_failure_report, load_sector_map) and, transitively, strategy_930
(Tier 1 -> Tier 2 -> forced Tier 3, LONG/SHORT, scoring, ranking, entry/SL/TP
math). The only thing this file adds is: fetch the same atomic 990-stock
snapshot, then restrict it to the NIFTY 50 constituent symbols before handing
it to the existing pipeline.

Usage:
    python 50.py --self-test
    python 50.py --preflight-only
    python 50.py
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from config import StrategyConfig
from psygrid_client import ENDPOINT_PATH, PsygridClient
from run_engine import (
    Audit,
    BASE_URL,
    MARKET_CLOSE,
    SESSION_START,
    build_candidates,
    health_failure_report,
    is_trading_day,
    load_sector_map,
    now_ist,
    parse_universe,
    preflight,
    print_candidate,
    print_preflight,
    select_global_best,
)

NIFTY50_TARGET_UNIVERSE = 50
NIFTY50_CONSTITUENTS_PATH = Path(__file__).with_name("nifty50_constituents.json")
NIFTY50_AUDIT_PATH = "engine_audit_nifty50.jsonl"


def load_nifty50_constituents(path: Path = NIFTY50_CONSTITUENTS_PATH) -> dict:
    """Load the NIFTY 50 constituent list from its own JSON data file.

    The list is data, not code, precisely so it can be replaced/updated
    without touching this engine's logic. Never fabricates or pads the
    list -- whatever is in the file is what gets used, deduplicated only.
    """
    if not path.exists():
        raise FileNotFoundError(f"NIFTY 50 constituent file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    symbols = data.get("symbols")
    if not isinstance(symbols, list) or not all(isinstance(s, str) and s.strip() for s in symbols):
        raise ValueError(f"{path}: 'symbols' must be a non-empty list of strings")

    seen: set[str] = set()
    deduped: list[str] = []
    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            deduped.append(symbol)
    data["symbols"] = deduped
    return data


def restrict_to_nifty50(raw: dict[str, dict], constituents: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Filter an already-fetched 990-stock snapshot down to NIFTY 50 symbols.

    Returns (filtered_raw, unavailable). `unavailable` lists constituents
    that are not present as keys in the live feed at all -- these are
    reported, never fabricated, and the scan proceeds with the rest.
    """
    wanted = set(constituents)
    filtered = {symbol: payload for symbol, payload in raw.items() if symbol in wanted}
    unavailable = sorted(wanted - set(raw.keys()))
    return filtered, unavailable


def print_banner(constituents_meta: dict) -> None:
    print("=" * 96)
    print("PSYGRID NIFTY 50 ENGINE")
    print("=" * 96)
    print("SAME strategy as the 990-stock engine (Tier 1 -> Tier 2 -> forced Tier 3), NIFTY 50 universe only")
    print("Evaluates every available NIFTY 50 constituent in both LONG and SHORT | returns one #1")
    print(f"Target universe: {NIFTY50_TARGET_UNIVERSE} | unavailable/malformed constituents are skipped, never fabricated")
    print("This is an isolated, experimental A/B universe variant -- it does not alter the 990-stock engine.")
    status = constituents_meta.get("status", "UNKNOWN")
    valid_through = constituents_meta.get("valid_through")
    print(
        f"Constituent list status: {status} | as_of={constituents_meta.get('as_of', 'unknown')} "
        f"| valid_through={valid_through or 'unknown'}"
    )
    if status != "USER_VERIFIED":
        print("-" * 96)
        print(f"WARNING: constituent list status = {status}")
        if constituents_meta.get("action_required"):
            print(f"         {constituents_meta['action_required']}")
    if valid_through and now_ist().date().isoformat() > valid_through:
        print("-" * 96)
        print(f"WARNING: constituent list valid_through={valid_through} has PASSED. Re-verify before trusting this list.")
    for change in constituents_meta.get("pending_index_changes", []):
        print(f"PENDING CHANGE (effective {change.get('effective')}): {change.get('change')} -- {change.get('status')}")
    print("=" * 96)


def run_self_test() -> int:
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_nifty50_engine")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PSYGRID NIFTY 50 scanner (same strategy as the 990-stock engine, restricted universe)")
    parser.add_argument("--self-test", action="store_true", help="run the NIFTY 50 engine's own offline tests and exit")
    parser.add_argument("--preflight-only", action="store_true", help="inspect the atomic 990-stock public endpoint and exit")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--constituents-file", default=str(NIFTY50_CONSTITUENTS_PATH))
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    cfg = StrategyConfig()
    cfg.validate()
    audit = Audit(path=NIFTY50_AUDIT_PATH)
    client = PsygridClient(args.base_url, timeout=cfg.http_timeout_seconds)
    sectors = load_sector_map()

    try:
        constituents_meta = load_nifty50_constituents(Path(args.constituents_file))
    except Exception as exc:
        print(f"FATAL: could not load NIFTY 50 constituent list: {exc}")
        audit.event("FATAL_NO_CONSTITUENT_LIST", error=str(exc))
        return 40
    constituents = constituents_meta["symbols"]

    print_banner(constituents_meta)
    print(f"CONSTITUENT LIST : {len(constituents)} symbols | source={constituents_meta.get('source', 'unknown')}")

    now = now_ist()
    today = now.date()
    audit.event("NIFTY50_ENGINE_START", base_url=args.base_url, constituent_count=len(constituents))

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

    # Same atomic 990-stock snapshot the 990 engine uses -- one fetch, then
    # restricted locally. This is not a separate/different feed endpoint.
    try:
        raw = client.market()
    except Exception as exc:
        print(f"FATAL: no usable stock feed returned: {exc}")
        audit.event("FATAL_NO_USABLE_FEED", error=str(exc))
        return 30

    filtered_raw, unavailable = restrict_to_nifty50(raw, constituents)
    print(f"NIFTY 50 TARGET UNIVERSE  : {NIFTY50_TARGET_UNIVERSE}")
    print(f"AVAILABLE IN LIVE FEED    : {len(filtered_raw)}")
    print(f"UNAVAILABLE (not in feed) : {len(unavailable)}")
    if unavailable:
        print(f"  missing: {', '.join(unavailable)}")
    audit.event("NIFTY50_UNIVERSE_RESTRICTION", target=NIFTY50_TARGET_UNIVERSE, available=len(filtered_raw), unavailable=unavailable)

    if not filtered_raw:
        print("FATAL: none of the NIFTY 50 constituents were present in the live feed.")
        print("This is a genuine feed/coverage failure -- no signal will be invented.")
        audit.event("FATAL_NO_NIFTY50_COVERAGE", unavailable=unavailable)
        return 41

    scan_time = now_ist()
    parsed = parse_universe(client, filtered_raw, scan_time)
    healthy = {s: d for s, d in parsed.items() if d.health.healthy}
    unhealthy = {s: d for s, d in parsed.items() if not d.health.healthy}

    print(f"SCAN TIME        : {scan_time:%Y-%m-%d %H:%M:%S.%f} IST")
    print(f"NIFTY 50 RECEIVED: {len(parsed)}")
    print(f"HEALTHY          : {len(healthy)}")
    print(f"UNHEALTHY        : {len(unhealthy)} stock(s) failed per-stock feed checks")
    if not sectors:
        print("SECTOR MAP       : absent -> sector RS benchmark uses healthy-universe median")

    failure_counts, failure_examples = health_failure_report(parsed)
    if failure_counts:
        print("HEALTH FAILURES:")
        for reason, count in failure_counts.most_common():
            print(f"  {reason:<35}: {count} | examples={', '.join(failure_examples[reason])}")

    if not healthy:
        print("FATAL: zero healthy NIFTY 50 stocks. No fake signal will be invented.")
        audit.event("FATAL_NO_HEALTHY_STOCKS", failure_reasons=dict(failure_counts))
        return 31

    candidates = build_candidates(parsed, sectors, cfg)
    longs = [c for c in candidates if c.side == "LONG"]
    shorts = [c for c in candidates if c.side == "SHORT"]
    tier1 = [c for c in candidates if c.tier == 1]
    tier2 = [c for c in candidates if c.tier == 2]
    tier3 = [c for c in candidates if c.tier == 3]
    print(
        f"HYPOTHESES : {len(candidates)} total | LONG={len(longs)} | SHORT={len(shorts)} "
        f"| T1={len(tier1)} | T2={len(tier2)} | T3={len(tier3)}"
    )

    selected = select_global_best(candidates)
    if selected is None:
        print("FATAL: no directional hypothesis could be calculated from the available NIFTY 50 data.")
        audit.event("FATAL_NO_CANDIDATE", healthy=len(healthy), received=len(parsed))
        return 32

    print_candidate("🏆 NIFTY 50 #1 BEST SIGNAL", selected)
    selected_data = parsed.get(selected.symbol)
    if selected_data and selected_data.candles:
        latest_bar = selected_data.candles[-1]
        print(f"LATEST CANDLE: {latest_bar.ts:%Y-%m-%d %H:%M:%S %Z}")
        print(f"CANDLE COUNT : {len(selected_data.candles)}")
        print(f"LATEST CLOSE : ₹{latest_bar.close:.4f}")
    print("\nSTATUS: SIGNAL_READY (NIFTY 50 ENGINE)")
    print("MODE: ISOLATED A/B EXPERIMENT — same strategy as the 990-stock engine, NIFTY 50 universe only")
    print("NOTE: deterministic research signal; not a guarantee of profit. Not production-ready.")
    audit.event(
        "SIGNAL_READY",
        scan_time=scan_time.isoformat(),
        target_universe=NIFTY50_TARGET_UNIVERSE,
        available=len(filtered_raw),
        unavailable=unavailable,
        healthy=len(healthy),
        unhealthy=len(unhealthy),
        candidate_count=len(candidates),
        long_count=len(longs),
        short_count=len(shorts),
        selected=asdict(selected),
        selected_latest_candle=selected_data.candles[-1].ts.isoformat() if selected_data and selected_data.candles else None,
        selected_candle_count=len(selected_data.candles) if selected_data else 0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
