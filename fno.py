"""FNO: deep pullbacks in F&O stocks only.

The same impulse + retracement engine as bbbbb.py (strategy_930), with two changes:

  - Universe: only stocks with an option chain (NSE F&O underlyings), the liquid names
    where a fill at the printed price is realistic. The list is NSE's own fo_mktlots.csv,
    downloaded at most once a week (data/fno_cache.json); if NSE cannot be reached the
    built-in fno_stocks.json is used.
  - Pullback depth: only DEEP retracements of the impulse qualify: 50%–78.6% for a
    strict (Tier 1) setup, scored best at the 61.8% level; 45%–85% for Tier 2.

Everything else is unchanged: the stock must make a real impulse, pull back on lighter
volume, start to reclaim, sit on the right side of VWAP and lead the market in the
trade's direction. Entry is the break of the last candle; stop beyond the pullback
extreme; 2R target.

    python fno.py                   # hunt from 09:15: rescan every minute, beep on a Tier 1 signal
    python fno.py --once            # one scan now
    python fno.py --risk-rupees 500
    python fno.py --until 14:30     # stop hunting earlier (default 15:00)
    python fno.py --refresh-fno     # re-download the F&O list now
    python fno.py --self-test
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

import run_engine
from bbbbb import live_core_argv
from flat import alert_beep, seconds_to_next_scan
from run_engine import IST, SESSION_START, now_ist

NSE_FNO_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
BUILTIN_PATH = Path(__file__).with_name("fno_stocks.json")
CACHE_PATH = Path("data/fno_cache.json")
CACHE_DAYS = 7
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}

# Deep pullbacks only (fractions of the impulse leg). The leg finder in strategy_930 already
# ignores anything outside 25%-85%, so 85% is the deepest a Tier 2 setup can be.
DEEP_RETRACEMENT = {
    "min_retracement_depth": 0.50,
    "max_retracement_depth": 0.786,
    "retracement_sweet_spot": 0.618,
    "fallback_retrace_min": 0.45,
    "fallback_retrace_max": 0.85,
}


def parse_mktlots(text: str) -> set[str]:
    """Stock symbols from NSE's fo_mktlots.csv (UNDERLYING, SYMBOL, lot sizes...); indices dropped."""
    symbols: set[str] = set()
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        symbol = row[1].strip().upper()
        if not symbol or symbol == "SYMBOL" or symbol in INDEX_UNDERLYINGS or symbol.startswith("NIFTY"):
            continue
        symbols.add(symbol)
    return symbols


def download_fno(timeout: float = 10.0) -> set[str]:
    req = Request(NSE_FNO_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
    with urlopen(req, timeout=timeout) as response:
        symbols = parse_mktlots(response.read().decode("utf-8", errors="replace"))
    if len(symbols) < 50:  # a block page or an empty file, not the F&O list
        raise ValueError(f"only {len(symbols)} symbols parsed")
    return symbols


def load_fno(refresh: bool = False, today: date | None = None, download=download_fno,
             cache_path: Path = CACHE_PATH, builtin_path: Path = BUILTIN_PATH) -> tuple[set[str], str]:
    """(symbols, where they came from). Fresh cache, else NSE download, else stale cache, else built-in."""
    today = today or date.today()
    cached = None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if cached and not refresh:
        age = today - date.fromisoformat(cached["date"])
        if age <= timedelta(days=CACHE_DAYS):
            return set(cached["symbols"]), f"NSE list from {cached['date']} (cached)"
    try:
        symbols = download(timeout=10.0)
    except Exception as exc:
        reason = f"NSE download failed: {exc}"
    else:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"date": today.isoformat(), "symbols": sorted(symbols)}), encoding="utf-8")
        except OSError:
            pass
        return symbols, f"NSE list downloaded {today.isoformat()}"
    if cached:
        return set(cached["symbols"]), f"NSE list from {cached['date']} (stale; {reason})"
    builtin = json.loads(builtin_path.read_text(encoding="utf-8"))
    return set(builtin["symbols"]), f"built-in fno_stocks.json ({reason})"


def run_self_test() -> int:
    import unittest
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName("tests.test_fno"))
    return 0 if result.wasSuccessful() else 10


def scan_once(engine_argv: list[str], symbols: set[str], note: str) -> int:
    return run_engine.main(engine_argv, cfg_overrides=DEEP_RETRACEMENT, universe=symbols,
                           mode_note=note, rerun_hint="fno.py")


