"""3. Failed_Breakout_Breakdown_Engine -- Failed Breakout / Failed Breakdown.

Mechanism (evidence + hypothesis): Osler (FRBNY SR 125) documents that trends tend to
reverse at predictable support/resistance levels and to accelerate once they are crossed;
Garzarelli et al. (2014, Scientific Reports) find prices bounce on identified S/R levels
more often than a memoryless process would. A breakout that is ACCEPTED (several closes
beyond the level) and then closes back inside traps the breakout buyers, whose exits add
to the reversal. The predictive value of that trap on NSE 1-minute data is a hypothesis.

Distinct from the Sweep engine: here >= 2 consecutive closes beyond the level by >= 0.05
ATR are REQUIRED (acceptance), and the failure is a close back inside by >= 0.10 ATR.

Definitions (written for the LONG frame = failed BREAKDOWN below support; the SHORT signal,
a failed breakout above resistance, is its mirror via features.Side):
  Levels: PDL (prev. session low), ORL (09:15-09:29 low), DL (session low before the
  breakdown, only when the breakdown starts at or after bar 30).
  Breakdown start b in [t-12, t-2]: C_{b-1} >= Lvl, and k >= 2 consecutive closes
  C_b..C_{b+k-1} < Lvl - 0.05 ATR.
  Extension E = (Lvl - min Low_{b..t}) / ATR <= 1.5 (further = a real breakdown).
  Failure at t: C_t > Lvl + 0.10 ATR and t is the FIRST such close since b; t - b <= 12.
  BVR = mean V over the k acceptance bars / median V of 20 bars before b;
  FVR = V_t / that same median.
Trade: stop-entry above H_t; stop = min Low_{b..t} - 0.10 ATR; target 2R.
"""
from __future__ import annotations

from datetime import time

from ..core import Component, Engine, Setup, lacking, no, ramp
from ..levels import Level, predefined_levels


class FailedBreakoutBreakdownEngine(Engine):
    NAME = "Failed_Breakout_Breakdown_Engine"
    TITLE = "Failed Breakout / Failed Breakdown"
    EARLIEST = time(9, 33)
    MIN_BARS = 19
    MAX_AGE, ACCEPT_ATR, FAIL_ATR, MAX_EXT_ATR = 12, 0.05, 0.10, 1.5
    COOLDOWN_MINUTES = 30
    STYLE = "fade"               # counter-trend by design: fade-specific safety gates

    def side(self, stock, sv, ctx):
        t = sv.t
        atr = sv.atr[t]
        if atr is None or atr <= 0:
            return lacking("atr_undefined")
        sup, _ = predefined_levels(stock, sv, or_significance=0.9, pd_significance=1.0)
        best = None
        for b in range(max(t - self.MAX_AGE, 1), t - 1):
            levels = list(sup)
            if b >= 30:
                # known from bars 0..b-1, so usable at b (since=0; the old since=b made it dead code)
                levels.append(Level("DL" if sv.s == 1 else "DH", min(sv.l[:b]), 0, 0.8))
            for lv in levels:
                if b <= lv.since or sv.c[b - 1] < lv.price:
                    continue
                line = lv.price - self.ACCEPT_ATR * atr
                k = 0
                while b + k < t and sv.c[b + k] < line:
                    k += 1
                if k < 2:
                    continue
                low = min(sv.l[b:t + 1])
                ext = (lv.price - low) / atr
                if ext > self.MAX_EXT_ATR:
                    continue
                fail_line = lv.price + self.FAIL_ATR * atr
                if sv.c[t] <= fail_line or any(sv.c[j] > fail_line for j in range(b, t)):
                    continue
                base = sv.med_volume(b, 20)
                if not base:
                    continue
                bvr = sum(sv.v[b:b + k]) / k / base
                fvr = sv.v[t] / base
                cand = (lv.significance, -b, lv, k, ext, bvr, fvr, low)
                if best is None or cand[:2] > best[:2]:
                    best = cand
        if best is None:
            return no("no_accepted_break_followed_by_first_failure_close")
        _, nb, lv, k, ext, bvr, fvr, low = best
        b = -nb
        depth = (sv.c[t] - lv.price) / atr
        comps = [
            Component("acceptance", 0.15, ramp(k, 1, 3), k, "closes accepted beyond the level: 2->0.5, >=3->1"),
            Component("small_extension", 0.15, ramp(ext, 1.5, 0.3), ext, "max excursion beyond level /ATR, 1.5->0, <=0.3->1"),
            Component("failure_depth", 0.20, ramp(depth, 0.10, 0.50), depth, "close back inside /ATR, 0.1->0, 0.5->1"),
            Component("weak_break_volume", 0.10, ramp(bvr, 2.0, 0.8), bvr, "breakout-bar volume ratio, 2.0->0, <=0.8->1"),
            Component("failure_volume", 0.15, ramp(fvr, 0.8, 2.0), fvr, "failure-bar volume ratio, 0.8->0, 2.0->1"),
            Component("speed", 0.10, ramp(t - b, 12, 3), t - b, "bars from break to failure, 12->0, <=3->1"),
            Component("level_significance", 0.15, lv.significance, lv.significance, "PDH/PDL 1.0, opening range 0.9, day extreme 0.8"),
        ]
        word = "breakdown below" if sv.s == 1 else "breakout above"
        conds = (
            f"{word} {lv.name} {sv.real(lv.price):.2f} at {sv.ts[b]:%H:%M}, accepted for {k} closes",
            f"max excursion only {ext:.2f} ATR (<=1.5), break volume {bvr:.1f}x",
            f"failed: {sv.ts[t]:%H:%M} close {sv.real(sv.c[t]):.2f} back inside by {depth:.2f} ATR on {fvr:.1f}x volume",
        )
        stop = low - 0.10 * atr
        return Setup(sv.h[t], stop, tuple(comps), f"{lv.name}@{sv.real(lv.price):.2f}|b{sv.ts[b]:%H%M}", conds)
