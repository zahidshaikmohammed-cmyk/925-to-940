"""Grade every signal the engine logged today against what the market did next.

    python grade_signals.py                 # grade today's signals from engine_audit.jsonl
    python grade_signals.py --base-url URL  # another PSYGRID feed

Run it before 15:15 IST: the Live Core clears its candles at the session end.

Each logged scan (one per bbbbb.py run, de-duplicated by symbol/side/candle) is
replayed exactly as printed:
  * fill: the stop-entry trigger must trade within ``trigger_valid_candles``
    candles, else NOT_FILLED (0R);
  * then, minute by minute: stop -> LOSS (-1R), target -> WIN, the +0.5R
    checkpoint missed -> CHECKPOINT_EXIT at that candle's close, the time stop
    -> TIME_STOP at the close; still running -> OPEN, marked at the last close.
  Whenever one candle touches both the stop and the target (or the stop in the
  fill candle) the stop is assumed first: the grade is never flattering.

Results are merged into graded_signals.jsonl (your own trade journal, kept on
your PC) and summarised by status, tier and conviction band, so the thresholds
can be set from measured results instead of guesses.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

from psygrid_client import PsygridClient
from run_engine import LIVE_CORE_URL, now_ist
from config import StrategyConfig
from psygrid_client import LIVE_CORE_UNIVERSE
from strategy_930 import Candle

GRADED_EVENTS = {
    "SIGNAL_READY", "NO_PRECISION_SETUP", "NO_NEW_ENTRIES",
    "LOW_CONFIDENCE_FORCED", "LOW_CONFIDENCE_WEAK",
}


def load_signals(path: Path, day: str) -> list[dict]:
    """Today's graded-event records, one per (symbol, side, latest candle)."""
    seen: dict[tuple, dict] = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") not in GRADED_EVENTS or not rec.get("trigger") or not rec.get("selected"):
            continue
        if not str(rec.get("scan_time", "")).startswith(day):
            continue
        sel = rec["selected"]
        key = (sel["symbol"], sel["side"], rec.get("selected_latest_candle"))
        seen.setdefault(key, rec)  # first scan that printed it
    return list(seen.values())


