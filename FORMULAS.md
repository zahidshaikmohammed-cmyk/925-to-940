# PSYGRID 09:31 Engine — Mathematical Specification

## 1. Observation window

The engine freezes the **15 completed one-minute candles from 09:15:00 through 09:29:59 IST**. It does not use the still-forming 09:30 candle.

NSE's normal equity market opens at 09:15 IST and closes at 15:30 IST. citeturn0search0

## 2. Opening gap

`Gap% = 100 * (09:15 open / previous close - 1)`

Hard exclusion:

`abs(Gap%) > 3.0%`

A robust cross-sectional z-score is also calculated using median/MAD:

`z = 0.6744897501960817 * (x - median(x)) / MAD(x)`

with `z = 0` when MAD is zero or fewer than five observations exist.

Hard exclusion:

`abs(Gap z) > 3`

## 3. True Range and ATR

For candle `t`:

`TR_t = max(H-L, |H-C_(t-1)|, |L-C_(t-1)|)`

The engine uses the median of the most recent 10 TR values rather than an exchange-supplied indicator.

## 4. Directional impulse

For LONG, the impulse extreme is the highest high in the opening window.

For SHORT, the impulse extreme is the lowest low.

`Impulse% = direction * 100 * (Extreme / OpeningPrice - 1)`

`ImpulseATR = |Extreme - OpeningPrice| / ATR`

Hard exclusions:

- `Impulse% < 0.35%`
- `ImpulseATR > 3.50`
- extreme occurs after bar index 12

## 5. Retracement depth

For LONG:

`Depth = (Extreme - RetracementLow) / (Extreme - OpeningPrice)`

For SHORT:

`Depth = (RetracementHigh - Extreme) / (OpeningPrice - Extreme)`

Strict range:

`0.38 <= Depth <= 0.70`

This deliberately rejects shallow pullbacks.

## 6. Reclaim ratio

For LONG:

`Reclaim = (LastClose - RetracementLow) / (Extreme - RetracementLow)`

For SHORT:

`Reclaim = (RetracementHigh - LastClose) / (RetracementHigh - Extreme)`

Strict requirement: `Reclaim >= 0.50`.

## 7. Retracement volume

`RetracementVolumeRatio = mean(retracement volume) / mean(impulse volume)`

Strict requirement: `<= 0.80`.

The desired geometry is expansion during impulse followed by lower participation during the pullback.

## 8. Directional efficiency

`Efficiency = |LastImpulseClose - OpeningPrice| / sum(|C_t - C_(t-1)|)`

Strict requirement: `>= 0.45`.

## 9. Persistence

`Persistence = favorable candle bodies / 14`

Strict requirement: `>= 0.60`.

## 10. VWAP

`VWAP = sum(TypicalPrice * Volume) / sum(Volume)`

`TypicalPrice = (High + Low + Close) / 3`

For LONG:

`VWAPDistanceATR = (LastClose - VWAP) / ATR`

For SHORT:

`VWAPDistanceATR = (VWAP - LastClose) / ATR`

Strict mode requires positive directional VWAP confirmation and rejects extension beyond 2 ATR.

## 11. Relative strength

`RS_market = direction * (StockReturn - MarketMedianReturn)`

`RS_sector = direction * (StockReturn - SectorMedianReturn)`

When a sector map is unavailable, the healthy-universe median is used as the deterministic benchmark rather than inventing sector classifications.

## 12. Ranking

The score is a weighted sum of normalized components:

- impulse: 18%
- retracement: 20%
- market relative strength: 14%
- sector relative strength: 8%
- volume: 10%
- VWAP: 10%
- structure: 10%
- volatility: 10%

The weights sum to exactly 100%.

Fallback candidates receive an explicit score penalty and are never allowed to outrank a better strict candidate solely because of the fallback path.

## 13. Stop loss

LONG:

`SL = RetracementLow - 0.35 * ATR`

SHORT:

`SL = RetracementHigh + 0.35 * ATR`

## 14. Target

`Risk = |Entry - SL|`

`TargetDistance = max(2.0 * Risk, 1.50 * ATR)`

LONG:

`TP = Entry + TargetDistance`

SHORT:

`TP = Entry - TargetDistance`

## 15. Emergency path

If the normal candidate set is empty, the engine evaluates fallback tiers while preserving the hard exclusions for extreme gaps and exhausted/overextended impulses.

If the selected price crosses the frozen structural stop between 09:30 and 09:31, the engine does not silently die. It deterministically rebases the stop to `max(0.35 ATR, minimum_risk_pct)` and marks the signal `EMERGENCY_STOP_REBASED_AT_09_31`.

## 16. Data-integrity rule

No synthetic candle is created by this strategy. Missing or malformed 09:15–09:29 candles make that stock unhealthy and exclude it from ranking.

A complete 450-symbol shard snapshot is retained so a transient network failure at exactly 09:30 does not destroy the decision.
