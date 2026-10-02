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

## 945.py -- 09:45 intraday selector (model 945-V1)

`945.py` reads the existing PSYGRID public feed (`/public/live.json`, optional
`/public/nifty.json`). It never writes to PSYGRID and opens no listening port.

### Automatic daily lifecycle (no manual archive needed)

Install once:

* Windows PC: `powershell -ExecutionPolicy Bypass -File deploy\windows\install_945_task.ps1`
  (weekdays 09:05, console window visible, catches up if the PC was off).
* Linux VM next to PSYGRID: `sudo bash deploy/systemd/install.sh` (timer weekdays
  09:05 IST, reads `127.0.0.1:10000`, CPU 50% / RAM 512 MB / idle-IO caps, no port).

Or simply run `python 945.py` any time on a trading day. It then runs the whole day:

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
