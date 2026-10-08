# SENSEX EXPIRY — ZERO→HERO ENGINE v1.0
### Research, architecture and locked constitution

Status on 2026-10-08: **research build. No real-data backtest has been run, so no edge has been shown.**
The reference implementation is in `sensex_expiry/`, with 89 tests under `tests/test_sensex_expiry_*.py`.
Live orders stay blocked by `sensex_expiry/validation_gate.py` until the evidence gates in §36 and §39 pass.

Every claim below carries one of these labels:

| Label | Meaning |
|---|---|
| **FACT** | Verified from an exchange, regulator, broker or official SDK source, or true by definition. The source and date are given. |
| **FACT (verify)** | Taken from reputable secondary sources. Confirm against the primary source before going live. |
| **HYPOTHESIS** | A claim this system would profit from. It is untested and must pass §27–§30. |
| **ASSUMPTION** | A modelling choice made without evidence, such as a cost or fill model. It is stress-tested in §31. |
| **SYNTHETIC RESULT** | Output of the engine on simulated random-walk data. It checks the machinery only and says nothing about markets. |
| **BACKTEST RESULT** | Output on real SENSEX data. **There are none yet.** |

---

## 1. Executive Summary

**Verdict first.** The concept as phrased, "zero→hero" option buying on expiry day, has **negative expected value by default**. A narrower, testable version exists. This document specifies that version and also the machinery that could prove it worthless.

1. **FACT.** Option buyers in aggregate lose money. Index options worldwide carry a variance risk premium: implied volatility on average exceeds the volatility that follows. Buyers pay that premium. This is documented across decades of index-option research (Bakshi & Kapadia 2003; Carr & Wu 2009). SEBI's studies of the Indian F&O segment report that about 9 in 10 individual traders lost money: 93% over FY22–FY24 (study published Sept 2024) and about 91% in FY25 (July 2025). *(FACT (verify): exact figures from SEBI's published studies.)*
2. **Consequence.** Buying expiry-day options at random loses roughly the premium spread plus the variance premium plus costs. An edge can only come from **conditional timing**: entering on a few measurable states where the move that follows is larger than the option has priced in. Whether any such state persists for SENSEX is a **HYPOTHESIS**.
3. **The dominant statistical problem is sample size.** **FACT:** SENSEX weekly options were relaunched in May 2023. Through Oct 2026 that gives about 170 weekly expiries, spread across three expiry weekdays: Friday, then Tuesday, then Thursday (§2). **SYNTHETIC RESULT:** on 150 random-walk expiry days the v1 rules fired 21 trades, about one per seven expiry days. The 95% confidence interval of the mean was **[−0.57R, +0.82R]**. To detect a true +0.2R edge with a per-trade standard deviation of about 1.8R at 95% confidence you need about (1.96·1.8/0.2)² ≈ **310 trades**. The 1.8R standard deviation is an ASSUMPTION typical of trailed option-buying R distributions. Expiry days alone will not supply that within several years. The research design therefore **pools** evidence (§27): the underlying signal is tested on every trading day, and the expiry-specific option payoff is tested on expiry days.
4. **What was removed after challenge:**
   - **VWAP.** The SENSEX index has no traded volume, so its VWAP cannot exist. "SENSEX VWAP" lines on charts come from futures or are artefacts (§12).
   - **Option-chain direction signals** (OI walls, PCR, max pain). Nothing showed predictive value, they are confounded by option writers' hedging, and they are cheap to overfit. They are logged for research but do not drive decisions (§11).
   - **The weighted 0–100 score as a gate.** Fitting weights on fewer than 100 trades is curve-fitting. The score is logged, not used to decide (§20).
   - **Deep-OTM "lottery" strikes.** These carry the worst variance premium and the widest relative spreads (§15).
   - **Martingale, averaging down and scaling on confidence.** Forbidden by construction (§22).
5. **What remains:** three pre-registered setups, each a HYPOTHESIS (§15):
   - **S1 Sweep & Reclaim** of a reference level.
   - **S2 Opening-Range Acceptance + Retest.**
   - **S3 Late Compression → Expansion.**

   All three are expressed by **buying a near-ATM weekly option**. They use mechanical close-based triggers, a structural invalidation on the underlying, an exchange-resident premium stop, and a trailing exit with no fixed target by default.
6. **Asymmetry comes from the instrument and the exit, not from risk.** A long option cannot lose more than its premium. The system caps premium outlay at **3× the per-trade risk budget**. So even total failure (no stop fill, a dead engine, a lost API) loses at most 3R, and a normal stop loses about 1R. Winners are allowed to run under a trail.
7. **The default action is NO_TRADE.** The engine trades only on expiry days, only inside its windows, only with GOOD data, and only when every gate passes. On 150 random-walk expiry days (about 56,000 bar-level decisions) it entered **21** times (SYNTHETIC RESULT).
8. **Next step:** Phase 1–2 of the roadmap (§40). Pull SENSEX 1-minute index history and Dhan's rolling expired-option data. Run `python -m sensex_expiry --walk-forward`. **Accept the result whatever it is.** If the out-of-sample gates in §36 fail, the conclusion is "no demonstrated edge", and the engine stays a paper tool.

---

## 2. Current SENSEX / BSE Derivatives Environment

Researched on 2026-10-08. `dhanhq.co` and `bseindia.com` were not reachable from the research environment, so exchange items are sourced from reputable secondary sources and flagged **verify**.

| Item | Value | Label / source |
|---|---|---|
| Underlying | S&P BSE SENSEX (30 stocks), a price index with no traded volume | FACT |
| Exchange / segment | BSE equity derivatives. Dhan segments: `IDX_I` for the index, `BSE_FNO` for options | FACT (dhanhq SDK 2.2.0 constants) |
| Trading hours | 09:15–15:30 IST, Mon–Fri except exchange holidays | FACT (long-standing BSE/NSE equity-derivatives session) |
| Weekly expiry day | **Thursday**, effective 1 Sep 2025. A holiday moves it to the previous trading day (example: Thu 28 May 2026 holiday → expiry Wed 27 May 2026) | FACT (verify): Business Standard; share.market (Jun 2026); AlgoTest (Aug 2026) |
| Expiry history | Weeklies relaunched May 2023 with **Friday** expiry. **Tuesday** from 7 Jan 2025. **Thursday** from 1 Sep 2025, after SEBI's May 2025 rule that equity derivatives expire only on Tuesday or Thursday, with NSE on Tuesday and BSE on Thursday | FACT (verify), same sources |
| Weekly vs monthly | One weekly benchmark per exchange (SEBI, Nov 2024): BSE kept SENSEX weeklies only, NSE kept Nifty 50 only. Monthly expiry = last Thursday-convention expiry of the month | FACT (verify) |
| Lot size | **20** (weekly and monthly; contracts checked Sep 2026). Raised from 10 in late 2024 | FACT (verify): AlgoTest 28 Sep 2026; Kotak Neo. **The engine reads lot size from Dhan's scrip master at runtime and never hard-codes it** |
| Strike interval | 100 index points near the money | FACT (verify): Samco/Bigul contract-spec summaries |
| Tick size | ₹0.05 | FACT (verify) |
| Settlement | Cash-settled, European. Final settlement price = SENSEX closing value on expiry day (BSE closing-price methodology; verify whether last-30-minute weighted) | FACT (verify) |
| STT (options) | **0.15% of premium on the sell side** from 1 Apr 2026 (was 0.10%). **0.15% of settlement value** if exercised (was 0.125%) | FACT (verify): Union Budget 2026; Zerodha, ICICI Direct, Groww notes |
| Exchange transaction charge | ₹3,250 per crore of premium turnover for SENSEX options (BSE revision effective 1 Oct 2024) | FACT (verify): Angel One, Business Standard. **No 2026 revision found; check the BSE circular** |
| SEBI turnover fee | ₹10 per crore | FACT (verify) |
| Stamp duty | 0.003% of buy-side premium | FACT (verify) |
| GST | 18% on brokerage + exchange + SEBI fees | FACT |
| Brokerage (Dhan) | Flat ₹20 per executed F&O order (or 0.03%, whichever is lower) | FACT (verify on your contract note) |
| SEBI Oct 2024 F&O measures | Upfront premium collection, intraday position-limit monitoring, no calendar-spread margin benefit on expiry day, +2% ELM on short options on expiry day | FACT (verify). Only upfront premium affects a long-only system |
| SEBI retail-algo framework | Binding on all brokers from **1 Apr 2026**. API orders must come from a registered app/algo ID through a **whitelisted static IP**, with daily 2FA, an order-rate cap (10/s cited) and, at several brokers, market orders converted to market-price-protection orders | FACT (verify): FYERS/Upstox notices, SEBI circulars as summarised by brokers. **Confirm Dhan's exact implementation (static-IP registration, IP-change lock period) before Phase 8** |
| Trading holidays | BSE publishes an annual list. The engine needs `holidays` for the expiry rule and cross-checks against Dhan's expiry list | FACT |

**Implication.** Costs are higher in 2026 than in most published backtests, because STT rose 50% for option sellers (an exit is a sell). Any edge found in 2023–2025 data must be re-costed at 2026 rates. `costs.py` always uses the current rates.

---

## 3. Dhan API Capability Audit

Source: the **official `dhanhq` Python SDK v2.2.0** (PyPI), whose source was read line by line on 2026-10-08. The SDK is authoritative for which calls exist and for request shapes. Response field names that the SDK does not define are marked **verify** and isolated in `dhan_adapter.FIELDS`.

### REST (base `https://api.dhan.co/v2`)

