"""PSYGRID NIFTY 50 OPTIONS ENGINE — isolated options-premium overlay.

This file adds a THIRD, standalone entry point. It does not modify
strategy_930.py, config.py, psygrid_client.py, run_engine.py, bbbbb.py, or
50.py. It reuses:

  - run_engine.py's own functions (parse_universe, build_candidates,
    select_global_best, load_sector_map, is_trading_day, Audit, session
    constants) for the EXACT SAME equity Tier 1 -> Tier 2 -> forced Tier 3
    signal generation the 990/NIFTY-50 stock engines already use. Nothing
    about which stock/side/tier/score wins is computed here -- it is the
    unmodified Candidate object select_global_best already returns.

  - 50.py's own NIFTY 50 constituent loading/restriction
    (load_nifty50_constituents, restrict_to_nifty50), loaded the same way
    `python 50.py` runs (its filename is not a valid Python identifier, so
    it cannot be `import`ed with a normal statement).

The ONLY new logic here is translating that equity signal's entry/stop/
target -- which remain untouched, computed in the underlying stock's own
price -- into the corresponding OPTION CONTRACT's premium terms, using the
new /public/stock-options/{SYMBOL}.json endpoint (real Dhan option-chain
data: strikes, last_price, and per-contract Greeks including delta).

Direction mapping: a LONG equity signal is expressed by BUYING the nearest
at-the-money CALL (CE); a SHORT equity signal is expressed by BUYING the
nearest at-the-money PUT (PE). ATM is chosen because it is the standard,
most liquid, best-known convention for signal-following options overlays,
and because delta near 0.5 keeps the linear premium approximation below
reasonably well-behaved.

Premium SL/TP translation is a first-order (delta) linear approximation:

    stop_premium   = entry_premium + delta * (equity.stop   - equity.entry)
    target_premium = entry_premium + delta * (equity.target - equity.entry)

This is intentionally simple and auditable, uses the contract's own SIGNED
delta (positive for calls, negative for puts) so the same formula is
correct for both CE and PE without any manual sign-flipping, and is NOT a
full options repricing model -- it ignores gamma, theta, vega, and IV
changes over the life of the trade. This is disclosed in every signal's
reasons and printed output, not hidden.

The feed's stock-options poller is a single round-robin queue across all
50 NIFTY 50 constituents (~3.2s per symbol, ~160s per full rotation), so a
freshly-fetched chain for the selected symbol can be stale by design, not
by bug. This engine reports that staleness (chain age in seconds) rather
than silently presenting it as live, and offers an optional bounded
--max-wait-seconds to poll for a fresher snapshot before giving up.

Usage:
    python 50options.py --self-test
    python 50options.py
    python 50options.py --max-wait-seconds 170
"""

from __future__ import annotations

import argparse
import importlib.util
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path

from config import StrategyConfig
from psygrid_client import PsygridClient
from strategy_930 import Candidate
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
    print_candidate,
    select_global_best,
)

# "50.py" is not a valid Python identifier, so it cannot be `import`ed with
# a normal statement. Loaded the same way `python 50.py` would run it and
# the same way tests/test_nifty50_engine.py already loads it -- reusing its
# constituent-loading and universe-restriction logic, not duplicating it.
_FIFTY_PATH = Path(__file__).with_name("50.py")
_FIFTY_SPEC = importlib.util.spec_from_file_location("nifty50_engine_reused", _FIFTY_PATH)
nifty50_engine = importlib.util.module_from_spec(_FIFTY_SPEC)
_FIFTY_SPEC.loader.exec_module(nifty50_engine)

OPTIONS_ENDPOINT_TEMPLATE = "public/stock-options/{symbol}.json"
OPTION_CHAIN_POLL_INTERVAL_SECONDS = 3.2  # matches the feed's own per-symbol refresh cadence
STALE_OPTIONS_WARN_SECONDS = 180.0  # a bit over one full 50-symbol round-robin rotation (~160s)
OPTIONS_AUDIT_PATH = "engine_audit_nifty50_options.jsonl"


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    side: str  # "CE" or "PE"
    strike: float
    last_price: float
    delta: float
    expiry: str
    top_bid_price: float | None
    top_ask_price: float | None


