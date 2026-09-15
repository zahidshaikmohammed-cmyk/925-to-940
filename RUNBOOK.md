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
