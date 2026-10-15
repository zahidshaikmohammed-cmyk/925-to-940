"""TEN STRATEGY ENGINES: scan the PSYGRID 989-stock universe every minute, alert 90+ signals.

    python strategy_engines.py                 # run today's session (waits for 09:16), Telegram alerts
    python strategy_engines.py --once          # one scan now, then exit
    python strategy_engines.py --engines Regime_Filtered_ORB_Engine,VWAP_Reclaim_Rejection_Engine
    python strategy_engines.py --no-telegram   # print alerts only
    python strategy_engines.py --telegram-test # send one test message and exit
    python strategy_engines.py --replay data/sessions/2026-10-13.json.gz [--verbose]
    python strategy_engines.py --list          # the ten engines and their trading windows
    python strategy_engines.py --self-test

Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment.
Journals: data/engines/YYYY-MM-DD/{scans.jsonl,ledger.json,universe.json}.
Session archive (for tomorrow's PDH/PDL/PDC and opening-volume baselines): data/sessions/.
Exit codes: 0 done, 10 self-test failed, 20 not a trading day, 23 session over, 30 feed error (--once),
33 feed not live (--once).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time as systime
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from bbbbb import live_core_argv
from psygrid_client import EXPECTED_UNIVERSE, PsygridClient
from run_engine import BASE_URL, IST, feed_is_live, is_trading_day, now_ist, parse_universe
from strategies.core import DATA_FAILURES, SIGNAL_THRESHOLD
from strategies.engines import ENGINE_CLASSES, ENGINE_NAMES, build_engines
from strategies.history import load_priors
from strategies.ledger import SignalLedger
from strategies.scanner import ScanReport, StockInput, run_scan
from strategies.telegram import TelegramNotifier, format_daily_summary, format_feed_problem, format_signal
from strategy_930 import Candle

FIRST_SCAN = time(9, 16)          # the 09:15 candle is complete at 09:16:00
LAST_SCAN = time(15, 16)          # last scan minute: at 15:16:xx the 15:15 square-off candle is complete
END_OF_DAY = time(15, 17)         # stop once the 15:16 scan has run (it books the 15:15 time exits)
FINAL_ARCHIVE = time(15, 31)      # the 15:29 candle is complete at 15:30: save the FULL session then
FINAL_ARCHIVE_UNTIL = time(16, 0) # give up on the final save after this
FEED_ALERT_EVERY = timedelta(minutes=15)
ARCHIVE_EVERY = timedelta(minutes=15)
MAX_TELEGRAM_ATTEMPTS = 5
PARTIAL_FEED_CHECK_FROM = time(9, 21)
MAX_FETCH_SECONDS = 45            # fetch + parse slower than this leaves only stale candles


@dataclass
class DayState:
    day: date
    root: Path
    ledger: SignalLedger
    notifier: TelegramNotifier
    engines: list
    priors: dict | None = None          # loaded at the first valid scan (see ensure_priors)
    universe: set = field(default_factory=set)
    scans: int = 0
    feed_failures: int = 0
    last_feed_alert: datetime | None = None
    last_archive: datetime | None = None
    summary_sent: bool = False
    final_archived: bool = False
    halted: str | None = None
    verbose: bool = True

    @property
    def day_dir(self) -> Path:
        return self.root / "engines" / self.day.isoformat()

    def journal(self, event: str, **fields) -> None:
        self.day_dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now_ist().isoformat(), "event": event, **fields}
        try:
            with (self.day_dir / "scans.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        except Exception as exc:
            print(f"[JOURNAL-WARN] {exc}")

    def save_universe(self) -> None:
        self.day_dir.mkdir(parents=True, exist_ok=True)
        (self.day_dir / "universe.json").write_text(json.dumps(sorted(self.universe)), encoding="utf-8")


def new_day_state(day: date, root: Path, engines: list, notifier: TelegramNotifier, persist: bool = True) -> DayState:
    cooldowns = {e.NAME: getattr(e, "COOLDOWN_MINUTES", 30) for e in engines}
    ledger_path = root / "engines" / day.isoformat() / "ledger.json" if persist else None
    st = DayState(day, root, SignalLedger(ledger_path, cooldowns), notifier, engines)
    uni = st.day_dir / "universe.json"
    if persist and uni.exists():
        try:
            st.universe = set(json.loads(uni.read_text(encoding="utf-8")))
        except Exception:
            pass
    return st


# ------------------------------------------------------------------------- inputs

def live_inputs(client: PsygridClient, raw: dict, scan_time: datetime) -> dict[str, StockInput]:
    parsed = parse_universe(client, raw, scan_time)
    out = {}
    for sym, d in parsed.items():
        payload = raw.get(sym) or {}
        try:
            to = float(payload.get("today_open")) if payload.get("today_open") is not None else None
        except (TypeError, ValueError):
            to = None
        out[sym] = StockInput(sym, d.candles, d.previous_close, to, d.health.healthy, d.health.reason)
    return out


def series_inputs(raw_session, scan_time: datetime) -> dict[str, StockInput]:
    """Inputs as they were known at ``scan_time`` from a saved session (replay): only bars
    that START before the scan minute, i.e. completed bars."""
    minute = scan_time.replace(second=0, microsecond=0)
    out = {}
    for sym, s in raw_session.stocks.items():
        bars = tuple(Candle(s.ts[i], s.o[i], s.h[i], s.l[i], s.c[i], s.v[i]) for i in range(len(s))
                     if s.ts[i].date() == raw_session.session_date and s.ts[i] < minute)
        out[sym] = StockInput(sym, bars, s.previous_close, s.today_open, True, "")
    for sym, why in raw_session.unparseable.items():
        out[sym] = StockInput(sym, (), None, None, False, why)
    return out


# --------------------------------------------------------------------------- cycle

@dataclass(frozen=True)
class Governor:
    """Daily risk limits across all engines (judgment values, see docs §7)."""
    max_new_per_scan: int = 2          # highest scores first
    max_open_same_direction: int = 3   # PENDING + TRIGGERED in one direction
    daily_stop_r: float = -3.0         # realised net R at which alerts stop for the day
    max_losing_streak: int = 3         # consecutive losing trades at which alerts stop


GOVERNOR = Governor()


def governor_block(st: DayState, direction: str, accepted_now: int) -> str | None:
    g = GOVERNOR
    if st.ledger.realised_r() <= g.daily_stop_r:
        return f"daily_loss_limit_{st.ledger.realised_r():+.2f}R"
    if st.ledger.losing_streak() >= g.max_losing_streak:
        return f"losing_streak_{st.ledger.losing_streak()}"
    if accepted_now >= g.max_new_per_scan:
        return "max_new_alerts_this_minute"
    if st.ledger.open_count(direction) >= g.max_open_same_direction:
        return f"max_open_{direction.lower()}_trades"
    return None


def handle_report(st: DayState, report: ScanReport, now: datetime, send: bool = True,
                  bars: dict | None = None) -> list[dict]:
    """Advance lifecycles, then dedup, apply the governor, record and alert. Returns new records.

    ``bars``: every stock's parsed bars (not only the currently valid ones), so an open trade
    keeps being tracked when its stock turns stale or illiquid."""
    if bars is None:
        bars = {s: v.bars for s, v in report.views.items()}
    changed = st.ledger.update(bars)
    new = []
    halted_before = st.halted
    scan_minute = now.replace(second=0, microsecond=0)
    for sig in sorted(report.signals, key=lambda x: (-x.score, x.engine, x.symbol)):
        if sig.signal_time < scan_minute:
            # decided on a bar older than the minute just completed: its entry window is mostly
            # gone and price may already be through the entry or stop -- never alert it
            st.journal("STALE_SIGNAL_SKIPPED", key=sig.key, signal_time=sig.signal_time.isoformat())
            continue
        ok, why = st.ledger.decide(sig)
        if not ok:
            continue
        block = governor_block(st, sig.direction, len(new))
        if block:
            st.journal("GOVERNOR_SKIP", key=sig.key, score=sig.score, reason=block)
            if block.startswith(("daily_loss", "losing_streak")):
                st.halted = block
            continue
        new.append(st.ledger.record(sig))
    if st.halted and not halted_before:
        msg = f"RISK LIMIT: {st.halted} -- no more alerts today"
        print(f"{now:%H:%M:%S} {msg}", flush=True)
        st.journal("GOVERNOR_HALT", reason=st.halted)
        if send and st.notifier.enabled:
            st.notifier.send(f"⛔ <b>{msg}</b>")
    if send:
        retry_unsent(st, now)
    for rec in new:
        print_alert(rec, st)
    for rec in changed:
        if st.verbose:
            print(f"{now:%H:%M:%S} [{rec['engine']}] {rec['symbol']} {rec['direction']} -> {rec['state']}"
                  + (f" ({rec['r_multiple']:+.2f}R gross, {rec['r_net']:+.2f}R net)"
                     if rec.get("r_net") is not None else ""))
    st.ledger.save()
    return new


def retry_unsent(st: DayState, now: datetime) -> None:
    """Send every recorded, unsent alert whose entry window is still open (never late)."""
    if not st.notifier.enabled:
        return
    for rec in st.ledger.records.values():
        tg = rec["telegram"]
        if tg["sent"] or tg["attempts"] >= MAX_TELEGRAM_ATTEMPTS or rec["state"] != "PENDING":
            continue
        if now >= datetime.fromisoformat(rec["valid_until"]):
            tg["error"] = (tg["error"] or "") + "|expired_before_send"
            tg["attempts"] = MAX_TELEGRAM_ATTEMPTS
            continue
        ok, info, mid = st.notifier.send(format_signal(rec))
        tg["attempts"] += 1
        tg["sent"], tg["error"], tg["message_id"] = ok, (None if ok else info), mid


def print_alert(rec: dict, st: DayState) -> None:
    print("\a", end="")
    print("\n" + "=" * 96)
    print(f"🔔 {rec['engine']} | {rec['direction']} {rec['symbol']} | score {rec['score']:.2f}")
    print(f"   entry {rec['entry']:.2f} | stop {rec['stop']:.2f} | target {rec['target']:.2f} "
          f"({rec['reward_r']:.1f}R) | risk {rec['risk_pct']:.2f}% | valid until "
          f"{datetime.fromisoformat(rec['valid_until']):%H:%M}")
    for c in rec["conditions"]:
        print(f"   - {c}")
    tg = rec["telegram"]
    if not st.notifier.enabled:
        print("   TELEGRAM NOT CONFIGURED (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): printed only")
    elif tg["sent"]:
        print("   telegram: sent")
    else:
        print(f"   telegram: NOT SENT ({tg['error']}); retried next minute while the entry is valid")
    print("=" * 96, flush=True)


def scan_line(report: ScanReport) -> str:
    sig = Counter(s.engine for s in report.signals)
    fails = sum(report.quality_counts.get(k, 0) for k in DATA_FAILURES)
    return (f"{report.scan_time:%H:%M:%S} universe {len(report.universe)} | received {report.received} | "
            f"valid {report.valid} | data-fail {fails} | ineligible {report.quality_counts.get('INELIGIBLE', 0)} | "
            f"market {'OK' if report.context.valid else 'INVALID:' + report.context.reason} | "
            f"90+ {len(report.signals)} ({', '.join(f'{k.split(chr(95))[0]}={v}' for k, v in sig.items()) or '-'})"
            f" | {report.seconds:.1f}s")


def journal_report(st: DayState, report: ScanReport, new: list[dict]) -> None:
    st.journal("SCAN", scan_time=report.scan_time.isoformat(), universe=len(report.universe),
               received=report.received, valid=report.valid, quality=dict(report.quality_counts),
               quality_reasons=dict(report.quality_reasons.most_common(8)),
               market={"valid": report.context.valid, "reason": report.context.reason,
                       "ret_open": report.context.ret_open, "breadth": report.context.breadth_above_vwap},
               counts={e: dict(c) for e, c in report.counts.items()},
               signals=[s.key for s in report.signals], new=[r["key"] for r in new],
               engine_errors={e: [m.splitlines()[0] for m in v] for e, v in report.engine_errors.items()},
               integrity_ok=report.integrity_ok, seconds=round(report.seconds, 3))
    for e, msgs in report.engine_errors.items():
        print(f"[ENGINE-ERROR] {e}: {len(msgs)} error(s); first: {msgs[0].splitlines()[0]}")
    if not report.integrity_ok:
        print("[INTEGRITY-FAIL] an engine changed shared features; results of this scan are suspect")


def feed_problem(st: DayState, now: datetime, detail: str, send: bool = True) -> None:
    st.feed_failures += 1
    print(f"{now:%H:%M:%S} DATA FEED PROBLEM: {detail} -- no signals from this data", flush=True)
    st.journal("FEED_PROBLEM", detail=detail)
    if send and st.notifier.enabled and (st.last_feed_alert is None or now - st.last_feed_alert >= FEED_ALERT_EVERY):
        st.notifier.send(format_feed_problem(now, detail))
        st.last_feed_alert = now


def archive_session(root: Path, day: date, raw: dict, meta: dict) -> Path | None:
    """Save today's raw stocks to data/sessions/DAY.json.gz in the 945.py archive format."""
    if not raw:
        return None
    target = root / "sessions" / f"{day.isoformat()}.json.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"stocks": raw, "stock_count": len(raw), "universe_size": meta.get("universe_size"),
               "status": meta.get("status"), "session": meta.get("session") or {}}
    blob = json.dumps({"archived_at": now_ist().isoformat(), "stocks_payload": payload, "index_payload": None},
                      separators=(",", ":"), default=str).encode()
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(blob, compresslevel=5))
    os.replace(tmp, target)
    return target