| Capability | SDK method → endpoint | Notes |
|---|---|---|
| Option chain (all strikes, one expiry) | `option_chain(under_security_id, under_exchange_segment, expiry)` → `POST /optionchain` | SDK docstring: returns **OI, Greeks, volume, LTP, best bid/ask, IV** across strikes. Rate limit: one unique request per 3 s (Dhan release notes; verify) |
| Expiry list | `expiry_list(under_security_id, under_exchange_segment)` → `POST /optionchain/expirylist` | Used as the authority for "today is expiry" |
| Intraday candles | `intraday_minute_data(security_id, exchange_segment, instrument_type, from, to, interval∈{1,5,15,25,60}, oi)` → `POST /charts/intraday` | Index: OHLC only (an index has no volume). Options: OHLCV + OI |
| Daily candles | `historical_daily_data(...)` → `POST /charts/historical` | Prior-day high/low/close |
| **Expired options history** | `expired_options_data(security_id, exchange_segment, instrument_type, expiry_flag∈{WEEK,MONTH}, expiry_code, strike∈{"ATM","ATM+1",…}, drv_option_type∈{CALL,PUT}, required_data⊆{open,high,low,close,iv,volume,strike,oi,spot}, from, to, interval)` → `POST /charts/rollingoption` | **The key research input.** Rolling moneyness series that must be split back into fixed strikes (`history.py`). **Has no bid/ask**, so spreads are modelled. BSE_FNO coverage and depth of history: **verify** |
| Snapshot quotes | `ticker_data` / `ohlc_data` / `quote_data` → `POST /marketfeed/ltp|ohlc|quote` | Fallback when the WebSocket is down |
| Place / modify / cancel | `place_order(...)` (`tag` = correlationId), `modify_order(order_id, order_type, leg_name, quantity, price, trigger_price, disclosed_quantity, validity)`, `cancel_order(order_id)` → `/orders` | Order types `LIMIT`, `MARKET`, `STOP_LOSS`, `STOP_LOSS_MARKET`. Validity `DAY`, `IOC`. Product `INTRADAY` |
| Super order (entry + target + SL + trail) | `place_super_order(..., targetPrice, stopLossPrice, trailingJump)` → `/super/orders` | Evaluated in paper stage only (§24) |
| Order status | `get_order_by_id`, `get_order_by_correlationID` → `/orders/external/{id}`, `get_order_list` | Correlation-ID lookup gives restart idempotency |
| Trades | `get_trade_book(order_id)`, `get_trade_history(from, to, page)` | Real fills for slippage measurement |
| Positions / holdings | `get_positions()` → `/positions` | Broker truth for reconciliation |
| Funds / margin | `get_fund_limits()` → `/fundlimit`; `margin_calculator(...)` → `/margincalculator` | |
| **Account kill switch** | `kill_switch('ACTIVATE'|'DEACTIVATE')`, `status_kill_switch()` → `/killswitch` | Disables trading for the rest of the day. Used as the last line of the emergency chain |
| Instrument master | `fetch_security_list()` downloads `https://images.dhan.co/api-data/api-scrip-master.csv` (compact) or `…-detailed.csv` | Security IDs, lot sizes, expiries, strikes |

Rate limits: **FACT (verify)**, Dhan FAQ. Orders: 10/s, 250/min, 1,000/h, 7,000/day. Data APIs: 5 req/s. Non-trading APIs: 20 req/s. Option chain: one unique request per 3 s.
Cost: the Data API is a paid add-on (₹499 + taxes/month cited). FACT (verify).

### WebSocket

| Feed | URL / mode | Content (from SDK binary parsers) |
|---|---|---|
| Market feed v2 | `wss://api-feed.dhan.co?version=2&token=…&clientId=…&authType=2` | Modes: **Ticker (15)**: LTP, LTT. **Quote (17)**: LTP, LTQ, LTT, ATP, volume, total buy/sell qty, OHLC. **Full (21)**: Quote + **OI, OI day high/low, 5-level depth** (bid/ask price, qty, orders). Also OI packets and prev-close packets (prev close, prev OI) |
| Subscription | 100 instruments per subscribe message (SDK batches by 100) | Per-connection and per-account caps (commonly cited as 5,000 instruments and 5 connections): **verify** |
| Disconnect codes | 805 too many connections, 806 data APIs not subscribed, 807 token expired, 808 invalid client ID, 809 auth failed | Each maps to a kill-switch/alert reason |
| Heartbeat | The SDK pings over the websockets library and reconnects on `ConnectionClosed` | Engine adds its own staleness timer (§5) |
| 20 / 200-level depth | `wss://depth-api-feed.dhan.co/twentydepth`, `wss://full-depth-api.dhan.co/` | Up to 50 instruments at 20-level, 1 at 200-level. BSE coverage: verify. Not required by v1 |
| Order updates | `wss://api-order-update.dhan.co` | Push fills. v1 also polls (belt and braces) |

**Timestamp caveat (FACT, from SDK source):** the SDK renders `LTT` with `datetime.utcfromtimestamp(epoch).strftime('%H:%M:%S')`. Whether Dhan's epoch is true UTC or IST-wall-clock-as-epoch decides whether that string is IST. The adapter **resolves this against local receipt time** (`dhan_adapter.normalize_ltt`, `tick_from_feed`) and never assumes. Historical epochs are resolved by requiring the first bar to fall near 09:15 IST, and the load fails otherwise (`history.resolve_epochs`).

---

## 4. Data Architecture

### Dhan Data Availability Matrix

| Requirement | Dhan available? | Exact API / SDK | Real-time? | Calculated internally? | Alternative |
|---|---|---|---|---|---|
| SENSEX LTP | Yes | WS Ticker on `IDX_I`; `/marketfeed/ltp` | Yes | — | — |
| SENSEX 1-min OHLC | Yes (history) | `/charts/intraday` (IDX_I, INDEX) | History | Live candles **built internally** from ticks | — |
| SENSEX tick data | LTP updates only | WS Ticker | Yes | — | — |
| SENSEX volume | **Dhan does not provide this field: an index has no volume** | — | — | No | Futures volume (thin), option volume as proxy. **Removed from v1** |
| SENSEX depth / bid / ask | **Dhan does not provide this field for an index** | — | — | No | Not needed: the index is not traded |
| SENSEX historical candles | Yes | `/charts/intraday`, `/charts/historical` | — | — | — |
| Previous-day H/L/C | Yes | `/charts/historical`; WS prev-close packet | — | — | — |
| VWAP (index) | **Not computable (no volume)** | — | — | **No** | **Removed** (§12) |
| Option contracts / IDs / lot | Yes | Scrip master CSV | Daily | — | — |
| Option chain snapshot | Yes | `/optionchain` | Polled (≤1 per 3 s) | — | WS Full for the few traded strikes |
| CE/PE LTP | Yes | WS Quote/Full | Yes | — | — |
| CE/PE bid/ask + qty | Yes | WS Full (5 levels); chain top bid/ask | Yes | Spread %, top-5 depth | — |
| Option volume | Yes | WS Quote/Full (cumulative) | Yes | Per-minute volume by differencing | — |
| OI / change in OI | Yes | WS Full / OI packet; chain `oi`, `previous_oi` | Yes (exchange OI updates are periodic) | ΔOI | — |
| IV | Yes (chain) | `/optionchain` `implied_volatility` | Polled | Can also be backed out with Black-Scholes | — |
| Greeks (delta) | Yes (chain) | `/optionchain` `greeks` | Polled | BS fallback (`options.bs_delta`) | — |
| Option historical (live contracts) | Yes | `/charts/intraday` (BSE_FNO, OPTIDX) | — | — | — |
| **Option historical (expired)** | Yes, rolling moneyness | `/charts/rollingoption` | — | Fixed-strike reassembly | — |
| Historical option bid/ask | **Dhan does not provide this field** | — | — | **Modelled** (ASSUMPTION, §31) | Record live spreads from Stage 3 onwards to calibrate |
| Funds / margin | Yes | `/fundlimit`, `/margincalculator` | On request | — | — |
| Positions / orders / trades | Yes | `/positions`, `/orders`, `/trades` | On request + order WS | — | — |
| Order place / modify / cancel | Yes | `/orders` | — | — | — |
| Exit position | Via a SELL order | `/orders` | — | — | — |
| Emergency square-off | Cancel + SELL all; account kill switch | `/orders`, `/killswitch` | — | — | — |
| Exchange holiday calendar | **Dhan does not provide this as an API** | — | — | — | Cross-check against `/optionchain/expirylist` + BSE list |

### Pipeline

```
Dhan WS (Ticker: IDX_I SENSEX; Full: ~10 option contracts)  ─┐
Dhan REST (chain every 30–60 s, expiry list, scrip master)  ─┤
                                                              ▼
 dhan_adapter.tick_from_feed     (raw → Tick, timestamp normalisation)
                                                              ▼
 quality.TickValidator           (duplicate / out-of-order / jump quarantine)
                                                              ▼
 candles.CandleBuilder           (closed 1-min candles; late ticks never rewrite history)
                                                              ▼
 quality.assess                  (stale / gap / invalid / disconnected → GOOD | BAD)
                                                              ▼
 features.snapshot + reference_levels                (pure functions of closed candles)
                                                              ▼
 regime.classify → setups.detect → engine.evaluate_entry     (DECISION only)
                                                              ▼
 risk.pre_trade_checks + size_position  (inside the engine; RiskConfig)
                                                              ▼
 execution.PreOrderGuard          (independent pre-order chain, §28)
                                                              ▼
 BrokerExecutionProvider          (PaperBroker | DhanBroker[armed only after gate])
                                                              ▼
 position.manage (every closed bar) + broker reconcile (every few seconds)
                                                              ▼
 audit.AuditLog                   (hash-chained JSONL of everything)
 risk.KillSwitch                  (reads broker truth; independent of strategy)
```

Separation: **DATA** (`candles`, `quality`, `dhan_adapter` feed parts) → **FEATURES** (`features`, `regime`) → **SIGNALS** (`setups`, `engine`) → **RISK** (`risk`) → **ORDERS** (`execution`) → **EXECUTION** (broker providers) → **ACCOUNT STATE** (broker reads plus reconciliation in `live`). No layer reaches back up the chain.

---

## 5. Data Quality Engine

Implementation: `quality.py`. Every check is a hard condition. **Anything except GOOD → NO_TRADE for entries.** Exits still run on degraded data, in favour of getting flat.

| Detects | Rule | Action |
|---|---|---|
| Stale underlying | `now − last index tick receipt > 3,000 ms` | BAD, `DATA_STALE` |
| Stale option quote | newest option quote older than 5,000 ms | BAD |
| Stale chain | chain snapshot older than 90 s | BAD, `CHAIN_STALE` |
| Missing ticks / candles | gaps reported by `CandleBuilder.gaps`; > 2 missing minutes since open | BAD |
| Duplicate ticks | same instrument, timestamp, price and volume | dropped, counted |
| Out-of-order ticks | timestamp earlier than the last accepted | dropped, counted |
| Late ticks | tick for an already-closed minute | never applied, counted |
| Abnormal timestamps | negative age < −1 s, or unresolved epoch convention | BAD |
| Invalid OHLC | high < max(open, close, low), low > min(…), non-positive or NaN | BAD |
| Abnormal jump | single tick move > 6 × 1-min ATR | quarantined (not applied). Persisting jumps surface as staleness |
| WS disconnect | `feed_connected = False`, or Dhan codes 805–809 | BAD, alert. If a position is open, the protective SL is already at the exchange |
| Market closed / wrong day | outside 09:15–15:30 or not a trading day | NO_TRADE |
| Incorrect expiry / contract | rule calendar and Dhan expiry list disagree → `CONFLICT`; scrip-master lot or ID mismatch | NO_TRADE for the day |
| Partial chain | the strike the engine wants has no quote | `DATA_STALE` → NO_TRADE |

