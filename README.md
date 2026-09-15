# 09:15–09:30 Opening-Impulse + Deep-Retracement Engine

Local Python engine for NSE cash equities. **Psygrid remains the data source; this repository performs the strategy mathematics locally and ignores Psygrid's precomputed indicators.**

## Exact operating sequence

- **09:15 onward:** continuously poll all ten PSYGRID A–J shards and maintain the 450-stock opening dataset.
- **09:30:00:** play a terminal beep, take one atomic snapshot, and freeze the ranking.
- The decision dataset is strictly the **15 completed 1-minute candles from 09:15 through 09:29**. The 09:30 candle is not used because it is not complete at 09:30:00.
- Evaluate every healthy stock in both directions.
- Produce **one best LONG and one best SHORT**.
- **09:31:00:** refresh only the two selected symbols and print their live LTP as the executable entry reference.

NSE's regular equity market opens at 09:15 IST. citeturn0search0

## Health layer

A stock is removed from the 09:30 strategy run if its feed is stale, LTP is invalid/stale, any required 09:15–09:29 minute is missing, OHLC geometry is invalid, or previous close is unavailable. The engine does **not** abort because some stocks fail; it runs on the remaining healthy universe.

## Strategy mathematics

For each healthy stock and each direction:

1. **Opening impulse:** from the 09:15 open to the directional extreme before 09:30.
2. **Deep retracement:** after the extreme, measure the opposite excursion relative to the complete impulse. Required depth is 38%–70%.
3. **Reclaim:** the final pre-09:30 close must recover at least 50% of the retracement leg.
4. **Volume contraction:** mean retracement volume / mean impulse volume must be <= 0.80.
5. **Directional efficiency:** absolute net impulse movement / total absolute close-to-close path must be >= 0.45.
6. **VWAP:** price must be on the candidate side of our own session VWAP and not more than 2 ATR away.
7. **Anti-chasing:** the directional extreme must not occur too late (bar 12+), impulse must not exceed 3.5 ATR, and an extreme impulse cannot remain glued to its extreme.
8. **Gap filter:** absolute opening gap must be <= 3%; robust MAD z-score must be <= 3 when enough historical gap observations exist.
9. **Persistence:** at least 60% of the 14 post-opening candle bodies must agree with the candidate direction.
10. **Relative strength:** candidate-direction stock return must outperform the healthy-universe market median in the same direction.
11. **Sector strength:** if `sector_map.json` is present, compare the stock with the median return of its mapped sector peers. Without that file, the engine uses the healthy-universe median as a transparent fallback proxy and prints a warning.

## Ranking

Score is deterministic and bounded to 0–100:

`100 × (0.18 impulse + 0.24 retracement + 0.18 relative-strength + 0.10 volume + 0.10 VWAP + 0.10 structure + 0.10 volatility)`

No machine learning and no Psygrid MA/EMA/RSI/VWAP fields are used.

## Risk levels

The stop is structural:

- LONG: retracement low − 0.35 × ATR
- SHORT: retracement high + 0.35 × ATR

Target distance is `max(2 × risk, 1.5 × ATR)`. These are research defaults and must be backtested/paper-tested before live use.

## Run locally in PowerShell

```powershell
cd path\to\925-to-940
python -m pip install -r requirements.txt
python main.py
```

The program is intentionally a **signal engine**, not an order-execution bot.

NSE Indices defines its sector classification as a four-level industry structure, so the optional `sector_map.json` is deliberately kept separate from strategy code and should be maintained from an authoritative classification source. citeturn2search1
