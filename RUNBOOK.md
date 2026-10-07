# Tomorrow Morning Runbook

## 1. Tonight

From the repository folder:

```text
python bbbbb.py --self-test
```

Expected ending:

```text
OK
```

Then run:

```text
python bbbbb.py --preflight-only
```

This must report:

```text
A-shard OK (45/45) | FULL UNIVERSE OK (450/450)
```

If preflight fails, fix the feed/network before the market opens.

## 2. Tomorrow

Start the engine before 09:15 IST:

```text
python bbbbb.py
```

The engine waits for 09:15 automatically.

## 3. Live sequence

### 09:15

Acquisition starts. Every cycle requests all ten 45-stock Psygrid shards.

### 09:15–09:29

The engine validates each stock's LTP, timestamp, previous close and all 15 opening candles.

### 09:30

The engine emits the first beep and freezes the latest verified complete 450-stock snapshot.

The 09:30 candle itself is NOT used.

### 09:30–09:31

Candidates are locked. No re-ranking is performed after the freeze.

Selection order:

1. Strict candidate.
2. Fallback Tier 2.
3. Fallback Tier 3.

Hard exclusions remain active through all tiers.

### 09:31

The engine obtains a fresh LTP for the selected stock, calculates the final executable entry, stop and target, emits the final beep and prints:

```text
STATUS: SIGNAL_READY
```

## 4. If a signal is wrong

That is a strategy-quality problem and can be improved through backtesting.

## 5. If the engine stops

The terminal will show the exact stage:

- PREFLIGHT
- ACQUISITION
- FREEZE
- CANDIDATE_LOCKED
- ENTRY_PRICE
- SIGNAL_READY
- FATAL_*

Do not manually modify the code during the 09:15–09:31 window.

## 6. Audit

Every major event is written to:

```text
engine_audit.jsonl
```

This makes tomorrow's run reproducible and debuggable.

## 945.py -- continuous scan (default)

Open PowerShell in the repo folder before 09:30 and run:

```powershell
python 945.py                      # Tier 1 alerts only
python 945.py --risk-rupees 1000   # also print a share quantity for Rs 1,000 risk
python 945.py --allow-tier2        # also alert on Tier 2
```

It waits for 09:30, then every minute (3 s after the candle closes) it fetches the whole
feed, rescans every stock in both directions with the 945 model, and prints one line:
the market regime, the top three on the shortlist with how many scans in a row they have
held, and why nothing is being signalled yet. It beeps three times and prints a full trade
plan only when ALL of these hold:

1. score >= 85 (Tier 1; `--tier1` changes it, +3 between 12:00 and 13:30),
2. the stock stayed in the top 10 for 3 scans in a row,
3. it trades with the market (regime UP -> longs only, DOWN -> shorts only),
4. it has not already moved 7% from the previous close in that direction,
5. it is before 14:45,
6. the stop sits within 3 ATR and the 2R target is still worth >= 1.5R after
   ~0.14% round-trip costs (fees, STT, stamp, GST and slippage on a Rs 1 lakh order).

The plan is a stop-entry order on the break of the last candle (cancel after 2 candles),
a stop beyond the last 5 candles' swing, a 2R target and a 60-minute time stop (square-off
15:15). The engine then follows the trade and beeps once on fill / expiry / target / stop /
time stop. One trade at a time, never the same stock and direction twice a day, at most 3
signals a day, and no more signals after 2 losing trades.

Everything is written to `data/scan/YYYY-MM-DD.jsonl` (every event) and
`data/scan/trades.csv` (one row per trade, gross and net R). Restarting the same day
resumes from the journal and never repeats a signal. After the close it waits for 15:31
and saves the full session to `data/sessions/` (skip with `--no-archive` or Ctrl+C).

Before trusting a threshold, replay saved days through the exact same logic:

```powershell
python 945.py --scan-replay data\sessions            # totals net of costs
python 945.py --scan-replay data\sessions --verbose  # every scan line and signal
python 945.py --scan-replay data\sessions --tier1 80 # what a looser threshold would have done
```

Judge it on 50+ filled trades, not on one day. Changing `--tier1` after looking at the
same days you replayed is curve fitting: pick it on older sessions, check it on newer ones.

## 945.py --daemon -- 09:45 research decision (model 945-V1)

`945.py` reads the existing PSYGRID public feed (`/public/live.json`, optional
`/public/nifty.json`). It never writes to PSYGRID and opens no listening port.

### Automatic daily lifecycle (no manual archive needed)

Install once:

* Windows PC: `powershell -ExecutionPolicy Bypass -File deploy\windows\install_945_task.ps1`
  (weekdays 09:05, console window visible, catches up if the PC was off).
* Linux VM next to PSYGRID: `sudo bash deploy/systemd/install.sh` (timer weekdays
  09:05 IST, reads `127.0.0.1:10000`, CPU 50% / RAM 512 MB / idle-IO caps, no port).

Or run `python 945.py --daemon` any time on a trading day. It then runs the whole day:

| Time (IST) | Step | Stored |
|---|---|---|
| 09:45:03 | validate feed (trading day, today's session, feed clock age, 09:44 candle, valid universe) -> freeze candles < 09:45 -> rank all stocks -> publish exactly one -> beep | decision + full feature matrix + full ranking + frozen input file (one transaction) |
| 09:51 / 10:01 / 10:16 | +5 / +15 / +30 min outcomes (return, MFE/MAE and their minute, hit, time-to-move) | `outcomes`, universe forward returns, feature IC |
| after +30 | daily report | `data/reports/YYYY-MM-DD.txt`, `data/reports/daily_summary.csv` |
| 15:31 | full-session archive | `data/sessions/YYYY-MM-DD.json.gz` |

If the feed fails validation, NO decision is published (retries until 15:25, every
attempt logged in `feed_checks`). Restarting at any time resumes the day; nothing is
ever duplicated or changed.

### Commands

| Command | Purpose |
|---|---|
| `python 945.py --status` | today's state; exit code 2 if a step is overdue (monitoring) |
| `python 945.py --verify [--date D]` | re-decide from the stored frozen input; fingerprints must match |
| `python 945.py --show [--date D]` | stored decision + outcomes |
| `python 945.py --research` | cumulative research report (features, regimes, liquidity, direction, horizon, concentration, stability, score vs outcome, calibration) |
| `python 945.py --backtest data/sessions` | walk-forward replay of archived sessions (separate `data/backtest_945.sqlite`) |
| `python 945.py --decide-only` / `--evaluate` / `--archive` | individual lifecycle steps |
| `python 945.py --benchmark` / `--self-test` | 989-stock integrated benchmark / tests |

### Integrity guarantees

* Decision, feature-matrix, ranking, frozen-input, outcome and universe-forward rows are
  protected by SQLite triggers (no UPDATE / DELETE) and a unique (date, mode, model_id) key.
* Every decision stores model id `945-V1`, feature version, config hash, weights hash,
  sector-map hash, input fingerprint and decision fingerprint.
* Score (0-100) is a ranking score, never a probability. Probability is
  `UNCALIBRATED_HEURISTIC` until 60 out-of-sample outcomes exist; afterwards it is
  estimated ONLY from earlier sessions (`EMPIRICAL_WALK_FORWARD`).
* 945-V1 weights (`intelligence/selector_weights_v1.json`) are hand-set priors. Do not
  tune them on the first sessions; use `--research` after 60+ sessions.
* Sector features exist only for symbols in `sector_map.json` (pluggable via
  `--sector-map`); coverage is printed with every decision, unclassified stocks stay null.