def ensure_priors(st: DayState, inputs: dict[str, StockInput]) -> None:
    """Load prior-session levels once, at the first scan that carries TODAY's candles: only then
    is the feed's previous_close known to refer to the previous session (before the open the
    feed may still hold yesterday's metadata, which would wrongly fail the close cross-check)."""
    if st.priors is not None:
        return
    prev_close = {s: si.previous_close for s, si in inputs.items() if si.previous_close}
    try:
        st.priors, rep = load_priors(st.day, st.root, is_trading_day, prev_close)
    except Exception as exc:
        st.priors, rep = {}, {"error": f"{type(exc).__name__}: {exc}"}
    print(f"PRIOR LEVELS : {rep}", flush=True)
    st.journal("PRIORS", report=rep)


def newest_bar_date(inputs: dict[str, StockInput]) -> date | None:
    days = Counter(si.bars[-1].ts.astimezone(IST).date() for si in inputs.values() if si.bars)
    return days.most_common(1)[0][0] if days else None


def live_cycle(st: DayState, client: PsygridClient, send: bool = True, clock=now_ist) -> ScanReport | None:
    now = clock()
    try:
        raw = client.market()
    except Exception as exc:
        feed_problem(st, now, f"feed unreachable: {exc}", send)
        return None
    meta = client.last_market_meta
    live, why = feed_is_live(meta)
    if not live:
        feed_problem(st, now, f"feed not live: {why}", send)
        return None
    inputs = live_inputs(client, raw, clock())
    newest = newest_bar_date(inputs)
    if newest != st.day:
        feed_problem(st, now, f"feed carries {newest} candles, not today's ({st.day})", send)
        return None
    st.universe |= set(inputs)
    st.save_universe()
    ensure_priors(st, inputs)
    scan_time = clock()
    lag = (scan_time - now).total_seconds()
    if lag > MAX_FETCH_SECONDS:
        # the download (or start-up work) took so long that every candle is now too old to trade:
        # say so plainly instead of reporting it as a "partial feed"
        feed_problem(st, now, f"data arrived {lag:.0f}s late (slow internet/PC or slow feed); "
                              "this minute skipped to avoid stale signals", send)
        return None
    report = run_scan(inputs, scan_time, st.engines, st.priors, st.universe)
    st.scans += 1
    if not report.context.valid and scan_time.time() >= PARTIAL_FEED_CHECK_FROM:
        # before 09:21 every stock has < 5 completed bars by construction: not a feed fault
        why = ", ".join(f"{k}={v}" for k, v in report.quality_reasons.most_common(3))
        feed_problem(st, now, f"partial feed: {report.valid} valid of {report.received} "
                              f"({report.context.reason}; main reasons: {why})", send)
    new = handle_report(st, report, scan_time, send, bars={s: si.bars for s, si in inputs.items() if si.bars})
    journal_report(st, report, new)
    print(scan_line(report), flush=True)
    if st.last_archive is None or scan_time - st.last_archive >= ARCHIVE_EVERY or scan_time.time() >= time(15, 14):
        try:
            archive_session(st.root, st.day, raw, meta)
            st.last_archive = scan_time
        except Exception as exc:
            print(f"[ARCHIVE-WARN] {exc}")
    return report


