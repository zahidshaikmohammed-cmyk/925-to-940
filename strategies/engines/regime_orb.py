"""7. Regime_Filtered_ORB_Engine -- Regime-Filtered Opening-Range Breakout.

Mechanism (evidence): Holmberg, Lonnbark & Lundstrom (2013, Finance Research Letters) test
ORB on crude-oil futures and report significant returns; Zarattini, Barbon & Aziz (2024,
SSRN / Swiss Finance Institute) report that a 5-minute ORB is strongly profitable only on
"Stocks in Play" -- stocks with abnormal opening volume (relative volume vs. the previous
14 days), trading in the direction of the opening candle, stop at a fraction of ATR, exit
at the close. These are US results (and partly the authors' own claims); no peer-reviewed
NSE replication was found, so on NSE this is a hypothesis to be tested.

Regime filters added here (hypotheses): the market (universe median since the open) and
breadth (share of stocks above VWAP) must agree with the breakout direction.

Definitions (LONG; SHORT mirrored):
  OR = the 15 bars 09:15..09:29 (all must exist): ORH, ORL, OR_open = O_0915,
  OR_close = C_0929, OR_vol = sum V.
  Stocks in play: RVOL = OR_vol / median(09:15-09:29 volume of >= 5 prior sessions)
    >= 1.5. Without that history the proxy is the cross-sectional percentile of OR
    turnover (>= 0.80); the proxy can earn at most HALF of the in-play component.
  Direction: OR_close > OR_open (opening drive agrees).
  Trigger: t is the FIRST bar after 09:29 with C_t > ORH, and no close below ORL before t.
  Regime: market return since open >= 0 and breadth above VWAP >= 0.50 (LONG).
  Window: decision bars 09:30-09:59.
Trade: stop-entry above H_t; stop = ORL (the literature's opposite-side stop); target 2R;
  time exit 30 minutes after entry (system-wide horizon; Zarattini et al. hold to the close,
  so this is a deliberate departure from the literature).
"""
from __future__ import annotations

from datetime import time

from ..core import Component, Engine, Setup, lacking, no, ramp
from ..levels import OR_BARS, opening_range_ok
from .relative_strength import _percentiles


class RegimeFilteredORBEngine(Engine):
    NAME = "Regime_Filtered_ORB_Engine"
    TITLE = "Regime-Filtered Opening-Range Breakout"
    EARLIEST = time(9, 30)
    LATEST = time(9, 59)                  # the earliness score caps anything later below 90
    MIN_BARS = OR_BARS + 1
    NEEDS_MARKET = True
    COOLDOWN_MINUTES = 24 * 60            # one ORB per stock and side per day
    RVOL_MIN, PROXY_PCTL_MIN, MIN_HISTORY_DAYS = 1.5, 0.80, 5

    def __init__(self):
        self._turnover_pct: dict[str, float] = {}

    def prepare(self, views, feats, ctx) -> None:
        turn = {}
        for sym in sorted(views):
            f = feats[sym]
            if f.n > OR_BARS and f.ts[OR_BARS - 1].time() == time(9, 29):
                turn[sym] = sum(f.c[i] * f.v[i] for i in range(OR_BARS))
        self._turnover_pct = _percentiles(turn) if len(turn) >= 100 else {}

    def side(self, stock, sv, ctx):
        if not opening_range_ok(sv):
            return lacking("opening_range_incomplete")
        t = sv.t
        atr = sv.atr[t]
        if atr is None or atr <= 0:
            return lacking("atr_undefined")
        orh, orl = max(sv.h[:OR_BARS]), min(sv.l[:OR_BARS])
        or_open, or_close = sv.o[0], sv.c[OR_BARS - 1]
        or_vol = sum(sv.v[:OR_BARS])
        if any(sv.c[j] < orl for j in range(OR_BARS, t)):
            return no("opposite_side_broke_first")
        if sv.c[t] <= orh or any(sv.c[j] > orh for j in range(OR_BARS, t)):
            return no("not_the_first_close_beyond_or")
        if or_close <= or_open:
            return no("opening_drive_disagrees")
        p = stock.prior
        if p is not None and p.or_volume_base and p.or_days >= self.MIN_HISTORY_DAYS:
            rvol = or_vol / p.or_volume_base
            if rvol < self.RVOL_MIN:
                return no(f"not_in_play_rvol_{rvol:.2f}")
            in_play = ramp(rvol, 1.5, 3.0)
            if p.or_source != "psygrid":
                # Yahoo 5-minute volume is not on the live PSYGRID scale (unverified ratio): treat
                # it like the turnover proxy -- at most half credit, so it cannot carry a 90+
                in_play *= 0.5
            in_play_note = f"RVOL {rvol:.2f}x the {p.or_days}-session opening-volume median ({p.or_source or 'history'})"
            raw = rvol
        else:
            pct = self._turnover_pct.get(stock.symbol)
            if pct is None:
                return lacking("no_rvol_history_and_no_turnover_cross_section")
            if pct < self.PROXY_PCTL_MIN:
                return no(f"not_in_play_turnover_pct_{pct:.2f}")
            in_play = 0.5 * ramp(pct, 0.80, 0.98)
            in_play_note = f"PROXY (no history): opening turnover percentile {pct:.0%}"
            raw = pct
        mkt = sv.s * ctx.ret_open
        breadth = sv.s * (ctx.breadth_above_vwap - 0.5)
        if mkt < 0:
            return no(f"market_against_{ctx.ret_open:+.4f}")
        if breadth < 0:
            return no(f"breadth_against_{ctx.breadth_above_vwap:.2f}")
        rng = orh - orl
        body = abs(or_close - or_open) / rng if rng > 0 else 0.0
        mean_or_v = or_vol / OR_BARS
        bv = sv.v[t] / mean_or_v if mean_or_v > 0 else 0.0
        clear = (sv.c[t] - orh) / atr
        minutes = (t - OR_BARS)
        comps = [
            Component("in_play", 0.25, in_play, raw, "RVOL 1.5->0, 3->1 (turnover-percentile proxy capped at 0.5)"),
            Component("market_regime", 0.15, ramp(mkt, 0.0, 0.005), mkt, "market return since open in direction, 0->0, 0.5%->1"),
            Component("breadth", 0.10, ramp(breadth, 0.0, 0.15), breadth, "share above VWAP minus 0.5 in direction, 0->0, 0.15->1"),
            Component("opening_drive", 0.10, ramp(body, 0.2, 0.6), body, "|OR close - OR open| / OR range, 0.2->0, 0.6->1"),
            Component("breakout_volume", 0.15, ramp(bv, 0.5, 1.5), bv, "breakout V / mean OR-bar V, 0.5->0, 1.5->1"),
            Component("clearance", 0.10, ramp(clear, 0.0, 0.3), clear, "(C - OR edge)/ATR, 0->0, 0.3->1"),
            Component("earliness", 0.15, ramp(minutes, 60, 0), minutes, "minutes after 09:30, 60->0, 0->1"),
        ]
        conds = (
            f"opening range 09:15-09:29 {min(sv.real(orl), sv.real(orh)):.2f}-"
            f"{max(sv.real(orl), sv.real(orh)):.2f}; first close beyond it at {sv.ts[t]:%H:%M}",
            f"stock in play: {in_play_note}",
            f"opening drive agrees; market since open {ctx.ret_open:+.2%}, breadth above VWAP {ctx.breadth_above_vwap:.0%}",
            "time exit 30 min after entry if neither stop nor target",
        )
        return Setup(sv.h[t], orl, tuple(comps), f"ORB{sv.ts[t]:%Y%m%d}", conds)
