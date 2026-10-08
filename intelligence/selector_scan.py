"""945 continuous scanner: rescan the whole universe every minute and alert only on a
confirmed top setup.

The 09:45 selector publishes one fixed decision. The scanner reuses the SAME feature
matrix and the SAME ranking model, but at every minute from 09:30 instead of once:

    every scan     cut the feed to completed candles (< scan minute), build the feature
                   matrix for every stock, rank every stock in both directions
    shortlist      the top `shortlist_size` directional candidates scoring >= watch_score;
                   each keeps a streak of consecutive scans on the shortlist
    signal         the best shortlisted candidate that is
                     - Tier 1 (score >= tier1_score; Tier 2 only with allow_tier2),
                     - confirmed (streak >= confirm_scans),
                     - with the market (regime UP -> longs only, DOWN -> shorts only),
                     - not already run (< max_day_move_pct from previous close),
                     - inside the entry window, and
                     - still worth it after costs (net reward/risk >= min_net_rr)
                   -> a stop-entry plan: trigger = break of the last candle, stop beyond
                      the recent swing, target = reward_r x risk
    manage         the order fills on the trigger or expires; then target, stop, time
                   stop or square-off. A candle that touches both stop and target counts
                   as the stop.

It behaves like a disciplined human trader: one trade at a time, never the same stock
and direction twice a day, at most `max_signals_per_day` signals, and it stops for the
day after `max_losses_per_day` losing trades. Every result is reported net of costs.

step() only ever reads candles that started before the scan minute, so the same code
runs live and on archived sessions (replay) with no look-ahead.
"""
from __future__ import annotations

import bisect
import csv
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .selector_945 import explain
from .selector_config import SelectorConfig
from .selector_data import IST, SESSION_START, RawSession, Series, SessionInput
from .selector_features import build_feature_table
from .selector_scoring import Candidate, LinearEvidenceModel

ONE_MIN = timedelta(minutes=1)
SIDE = {"UP": "LONG", "DOWN": "SHORT"}


@dataclass(frozen=True)
class ScanConfig:
    # clock
    start: time = time(9, 30)                  # first scan (uses the 09:15..09:29 candles)
    last_entry: time = time(14, 45)            # no new signals at or after this minute
    square_off: time = time(15, 15)            # every open trade is closed by this time
    stop_scanning: time = time(15, 20)
    lunch_start: time = time(12, 0)
    lunch_end: time = time(13, 30)
    lunch_extra_score: float = 3.0             # midday chop: demand a higher score
    # universe
    min_bars: int = 15                         # 09:30 has 15 completed candles
    # ranking thresholds (945 score, 0..100; on random noise the best stock scores ~70)
    watch_score: float = 70.0
    tier2_score: float = 78.0
    tier1_score: float = 85.0
    allow_tier2: bool = False
    shortlist_size: int = 10
    confirm_scans: int = 3
    # gates
    follow_regime: bool = True
    max_day_move_pct: float = 7.0
    # trade plan
    trigger_valid_bars: int = 2
    stop_lookback_bars: int = 5
    stop_buffer_atr: float = 0.25
    min_risk_atr: float = 0.5
    max_risk_atr: float = 3.0
    reward_r: float = 2.0
    min_net_rr: float = 1.5
    max_hold_minutes: int = 60
    # discipline
    max_signals_per_day: int = 3
    max_losses_per_day: int = 2
    # costs (NSE intraday equity, discount broker; percentages of order value)
    order_value: float = 100_000.0
    brokerage_pct: float = 0.03
    brokerage_cap: float = 20.0
    stt_sell_pct: float = 0.025
    exchange_pct: float = 0.00297
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0
    slippage_pct_per_side: float = 0.03
    risk_rupees: float | None = None           # print a share quantity for this risk
    max_position: float | None = None          # ... never more than this many rupees per position


