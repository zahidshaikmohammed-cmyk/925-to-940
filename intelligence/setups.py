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

import bisect
import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median

from .history import IST, SESSION_OPEN, Bar, Baseline, HistoryIndex, to_five_minute  # noqa: F401

FIVE = timedelta(minutes=5)
ONE = timedelta(minutes=1)
EMPTY_BASE = Baseline(days=0, prev_close=None, atr_daily=None, rvol_base=None, fh_sd=None, turnover_5m=None)
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
    orb_gap_proxy: float = 1.5               # day-one mode (no volume history): |gap| >= 1.5%
    orb_turnover_pct: float = 0.7            # ... and opening turnover in the top 30%
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
        """Number of bars complete at `now` (bars are in time order)."""
        return bisect.bisect_right(self.ts, now - FIVE)


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
    vwap_checked_to: str = ""          # VWT: 5-min bars ending at or before this are checked

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
        self.near: list[dict] = []         # stocks that failed exactly one condition
        self.diag: dict = {}               # e.g. median RVOL at 09:20 (volume-scale check)

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
        base = {s: base.get(s) or EMPTY_BASE for s in bars}
        ks = {s: b.done(now) for s, b in bars.items() if b.ts}
        live = [s for s, k in ks.items() if k > 0 and self._turnover(bars[s], base[s], k) >= cfg.min_turnover_5m]
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
    @staticmethod
    def _turnover(b: DayBars, bl: Baseline, k: int) -> float:
        """Median rupee turnover per 5-min bar: from history, else today's bars so far."""
        if bl.turnover_5m:
            return bl.turnover_5m
        return median(b.c[i] * b.v[i] for i in range(k))

    def _cost_r(self, trigger: float, stop: float) -> float:
        risk = abs(trigger - stop)
        return self.cost_pct / 100 * trigger / risk if risk > 0 else float("inf")

    def _note(self, setup: str, now: datetime, sym: str, d: int, checks: list) -> None:
        """Remember a stock that failed exactly one condition (for the daily audit)."""
        failed = [(name, detail) for name, ok, detail in checks if not ok]
        if len(failed) == 1:
            self.near.append({"setup": setup, "at": now.isoformat(), "symbol": sym,
                              "direction": "LONG" if d > 0 else "SHORT",
                              "failed": failed[0][0], "detail": failed[0][1]})

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
        """Paper order: in-play threshold, top N, then trade the first candle's direction.

        In play = RVOL >= 2 (opening-bar volume vs its own 14-day median). Without volume
        history (day-one mode) the stand-in is a gap of >= 1.5% with opening turnover in
        the top 30% of the market, ranked by gap size."""
        cfg = self.cfg
        opened = [s for s in live if bars[s].ts[0].time() == SESSION_OPEN]
        measured = [s for s in opened if base[s].rvol_base]
        proxy = len(measured) < 0.5 * max(1, len(opened))
        self.diag["orb_mode"] = "DAY-ONE PROXY (gap + opening turnover)" if proxy else "RVOL"
        scores = []                                   # (score, symbol, passes threshold, label)
        if not proxy:
            for s in measured:
                r = bars[s].v[0] / base[s].rvol_base
                scores.append((r, s, r >= cfg.orb_rvol, f"RVOL {r:.2f}x, needs {cfg.orb_rvol:.1f}x"))
            if scores:
                self.diag["orb_median_rvol"] = round(median(x[0] for x in scores), 3)
                self.diag["orb_stocks_measured"] = len(scores)
        else:
            turn = sorted(bars[s].c[0] * bars[s].v[0] for s in opened)
            for s in opened:
                pc = base[s].prev_close
                if not pc:
                    continue
                b = bars[s]
                gap = (b.o[0] / pc - 1) * 100
                pct = bisect.bisect_left(turn, b.c[0] * b.v[0]) / max(1, len(turn) - 1)
                ok = abs(gap) >= cfg.orb_gap_proxy and pct >= cfg.orb_turnover_pct
                scores.append((abs(gap), s, ok,
                               f"gap {gap:+.2f}% (needs {cfg.orb_gap_proxy:.1f}%), opening turnover in the top "
                               f"{(1 - pct) * 100:.0f}% (needs top {(1 - cfg.orb_turnover_pct) * 100:.0f}%)"))
            self.diag["orb_stocks_measured"] = len(scores)
        scores.sort(key=lambda x: (-x[0], x[1]))
        ranked = {s: i + 1 for i, (_, s, _, _) in enumerate(scores)}
        in_play = [x for x in scores if x[2]]
        rank_in_play = {s: i + 1 for i, (_, s, _, _) in enumerate(in_play)}
        events = []
        until = datetime.combine(now.date(), cfg.orb_until, IST)
        for score, s, ok, label in scores:
            b, bl = bars[s], base[s]
            rng = b.h[0] - b.l[0]
            if rng <= 0:
                continue
            d = 1 if b.c[0] >= b.o[0] else -1
            body = abs(b.c[0] - b.o[0]) / rng
            trigger = b.h[0] if d > 0 else b.l[0]
            if cfg.orb_stop == "atr10" and bl.atr_daily:
                stop = trigger - d * cfg.orb_atr_frac * bl.atr_daily
            else:
                stop = b.l[0] if d > 0 else b.h[0]
            cost_r = self._cost_r(trigger, stop)
            top = ok and rank_in_play[s] <= cfg.orb_top
            checks = [("in play", ok, label),
                      ("top rank", (not ok) or top, f"#{rank_in_play.get(s, ranked[s])} in play, needs top {cfg.orb_top}"),
                      ("candle body", body >= cfg.orb_body, f"body {body:.0%} of range, needs {cfg.orb_body:.0%}"),
                      ("costs", cost_r <= cfg.max_cost_r, f"costs {cost_r:.2f}R, max {cfg.max_cost_r:.2f}R")]
            if not (top and body >= cfg.orb_body and cost_r <= cfg.max_cost_r):
                if ranked[s] <= 3 * cfg.orb_top:
                    self._note("ORB", now, s, d, checks)
                continue
            why = (f"RVOL {score:.1f}x its 14-day median in the 09:15 bar" if not proxy else
                   f"DAY-ONE PROXY (no volume history yet): {label}")
            ev = self._arm("ORB", s, d, trigger, stop, now, until,
                           [f"{why} (#{rank_in_play[s]} of {len(in_play)} stocks in play)",
                            f"09:15 bar closed {'green' if d > 0 else 'red'}: body {body:.0%} of its range"])
            if ev:
                events.append(ev)
        return events

    def _arm_fhm(self, bars, base, ks, live, now, regime):
        cfg = self.cfg
        rows = []
        for s in live:
            b, bl = bars[s], base[s]
            i940 = next((i for i in range(ks[s]) if b.ts[i].time() == time(9, 40)), None)
            if i940 is None or not bl.prev_close:
                continue
            sd, src = bl.fh_sd, "of its last 14 sessions"
            if not sd and ks[s] > 20:
                rets = [b.c[i] / b.c[i - 1] - 1 for i in range(1, ks[s])]
                m = sum(rets) / len(rets)
                sd = (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5 * 6 ** 0.5
                src = "of today's own half-hour volatility (day-one proxy)"
            if not sd:
                continue
            rows.append((s, b.c[i940] / bl.prev_close - 1, sd, src))
        if not rows:
            return []
        mkt = median(r for _, r, _, _ in rows)
        src_of = {s: src for s, _, _, src in rows}
        scored = []
        for s, r, sd, _ in rows:
            z = r / sd if sd > 0 else 0.0
            d = 1 if r > 0 else -1
            b = bars[s]
            idx = [i for i in range(ks[s]) if cfg.fhm_range_start <= b.ts[i].time() < cfg.fhm_range_end]
            hi = max(b.h[i] for i in idx) if idx else None
            lo = min(b.l[i] for i in idx) if idx else None
            trig, stop = (hi, lo) if d > 0 else (lo, hi)
            cost_r = self._cost_r(trig, stop) if idx else float("inf")
            checks = [("move size", abs(z) >= cfg.fhm_z, f"first half-hour {z:+.2f} s.d., needs {cfg.fhm_z:.1f}"),
                      ("market direction", (mkt > 0) == (r > 0), f"stock {r * 100:+.2f}% vs market {mkt * 100:+.2f}%"),
                      ("regime", not ((regime == "UP" and d < 0) or (regime == "DOWN" and d > 0)), f"regime {regime}"),
                      ("14:30-14:45 range", bool(idx), "no candles in the range"),
                      ("costs", cost_r <= cfg.max_cost_r, f"costs {cost_r:.2f}R, max {cfg.max_cost_r:.2f}R")]
            scored.append((abs(z), s, r, z, d, trig, stop, checks))
        scored.sort(key=lambda x: (-x[0], x[1]))
        events = []
        until = datetime.combine(now.date(), cfg.fhm_until, IST)
        taken = 0
        for _, s, r, z, d, trig, stop, checks in scored:
            if all(ok for _, ok, _ in checks) and taken < cfg.fhm_top:
                ev = self._arm("FHM", s, d, trig, stop, now, until,
                               [f"09:15-09:45 move {r * 100:+.2f}% incl. gap = {z:+.1f} s.d. {src_of[s]}",
                                f"market's first half-hour {mkt * 100:+.2f}% (median stock), regime {regime}"])
                if ev:
                    events.append(ev)
                    taken += 1
            elif all(ok for _, ok, _ in checks):
                self._note("FHM", now, s, d, checks + [("top-10 rank", False, f"ranked below the top {cfg.fhm_top}")])
            elif abs(z) >= 0.75 * cfg.fhm_z:
                self._note("FHM", now, s, d, checks)
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
            if not (cfg.vwt_from <= end.time() <= cfg.vwt_to) or i < cfg.vwt_look or ("VWT", s) in self.done:
                continue
            look = range(i - cfg.vwt_look + 1, i + 1)
            side = [1 if b.c[j] > b.vwap[j] else -1 for j in look]
            ups = side.count(1) / len(side)
            d = 1 if ups >= 0.5 else -1
            share = ups if d > 0 else 1 - ups
            recent = side[-cfg.vwt_cross_look:]
            crosses = sum(1 for a, z in zip(recent, recent[1:]) if a != z)
            atr = b.atr[i]
            gap = b.l[i] - b.vwap[i] if d > 0 else b.vwap[i] - b.h[i]
            avg_v = sum(b.v[j] for j in range(i - cfg.vwt_look, i)) / cfg.vwt_look
            trigger = b.h[i] if d > 0 else b.l[i]
            stop = b.vwap[i] - d * cfg.vwt_stop_atr * atr
            cost_r = self._cost_r(trigger, stop)
            checks = [("trend", share >= cfg.vwt_side,
                       f"{share:.0%} of last {cfg.vwt_look} closes {'above' if d > 0 else 'below'} VWAP, needs {cfg.vwt_side:.0%}"),
                      ("chop", crosses <= cfg.vwt_max_cross, f"{crosses} VWAP crosses in {cfg.vwt_cross_look} bars, max {cfg.vwt_max_cross}"),
                      ("relative strength", d * (rets[s] - mkt) > 0, f"{rets[s] * 100:+.2f}% vs market {mkt * 100:+.2f}%"),
                      ("regime", not ((regime == "UP" and d < 0) or (regime == "DOWN" and d > 0)), f"regime {regime}"),
                      ("pullback to VWAP", gap <= cfg.vwt_pull_atr * atr, f"{gap / atr if atr else 0:.2f} ATR from VWAP, needs <= {cfg.vwt_pull_atr}"),
                      ("held VWAP", d * (b.c[i] - b.vwap[i]) > 0, "closed through VWAP"),
                      ("lighter volume", b.v[i] < avg_v, f"volume {b.v[i] / avg_v if avg_v else 0:.2f}x the 12-bar average"),
                      ("costs", cost_r <= cfg.max_cost_r, f"costs {cost_r:.2f}R, max {cfg.max_cost_r:.2f}R")]
            if not all(ok for _, ok, _ in checks):
                if share >= cfg.vwt_side:                      # only stocks that are trending
                    self._note("VWT", now, s, d, checks)
                continue
            cands.append((d * (rets[s] - mkt), s, d, i, share, crosses, trigger, stop))
        cands.sort(key=lambda x: (-x[0], x[1]))
        events = []
        room = cfg.vwt_max_day - sum(1 for t in self.trades if t.setup == "VWT")
        take = max(0, min(cfg.vwt_top, room))
        for n, (rs, s, d, i, share, crosses, trigger, stop) in enumerate(cands):
            if n >= take:
                self._note("VWT", now, s, d, [("daily/bar cap", False,
                                               f"passed every rule but the cap ({cfg.vwt_top} per bar, {cfg.vwt_max_day} a day) was full")])
                continue
            b = bars[s]
            ev = self._arm("VWT", s, d, trigger, stop, now, now + cfg.vwt_valid_bars * FIVE,
                           [f"{share:.0%} of the last {cfg.vwt_look} 5-min closes "
                            f"{'above' if d > 0 else 'below'} VWAP, {crosses} cross(es) in the last {cfg.vwt_cross_look}",
                            f"{rets[s] * 100:+.2f}% since the open vs market {mkt * 100:+.2f}%; pulled back to VWAP "
                            f"Rs {b.vwap[i]:.2f} on lighter volume",
                            f"market regime {regime} ({above:.0%} of stocks above VWAP)"])
            if ev:
                events.append(ev)
        return events

    # ---------------------------------------------------------------- management
    def _manage(self, t: SetupTrade, b: DayBars | None, fine: list[Bar] | None, now: datetime) -> list[SetupEvent]:
        """Walk the new bars in time order: trigger, stop (first), VWAP-close exit, square-off."""
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
        last_close = None

        def vwap_exit(upto: datetime) -> bool:
            """VWT only: a completed 5-min bar after the entry that closed through VWAP, ending <= upto."""
            if t.setup != "VWT" or t.state != "OPEN" or b is None:
                return False
            entered = datetime.fromisoformat(t.entry_time)
            checked = datetime.fromisoformat(t.vwap_checked_to) if t.vwap_checked_to else entered
            for i in range(len(b.ts)):
                end = b.ts[i] + FIVE
                if end <= entered or end <= checked or end > upto or end > now:
                    continue
                t.vwap_checked_to = end.isoformat()
                if d * (b.c[i] - b.vwap[i]) < 0:
                    self._close(t, b.c[i], b.ts[i], "EXIT_VWAP", events)
                    return True
            return False

        for ts, o, h, l, c, _ in rows:
            if vwap_exit(ts):                       # a 5-min close through VWAP before this bar
                return events
            t.seen_to = (ts + width).isoformat()
            last_close = c
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
        if vwap_exit(now):
            return events
        if t.state == "OPEN" and now >= square + width:
            # no candle reached 15:15 (halt / feed gap): square off at the last known price
            price = last_close
            if price is None and b is not None and b.done(now):
                price = b.c[b.done(now) - 1]
            if price is None and fine:
                price = fine[-1][4]
            if price is not None:
                self._close(t, price, now - width, "SQUARE_OFF", events)
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

    def write(self, ev: SetupEvent, logged_at: datetime | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        row = {"kind": ev.kind, "at": ev.at, "trade": ev.trade}
        if logged_at is not None:
            row["logged_at"] = logged_at.isoformat()        # wall clock: when you were told
        with self.path(ev.at[:10]).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
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
        return "UNPROVEN: no backtest yet (python 945.py --setup-backtest once 11+ sessions are saved)"
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
                 day: date, clock=None):
        self.engine, self.base, self.stats, self.journal, self.day = engine, dict(base), stats, journal, day
        self.clock = clock or (lambda: datetime.now(IST))
        self.prev_close_fixed = False
        self.warned_rvol = False
        done = journal.trades(day.isoformat())
        if done:
            engine.restore(day, done)

    def _use_feed_previous_close(self, stocks: dict) -> None:
        """The feed's previous close is authoritative (the history may end a day early)."""
        from dataclasses import replace as _replace
        for sym, s in stocks.items():
            pc = getattr(s, "previous_close", None)
            if sym in self.base and pc:
                self.base[sym] = _replace(self.base[sym], prev_close=pc)
            elif sym not in self.base:
                self.base[sym] = _replace(EMPTY_BASE, prev_close=pc)      # no history: day-one proxies
        self.prev_close_fixed = True

    def rvol_warning(self) -> str | None:
        """Yahoo and PSYGRID may count opening volume differently; on a normal day the
        median stock's RVOL is near 1. Far from 1 means the two volume scales disagree."""
        m = self.engine.diag.get("orb_median_rvol")
        if m is None or self.warned_rvol:
            return None
        self.warned_rvol = True
        if 0.4 <= m <= 2.5:
            return None
        return (f"VOLUME CHECK: median stock RVOL at 09:20 is {m:.2f}x (normal ~1x). Yahoo history and the "
                f"live feed may count volume differently -- treat today's ORB list with care.")

    def muted(self, setup: str) -> bool:
        return (self.stats.get(setup) or {}).get("status") == "MUTED"

    def on_scan(self, stocks: dict, cutoff: datetime) -> list[SetupEvent]:
        """`stocks`: symbol -> 1-minute Series (selector_data.Series) of today."""
        if not self.prev_close_fixed:
            self._use_feed_previous_close(stocks)
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
        stamp = self.clock()
        for ev in events:
            self.journal.write(ev, stamp)
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