@dataclass(frozen=True)
class OptionsSignal:
    equity: Candidate
    contract: OptionContract
    entry_premium: float
    stop_premium: float
    target_premium: float
    underlying_ltp: float
    updated_at: str | None
    age_seconds: float | None
    reasons: tuple[str, ...]


def fetch_stock_option_chain(client: PsygridClient, symbol: str) -> dict:
    """One stock's live option-chain snapshot from the isolated stock-options
    endpoint. Reuses PsygridClient's own HTTP fetch (gzip, cache-busting,
    duplicate-key-aware JSON parsing) rather than reimplementing it.
    """
    path = OPTIONS_ENDPOINT_TEMPLATE.format(symbol=symbol)
    payload = client._get(path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path}: non-object payload")
    return payload


def wait_for_live_option_chain(
    client: PsygridClient, symbol: str, max_wait_seconds: float = 0.0
) -> tuple[dict | None, str | None]:
    """Fetch the symbol's option chain, optionally polling until it reports
    LIVE or a bounded deadline passes. Never blocks by default
    (max_wait_seconds=0 fetches exactly once). Returns whatever the last
    attempt produced -- LIVE, some other real status, or an error -- for
    the caller to report honestly rather than silently retrying forever.
    """
    deadline = time.monotonic() + max(0.0, max_wait_seconds)
    last_chain: dict | None = None
    last_error: str | None = None
    while True:
        try:
            last_chain = fetch_stock_option_chain(client, symbol)
            last_error = None
        except Exception as exc:
            last_chain = None
            last_error = f"{type(exc).__name__}: {exc}"
        status = (last_chain or {}).get("status")
        if status == "LIVE" or time.monotonic() >= deadline:
            return last_chain, last_error
        time.sleep(min(OPTION_CHAIN_POLL_INTERVAL_SECONDS, max(0.1, deadline - time.monotonic())))


def select_atm_contract(chain: dict, option_side: str) -> OptionContract | None:
    """Nearest-to-underlying strike carrying a genuinely usable contract for
    the requested side: finite positive last_price, and a delta whose SIGN
    matches the side's convention (positive for CE, negative for PE) --
    guards against acting on a malformed/mislabeled Greeks feed, which
    would otherwise silently invert the stop/target. Never fabricates a
    contract; returns None if nothing in the chain qualifies.
    """
    underlying_ltp = chain.get("underlying_ltp")
    strikes = chain.get("strikes")
    expiry = chain.get("expiry")
    symbol = chain.get("symbol", "")
    if not isinstance(underlying_ltp, (int, float)) or not isfinite(underlying_ltp) or underlying_ltp <= 0:
        return None
    if not isinstance(strikes, list) or not strikes:
        return None

    key = "ce" if option_side == "CE" else "pe"
    ranked: list[tuple[float, OptionContract]] = []
    for row in strikes:
        if not isinstance(row, dict):
            continue
        strike = row.get("strike")
        contract = row.get(key)
        if not isinstance(strike, (int, float)) or not isfinite(strike):
            continue
        if not isinstance(contract, dict):
            continue

        last_price = contract.get("last_price")
        if not isinstance(last_price, (int, float)) or not isfinite(last_price) or last_price <= 0:
            continue

        greeks = contract.get("greeks") if isinstance(contract.get("greeks"), dict) else {}
        delta = greeks.get("delta")
        if not isinstance(delta, (int, float)) or not isfinite(delta) or delta == 0:
            continue
        if option_side == "CE" and delta <= 0:
            continue
        if option_side == "PE" and delta >= 0:
            continue

        bid = contract.get("top_bid_price")
        ask = contract.get("top_ask_price")
        ranked.append((
            abs(strike - underlying_ltp),
            OptionContract(
                symbol=symbol, side=option_side, strike=float(strike),
                last_price=float(last_price), delta=float(delta), expiry=str(expiry),
                top_bid_price=float(bid) if isinstance(bid, (int, float)) and isfinite(bid) else None,
                top_ask_price=float(ask) if isinstance(ask, (int, float)) and isfinite(ask) else None,
            ),
        ))

    if not ranked:
        return None
    ranked.sort(key=lambda pair: pair[0])
    return ranked[0][1]