def simulate(rec: dict, candles: Sequence[Candle], trigger_valid_candles: int) -> dict:
    sel = rec["selected"]
    side = -1 if sel["side"] == "SHORT" else 1
    trigger, stop = float(rec["trigger"]), float(sel["stop"])
    target = float(rec.get("trigger_target") or sel["target"])
    risk = abs(trigger - stop)
    timing = rec.get("exit_timing") or {}
    checkpoint_minutes = timing.get("checkpoint_minutes")
    checkpoint_price = timing.get("checkpoint_price")
    time_stop_minutes = timing.get("time_stop_minutes")
    last_seen = datetime.fromisoformat(rec["selected_latest_candle"])
    after = [c for c in candles if c.ts > last_seen]
    base = {"symbol": sel["symbol"], "side": sel["side"], "status": rec["event"], "tier": sel["tier"],
            "setup_score": sel["score"], "conviction": rec.get("conviction"),
            "persistence": (rec.get("persistence") or {}).get("score"),
            "scan_time": rec.get("scan_time"), "trigger": trigger, "stop": stop, "target": target}
    if risk <= 0:
        return {**base, "outcome": "INVALID", "r": 0.0}

    def r_at(price: float) -> float:
        return side * (price - trigger) / risk

    fill = None
    for i, c in enumerate(after[:trigger_valid_candles]):
        if side * ((c.high if side == 1 else c.low) - trigger) >= 0:
            fill = i
            break
    if fill is None:
        state = "NOT_FILLED" if len(after) >= trigger_valid_candles else "PENDING"
        return {**base, "outcome": state, "r": 0.0}

    fill_bar = after[fill]
    mfe = mae = 0.0
    if side * ((fill_bar.low if side == 1 else fill_bar.high) - stop) <= 0:
        return {**base, "outcome": "LOSS", "r": -1.0, "fill_time": fill_bar.ts.isoformat(), "minutes": 0,
                "mfe_r": 0.0, "mae_r": -1.0}
    reached_checkpoint = checkpoint_price is None
    for j, c in enumerate(after[fill + 1:], 1):
        best = c.high if side == 1 else c.low
        worst = c.low if side == 1 else c.high
        mfe, mae = max(mfe, r_at(best)), min(mae, r_at(worst))
        common = {"fill_time": fill_bar.ts.isoformat(), "minutes": j, "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}
        if side * (worst - stop) <= 0:
            return {**base, **common, "outcome": "LOSS", "r": -1.0}
        if side * (best - target) >= 0:
            return {**base, **common, "outcome": "WIN", "r": round(r_at(target), 3)}
        if not reached_checkpoint and side * (best - float(checkpoint_price)) >= 0:
            reached_checkpoint = True
        if not reached_checkpoint and checkpoint_minutes is not None and j >= checkpoint_minutes:
            return {**base, **common, "outcome": "CHECKPOINT_EXIT", "r": round(r_at(c.close), 3)}
        if time_stop_minutes is not None and j >= time_stop_minutes:
            return {**base, **common, "outcome": "TIME_STOP", "r": round(r_at(c.close), 3)}
    last = after[-1]
    return {**base, "outcome": "OPEN", "r": round(r_at(last.close), 3), "fill_time": fill_bar.ts.isoformat(),
            "minutes": len(after) - fill - 1, "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}


def band(conviction) -> str:
    if conviction is None:
        return "n/a"
    lo = int(conviction // 10) * 10
    return f"{lo}-{lo + 10}"


def summarise(results: list[dict]) -> list[str]:
    lines = []
    for title, key in (("BY STATUS", "status"), ("BY TIER", "tier"), ("BY CONVICTION", "band")):
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in results:
            groups[str(band(r.get("conviction")) if key == "band" else r[key])].append(r)
        lines.append(f"\n{title}:")
        for name in sorted(groups):
            rows = groups[name]
            filled = [r for r in rows if r["outcome"] not in ("NOT_FILLED", "PENDING", "INVALID")]
            wins = sum(1 for r in filled if r["r"] > 0)
            total = sum(r["r"] for r in filled)
            hit = f"{100 * wins / len(filled):.0f}%" if filled else "-"
            avg = f"{total / len(filled):+.2f}R" if filled else "-"
            lines.append(f"  {name:<22} signals={len(rows):<3} filled={len(filled):<3} winning={hit:<5} "
                         f"avg={avg:<7} total={total:+.2f}R")
    return lines


def merge_journal(path: Path, results: list[dict]) -> None:
    rows: dict[tuple, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                rows[(row["symbol"], row["side"], row["scan_time"])] = row
            except (ValueError, KeyError):
                continue
    for r in results:
        rows[(r["symbol"], r["side"], r["scan_time"])] = r
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows.values()), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade today's logged signals against the following candles")
    parser.add_argument("--base-url", default=LIVE_CORE_URL)
    parser.add_argument("--expected-universe", type=int, default=LIVE_CORE_UNIVERSE)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--audit", default="engine_audit.jsonl")
    parser.add_argument("--journal", default="graded_signals.jsonl")
    args = parser.parse_args(argv)

    now = now_ist()
    signals = load_signals(Path(args.audit), now.date().isoformat())
    if not signals:
        print(f"No signals logged today in {args.audit}. Run bbbbb.py during the session first.")
        return 0
    client = PsygridClient(args.base_url, timeout=args.timeout, expected_universe=args.expected_universe)
    try:
        raw = client.market()
    except Exception as exc:
        print(f"Feed unavailable: {exc}")
        return 30
    cfg = StrategyConfig()
    results = []
    for rec in signals:
        payload = raw.get(rec["selected"]["symbol"])
        candles = client.stock(rec["selected"]["symbol"], payload, now).candles if payload else ()
        results.append(simulate(rec, candles, cfg.trigger_valid_candles))

    print(f"GRADED {len(results)} signal(s) logged on {now.date()} (as of {now:%H:%M} IST)\n")
    for r in sorted(results, key=lambda x: x["scan_time"]):
        conv = f"{r['conviction']:.0f}" if r.get("conviction") is not None else "-"
        print(f"  {r['scan_time'][11:16]} {r['symbol']:<14} {r['side']:<5} {r['status']:<22} conv={conv:<3} "
              f"-> {r['outcome']:<15} {r['r']:+.2f}R"
              + (f" in {r['minutes']} min" if r.get("minutes") is not None else ""))
    for line in summarise(results):
        print(line)
    merge_journal(Path(args.journal), results)
    print(f"\nJournal updated: {args.journal}. Small samples mean nothing: judge after 50+ SIGNAL_READY trades.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
