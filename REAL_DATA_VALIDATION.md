# SENSEX Expiry Engine — Real-Data Baseline Validation

**Status, 2026-10-08: the real-data baseline has NOT been produced.** The data could not be obtained
from the research environment (§1). Nothing in this file or in `reports/` is a real-data result.
No option price, expiry date or index minute has been invented to fill the gap.
**Live orders remain disabled**: `validation_gate.live_allowed()` refuses because no validation report exists.

---

## 1. Why there is no baseline yet

| Requirement | Status in this environment |
|---|---|
| Dhan REST (`api.dhan.co`) | **Blocked**: the environment's network policy returns HTTP 403 on CONNECT |
| Dhan scrip master (`images.dhan.co`) | **Blocked** (403) |
| BSE (`bseindia.com`, `api.bseindia.com`) | **Blocked** (403) |
| Yahoo Finance, Kaggle, HuggingFace | **Blocked** (403) |
| Dhan credentials | **Not present** (no `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` in the environment) |
| PyPI, GitHub | reachable (used only to read the official `dhanhq` SDK source) |

To run the baseline, either:
- **(a)** run the three commands in §3 on a machine that can reach Dhan (your own PC or VPS), or
- **(b)** allow `api.dhan.co` and `images.dhan.co` in this cloud environment's network settings, and add the two Dhan credentials as environment secrets. Then I can run it here.

Unofficial GitHub/Kaggle dumps of "SENSEX option data" were deliberately **not** used. Their provenance, survivorship and timestamp conventions cannot be verified, which is exactly the failure this validation exists to prevent.

---

## 2. Data sources and what each one can and cannot provide

| Need | Source | Status | Missing fields → treatment |
|---|---|---|---|
| SENSEX 1-min underlying | Dhan `POST /charts/intraday` (`IDX_I`, `INDEX`), 60-day request windows | Supported by SDK 2.2.0. History depth: verify on first fetch | No volume (an index has none). Not used by the strategy |
| Prior-day H/L/C, trading calendar | Dhan `POST /charts/historical` (daily) | Supported | Holidays = weekdays with no daily bar (data-derived, no assumption) |
| Weekly option 1-min OHLC, OI, IV, spot | Dhan `POST /charts/rollingoption` (`BSE_FNO`, `OPTIDX`, `WEEK`, ATM−10…ATM+10, CALL/PUT, 28-day windows) | Supported by SDK 2.2.0. **BSE_FNO coverage and depth must be confirmed by the first fetch** | Rolling moneyness → rebuilt into fixed strikes from the per-minute `strike` field (`history.split_rolling`). Conflicting prints void that day's options |
| Which `expiry_code` = the expiring weekly | Data-detected (`realdata.detect_expiry_code`): the ATM option's time value at the expiry close must be ≈ 0, and the day before > ₹30 | Automatic | If no code passes, the option fetch stops: nothing is assumed |
| Correct expiry dates | Exchange convention (Fri to 2024-12-31, Tue 2025-01-01…2025-08-31, Thu from 2025-09-01), holiday-shifted with the data-derived calendar, **then confirmed from option decay per day** | Automatic | Calendar/data disagreement → day logged and excluded from Test B |
| Strike mapping | Per-minute `strike` field of the rolling series | Automatic | — |
| Contract specs | Strike step 100, tick ₹0.05. Lot 10 until 2024-12-31, 20 after (**verify the change date**) | Config + `realdata.LOT_SCHEDULE` | Lot size only affects rupee sizing and fixed brokerage, not R of the signal |
| **Bid/ask history** | **Dhan does not provide historical bid/ask** | — | **Modelled**: 0.5% of premium round trip + 2 ticks of slippage per side, stressed ×2 in the report. If this assumption is challenged, the only legitimate remedies are recording live spreads during the paper stage, or a paid tick vendor (not used by default) |

Raw responses are cached verbatim in `raw/<kind>/<hash>.json` with their request parameters. The dataset build reads only that cache, so the whole chain is reproducible offline and auditable.

---

## 3. How to run the baseline (no parameter can be passed that changes the strategy)

```bash
pip install dhanhq==2.2.0
export DHAN_CLIENT_ID=...  DHAN_ACCESS_TOKEN=...          # Data API add-on required
python -m sensex_expiry.realdata fetch --from 2023-05-15 --to 2026-10-07 --raw data/sensex_expiry/raw
python -m sensex_expiry.realdata build --raw data/sensex_expiry/raw --out data/sensex_expiry/days
python -m sensex_expiry --baseline data/sensex_expiry/days --out reports/baseline --workers 4 --n-null 200
```