Every signal carries `data_timestamp`, `signal_timestamp`, `data_age_ms` and `data_quality` (see `QualityReport.as_dict`).

---

## 6. Market Microstructure Analysis

What is special about expiry day. Each item states what is known and what must be measured.

| Phenomenon | Status | What it implies / how it is measured |
|---|---|---|
| Theta collapse | FACT (option math): the time value of an ATM option goes to 0 by 15:30 | Every minute held costs more late in the day. Measure the ATM straddle decay curve per 15-min bucket from rolling-option history |
| Gamma concentration near ATM | FACT (option math) | Small index moves cause large % premium moves. This is the source of convexity, and also of violent stops |
| Implied vs realised (variance premium) | FACT for index options in general; **HYPOTHESIS** for SENSEX expiry intraday | Compute IV (from the `iv` field) against realised 1-min volatility over the following N minutes, by time bucket. If implied > realised in every bucket, conditional entries must find where it is not |
| Spread widening late and on fast candles | HYPOTHESIS (practitioner consensus) | Record live spreads from Stage 3. Until then spreads are modelled at 0.5% of premium per side and stressed ×2 |
| Pinning to big strikes | HYPOTHESIS (weak evidence; confounded) | Test: distance of the 15:30 close to the max-OI strike against a null of random strikes. **Not used by v1** |
| Strike migration (OI shifting) | HYPOTHESIS | Logged (`chain_research_features`) |
| Exercise STT trap | FACT: an ITM long option left to settle pays STT on **settlement value**, not premium | Every position is flat by 15:10 (§47) |
| Liquidity concentration | FACT (verify): SENSEX weekly expiry-day option volumes are among the largest on BSE; futures volumes are small | Options are the only practical vehicle (§15) |
| Latency | ASSUMPTION: a retail cloud VM to Dhan adds about 50–300 ms per REST call | Close-based triggers make sub-second latency irrelevant to signals. It matters only for stop chasing |

---

## 7. Market Regime Engine

Implementation: `regime.py`. **HYPOTHESIS:** removing it will hurt out-of-sample expectancy. If the `no_regime` ablation does not hurt, the regime filter is removed (§29).

Inputs per closed bar *t* (all causal):
- ATR14: Wilder, 1-min, seeded from the first bars and prior close.
- ER30 = |c_t − c_{t−30}| / Σ|Δc| over 30 bars.
- slope = OLS slope of the last 30 closes.
- OR15 high/low.
- Structure from confirmed k=3 fractal swings:
  - UP = last two swing highs and last two swing lows both rising.
  - DOWN = both falling.
  - MIXED otherwise.
- Compression = (range of last 20 bars) / (ATR60 · √20). About 1 for a random walk; low means compressed.
- last TR.

Precedence, first match wins:

| # | Regime | Rule |
|---|---|---|
| 1 | UNCLEAR | < 20 closed bars, or ATR undefined |
| 2 | FAILED_BREAKOUT | within the last 10 bars a close beyond ORH/PDH (ORL/PDL) by ≥ 0.10 ATR, **and** the latest close back inside by ≥ 0.25 ATR |
| 3 | REVERSAL | structure 30 bars ago was UP (DOWN) and is now DOWN (UP) |
| 4 | STRONG_BULL | ER30 ≥ 0.45 ∧ slope > 0 ∧ close > ORH ∧ structure UP |
| 4 | STRONG_BEAR | ER30 ≥ 0.45 ∧ slope < 0 ∧ close < ORL ∧ structure DOWN |
| 5 | EXPANSION | last TR ≥ 2.0 × ATR14 |
| 6 | COMPRESSION | compression ≤ 0.60 |
| 7 | RANGE | ER30 < 0.25 |
| 8 | UNCLEAR | otherwise |

Setup permissions (`regime.ALLOWED`):
- S1 is allowed everywhere except a strong trend **against** it. A long sweep in STRONG_BEAR is forbidden.
- S2 is allowed only in STRONG_* aligned with it, EXPANSION, or UNCLEAR.
- S3 is allowed in COMPRESSION, EXPANSION, RANGE or UNCLEAR.

---

## 8. Liquidity Engine (sweeps)

**HYPOTHESIS:** a brief run beyond a widely watched level that is quickly reclaimed marks exhausted one-sided order flow, and is followed by a move away from the level larger than a random entry would produce.

Levels considered: PDH/PDL, ORH/ORL (15-min), and confirmed swing highs/lows. Priority when several qualify: PD > OR > swing. Not considered:
- VWAP (does not exist, §12).
- Option strikes as levels. Round 100-point strikes coincide with round numbers; this is tested only as a research ablation.
- Session high/low as separate levels. They are covered by swings and OR.

Definitions (bullish; bearish is mirrored in code, `setups.mirror`):

| Element | Rule (ATR = ATR14 at the trigger bar) |
|---|---|
| Level established | level valid at least 10 bars before the sweep bar (PD levels always valid) |
| Approach | the bar before the sweep **closed ≥ level** (price arrived from above) |
| Minimum penetration | sweep bar low < level − **0.10 ATR** |
| Maximum penetration | lowest low from sweep to reclaim ≥ level − **1.5 ATR** (deeper = breakdown, rejected) |
| Reclaim | the first close > level + **0.05 ATR**, within **5 bars** of the sweep bar inclusive |
| Rejection | reclaim close − sweep extreme ≥ **0.75 ATR** |
| Entry trigger | the first subsequent close > the reclaim bar's high, within **3 bars** |
| Displacement | recorded as a confirmation (bar range ≥ 1.5 ATR, body ≥ 60%, close in the outer quarter). **Not required**: requiring it is an ablation candidate |
| Volume | **Not used** (the index has no volume) |
| Invalidation | sweep extreme − **0.25 ATR**. An underlying close beyond it ends the trade |
| Room | next opposing level ≥ **1.5×** stop distance away (none = open space) |
| Max holding | 60 bars, with a time stop after 15 bars if MFE < +0.5R |

Variants to test (pre-registered, total 6): penetration min ∈ {0.10, 0.25}; reclaim bars ∈ {3, 5}; rejection ∈ {0.5, 0.75}. They are evaluated only as a **neighbourhood stability** check (§28), never to pick the best.

---

## 9. Opening Range Engine

Every OR length {5, 10, 15, 30} becomes a hypothesis family the moment it is tried. v1 locks **OR15** and treats the others as a robustness neighbourhood, not as a choice.

**HYPOTHESIS for each use:**

| Use | v1 decision | Why |
|---|---|---|
| Structural level for sweeps (S1) | **Yes** | It is the most widely watched intraday level |
| Entry trigger on raw breakout | **No** | Raw ORB on liquid indices has decayed in published studies, and on expiry day false breaks are expected to be frequent (HYPOTHESIS to measure) |
| Acceptance + retest trigger (S2) | **Yes**, until 12:00 | Requires two closes beyond (acceptance) plus a held retest, which filters out most false breaks by construction |
| Regime input | Yes (STRONG_* needs close beyond the OR) | |
| Stop reference | S2 stop = min(retest low, ORH) − 0.25 ATR | |
| No-trade boundary | No | It would duplicate the regime engine |

Research outputs required (§34 `by_hour`, setup breakdowns) for each OR length, expiry vs non-expiry: break frequency, false-break frequency (close back inside within 10 bars), win rate, expectancy, PF, MAE/MFE, time to target and failure, excursion distributions. Until real data is run these are **unknown**.

---

## 10. VWAP / Structure Engine

### VWAP: removed

- **FACT:** VWAP = Σ(price·volume)/Σvolume. SENSEX has no volume, so **SENSEX VWAP cannot be calculated**.
- The near-month SENSEX future has volume. **FACT (verify):** BSE SENSEX futures are thinly traded, so a futures VWAP would be computed from sparse prints.
- An option-volume-weighted proxy has no theoretical basis.

**Decision: VWAP is removed from v1.** It may return only if a futures-VWAP feature passes an ablation on real data.

### Market structure (objective)

| Concept | Definition (`features.py`) |
|---|---|
| Swing high / low | fractal: high strictly above the 3 prior highs and ≥ the 3 following highs; **confirmed 3 bars later** and invisible until then |
| HH / HL / LH / LL | comparison of the last two confirmed swing highs (lows) |
| Break of structure | close beyond the most recent confirmed swing in the trend direction |
| Market-structure shift | structure label flips UP↔DOWN (regime REVERSAL) |
| Displacement | range ≥ 1.5 ATR, body ≥ 60% of range, close in the outer 25% |
| Failed breakout / breakdown | see regime FAILED_BREAKOUT |
| Compression / expansion | compression ratio ≤ 0.60 / TR ≥ 2 ATR |

---

## 11. Options Structure Engine

Logged per decision (`options.chain_research_features`): ATM, near-ATM PCR (±5 strikes), max-OI CE strike, max-OI PE strike. Later research adds ΔOI by strike, volume/OI, IV term, premium acceleration and strike migration.

**Why none of it drives decisions in v1:**
1. "Highest call OI = resistance" is a claim about option *writers'* positions. Writers hedge dynamically, and the hedge flow can push price toward **or away from** the strike. The sign is not identifiable a priori. **HYPOTHESIS with an unknown sign.**
2. OI is reported at exchange cadence (not tick by tick), and the chain API is limited to one request per 3 s. Signals built on ΔOI therefore have coarse, uneven timing.
3. Many candidate OI features (PCR, max pain, walls, ΔOI, IV skew) on a small sample → severe multiple-testing risk (§28).

Test protocol before any OI feature can enter: a single pre-registered feature. It must improve out-of-sample expectancy by ≥ 0.1R in the ablation and survive a permutation test at p < 0.05 **after** Bonferroni correction for every OI feature tried.

---

## 12. Volatility Engine

Measures: ATR14 and ATR60 (1-min), compression ratio, last-TR/ATR, session range / prior-day range. When the chain is available: ATM IV and its change since the open.

