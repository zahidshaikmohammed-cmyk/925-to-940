"""Research setups: detect -> arm -> trigger -> manage, on every stock, every bar.

Three setups with published (US) evidence, defined exactly so the engine can measure
them (see the "High-Probability Intraday Setups: NSE Research" doc):

    ORB  stocks-in-play opening range breakout: at 09:20 the 20 stocks with the highest
         relative volume in the 09:15 5-minute bar (RVOL >= 2) and a clear candle body
         arm a stop order at that bar's high (green) or low (red). Stop: the other side
         of the bar (default) or 10% of the daily ATR. Held to 15:15. Valid until 11:00.
    FHM  first-half-hour momentum: at 14:45 the 10 stocks whose 09:15-09:45 move
         (incl. the gap) was largest against their own normal size (|z| >= 1) and in the
         same direction as the market arm a stop order at the 14:30-14:45 range high/low.
         Stop: other side of that range. Held to 15:15. Valid until 15:05.
    VWT  VWAP trend pullback, 10:15-14:30: a stock that closed on one side of its VWAP in
         >= 80% of the last 12 bars with <= 2 crosses in the last 6, beats the market in
         that direction, and pulls back to within 0.3 ATR of VWAP on lighter volume,
         arms a stop order at the pullback bar's high/low (best 5 per bar, 20 a day by
         relative strength). Stop: 0.5 ATR beyond VWAP.
         Exit: a 5-minute close back through VWAP, or 15:15. Valid for 2 bars.

Every setup: liquid stocks only, and skipped when round-trip costs would be more than
half the risk. VWT and FHM trade with the market regime (share of stocks above VWAP).

Arming reads only completed 5-minute bars (built from the 1-minute feed live, or from
history in the backtest). Triggers and stops are checked on the finest bars available
(1-minute live, 5-minute in the backtest). A bar that touches both stop and target-side
exit counts the stop. The same code runs live and in the backtest.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median

from .history import IST, SESSION_OPEN, Bar, Baseline, HistoryIndex

FIVE = timedelta(minutes=5)
ONE = timedelta(minutes=1)
NAMES = {"ORB": "Stocks-in-play opening range breakout",
         "FHM": "First-half-hour momentum",
         "VWT": "VWAP trend pullback"}


@dataclass(frozen=True)
class SetupConfig:
    enabled: tuple = ("ORB", "FHM", "VWT")
    min_turnover_5m: float = 1_000_000.0     # Rs 10 lakh per 5-min bar (median of last 5 days)
    max_cost_r: float = 0.5                  # skip when costs > 0.5R
    square_off: time = time(15, 15)
    regime_long: float = 0.6                 # >= 60% of stocks above VWAP: longs only
    regime_short: float = 0.4
    # ORB
    orb_rvol: float = 2.0
    orb_top: int = 20
    orb_body: float = 0.2
    orb_stop: str = "range"                  # "range" | "atr10" (the paper's 10% of daily ATR)
    orb_atr_frac: float = 0.10
    orb_until: time = time(11, 0)
    # FHM
    fhm_z: float = 1.0
    fhm_top: int = 10
    fhm_range_start: time = time(14, 30)
    fhm_range_end: time = time(14, 45)
    fhm_until: time = time(15, 5)
    # VWT
    vwt_from: time = time(10, 15)
    vwt_to: time = time(14, 30)
    vwt_look: int = 12
    vwt_side: float = 0.8
    vwt_cross_look: int = 6
    vwt_max_cross: int = 2
    vwt_pull_atr: float = 0.3
    vwt_stop_atr: float = 0.5
    vwt_valid_bars: int = 2
    vwt_top: int = 5                         # best 5 per bar by relative strength
    vwt_max_day: int = 20                    # at most 20 VWT setups armed per day
    atr_bars: int = 14


# --------------------------------------------------------------------------- bars

@dataclass
class DayBars:
    """One stock, one day, 5-minute bars with causal VWAP and ATR."""
    symbol: str
    ts: list
    o: list
    h: list
    l: list
    c: list
    v: list
    vwap: list
    atr: list

    def done(self, now: datetime) -> int:
        """Number of bars complete at `now`."""
        k = 0
        while k < len(self.ts) and self.ts[k] + FIVE <= now:
            k += 1
        return k


def build_day(symbol: str, bars: list[Bar], atr_bars: int = 14) -> DayBars:
    ts, o, h, l, c, v = ([b[i] for b in bars] for i in range(6))
    vwap, atr, trs = [], [], []
    pv = vol = 0.0
    for i in range(len(bars)):
        pv += (h[i] + l[i] + c[i]) / 3 * v[i]
        vol += v[i]
        vwap.append(pv / vol if vol > 0 else c[i])
        trs.append(h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
        w = trs[-atr_bars:]
        atr.append(sum(w) / len(w))
    return DayBars(symbol, ts, o, h, l, c, v, vwap, atr)


def to_five_minute(one_minute: list[Bar], now: datetime) -> list[Bar]:
    """Aggregate 1-minute bars into 5-minute bars that are complete at `now`."""
    out, cur, key = [], None, None
    for b in one_minute:
        t = b[0]
        mins = (t.hour * 60 + t.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
        if mins < 0:
            continue
        start = t.replace(second=0, microsecond=0) - timedelta(minutes=mins % 5)
        if start + FIVE > now:
            break
        if start != key:
            if cur:
                out.append(tuple(cur))
            key, cur = start, [start, b[1], b[2], b[3], b[4], b[5]]
        else:
            cur[2], cur[3], cur[4], cur[5] = max(cur[2], b[2]), min(cur[3], b[3]), b[4], cur[5] + b[5]
    if cur:
        out.append(tuple(cur))
    return out


# ------------------------------------------------------------------------- trades

@dataclass
class SetupTrade:
    id: str
    day: str
    setup: str
    symbol: str
    direction: str                     # LONG | SHORT
    side: int
    armed_at: str
    trigger: float
    stop: float
    valid_until: str
    cost_pct: float
    facts: list = field(default_factory=list)
    state: str = "ARMED"               # ARMED | OPEN | CLOSED | EXPIRED
    entry: float | None = None
    entry_time: str | None = None
    exit: float | None = None
    exit_time: str | None = None
    exit_reason: str | None = None
    gross_r: float | None = None
    net_r: float | None = None
    net_pct: float | None = None
    seen_to: str = ""                  # bars before this time are processed

    @property
    def live(self) -> bool:
        return self.state in ("ARMED", "OPEN")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SetupTrade":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class SetupEvent:
    kind: str                          # ARMED | TRIGGERED | EXPIRED | STOP | EXIT_VWAP | SQUARE_OFF
    at: str
    trade: dict


# ------------------------------------------------------------------------- engine

class SetupEngine:
    def __init__(self, cfg: SetupConfig, cost_pct: float):
        self.cfg, self.cost_pct = cfg, cost_pct
        self.reset(None)

    def reset(self, day: date | None) -> None:
        self.day = day
        self.trades: list[SetupTrade] = []
        self.done: set = set()             # (setup, symbol) already armed today
        self.orb_checked = self.fhm_checked = False
        self.vwt_seen: dict = {}           # symbol -> bars already evaluated for VWT

    def restore(self, day: date, trades: list[SetupTrade]) -> None:
        self.reset(day)
        self.trades = list(trades)
        self.done = {(t.setup, t.symbol) for t in trades}
        self.orb_checked = any(t.setup == "ORB" for t in trades)
        self.fhm_checked = any(t.setup == "FHM" for t in trades)

    # ---------------------------------------------------------------- one step
    def step(self, day: date, bars: dict[str, DayBars], base: dict[str, Baseline], now: datetime,
             fine: dict[str, list[Bar]] | None = None) -> list[SetupEvent]:
        if self.day != day:
            self.reset(day)
        events: list[SetupEvent] = []
        for t in [t for t in self.trades if t.live]:
            events += self._manage(t, bars.get(t.symbol), (fine or {}).get(t.symbol), now)
        cfg = self.cfg
        liquid = [s for s, b in bars.items()
                  if s in base and (base[s].turnover_5m or 0) >= cfg.min_turnover_5m and b.ts]
        ks = {s: bars[s].done(now) for s in liquid}
        live = [s for s in liquid if ks[s] > 0]
        if not live:
            return events
        above = sum(1 for s in live if bars[s].c[ks[s] - 1] > bars[s].vwap[ks[s] - 1]) / len(live)
        regime = "UP" if above >= cfg.regime_long else ("DOWN" if above <= cfg.regime_short else "MIXED")
        t_now = now.astimezone(IST).time()
        if "ORB" in cfg.enabled and not self.orb_checked and t_now >= time(9, 20):
            self.orb_checked = True
            events += self._arm_orb(bars, base, ks, live, now)
        if "FHM" in cfg.enabled and not self.fhm_checked and t_now >= cfg.fhm_range_end:
            self.fhm_checked = True
            events += self._arm_fhm(bars, base, ks, live, now, regime)
        if "VWT" in cfg.enabled:
            events += self._arm_vwt(bars, ks, live, now, regime, above)
        return events

    # ---------------------------------------------------------------- arming
    def _arm(self, setup, sym, d, trigger, stop, now, until, facts) -> SetupEvent | None:
        risk = abs(trigger - stop)
        if risk <= 0 or d * (trigger - stop) <= 0:
            return None
        cost_r = self.cost_pct / 100 * trigger / risk
        if cost_r > self.cfg.max_cost_r:
            return None
        self.done.add((setup, sym))
        t = SetupTrade(id=f"{now.date().isoformat()}-{setup}-{sym}", day=now.date().isoformat(), setup=setup,
                       symbol=sym, direction="LONG" if d > 0 else "SHORT", side=d, armed_at=now.isoformat(),
                       trigger=round(trigger, 2), stop=round(stop, 2), valid_until=until.isoformat(),
                       cost_pct=round(self.cost_pct, 4),
                       facts=facts + [f"costs {cost_r:.2f}R of a {risk / trigger * 100:.2f}% risk"],
                       seen_to=now.isoformat())
        self.trades.append(t)
        return SetupEvent("ARMED", now.isoformat(), t.to_dict())

    def _arm_orb(self, bars, base, ks, live, now):
        cfg = self.cfg
        cands = []
        for s in live:
            b, bl = bars[s], base[s]
            if b.ts[0].time() != SESSION_OPEN or not bl.rvol_base:
                continue
            rvol = b.v[0] / bl.rvol_base
            rng, body = b.h[0] - b.l[0], b.c[0] - b.o[0]
            if rvol < cfg.orb_rvol or rng <= 0 or abs(body) < cfg.orb_body * rng:
                continue
            cands.append((rvol, s))
        cands.sort(reverse=True)
        events = []
        until = datetime.combine(now.date(), cfg.orb_until, IST)
        for rank, (rvol, s) in enumerate(cands[:cfg.orb_top], 1):
            b, bl = bars[s], base[s]
            d = 1 if b.c[0] > b.o[0] else -1
            trigger = b.h[0] if d > 0 else b.l[0]
            if cfg.orb_stop == "atr10" and bl.atr_daily:
                stop = trigger - d * cfg.orb_atr_frac * bl.atr_daily
            else:
                stop = b.l[0] if d > 0 else b.h[0]
            ev = self._arm("ORB", s, d, trigger, stop, now, until,
                           [f"RVOL {rvol:.1f}x its 14-day median in the 09:15 bar (#{rank} of {len(cands)} stocks in play)",
                            f"09:15 bar closed {'green' if d > 0 else 'red'}: body {abs(b.c[0] - b.o[0]) / (b.h[0] - b.l[0]):.0%} of its range"])
            if ev:
                events.append(ev)
        return events

    def _arm_fhm(self, bars, base, ks, live, now, regime):
        cfg = self.cfg
        rows = []
        for s in live:
            b, bl = bars[s], base[s]
            i940 = next((i for i in range(ks[s]) if b.ts[i].time() == time(9, 40)), None)
            if i940 is None or not bl.fh_sd or not bl.prev_close:
                continue
            rows.append((s, b.c[i940] / bl.prev_close - 1, bl.fh_sd))
        if not rows:
            return []
        mkt = median(r for _, r, _ in rows)
        cands = []
        for s, r, sd in rows:
            z = r / sd if sd > 0 else 0.0
            d = 1 if r > 0 else -1
            if abs(z) < cfg.fhm_z or (mkt > 0) != (r > 0):
                continue
            if (regime == "UP" and d < 0) or (regime == "DOWN" and d > 0):
                continue
            cands.append((abs(z), s, r, z, d))
        cands.sort(reverse=True)
        events = []
        until = datetime.combine(now.date(), cfg.fhm_until, IST)
        for _, s, r, z, d in cands[:cfg.fhm_top]:
            b = bars[s]
            idx = [i for i in range(ks[s]) if cfg.fhm_range_start <= b.ts[i].time() < cfg.fhm_range_end]
            if not idx:
                continue
            hi, lo = max(b.h[i] for i in idx), min(b.l[i] for i in idx)
            ev = self._arm("FHM", s, d, hi if d > 0 else lo, lo if d > 0 else hi, now, until,
                           [f"09:15-09:45 move {r * 100:+.2f}% incl. gap = {z:+.1f} s.d. of its last 14 sessions",
                            f"market's first half-hour {mkt * 100:+.2f}% (median stock), regime {regime}"])
            if ev:
                events.append(ev)
        return events

    def _arm_vwt(self, bars, ks, live, now, regime, above):
        cfg = self.cfg
        cands = []
        rets = {s: bars[s].c[ks[s] - 1] / bars[s].o[0] - 1 for s in live}
        mkt = median(rets.values())
        for s in live:
            b, k = bars[s], ks[s]
            if self.vwt_seen.get(s) == k:
                continue
            self.vwt_seen[s] = k
            i = k - 1
            end = b.ts[i] + FIVE
            if not (cfg.vwt_from <= end.time() <= cfg.vwt_to) or i < cfg.vwt_look:
                continue
            look = range(i - cfg.vwt_look + 1, i + 1)
            side = [1 if b.c[j] > b.vwap[j] else -1 for j in look]
            ups = side.count(1) / len(side)
            d = 1 if ups >= cfg.vwt_side else (-1 if 1 - ups >= cfg.vwt_side else 0)
            if d == 0 or ("VWT", s) in self.done:
                continue
            if (regime == "UP" and d < 0) or (regime == "DOWN" and d > 0):
                continue
            recent = side[-cfg.vwt_cross_look:]
            crosses = sum(1 for a, z in zip(recent, recent[1:]) if a != z)
            if crosses > cfg.vwt_max_cross or d * (rets[s] - mkt) <= 0:
                continue
            atr = b.atr[i]
            near = (b.l[i] - b.vwap[i] if d > 0 else b.vwap[i] - b.h[i]) <= cfg.vwt_pull_atr * atr
            held = d * (b.c[i] - b.vwap[i]) > 0
            avg_v = sum(b.v[j] for j in range(i - cfg.vwt_look, i)) / cfg.vwt_look
            if not (near and held and b.v[i] < avg_v):
                continue
            cands.append((d * (rets[s] - mkt), s, d, i, ups, crosses))
        cands.sort(reverse=True)
        events = []
        room = cfg.vwt_max_day - sum(1 for t in self.trades if t.setup == "VWT")
        for rs, s, d, i, ups, crosses in cands[:max(0, min(cfg.vwt_top, room))]:
            b = bars[s]
            trigger = b.h[i] if d > 0 else b.l[i]
            stop = b.vwap[i] - d * cfg.vwt_stop_atr * b.atr[i]
            ev = self._arm("VWT", s, d, trigger, stop, now, now + cfg.vwt_valid_bars * FIVE,
                           [f"{(ups if d > 0 else 1 - ups):.0%} of the last {cfg.vwt_look} 5-min closes "
                            f"{'above' if d > 0 else 'below'} VWAP, {crosses} cross(es) in the last {cfg.vwt_cross_look}",
                            f"{rets[s] * 100:+.2f}% since the open vs market {mkt * 100:+.2f}%; pulled back to VWAP "
                            f"Rs {b.vwap[i]:.2f} on lighter volume",
                            f"market regime {regime} ({above:.0%} of stocks above VWAP)"])
            if ev:
                events.append(ev)
        return events

    # ---------------------------------------------------------------- management
    def _manage(self, t: SetupTrade, b: DayBars | None, fine: list[Bar] | None, now: datetime) -> list[SetupEvent]:
        events: list[SetupEvent] = []
        seen = datetime.fromisoformat(t.seen_to)
        until = datetime.fromisoformat(t.valid_until)
        square = datetime.combine(now.date(), self.cfg.square_off, IST)
        if fine is not None:
            rows, width = [x for x in fine if x[0] >= seen and x[0] + ONE <= now], ONE
        elif b is not None:
            rows = [(b.ts[i], b.o[i], b.h[i], b.l[i], b.c[i], b.v[i]) for i in range(b.done(now)) if b.ts[i] >= seen]
            width = FIVE
        else:
            rows, width = [], ONE
        d = t.side
        for ts, o, h, l, c, _ in rows:
            t.seen_to = (ts + width).isoformat()
            fill_bar = False
            if t.state == "ARMED":
                if ts >= until:
                    break
                if not ((d > 0 and h >= t.trigger) or (d < 0 and l <= t.trigger)):
                    continue
                t.entry = round(max(t.trigger, o) if d > 0 else min(t.trigger, o), 2)
                t.entry_time, t.state, fill_bar = ts.isoformat(), "OPEN", True
                events.append(SetupEvent("TRIGGERED", ts.isoformat(), t.to_dict()))
            if (d > 0 and l <= t.stop) or (d < 0 and h >= t.stop):
                gap = not fill_bar and d * (o - t.stop) < 0
                self._close(t, o if gap else t.stop, ts, "STOP", events)
                return events
            if ts + width >= square:
                self._close(t, c, ts, "SQUARE_OFF", events)
                return events
        if t.state == "OPEN" and t.setup == "VWT" and b is not None:
            k = b.done(now)
            entered = datetime.fromisoformat(t.entry_time)
            for i in range(k):
                if b.ts[i] + FIVE <= entered or b.ts[i] + FIVE > now:
                    continue
                if d * (b.c[i] - b.vwap[i]) < 0:
                    self._close(t, b.c[i], b.ts[i], "EXIT_VWAP", events)
                    return events
        if t.state == "ARMED" and now >= until:
            t.state = "EXPIRED"
            events.append(SetupEvent("EXPIRED", now.isoformat(), t.to_dict()))
        return events

    @staticmethod
    def _close(t: SetupTrade, price: float, ts: datetime, reason: str, events: list) -> None:
        t.exit, t.exit_time, t.exit_reason, t.state = round(price, 2), ts.isoformat(), reason, "CLOSED"
        risk = abs(t.entry - t.stop) or abs(t.trigger - t.stop)
        move = t.side * (t.exit - t.entry)
        t.gross_r = round(move / risk, 3)
        t.net_r = round((move - t.cost_pct / 100 * t.entry) / risk, 3)
        t.net_pct = round(move / t.entry * 100 - t.cost_pct, 4)
        events.append(SetupEvent(reason, ts.isoformat(), t.to_dict()))


# ------------------------------------------------------------------------- backtest

def session_steps(day: date):
    t = datetime.combine(day, time(9, 20), IST)
    end = datetime.combine(day, time(15, 20), IST)
    while t <= end:
        yield t
        t += FIVE


def backtest(index: HistoryIndex, cfg: SetupConfig, cost_pct: float, min_prior: int = 10,
             say=None) -> list[SetupTrade]:
    days = index.days()
    trades: list[SetupTrade] = []
    for n, day in enumerate(days):
        if n < min_prior:
            continue
        base = index.baselines(day)
        bars = {s: build_day(s, rows, cfg.atr_bars) for s in index.daily_bars
                if (rows := index.bars_on(s, day))}
        eng = SetupEngine(cfg, cost_pct)
        for now in session_steps(day):
            eng.step(day, bars, base, now)
        trades += eng.trades
        if say:
            closed = [t for t in eng.trades if t.state == "CLOSED"]
            say(f"  {day}: {len(eng.trades)} armed, {len(closed)} traded, "
                f"net {sum(t.net_r for t in closed):+.1f}R")
    return trades


def setup_stats(trades: list[SetupTrade], min_trades: int = 30) -> dict:
    out = {}
    for setup in NAMES:
        mine = [t for t in trades if t.setup == setup]
        closed = sorted((t for t in mine if t.state == "CLOSED"), key=lambda t: t.entry_time)
        n = len(closed)
        wins = [t for t in closed if t.net_r > 0]
        gain = sum(t.net_r for t in wins)
        loss = -sum(t.net_r for t in closed if t.net_r <= 0)
        half = n // 2
        avg = lambda xs: round(sum(t.net_r for t in xs) / len(xs), 3) if xs else None
        first, second = avg(closed[:half]), avg(closed[half:])
        avg_all = avg(closed)
        if n < min_trades:
            status = "UNPROVEN"
        elif avg_all > 0 and (first or 0) > 0 and (second or 0) > 0:
            status = "ACTIVE"
        else:
            status = "MUTED"
        out[setup] = {"name": NAMES[setup], "armed": len(mine), "trades": n,
                      "win_rate": round(len(wins) / n, 3) if n else None,
                      "avg_net_r": avg_all, "total_net_r": round(sum(t.net_r for t in closed), 2),
                      "avg_gross_r": round(sum(t.gross_r for t in closed) / n, 3) if n else None,
                      "profit_factor": round(gain / loss, 2) if loss > 0 else None,
                      "first_half_avg_r": first, "second_half_avg_r": second,
                      "exits": {k: sum(1 for t in closed if t.exit_reason == k)
                                for k in ("STOP", "EXIT_VWAP", "SQUARE_OFF")},
                      "status": status}
    return out


def render_stats(stats: dict, title: str) -> str:
    lines = ["", "=" * 96, title, "=" * 96,
             f"{'Setup':<40}{'Trades':>7}{'Win%':>7}{'AvgNetR':>9}{'TotalR':>9}{'PF':>6}"
             f"{'1stHalf':>9}{'2ndHalf':>9}  Status"]
    for k, s in stats.items():
        f = lambda x, p=2: "-" if x is None else f"{x:+.{p}f}"
        wr = "-" if s["win_rate"] is None else f"{s['win_rate']:.0%}"
        pf = "-" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
        lines.append(f"{k + ' ' + s['name']:<40}{s['trades']:>7}{wr:>7}{f(s['avg_net_r']):>9}"
                     f"{f(s['total_net_r'], 1):>9}{pf:>6}{f(s['first_half_avg_r']):>9}{f(s['second_half_avg_r']):>9}  {s['status']}")
    lines += ["R is measured after costs. ACTIVE = 30+ trades and positive in both halves of the period;",
              "MUTED setups are still detected and logged live, but never beep. UNPROVEN = too few trades.",
              "=" * 96]
    return "\n".join(lines)


def save_stats(path: Path, stats: dict, days: list[date]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"computed_at": datetime.now(IST).isoformat(),
                                "from": days[0].isoformat() if days else None,
                                "to": days[-1].isoformat() if days else None, "setups": stats}, indent=2))


def load_stats(path: Path) -> dict:
    try:
        return json.loads(path.read_text()).get("setups", {})
    except Exception:
        return {}


# ------------------------------------------------------------------------- live journal + text

class SetupJournal:
    FIELDS = ("day", "setup", "symbol", "direction", "armed_at", "trigger", "stop", "state", "entry",
              "entry_time", "exit", "exit_time", "exit_reason", "gross_r", "net_r", "net_pct")

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, day: str) -> Path:
        return self.root / f"{day}.jsonl"

    def write(self, ev: SetupEvent) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path(ev.at[:10]).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": ev.kind, "at": ev.at, "trade": ev.trade}, separators=(",", ":")) + "\n")
        if ev.trade["state"] in ("CLOSED", "EXPIRED"):
            p = self.root / "trades.csv"
            new = not p.exists()
            with p.open("a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=self.FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(ev.trade)

    def trades(self, day: str) -> list[SetupTrade]:
        latest = {}
        p = self.path(day)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                latest[row["trade"]["id"]] = SetupTrade.from_dict(row["trade"])
        return list(latest.values())


def _hm(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone(IST).strftime("%H:%M") if iso else "--:--"


def record_line(stats: dict, setup: str) -> str:
    s = stats.get(setup)
    if not s or not s.get("trades"):
        return "no backtest yet (run: python 945.py --setup-backtest)"
    return (f"{s['status']}: {s['trades']} backtest trades, {s['win_rate']:.0%} wins, "
            f"{s['avg_net_r']:+.2f}R avg after costs")


def render_trigger(t: dict, stats: dict, risk_rupees: float | None = None) -> str:
    buy = t["side"] > 0
    risk = abs(t["entry"] - t["stop"])
    plan = {"ORB": "hold to 15:15 unless stopped (no fixed target: this setup earns from trend days)",
            "FHM": "hold to 15:15 unless stopped",
            "VWT": "exit on a 5-min close back through VWAP, or 15:15"}[t["setup"]]
    lines = ["", "*" * 72,
             f"  SETUP TRIGGERED  |  {NAMES[t['setup']].upper()}  |  {t['symbol']}  {t['direction']}",
             "*" * 72,
             f"Triggered    : {_hm(t['entry_time'])} IST at Rs {t['entry']:.2f} ({'buy' if buy else 'sell'} stop "
             f"Rs {t['trigger']:.2f}, armed {_hm(t['armed_at'])})",
             f"STOP LOSS    : Rs {t['stop']:.2f}   risk Rs {risk:.2f}/share ({risk / t['entry'] * 100:.2f}%)",
             f"EXIT PLAN    : {plan}"]
    if risk_rupees:
        lines.append(f"QUANTITY     : {int(risk_rupees // (risk + t['cost_pct'] / 100 * t['entry']))} shares "
                     f"for Rs {risk_rupees:,.0f} risk incl. costs")
    lines.append("WHY THIS SETUP:")
    lines += [f"  - {x}" for x in t.get("facts", [])]
    lines.append(f"TRACK RECORD : {record_line(stats, t['setup'])}")
    lines.append("*" * 72)
    return "\n".join(lines)


def render_setup_update(ev: SetupEvent, stats: dict) -> str:
    t = ev.trade
    head = f"[{_hm(ev.at)}] {t['setup']} {t['symbol']} {t['direction']}"
    if ev.kind == "ARMED":
        word = "above" if t["side"] > 0 else "below"
        status = (stats.get(t["setup"]) or {}).get("status", "UNPROVEN")
        return f"{head} ARMED: trigger {word} Rs {t['trigger']:.2f}, stop Rs {t['stop']:.2f}, until {_hm(t['valid_until'])} [{status}]"
    if ev.kind == "TRIGGERED":
        return f"{head} TRIGGERED at Rs {t['entry']:.2f} (muted setup: logged, no alert)"
    if ev.kind == "EXPIRED":
        return f"{head} expired untriggered"
    return (f"{head} {ev.kind} at Rs {t['exit']:.2f}: {t['gross_r']:+.2f}R gross, {t['net_r']:+.2f}R net "
            f"({t['net_pct']:+.2f}%)")


# ------------------------------------------------------------------------- live adapter

class LiveSetups:
    """Runs the setup engine on the live 1-minute feed once a minute."""

    def __init__(self, engine: SetupEngine, base: dict[str, Baseline], stats: dict, journal: SetupJournal,
                 day: date):
        self.engine, self.base, self.stats, self.journal, self.day = engine, base, stats, journal, day
        done = journal.trades(day.isoformat())
        if done:
            engine.restore(day, done)

    def muted(self, setup: str) -> bool:
        return (self.stats.get(setup) or {}).get("status") == "MUTED"

    def on_scan(self, stocks: dict, cutoff: datetime) -> list[SetupEvent]:
        """`stocks`: symbol -> 1-minute Series (selector_data.Series) of today."""
        one, bars = {}, {}
        for sym, s in stocks.items():
            rows = [(s.ts[i], s.o[i], s.h[i], s.l[i], s.c[i], s.v[i]) for i in range(len(s)) if s.ts[i] < cutoff]
            if not rows:
                continue
            one[sym] = rows
            five = to_five_minute(rows, cutoff)
            if five:
                bars[sym] = build_day(sym, five, self.engine.cfg.atr_bars)
        fine = {t.symbol: one.get(t.symbol, []) for t in self.engine.trades if t.live}
        events = self.engine.step(self.day, bars, self.base, cutoff, fine)
        for ev in events:
            self.journal.write(ev)
        return events

    def summary(self) -> str:
        trades = self.engine.trades
        lines = ["", "SETUPS TODAY"]
        if not trades:
            lines.append("  nothing armed")
        for t in trades:
            tail = (f"{t.exit_reason} {t.net_r:+.2f}R net" if t.state == "CLOSED" else t.state)
            lines.append(f"  {_hm(t.armed_at)} {t.setup} {t.symbol:<12} {t.direction:<5} {tail}"
                         f"{'  (muted)' if self.muted(t.setup) else ''}")
        return "\n".join(lines)
