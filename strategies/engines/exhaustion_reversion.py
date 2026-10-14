"""9. Exhaustion_Mean_Reversion_Engine -- Exhaustion Reversal + Mean-Reversion Confirmation.

Mechanism (evidence): Campbell, Grossman & Wang (1993, QJE): price moves accompanied by high
volume tend to reverse (liquidity providers are paid for absorbing non-informational
pressure). Grant, Wolf & Yu (2005, J. Banking & Finance): significant intraday reversals
after large opening moves in US index futures, though much of the significance disappears
after bid-ask transaction costs. Heston, Korajczyk & Sadka (2010): reversal at lags under
an hour from temporary liquidity imbalances. Evidence is mostly index/daily level, so a
stock-level 1-minute exhaustion edge net of NSE costs is a hypothesis to be tested.

The engine never fades a move on stretch alone: it requires climax volume, an exhaustion
shape, AND a confirmation close back through the climax bar, and its target is the
mathematically defined mean (session VWAP), which must lie >= 1.5R away.

Definitions (LONG frame = fade a DOWNSIDE exhaustion; SHORT = fade an upside exhaustion):
  Climax bar x in [t-5, t-1] with L_x = min Low_{x-30..t} (no lower low after x).
  Stretch z = (VWAP_x - L_x) / sd_x >= 2.5  (sd = volume-weighted s.d. of typical price).
  Run = (max High_{x-15..x} - L_x) / ATR_x >= 3.0.
  Climax volume CV = max(V_{x-1}, V_x) / median V of the 30 bars before x-1 >= 3.0.
  Exhaustion shape: lower-wick ratio of x >= 0.35 OR RSI14(x) <= 20.
  Confirmation: C_t > H_x, the first such close since x.
Trade: stop-entry beyond H_t; stop = L_x - 0.10 ATR; target = VWAP_t (>= 1.5R required).
"""
from __future__ import annotations

from datetime import time

from ..core import Component, Engine, Setup, lacking, no, ramp
from .. import indicators as ind


class ExhaustionMeanReversionEngine(Engine):
    NAME = "Exhaustion_Mean_Reversion_Engine"
    TITLE = "Exhaustion Reversal + Mean-Reversion Confirmation"
    EARLIEST = time(9, 50)
    MIN_BARS = 37
    Z_MIN, RUN_MIN, CV_MIN, WICK_MIN, RSI_MAX = 2.5, 3.0, 3.0, 0.35, 20.0
    COOLDOWN_MINUTES = 30
    STYLE = "fade"               # counter-trend by design: fade-specific safety gates

    def side(self, stock, sv, ctx):
        t = sv.t
        atr, vwt = sv.atr[t], sv.vwap[t]
        if atr is None or atr <= 0 or vwt is None:
            return lacking("atr_or_vwap_undefined")
        x = None
        for k in range(t - 1, t - 6, -1):
            if k - 31 < 0:
                break
            if sv.l[k] == min(sv.l[k - 30:t + 1]):
                x = k
                break
        if x is None:
            return no("no_recent_climax_extreme")
        vwx, sdx, atrx = sv.vwap[x], sv.vwap_sd[x], sv.atr[x] or atr
        if vwx is None or not sdx or sdx <= 0:
            return no("undefined_vwap_dispersion")
        z = (vwx - sv.l[x]) / sdx
        run = (max(sv.h[x - 15:x + 1]) - sv.l[x]) / atrx
        base = sv.med_volume(x - 1, 30)
        cv = max(sv.v[x - 1], sv.v[x]) / base if base else 0.0
        wick = ind.lower_wick_ratio(sv.o[x], sv.h[x], sv.l[x], sv.c[x])
        rsi = ind.rsi_wilder(sv.c[:x + 1], 14)
        if z < self.Z_MIN:
            return no(f"stretch_z_{z:.2f}<{self.Z_MIN}")
        if run < self.RUN_MIN:
            return no(f"run_{run:.2f}ATR<{self.RUN_MIN}")
        if cv < self.CV_MIN:
            return no(f"no_climax_volume_{cv:.2f}x")
        if wick < self.WICK_MIN and (rsi is None or rsi > self.RSI_MAX):
            return no("no_exhaustion_shape")
        if sv.c[t] <= sv.h[x] or any(sv.c[j] > sv.h[x] for j in range(x + 1, t)):
            return no("no_first_confirmation_close_through_climax_bar")
        stop = sv.l[x] - 0.10 * atr
        risk = sv.h[t] - stop
        rr = (vwt - sv.h[t]) / risk if risk > 0 else 0.0
        if rr < self.risk.min_reward_r:
            return no(f"vwap_target_only_{rr:.2f}R")
        depth = (sv.c[t] - sv.h[x]) / atr
        rsi_v = rsi if rsi is not None else 50.0
        comps = [
            Component("stretch", 0.20, ramp(z, 2.5, 3.5), z, "(VWAP - extreme)/VWAP s.d., 2.5->0, 3.5->1"),
            Component("climax_volume", 0.20, ramp(cv, 3.0, 6.0), cv, "climax V / median(30), 3->0, 6->1"),
            Component("run", 0.10, ramp(run, 3.0, 6.0), run, "move into the extreme /ATR, 3->0, 6->1"),
            Component("rejection_wick", 0.10, ramp(wick, 0.2, 0.6), wick, "climax-bar rejection wick/range, 0.2->0, 0.6->1"),
            Component("rsi_extreme", 0.10, ramp(rsi_v, 30.0, 15.0), rsi_v, "RSI14 at the extreme (direction-normalised), 30->0, 15->1"),
            Component("confirmation", 0.15, ramp(depth, 0.0, 0.4), depth, "(C_t - climax bar far edge)/ATR, 0->0, 0.4->1"),
            Component("reward_to_mean", 0.15, ramp(rr, 1.5, 3.0), rr, "(VWAP - entry)/risk, 1.5->0, 3->1"),
        ]
        word = "selling" if sv.s == 1 else "buying"
        conds = (
            f"{word} climax {sv.ts[x]:%H:%M}: extreme {sv.real(sv.l[x]):.2f} is {z:.1f} VWAP s.d. from VWAP, "
            f"{run:.1f} ATR run, {cv:.1f}x volume",
            f"exhaustion shape: wick {wick:.0%}, RSI14 {rsi_v if sv.s == 1 else 100.0 - rsi_v:.0f}",
            f"confirmed: {sv.ts[t]:%H:%M} close {sv.real(sv.c[t]):.2f} back through the climax bar",
            f"target = session VWAP {sv.real(vwt):.2f} ({rr:.1f}R)",
        )
        return Setup(sv.h[t], stop, tuple(comps), f"clx{sv.ts[x]:%H%M}", conds, target=vwt)