Classification, by percentile of session-to-date ATR60 against the trailing 40 expiry days at the same time of day:

| Class | Percentile | Effect in v1 |
|---|---|---|
| LOW | < 20 | allowed. S3 relies on compression |
| NORMAL | 20–80 | allowed |
| HIGH | 80–95 | allowed. Stops are structural, so they widen in points, but **rupee risk is unchanged** because sizing is on rupees |
| EXTREME | > 95, or any 1-min TR > 6 ATR (jump quarantine) | **NO_TRADE** for new entries |

Volatility adapts the **stop distance in points** (ATR-scaled buffers) and therefore lot count. It never changes the **rupee risk**. It never raises trade frequency.

Status: the percentile table needs ≥ 40 expiry days of history before it can be computed. Until then only the EXTREME jump rule is active. **ASSUMPTION.**

---

## 13. Time-of-Day Engine

Locked windows (HYPOTHESIS: entries outside them are worse; tested by the `no_time` ablation):

| Window | Policy | Rationale |
|---|---|---|
| 09:15–09:20 | **No entries** | opening auction residue, widest spreads, OR still forming |
| 09:20–09:35 | **No entries** | OR15 completes at 09:30; 5 bars settling |
| 09:35–10:00 | S1, S2 | first full-information period |
| 10:00–11:00 | S1, S2 | |
| 11:00–12:00 | S1, S2 (S2 last entry 12:00) | |
| 12:00–13:00 | S1 only | the "lunch" lull is a HYPOTHESIS; low ER expected |
| 13:00–14:30 | S1, S3 | expiry gamma builds; S3 window |
| 14:30–14:45 | S1 only | last entry 14:45 |
| 14:45–15:10 | **No entries**; management only | |
| 15:10 | **Hard flat** | margin before Dhan's own intraday auto square-off and before settlement-window dynamics |
| 15:10–15:30 | Nothing | |

Research output: `by_hour` breakdown for every metric in §34. Any window whose out-of-sample expectancy is negative with P(mean ≤ 0) > 0.8 is closed in the next version, with full revalidation.

---

## 14. Setup Research

Candidate list from the brief, with the decision for each:

| Candidate | Decision | Reason |
|---|---|---|
| A Liquidity Sweep + Reclaim | **Kept → S1** | Clear mechanical definition and a bounded stop. Merges A, B, E and F into one family, which reduces multiple testing |
| B Opening Range Failure | **Merged into S1** | An OR failure is a sweep of ORH/ORL |
| C ORB + Retest | **Kept → S2** | The logical opposite of S1. Keeping both stops the research from assuming only one regime exists |
| D VWAP Reclaim + Displacement | **Removed** | Index VWAP does not exist |
| E VWAP Rejection + Sweep | **Removed** | Same reason |
| F Major-strike sweep + reversal | **Merged into S1** if strikes ever become levels | OI-based strike selection is unproven (§11) |
| G Late expiry expansion | **Kept → S3** | Directly targets the expiry-specific mechanism (gamma) that motivates the whole project |

Three setups, not seven. Each extra setup multiplies the false-discovery risk on a sample this small.

---

## 15. Final Setup Library

Common to all three:
- Signals only on **closed 1-min bars**.
- Order sent after the trigger bar closes. The backtest fills at the next bar's option open + ½ spread + 2 ticks.
- Instrument: weekly SENSEX option expiring **today**, **ATM** (strike nearest the trigger close), **CE for LONG, PE for SHORT**.
- **Why options, not futures or index:**
  - The index is not tradable.
  - SENSEX futures are thin (FACT (verify)) and have unbounded intraday loss.
  - Long options give bounded loss (the premium) and convexity.
  - The cost is theta and the variance premium, which conditional entries must overcome.
- **Why ATM and not OTM:** OTM strikes have the worst variance premium and the widest relative spreads. ITM strikes have little convexity. ATM ±1 are pre-registered alternatives (`OptionConfig.moneyness`) and are tested only as a neighbourhood check.

### S1 — Sweep & Reclaim (`S1_SWEEP_RECLAIM`)
- **Regime:** anything but a strong trend against the trade.
- **Prerequisites:** §8 table.
- **Trigger:** first close > reclaim-bar high within 3 bars of the reclaim.
- **Entry:** marketable IOC limit at ask + 2 ticks.
- **Stop:**
  - Underlying invalidation = sweep extreme − 0.25 ATR (close-based).
  - Premium stop = entry − |Δ| × (trigger − invalidation) × 1.2, clamped to [10%, 50%] of premium. More than 50% needed → **no trade** (`STOP_TOO_WIDE`).
- **Target:** none (trail), see §18.
- **Time invalidation:** 15 bars without +0.5R MFE.
- **Max hold:** 60 bars.
- **No-trade:** §21.
- **Expected R:** **unknown** (`expected_R = null` until validated).
- **Reason codes:** `SWEEP_DETECTED`, `LEVEL_RECLAIMED` (+ `DISPLACEMENT`, `LEVEL_CONFLUENCE`, `REGIME_ALIGNED`, `RISK_REWARD_VALID`).

### S2 — Opening-Range Acceptance + Retest (`S2_ORB_ACCEPT`)
- **Regime:** STRONG_* aligned, EXPANSION, or UNCLEAR.
- **Window:** 09:35–12:00.
- **Acceptance:** the first 2 consecutive closes > ORH + 0.10 ATR.
- **Retest:** within 10 bars, a bar with low ≤ ORH + 0.25 ATR and close > ORH.
- **Any close < ORH after acceptance cancels the setup.**
- **Trigger:** the first close > retest-bar high within 3 bars.
- **Invalidation:** min(retest low, ORH) − 0.25 ATR.
- Premium stop, sizing, exits and max hold as S1.
- **Reason codes:** `ORB_ACCEPTANCE`, `ORB_RETEST_HELD`.

### S3 — Late Compression → Expansion (`S3_LATE_COMPRESSION`)
- **Window:** 13:00–14:30.
- **Box:** the 20 bars before the trigger, with compression ratio (vs ATR60 at t−1) ≤ 0.60.
- **Trigger:** a close > box high + 0.10 ATR on a displacement bar.
- **Invalidation:** box mid − 0.25 ATR (pre-registered alternative: box low).
- **Rationale (HYPOTHESIS):** after a compression, late-day ATM premiums are cheap in rupees while gamma is maximal, so a genuine expansion pays convexly.
- **Reason codes:** `COMPRESSION_BREAK`, `VOLATILITY_EXPANSION`, `DISPLACEMENT`.

If several setups fire on one bar in the **same direction**, priority is S1 > S2 > S3. If they fire in **opposite directions**, the result is NO_TRADE (`REGIME_CONFLICT`).

---

## 16. Entry Engine

| Field | Definition |
|---|---|
| ENTRY_ELIGIBILITY | expiry day (broker list + rule agree) ∧ data GOOD ∧ bar close in window ∧ setup fired ∧ regime allows ∧ room ≥ 1.5 ∧ risk checks pass ∧ quote tradable |
| ENTRY_TRIGGER | the setup's close-based condition on the last closed bar (§15) |
| ENTRY_PRICE | IOC LIMIT at best ask + 2 ticks (`PreOrderGuard`). Backtest: next bar's option open + max(₹0.05, 0.25% × premium) + 2 ticks |
| ENTRY_EXPIRY | today's weekly SENSEX expiry. Never a later expiry in v1 |
| ENTRY_TIMEOUT | IOC: unfilled → cancelled immediately. **No re-send.** The signal is consumed (one attempt per setup key per day) |

No discretion, no "momentum looks strong": every term above is code.

---

## 17. Stop-Loss Engine

Candidates and verdicts:

| Stop | Verdict |
|---|---|
| Fixed-candle low | Rejected. It is arbitrary and noise-dominated on 1-min bars |
| Pure ATR stop | Rejected as primary. It ignores where the idea is actually wrong |
| **Structural (sweep extreme / retest low / box mid) − 0.25 ATR, close-based** | **Primary invalidation.** It is the price where the setup's premise is false |
| **Premium stop (exchange SL order)** | **Primary loss bound.** Derived from the structural distance through delta (×1.2 for gamma/vega), clamped to 10–50% of premium |
| Tightest possible stop | Rejected. On expiry gamma it is hit by noise, and transaction costs then dominate |

Why both: the structural stop is close-based, so it cannot protect against an intrabar crash. The exchange-resident premium SL can. Whichever triggers first exits.

Robustness: the 1.2 multiplier and the [10%, 50%] clamp form a neighbourhood test: {1.0, 1.2, 1.5} × clamp max {40%, 50%}. **The stop never widens after entry.** It only ratchets: at MFE ≥ +1R it moves to entry plus per-unit costs and slippage, exactly once (tested).

---

## 18. Exit Engine

Order of evaluation each closed bar (`position.manage`), most conservative first:
1. Premium stop (a gap through the stop fills at the bar open, not at the stop).
2. Fixed target, if the policy has one. If stop and target are both touched in one bar, the stop is assumed first.
3. Hard flat.
4. Structural invalidation (close).
5. Trail.
6. Time stop.
7. Max hold.

Close-based exits fill at the **next** bar's open (bid side) − 2 ticks.

Pre-registered exit policies. These four are the **only** thing selected on validation folds (`__main__.cmd_walk_forward`):

| Policy | Rule |
|---|---|
| **TRAIL (default)** | after MFE ≥ +1R: underlying chandelier, exit on close beyond best close ∓ 2.0 ATR |
| FIXED_2R | limit at entry + 2R per unit |
| FIXED_3R | limit at entry + 3R per unit |
| TIME_ONLY | no target, no trail. Only time stop, max hold and hard flat |

§46 question ("no target by default?"): TRAIL is the default because the zero→hero thesis **requires** an uncapped right tail. If walk-forward selects FIXED_2R in most folds, that is evidence *against* the right-tail thesis, and it must be reported as such.

---

## 19. Zero→Hero Asymmetric Engine

- **1R** = rupees lost if the premium stop fills at its price, plus 2 ticks of slippage on each side, plus round-trip costs (`risk.size_position`, re-anchored on the real fill in `position.open_position`).
- Outcomes allowed:
  - −1R (stop).
  - Worse than −1R only through gap/slippage. The backtest checks this stays above −1.6R.
  - Scratch (breakeven ratchet).
  - +1R to +5R+ (trail).
