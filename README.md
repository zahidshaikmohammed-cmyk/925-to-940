# 09:15–09:30 Opening-Impulse + Deep-Retracement Engine

Local Python engine for NSE cash equities. **Psygrid remains the live data source; this repository performs the strategy mathematics locally and ignores Psygrid's precomputed indicators.**

## Exact operating sequence

- **09:15 onward:** continuously poll all ten PSYGRID A–J shards and maintain the 450-stock opening dataset.
- **09:30:00:** terminal beep, one final atomic snapshot, then immutable ranking.
- The decision dataset is strictly the **15 completed 1-minute candles from 09:15 through 09:29**. The 09:30 candle is deliberately excluded to prevent look-ahead.
- Evaluate every healthy stock in both directions.
- Output **one best LONG and one best SHORT**.
- **09:31:00:** refresh only those two symbols and print the live LTP as the entry reference.

NSE's regular equity market opens at 09:15 IST. citeturn0search0

## Health layer

A stock is removed from that run if its LTP is stale/invalid, any required 09:15–09:29 minute is missing, OHLC geometry is invalid, or previous close is unavailable. The engine does **not** abort because some stocks fail; it evaluates every remaining healthy stock.

## Strategy mathematics

For every healthy stock and both LONG/SHORT directions:

1. **Opening impulse:** 09:15 open to the directional extreme before 09:30.
2. **Deep retracement:** opposite excursion divided by the complete impulse; required depth **38%–70%**.
3. **Reclaim:** final pre-09:30 close recovers at least **50%** of the retracement leg.
4. **Volume contraction:** mean retracement volume / mean impulse volume <= **0.80**.
5. **Directional efficiency:** absolute net impulse movement / absolute close-to-close path >= **0.45**.
6. **VWAP:** our own OHLCV VWAP; candidate price must be on the correct side and <= **2 ATR** away.
7. **Anti-chasing:** extreme cannot occur too late; impulse <= **3.5 ATR**; a huge impulse still glued to its extreme is rejected as exhaustion.
8. **Gap filter:** absolute opening gap <= **3%** and robust MAD z-score <= **3** when sufficient gap history exists.
9. **Persistence:** at least **60%** of post-opening candle bodies agree with the direction.
10. **Relative strength:** stock return versus the healthy-universe market median.
11. **Sector strength:** stock return versus the median return of mapped sector peers when `sector_map.json` is present. If it is absent, the engine explicitly reports that the market median is being used as a proxy.

### Deterministic score

`Score = 100 × (0.18·Impulse + 0.24·Retracement + 0.12·MarketRS + 0.06·SectorRS + 0.10·Volume + 0.10·VWAP + 0.10·Structure + 0.10·Volatility)`

All components are fixed piecewise-linear normalizations. No ML and no Psygrid MA/EMA/RSI values are consumed.

### Risk

LONG stop = retracement low − 0.35×ATR. SHORT stop = retracement high + 0.35×ATR.

Target distance = `max(2×risk, 1.5×ATR)`.

These are research defaults, not a profitability guarantee; backtest and paper-test before live use.

## Run locally in PowerShell

```powershell
cd path\to\925-to-940
python -m pip install -r requirements.txt
python main.py
```

The program is a **signal engine only**; it does not place orders.

NSE Indices maintains an official four-level industry classification (macro sector, sector, industry, basic industry), which is why the sector map is kept separate and should be sourced from an authoritative classification. citeturn2search1
