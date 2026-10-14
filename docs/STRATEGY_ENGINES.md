# Ten intraday strategy engines (`strategy_engines.py`, package `strategies/`)

Ten independent engines scan the PSYGRID 989-stock universe every minute from 09:16 to 15:16 IST.
Each one implements its own strategy mathematics, gates and score. A Telegram alert goes out only
when a stock passes **all** of that engine's mandatory conditions **and** scores 90 or more.

> **Read this first.** A score of 90+ means that the strategy's conditions are present at high
> quality, as defined below. It is **not** a probability of profit. No engine here has been
> validated on NSE data with out-of-sample, net-of-cost results; the repository has no
> 1-minute history to do so. Every threshold is a **design prior** set before any results were seen.
> Treat the engines as research instruments: replay saved sessions (`--replay`) and judge each one
> only on a large sample of out-of-sample trades.

## Contents
1. [Evidence grading](#1-evidence-grading)
2. [Data, timing and look-ahead control](#2-data-timing-and-look-ahead-control)
3. [Shared definitions](#3-shared-definitions)
4. [The score and what 90+ means](#4-the-score-and-what-90-means)
5. [Trade plan, ticks and costs](#5-trade-plan-ticks-and-costs)
6. [The ten engines](#6-the-ten-engines)
7. [Duplicates, lifecycle and Telegram](#7-duplicates-lifecycle-and-telegram)
8. [Daily operation](#8-daily-operation)
9. [Validation performed](#9-validation-performed)
10. [Known limitations and what remains unverified](#10-known-limitations-and-what-remains-unverified)
11. [References](#11-references)

---

## 1. Evidence grading

Each mechanism below is labelled with one of these grades:

- **[E] Evidence:** a peer-reviewed study documents the effect. The study's market and horizon
  are named, and they are rarely NSE single stocks on 1-minute bars.
- **[P] Practitioner or working paper:** a non-peer-reviewed source, or one whose authors
  promote the result. Independent replications may disagree.
- **[H] Hypothesis:** the specific rule used here is plausible given [E]/[P], but is untested. It
  must be validated on NSE data before anyone relies on it.

## 2. Data, timing and look-ahead control

- **Source.** PSYGRID Live Core `public/live.json`. It returns 989 stocks with today's completed
  1-minute OHLCV, `previous_close` and `today_open`. There is no bid/ask, depth or trade-by-trade
  data. The PSYGRID index feed is not used.
- **History.** The engines use the repository's own saved sessions, `data/sessions/*.json.gz`,
  written by 945.py and by this runner every 15 minutes. If present, they also use the optional
  Yahoo 5-minute file `data/history/yahoo_5m.json.gz` (`945.py --bootstrap`). Only days strictly
  before today are read.
- **Completed bars only.** A bar stamped `hh:mm` covers `hh:mm:00–hh:mm:59`, so it is used only
  once the clock has reached `hh:mm+1`. The client drops the forming bar. `quality.py` then
  rejects any bar where `ts + 1 min > scan minute` (status `INVALID_DATA`), so a future bar can
  never enter a scan. A replay cuts each saved session at the same rule (`series_inputs`).
- **Signal time.** This is the decision bar's start plus one minute, the moment its data existed.
  Scans run 8 seconds past each minute.
- **Per-stock validity** (`quality.py`). A stock that fails any check gets that status from
  every engine:

  | Status | Conditions |
  |---|---|
  | `INVALID_DATA` | The feed marked the stock unhealthy, or any bar is malformed (non-finite, ≤ 0, high < max(open, close), low > min(open, close)), off the minute grid, from another day, outside 09:15–15:29, incomplete or in the future, or duplicated or out of order. |
  | `INSUFFICIENT_DATA` | The 09:15 bar is missing (VWAP would be wrong). The newest bar closed more than 2 minutes ago (stale). Fewer than 90% of minutes since 09:15 are present. More than 3 minutes are missing in the last 30. |
  | `INELIGIBLE` | The last price is below ₹20, or the median turnover over the last 30 bars is below ₹2 lakh per minute. |

- **Universe accounting.** Every symbol in the universe gets exactly one evaluation from every
  engine; the universe is every symbol seen today. A symbol that disappears from the feed is
  reported as `INVALID_DATA: missing_from_feed`. The per-engine status counts always sum to the
  universe size, and the code asserts this. Missing prices are never filled in.
- **Feed-level failures** are reported, never silently treated as "no setup":
  - unreachable feed;
  - status not `OK` / `LIVE`;
  - candles not dated today;
  - fewer than 30% of the stocks received have usable data, counting illiquid ones as usable,
    or fewer than 100 liquid valid stocks. This check starts at 09:21; before then every stock
    has fewer than 5 bars by construction.

  Each of these is logged as `DATA FEED PROBLEM`. A Telegram notice goes out at most once every
  15 minutes and says that silence does not mean no opportunity.

## 3. Shared definitions

Bars are indexed `i = 0` (09:15) to `t` (the newest completed bar). Units are rupees and shares
unless stated otherwise.

| Symbol | Definition |
|---|---|
| `TR_i` | `max(H_i − L_i, |H_i − C_{i−1}|, |L_i − C_{i−1}|)`. For the first bar `TR_0 = H_0 − L_0`, so the overnight gap is excluded. |
| `ATR_t` | Simple mean of `TR` over the last 14 bars. It is undefined, and the engine reports `INSUFFICIENT_DATA`, before 14 bars exist. |
| `TP_i` | `(H_i + L_i + C_i)/3` |
| `VWAP_t` | `Σ_{i≤t} TP_i·V_i / Σ_{i≤t} V_i`. It is undefined while cumulative volume is 0. |
| `sd_t` | Volume-weighted s.d. of TP: `sqrt(Σ V_i·TP_i² / Σ V_i − VWAP_t²)` |
| `CLV` | `((C−L) − (H−C))/(H−L)`, in [−1, 1]. It is 0 when the range is zero. |
| body ratio | `|C−O|/(H−L)` |
| `med V(n, before k)` | Median volume of the n bars before index k |
| Market | Equal-weighted **median** of the liquid, valid universe, computed from the same bars: `ret_open` = median of `C_t/O_0915 − 1`; `breadth` = share of stocks with `C_t > VWAP_t`; market 1-minute return `m_k` = median of `C_k/C_{k−1} − 1` over stocks with both minutes (at least 50 stocks). The context is valid only with at least 100 liquid valid stocks, and with at least 30% of received stocks having usable data. |

**LONG/SHORT symmetry.** Each engine's rules are written once, in the LONG frame. The SHORT
engine runs the same code on a reflected view: `H′ = −L`, `L′ = −H`, `O′ = −O`, `C′ = −C`,
`VWAP′ = −VWAP`. Volume, TR, ATR and `sd` are unchanged. Tests confirm that a price-reflected
scenario gives the SHORT signal with the same score and reflected prices.

## 4. The score and what 90+ means

Each engine defines its own components `c_j`. Each has a weight `w_j` (with `Σ w_j = 1`, checked at
run time) and a quality `q_j ∈ [0, 1]`. Every quality is a piecewise-linear function of a
measured value:

- `ramp(x; a, b)`: 0 at `a` and 1 at `b`, clipped.
- `band`: a trapezoid that is 1 inside the ideal interval.

```
score = round(100 · Σ_j w_j q_j, 2)
Q90 rule: if score ≥ 90 and min_j q_j < 0.5, score := 89.99
SIGNAL ⇔ every mandatory gate passed ∧ risk/cost gates passed ∧ score ≥ 90 ∧ not a duplicate
```

So **90+ means**: every hard condition holds, the weighted shortfall from the ideal is at most
10%, and no single component is below half quality. A weighted mean could otherwise hide one
failed leg behind several perfect ones. Scores are comparable within an engine, not across
engines.

The anchors are design priors chosen before any results were seen. They are not fitted, and
fitting them to the same days you then evaluate would be curve fitting.

## 5. Trade plan, ticks and costs

- **Entry.** A stop-entry one tick beyond the decision bar's extreme (LONG: above `H_t`). It is
  rounded away from the market and is valid for 3 minutes (`valid_until`).
- **Stop and target.** Each engine defines its stop. The target is `entry ± 2R`, except for the
  Exhaustion engine, whose target is the VWAP, the mathematically defined mean.
- **Rounding.** All prices are rounded against the trader: a LONG entry rounds up, a LONG stop
  and target round down.
- **NSE tick sizes** (circular 33/2025, effective 15 April 2025, reviewed monthly from last
  month's close):

  | Price (₹) | Tick (₹) |
  |---|---|
  | below 250 | 0.01 |
  | 250 to 1,000 | 0.05 |
  | 1,000 to 5,000 | 0.10 |
  | 5,000 to 10,000 | 0.50 |
  | 10,000 to 20,000 | 1.00 |
  | 20,000 and above | 5.00 |

  The engine uses the coarser of the ticks at `price/1.1` and `price×1.1`. Each tick divides the
  next, so the result is valid whichever band the exchange assigned this month.
- **Risk gates.**
  - Risk must be between 0.15% and 2.0% of entry.
  - Round-trip cost must be at most 0.25R.
  - Costs use `edge/costs.py` on ₹1 lakh with 2 bps slippage per side, which is 0.122% round
    trip. Stocks with median turnover below ₹10 lakh per minute are costed at 5 bps per side.
    The slippage is an assumption, because there are no quotes.
  - In effect the stop must be at least about 0.49% away.
  - An explicit target must still pay at least 1.5R.
- **Time stop.** 15:15. No new signal starts after the 14:15 decision bar.

## 6a. Loss-avoidance safety gates (added after the first live session)

These gates came from the first live alerts on 2026-10-14 and an independent code review:
- **AARTIPHARM:** an Order-Flow LONG on a stock down 2.7% on the day.
- **ASTERDM:** a Compression SHORT whose risk was 90% chase.

They apply to every engine, in `core.Engine._evaluate`, after the strategy's own gates.
Every threshold is a judgment call, not a tested value. `s` is +1 for LONG and −1 for SHORT;
`day` = C/previous close − 1; `from_open` = C/09:15 open − 1; `market` is the universe median
change vs previous close.

| Gate | Rule | Engines |
|---|---|---|
| With the day | reject `s·day < −1%` unless `s·from_open ≥ +1%` (a real reversal from the open) | trend engines |
| With the market | reject `s·market < −0.5%` or `s·(breadth − 0.5) < −0.20` | trend engines |
| News / circuit | reject `|day| ≥ 8%` | trend engines |
| Fade limits | reject `|day| ≥ 4%` or `s·market < −0.5%` | fade engines (sweep, failed breakout, exhaustion) |
| Reachable target | reject if `|target − entry| > ATR·sqrt(M)`, where M is the minutes from the entry deadline to 15:15 | all |
| Last entry | no decision bar after 14:15 (ORB: 09:59, when its earliness score stops allowing 90+) | all |
| Thin stocks | 5 bps slippage below ₹10 lakh per minute median turnover | all |
| Lunch volume | every "× normal volume" baseline is at least 0.6 × the median volume since 09:30 | all |
| No chase | compression: expansion range ≤ 8× box TR, and entry ≤ 0.6R beyond the box edge | compression |
| Beat the market | order flow: the 10-bar window return minus the market's ≥ +0.3% | order flow |

Every alert now carries a context line, for example `context: day −2.70% vs prev close,
+0.40% vs open, market +0.35%, breadth 61%`. Fade alerts are labelled `counter-trend setup by
design`.

**Bug fixes from the same review:**
- The failed-breakout engine's session-low/high level was always skipped (dead code). It is
  now used.
- ORB's window was documented as running to 10:59, but its score could not reach 90 after
  about 10:00. The window is now 09:30–09:59.

## 6. The ten engines

The table under each engine is the exact score definition. It was generated from the code by
`tests/test_strategy_engines_engines.py` scenarios. "Textbook scenario" is the synthetic
positive case used in the tests.

### 1. `Order_Flow_Imbalance_Engine`: Order-Flow Imbalance + Price Confirmation

**Mechanism**
- **[E]** Cont, Kukanov & Stoikov (2014) find that price changes over short intervals are
  approximately linear in order-flow imbalance (OFI), with a slope that scales with 1/depth. The
  data were US equities with quote data.
- **[E]** Chordia, Roll & Subrahmanyam (2002) find that market returns respond to order
  imbalance.

**Data limitation:** true OFI needs best-quote queue changes, which PSYGRID does not provide.
The engine uses **Bulk Volume Classification** (Easley, López de Prado & O'Hara, 2016) as a
proxy. Its accuracy depends on bar size and is disputed (Andersen & Bondarenko). BVC infers the
sign from price, so it is not independent of price. The independent information is the volume
that weights it. For that reason the confirmation uses a different price feature (a break of the
range before the window) from the one the classifier uses.

**Inputs:** at least 41 bars. Decision bars run 09:55–14:15.

**Definitions**
- `σ` = sample s.d. of `ΔC` over the 30 bars *before* the 10-bar window (causal).
- `b_i = Φ(ΔC_i/σ)`
- `OI_10 = Σ V_i(2b_i − 1)/Σ V_i`
- `OI_3` = the same over the last 3 bars.
- `VR` = window volume / (10 × median V of the 30 bars before the window).
- `REF` = max H of the 20 bars before the window.

**Gates (LONG):** `OI_10 ≥ 0.30`; `OI_3 ≥ 0.20`; `VR ≥ 1.5`; `C_t > REF`; `C_t > VWAP_t`;
`C_t > C_{t−10}`; `CLV_t ≥ 0`; `(C_t − VWAP_t)/ATR ≤ 3`.

**Stop:** min L of the last 5 bars − 0.10 ATR.

**Failure modes:**
- BVC misclassification in choppy bars.
- Closing-auction and block prints.
- Flow absorbed by hidden liquidity; the proxy cannot see it.

**Regimes:** best in trending, high-participation sessions. In range-bound sessions the `REF`
break gate mostly blocks it.

### 2. `Liquidity_Sweep_Reclaim_Engine`: Liquidity Sweep + Reclaim

**Mechanism**
- **[E]** Osler (FRBNY Staff Report 125; 2003, 2005) finds that stop-loss orders cluster just
  beyond salient levels and, once triggered, cause price cascades.
- **[E]** Kavajecz & Odders-White (2004) find that support and resistance coincide with peaks in
  order-book depth.
- **[H]** A sweep that is absorbed and reclaimed within a few bars predicts a move away from the
  level.

**Levels** (all known before the sweep):
- PDL/PDH, the previous session low/high;
- ORL/ORH, from 09:15–09:29, usable from 09:30;
- confirmed fractal swing lows/highs (k = 3), at least 10 bars old and not traded through since
  they formed.

**Gates (LONG):**
- The sweep bar `s` is in `[t−3, t]`.
- Penetration `(Lvl − L_s)/ATR` is in `[0.05, 1.0]`.
- No lower low after `s`.
- At most **one** close more than 0.05 ATR beyond the level. This is the dividing line with
  engine 3.
- `t` is the first close back above the level.
- `V_s / med V(20) ≥ 1.5`.

**Stop:** `L_s − 0.10 ATR`.

**Failure modes:**
- The level gives way on the next attempt (a real breakdown).
- Sweeps on news.
- Stacked levels.

### 3. `Failed_Breakout_Breakdown_Engine`: Failed Breakout / Failed Breakdown

**Mechanism**
- **[E]** Osler (SR 125) finds that trends tend to reverse at predictable S/R levels.
- **[E]** Garzarelli et al. (2014) find bounce "memory" at S/R levels.
- **[H]** Breakout traders trapped by a failure add fuel to the reversal.

**Levels:**
- PDL/PDH and ORL/ORH;
- the session extreme before the break, only if the break comes at bar 30 or later.

**Gates** (LONG frame = failed *breakdown*; the SHORT signal is a failed breakout):
- The break starts at `b ∈ [t−12, t−2]`, with `C_{b−1} ≥ Lvl`.
- **At least 2 consecutive** closes below `Lvl − 0.05 ATR` (acceptance).
- Excursion `(Lvl − min L)/ATR ≤ 1.5`. Beyond that it is a real breakdown.
- `t` is the first close above `Lvl + 0.10 ATR`.
- `t − b ≤ 12`.

**Stop:** min L since `b` − 0.10 ATR.

**Failure modes:** a second break after the failure; trend days.

### 4. `Trend_Pullback_Continuation_Engine`: Trend Pullback + Continuation

**Mechanism**
- **[E]** Intraday momentum exists at the index level: Gao, Han, Li & Zhou (2018) on SPY;
  Baltussen et al. (2021) on more than 60 futures.
- **[E]** Short-horizon reversals come from temporary liquidity imbalances (Heston, Korajczyk &
  Sadka, 2010).
- **[H]** A low-volume, partial retracement inside a stock-level trend resolves in the trend's
  direction.

**Inputs:** at least 61 bars and the market context. Decision bars run 10:15–14:15.

**Gates (LONG):**
- **Trend:**
  - `EMA20 > EMA50`, and EMA50 is rising (vs 10 bars ago).
  - The OLS slope t-stat of the last 12 complete 5-minute closes is at least 2.5.
  - At least 80% of the last 60 closes are above VWAP.
- **Pullback:**
  - `SH` = the latest max H in the last 30 bars, at least 3 bars old.
  - `A` = min L of the 30 bars before `SH`.
  - Depth `(SH − PL)/(SH − A)` is in [0.236, 0.618].
  - `PL ≥ VWAP − 0.25 ATR`.
  - Pullback volume ≤ 0.9 × impulse volume.
  - `PL` was made within the last 6 bars.
- **Trigger:** a fresh close above the 3-bar high, with `CLV ≥ 0.3`.
- **Market:** `ret_open ≥ −0.30%`.

**Stop:** `PL − 0.10 ATR`.

**Failure modes:**
- Late-trend exhaustion.
- Lunchtime chop.
- The OLS t-stat is inflated by autocorrelated residuals. It is used as a strength ordering, not
  a significance test.

### 5. `Relative_Strength_Weakness_Engine`: Relative-Strength Leader / Relative-Weakness Laggard

**Mechanism**
- **[E]** Cross-sectional momentum is well documented at monthly horizons (Jegadeesh & Titman,
  1993).
- **[E]** Intraday, Heston, Korajczyk & Sadka (2010) find continuation only at lags that are
  multiples of one day. At lags under one hour they find **reversal**.
- **[H]** Persistent, beta-adjusted intraday leadership plus a fresh breakout continues. This is
  the weakest-evidenced engine, and its rules are deliberately strict.

**Beta and RS**
- `β̂` = OLS beta of the stock's 1-minute returns on the market's `m_k` over the session
  (n ≥ 30).
- `β = clamp(1/3 + 2/3·β̂, 0, 3)`. This is Blume-style shrinkage toward 1.
- `RS_open = (C_t/C_0915close − 1) − β·M(09:15→t)`
- `RS_60` is the same over the last 60 bars.
- Both are ranked cross-sectionally into percentiles over at least 100 stocks.

**Gates (LONG leader):**
- `P_open ≥ 0.95` and `P_60 ≥ 0.80`.
- At least 4 of the last six 10-bar blocks show positive excess return.
- The stock's own return is positive.
- `C_t > VWAP` and `z = (C_t − VWAP)/sd ≤ 3.0`. A perfectly linear trend sits at `z = √3 ≈ 1.73`,
  so q is full up to 1.75.
- A fresh 15-bar high. Decision bars run 10:15–14:15.

**Stop:** min L of the last 15 bars − 0.10 ATR.

**Failure modes:**
- Short-horizon reversal, which has evidence behind it.
- News-driven leaders.
- Index rebalancing days.

### 6. `Volatility_Compression_Expansion_Engine`: Volatility Compression → Confirmed Expansion

**Mechanism**
- **[E]** Volatility clusters and mean-reverts (Engle, 1982; Bollerslev, 1986). Quiet periods
  predict a return of volatility (its magnitude), not its direction.
- **[E]** The Parkinson (1980) range estimator is about 5× as efficient as close-to-close under
  zero drift.
- **[H]** The direction of a confirmed expansion persists.

**Definitions**
- `σ_P = sqrt(mean ln(H/L)² / (4 ln 2))`
- Box = 15 bars `[e−15, e−1]`, starting no earlier than 09:30.
- `CR = σ_P(box) / median σ_P(every 15-bar window from 09:30 to the box)`, with at least 15
  windows.

**Gates (LONG):**
- `CR ≤ 0.60`.
- The expansion bar `e = t−1` satisfies all of:
  - `C_e ≥ hi + 0.10W`;
  - `TR_e ≥ 2 × mean box TR`;
  - `V_e ≥ 2 × median box V`;
  - body ≥ 0.55 and `CLV ≥ 0.3`.
- The confirmation bar `t` closes above `hi` with `L_t ≥ hi − 0.25W`.

**Stop:** box midpoint. **Entry:** above `max(H_e, H_t)`.

**Limitation:** intraday volatility is U-shaped, so a midday box is partly *seasonally* quiet.
The baseline excludes the first 15 minutes but is not slot-adjusted. Distinct from `flat.py`,
which requires a flat day relative to the previous close.

### 7. `Regime_Filtered_ORB_Engine`: Regime-Filtered Opening-Range Breakout

**Mechanism**
- **[E]** Holmberg, Lönnbark & Lundström (2013) find ORB profitable on crude-oil futures.
- **[P]** Zarattini, Barbon & Aziz (2024, SSRN/SFI) report that a 5-minute ORB works mainly on
  "stocks in play". These are stocks with abnormal opening volume, measured against a 14-day
  baseline, traded in the direction of the opening candle and exited at the close. The results
  are on US stocks, with headline numbers from the authors.
- **[H]** The regime filters (market direction and breadth).

**Opening range:** 09:15–09:29. All 15 bars are required.

**Gates (LONG):**
- **Stocks in play:** `RVOL = OR volume / median(09:15–09:29 volume, at least 5 prior sessions)
  ≥ 1.5`.
  - Without that history, the fallback is a cross-sectional opening-turnover percentile of at
    least 0.80. The proxy can earn at most half of the 0.25 in-play weight, so **without history
    the engine cannot reach 90 (maximum 87.5)**. This is deliberate.
- The OR close is above the OR open.
- `t` is the first close beyond ORH, and the opposite side has not been broken first.
- `ret_open ≥ 0` and breadth ≥ 0.5.
- Decision bars run 09:30–09:59.

**Stop:** ORL, the literature's opposite-side stop. **Target:** 2R. **Time exit:** 15:15.

**Volume scale:** RVOL from Yahoo history may differ in scale from PSYGRID volume. The source is
named in the alert.

### 8. `VWAP_Reclaim_Rejection_Engine`: VWAP Reclaim / Rejection + Retest

**Mechanism**
- **[E]** VWAP is the dominant institutional execution benchmark (Berkowitz, Logue & Noser,
  1988).
- **[P]** Zarattini & Aziz (2023) report a VWAP trend rule on QQQ; replications disagree on its
  size.
- **[H]** A reclaim followed by a held retest continues.

**Gates (LONG reclaim; SHORT = rejection):**
- The cross bar `c ∈ [t−12, t−2]` closes above VWAP after closing at or below it on the previous
  bar.
- At least 10 of the 15 prior closes were below VWAP.
- Cross body ≥ 0.5 and cross volume ≥ 1.2× the median.
- No close below `VWAP − 0.10 ATR` after the cross.
- A retest bar `r` touches `VWAP + 0.15 ATR` and closes at or above VWAP.
- `t` is the first close above `H_r`, with `t − r ≤ 5`.

**Score note:** the VWAP "turn" is measured since the cross. While price sits below VWAP the
average must fall, so a pre-cross slope would penalise every reclaim.

**Stop:** min L since `r` − 0.10 ATR.

### 9. `Exhaustion_Mean_Reversion_Engine`: Exhaustion Reversal + Mean-Reversion Confirmation

**Mechanism**
- **[E]** Campbell, Grossman & Wang (1993) find that high-volume price moves tend to reverse.
- **[E]** Grant, Wolf & Yu (2005) find intraday reversals after large opening moves in US index
  futures, but much of the significance disappears after transaction costs.
- **[E]** Heston, Korajczyk & Sadka (2010) find reversals at lags under one hour.
- **[H]** These carry over to NSE single stocks, net of costs.

**Gates (LONG = fade a selling climax):**
- The climax bar `x ∈ [t−5, t−1]` is the lowest low of `[x−30, t]`.
- Stretch `z = (VWAP_x − L_x)/sd_x ≥ 2.5`.
- The run into the climax is at least 3 ATR.
- Climax volume ≥ 3× the median of the 30 bars before it.
- Exhaustion shape: lower wick ≥ 0.35, or `RSI14 ≤ 20`.
- `t` is the first close above `H_x`.

**Target:** `VWAP_t`, which must be at least 1.5R away. **Stop:** `L_x − 0.10 ATR`.

**Failure mode:** trend days. These are the reason the engine never fades without climax
volume and a confirmation close.

### 10. `Structural_Break_Retest_Engine`: Break-and-Retest of a Predefined Structural Level

**Mechanism**
- **[E]** Kavajecz & Odders-White (2004) find depth peaks at S/R levels.
- **[E]** Osler finds momentum after S/R levels are crossed, as stops trigger.
- **[P]** Old resistance becomes support ("polarity").
- **[H]** The retest entry carries a net edge.

**Levels:** only **predefined** levels are used: PDH/PDL/PDC from the saved previous session,
and ORH/ORL fixed at 09:30.

**Gates (LONG):**
- **Break bar `b`** is in `[t−20, t−3]`:
  - the 10 closes before it are all at or below the level;
  - `C_b ≥ Lvl + 0.15 ATR`;
  - `V_b ≥ 1.5×` the median.
- No close below `Lvl − 0.10 ATR` afterwards. That case would be engine 3's.
- **Departure:** price moved at least 0.30 ATR above the level.
- **Retest:** a bar `r` touches `Lvl + 0.15 ATR` and holds.
- `t` is the first close above `H_r`, with `t − r ≤ 5`.

**Stop:** min L since `r` − 0.10 ATR. Room to the next predefined level is scored.

**Without saved sessions** only PDC (from the feed's previous close) and ORH/ORL exist.

### Score definitions (generated from the code)

#### Order_Flow_Imbalance_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `imbalance` | 0.25 | OI10 = sum V(2b-1)/sum V, 0.30->0, 0.60->1 |
| `persistence` | 0.15 | share of window bars with flow in trade direction, 0.5->0, 0.8->1 |
| `participation` | 0.20 | window volume / (10 x median), 1.5->0, 3->1 |
| `price_efficiency` | 0.15 | Kaufman ER of window closes, 0.3->0, 0.7->1 |
| `breakout_clearance` | 0.10 | (C - REF)/ATR, 0->0, 0.5->1 |
| `not_extended` | 0.15 | (C - VWAP)/ATR, 3->0, <=1->1 |

Textbook test scenario: score 99.56, entry 101.14, stop 100.22, target 102.98

#### Liquidity_Sweep_Reclaim_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `level_significance` | 0.15 | PDL/PDH 1.0, opening range 0.9, retested swing 0.85, swing 0.6 |
| `penetration` | 0.15 | (level - sweep low)/ATR: full in [0.10, 0.50], 0 at 0 and 1.0 |
| `sweep_volume` | 0.20 | sweep bar V / median(20), 1.5->0, 3.5->1 |
| `rejection_wick` | 0.15 | sweep-bar rejection wick / range, 0.3->0, 0.6->1 |
| `reclaim_speed` | 0.10 | bars from sweep to reclaim: 0->1.0, 3->0.4 |
| `reclaim_strength` | 0.15 | (C - level)/ATR, 0->0, 0.4->1 |
| `reclaim_close` | 0.10 | reclaim bar close location, 0->0, 0.6->1 |

Textbook test scenario: score 96.99, entry 99.89, stop 99.3, target 101.07

#### Failed_Breakout_Breakdown_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `acceptance` | 0.15 | closes accepted beyond the level: 2->0.5, >=3->1 |
| `small_extension` | 0.15 | max excursion beyond level /ATR, 1.5->0, <=0.3->1 |
| `failure_depth` | 0.20 | close back inside /ATR, 0.1->0, 0.5->1 |
| `weak_break_volume` | 0.10 | breakout-bar volume ratio, 2.0->0, <=0.8->1 |
| `failure_volume` | 0.15 | failure-bar volume ratio, 0.8->0, 2.0->1 |
| `speed` | 0.10 | bars from break to failure, 12->0, <=3->1 |
| `level_significance` | 0.15 | PDH/PDL 1.0, opening range 0.9, day extreme 0.8 |

Textbook test scenario: score 95.03, entry 99.81, stop 99.2, target 101.03

#### Trend_Pullback_Continuation_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `trend_t` | 0.20 | 5-min OLS slope t, 2.5->0, 6->1 |
| `vwap_hold` | 0.10 | share of 60 closes beyond VWAP, 0.80->0, 0.95->1 |
| `depth` | 0.15 | retracement: full 0.382-0.50 |
| `pullback_dryness` | 0.15 | pullback/impulse mean volume, 0.9->0, 0.5->1 |
| `trigger_volume` | 0.10 | trigger V / median(20), 0.8->0, 1.5->1 |
| `trigger_close` | 0.10 | trigger close location, 0.3->0, 0.8->1 |
| `efficiency` | 0.10 | 60-bar Kaufman ER, 0.15->0, 0.40->1 |
| `market_alignment` | 0.10 | market return since open in trade direction |

Textbook test scenario: score 97.62, entry 102.57, stop 101.92, target 103.87

#### Relative_Strength_Weakness_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `session_rs_rank` | 0.20 | beta-adj RS since open percentile, 0.95->0, 0.99->1 |
| `hour_rs_rank` | 0.15 | beta-adj RS last 60 min percentile, 0.80->0, 0.95->1 |
| `consistency` | 0.15 | 10-min blocks with excess return in direction: 3->0, 6->1 |
| `absolute_move` | 0.15 | own return since 09:15 close, 0->0, 1%->1 |
| `not_stretched` | 0.10 | (C - VWAP)/VWAP s.d., 3->0, <=1.75->1 |
| `participation` | 0.15 | mean V last 30 / session median V, 1->0, 2->1 |
| `breakout_close` | 0.10 | breakout bar close location, 0.2->0, 0.7->1 |

Textbook test scenario: score 91.67, entry 103.36, stop 102.49, target 105.1

#### Volatility_Compression_Expansion_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `compression` | 0.20 | Parkinson box/baseline, 0.60->0, <=0.35->1 |
| `range_expansion` | 0.15 | TR_e / mean box TR, 2->0, 4->1 |
| `volume_expansion` | 0.20 | V_e / median box V, 2->0, 5->1 |
| `body` | 0.10 | expansion body/range, 0.55->0, 0.85->1 |
| `clearance` | 0.10 | (C_e - box edge)/W, 0.1->0, 0.5->1 |
| `confirmation` | 0.15 | (C_t - box edge)/W, 0->0, 0.5->1 |
| `vwap_side` | 0.10 | (C_t - VWAP)/ATR in direction, -0.5->0, 0.5->1 |

Textbook test scenario: score 99.45, entry 100.59, stop 100.08, target 101.61

#### Regime_Filtered_ORB_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `in_play` | 0.25 | RVOL 1.5->0, 3->1 (turnover-percentile proxy capped at 0.5) |
| `market_regime` | 0.15 | market return since open in direction, 0->0, 0.5%->1 |
| `breadth` | 0.10 | share above VWAP minus 0.5 in direction, 0->0, 0.15->1 |
| `opening_drive` | 0.10 | |OR close - OR open| / OR range, 0.2->0, 0.6->1 |
| `breakout_volume` | 0.15 | breakout V / mean OR-bar V, 0.5->0, 1.5->1 |
| `clearance` | 0.10 | (C - OR edge)/ATR, 0->0, 0.3->1 |
| `earliness` | 0.15 | minutes after 09:30, 60->0, 0->1 |

Textbook test scenario: score 93.01, entry 100.87, stop 99.95, target 102.71

#### VWAP_Reclaim_Rejection_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `prior_regime` | 0.10 | closes on the other side in 15 before cross, 10->0, 15->1 |
| `cross_volume` | 0.15 | cross V / median(20), 1.2->0, 2.5->1 |
| `cross_body` | 0.10 | cross body/range, 0.5->0, 0.8->1 |
| `retest_precision` | 0.15 | (retest extreme - VWAP)/ATR: full in [-0.10, 0.05] |
| `retest_dryness` | 0.15 | retest V / cross V, 1->0, 0.5->1 |
| `trigger_volume` | 0.10 | trigger V / median(20), 0.8->0, 1.5->1 |
| `vwap_turn` | 0.10 | (VWAP_t - VWAP_cross)/ATR in direction: -0.15->0, flat or turned->1 |
| `entry_near_vwap` | 0.15 | (entry - VWAP)/ATR, 1.5->0, <=0.5->1 |

Textbook test scenario: score 95.48, entry 101.51, stop 100.94, target 102.65

#### Exhaustion_Mean_Reversion_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `stretch` | 0.20 | (VWAP - extreme)/VWAP s.d., 2.5->0, 3.5->1 |
| `climax_volume` | 0.20 | climax V / median(30), 3->0, 6->1 |
| `run` | 0.10 | move into the extreme /ATR, 3->0, 6->1 |
| `rejection_wick` | 0.10 | climax-bar rejection wick/range, 0.2->0, 0.6->1 |
| `rsi_extreme` | 0.10 | RSI14 at the extreme (direction-normalised), 30->0, 15->1 |
| `confirmation` | 0.15 | (C_t - climax bar far edge)/ATR, 0->0, 0.4->1 |
| `reward_to_mean` | 0.15 | (VWAP - entry)/risk, 1.5->0, 3->1 |

Textbook test scenario: score 94.22, entry 96.59, stop 95.39, target 99.67

#### Structural_Break_Retest_Engine

| Component | Weight | Quality q (piecewise linear) |
|---|---:|---|
| `level_significance` | 0.15 | PDH/PDL 1.0, PDC 0.85, opening range 0.8 |
| `prior_respect` | 0.10 | bars testing the level before the break, 0->0, 3->1 |
| `break_volume` | 0.15 | break V / median(20), 1.5->0, 3->1 |
| `break_clearance` | 0.10 | (C_b - level)/ATR, 0.15->0, 0.6->1 |
| `retest_precision` | 0.15 | (retest low - level)/ATR: full in [-0.05, 0.10] |
| `retest_dryness` | 0.10 | retest V / break V, 1->0, 0.4->1 |
| `trigger_volume` | 0.10 | trigger V / median(20), 0.8->0, 1.5->1 |
| `room` | 0.15 | next predefined level beyond entry in R, 1->0, >=2 (or none)->1 |

Textbook test scenario: score 100.00, entry 101.71, stop 100.96, target 103.21

## 7. Duplicates, lifecycle and Telegram

**What makes a signal new.** It must pass all four checks:
1. The key `engine|symbol|direction|anchor` has not been seen today. The anchor is the
   engine-defined identity of the setup: the swept level and sweep bar, the break bar, the ORB
   date, and so on.
2. No signal for the same engine, symbol and direction is still `PENDING` or `TRIGGERED`. A
   continuing condition is not a new signal.
3. The engine's cooldown has passed: 15 minutes for the sweep engine, 60 for RS, one day for
   ORB, 30 for the others.
4. No engine has alerted the same stock in the last 30 minutes. An opposite-direction alert
   inside that window is a conflict and is suppressed.

**Daily risk limits** (`strategy_engines.Governor`). These are judgment values. Signals are
taken highest score first.
- At most 2 new alerts per minute.
- At most 3 open (`PENDING` or `TRIGGERED`) trades in one direction.
- Alerts stop for the day once realised net R ≤ −3, or after 3 losing trades in a row. One
  Telegram notice says so.

Closed trades record `r_multiple` (gross, at modelled prices) and `r_net`
(`r_multiple − cost_r`). Summaries and replays report the net figure. The ledger
(`data/engines/DAY/ledger.json`) is written atomically and survives restarts. A corrupt ledger
is moved aside, with a warning.

**Lifecycle.** Only bars that start at or after the signal time count.

| From | To | When (LONG) |
|---|---|---|
| `PENDING` | `TRIGGERED` | High ≥ entry. A gap fills at the open. |
| `PENDING` | `INVALIDATED` | The stop trades before entry. |
| `PENDING` | `EXPIRED` | 3 minutes pass without a trigger. |
| `TRIGGERED` | `STOPPED` | The stop trades. |
| `TRIGGERED` | `TARGET` | The target trades. |
| `TRIGGERED` | `TIME_EXIT` | The 15:15 bar is reached. |

If one bar touches both the stop and the target, the stop is assumed.

**Telegram** (`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment). HTML-escaped
alerts contain:
- symbol, engine, strategy, direction and score;
- entry trigger with its cancel time, stop, target and R, risk %, and cost in R;
- signal time and decision candle;
- the conditions responsible, and the main score drivers.

Delivery rules:
- At most about one message per 1.1 s.
- A 429 response's `retry_after` is honoured.
- 5xx and network errors are retried with backoff. 4xx errors are not retried.
- The token is redacted from every logged error.
- An unsent alert is retried each minute **only while its entry is still valid**. A stale alert
  is never sent late.

There is also a daily summary, and data-feed problem notices.

## 8. Daily operation

```
python strategy_engines.py                 # whole trading day; waits for 09:16
python strategy_engines.py --once          # one scan now
python strategy_engines.py --replay data/sessions [--verbose]   # deterministic replay of saved days
python strategy_engines.py --telegram-test
python strategy_engines.py --list
python strategy_engines.py --self-test
```

**Automatic start**
- **Linux VM:** `deploy/systemd/psygrid-engines.{service,timer}`, Mon–Fri 09:05 IST, installed
  by `deploy/systemd/install.sh`. Put the Telegram variables in `/etc/psygrid-engines.env`
  (mode 0600).
- **Windows:** `deploy/windows/install_engines_task.ps1`.

**Daily flow**
1. Check the trading day against `run_engine.NSE_HOLIDAYS_2026`.
2. At the first scan that carries today's candles, load prior-day levels from saved sessions.
   Only then does the feed's `previous_close` used in the cross-check refer to the previous
   session.
3. Scan every minute from 09:16 to 15:16.
4. Archive the session every 15 minutes; it is tomorrow's PDH/PDL/PDC/RVOL input.
5. Send the summary.

Journals go to `data/engines/DAY/scans.jsonl`: per-scan status counts for every engine, quality
reasons, the market context, engine errors and timings.

**Performance:** a 989-stock synthetic universe took 2–9 s per scan in this container. Scans
take longer later in the day.

## 9. Validation performed

All of these suites ran and passed in this repository's container; run
`python strategy_engines.py --self-test` to repeat them.

- **`tests/test_strategy_engines_math.py`** checks the mathematics against independent hand
  values:
  - TR, VWAP and the volume-weighted s.d.;
  - EMA, Parkinson, BVC (Φ), sample s.d. and the efficiency ratio;
  - Wilder RSI on the StockCharts worked example (70.464, against StockCharts' rounded 70.53);
  - the OLS slope t-stat;
  - fractal pivots that never use unconfirmed bars;
  - ramp/band, the weighted score and the Q90 cap;
  - NSE tick bands, safe tick and directional rounding;
  - the round-trip cost and the trade gates;
  - exact Side reflection and complete 5-minute buckets.
- **`tests/test_strategy_engines_engines.py`** tests the engines:
  - each engine signals ≥ 90 on its textbook LONG scenario;
  - the price-reflected scenario gives the SHORT signal with the same score (within 1 point
    where Parkinson or beta break exact reflection);
  - **each scenario fires only its own engine** (a 10 × 10 distinctness matrix);
  - key quantities are recomputed independently: the OI from Φ, sweep penetration and volume,
    acceptance counts, pullback depth and volume, beta and beta-adjusted RS, the compression
    ratio from an independent Parkinson, RVOL and the ORL stop, the VWAP retest precision, and
    the exhaustion stretch with its VWAP target;
  - gate mutations give `NO_SETUP`;
  - window and insufficient-context cases are covered;
  - the ORB proxy cannot reach 90;
  - future bars never change a decision;
  - identical inputs in any order give identical outputs.
- **`tests/test_strategy_engines_system.py`** tests the system:
  - **Accounting:** a 989-symbol universe that includes malformed, duplicated, future, stale,
    no-09:15, illiquid, penny, unhealthy and missing stocks. Every engine accounts for all 989,
    with the right status for each.
  - **Fault isolation:** an engine raising in `prepare` or `side` leaves the other engines
    byte-identical.
  - **Integrity:** an engine mutating shared features is detected.
  - **Ledger:** duplicate, continuing-condition and cooldown rules; every lifecycle path,
    including the same-bar stop assumption, gap fills and the 15:15 exit; restart persistence;
    a corrupt ledger.
  - **Telegram:** not configured, HTML, 429 `retry_after`, no retry on 4xx, token redaction,
    network retries, and an alert content and escaping check.
  - **History:** previous trading day across weekends and holidays; the complete-session,
    PDC-mismatch and no-mixed-volume-source rules; archive → next-day levels round trip.
  - **Daily runner** on a simulated clock: one alert across repeated scans, the summary and the
    archive; feed down → reported and rate-limited; a feed that is not live gives no signals;
    waiting for 09:16.
  - **Replay:** a saved session file.

## 10. Known limitations and what remains unverified

- **No empirical validation on NSE.**
  - No profitability, win rate or expectancy is claimed for any engine.
  - The container cannot reach the PSYGRID feed, and the repository holds no saved sessions.
    No live scan and no real-data replay has been run.
  - Before trusting any engine, replay at least several weeks of saved sessions with
    `--replay`. Judge each engine on 50+ closed trades per engine, out of sample, net of costs.
- **Thresholds are priors.** Grades [H] and [P] mean exactly what they say.
- **Order-flow proxy.** Engine 1 measures volume-weighted directional pressure from OHLCV, not
  true OFI.
- **Market proxy.** The market is the universe median, not the NIFTY index.
- **Holidays.** The holiday calendar covers 2026 only (`run_engine.NSE_HOLIDAYS_2026`). From 2027
  the feed's LIVE status and today's-date check are the only guards until it is extended.
- **Ticks.** Tick bands change by circular; verify them periodically.
- **Lifecycle fills.** Fills are modelled at trigger, stop and target prices. Real fills and
  slippage will differ.
- **Engine 6.** Volatility seasonality is not slot-adjusted.
- **Volume scales.** Yahoo-based RVOL may differ in scale from PSYGRID volume.

## 11. References

- Andersen, T. G., & Bondarenko, O. (2014). Reflecting on the VPIN dispute. *Journal of
  Financial Markets*.
- Baltussen, G., Da, Z., Lammers, S., & Martens, M. (2021). Hedging demand and market intraday
  momentum. *Journal of Financial Economics*.
- Berkowitz, S., Logue, D., & Noser, E. (1988). The total cost of transactions on the NYSE.
  *Journal of Finance*.
- Blume, M. (1975). Betas and their regression tendencies. *Journal of Finance*.
- Bollerslev, T. (1986). Generalized autoregressive conditional heteroskedasticity. *Journal of
  Econometrics*.
- Campbell, J., Grossman, S., & Wang, J. (1993). Trading volume and serial correlation in stock
  returns. *Quarterly Journal of Economics*, 108(4), 905–939.
- Chordia, T., Roll, R., & Subrahmanyam, A. (2002). Order imbalance, liquidity, and market
  returns. *Journal of Financial Economics*, 65(1), 111–130.
- Cont, R., Kukanov, A., & Stoikov, S. (2014). The price impact of order book events. *Journal of
  Financial Econometrics*, 12(1), 47–88.
- Easley, D., López de Prado, M., & O'Hara, M. (2016). Discerning information from trade data.
  *Journal of Financial Economics*.
- Engle, R. (1982). Autoregressive conditional heteroscedasticity. *Econometrica*.
- Gao, L., Han, Y., Li, S. Z., & Zhou, G. (2018). Market intraday momentum. *Journal of Financial
  Economics*, 129, 394–414.
- Garzarelli, F., Cristelli, M., Pompa, G., Zaccaria, A., & Pietronero, L. (2014). Memory effects
  in stock price dynamics: evidences of technical trading. *Scientific Reports*.
- Grant, J., Wolf, A., & Yu, S. (2005). Intraday price reversals in the US stock index futures
  market: a 15-year study. *Journal of Banking & Finance*, 29(5), 1311–1327.
- Heston, S., Korajczyk, R., & Sadka, R. (2010). Intraday patterns in the cross-section of stock
  returns. *Journal of Finance*, 65(4), 1369–1407.
- Holmberg, U., Lönnbark, C., & Lundström, C. (2013). Assessing the profitability of intraday
  opening range breakout strategies. *Finance Research Letters*, 10(1), 27–33.
- Jegadeesh, N., & Titman, S. (1993). Returns to buying winners and selling losers. *Journal of
  Finance*.
- Kavajecz, K., & Odders-White, E. (2004). Technical analysis and liquidity provision. *Review of
  Financial Studies*, 17(4), 1043–1071.
- NSE circular 33/2025 (13 March 2025): revision of tick sizes, effective 15 April 2025.
- Osler, C. (2001/2003). Currency orders and exchange-rate dynamics: an explanation for the
  predictive success of technical analysis. FRBNY Staff Report 125; *Journal of Finance* 2003.
- Parkinson, M. (1980). The extreme value method for estimating the variance of the rate of
  return. *Journal of Business*, 53(1), 61–65.
- Telegram Bot API FAQ: about one message per second per chat, about 30 per second in bulk, and
  `retry_after` on 429 responses.
- Zarattini, C., & Aziz, A. (2023). Volume Weighted Average Price (VWAP): the holy grail for day
  trading systems. SSRN 4631351 (working paper).
- Zarattini, C., Barbon, A., & Aziz, A. (2024). A profitable day trading strategy for the U.S.
  equity market. SSRN / Swiss Finance Institute (working paper).