def translate_to_premium(equity: Candidate, contract: OptionContract) -> tuple[float, float]:
    """First-order (delta) linear approximation of the equity engine's own,
    unmodified stop/target -- in option premium terms. See module
    docstring for the exact formula and its explicit limitations.
    """
    stop_premium = contract.last_price + contract.delta * (equity.stop - equity.entry)
    target_premium = contract.last_price + contract.delta * (equity.target - equity.entry)
    return stop_premium, target_premium


def build_options_signal(
    client: PsygridClient, equity: Candidate, max_wait_seconds: float = 0.0
) -> tuple[OptionsSignal | None, tuple[str, ...]]:
    """Fetch the selected equity signal's option chain and translate it.
    Returns (signal, ()) on success or (None, reasons) if the options
    overlay genuinely cannot be computed right now -- the equity signal
    itself remains valid regardless; this function only ever concerns the
    premium translation on top of it.
    """
    option_side = "CE" if equity.side == "LONG" else "PE"
    chain, fetch_error = wait_for_live_option_chain(client, equity.symbol, max_wait_seconds)

    if chain is None:
        return None, (f"options_endpoint_unreachable:{fetch_error}",)

    status = chain.get("status")
    if status != "LIVE":
        reasons = [f"options_status={status or 'UNKNOWN'}"]
        if chain.get("error"):
            reasons.append(f"options_error={chain['error']}")
        return None, tuple(reasons)

    contract = select_atm_contract(chain, option_side)
    if contract is None:
        return None, (f"no_valid_atm_{option_side}_contract_with_price_and_correctly_signed_delta",)

    stop_premium, target_premium = translate_to_premium(equity, contract)
    if stop_premium <= 0 or target_premium <= 0:
        return None, ("premium_translation_non_positive",)
    if not (target_premium > contract.last_price > stop_premium):
        return None, ("premium_translation_incoherent_direction",)

    updated_at = chain.get("updated_at")
    age_seconds = None
    if isinstance(updated_at, str):
        try:
            age_seconds = (now_ist() - datetime.fromisoformat(updated_at)).total_seconds()
        except Exception:
            age_seconds = None

    reasons = [
        f"underlying_signal_tier={equity.tier}",
        f"option_side={option_side}",
        f"atm_strike={contract.strike:g}",
        f"delta={contract.delta:+.4f}",
        "premium_translation=DELTA_LINEAR_APPROXIMATION",
        "NOT_A_FULL_OPTIONS_REPRICING_MODEL_IGNORES_GAMMA_THETA_VEGA",
    ]
    if age_seconds is not None and age_seconds > STALE_OPTIONS_WARN_SECONDS:
        reasons.append(f"WARNING_STALE_OPTION_CHAIN_age_seconds={age_seconds:.0f}")

    signal = OptionsSignal(
        equity=equity,
        contract=contract,
        entry_premium=contract.last_price,
        stop_premium=stop_premium,
        target_premium=target_premium,
        underlying_ltp=float(chain.get("underlying_ltp")),
        updated_at=updated_at,
        age_seconds=age_seconds,
        reasons=tuple(reasons),
    )
    return signal, ()