To give Test A more sessions, `--from` may start earlier: index minutes back to the earliest date Dhan serves, typically about 5 years (verify). Option windows before May 2023 simply return nothing. The fetch makes about 2,000 requests at ≤ 4/s, roughly 10 minutes, and is resumable. Test A over ~850 sessions takes about 30 min on 4 workers.
Outputs:
- `reports/baseline/VALIDATION_REPORT.md`
- `validation_report.json`
- the complete trade lists: `trades_testA_underlying.csv`, `trades_testB_options.csv`, `trades_control_C5_simple_atm_buy.csv`
- `data/sensex_expiry/days/_build_report.json` (exclusions and expiry-label conflicts)

The strategy that runs is `EngineConfig()` exactly as committed, config hash **`b6a274f0c76a5c9a`**. The report records the hash; any other hash means something was changed.

---

## 4. What the baseline report contains (all pre-registered, fixed before any real data)

| Item | Definition |
|---|---|
| TEST A — all trading days | Locked signal engine on every session. Expiry gate bypassed by design; option gates not applicable. R = index points / setup stop distance. Exits are the locked order translated to the index: intrabar stop at 1.2× stop distance (the premium stop's delta translation) until +1R, then breakeven; invalidation close; 2-ATR trail after +1R; 15-bar time stop; 60-bar max; flat 15:10. **No costs: it measures information, not money** |
| TEST B — expiry days, options | Full locked engine, ATM weekly option, next-bar-open fills + modelled spread + slippage, 2026 statutory costs |
| TEST C | S1, S2, S3 separately, for A and B |
| TEST D | Signal time in 09:15–09:30, 09:30–10:00, 10:00–11:00, 11:00–12:00, 12:00–13:00, 13:00–14:00, 14:00–14:30, 14:30–15:10. Windows before 09:35 and after 14:45 show 0 trades **by construction** (the locked rules forbid entries there) |
| TEST E | Regime at signal: Trend (STRONG_BULL/BEAR), Range, Compression, Expansion, Reversal (REVERSAL + FAILED_BREAKOUT), Unclear |
| Metrics per test | trades, win rate, avg win/loss, expectancy, PF, total R, max DD, avg MAE, avg MFE, avg hold, largest winner/loser, max losing streak, costs ₹, slippage ₹, gross and net expectancy |
| Controls | C1 random entry · C2 random direction · C3 random entry, same holding period · C4 random entry, same stop · C5 simple ATM option buy · C6 simple ORB · C7 simple prior-day breakout · C8 buy-and-hold (index, R = 1.5 ATR; % also shown). Random controls are repeated 200× and the strategy mean is ranked within that null distribution |
| Significance | t-CI, bootstrap 95% CI, P(mean > 0), Cohen's d, sign-flip randomization p, trades needed for +0.15R at 80% power |
| Multiple testing | Family = {A, B} × {S1, S2, S3, ALL}, all reported. Holm (FWER) and Benjamini–Hochberg (FDR) adjusted p. **Nothing is selected** |
| Holdout | No parameter is fitted, so every real day is out-of-sample for the pre-registered rules. A chronological 70/30 split is still reported as a stability check |
| Look-ahead audit | On real days: truncation equality; future option-price poisoning; future index poisoning; Test A truncation equality; prior day strictly before the session; every entry after its signal bar. **Any failure voids the verdict** |

**Decision rule** (`validation.verdict`, written before data):
- **YES** requires all of:
  - audit passes;
  - ≥ 60 option trades;
  - bootstrap 95% CI of net R entirely > 0;
  - Holm-adjusted p < 0.05 for B:ALL **and** A:ALL;
  - beats the simple ATM-buy control with p < 0.05.
- **NO** if either:
  - with ≥ 30 option trades, the CI lies entirely < 0;
  - with ≥ 100 underlying trades, the Test A CI lies entirely < 0.
- **INCONCLUSIVE** otherwise. The report then states the trade count needed.

---

## 5. If the baseline loses money: diagnosis before any change

| Question | Where the report answers it |
|---|---|
| Is the entry wrong? | Test A vs C1/C2/C4. Strategy mean not above the random-entry and random-direction nulls → entries carry no information |
| Is the exit wrong? | MFE vs realised R; exit-reason breakdown; time-stop share. Large MFE with small realised R → exits leak edge. A shows positive MFE but negative R → exits |
| Is the instrument wrong? | Test A positive but Test B negative → the edge exists on the index but the option does not pay for it (theta, spread, IV) |
| Is option selection wrong? | B trades: share of premium-stop exits where the index never reached invalidation (the option stopped out on noise or IV) |
| Is it regime-dependent? | Test E. An edge confined to one regime with few trades is not an edge |
| Are costs killing it? | B gross vs net expectancy; costs ₹ and slippage ₹ per trade vs 1R |
| Is the sample too small? | `n_needed_for_0.15R_80pct_power` vs actual n |
| Is the concept wrong? | A ≤ nulls **and** B < 0 **and** C5/C6/C7 do no worse → the concept adds nothing |

Only after that diagnosis may a change be proposed. Any change is then:
- a new config hash, voiding the baseline;
- tested as a **neighbourhood**, e.g. sweep penetration ∈ {0.10, 0.15, 0.25}, reclaim bars ∈ {3, 4, 5}, rejection ∈ {0.5, 0.75, 1.0};
- accepted only if ≥ 70% of neighbours stay positive out-of-sample (walk-forward), with every variant tried counted in the multiple-testing family.

A change that works only at one exact value is flagged **fragile** and rejected.

---

## 6. Synthetic pipeline check (machinery only, NOT evidence)

`python -m sensex_expiry --validate-synthetic 60 --n-null 100 --workers 4` was run on 60 synthetic expiry days plus 120 synthetic non-expiry days. These are random walks with Black-Scholes-priced options, so **by construction there is no edge**. Full output: `reports/synthetic_pipeline_check/`. What it shows:

1. **The machinery works end to end.**
   - The look-ahead audit passes on all six checks.
   - Tests A–E, all eight controls, the null distributions and the multiple-testing table are produced.
   - The fetch → build path reproduces every fixed-strike option print exactly from Dhan-shaped rolling responses (`tests/test_sensex_expiry_validation.py`).
2. **It shows why a small sample cannot be trusted. On pure noise:**

   | | n | mean R | 95% bootstrap CI | PF | trades needed for +0.15R @ 80% power |
   |---|---|---|---|---|---|
   | Test A, all days (underlying) | 54 | +0.11 | [−0.25, +0.49] | 1.23 | 560 |
   | Test A, expiry days only | 19 | +0.20 | [−0.38, +0.93] | 1.54 | 636 |
   | **Test B, expiry options** | **8** | **+0.89** | **[−0.30, +2.68]** | **5.47** | **1,681** |

   Eight random-walk option trades produced a **+0.89R mean and a profit factor of 5.5**. That is exactly the kind of "excellent backtest" this process exists to reject.
   - The strategy did not beat any control: randomization p = 0.19–0.35 on A, 0.14 on B.
   - Holm-adjusted p = 1.0 everywhere.
   - The pre-registered rule returned **INCONCLUSIVE**, as it should.
3. **Firing rate** (rules unchanged): 0.30 underlying signals per session, and **0.13 option trades per expiry day**. The option gates remove most signals: premium stop > 50%, room, spread, and the needed strike not being available.


---

## 7. The answer to "does the strategy show a genuine edge on unseen SENSEX data?"

**INCONCLUSIVE — insufficient evidence. In fact there is no real-data evidence at all yet,** because the data could not be fetched from this environment (§1). A real-data answer requires the §3 run.

**What the real run can and cannot decide.** The projections below use the firing rates and R dispersion from §6. Those are synthetic estimates; the real run reports its own numbers.

| | Expected real sample | Smallest edge detectable (80% power, one-sided 5%) | Trades needed to detect +0.15R |
|---|---|---|---|
| Test A, all ~850 sessions since May 2023 | ≈ 255 underlying trades | ≈ +0.22R | ≈ 560 → ≈ 1,900 sessions ≈ 7.5 years |
| Test B, ~170 weekly expiries since May 2023 | ≈ 22 option trades | ≈ +1.3R (no realistic edge is this large) | ≈ 1,680 → ≈ 13,000 expiry days |

**Plain conclusion, before any real data:**
- **Test B by itself can never return YES within the history that exists.** At the locked rules' selectivity, SENSEX weekly-expiry history holds roughly 20–25 option trades, and a realistic edge (+0.1R to +0.3R) needs 400–1,700.
- **Test B can return NO.** If the option CI on ≥ 30 trades lies entirely below zero, the strategy is rejected regardless.
- **Test A is the only test with enough data to show the entries carry information.** It can resolve an edge of about +0.2R in the index signal. If Test A fails against the random controls, the strategy has no demonstrated edge, and the right action is to stop, not to tune.

Additional data needed for a YES under the pre-registered rule:
- At least **60** live or historical expiry-day option trades for the first gate. At ~0.13 trades per expiry day, that is about **460 expiry days ≈ 9 years**.
- Plus enough trades for the CI to clear zero at the true effect size: about 420 trades at +0.3R, 1,680 at +0.15R.

If the real Test A shows a clear edge (Holm p < 0.05 vs the random-entry null), the honest path forward is not to wait nine years. It is to bring the validation sample up to that size from **non-expiry days with the nearest weekly option**, as a separate, pre-registered study (spec §27, Study C). That is a new hypothesis with a new config hash, not a tweak of this one.

