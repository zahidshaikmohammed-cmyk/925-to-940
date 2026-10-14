"""8. VWAP_Reclaim_Rejection_Engine -- VWAP Reclaim / Rejection + Retest.

Mechanism (evidence + hypothesis): VWAP is the dominant institutional execution benchmark
(Berkowitz, Logue & Noser 1988, JF); benchmarked execution algorithms trade around it, so it
is a widely watched intraday reference. Zarattini & Aziz (2023, SSRN 4631351) report a
long-above / short-below VWAP rule on QQQ; independent replications disagree on its size.
The claim that a reclaim of VWAP followed by a successful retest predicts continuation is a
practitioner hypothesis that must be tested on NSE data.

LONG = reclaim (price was below VWAP, crosses above, retests from above and holds).
SHORT = rejection (the mirror image via features.Side: price above, loses VWAP, retests
from below and is rejected).

Definitions (LONG):
  Cross bar c in [t-12, t-2]: C_c > VWAP_c and C_{c-1} <= VWAP_{c-1};
    prior regime: >= 10 of the 15 closes before c below VWAP;
    body ratio_c >= 0.5; V_c >= 1.2 x median V of the 20 bars before c.
  Hold: no close below VWAP - 0.10 ATR in c+1..t.
  Retest bar r (latest) in c+1..t-1: L_r <= VWAP_r + 0.15 ATR and C_r >= VWAP_r.
  Trigger: C_t > H_r, the first close above H_r since r, and t - r <= 5.
  VWAP turn = (VWAP_t - VWAP_c)/ATR (scored, not gated): while price is below VWAP the
  average necessarily falls, so the slope is measured only since the cross.
Trade: stop-entry above H_t; stop = min Low_{r..t} - 0.10 ATR; target 2R.
"""
from __future__ import annotations

from datetime import time

from ..core import Component, Engine, Setup, band, lacking, no, ramp
from .. import indicators as ind


class VWAPReclaimRejectionEngine(Engine):
    NAME = "VWAP_Reclaim_Rejection_Engine"
    TITLE = "VWAP Reclaim / Rejection + Retest"
    EARLIEST = time(9, 45)
    MIN_BARS = 31
    COOLDOWN_MINUTES = 30

    def side(self, stock, sv, ctx):
        t = sv.t
        atr = sv.atr[t]
        vw = sv.vwap
        if atr is None or atr <= 0 or vw[t] is None:
            return lacking("atr_or_vwap_undefined")
        found = None
        for c in range(t - 2, max(t - 12, 16) - 1, -1):
            if vw[c] is None or vw[c - 1] is None:
                continue
            if not (sv.c[c] > vw[c] and sv.c[c - 1] <= vw[c - 1]):
                continue
            below = sum(1 for j in range(c - 15, c) if vw[j] is not None and sv.c[j] < vw[j])
            if below < 10:
                continue
            found = (c, below)
            break                                  # the most recent qualifying cross
        if found is None:
            return no("no_vwap_cross_after_established_regime")
        c, below = found
        base = sv.med_volume(c, 20)
        cvr = sv.v[c] / base if base else 0.0
        body = ind.body_ratio(sv.o[c], sv.h[c], sv.l[c], sv.c[c])
        if body < 0.5 or cvr < 1.2:
            return no("weak_cross_bar")
        for j in range(c + 1, t + 1):
            a = sv.atr[j] or atr
            if sv.c[j] < vw[j] - 0.10 * a:
                return no("lost_vwap_after_cross")
        r = None
        for j in range(t - 1, c, -1):
            a = sv.atr[j] or atr
            if sv.l[j] <= vw[j] + 0.15 * a and sv.c[j] >= vw[j]:
                r = j
                break
        if r is None:
            return no("no_retest")
        if t - r > 5:
            return no("retest_too_old")
        if sv.c[t] <= sv.h[r] or any(sv.c[j] > sv.h[r] for j in range(r + 1, t)):
            return no("no_first_close_above_retest_high")
        atr_r = sv.atr[r] or atr
        prec = (sv.l[r] - vw[r]) / atr_r
        rdry = sv.v[r] / sv.v[c] if sv.v[c] > 0 else 1.0
        med = sv.med_volume(t, 20)
        tv = sv.v[t] / med if med else 0.0
        slope = (vw[t] - vw[c]) / atr
        dist = (sv.h[t] - vw[t]) / atr
        comps = [
            Component("prior_regime", 0.10, ramp(below, 10, 15), below, "closes on the other side in 15 before cross, 10->0, 15->1"),
            Component("cross_volume", 0.15, ramp(cvr, 1.2, 2.5), cvr, "cross V / median(20), 1.2->0, 2.5->1"),
            Component("cross_body", 0.10, ramp(body, 0.5, 0.8), body, "cross body/range, 0.5->0, 0.8->1"),
            Component("retest_precision", 0.15, band(prec, -0.35, -0.10, 0.05, 0.16), prec,
                      "(retest extreme - VWAP)/ATR: full in [-0.10, 0.05]"),
            Component("retest_dryness", 0.15, ramp(rdry, 1.0, 0.5), rdry, "retest V / cross V, 1->0, 0.5->1"),
            Component("trigger_volume", 0.10, ramp(tv, 0.8, 1.5), tv, "trigger V / median(20), 0.8->0, 1.5->1"),
            Component("vwap_turn", 0.10, ramp(slope, -0.15, 0.0), slope,
                      "(VWAP_t - VWAP_cross)/ATR in direction: -0.15->0, flat or turned->1"),
            Component("entry_near_vwap", 0.15, ramp(dist, 1.5, 0.5), dist, "(entry - VWAP)/ATR, 1.5->0, <=0.5->1"),
        ]
        word = "reclaimed" if sv.s == 1 else "lost"
        conds = (
            f"VWAP {word} at {sv.ts[c]:%H:%M} after {below}/15 closes on the other side; cross volume {cvr:.1f}x",
            f"retest {sv.ts[r]:%H:%M} touched VWAP {sv.real(vw[r]):.2f} and held ({prec:+.2f} ATR)",
            f"trigger: {sv.ts[t]:%H:%M} close {sv.real(sv.c[t]):.2f} beyond the retest bar extreme {sv.real(sv.h[r]):.2f}",
        )
        stop = min(sv.l[r:t + 1]) - 0.10 * atr
        return Setup(sv.h[t], stop, tuple(comps), f"x{sv.ts[c]:%H%M}", conds)
