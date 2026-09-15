# PSYGRID 09:31 Engine — Mathematical Specification

## 1. Observation window

The engine freezes the 15 completed one-minute candles from 09:15:00 through 09:29:59 IST. It does not use the still-forming 09:30 candle.

## 2. Opening gap

`Gap% = 100 * (09:15 open / previous close - 1)`

Hard exclusion: `abs(Gap%) > 3.0%`.

Robust cross-sectional z-score:

`z = 0.6744897501960817 * (x - median(x)) / MAD(x)`

with `z = 0` when MAD is zero or fewer than five observations exist.

Hard exclusion: `abs(Gap z) > 3`.

## 3. True Range and ATR

`TR_t = max(H-L, |H-C_(t-1)|, |L-C_(t-1)|)`

ATR is the median of the most recent 10 TR values.

## 4. Directional impulse

For LONG, the impulse extreme is the highest high. For SHORT, it is the lowest low.

`Impulse% = direction * 100 * (Extreme / OpeningPrice - 1)`

`ImpulseATR = |Extreme - OpeningPrice| / ATR`

Hard exclusions:

- `Impulse% < 0.35%`
- `ImpulseATR > 3.50`
- extreme occurs after bar index 12

## 5. Retracement depth

LONG:
`Depth = (Extreme - RetracementLow) / (Extreme - OpeningPrice)`

SHORT:
`Depth = (RetracementHigh - Extreme) / (OpeningPrice - Extreme)`

Strict range: `0.38 <= Depth <= 0.70`.

## 6. Reclaim ratio

LONG:
`Reclaim = (LastClose - RetracementLow) / (Extreme - RetracementLow)`

SHORT:
`Reclaim = (RetracementHigh - LastClose) / (RetracementHigh - Extreme)`

Strict requirement: `Reclaim >= 0.50`.

## 7. Retracement volume

`RetracementVolumeRatio = mean(retracement volume) / mean(impulse volume)`

Strict requirement: `<= 0.80`.

## 8. Directional efficiency

`Efficiency = |LastImpulseClose - OpeningPrice| / sum(|C_t - C_(t-1)|)`

Strict requirement: `>= 0.45`.

## 9. Persistence

`Persistence = favorable candle bodies / 14`

Strict requirement: `>= 0.60`.

## 10. VWAP

`VWAP = sum(TypicalPrice * Volume) / sum(Volume)`

`TypicalPrice = (High + Low + Close) / 3`

LONG:
`VWAPDistanceATR = (LastClose - VWAP) / ATR`

SHORT:
`VWAPDistanceATR = (VWAP - LastClose) / ATR`

Strict mode requires positive directional VWAP confirmation and rejects extension beyond 2 ATR.

## 11. Relative strength

`RS_market = direction * (StockReturn - MarketMedianReturn)`

`RS_sector = direction * (StockReturn - SectorMedianReturn)`

When a sector map is unavailable, the healthy-universe median is used as the deterministic benchmark rather than inventing classifications.

## 12. Ranking

Weights:

- impulse: 18%
- retracement: 20%
- market relative strength: 14%
- sector relative strength: 8%
- volume: 10%
- VWAP: 10%
- structure: 10%
- volatility: 10%

Total: exactly 100%.

Fallback candidates receive an explicit score penalty.

## 13. Stop loss

LONG: `SL = RetracementLow - 0.35 * ATR`

SHORT: `SL = RetracementHigh + 0.35 * ATR`

## 14. Target

`Risk = |Entry - SL|`

`TargetDistance = max(2.0 * Risk, 1.50 * ATR)`

LONG: `TP = Entry + TargetDistance`

SHORT: `TP = Entry - TargetDistance`

## 15. Emergency path

If strict candidates are empty, fallback tiers are evaluated while preserving the hard exclusions for extreme gaps and exhausted/overextended impulses.

If the selected price crosses the frozen structural stop between 09:30 and 09:31, the engine deterministically rebases the stop to an ATR/risk-based emergency distance and marks the signal `EMERGENCY_STOP_REBASED_AT_09_31`.

## 16. Data integrity

No synthetic candle is created. Missing or malformed 09:15–09:29 candles make that stock unhealthy and exclude it from ranking.

A complete 450-stock snapshot is retained so a transient network failure at exactly 09:30 does not destroy the decision.