def is_tier1_signal(status: str | None, selected) -> bool:
    return status == "SIGNAL_READY" and selected is not None and selected.tier == 1


def hunt(engine_argv: list[str], symbols: set[str], note: str, until: str, offset: int, realert_minutes: int = 60,
         clock=now_ist, sleep=time.sleep, beep=alert_beep, scan=scan_once) -> int:
    """Rescan every minute from 09:15 to ``until``; print and beep only on a new Tier 1 SIGNAL_READY.

    Every other minute prints one line (the closest stock and why it is not a signal). The same
    stock and side alerts again only after ``realert_minutes``.
    """
    stop_at = datetime.strptime(until, "%H:%M").time()
    alerted: dict[tuple[str, str], datetime] = {}
    print(note)
    print(f"HUNT MODE    : rescanning every minute from {SESSION_START:%H:%M} until {stop_at:%H:%M} IST | "
          f"beeps ONLY on a Tier 1 SIGNAL_READY | Ctrl+C to stop", flush=True)
    while True:
        now = clock()
        if now.time() >= stop_at:
            print(f"\n{now:%H:%M:%S} hunt over ({stop_at:%H:%M} IST). {len(alerted)} Tier 1 signal(s) today.")
            return 0
        first = datetime.combine(now.date(), SESSION_START, IST) + timedelta(minutes=1, seconds=offset)
        if now < first:
            print(f"{now:%H:%M:%S} waiting for the open: first scan at {first:%H:%M:%S} IST", flush=True)
            sleep(max(1.0, (first - now).total_seconds()))
            continue
        run_engine.LAST_STATUS = run_engine.LAST_SELECTED = None  # a crashed scan must not reuse the last result
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                code = scan(engine_argv, symbols, note)
            except Exception as exc:  # one bad minute must not end the hunt
                code = -1
                print(f"ERROR: {exc}")
        status, best = run_engine.LAST_STATUS, run_engine.LAST_SELECTED
        stamp = clock()
        if code == 20:
            print(buf.getvalue().strip().splitlines()[-1])
            return 20  # not a trading day
        key = (best.symbol, best.side) if best is not None else None
        fresh = key is not None and (key not in alerted or stamp - alerted[key] >= timedelta(minutes=realert_minutes))
        if is_tier1_signal(status, best) and fresh:
            beep()
            alerted[key] = stamp
            print("\n" + "=" * 96)
            print(f"🔔 TIER 1 SIGNAL @ {stamp:%H:%M:%S} IST: {best.symbol} {best.side}")
            print(buf.getvalue(), flush=True)
        elif best is not None:
            why = ("already alerted" if is_tier1_signal(status, best) else
                   f"Tier {best.tier}" + ("" if status == "SIGNAL_READY" else f", {status}"))
            print(f"{stamp:%H:%M:%S} no Tier 1 signal | closest {best.symbol} {best.side} ({why})", flush=True)
        else:
            last = [line for line in buf.getvalue().splitlines() if line.strip()]
            print(f"{stamp:%H:%M:%S} no Tier 1 signal | {last[-1][:110] if last else f'exit code {code}'}", flush=True)
        sleep(seconds_to_next_scan(clock(), offset))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="F&O deep-pullback hunter", add_help=False)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--refresh-fno", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--until", default="15:00")
    parser.add_argument("--scan-offset", type=int, default=8)
    args, engine_args = parser.parse_known_args(argv)
    if args.self_test:
        return run_self_test()
    datetime.strptime(args.until, "%H:%M")  # fail fast on a bad time
    symbols, source = load_fno(refresh=args.refresh_fno)
    d = DEEP_RETRACEMENT
    note = (f"F&O ONLY     : {len(symbols)} option-chain stocks ({source})\n"
            f"DEEP PULLBACK: Tier 1 retracement {d['min_retracement_depth']:.0%}–{d['max_retracement_depth']:.1%} "
            f"of the impulse (best at {d['retracement_sweet_spot']:.1%}) | Tier 2 "
            f"{d['fallback_retrace_min']:.0%}–{d['fallback_retrace_max']:.0%}")
    engine_argv = live_core_argv(engine_args)
    if args.once or "-h" in engine_args or "--help" in engine_args:
        return scan_once(engine_argv, symbols, note)
    try:
        return hunt(engine_argv, symbols, note, args.until, args.scan_offset)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