def round_trip_cost_pct(cfg: ScanConfig) -> float:
    """All-in cost of one intraday round trip as % of the order value."""
    v = cfg.order_value
    brokerage = 2 * min(v * cfg.brokerage_pct / 100, cfg.brokerage_cap)
    exchange = 2 * v * cfg.exchange_pct / 100
    sebi = 2 * v * cfg.sebi_pct / 100
    stt = v * cfg.stt_sell_pct / 100           # one sell leg (long exit or short entry)
    stamp = v * cfg.stamp_buy_pct / 100        # one buy leg
    gst = (brokerage + exchange + sebi) * cfg.gst_pct / 100
    slippage = 2 * v * cfg.slippage_pct_per_side / 100
    return (brokerage + exchange + sebi + stt + stamp + gst + slippage) / v * 100


# --------------------------------------------------------------------------- data

def _slice(s: Series, start: datetime, end: datetime) -> Series:
    i, j = bisect.bisect_left(s.ts, start), bisect.bisect_left(s.ts, end)
    if i == 0 and j == len(s):
        return s
    return Series(s.symbol, s.ts[i:j], s.o[i:j], s.h[i:j], s.l[i:j], s.c[i:j], s.v[i:j],
                  s.previous_close, s.today_open)


def information_set(raw: RawSession, cutoff: datetime) -> SessionInput:
    """Completed candles only: every candle that started before `cutoff`."""
    start = datetime.combine(raw.session_date, SESSION_START, IST)
    stocks = {sym: _slice(s, start, cutoff) for sym, s in raw.stocks.items()}
    index = _slice(raw.index, start, cutoff) if raw.index is not None else None
    received = tuple(sorted(set(raw.stocks) | set(raw.unparseable)))
    return SessionInput(raw.session_date, cutoff, stocks, received, dict(raw.unparseable),
                        index if index is not None and len(index) else None, dict(raw.feed_meta), raw.source)


# ------------------------------------------------------------------------ trades

@dataclass
class Trade:
    signal_id: str
    day: str
    number: int
    symbol: str
    direction: str                 # LONG | SHORT
    side: int                      # +1 | -1
    tier: str
    score: float
    rank: int
    confirmed_scans: int
    signal_time: str               # scan minute (ISO)
    signal_bar: str                # HH:MM of the candle whose break triggers
    trigger: float
    stop: float
    target: float
    risk: float
    atr: float
    cost_pct: float
    net_rr: float
    expires_at: str
    reasons: list = field(default_factory=list)
    market: dict = field(default_factory=dict)
    quantity: int | None = None
    hold_minutes: int = 60
    state: str = "PENDING"         # PENDING | OPEN | CLOSED | EXPIRED
    entry: float | None = None
    entry_time: str | None = None
    time_stop: str | None = None
    exit: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    gross_r: float | None = None
    net_r: float | None = None
    net_pct: float | None = None
    next_bar: str = ""

    @property
    def live(self) -> bool:
        return self.state in ("PENDING", "OPEN")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Trade":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass(frozen=True)
class Event:
    kind: str                      # SIGNAL | FILLED | EXPIRED | TARGET | STOP | TIME_STOP | SQUARE_OFF | DAY_LOCKED
    at: str
    trade: dict | None
    text: str = ""


@dataclass(frozen=True)
class ScanSummary:
    at: datetime
    universe: int
    eligible: int
    regime: str
    breadth: float | None
    market_return: float | None
    market_source: str
    watch: tuple                   # ((symbol, LONG/SHORT, score, streak), ...)
    status: str


# ----------------------------------------------------------------------- scanner

