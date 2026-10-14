"""6. Volatility_Compression_Expansion_Engine -- Volatility Compression -> Confirmed Expansion.

Mechanism (evidence + hypothesis): volatility clusters and mean-reverts (Engle 1982;
Bollerslev 1986), so an unusually quiet stretch is expected to be followed by a return
toward normal volatility -- that predicts MAGNITUDE, not direction. Direction is taken only
from a confirmed expansion bar. Range volatility uses Parkinson (1980): roughly 5x more
efficient than close-to-close under zero drift. Intraday volatility is U-shaped, so a
quiet midday window is partly seasonal; the baseline excludes the first 15 minutes and
this limitation is documented. Directional follow-through after the expansion is a
practitioner hypothesis (narrow-range breakouts), not an established finding.

Distinct from flat.py (which needs a flat DAY vs the previous close) and from the ORB
engine (a fixed clock range): here the box is any 15-bar window that is quiet RELATIVE
TO THE STOCK'S OWN SESSION, confirmed by a second bar.

Definitions (LONG; SHORT mirrored). e = t-1 is the expansion bar, t the confirmation bar.
  Box = bars e-15..e-1 (must start at or after 09:30), hi/lo/W = hi - lo, mid = (hi+lo)/2.
  sigma_P(window) = sqrt(mean ln(H/L)^2 / (4 ln 2)).
  Baseline = median sigma_P over every 15-bar window starting at index >= 15 (09:30) and
  ending before the box (>= 15 windows required).  CR = sigma_P(box) / baseline <= 0.60.
  Expansion e: C_e >= hi + 0.10 W; TR_e >= 2.0 x mean TR(box); V_e >= 2.0 x median V(box);
  body ratio >= 0.55; CLV_e >= 0.3.
  Confirmation t: C_t > hi and L_t >= hi - 0.25 W.
  No chase: TR_e <= 8 x mean box TR (a shock candle is news, not compression) and
  (trigger - hi) <= 0.6 x (trigger - mid) (the risk must not be mostly the chase).
Trade: stop-entry above max(H_e, H_t); stop = mid (back to the middle = failed expansion);
  target 2R.
"""
from __future__ import annotations

import math
from datetime import time
from statistics import median

from ..core import Component, Engine, Setup, lacking, no, ramp
from .. import indicators as ind


