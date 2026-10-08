# SENSEX Expiry Engine — Live Dashboard

A local browser dashboard for the running engine. During market hours you never touch a
terminal: the engine starts by itself at 08:50, starts **DISARMED**, and stops itself at 15:40.
Everything happens in the browser.

```
python -m sensex_expiry.session --feed dhan --mode paper     # live Dhan data, paper orders
python -m sensex_expiry.session --feed dhan --mode live      # live Dhan data, real orders once ARMED
python -m sensex_expiry.session --feed replay --day data/sensex_expiry/days/2026-10-08.json --speed 30
```

Open **http://127.0.0.1:8765**. The URL is also written to `<state-dir>/dashboard_url.txt`, and `--open-browser` opens it for you.

## What it shows (live, ~2 updates/second over Server-Sent Events)

| Panel | Contents |
|---|---|
| Top bar | Mode (PAPER/LIVE), REPLAY marker, ARMED/DISARMED, ENTRIES PAUSED, kill switch, feed up/down, data GOOD/BAD, expiry day or not, engine clock, config hash |
| SENSEX | Last price, last tick time and age |
| Engine state | State-machine state (WAITING … POSITION_MANAGEMENT … KILLED / DAY_DONE), since when and why |
| Regime | Regime at the last decision, 1-min ATR |
| Day P&L | Total, realised (₹ and R), open, trade count |
| Chart | Today's 1-minute closes with PDH/PDL/PDC/ORH/ORL. With a position open, it also shows the entry trigger and the invalidation level. Hover crosshair, plus a table view |
| Position | Contract, qty, entry, mark (bid), LTP, premium stop (current and initial), invalidation, target (none: TRAIL), 1R, unrealised ₹/R, MFE, bars held, pending exit |
| Active setup / last decision | Setup, direction, level, trigger/invalidation, option, entry/SL, target, lots/1R, score (logged only), reason codes |
| Risk usage | Trades used, daily loss used (R), consecutive losses, open positions, risk per trade, kill-switch loss, cooldown |
| Data & connection | Data quality and reasons, index/option tick age vs limits, missing minutes, late/duplicate/out-of-order/quarantined ticks, feed status, reconnects, last disconnect, broker responding |
| Orders | Protective stop order and status, exit order and chase attempt, last order event, every open order at the broker |
| Reason codes today | Count per code across all decisions |
| Trades today | Every closed trade: times, setup, contract, qty, entry/exit, net ₹, R, MFE, exit reason |
| Audit log | The complete hash-chained audit file: newest first, filter by kind, page back through everything, raw JSON per record, chain-integrity check every 30 s |
| Control history | Every control action and its result, plus the last state transitions |

When the page has received nothing for more than 3.5 s, a red banner says the view is **stale**. The engine itself does not depend on the page.

## Controls (the only writable actions)

| Button | What it does | Confirmation |
|---|---|---|
| **ARM** | Lets the engine place orders autonomously. **PAPER:** paper orders. **LIVE:** real Dhan orders | Type `ARM PAPER`, or `ARM <config-hash>` in live mode. **Refused** if any blocker holds: live validation gate not passed for this config hash; kill switch tripped; engine KILLED/DAY_DONE; feed down; no SENSEX tick in 5 s; past 14:45; not an expiry day. The blockers are listed in the dialog |
| **DISARM** | No new entries. An open position stays protected by its exchange stop and is still managed and exited (a SELL is always allowed) | click |
| **PAUSE NEW ENTRIES / RESUME** | Same as DISARM for entries, but keeps the arm. Skipped signals are logged as `ENTRIES_PAUSED` | click |
| **EMERGENCY SQUARE-OFF** | Trips the kill switch: cancels all orders, exits all positions, disarms, **locks** the engine. Reset by deleting `KILL_SWITCH.lock` and restarting. If the engine loop is hung, the server sends the square-off to the broker directly | Type `SQUARE OFF` |

Once armed, the strategy is fully autonomous: closing the browser changes nothing. The arm is cleared automatically by a kill-switch trip, by end of day, and by any restart (**a session always starts DISARMED**).

## Security

The dashboard can arm a live trading engine, so it is locked down:
- **Network:** binds `127.0.0.1` only. Binding elsewhere needs `--allow-remote`, and then the control token is not embedded in the page (it is in `<state-dir>/dashboard_token.txt`, mode 0600). For a VPS, use an SSH tunnel: `ssh -N -L 8765:127.0.0.1:8765 <vps>`.
- **Request checks:** the Host header must match (blocks DNS rebinding). Controls need a same-origin `Origin`, a JSON body and the per-session token (`X-Control-Token`). There are no CORS headers.
- **Page hardening:** strict CSP (no inline script, no third-party resources: the page works offline), and at most 10 control requests per minute.
- **Isolation:** controls run on the engine thread, the only thread that touches trading state. Every control is written to the hash-chained audit log.

## Automatic start (no terminal)

- **Windows:** `powershell -ExecutionPolicy Bypass -File deploy\windows\install_sensex_expiry_task.ps1 -Mode paper` starts at 08:50 on weekdays and opens the dashboard.
- **Linux VPS:** `sudo bash deploy/systemd/install_sensex_expiry.sh`. Credentials go in `/etc/sensex-expiry.env`.

Live mode stays useless until `validation_gate_report.json` passes for the running config hash. **That is intentional.** The progression is backtest → walk-forward → out-of-sample → live-data replay → paper → tiny capital, and the ARM button enforces it.

## Rehearse before the market opens

Replay any built day file at speed to practise ARM, PAUSE and SQUARE-OFF with paper orders:
`python -m sensex_expiry.session --feed replay --day <day.json> --speed 30`. Replays are paper-only. The replay drives the **same** runner, guard and position manager as live trading. It reproduced the backtest's trade exactly in testing: same entry minute, exit minute and exit reason.
