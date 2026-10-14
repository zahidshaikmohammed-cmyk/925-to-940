"""2. Liquidity_Sweep_Reclaim_Engine -- Liquidity Sweep + Reclaim.

Mechanism (evidence): Osler (2003, 2005; FRBNY Staff Report 125) finds stop-loss orders
cluster just beyond salient levels; when triggered they become market orders that produce
fast "price cascades". Kavajecz & Odders-White (2004, RFS) show support/resistance levels
coincide with peaks of limit-order-book depth. A sweep is a brief run through such a level
that triggers resting stops; if the level's opposing depth absorbs that flow, price closes
back inside within a bar or few ("reclaim"). The hypothesis that the reclaim predicts a move
away from the level is NOT established by these papers -- it requires out-of-sample testing.

Distinct from the Failed-Breakout engine: a sweep allows AT MOST ONE close beyond the level
(no acceptance) and the reclaim must come within 3 bars; a failed breakout requires >= 2
accepted closes beyond the level first.

Definitions (LONG = sweep of a support pool; SHORT mirrored):
  Support pools (known before the sweep): PDL (previous session low), ORL (09:15-09:29
  low, known from 09:30), confirmed swing lows (fractal k = 3, confirmed >= 10 bars
  before the sweep, not traded through since).
  Sweep bar s in [t-3, t]: L_s < Lvl, penetration p = (Lvl - L_s)/ATR in [0.05, 1.0];
  level intact before s (min Low between its formation and s-1 >= Lvl);
  no lower low after s; at most ONE close more than 0.05 ATR below Lvl in s..t-1;
  reclaim: t is the FIRST bar since s with C > Lvl;
  sweep volume VR_s = V_s / median V of the 20 bars before s >= 1.5.
Trade: stop-entry above H_t; stop = L_s - 0.10 ATR; target 2R.
"""
from __future__ import annotations

from datetime import time

from ..core import Component, Engine, Setup, band, lacking, no, ramp
from .. import indicators as ind
from ..levels import Level, predefined_levels


class LiquiditySweepReclaimEngine(Engine):
    NAME = "Liquidity_Sweep_Reclaim_Engine"
    TITLE = "Liquidity Sweep + Reclaim"
    EARLIEST = time(9, 31)
    MIN_BARS = 17
    PIVOT_K, PIVOT_MIN_AGE, MAX_SWEEP_AGE = 3, 10, 3
    P_MIN, P_MAX, VR_MIN = 0.05, 1.0, 1.5
    COOLDOWN_MINUTES = 15
    STYLE = "fade"               # counter-trend by design: fade-specific safety gates

    def _pools(self, stock, sv, t: int, atr: float) -> list[tuple[Level, int]]:
        """(level, first sweep index allowed). Swing pivots are computed once on bars < t;
        a pivot at i is confirmed at i+k and must be at least PIVOT_MIN_AGE bars old."""
        sup, _ = predefined_levels(stock, sv, or_significance=0.9, pd_significance=1.0)
        pools = [(lv, lv.since + 1) for lv in sup]
        _, lows = ind.swing_points(sv.h, sv.l, self.PIVOT_K, t - 1)
        for i, price in lows:
            touches = sum(1 for j in range(0, i) if abs(sv.l[j] - price) <= 0.10 * atr)
            lv = Level("SWING", price, i + 1, 0.85 if touches >= 1 else 0.6)
            pools.append((lv, i + self.PIVOT_K + self.PIVOT_MIN_AGE))
        return pools

    def side(self, stock, sv, ctx):
        t = sv.t
        atr = sv.atr[t]
        if atr is None or atr <= 0:
            return lacking("atr_undefined")
        best = None
        pools = self._pools(stock, sv, t, atr)
        for s in range(t, max(t - self.MAX_SWEEP_AGE, 1) - 1, -1):
            for lv, first_ok in pools:
                if s < first_ok:
                    continue
                if min(sv.l[lv.since:s]) < lv.price:
                    continue                      # already traded through: not an intact pool
                if sv.l[s] >= lv.price:
                    continue
                atr_s = sv.atr[s] or atr
                pen = (lv.price - sv.l[s]) / atr_s
                if not self.P_MIN <= pen <= self.P_MAX:
                    continue
                if s < t and min(sv.l[s + 1:t + 1]) <= sv.l[s]:
                    continue                      # a lower low after the sweep
                accepted = sum(1 for j in range(s, t) if sv.c[j] < lv.price - 0.05 * atr_s)
                if accepted > 1:
                    continue                      # acceptance beyond: failed-breakout territory
                if sv.c[t] <= lv.price or any(sv.c[j] > lv.price for j in range(s, t)):
                    continue                      # t must be the first reclaim close
                base = sv.med_volume(s, 20)
                if not base:
                    continue
                vr = sv.v[s] / base
                if vr < self.VR_MIN:
                    continue
                cand = (lv.significance, pen, s, lv, vr)
                if best is None or cand[:2] > best[:2]:
                    best = cand
        if best is None:
            return no("no_sweep_and_first_reclaim_of_an_intact_level")
        _, pen, s, lv, vr = best
        c = sv.c[t]
        wick = ind.lower_wick_ratio(sv.o[s], sv.h[s], sv.l[s], sv.c[s])
        speed = 1.0 - 0.2 * (t - s)
        strength = (c - lv.price) / atr
        clv = ind.clv(sv.o[t], sv.h[t], sv.l[t], c)
        comps = [
            Component("level_significance", 0.15, lv.significance, lv.significance,
                      "PDL/PDH 1.0, opening range 0.9, retested swing 0.85, swing 0.6"),
            Component("penetration", 0.15, band(pen, 0.0, 0.10, 0.50, 1.0), pen,
                      "(level - sweep low)/ATR: full in [0.10, 0.50], 0 at 0 and 1.0"),
            Component("sweep_volume", 0.20, ramp(vr, 1.5, 3.5), vr, "sweep bar V / median(20), 1.5->0, 3.5->1"),
            Component("rejection_wick", 0.15, ramp(wick, 0.3, 0.6), wick, "sweep-bar rejection wick / range, 0.3->0, 0.6->1"),
            Component("reclaim_speed", 0.10, speed, t - s, "bars from sweep to reclaim: 0->1.0, 3->0.4"),
            Component("reclaim_strength", 0.15, ramp(strength, 0.0, 0.4), strength, "(C - level)/ATR, 0->0, 0.4->1"),
            Component("reclaim_close", 0.10, ramp(clv, 0.0, 0.6), clv, "reclaim bar close location, 0->0, 0.6->1"),
        ]
        conds = (
            f"swept {lv.name} {sv.real(lv.price):.2f} by {pen:.2f} ATR at {sv.ts[s]:%H:%M} on {vr:.1f}x volume",
            f"reclaimed on the {sv.ts[t]:%H:%M} close {sv.real(c):.2f} ({t - s} bar(s) after the sweep)",
            f"no follow-through: no new {'low' if sv.s == 1 else 'high'} after the sweep, <=1 accepted close beyond",
        )
        stop = sv.l[s] - 0.10 * atr
        return Setup(sv.h[t], stop, tuple(comps), f"{lv.name}@{sv.real(lv.price):.2f}|sw{sv.ts[s]:%H%M}", conds)