def print_banner(constituents_meta: dict) -> None:
    print("=" * 96)
    print("PSYGRID NIFTY 50 OPTIONS ENGINE")
    print("=" * 96)
    print("SAME equity strategy as 50.py (Tier 1 -> Tier 2 -> forced Tier 3, NIFTY 50 universe) selects the #1 stock+side.")
    print("This engine ONLY translates that unmodified entry/stop/target into the corresponding option's PREMIUM terms.")
    print("Direction: LONG equity signal -> BUY ATM CALL (CE) | SHORT equity signal -> BUY ATM PUT (PE)")
    print("Premium SL/TP = delta-linear approximation of the equity SL/TP -- NOT a full options repricing model.")
    print("This is an isolated, experimental overlay -- it does not alter the 990-stock or NIFTY-50-stock engines.")
    status = constituents_meta.get("status", "UNKNOWN")
    print(f"Constituent list status: {status} | as_of={constituents_meta.get('as_of', 'unknown')} | valid_through={constituents_meta.get('valid_through', 'unknown')}")
    print("=" * 96)


def print_options_signal(signal: OptionsSignal) -> None:
    c = signal.contract
    print("\n" + "-" * 96)
    print("🏆 NIFTY 50 OPTIONS #1 SIGNAL (PREMIUM TERMS)")
    print("-" * 96)
    print(f"UNDERLYING     : {signal.equity.symbol}")
    print(f"EQUITY SIGNAL  : {signal.equity.side} | TIER {signal.equity.tier} | SCORE {signal.equity.score:.2f}/100 (unmodified equity strategy)")
    print(f"OPTION ACTION  : BUY {c.side} {c.strike:g} exp {c.expiry}")
    print(f"UNDERLYING LTP : ₹{signal.underlying_ltp:.4f}")
    print(f"ENTRY PREMIUM  : ₹{signal.entry_premium:.4f}")
    print(f"STOP PREMIUM   : ₹{signal.stop_premium:.4f}")
    print(f"TARGET PREMIUM : ₹{signal.target_premium:.4f}")
    print(f"RISK/SHARE     : ₹{abs(signal.entry_premium - signal.stop_premium):.4f} of premium (multiply by the exchange lot size yourself -- not available from this feed)")
    print(f"RR             : {abs(signal.target_premium - signal.entry_premium) / max(abs(signal.entry_premium - signal.stop_premium), 1e-12):.2f}R")
    print(f"DELTA USED     : {c.delta:+.4f}")
    if c.top_bid_price is not None and c.top_ask_price is not None:
        print(f"BID/ASK        : ₹{c.top_bid_price:.4f} / ₹{c.top_ask_price:.4f}")
    if signal.age_seconds is not None:
        print(f"CHAIN UPDATED  : {signal.updated_at} (age {signal.age_seconds:.0f}s)")
    else:
        print(f"CHAIN UPDATED  : {signal.updated_at}")
    print(f"REASONS        : {' | '.join(signal.reasons)}")