def final_archive(st: DayState, client: PsygridClient, clock=now_ist) -> None:
    """Save today's COMPLETE session (through the 15:29 candle) so tomorrow has PDH/PDL/PDC.
    The in-session saves stop at 15:14, which the history loader rightly treats as incomplete."""
    st.final_archived = True
    try:
        raw = client.market()
        inputs = live_inputs(client, raw, clock())
        if newest_bar_date(inputs) != st.day:
            print("[ARCHIVE] final save skipped: feed no longer carries today's candles", flush=True)
            return
        path = archive_session(st.root, st.day, raw, client.last_market_meta)
        print(f"[ARCHIVE] complete session saved for tomorrow's levels: {path}", flush=True)
        st.journal("FINAL_ARCHIVE", path=str(path))
    except Exception as exc:
        print(f"[ARCHIVE-WARN] final save failed: {exc}", flush=True)
        st.journal("FINAL_ARCHIVE_FAILED", error=str(exc))


def seconds_to_next_scan(now: datetime, offset: int) -> float:
    target = now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=offset)
    return max(1.0, (target - now).total_seconds())


def daily_summary(st: DayState, send: bool = True) -> None:
    if st.summary_sent:
        return
    text = format_daily_summary(st.day.isoformat(), st.ledger.summary(), st.scans, st.feed_failures)
    print("\n" + text.replace("<b>", "").replace("</b>", ""))
    if send and st.notifier.enabled:
        st.notifier.send(text)
    st.summary_sent = True
    st.journal("DAILY_SUMMARY", summary=st.ledger.summary(), scans=st.scans, feed_failures=st.feed_failures)