- **Catastrophic bound:** premium outlay ≤ 3 × risk budget, so a total loss of premium (stop never fills) costs ≤ 3R. **This is the only "hero" in the design: the downside is capped by construction.**
- Forbidden, all enforced in code:
  - Averaging: one position max (`RiskConfig.max_open_positions = 1`, validated).
  - Doubling after losses: risk % is constant and config-locked.
  - Raising risk on confidence: the score has no path into sizing.
  - Chasing: IOC with no re-send.
  - Revenge: 2 consecutive losses end the day; 5-min cooldown; the same setup key never retrades.
  - Widening a stop: the ratchet only goes up.

Research outputs needed: the R distribution (§35), especially P(R ≥ 3) and P(R ≥ 5), and the share of total profit from the top 5 trades. **SYNTHETIC RESULT:** on noise, the top-5 trades produced 10× the net total. Profit concentrated in a few outliers is the *default* shape of option-buying P&L. It must be shown not to be the *only* source of profit (gate: expectancy excluding the top 5 ≥ 0).

---

## 20. Scoring Engine

**Decision: the score is logged, not used to decide anything.**

Score = 100 × (share of five binary confirmations present): displacement, level confluence, room ≥ 2R, regime aligned, prime window. It is transparent and unweighted.

Why not a weighted 0–100 gate with VALID/HIGH-QUALITY/EXTREME bands:
- With an expected 20–80 real expiry-day trades, fitting 15 weights and 4 thresholds means more free parameters than independent outcomes. The placeholder bands (60/75/85/95) are therefore **not justified**.
- A feature-importance ranking from that few trades is noise.

Path to a real score (v2 at the earliest):
- ≥ 300 out-of-sample trades, pooled across days (§27).
- Logistic regression of P(R > 0) or regression of R on confirmations, fitted on training folds only.
- Kept only if it improves out-of-sample expectancy over the unscored rules in walk-forward and survives a permutation test.

---

## 21. No-Trade Engine

Reason codes are emitted for every rejection (`models.Reason`). A decision is NO_TRADE if **any** of these holds:

| Condition | Code |
|---|---|
| Not a weekly expiry day, or calendar conflict | `NOT_EXPIRY_DAY` |
| Data not GOOD (stale, gap, invalid, disconnected, stale chain) | `DATA_STALE` / `DATA_BAD` / `CHAIN_STALE` |
| Outside entry windows / after 14:45 | `OUTSIDE_WINDOW` |
| No setup fired | `NO_EDGE` |
| Setups disagree on direction; regime forbids the setup | `REGIME_CONFLICT` |
| Next opposing level < 1.5 stop distances | `ROOM_TOO_SMALL` |
| Structure needs > 50% premium stop | `STOP_TOO_WIDE` |
| < 1 lot fits the risk budget | `RISK_TOO_HIGH` |
| Spread > 2% of mid, or top-5 depth < 5× order qty | `SPREAD_TOO_WIDE` / `DEPTH_TOO_THIN` |
| Premium < ₹20 | `PREMIUM_TOO_LOW` |
| Daily loss: realised − 1R (the new trade) < −2R | `DAILY_LOSS_LIMIT` |
| 2 consecutive losses | `CONSECUTIVE_LOSSES` |
| 2 trades already today | `MAX_TRADES` |
| Position open / pending order (engine **or broker**) | `POSITION_OPEN` |
| < 5 min since last exit | `COOLDOWN` |
| Same setup/level/direction already traded today, or correlation ID exists at broker | `DUPLICATE_SIGNAL` |
| Kill switch tripped | `KILL_SWITCH` |
| Live gate not passed for this config hash | `LIVE_NOT_VALIDATED` |
| Extreme volatility (1-min TR > 6 ATR, jump quarantine) | via `DATA_BAD` |

"Move already extended" is covered by the room filter, the reclaim/acceptance geometry, and the stop cap: a far-extended entry has a structural stop > 50% of premium. "Chop" is covered by regime and by the fact that sweeps need ≥ 0.75 ATR rejection.

---

## 22. Risk Engine

Implementation: `risk.py`. It is independent of setups and sees only prices, quantities and state.

| Rule | Value | Enforced in |
|---|---|---|
| Risk per trade | 0.5% of capital (0.25% in TINY_LIVE) | `size_position` |
| Max loss per day | 2R (a new trade must fit inside what remains) | `pre_trade_checks` |
| Max consecutive losses | 2 → done for the day | `pre_trade_checks` |
| Max trades per day | 2 | `pre_trade_checks` |
| Max open positions | 1 (validated: config refuses anything else) | `EngineConfig.validate` |
| Max lots | 10 | `size_position` |
| Max premium outlay | min(10% of capital, 3 × risk budget) | `size_position` |
| Cooldown | 5 minutes after any exit | `pre_trade_checks` |
| Rolling drawdown | 20-trade drawdown > 8R → back to PAPER stage | process rule, §36 |
| Kill-switch daily loss (broker P&L) | 3R in rupees | `KillSwitch.check_broker_state` |

Quantity = ⌊ risk budget / (per-lot 1R) ⌋, capped by the limits above. If fewer than 1 lot fits → NO_TRADE. **Never rounded up.**

---

## 23. Position Sizing

| Model | Verdict |
|---|---|
| **Fixed fractional on 1R** (0.5% of current capital) | **Chosen.** Simple, scale-free, and it shrinks after drawdowns |
| Fixed monetary risk | Equivalent within a day. Fractional is preferred across weeks |
| Volatility-adjusted | Already implicit: structural stops widen with volatility while rupee risk stays fixed, so exposure falls automatically |
| Full Kelly | **Rejected.** It needs a known edge, and the estimated edge has a CI spanning zero (§1). Errors in the estimate make Kelly overbet |
| Fractional Kelly | **Reported only** (`risk.kelly_fraction`) as a ceiling check. It may never raise risk above the config value. Rule: if 0.1 × Kelly computed from the **lower** 95% CI of expectancy is below 0.5%, the risk % must be cut to it (an extra safety rule, never an increase) |

Example: 0.5% risk at a capital of ₹5,00,000 gives a ₹2,500 budget. With ATM premium ₹150.10, a stop at 30% (₹104.66), lot size 20 and costs of about ₹52 per lot, 1R per lot ≈ ₹965, so 2 lots. The outlay cap (3 × ₹2,500 = ₹7,500 ≥ ₹6,008) does not bind. This is the §37 example exactly.

---

## 24. Execution Engine

| Aspect | v1 rule |
|---|---|
| Entry order | LIMIT, IOC, at ask + 2 ticks. **No market orders**: depth can vanish for a second on expiry afternoons, and brokers may convert market orders to MPP under the 2026 algo framework |
| Protective stop | STOP_LOSS (SL-limit) SELL, trigger = premium stop, limit 10% below trigger. Placed immediately after the fill |
| Stop chase | if the trigger has traded and the SL is still open after 3 s: cancel, then send a LIMIT SELL at bid − 3%. Repeat until flat |
| Close-based exits | cancel the SL (booking it if it filled meanwhile), send a LIMIT SELL at bid × 0.97. **Only one exit order is ever live.** Unfilled after 3 s: cancel and re-price 3% lower. After 5 attempts: kill switch `EXIT_NOT_FILLING` → square-off (`live._poll_exit`) |
| Partial fills | the entry is IOC, so a partial is final. Position = filled qty, and 1R is recomputed on filled qty. The SL is sized to filled qty |
| Modification | only the stop ratchet (upward) |
| Slippage model | 2 ticks per side plus a modelled half-spread (ASSUMPTION; stressed ×2) |
| Latency | close-based logic, so latency does not change signals. Stop chase is time-based |
| Illiquid strikes | blocked by spread/depth/premium filters |
| Super order | **Not used in v1.** It is atomic (entry + SL in one call) and therefore attractive, but its trail and target semantics differ from §18. It may replace the 2-order flow only after a paper-stage comparison |
| Fill assumptions in backtests | never the signal-bar close. Always the next bar open, plus spread and slippage. A stop gap fills at the open. Stop before target in the same bar. No fill if there is no print |

---

## 25. Dhan Integration

`dhan_adapter.DhanBroker` implements the broker interface with `dhanhq` 2.2.0.

1. **Startup:**
   - Load the scrip master; find the SENSEX `IDX_I` id (expected `51`; any mismatch → refuse to run).
   - Find today's SENSEX weekly contracts and lot size.
   - Call `/optionchain/expirylist`. Today must be the nearest expiry and must agree with the rule calendar.
2. **Feed:**
   - The WS v2 subscribes Ticker for the index.
   - Full for ATM ± 5 strikes CE+PE. As the index drifts more than 3 strikes, re-subscribe the new window. Never more than ~30 instruments.
   - Chain REST every 60 s (delta and IV for the stop calculation). Throttled to ≥ 3 s between calls.
3. **Orders:** only through `PreOrderGuard`. `DhanBroker.armed` is False unless `validation_gate.live_allowed()` returns True for the running config hash; unarmed `place()` raises. **Exits and square-off work even when unarmed.**
4. **Idempotency:**
   - The correlation ID is derived from the setup key (date | setup | level | direction).
   - On restart, the engine queries `/orders/external/{correlationId}` before ever re-sending.
5. **Reconciliation** every 5 s: positions, open orders and day P&L from the broker against the engine. Any mismatch trips the kill switch, after first booking a legitimately filled stop.
6. **Compliance (2026 framework):**
   - Run from the static IP registered with Dhan.
   - Complete daily login/2FA token generation before 09:00.
   - Keep the order rate far below 10/s (the engine sends at most about 6 orders per trade, and the runaway detector trips at > 6/min).

---

## 26. State Machine

`state_machine.py`:

```
WAITING → DATA_VALID → REGIME_IDENTIFIED → (SETUP_FORMING) → SETUP_CONFIRMED → RISK_APPROVED
  → ORDER_PENDING → POSITION_OPEN → POSITION_MANAGEMENT ⟲ → EXIT_PENDING ⟲ → COOLDOWN → WAITING
any → KILLED (terminal; restart + manual lock removal)      WAITING/... → DAY_DONE (terminal)
```

- Invalid transitions raise `InvalidTransition`, which the runner treats as a kill-switch event.
- `accepts_entries` is False from ORDER_PENDING onward, so **no signal can create a second position**.
- Duplicate protection has three independent layers:
  - The state machine.
  - `DailyRiskState.traded_keys`.
  - The broker correlation-ID lookup plus the broker position check in the guard.

---

