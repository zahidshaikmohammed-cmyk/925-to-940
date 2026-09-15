# 09:25–09:40 Opening-Momentum + Relative-Strength Engine

Deterministic Python strategy engine for the first 15 minutes after the opening phase of the NSE session.

## Objective

Continuously evaluate the configured liquid NIFTY 500 subset during **09:25–09:40 Asia/Kolkata**, rank only confirmed directional candidates, and **lock exactly Top 3 decisions at 09:40**. No selection changes are permitted after the 09:40 lock.

This repository is a strategy engine, not an order-execution system. It does not place orders, size positions, invent candles, or fabricate missing data.

## Data contract

The production source is PSYGRID. The engine expects genuine completed 1-minute OHLCV plus PSYGRID's native 15-minute, 1-hour and daily context. NIFTY benchmark data is required. Sector membership is supplied through `sector_map.json` or an equivalent adapter.

The engine never interpolates or fills missing OHLCV.

## Decision timeline

- 09:15–09:24: opening data accumulation.
- 09:25–09:40: candidate evaluation on each newly completed 1-minute candle.
- 09:40: final deterministic ranking and **Top 3 lock**.
- After 09:40: decisions are immutable for the session.

## Direction

A stock can qualify LONG or SHORT. The directional signal is determined by the same mathematical feature set with sign symmetry.

## Hard gates

A candidate must satisfy all of these before scoring:

1. Complete 09:15–09:24 opening history and valid NIFTY benchmark.
2. Price and OHLCV data are finite and non-negative where applicable.
3. Minimum liquidity/activity threshold is met.
4. Opening directional move is at least 0.35% in absolute value.
5. Stock relative strength versus NIFTY is at least 0.20 percentage points in the candidate direction.
6. Sector relative strength versus the sector median is at least 0.10 percentage points in the candidate direction.
7. Price is on the correct side of session VWAP.
8. Price is not already extended: distance from VWAP must be <= 1.80 ATR(1m-equivalent) and distance from opening extreme must be <= 1.25 ATR(1m-equivalent).
9. A directional 1-minute structure confirmation exists: latest close is above the 09:15–09:24 opening high for LONG or below the opening low for SHORT, OR the latest two closes make a higher-high/higher-low or lower-low/lower-high continuation pattern.
10. Higher-timeframe alignment: 15m and 1h trend signs must agree with the candidate direction.

If fewer than 3 candidates pass the gates, fewer than 3 are locked. The engine never manufactures a third choice.

## Ranking score

Each gated candidate receives a 0–100 score:

`Score = 100 * (0.22*M + 0.22*RS + 0.16*SECTOR + 0.14*RVOL + 0.10*VWAP + 0.08*STRUCTURE + 0.05*HTF + 0.03*PERSISTENCE)`

All component values are normalized to `[0, 1]` using fixed piecewise-linear clamps. No machine learning or adaptive thresholding is used in v1.

- `M`: opening directional momentum.
- `RS`: relative strength versus NIFTY.
- `SECTOR`: relative strength versus sector median.
- `RVOL`: relative activity/volume.
- `VWAP`: distance and side-of-VWAP quality.
- `STRUCTURE`: opening-range break/continuation quality.
- `HTF`: 15m + 1h directional alignment.
- `PERSISTENCE`: fraction of post-09:25 observations that retain the same directional edge.

## Persistence

At each completed 1-minute evaluation, the candidate direction is recorded. Persistence is:

`P = directional_observations / eligible_observations`

A candidate must have at least 3 eligible observations by 09:40 and `P >= 0.60` to pass the final persistence gate.

## Tie-breakers

Ranks are deterministic:

1. higher score;
2. higher relative strength;
3. higher sector relative strength;
4. higher persistence;
5. lower extension distance;
6. lexicographically smaller symbol.

## Important

This is a mathematically explicit **research specification**, not a claim of profitability. It must be backtested and then paper-tested on genuine PSYGRID data before any live trading use.
