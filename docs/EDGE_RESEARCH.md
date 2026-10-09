# Six-family intraday edge research (15–30 minute holds)

Research only: nothing here places orders or connects to a broker. Run on the PC that holds the data:

```
python 945.py --bootstrap      # once: data/history/yahoo_5m.json.gz (60 days, 5-minute candles)
python edge_research.py        # report on screen + data/edge_report.txt + data/edge_trades.csv
python -m pytest tests/test_edge.py
```

`research.py` (the earlier 7-strategy test) and the dataset are unchanged.

## Previous methodology (research.py), for comparison
- Cost: a flat 0.142% per round trip.
- Entries at the next bar's open; the stop is checked before the target.
- Holds run to 15:15.
- The universe is the top 300 by the prior 10 days' turnover.
- Design/test split at 2026-09-16.
- Day selection used each day's full-session completeness. That is acceptable for picking days, but it is not decision-time information.

## Data and eligibility (edge/data.py)
- Days are laid out on a 75-slot grid (09:15–15:25). A missing candle stays `None`: it is never forward-filled and its volume is never set to zero.
- The audit reports:
  - symbols loaded and failed downloads (the `failed` map in the file);
  - candles off the 5-minute grid, duplicate timestamps and invalid OHLC;
  - zero-volume candles and stale runs (6 or more flat bars with no volume);
  - dates dropped as partial, including the download day;
  - coverage by date and by symbol, and the most-missing time slots;
  - overnight gaps above 15%, flagged as possible corporate actions.
- The downloader (`intelligence/history.parse_chart`) had already dropped invalid or NaN candles and de-duplicated timestamps. Those therefore appear only as missing intervals.
- Universe for session i, built only from sessions before i:
  - 8 or more of the previous 10 sessions present;
  - previous close at least ₹50;
  - top 300 by median daily turnover over those sessions.
  The reasons each symbol was excluded are reported.
- Survivorship: the list is today's 989-stock universe. Delisted, merged and renamed stocks are missing, and there is no corporate-action audit trail. There is no bid/ask or depth data.

## Shared definitions
- **ATR(k):** median high–low of up to 12 present bars before k, same day only.
- **MKT:** the universe median.
- **Signal timing:** signals form on the close of bar k = 3..65. Entry is at the next bar's open; no trade if that candle is missing.
- **Risk:** 0.10–3% of the entry price.
- **Frequency:** one trade per symbol, per variant, per day.
- **Exits:**
  - the stop is checked first; a gap through it fills at the open;
  - if one bar touches both stop and target, the stop is assumed. This is counted, and the optimistic alternative (target first) is reported too.
  - otherwise a time exit at the close of the 3rd or 6th bar (15 or 30 minutes), never later than the 15:10 bar's close.

## Families (15 variants, fixed before any result)

| Family | Rule | Variants |
|---|---|---|
| SWEEP | Bar k trades ≥0.1 ATR beyond the L-bar high/low and closes back inside → fade. Stop beyond bar k's extreme by 0.1 ATR; 1.5R target; 30 min | L = 6, 12 |
| IMPULSE | 3 same-direction bars moving ≥3 ATR, ending at k−4..k−2; pullback retrace in band f; bar k closes beyond bar k−1's extreme → continue. Stop at the pullback extreme; 1.5R; 30 min | f = [0.382, 0.618], [0.25, 0.50] |
| SQUEEZE | 6-bar box width ≤ q × the median of the same slots over the prior 10 days; bar k closes ≥0.1 ATR outside with body ≥60% → break. Stop at the box midpoint (false breakout); 2R; 30 min | q = 0.5, 0.7 |
| XSRS | Every 30 minutes (09:45–14:10), rank the universe by 30-min return minus the median. Top 5 long / bottom 5 short (CONT) or the reverse (REV). Catastrophe stop 1.5%; time exit | CONT 15 min, CONT 30 min, REV 30 min |
| VOLSURP | Volume / median of the same slot over the prior 10 days ≥ T; range ≥0.5 ATR. CONT: strong body closing at the extreme → with the candle. REV: rejection wick ≥60% → against it. 1.5R; 30 min | T = 3, 5 × CONT/REV |
| FAILBO | Opening range = first N bars; the first close outside it, then a close back inside within 3 bars → trade back in. Stop beyond the extreme since the breakout; 1.5R; 30 min | N = 3, 6 |

## Costs (edge/costs.py)

**Base model, for a ₹5,000 position:**

| Component | Rate |
|---|---|
| Brokerage | lower of ₹20 and 0.03% per order |
| STT | 0.025% on the sell side |
| NSE transaction charge | 0.00297% per side |
| SEBI fee | 0.0001% per side |
| GST | 18% on brokerage + exchange charge + SEBI fee |
| Stamp duty | 0.003% on the buy side |
| Slippage | 2 bps per side (an assumption; there are no quotes) |

**Scenarios reported:**
- OLD_FLAT: 0.142%
- BASE: 2 bps/side slippage
- ADVERSE: 5 bps/side
- SEVERE: 10 bps/side

Verify the rates against the broker's current schedule.

## Validation (edge/validate.py)

**Splits:**
- 10 warm-up sessions, used only for baselines.
- The **last 20% of sessions (at least 8)** form the final holdout. It is not used for any choice and is evaluated once.
- The remaining sessions form 4 chronological folds. For folds 1–3, the variant is chosen on the earlier folds: best mean net per trade with at least 30 training trades, else the pre-registered default.
- No trade spans sessions, so forward windows cannot overlap a boundary. An `--embargo N` option still exists.

**Uncertainty:**
- Day-clustered bootstrap: 2,000 draws with a fixed seed, giving a 95% CI and a one-sided p-value.
- Holm correction across the six families.
- A matched random-entry baseline: same symbol, day, risk, target and hold.

**Adequacy:** the run states whether the holdout (≥15 sessions) and the walk-forward period (≥40) are adequate. With 60 sessions they are **not**.

## Promotion criteria (all must hold)
1. Out-of-sample net > 0, with Holm-adjusted p < 0.05.
2. Profit factor ≥ 1.2.
3. At least 100 trades on at least 15 sessions.
4. Positive in at least 2 of the 3 folds.
5. Holdout net > 0 on at least 20 trades.
6. Net > 0 at ADVERSE cost.
7. Beats the matched random baseline.
8. No single symbol above 25% of positive net.

A strategy that passes is a **candidate**. Sixty days cannot prove a durable edge.

## Verified behaviour
- `tests/test_edge.py` (20 tests) covers:
  - signals unchanged when every later candle is scrambled;
  - next-bar entry;
  - stop-first on ambiguous bars, gap fills, the 15:10 cap and risk limits;
  - cost arithmetic;
  - audit counts, with missing candles kept missing;
  - partial-day removal;
  - universe and baselines that ignore later data;
  - chronological splits with an untouched holdout;
  - Holm correction and a deterministic bootstrap.
- A synthetic random walk (400 stocks × 60 days) gives gross ≈ 0 and fails every family, as it must.
- The leakage test found a real bug during development: XSRS at 09:45 read `c[k-6]` = `c[-1]`, the day's closing price. It is fixed, and the test fails if the old line returns.