## 27. Backtesting Framework

Implementation: `backtest.py`, `history.py`, `__main__.py`.

**Data assembly:**
- Index 1-min (`/charts/intraday`, `IDX_I`).
- Prior-day H/L/C.
- Expired options via `/charts/rollingoption`, ATM−10…ATM+10 CE and PE, split into fixed strikes. Conflicting prints raise an error.
- Each day is stored as a JSON file and labelled with expiry flag, convention (Fri/Tue/Thu), gap, volatility class and event tags.

**Pooled design (essential given §1):**

| Study | Days | Instrument | Answers |
|---|---|---|---|
| A. Underlying signal | **all** trading days since 2015 (index 1-min history is far longer than option history) | underlying R (invalidation-based) | Do S1–S3 predict the next 60 min of index movement better than random entries? |
| B. Expiry payoff | weekly expiry days (~170) | the option, as specified | Does expiry-day convexity turn study A's edge into option profit after costs? |
| C. Non-expiry option payoff | non-expiry days with DTE 1–4 options | the option | Is expiry day actually better than other days, which is the premise of the project? |

Study A uses no option data, so its sample is about 10× larger. If A fails, B is moot. If A passes and B fails, the edge exists but expiry options do not pay for it.

Splits: chronological only.
- **In-sample** (development): the first 50% of days.
- **Validation:** the next 20%, used only to choose among the four pre-registered exit policies.
- **Out-of-sample:** the last 30%, opened **once**.
- **Walk-forward:** rolling folds of 40 train / 15 validate / 15 test expiry days, stepping by 15. Only test-fold trades are concatenated as out-of-sample.

Scenario breakdowns are required in every report. Bull, bear and range are defined by the 20-day index trend. Further breakdowns: high and low volatility (§12 classes), gap up and gap down (> ±0.5%), large expiry moves (> 1.5% open→close) and flat days (< 0.3%), trending vs reversal days (ER of the day), event days (RBI policy, Budget, US CPI/FOMC, election results, tagged manually *before* testing), and expiry convention (Fri/Tue/Thu).

---

## 28. Anti-Curve-Fitting Framework

| Bias | Control |
|---|---|
| Look-ahead | Stateless detectors on `c[:t+1]`; swings confirmed k bars later; OR hidden until complete; resample emits complete buckets only. **Tests:** truncation equality, and poisoning future option prices leaves past decisions unchanged (`TestBacktestIntegrity`) |
| Survivorship | Not applicable to a single index. Expired contracts come from the rolling endpoint, not from today's listing |
| Data snooping | All parameters in this document were fixed before data access, and `config_hash` is recorded in every report. Changing a rule creates a new hash and voids prior validation |
| Selection bias | Report **every** variant tried (`ablation`, neighbourhood grid), not the best |
| Multiple testing | 3 setups × 4 exits × 6 neighbourhood variants = 72 configurations. Use a permutation test against random entries, and judge the **median** neighbour, not the best (gate: ≥ 70% of neighbours positive). Bonferroni for any additional feature family (§11) |
| Parameter overfitting | Only the exit policy is selected, from 4 pre-registered values. All thresholds are round numbers (0.10/0.25/0.5/0.75/1.5 ATR, 5/10/15/20 bars) |
| Regime overfitting | Per-regime, per-convention and per-year breakdowns. An edge present in only one year or one convention is not an edge |
| Unrealistic execution | §24 fill rules; costs at 2026 rates; ×2 slippage and ×1.5 cost stress must stay ≥ 0 |
| Noise baseline | `--null-test`: random walks with fairly priced options must show no edge (SYNTHETIC RESULT: +0.06R, CI [−0.57, +0.82], did not beat random, p = 0.62) |

Suspicion rule: if profitability exists only at a specific value (e.g. the sweep reclaim works at 4 bars but not at 3 or 5), treat the whole setup as unvalidated.

---

## 29. Ablation Testing

`python -m sensex_expiry --ablation DATA_DIR` runs FULL and then removes one component at a time (`config.ABLATIONS`):

| Ablation | Question |
|---|---|
| `no_regime` | Does the regime filter add value? |
| `no_room` | Does the next-level room filter add value? |
| `no_time` | Do the time windows add value? |
| `no_spread` | Is the spread/depth filter costing good trades (live only)? |
| `only_S1…/only_S2…/only_S3…` | Which setup carries the edge, if any? |
| `exit_FIXED_2R / FIXED_3R / TIME_ONLY` | Is the right tail (trail) real? |
| Random-entry baseline (always reported) | Do entries add anything beyond the exit engine? |
| Options data, VWAP, volume | Already removed in v1. They re-enter only via ablation-positive evidence |

Rule: **a component whose removal does not reduce out-of-sample expectancy by ≥ 0.05R is removed.** The simplest system that keeps the edge wins.

---

## 30. Monte Carlo Analysis

`backtest.monte_carlo`: 5,000 bootstrap paths of the **out-of-sample** R sequence at 0.5% risk. It reports:
- Mean and median return, 5th and 95th percentile return.
- Median, 95% and 99% maximum drawdown, and the worst drawdown.
- 95th-percentile losing streak and longest time underwater (trades).
- Probability of ruin (default: a 20% equity drawdown).
- Best and worst path.

Acceptance (part of the WALK_FORWARD gate):
- 99% drawdown < 15% of equity.
- Probability of ruin < 1%.
- 95% losing streak ≤ 12 trades, so the process-level 20-trade rule (§22) is not triggered by expected bad luck.

Note: bootstrap assumes trades are independent. Block-bootstrap by week as a sensitivity check.

---

## 31. Transaction Cost Model

`costs.round_trip` (per round trip on premium turnover):

| Component | Rate (2026-10) | Side |
|---|---|---|
| Brokerage | ₹20 per executed order | both |
| STT | 0.15% of premium | sell |
| Exchange txn (BSE) | 0.0325% of premium | both |
| SEBI fee | 0.0001% | both |
| Stamp duty | 0.003% | buy |
| GST | 18% of (brokerage + exchange + SEBI) | — |
| Spread | max(₹0.05, 0.25% of premium) per side (ASSUMPTION) | both |
| Slippage | 2 ticks (₹0.10) per side (ASSUMPTION) | both |

Example (`costs.round_trip(150, 195, 40)`): 2 lots × 20 = 40 qty, buy ₹150, sell ₹195. Brokerage ₹40.00, STT ₹11.70, exchange ₹4.48, SEBI ₹0.01, stamp ₹0.18, GST ₹8.01, so statutory and broker costs total **₹64.39**. Add spread (about 2 × ₹0.38 × 40 ≈ ₹30) and slippage (₹8) for about ₹102 all-in, roughly 0.05R on the ₹1,930 1R of the §37 example. A 1-lot losing trade costs about ₹52 in fees alone. The flat ₹40 brokerage dominates at small size, so **1-lot tiny-live trades pay proportionally the most**. **At 1–2 lots, costs plus spread take a third or more of a plausible +0.15R edge, so gross results are never reported alone.** Every report shows gross and net (`metrics_gross`, `metrics_net`) plus `sensitivity` (spread ×2, slippage ×2, costs ×1.5).

---

## 32. Failure Modes

| Failure | Detection | Protection | Recovery |
|---|---|---|---|
| False breakout / sweep failure | invalidation close; premium SL | exchange SL; 1R sizing | normal loss. 2 losses → day over |
| Sudden reversal through stop (gap) | fill < stop | outlay ≤ 3R budget; gap modelled in backtest | loss > 1R logged; > 1.6R is an incident |
| IV crush after entry | premium falls while the index is unchanged | premium SL; time stop 15 bars | — |
| Theta decay | MFE < +0.5R after 15 bars | time stop | — |
| Spread expansion | spread filter at entry; exit at bid × 0.97 | no entry if > 2% | stop chase |
| Slippage | fill vs signal recorded | IOC entry; 2-tick model; ×2 stress | if realised > 1.5× model, back to PAPER |
| API failure (REST) | non-success status / exceptions | no order without a confirmed response; idempotent IDs | retry status by correlation ID; kill switch if unresolved > 30 s with a position |
| WebSocket failure | stale > 3 s; codes 805–809 | BAD → no entries; the SL sits at the exchange | reconnect; if > 60 s with a position → exit via REST |
| Wrong instrument | scrip-master ID/lot mismatch; expiry conflict | refuse to run | manual fix |
| Stale data | `quality.assess` | NO_TRADE | — |
| Duplicate order | correlation lookup; broker position check | 3-layer guard | kill switch on unexpected position |
| Excessive trading | trades/day counter; runaway detector > 6 orders/min | caps | lock |
| Regime change | rolling 20-trade drawdown > 8R; per-month expectancy | automatic demotion to PAPER | revalidate |
| Event shock | jump quarantine; EXTREME volatility | no entries | — |
| Overfitting | OOS gates, permutation, neighbourhood | locked config hash | — |
| Transaction cost change | yearly review of rates | cost stress ×1.5 | re-cost history |
| Tail event (circuit, exchange halt) | no prints; feed stale | outlay ≤ 3R | square-off at reopen |
| Engine crash with a position open | process supervisor; reconcile on start | exchange SL is resident; long-premium loss ≤ outlay | restart → reconcile → manage or exit |
| Expiry-settlement STT trap | — | hard flat 15:10 | — |
| Dhan auto square-off near close | — | own flat at 15:10 precedes it | — |

---

## 33. Emergency / Kill-Switch Architecture

`risk.KillSwitch` sits **outside** the strategy. It reads broker positions, open orders and P&L, and writes a lock file. The strategy cannot reset it. Restarting requires deleting the lock by hand.

**Triggers:**
- Broker day P&L ≤ −3R in rupees.
- Unexpected position (broker qty ≠ engine qty, after booking stop fills).
- Unexpected open orders.
- Gross qty > max.
- > 6 orders per minute (runaway loop).
- An `InvalidTransition`.
- WS down > 60 s with a position.
- REST order status unresolved > 30 s.
- Data anomaly with a position (no option prints for 3 consecutive minutes).
- An exit that has not filled after 5 chase attempts (`EXIT_NOT_FILLING`).

**Sequence** (`live.LiveRunner._emergency`):

```
STOP NEW ENTRIES  (state → KILLED; accepts_entries False)
CANCEL PENDING    (broker.square_off_all cancels every open order)
ASSESS POSITIONS  (broker.positions)
EXIT IF REQUIRED  (SELL every long; Dhan: MARKET is acceptable here; exits beat price)
LOCK ENGINE       (lock file persists; optional Dhan account kill switch /killswitch ACTIVATE)
```