def keep_awake() -> bool:
    """On Windows, stop the PC from sleeping while this process runs (released automatically
    when it exits). Returns True when the request was made."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    except Exception:
        return False


def run_day(st: DayState, client: PsygridClient, offset: int, send: bool = True,
            clock=now_ist, sleep=systime.sleep) -> int:
    print(f"DAILY RUN    : {st.day} | scans every minute {FIRST_SCAN:%H:%M}-{LAST_SCAN:%H:%M} IST "
          f"({offset}s past the minute) | engines {len(st.engines)} | telegram "
          f"{'ON' if st.notifier.enabled and send else 'OFF'}", flush=True)
    awake = keep_awake()
    st.journal("RUN_START", keep_awake=awake)
    if send and st.notifier.enabled:
        started = clock()
        st.notifier.send(f"✅ <b>Strategy engines running</b> — {st.day} {started:%H:%M} IST\n"
                         f"{len(st.engines)} engines, scans every minute until {LAST_SCAN:%H:%M}. "
                         "You will get a message for every 90+ signal, any data-feed problem, and a summary at the close.")
    while True:
        now = clock()
        if now.time() >= END_OF_DAY:
            daily_summary(st, send)
            if st.final_archived or now.time() >= FINAL_ARCHIVE_UNTIL:
                return 0
            if now.time() < FINAL_ARCHIVE:
                wait = (datetime.combine(now.date(), FINAL_ARCHIVE, IST) - now).total_seconds() + offset
                print(f"{now:%H:%M:%S} waiting until {FINAL_ARCHIVE:%H:%M} to save the complete session "
                      "(tomorrow's previous-day levels)", flush=True)
                sleep(max(1.0, wait))
                continue
            final_archive(st, client, clock)
            return 0
        if now.time() < FIRST_SCAN:
            wait = (datetime.combine(now.date(), FIRST_SCAN, IST) - now).total_seconds() + offset
            print(f"{now:%H:%M:%S} waiting for the first completed candle (first scan {FIRST_SCAN:%H:%M}:{offset:02d})",
                  flush=True)
            sleep(max(1.0, wait))
            continue
        try:
            live_cycle(st, client, send, clock)
        except Exception as exc:          # never let one bad minute end the day
            print(f"[CYCLE-ERROR] {type(exc).__name__}: {exc}", flush=True)
            st.journal("CYCLE_ERROR", error=f"{type(exc).__name__}: {exc}")
        sleep(seconds_to_next_scan(clock(), offset))


# --------------------------------------------------------------------------- replay

def replay(paths: list[str], engines_names: list[str] | None, data_dir: Path, verbose: bool) -> int:
    from intelligence.selector_data import load_session_file
    files: list[Path] = []
    for item in paths:
        p = Path(item)
        files += sorted(p.glob("*.json*")) if p.is_dir() else ([p] if p.is_file() else [])
    if not files:
        print("No session files found.")
        return 2
    total = {}
    for f in files:
        raw = load_session_file(f)
        day = raw.session_date
        engines = build_engines(engines_names)
        st = new_day_state(day, data_dir, engines, TelegramNotifier("", ""), persist=False)
        st.verbose = verbose
        prev_close = {s: ser.previous_close for s, ser in raw.stocks.items()}
        st.priors, prep = load_priors(day, data_dir, is_trading_day, prev_close)
        print(f"\nREPLAY {day} ({f.name}) | priors: {prep}")
        minute = datetime.combine(day, FIRST_SCAN, IST)
        end = datetime.combine(day, LAST_SCAN, IST)
        while minute <= end:
            scan_time = minute + timedelta(seconds=8)
            inputs = series_inputs(raw, scan_time)
            st.universe |= set(inputs)
            report = run_scan(inputs, scan_time, engines, st.priors, st.universe)
            handle_report(st, report, scan_time, send=False,
                          bars={s: si.bars for s, si in inputs.items() if si.bars})
            if verbose:
                print(scan_line(report))
            minute += timedelta(minutes=1)
        summ = st.ledger.summary()
        for eng, s in summ.items():
            t = total.setdefault(eng, {"signals": 0, "states": Counter(), "r": []})
            t["signals"] += s["signals"]
            t["states"].update(s["states"])
            t["r"] += s["r"]
    print("\nREPLAY TOTALS (net R after modelled costs; stop assumed first on two-sided bars)")
    for name in ENGINE_NAMES:
        t = total.get(name)
        if not t:
            print(f"  {name:<42} 0 signals")
            continue
        r = t["r"]
        print(f"  {name:<42} {t['signals']:>3} signals | {dict(t['states'])} | closed {len(r)} "
              f"| mean {sum(r) / len(r) if r else 0:+.2f}R")
    print("Judge an engine only on a large out-of-sample count (50+ closed trades), never on one day.")
    return 0


# ---------------------------------------------------------------------------- main

def list_engines() -> None:
    for c in ENGINE_CLASSES:
        print(f"{c.NAME:<42} {c.TITLE:<55} {c.EARLIEST:%H:%M}-{c.LATEST:%H:%M}"
              f"{' needs-market' if c.NEEDS_MARKET else ''}")
    print(f"Signal threshold: score >= {SIGNAL_THRESHOLD:g} and every score component >= 0.5")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ten research-backed intraday strategy engines (PSYGRID 989 stocks)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--once", action="store_true", help="one scan now, then exit")
    ap.add_argument("--replay", nargs="+", metavar="PATH", help="replay saved session file(s)/folder(s)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--engines", default="", help="comma-separated engine names (default: all ten)")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--telegram-test", action="store_true")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--scan-offset", type=int, default=8, help="seconds past each minute to scan (default 8)")
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--expected-universe", type=int, default=EXPECTED_UNIVERSE)
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)

    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.discover("tests", pattern="test_strategy_engines*.py", top_level_dir=".")
        return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 10
    if args.list:
        list_engines()
        return 0
    names = [n.strip() for n in args.engines.split(",") if n.strip()] or None
    try:
        engines = build_engines(names)
    except ValueError as exc:
        ap.error(str(exc))
    if not 0 <= args.scan_offset < 60:
        ap.error("--scan-offset must be 0-59")
    data_dir = Path(args.data_dir)
    if args.replay:
        return replay(args.replay, names, data_dir, args.verbose)
    notifier = TelegramNotifier()
    if args.telegram_test:
        if not notifier.enabled:
            print("TELEGRAM NOT CONFIGURED: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
            return 2
        ok, info, _ = notifier.send("✅ PSYGRID strategy engines: Telegram test message")
        print(f"telegram test: {'OK' if ok else 'FAILED ' + info}")
        return 0 if ok else 2

    today = now_ist()
    if not is_trading_day(today.date()):
        print(f"NOT AN NSE TRADING DAY: {today.date()} (holiday calendar: run_engine.NSE_HOLIDAYS_2026)")
        return 20
    if today.time() >= END_OF_DAY:
        print("Session over for today.")
        return 23
    client = PsygridClient(args.base_url, timeout=args.timeout, expected_universe=args.expected_universe)
    st = new_day_state(today.date(), data_dir, engines, notifier)
    send = not args.no_telegram
    st.journal("START", engines=[e.NAME for e in engines], telegram=notifier.enabled and send)
    if args.once:
        report = live_cycle(st, client, send)
        if report is None:
            return 30
        return 0
    try:
        return run_day(st, client, args.scan_offset, send)
    except KeyboardInterrupt:
        print("\nStopped.")
        st.ledger.save()
        return 0


if __name__ == "__main__":
    raise SystemExit(main(live_core_argv(sys.argv[1:])))
