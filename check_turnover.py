"""Show a stock's recent 1-minute volume and rupee turnover from the PSYGRID feed.

    python check_turnover.py 360ONE            # last 15 candles
    python check_turnover.py 360ONE 30

Compare the VOLUME column with the 1-minute volume bars on your broker's chart.
If they match, the stock really is trading below the engine's turnover floor; if the
feed's numbers are much smaller, the feed is undercounting volume.
"""

from __future__ import annotations

import sys
from statistics import median

from config import StrategyConfig
from psygrid_client import PsygridClient
from run_engine import LIVE_CORE_URL


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    symbol = argv[0].upper()
    count = int(argv[1]) if len(argv) > 1 else 15
    payload = PsygridClient(LIVE_CORE_URL, timeout=20)._get(f"public/stock/{symbol}.json")
    candles = payload.get("candles_1m") or []
    if not candles:
        print(f"{symbol}: status={payload.get('status')} - no candles")
        return 1
    floor = StrategyConfig().min_median_turnover_rupees
    lookback = StrategyConfig().turnover_lookback_bars
    print(f"{symbol}: {len(candles)} candles today")
    print(f"{'TIME':<9}{'CLOSE':>12}{'VOLUME':>12}{'TURNOVER':>14}")
    for c in candles[-count:]:
        turnover = c["close"] * c["volume"]
        print(f"{c['timestamp'][11:16]:<9}{c['close']:>12.2f}{c['volume']:>12,}{turnover / 1e5:>12.1f} L")
    recent = [c["close"] * c["volume"] for c in candles[-lookback:]]
    day = sum(c["close"] * c["volume"] for c in candles)
    print(f"\nMEDIAN TURNOVER (last {lookback}): ₹{median(recent) / 1e5:.1f} lakh/min | engine floor ₹{floor / 1e5:.1f} lakh/min")
    print(f"DAY TURNOVER SO FAR: ₹{day / 1e7:.2f} Cr | DAY VOLUME: {sum(c['volume'] for c in candles):,} shares")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