class Scanner:
    def __init__(self, cfg: ScanConfig, weights: dict, sectors: dict | None = None,
                 selector_cfg: SelectorConfig | None = None):
        self.cfg = cfg
        self.sel = replace(selector_cfg or SelectorConfig(), min_bars=cfg.min_bars)
        self.model = LinearEvidenceModel(weights)
        self.sectors = sectors or {}
        self.cost_pct = round_trip_cost_pct(cfg)
        self.day: date | None = None
        self.reset(None)

    # ------------------------------------------------------------- state
    def reset(self, day: date | None) -> None:
        self.day = day
        self.streak: dict = {}
        self.trades: list[Trade] = []
        self.alerted: set = set()
        self.locked = False

    def restore(self, day: date, trades: list[Trade]) -> None:
        """Resume a day after a restart from the journal (no duplicate signals)."""
        self.reset(day)
        self.trades = sorted(trades, key=lambda t: t.number)
        self.alerted = {(t.symbol, t.direction) for t in trades}
        self.locked = self.losses() >= self.cfg.max_losses_per_day

    @property
    def active(self) -> Trade | None:
        return next((t for t in self.trades if t.live), None)

    def losses(self) -> int:
        return sum(1 for t in self.trades if t.state == "CLOSED" and (t.net_r or 0) < 0)

    # ------------------------------------------------------------- one scan
    def step(self, raw: RawSession, cutoff: datetime) -> tuple[list[Event], ScanSummary]:
        if self.day != raw.session_date:
            self.reset(raw.session_date)
        cfg = self.cfg
        events: list[Event] = []
        if self.active:
            events += self._manage(self.active, raw.stocks.get(self.active.symbol), cutoff)
            if (not self.locked and self.losses() >= cfg.max_losses_per_day):
                self.locked = True
                events.append(Event("DAY_LOCKED", cutoff.isoformat(), None,
                                    f"{self.losses()} losing trades today -- no more signals until tomorrow"))

        si = information_set(raw, cutoff)
        table = build_feature_table(si, self.sel, self.sectors)
        ranked = self.model.rank(table) if table.eligible else []

        shortlist = [c for c in ranked[:cfg.shortlist_size] if c.score >= cfg.watch_score]
        self.streak = {(c.symbol, c.direction): self.streak.get((c.symbol, c.direction), 0) + 1
                       for c in shortlist}

        status = self._signal(si, table, ranked, shortlist, cutoff, events)
        m = table.market
        summary = ScanSummary(cutoff, len(si.received_symbols), len(table.eligible), m.regime, m.breadth,
                              m.market_return, m.market_source,
                              tuple((c.symbol, SIDE[c.direction], round(c.score, 1),
                                     self.streak[(c.symbol, c.direction)]) for c in shortlist[:3]),
                              status)
        return events, summary

    # ------------------------------------------------------------- signals
    def _needed(self, cutoff: datetime) -> tuple[float, float]:
        extra = self.cfg.lunch_extra_score if self.cfg.lunch_start <= cutoff.time() < self.cfg.lunch_end else 0.0
        return self.cfg.tier1_score + extra, self.cfg.tier2_score + extra

    def _signal(self, si, table, ranked, shortlist, cutoff, events) -> str:
        cfg = self.cfg
        if self.active:
            t = self.active
            return f"{'waiting for trigger' if t.state == 'PENDING' else 'in trade'}: {t.symbol} {t.direction}"
        if self.locked:
            return "day locked (loss limit)"
        if len(self.trades) >= cfg.max_signals_per_day:
            return f"signal limit reached ({cfg.max_signals_per_day})"
        if not (cfg.start <= cutoff.time() < cfg.last_entry):
            return "outside entry window"
        if not shortlist:
            return "nothing above the watch score"
        t1, t2 = self._needed(cutoff)
        rank_of = {(c.symbol, c.direction): i + 1 for i, c in enumerate(ranked[:cfg.shortlist_size])}
        closest = None
        for c in shortlist:                                   # already best-first
            key = (c.symbol, c.direction)
            tier = "TIER 1" if c.score >= t1 else ("TIER 2" if cfg.allow_tier2 and c.score >= t2 else None)
            if tier is None:
                closest = closest or f"{c.symbol} {SIDE[c.direction]} {c.score:.1f} below Tier {'1/2' if cfg.allow_tier2 else '1'} ({t2 if cfg.allow_tier2 else t1:.0f})"
                continue
            if self.streak[key] < cfg.confirm_scans:
                closest = closest or f"{c.symbol} {SIDE[c.direction]} {tier} confirming {self.streak[key]}/{cfg.confirm_scans}"
                continue
            reason = self._gate(c, table)
            if reason:
                closest = closest or f"{c.symbol} {SIDE[c.direction]} blocked: {reason}"
                continue
            plan, why = self._plan(c, si, table, cutoff)
            if plan is None:
                closest = closest or f"{c.symbol} {SIDE[c.direction]} blocked: {why}"
                continue
            plan.tier, plan.rank, plan.confirmed_scans = tier, rank_of[key], self.streak[key]
            self.trades.append(plan)
            self.alerted.add((c.symbol, plan.direction))
            events.append(Event("SIGNAL", cutoff.isoformat(), plan.to_dict()))
            return f"SIGNAL {plan.symbol} {plan.direction}"
        return closest or "no confirmed setup"

    def _gate(self, c: Candidate, table) -> str | None:
        d = 1 if c.direction == "UP" else -1
        if (c.symbol, SIDE[c.direction]) in self.alerted:
            return "already signalled today"
        regime = table.market.regime
        if self.cfg.follow_regime and ((regime == "UP" and d < 0) or (regime == "DOWN" and d > 0)):
            return f"against the market ({regime})"
        f = table.features[c.symbol]
        move = f.get("dist_prev_close_pct")
        if move is None:
            move = f.get("return_since_open")
        if move is not None and d * move >= self.cfg.max_day_move_pct:
            return f"already moved {move:+.1f}% today"
        return None

    def _plan(self, c: Candidate, si: SessionInput, table, cutoff: datetime):
        cfg = self.cfg
        s = si.stocks[c.symbol]
        f = table.features[c.symbol]
        d = 1 if c.direction == "UP" else -1
        price = s.c[-1]
        atr = f["atr_pct"] / 100 * price
        if atr <= 0:
            return None, "no measurable range"
        lb = cfg.stop_lookback_bars
        if d > 0:
            trigger = s.h[-1]
            stop = min(s.l[-lb:]) - cfg.stop_buffer_atr * atr
        else:
            trigger = s.l[-1]
            stop = max(s.h[-lb:]) + cfg.stop_buffer_atr * atr
        risk = abs(trigger - stop)
        if risk < cfg.min_risk_atr * atr:
            risk = cfg.min_risk_atr * atr
            stop = trigger - d * risk
        if risk > cfg.max_risk_atr * atr:
            return None, f"stop too wide ({risk / atr:.1f} ATR)"
        target = trigger + d * cfg.reward_r * risk
        cost = self.cost_pct / 100 * trigger
        net_rr = (cfg.reward_r * risk - cost) / (risk + cost)
        if net_rr < cfg.min_net_rr:
            return None, f"costs eat the edge (net reward/risk {net_rr:.2f})"
        qty = int(cfg.risk_rupees // (risk + cost)) if cfg.risk_rupees else None
        if qty is not None and cfg.max_position:
            qty = min(qty, int(cfg.max_position // trigger))
        m = table.market
        number = len(self.trades) + 1
        return Trade(
            signal_id=f"{si.session_date.isoformat()}-{number}-{c.symbol}-{SIDE[c.direction]}",
            day=si.session_date.isoformat(), number=number, symbol=c.symbol, direction=SIDE[c.direction],
            side=d, tier="", score=round(c.score, 2), rank=0, confirmed_scans=0,
            signal_time=cutoff.isoformat(), signal_bar=s.ts[-1].strftime("%H:%M"),
            trigger=round(trigger, 2), stop=round(stop, 2), target=round(target, 2), risk=round(risk, 4),
            atr=round(atr, 4), cost_pct=round(self.cost_pct, 4), net_rr=round(net_rr, 2),
            expires_at=(cutoff + cfg.trigger_valid_bars * ONE_MIN).isoformat(),
            reasons=explain(c, top=4),
            market={"regime": m.regime, "breadth": m.breadth, "market_return_pct": m.market_return,
                    "source": m.market_source},
            quantity=qty, hold_minutes=cfg.max_hold_minutes, next_bar=cutoff.isoformat(),
        ), ""

    # ------------------------------------------------------------- trade management
    def _manage(self, t: Trade, s: Series | None, cutoff: datetime) -> list[Event]:
        events: list[Event] = []
        nxt = datetime.fromisoformat(t.next_bar)
        expires = datetime.fromisoformat(t.expires_at)
        bars = _slice(s, nxt, cutoff) if s is not None else None
        d = t.side
        for i in range(len(bars) if bars is not None else 0):
            ts, o, h, l, c = bars.ts[i], bars.o[i], bars.h[i], bars.l[i], bars.c[i]
            t.next_bar = (ts + ONE_MIN).isoformat()
            if t.state == "PENDING":
                if ts >= expires:
                    break
                if not ((d > 0 and h >= t.trigger) or (d < 0 and l <= t.trigger)):
                    continue
                t.entry = round(max(t.trigger, o) if d > 0 else min(t.trigger, o), 2)
                t.entry_time = ts.isoformat()
                square = datetime.combine(ts.date(), self.cfg.square_off, IST)
                t.time_stop = min(ts + timedelta(minutes=self.cfg.max_hold_minutes), square).isoformat()
                t.state = "OPEN"
                events.append(Event("FILLED", ts.isoformat(), t.to_dict()))
                fill_bar = True
            else:
                fill_bar = False
            # OPEN: stop first, then target, then time. On the fill candle the order of
            # moves is unknown, so a touched stop counts and a touched target does not.
            stop_hit = l <= t.stop if d > 0 else h >= t.stop
            tgt_hit = h >= t.target if d > 0 else l <= t.target
            if stop_hit:
                gap = not fill_bar and d * (o - t.stop) < 0          # opened beyond the stop
                self._close(t, o if gap else t.stop, ts, "STOP", events)
                return events
            if tgt_hit and not fill_bar:
                gap = d * (o - t.target) > 0                          # opened beyond the target
                self._close(t, o if gap else t.target, ts, "TARGET", events)
                return events
            if ts + ONE_MIN >= datetime.fromisoformat(t.time_stop):
                reason = "SQUARE_OFF" if (ts + ONE_MIN).time() >= self.cfg.square_off else "TIME_STOP"
                self._close(t, c, ts, reason, events)
                return events
        if t.state == "PENDING" and cutoff >= expires:
            t.state = "EXPIRED"
            t.next_bar = cutoff.isoformat()
            events.append(Event("EXPIRED", cutoff.isoformat(), t.to_dict(),
                                f"trigger {t.trigger:.2f} not traded within {self.cfg.trigger_valid_bars} candle(s)"))
        return events

    def _close(self, t: Trade, price: float, ts: datetime, reason: str, events: list[Event]) -> None:
        t.exit, t.exit_time, t.exit_reason, t.state = round(price, 2), ts.isoformat(), reason, "CLOSED"
        risk = abs(t.entry - t.stop) or t.risk
        move = t.side * (t.exit - t.entry)
        t.gross_r = round(move / risk, 3)
        t.net_r = round((move - t.cost_pct / 100 * t.entry) / risk, 3)
        t.net_pct = round(move / t.entry * 100 - t.cost_pct, 4)
        events.append(Event(reason, ts.isoformat(), t.to_dict()))


# ----------------------------------------------------------------------- journal

class Journal:
    """data/scan/YYYY-MM-DD.jsonl (every event) + data/scan/trades.csv (one row per trade)."""
    CSV_FIELDS = ("day", "number", "symbol", "direction", "tier", "score", "signal_time", "trigger", "stop",
                  "target", "state", "entry", "entry_time", "exit", "exit_time", "exit_reason", "gross_r",
                  "net_r", "net_pct", "cost_pct")

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, day: str) -> Path:
        return self.root / f"{day}.jsonl"

    def write(self, ev: Event) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path(ev.at[:10]).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": ev.kind, "at": ev.at, "text": ev.text, "trade": ev.trade},
                                separators=(",", ":")) + "\n")
        if ev.trade and ev.trade["state"] in ("CLOSED", "EXPIRED"):
            p = self.root / "trades.csv"
            new = not p.exists()
            with p.open("a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=self.CSV_FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(ev.trade)

    def trades(self, day: str) -> list[Trade]:
        p = self.path(day)
        latest: dict = {}
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("trade"):
                    latest[row["trade"]["signal_id"]] = Trade.from_dict(row["trade"])
        return list(latest.values())


# ----------------------------------------------------------------------- replay

def scan_minutes(day: date, cfg: ScanConfig):
    t = datetime.combine(day, cfg.start, IST)
    end = datetime.combine(day, cfg.stop_scanning, IST)
    while t <= end:
        yield t
        t += ONE_MIN


def replay(raw: RawSession, scanner: Scanner, on_event=None, on_scan=None) -> list[Trade]:
    """Run the live scan logic minute by minute over a saved full session."""
    scanner.reset(raw.session_date)
    for cutoff in scan_minutes(raw.session_date, scanner.cfg):
        events, summary = scanner.step(raw, cutoff)
        if on_scan:
            on_scan(summary)
        for ev in events:
            if on_event:
                on_event(ev)
    return list(scanner.trades)


def stats(trades: list[Trade]) -> dict:
    closed = [t for t in trades if t.state == "CLOSED"]
    wins = [t for t in closed if t.net_r > 0]
    n = len(closed)
    return {"signals": len(trades), "filled": n, "expired": sum(1 for t in trades if t.state == "EXPIRED"),
            "wins": len(wins), "losses": n - len(wins), "win_rate": (len(wins) / n) if n else None,
            "gross_r": round(sum(t.gross_r for t in closed), 2), "net_r": round(sum(t.net_r for t in closed), 2),
            "avg_net_r": round(sum(t.net_r for t in closed) / n, 3) if n else None,
            "net_pct": round(sum(t.net_pct for t in closed), 3),
            "by_exit": {k: sum(1 for t in closed if t.exit_reason == k)
                        for k in ("TARGET", "STOP", "TIME_STOP", "SQUARE_OFF")}}


# ----------------------------------------------------------------------- rendering

def _hm(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone(IST).strftime("%H:%M") if iso else "--:--"


def render_signal(t: dict) -> str:
    buy = t["side"] > 0
    word = "above" if buy else "below"
    risk = t["risk"]
    lines = [
        "",
        "#" * 72,
        f"  945 SCAN SIGNAL #{t['number']}  |  {t['tier']}  |  {t['symbol']}  {t['direction']} ({'BUY' if buy else 'SELL SHORT'})",
        "#" * 72,
        f"Signal time  : {_hm(t['signal_time'])} IST (built from candles up to {t['signal_bar']})",
        f"Score        : {t['score']:.1f} / 100 | rank #{t['rank']} of all stocks x both directions | "
        f"on the shortlist {t['confirmed_scans']} scans in a row",
        f"ENTRY        : {'BUY' if buy else 'SELL'} STOP {word} Rs {t['trigger']:.2f} "
        f"(break of the {t['signal_bar']} candle {'high' if buy else 'low'})",
        f"               cancel if not triggered by {_hm(t['expires_at'])}",
        f"STOP LOSS    : Rs {t['stop']:.2f}   risk Rs {risk:.2f}/share ({risk / t['trigger'] * 100:.2f}%)",
        f"TARGET       : Rs {t['target']:.2f}   {abs(t['target'] - t['trigger']) / risk:.1f}R",
        f"COSTS        : ~{t['cost_pct']:.3f}% round trip -> net reward/risk {t['net_rr']:.2f}",
        f"TIME STOP    : exit at market {t.get('hold_minutes', 60)} min after the fill if neither is hit (15:15 at the latest)",
    ]
    if t.get("quantity") is not None:
        if t["quantity"] <= 0:
            lines.append("QUANTITY     : 0 -- SKIP this trade (one share risks more than your rupee risk)")
        else:
            lines.append(f"QUANTITY     : {t['quantity']} shares for the chosen rupee risk (incl. costs)")
    m = t.get("market") or {}
    br = m.get("breadth")
    mr = m.get("market_return_pct")
    lines.append(f"MARKET       : {m.get('regime')} | breadth {'n/a' if br is None else f'{br:.0%}'} green | "
                 f"{m.get('source')} {'n/a' if mr is None else f'{mr:+.2f}%'}")
    lines.append("WHY          :")
    lines += [f"  - {r}" for r in t.get("reasons", [])]
    lines.append("Score ranks setups; it is not a win probability. Judge it on 50+ graded trades.")
    lines.append("#" * 72)
    return "\n".join(lines)


def render_update(ev: Event) -> str:
    t = ev.trade
    if ev.kind == "DAY_LOCKED":
        return f"[{_hm(ev.at)}] DAY LOCKED: {ev.text}"
    if ev.kind == "FILLED":
        return (f"[{_hm(ev.at)}] FILLED  #{t['number']} {t['symbol']} {t['direction']} @ Rs {t['entry']:.2f} | "
                f"SL {t['stop']:.2f} | TP {t['target']:.2f} | time stop {_hm(t['time_stop'])}")
    if ev.kind == "EXPIRED":
        return f"[{_hm(ev.at)}] EXPIRED #{t['number']} {t['symbol']} {t['direction']}: {ev.text} -- no trade"
    return (f"[{_hm(ev.at)}] {ev.kind:<10} #{t['number']} {t['symbol']} {t['direction']} exit Rs {t['exit']:.2f} | "
            f"{t['gross_r']:+.2f}R gross, {t['net_r']:+.2f}R net of costs ({t['net_pct']:+.2f}%)")


def render_heartbeat(s: ScanSummary, cfg: ScanConfig) -> str:
    br = "n/a" if s.breadth is None else f"{s.breadth:.0%}"
    watch = ", ".join(f"{sym} {d} {sc:.1f} ({st}/{cfg.confirm_scans})" for sym, d, sc, st in s.watch) or "-"
    return (f"[{s.at:%H:%M}] {s.eligible}/{s.universe} scanned | market {s.regime} {br} green | "
            f"top: {watch} | {s.status}")


def render_day(trades: list[Trade], title: str = "DAY SUMMARY") -> str:
    st = stats(trades)
    lines = ["", "=" * 72, title, "=" * 72]
    for t in trades:
        if t.state == "CLOSED":
            lines.append(f"#{t.number} {_hm(t.signal_time)} {t.symbol:<12} {t.direction:<5} {t.tier}  "
                         f"{t.exit_reason:<10} {t.net_r:+.2f}R net ({t.net_pct:+.2f}%)")
        else:
            lines.append(f"#{t.number} {_hm(t.signal_time)} {t.symbol:<12} {t.direction:<5} {t.tier}  {t.state}")
    if not trades:
        lines.append("No signal today: nothing cleared Tier 1 with confirmation. That is a valid outcome.")
    wr = "n/a" if st["win_rate"] is None else f"{st['win_rate']:.0%}"
    lines.append(f"Signals {st['signals']} | filled {st['filled']} | expired {st['expired']} | "
                 f"wins {st['wins']} / losses {st['losses']} ({wr}) | net {st['net_r']:+.2f}R ({st['net_pct']:+.2f}%)")
    lines.append("=" * 72)
    return "\n".join(lines)
