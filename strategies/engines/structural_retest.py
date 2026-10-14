"""10. Structural_Break_Retest_Engine -- Break-and-Retest of a Predefined Structural Level.

Mechanism (evidence + hypothesis): Kavajecz & Odders-White (2004, RFS): support/resistance
levels coincide with depth peaks in the limit-order book. Osler (FRBNY SR 125): once such a
level is crossed, triggered stop orders make trends gain momentum. "Polarity" (old
resistance becoming support on a retest) is practitioner lore; Garzarelli et al. (2014)
report bounce memory at S/R levels. The net-of-cost edge of a break-and-retest entry on NSE
is a hypothesis to be tested.

Only PREDEFINED levels are used -- fixed before the break and never drawn after the fact:
PDH / PDL (previous session high/low from saved sessions), PDC (previous close), ORH / ORL
(09:15-09:29 range, fixed at 09:30). Distinct from the VWAP engine (a moving level) and the
Failed-Breakout engine (which trades the OPPOSITE outcome: a break that fails).

Definitions (LONG = break up through a level and retest from above; SHORT mirrored):
  Break bar b in [t-20, t-3]: the 10 closes before b (all after the level became known)
    are <= Lvl; C_b >= Lvl + 0.15 ATR; V_b >= 1.5 x median V of the 20 bars before b.
  Acceptance: no close below Lvl - 0.10 ATR in b+1..t (else it is a failed breakout).
  Departure: max High between b and the retest >= Lvl + 0.30 ATR.
  Retest r (latest) in b+2..t-1: L_r <= Lvl + 0.15 ATR and C_r >= Lvl.
  Trigger: C_t > H_r, the first such close since r; t - r <= 5.
Trade: stop-entry above H_t; stop = min Low_{r..t} - 0.10 ATR; target 2R. The nearest other
  predefined level beyond the entry is reported, and "room" to it is scored.
"""
from __future__ import annotations

from datetime import time

from ..core import Component, Engine, Setup, band, lacking, no, ramp
from ..levels import predefined_levels


class StructuralBreakRetestEngine(Engine):
    NAME = "Structural_Break_Retest_Engine"
    TITLE = "Break-and-Retest of a Predefined Structural Level"
    EARLIEST = time(9, 45)
    MIN_BARS = 31
    COOLDOWN_MINUTES = 30

    def side(self, stock, sv, ctx):
        t = sv.t
        atr = sv.atr[t]
        if atr is None or atr <= 0:
            return lacking("atr_undefined")
        sup, res = predefined_levels(stock, sv, or_significance=0.8, pd_significance=1.0, pdc_significance=0.85)
        levels = {}
        for lv in sup + res:
            levels.setdefault((lv.name, round(lv.price, 6)), lv)
        cands = []
        for lv in levels.values():
            for b in range(t - 3, max(t - 20, lv.since + 10) - 1, -1):
                if any(sv.c[j] > lv.price for j in range(b - 10, b)):
                    continue
                atr_b = sv.atr[b] or atr
                if sv.c[b] < lv.price + 0.15 * atr_b:
                    continue
                base = sv.med_volume(b, 20)
                bvr = sv.v[b] / base if base else 0.0
                if bvr < 1.5:
                    continue
                if any(sv.c[j] < lv.price - 0.10 * (sv.atr[j] or atr) for j in range(b + 1, t + 1)):
                    break                          # the break failed: not this engine's trade
                r = None
                for j in range(t - 1, b + 1, -1):
                    if sv.l[j] <= lv.price + 0.15 * (sv.atr[j] or atr) and sv.c[j] >= lv.price:
                        r = j
                        break
                if r is None or t - r > 5:
                    break
                if max(sv.h[b:r]) < lv.price + 0.30 * atr:
                    break                          # never departed: a stall, not a retest
                if sv.c[t] <= sv.h[r] or any(sv.c[j] > sv.h[r] for j in range(r + 1, t)):
                    break
                cands.append((lv.significance, b, lv, r, bvr))
                break
        if not cands:
            return no("no_break_accept_retest_trigger_sequence")
        _, b, lv, r, bvr = max(cands, key=lambda x: (x[0], x[1]))
        touches = sum(1 for j in range(lv.since, b) if abs(sv.h[j] - lv.price) <= 0.15 * (sv.atr[j] or atr))
        clear = (sv.c[b] - lv.price) / (sv.atr[b] or atr)
        prec = (sv.l[r] - lv.price) / (sv.atr[r] or atr)
        rdry = sv.v[r] / sv.v[b] if sv.v[b] > 0 else 1.0
        med = sv.med_volume(t, 20)
        tv = sv.v[t] / med if med else 0.0
        stop = min(sv.l[r:t + 1]) - 0.10 * atr
        risk = sv.h[t] - stop
        beyond = [x.price for x in levels.values() if x.price > sv.h[t]]
        nxt = min(beyond) if beyond else None
        room = (nxt - sv.h[t]) / risk if nxt is not None and risk > 0 else None
        comps = [
            Component("level_significance", 0.15, lv.significance, lv.significance, "PDH/PDL 1.0, PDC 0.85, opening range 0.8"),
            Component("prior_respect", 0.10, ramp(touches, 0, 3), touches, "bars testing the level before the break, 0->0, 3->1"),
            Component("break_volume", 0.15, ramp(bvr, 1.5, 3.0), bvr, "break V / median(20), 1.5->0, 3->1"),
            Component("break_clearance", 0.10, ramp(clear, 0.15, 0.6), clear, "(C_b - level)/ATR, 0.15->0, 0.6->1"),
            Component("retest_precision", 0.15, band(prec, -0.25, -0.05, 0.10, 0.16), prec, "(retest low - level)/ATR: full in [-0.05, 0.10]"),
            Component("retest_dryness", 0.10, ramp(rdry, 1.0, 0.4), rdry, "retest V / break V, 1->0, 0.4->1"),
            Component("trigger_volume", 0.10, ramp(tv, 0.8, 1.5), tv, "trigger V / median(20), 0.8->0, 1.5->1"),
            Component("room", 0.15, 1.0 if room is None else ramp(room, 1.0, 2.0), -1.0 if room is None else room,
                      "next predefined level beyond entry in R, 1->0, >=2 (or none)->1"),
        ]
        word = "broke above" if sv.s == 1 else "broke below"
        conds = (
            f"{word} {lv.name} {sv.real(lv.price):.2f} at {sv.ts[b]:%H:%M} on {bvr:.1f}x volume ({touches} prior tests)",
            f"retest {sv.ts[r]:%H:%M} held the level ({prec:+.2f} ATR) on {rdry:.2f}x the break volume",
            f"trigger: {sv.ts[t]:%H:%M} close {sv.real(sv.c[t]):.2f} beyond the retest bar",
            "next level: " + ("none" if nxt is None else f"{sv.real(nxt):.2f} ({room:.1f}R)"),
        )
        return Setup(sv.h[t], stop, tuple(comps), f"{lv.name}@{sv.real(lv.price):.2f}|b{sv.ts[b]:%H%M}", conds)