def run_self_test() -> int:
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_nifty50_options_engine")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PSYGRID NIFTY 50 options-premium overlay (same equity strategy, premium-terms SL/TP)")
    parser.add_argument("--self-test", action="store_true", help="run this engine's own offline tests and exit")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--constituents-file", default=str(nifty50_engine.NIFTY50_CONSTITUENTS_PATH))
    parser.add_argument(
        "--max-wait-seconds", type=float, default=0.0,
        help="seconds to poll for the selected symbol's option chain to report LIVE before giving up "
             "(the feed round-robins ~3.2s/symbol, ~160s per full 50-symbol rotation); 0 = fetch once, no wait",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    cfg = StrategyConfig()
    cfg.validate()
    audit = Audit(path=OPTIONS_AUDIT_PATH)
    client = PsygridClient(args.base_url, timeout=cfg.http_timeout_seconds)
    sectors = load_sector_map()

    try:
        constituents_meta = nifty50_engine.load_nifty50_constituents(Path(args.constituents_file))
    except Exception as exc:
        print(f"FATAL: could not load NIFTY 50 constituent list: {exc}")
        audit.event("FATAL_NO_CONSTITUENT_LIST", error=str(exc))
        return 40
    constituents = constituents_meta["symbols"]

    print_banner(constituents_meta)

    now = now_ist()
    today = now.date()
    audit.event("NIFTY50_OPTIONS_ENGINE_START", base_url=args.base_url, constituent_count=len(constituents))

    if not is_trading_day(today):
        print(f"NOT A NORMAL NSE EQUITY TRADING DAY: {today.isoformat()}")
        return 20
    if now.time() < SESSION_START:
        print(f"Market has not opened. Run again at/after {SESSION_START.strftime('%H:%M')} IST.")
        return 22
    if now.time() >= MARKET_CLOSE:
        print(f"Market session is closed. Last live scan window ended at {MARKET_CLOSE.strftime('%H:%M')} IST.")
        return 23

    try:
        raw = client.market()
    except Exception as exc:
        print(f"FATAL: no usable stock feed returned: {exc}")
        audit.event("FATAL_NO_USABLE_FEED", error=str(exc))
        return 30

    filtered_raw, unavailable = nifty50_engine.restrict_to_nifty50(raw, constituents)
    print(f"NIFTY 50 TARGET UNIVERSE  : {nifty50_engine.NIFTY50_TARGET_UNIVERSE}")
    print(f"AVAILABLE IN LIVE FEED    : {len(filtered_raw)}")
    print(f"UNAVAILABLE (not in feed) : {len(unavailable)}")
    if unavailable:
        print(f"  missing: {', '.join(unavailable)}")

    if not filtered_raw:
        print("FATAL: none of the NIFTY 50 constituents were present in the live feed.")
        print("This is a genuine feed/coverage failure -- no signal will be invented.")
        audit.event("FATAL_NO_NIFTY50_COVERAGE", unavailable=unavailable)
        return 41

    scan_time = now_ist()
    parsed = parse_universe(client, filtered_raw, scan_time)
    healthy = {s: d for s, d in parsed.items() if d.health.healthy}

    print(f"SCAN TIME        : {scan_time:%Y-%m-%d %H:%M:%S.%f} IST")
    print(f"NIFTY 50 RECEIVED: {len(parsed)}")
    print(f"HEALTHY          : {len(healthy)}")

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
    selected = select_global_best(candidates)
    if selected is None:
        print("FATAL: no directional hypothesis could be calculated from the available NIFTY 50 data.")
        audit.event("FATAL_NO_CANDIDATE", healthy=len(healthy))
        return 32

    print_candidate("NIFTY 50 #1 UNDERLYING EQUITY SIGNAL (unmodified equity strategy)", selected)

    print("\nFetching live option chain for the selected symbol...")
    signal, reject_reasons = build_options_signal(client, selected, max_wait_seconds=args.max_wait_seconds)
    if signal is None:
        print("\nOPTIONS SIGNAL: UNAVAILABLE")
        for reason in reject_reasons:
            print(f"  - {reason}")
        print("\nThe underlying equity #1 above is a valid, complete signal on its own.")
        print("Only its OPTIONS premium translation could not be computed right now -- genuine data gap, nothing invented.")
        print("STATUS: EQUITY_SIGNAL_READY_OPTIONS_UNAVAILABLE")
        audit.event(
            "OPTIONS_UNAVAILABLE",
            equity_selected=asdict(selected),
            reasons=list(reject_reasons),
        )
        return 33

    print_options_signal(signal)
    print("\nSTATUS: SIGNAL_READY (NIFTY 50 OPTIONS ENGINE)")
    print("MODE: ISOLATED OPTIONS-PREMIUM OVERLAY -- entry/SL/TP above are the OPTION PREMIUM, not the underlying stock price")
    print("NOTE: delta-linear premium approximation; not a full options repricing model. Not a guarantee of profit. Not production-ready.")
    audit.event(
        "SIGNAL_READY",
        scan_time=scan_time.isoformat(),
        equity_selected=asdict(selected),
        option_side=signal.contract.side,
        strike=signal.contract.strike,
        entry_premium=signal.entry_premium,
        stop_premium=signal.stop_premium,
        target_premium=signal.target_premium,
        delta=signal.contract.delta,
        chain_updated_at=signal.updated_at,
        chain_age_seconds=signal.age_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