class VolatilityCompressionExpansionEngine(Engine):
    NAME = "Volatility_Compression_Expansion_Engine"
    TITLE = "Volatility Compression -> Confirmed Expansion"
    EARLIEST = time(10, 15)
    MIN_BARS = 61
    BOX, CR_MAX = 15, 0.60
    MAX_RANGE_RATIO = 8.0        # a shock candle (news) is not a compression breakout
    MAX_CHASE = 0.6              # entry may sit at most 60% of the risk beyond the box edge
    COOLDOWN_MINUTES = 30

    def __init__(self):
        self._cache = (None, ())

    def _prefix(self, f):
        """Prefix sums of ln(H/L)^2 so every window's Parkinson sigma is O(1); cached for
        the two directions of the same Features object."""
        if self._cache[0] is not f:
            ps = [0.0]
            for hi, lo in zip(f.h, f.l):
                r = math.log(hi / lo)
                ps.append(ps[-1] + r * r)
            self._cache = (f, tuple(ps))
        return self._cache[1]

    def _sigma(self, f, a: int, b: int) -> float:
        """Parkinson sigma of bars [a, b): sqrt(sum ln(H/L)^2 / (n 4 ln2))."""
        ps = self._prefix(f)
        return math.sqrt(max(0.0, ps[b] - ps[a]) / ((b - a) * ind.LN2x4))

    def side(self, stock, sv, ctx):
        t = sv.t
        e = t - 1
        b0, b1 = e - self.BOX, e            # box = [b0, b1)
        if b0 < 15:
            return lacking("box_would_include_opening_range")
        f = sv.base                         # Parkinson needs real (positive) prices
        sig_box = self._sigma(f, b0, b1)
        windows = [self._sigma(f, i, i + self.BOX) for i in range(15, b0 - self.BOX + 1)]
        if len(windows) < 15:
            return lacking(f"only_{len(windows)}_baseline_windows")
        base = median(windows)
        if base <= 0:
            return no("undefined_volatility")
        cr = sig_box / base
        if cr > self.CR_MAX:
            return no(f"no_compression_ratio_{cr:.2f}")
        hi, lo = max(sv.h[b0:b1]), min(sv.l[b0:b1])
        w = hi - lo
        if w <= 0:
            return no("zero_width_box")
        mid = (hi + lo) / 2.0
        mean_tr = sum(sv.tr[b0:b1]) / self.BOX
        med_v = median(sv.v[b0:b1])
        if mean_tr <= 0 or med_v <= 0:
            return no("dead_box")
        ce = sv.c[e]
        clear = (ce - hi) / w
        trr = sv.tr[e] / mean_tr
        vr = sv.v[e] / med_v
        body = ind.body_ratio(sv.o[e], sv.h[e], sv.l[e], ce)
        clv = ind.clv(sv.o[e], sv.h[e], sv.l[e], ce)
        if clear < 0.10:
            return no("no_expansion_close_beyond_box")
        if trr < 2.0:
            return no(f"expansion_range_{trr:.2f}x")
        if vr < 2.0:
            return no(f"expansion_volume_{vr:.2f}x")
        if body < 0.55 or clv < 0.3:
            return no("expansion_bar_not_decisive")
        if trr > self.MAX_RANGE_RATIO:
            return no(f"shock_candle_range_{trr:.1f}x")
        if sv.c[t] <= hi or sv.l[t] < hi - 0.25 * w:
            return no("expansion_not_confirmed")
        atr, vw = sv.atr[t], sv.vwap[t]
        if atr is None or atr <= 0 or vw is None:
            return lacking("atr_or_vwap_undefined")
        hold = (sv.c[t] - hi) / w
        vside = (sv.c[t] - vw) / atr
        comps = [
            Component("compression", 0.20, ramp(cr, 0.60, 0.35), cr, "Parkinson box/baseline, 0.60->0, <=0.35->1"),
            Component("range_expansion", 0.15, ramp(trr, 2.0, 4.0), trr, "TR_e / mean box TR, 2->0, 4->1"),
            Component("volume_expansion", 0.20, ramp(vr, 2.0, 5.0), vr, "V_e / median box V, 2->0, 5->1"),
            Component("body", 0.10, ramp(body, 0.55, 0.85), body, "expansion body/range, 0.55->0, 0.85->1"),
            Component("clearance", 0.10, ramp(clear, 0.10, 0.50), clear, "(C_e - box edge)/W, 0.1->0, 0.5->1"),
            Component("confirmation", 0.15, ramp(hold, 0.0, 0.5), hold, "(C_t - box edge)/W, 0->0, 0.5->1"),
            Component("vwap_side", 0.10, ramp(vside, -0.5, 0.5), vside, "(C_t - VWAP)/ATR in direction, -0.5->0, 0.5->1"),
        ]
        conds = (
            f"15-bar box {min(sv.real(lo), sv.real(hi)):.2f}-{max(sv.real(lo), sv.real(hi)):.2f} ({sv.ts[b0]:%H:%M}-{sv.ts[b1 - 1]:%H:%M}), "
            f"Parkinson vol {cr:.2f}x the session baseline (<=0.60)",
            f"expansion {sv.ts[e]:%H:%M}: range {trr:.1f}x, volume {vr:.1f}x, body {body:.0%}",
            f"confirmed by the {sv.ts[t]:%H:%M} close {sv.real(sv.c[t]):.2f} holding outside",
        )
        trigger = max(sv.h[e], sv.h[t])
        chase = (trigger - hi) / (trigger - mid) if trigger > mid else 1.0
        if chase > self.MAX_CHASE:
            return no(f"chasing_{chase:.0%}_of_risk_beyond_box")
        return Setup(trigger, mid, tuple(comps), f"box{sv.ts[b0]:%H%M}-{sv.ts[e]:%H%M}", conds)