A separate watchdog process (Phase 9) runs the same broker checks with its own credentials session, so a hung strategy process cannot disable protection.

---

## 34. Python Architecture

```
sensex_expiry/
├── __init__.py          version
├── __main__.py          research CLI: self-test, null-test, backtest, walk-forward, ablation, gate
├── config.py            frozen EngineConfig (the constitution's numbers) + config_hash + ablations
├── models.py            Tick, Candle, OptionQuote, SetupCandidate, Decision, enums, reason codes
├── session_calendar.py  expiry conventions, holidays, session times
├── candles.py           tick → 1m builder, resampling
├── quality.py           TickValidator, assess()
├── features.py          ATR, OR, swings, structure, compression, displacement, levels
├── regime.py            classify(), setup permissions
├── setups.py            S1/S2/S3 detectors (long logic + mirroring)
├── options.py           strike choice, tradability, premium stop, chain parsing, BS fallback
├── costs.py             round-trip cost model
├── risk.py              sizing, daily rules, Kelly report, KillSwitch
├── engine.py            StrategyEngine.evaluate_entry → Decision + signal JSON
├── position.py          Position, manage(), exits
├── state_machine.py     formal FSM
├── execution.py         interfaces, PreOrderGuard, protective stop, PaperBroker
├── dhan_adapter.py      DhanBroker (REST), feed conversion, scrip master, FIELDS to verify
├── live.py              LiveRunner: feed → engine → guard → broker → monitor → kill switch
├── backtest.py          event-driven backtester, metrics, bootstrap, MC, WF folds, baseline, stress
├── history.py           rolling-option reassembly, on-disk day format
├── audit.py             hash-chained JSONL audit log
├── validation_gate.py   stage criteria, live gate
└── synthetic.py         random-walk days for tests (never evidence)
```

Interfaces: `BrokerDataProvider`, `BrokerExecutionProvider` (Protocols in `execution.py`). The strategy (`engine`, `setups`, `features`, `regime`) imports nothing broker-specific, so swapping Dhan for another broker touches only `dhan_adapter.py`.

Dependencies: Python ≥ 3.12 standard library only. `dhanhq==2.2.0` is imported lazily, and only for live or paper use with Dhan data.

---

## 35. Test Architecture

89 tests, stdlib `unittest`, run in CI with the rest of the repository (`python -m unittest discover -s tests -t .`).

| Area | Tests (file :: class) |
|---|---|
| Config / constitution | `core::TestConfig`: hash stability, capital excluded, risk > 1% rejected, ablations build |
| Calendar | `core::TestCalendar`: Fri/Tue/Thu conventions, holiday shift, broker conflict |
| Candle aggregation | `core::TestCandles`: boundaries, late ticks, gaps, grace close, volume differencing, complete-bucket resample |
| Data validation / stale data / duplicates | `core::TestQuality` |
| Features / causality | `core::TestFeatures`: ATR value, OR hidden until complete, swings causal by truncation |
| Signals | `core::TestSetups`: sweep fires once on the trigger bar, mirrored short, too deep rejected, no reclaim, ORB acceptance + retest |
| Regime | `core::TestRegime` |
| Options / costs | `core::TestOptionsAndCosts`: strike choice, stop clamp, spread/depth/premium filters, chain parsing (good/bad), cost arithmetic |
| Risk / sizing | `core::TestRisk`: floor not round, outlay cap, cooldown, consecutive losses, daily limit, duplicates |
| Position / exits | `core::TestPosition`: gap fills at open, stop before target, single ratchet, invalidation next-open, hard flat, missing option bar |
| Engine / signal JSON | `core::TestEngine`: not-expiry, bad data, schema, missing quote, entry cutoff |
| Look-ahead / backtest integrity | `core::TestBacktestIntegrity`: truncation equality, future-price poisoning, non-expiry, limits, no-print no-fill |
| Statistics | `core::TestStatistics`: metrics, bootstrap, permutation, MC ruin, non-overlapping folds |
| Execution / guard | `execution::TestGuard`, `TestPaperBroker` |
| Kill switch / emergency square-off | `execution::TestKillSwitch`, `TestLiveReplay.test_kill_switch_squares_off_and_locks` |
| State machine | `execution::TestStateMachine`: valid cycle, no second position, KILLED terminal |
| Audit | `execution::TestAudit`: tamper detection |
| Validation gate | `execution::TestValidationGate` |
| History | `execution::TestHistory`: rolling split, conflicts, epoch resolution, round trip |
| Dhan integration (fake client) | `execution::TestDhanAdapter`: unarmed refuses entries but allows exits, account reads, feed conversion, LTT resolution, scrip master |
| Live path / reconcile | `execution::TestLiveReplay`: full entry path with a resident stop; the exit chase never has two sells live and escalates to the kill switch; a clean exit books the trade; a rogue position trips the kill switch |

Still to add in Phase 8: a recorded real Dhan session replay (live ≡ backtest decisions on the same bars); an extreme-volatility replay (a crash minute); a WS-disconnect replay with an open position.

---

## 36. Live / Paper Deployment Architecture

Stages and **exit criteria** (`validation_gate.CRITERIA`). These are written before any data is seen:

| Stage | What runs | Leave when |
|---|---|---|
| 1 Historical backtest | studies A/B/C (§27) | all reports produced; null test passes |
| 2 Walk-forward | rolling folds; OOS concatenated | ≥ 60 OOS trades · expectancy ≥ +0.15R net · P(mean ≤ 0) ≤ 10% · PF ≥ 1.2 · beats random by ≥ 0.15R with p ≤ 0.10 · expectancy excluding top 5 ≥ 0 · ≥ 0 under costs ×1.5 and slippage ×2 · max DD ≤ 12R · ≥ 70% of parameter neighbours positive · ≥ 60% of folds positive |
| 3 Live Dhan market-data simulation | real feed, `PaperBroker`, decisions logged | ≥ 4 expiry days with zero data incidents; live decisions identical to offline replay of the recorded bars |
| 4 Paper execution | same, with spread/depth recorded at every decision | ≥ 12 expiry days · ≥ 15 trades · realised slippage ≤ 1.5× model · 0 high-severity incidents · expectancy ≥ −0.25R (plumbing check) |
| 5 Tiny live | `DhanBroker(armed=True)`, **1 lot**, risk 0.25% | ≥ 30 trades · expectancy ≥ 0 · slippage ≤ 1.5× model · 0 high-severity incidents · DD ≤ 8R |
| 6 Controlled production | 0.5% risk, max 10 lots | ongoing. Any of: 20-trade DD > 8R, slippage > 1.5× model, an incident → back to stage 4 |

Realism check: at the synthetic firing rate (about 1 trade per 7 expiry days), stage 2's 60 OOS trades could need more expiry-day history than exists. **If stage 2 cannot be reached, that is the answer.** Do not lower the bar. Rely on studies A/C, or accept a no-trade engine.

Deployment: one VM on the static IP registered with Dhan. Processes:
- `live` (strategy).
- `watchdog` (broker-truth kill switch).
- systemd timers 08:55 start and 15:35 stop (as `deploy/` already does for the 945 engine).
- Daily token refresh.
- The audit log is shipped off-box after the close.

---

## 37. Signal JSON Schema

Produced by `engine.StrategyEngine._payload` for **every** decision:

```json
{
  "schema": "sensex-expiry-signal/1", "engine_version": "1.0.0", "config_hash": "b6a274f0c76a5c9a",
  "symbol": "SENSEX", "expiry": "2026-10-08",
  "signal_timestamp": "2026-10-08T09:59:00+05:30", "bar_timestamp": "2026-10-08T09:58:00+05:30",
  "data_timestamp": null, "data_age_ms": 0, "data_quality": "GOOD", "quality_reasons": [],
  "underlying_close": 81055, "regime": "FAILED_BREAKOUT", "atr": 22.961, "er": 0.0857, "compression": 0.7934,
  "action": "BUY", "setup": "S1_SWEEP_RECLAIM", "direction": "LONG",
  "level": {"name": "ORL", "price": 81005.0}, "trigger": 81055, "invalidation": 80979.26,
  "instrument": "SENSEX 2026-10-08 81100 CE", "strike": 81100, "right": "CE", "security_id": "X1",
  "option_ltp": 150.0, "bid": 149.9, "ask": 150.1, "spread_pct": 0.0013,
  "entry": 150.1, "stop_loss": 104.66, "premium_stop": 104.66, "stop_frac": 0.3028, "delta_used": 0.5,
  "target": null, "lots": 2, "qty": 40, "risk": 1930.37, "one_r_rupees": 1930.37,
  "outlay": 6008.0, "est_costs": 57.57, "room_r": 1.9144, "score": 80,
  "confirmations": {"displacement": true, "level_confluence": true, "room_2r": false,
                    "regime_aligned": true, "prime_window": true},
  "expected_R": null,
  "reason_codes": ["SWEEP_DETECTED", "LEVEL_RECLAIMED", "RISK_REWARD_VALID", "REGIME_ALIGNED",
                   "DISPLACEMENT", "LEVEL_CONFLUENCE"]
}
```

