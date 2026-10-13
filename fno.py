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

    python fno.py                   # one scan now
    python fno.py --risk-rupees 500
    python fno.py --refresh-fno     # re-download the F&O list now
    python fno.py --self-test
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

import run_engine
from bbbbb import live_core_argv

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
        if not symbol or symbol == "SYMBOL" or symbol in INDEX_UNDERLYINGS:
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


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return run_self_test()
    refresh = "--refresh-fno" in argv
    argv = [a for a in argv if a != "--refresh-fno"]
    symbols, source = load_fno(refresh=refresh)
    d = DEEP_RETRACEMENT
    note = (f"F&O ONLY     : {len(symbols)} option-chain stocks ({source})\n"
            f"DEEP PULLBACK: Tier 1 retracement {d['min_retracement_depth']:.0%}–{d['max_retracement_depth']:.1%} "
            f"of the impulse (best at {d['retracement_sweet_spot']:.1%}) | Tier 2 "
            f"{d['fallback_retrace_min']:.0%}–{d['fallback_retrace_max']:.0%}")
    return run_engine.main(live_core_argv(argv), cfg_overrides=DEEP_RETRACEMENT, universe=symbols,
                           mode_note=note, rerun_hint="fno.py")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
