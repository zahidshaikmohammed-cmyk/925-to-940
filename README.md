# 09:15–09:30 Opening-Impulse + Deep-Retracement Engine

> **945 continuous scan:** `python 945.py` now rescans every stock once a minute from 09:30
> and beeps only on a confirmed Tier 1 setup that still pays after costs. It tracks the
> trade, keeps a journal in `data/scan/`, and can replay saved sessions with
> `python 945.py --scan-replay data/sessions`. See RUNBOOK.md. The original 09:45 single
> decision is `python 945.py --daemon`.

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

### Precision gates (`run_engine.py` / `bbbbb.py`)

- **Live data only:** no signal unless the feed says `status: OK` and session `LIVE` (exit code 33).
- **Per-stock screen:** a stock is skipped if its newest candle closed more than 3 minutes ago, or if its median rupee turnover per minute over the last 30 minutes is under ₹2 lakh.
- **Direction follows relative strength:** no tier, Tier 3 included, buys a stock that is lagging the market or shorts one that is beating it (`min_rs_market`, default 0%).
- **VWAP stretch cap:** Tier 1 and Tier 2 reject a close more than 2.0 ATR from VWAP (`max_extension_from_vwap_atr`, `fallback_extension_atr_max`). The VWAP score peaks at 1.0 ATR and decays to 0 at the cap.
- **Trigger entry:** enter only on a break of the last completed candle's low (SHORT) or high (LONG), placed as a stop-entry order. Cancel it after 2 candles, or if the stop trades first. The target is re-measured from the trigger so the trade stays 2R. `--risk-rupees N` prints the share quantity; `--min-turnover N` changes the ₹2 lakh/min liquidity floor (0 = off).
- **Exit timing:** the clock comes from the stock's own pace, not a fixed 30 or 60 minutes. Pace is half the stock's fastest sustained 5–15-bar run in the trade's direction over the last 36 bars. From that pace the engine prints three times:
  - a **checkpoint**: price must reach +0.5R within 1.5× the expected time, or exit;
  - a **target ETA**: distance ÷ pace;
  - a **time stop**: 2× the ETA, never after 15:15.
  If the stock has no measurable move in the trade's direction, it prints "skip".
- **Status:** `SIGNAL_READY` only for a Tier 1/2 setup scoring ≥ 55; a weaker one is `LOW_CONFIDENCE_WEAK`. Tier 3 scores use a looser formula and are not comparable (shown as `forced=`); Tier 3 is labelled `LOW_CONFIDENCE_FORCED`: no stock passed the pattern gates, so either skip the trade or trade minimum size.

## Run locally in PowerShell

```powershell
cd path\to\925-to-940
python -m pip install -r requirements.txt
python main.py
```

The program is a **signal engine only**; it does not place orders.

NSE Indices maintains an official four-level industry classification (macro sector, sector, industry, basic industry), which is why the sector map is kept separate and should be sourced from an authoritative classification. citeturn2search1

## Precision pick (`precision.py`) and outcome grading (`grade_signals.py`)

A `SIGNAL_READY` trade must pass every one of these checks:

1. **A Tier 1 or 2 setup** that passes all the gates above, with a setup score of at least 55.
2. **The market on its side.** The engine counts the share of liquid stocks above VWAP: 60% or more means longs only, 40% or less means shorts only. In between, the trade needs 10 more persistence points.
3. **Not already run.** No entry in a stock that has already moved 7% or more today in the trade's direction, counting from the previous close so gaps count too (`max_day_move_pct`).
4. **The entry window.** No entries before 09:20 or after 14:45. Between 12:00 and 13:30 the trade needs 10 more persistence points.
5. **Trend persistence of at least 60/100.** This measures, on the trade's side:
   - VWAP hold: the share of the last 60 candles that closed on the trade's side of VWAP, and how few times price crossed VWAP in the last 30;
   - 5-minute structure: higher highs and higher lows for a long, lower highs and lower lows for a short;
   - volume agreement: how much volume is moving the trade's way versus against it;
   - opening range: whether price has held beyond the 09:15–09:29 range;
   - relative strength against both the market and the sector.

   It is reduced for these reversal warnings:
   - a climax candle;
   - a rejection wick at the day's high or low;
   - three or more failed breaks of the day's high or low;
   - after 14:00, a stock already up (or down) more than 6% on the day.

Among the trades that pass, the engine picks the highest **conviction**: 0.4 × setup score + 0.6 × persistence. If none pass, it prints `NO TRADE NOW` with only the closest candidate's name and what blocked it, and no trade plan, and the status `NO_PRECISION_SETUP` or `NO_NEW_ENTRIES`. Do not trade those.

Run `python grade_signals.py` before 15:15, when Live Core clears its candles. It replays every signal you logged today exactly as it was printed:
- fill on the trigger;
- then stop, target, missed checkpoint or time stop;
- whenever one candle touches both the stop and the target, the stop counts first.

Results go into `graded_signals.jsonl`, your trade journal, with summaries by status, tier and conviction band. All thresholds are starting values. Judge them after 50 or more `SIGNAL_READY` trades.