(This is the engine's real output for the hand-built sweep fixture in `tests/test_sensex_expiry_core.py`. In a backtest `data_timestamp` is null and the age is 0; live they carry the index tick receipt time and its age.)

`expected_R` stays `null` until a validated out-of-sample estimate exists. It is **never** filled with a guess. A `NO_TRADE` decision has the same envelope with its blocking reason codes.

---

## 38. Logging / Audit Architecture

`audit.AuditLog`: append-only JSONL. Each record holds `kind`, `ts`, `payload`, `prev` (the previous hash) and `hash` (SHA-256 of prev plus body). `verify()` detects any edit, deletion or reordering.

| Kind | Payload |
|---|---|
| `DECISION` | the full signal JSON (every bar, BUY or NO_TRADE): timestamp, underlying, option price, regime, features, score, setup, entry, SL, size, reasons, data quality |
| `GUARD_BLOCK` | pre-order chain reasons |
| `ORDER` | request (correlation ID, price, qty, type), broker status, order ID |
| `FILL` | fill price and qty, stop, SL order ID, 1R |
| `STOP_MODIFIED` | from → to |
| `EXIT_ORDER` | reason, price, status |
| `TRADE_CLOSED` | entry, exit, qty, reason, gross, costs, net, R gross/net, MFE R |
| `KILL_SWITCH` | trigger, square-off results |

Slippage per trade = fill − signal `entry` (logged both). Every trade is reconstructable from the `DECISION` → `ORDER` → `FILL` → `TRADE_CLOSED` chain plus the recorded bars.

---

## 39. Final Locked Constitution

# SENSEX EXPIRY ZERO→HERO ENGINE v1.0 — LOCKED CONSTITUTION

*Config hash `b6a274f0c76a5c9a` (`EngineConfig()` at v1.0.0). Any change to an article below changes the hash and voids all validation.*

**1. Market universe.**
Weekly SENSEX index options (BSE_FNO, OPTIDX) expiring **today**. Long only: BUY CE for LONG, BUY PE for SHORT. Strike = the strike nearest the trigger close (ATM). Trading only on days that are the weekly expiry by **both** the broker expiry list and the exchange calendar. No futures, no option selling, no other expiry, no other index.

**2. Data requirements.**
- SENSEX index ticks (Ticker) and Full-mode quotes for ATM ± 5 strikes.
- The option chain refreshed ≤ 90 s.
- Closed 1-minute candles built locally.
- Entry data must be GOOD: index tick ≤ 3 s old, option quote ≤ 5 s, no invalid candles, ≤ 2 missing minutes, feed connected.

**3. Trading hours.**
- Entries from the bar closing at 09:35 to the bar closing at 14:45.
- S2 entries until 12:00.
- S3 entries 13:00–14:30.
- No entries outside these windows.

**4. Regime rules.**
Regime per §7, first match wins. S1 is forbidden against a STRONG trend. S2 only in aligned STRONG, EXPANSION or UNCLEAR. S3 only in COMPRESSION, EXPANSION, RANGE or UNCLEAR.

**5. Setup rules.**
S1, S2 and S3 exactly as §15. All thresholds are in ATR14 units as listed. Triggers are close-based. Opposite-direction triggers on one bar → no trade. Same-direction priority S1 > S2 > S3.

**6. Entry rules.**
- IOC limit at ask + 2 ticks, one attempt per setup key per day.
- Room to the next opposing level ≥ 1.5 × stop distance.
- Premium ≥ ₹20; spread ≤ 2% of mid; top-5 ask depth ≥ 5 × order quantity.

**7. Stop rules.**
- Underlying invalidation (close-based) per setup.
- Exchange-resident SL-limit at premium stop = entry − |Δ| × stop distance × 1.2, clamped to [10%, 50%] of premium. A required stop > 50% → no trade.
- The stop moves only once: to entry plus costs at +1R MFE. It never widens.

**8. Exit rules.**
Every closed bar, in order: premium stop → hard flat → invalidation close → trail (after +1R, 2 ATR chandelier on underlying closes) → time stop (15 bars, MFE < 0.5R) → max hold (60 bars). Close-based exits use a marketable limit at bid × 0.97. Exit policy TRAIL unless walk-forward validation selects another of {FIXED_2R, FIXED_3R, TIME_ONLY}; that selection is recorded in the hash.

**9. Position sizing.**
- 1R = (entry − stop + 2 × 2 ticks) × qty + round-trip costs.
- Lots = ⌊0.5% × capital / 1R-per-lot⌋. Tiny-live: 0.25%.
- Capped at 10 lots, outlay ≤ 10% of capital, and outlay ≤ 3 × risk budget.
- Zero lots → no trade. Never round up.

**10. Score thresholds.**
None. The score is logged only.

**11. No-trade rules.**
Every condition in §21 blocks. NO_TRADE is the default outcome.

**12. Daily risk rules.**
- Max 2 trades.
- Max 1 open position.
- Stop for the day after 2 consecutive losses or realised ≤ −2R (counting the next trade's 1R).
- 5-minute cooldown after any exit.
- No re-entry on a traded setup key.

**13. Execution rules.**
- Orders only through the pre-order chain (§28 order: risk → position → margin → duplicate → market status → freshness → SL → order validation).
- Deterministic correlation IDs; restart reconciles before acting.
- Broker reconciliation every 5 s.
- No market orders except emergency exits.

**14. Emergency rules.**
- Kill switch independent of strategy; its triggers are listed in §33.
- On trip: stop entries, cancel all, exit all, lock until manual reset.

**15. Hard cutoff.**
All positions flat at **15:10 IST**. No position may reach expiry settlement.

**16. Logging requirements.**
Hash-chained audit of every decision, order, fill, modification, exit and kill event. `verify()` must pass daily.

**17. Validation requirements.**
Live orders only when `validation_gate.live_allowed()` passes for this exact config hash, through the stages and criteria of §36. Demotion to paper on any §22 or §36 breach.

---

## 40. Implementation Roadmap

### What must never change casually

Each item below triggers BACKTEST → WALK-FORWARD → OUT-OF-SAMPLE → PAPER → REVALIDATION, and `config_hash` enforces it mechanically:
- Entry definitions and every setup threshold.
- Stop definition and clamps.
- Exit policy.
- Risk %.
- Daily limits.
- Trading windows.
- Instrument and strike selection (moneyness).
- Spread/depth/premium filters.
- Fill and cost assumptions.
- ATR period and OR length.

Cost-rate updates (taxes) are allowed without revalidating entries, but every report must be re-costed.

| Phase | Objective | Files / modules | Tests | Acceptance | Failure | Must NOT do yet |
|---|---|---|---|---|---|---|
| 1 Data acquisition | Dhan access works; identities verified | `dhan_adapter` (scrip master, expiry list, chain, intraday, rollingoption) | `TestDhanAdapter` + a one-off live smoke script | SENSEX ID, lot, expiries and FIELDS verified against real responses; LTT convention resolved | any FIELD unverifiable → feature removed | place any order |
| 2 Historical dataset | All expiry days since May 2023 + all trading days for study A | `history.py`, fetch script, `data/sensex_expiry/` | `TestHistory`; coverage report (missing minutes per day) | ≥ 95% minute coverage on traded strikes; conflicting prints = 0 | rolling data lacks BSE or depth → study B impossible; say so | look at strategy results |
| 3 Feature engine | Causal features | `features.py`, `candles.py`, `quality.py` | `TestFeatures`, `TestCandles`, `TestQuality`, truncation | all causality tests green | any look-ahead → stop | tune thresholds |
| 4 Regime engine | Mechanical regimes | `regime.py` | `TestRegime` + regime frequency report | every regime occurs; UNCLEAR < 50% of bars | one regime dominates → simplify | use regime to pick setups by performance |
| 5 Setup research | Study A on all days | `setups.py`, `__main__ --backtest` (underlying R) | `TestSetups`, null test | at least one setup beats random on underlying R out-of-sample with p ≤ 0.10 | none beats random → **stop the project or redesign from scratch with a new hash** | add setups until one works |
| 6 Backtester | Studies B/C, walk-forward, ablation, MC, stress | `backtest.py`, `__main__` | `TestBacktestIntegrity`, `TestStatistics` | WALK_FORWARD gate (§36) | gate fails → no live trading; publish the negative result | paper trade a failed system |
| 7 Risk engine | Sizing and limits proven | `risk.py`, `costs.py` | `TestRisk`, cost reconciliation vs 3 real contract notes | costs within ±5% of contract notes | mismatch → fix model, re-cost history | — |
| 8 Dhan paper execution | Live data + paper broker | `live.py`, `execution.py`, watchdog | `TestLiveReplay`, recorded-session replay, disconnect replay | stage 3 + 4 criteria | live ≠ replay → find nondeterminism | arm `DhanBroker` |
| 9 Live monitoring | Ops readiness | watchdog process, alerts, audit shipping | kill-switch drills each Monday on paper | 4 consecutive clean drills | any drill fails | trade real size |
| 10 Tiny-capital production | Real fills at 1 lot | `DhanBroker(armed=True)` via gate | daily `verify()`, slippage report | stage 5 criteria | any breach → back to 8 | increase risk |
| 11 Controlled scaling | 0.25% → 0.5% risk, ≤ 10 lots | config change (new hash, already validated parameters) | same | 3 months of stage-6 metrics within OOS bands | DD > 8R / slippage > 1.5× → demote | exceed 0.5% risk; add instruments; add setups without full revalidation |

---

### Sources consulted (2026-10-08)

- Dhan official Python SDK `dhanhq` 2.2.0 (PyPI wheel, source read in full); Dhan support FAQ on API rate limits and expired-options data; DhanHQ v2 release notes (via search; `dhanhq.co` blocked from the research VM).
- SENSEX expiry/lot: [AlgoTest – Sensex lot size](https://algotest.in/blog/sensex-lot-size/), [Share.Market – weekly expiry days](https://www.share.market/buzz/insights/weekly-expiry-days-in-indian-fo-markets/), [Business Standard – expiry swap](https://www.business-standard.com/markets/news/nse-bids-adieu-to-thursday-expiry-as-dates-swap-come-into-effect-explained-125082800635_1.html), [Kotak – Sensex/Bankex change](https://www.kotaksecurities.com/investing-guide/articles/what-the-sensex-bankex-change-means).
- STT 2026: [ICICI Direct](https://www.icicidirect.com/ilearn/futures-and-options/articles/stt-changes-in-budget-2026-what-f-o-traders-should-know), [Zerodha support](https://support.zerodha.com/category/account-opening/resident-individual/ri-charges/articles/how-is-the-securities-transaction-tax-stt-calculated), [Groww](https://groww.in/blog/what-is-stt).
- BSE charges: [Angel One – BSE revises Sensex option fees](https://www.angelone.in/news/market-updates/bse-updates-transaction-fees-for-sensex-bankex-options).
- Contract specs: [Samco](https://www.samco.in/knowledge-center/articles/the-contract-of-bse-sensex-fno/), [BSE contract page](https://www.bseindia.com/static/markets/Derivatives/DeriReports/contractindex.aspx).
- Retail algo framework: [FYERS notice](https://fyers.in/notice-board/new-sebi-framework-for-retail-algo-trading-from-april-01-2026/), [Upstox community guide](https://community.upstox.com/t/a-guide-for-our-api-algo-partners-and-clients/11826).
- Dhan limits: [Dhan – API rate limits](https://dhan.co/support/platforms/dhanhq-api/what-are-the-api-rate-limits-for-dhan/), [Dhan – expired options data](https://dhan.co/support/platforms/dhanhq-api/do-dhan-provides-expired-options-data-via-the-api-s/).
