"""1. Order_Flow_Imbalance_Engine -- Order-Flow Imbalance + Price Confirmation.

Mechanism (evidence): Cont, Kukanov & Stoikov (2014, J. Financial Econometrics) show that
over short intervals price changes are approximately linear in order-flow imbalance (OFI),
with slope inversely proportional to depth. Chordia, Roll & Subrahmanyam (2002, JFE) find
returns respond to contemporaneous and lagged order imbalance.

Data limitation (critical): true OFI needs best bid/ask queue updates. The PSYGRID feed has
only 1-minute OHLCV, so this engine uses the Bulk Volume Classification proxy of Easley,
Lopez de Prado & O'Hara (2016, JFE): buy share of bar i = Phi(dP_i / sigma_dP). BVC accuracy
depends on bar size and is disputed (Andersen & Bondarenko). Because BVC derives the sign
from price, it is NOT independent of price; the independent information is the volume that
weights it. The price confirmation therefore uses a different price feature (a break of
the range BEFORE the flow window) than the classifier (close-to-close changes).

Definitions (LONG; SHORT is the mirror image via features.Side):
  W = 10 bars (flow window i = t-9..t), sigma = sample s.d. of dP over the 30 bars before
  the window (causal; the window's own surge cannot dampen its classification).
  b_i  = Phi((C_i - C_{i-1}) / sigma)
  OI_W = sum V_i (2 b_i - 1) / sum V_i          in [-1, 1]
  OI_3 = the same over the last 3 bars
  VR   = sum_{window} V / (W * median V of the 30 bars before the window)
  REF  = max High of the 20 bars before the window
Mandatory gates: OI_W >= 0.30; OI_3 >= 0.20; VR >= 1.5; C_t > REF; C_t > VWAP_t;
  C_t > C_{t-10} (net price change agrees with flow); CLV_t >= 0; (C_t - VWAP_t)/ATR <= 3.
Trade: stop-entry above H_t; stop = min Low of last 5 bars - 0.10 ATR; target 2R.
"""
from __future__ import annotations

from datetime import time

from ..core import Component, Engine, Setup, lacking, no, ramp
from .. import indicators as ind


class OrderFlowImbalanceEngine(Engine):
    NAME = "Order_Flow_Imbalance_Engine"
    TITLE = "Order-Flow Imbalance + Price Confirmation"
    EARLIEST = time(9, 55)
    W, FAST, SIGMA_BARS, REF_BARS = 10, 3, 30, 20
    MIN_BARS = W + SIGMA_BARS + 1
    OI_MIN, OI_FAST_MIN, VR_MIN, MAX_EXT_ATR = 0.30, 0.20, 1.5, 3.0
    COOLDOWN_MINUTES = 30

    def side(self, stock, sv, ctx):
        t = sv.t
        atr, vw = sv.atr[t], sv.vwap[t]
        if atr is None or atr <= 0 or vw is None:
            return lacking("atr_or_vwap_undefined")
        w0 = t - self.W + 1
        hist = [sv.c[i] - sv.c[i - 1] for i in range(w0 - self.SIGMA_BARS, w0)]
        sigma = ind.stdev(hist)
        if not sigma or sigma <= 0:
            return no("no_price_variation_before_window")
        signed, vols = [], []
        for i in range(w0, t + 1):
            b = ind.bvc_buy_fraction(sv.c[i] - sv.c[i - 1], sigma)
            signed.append(sv.v[i] * (2.0 * b - 1.0))
            vols.append(sv.v[i])
        vol = sum(vols)
        if vol <= 0:
            return no("zero_volume_in_window")
        oi = sum(signed) / vol
        fv = sum(vols[-self.FAST:])
        oi3 = sum(signed[-self.FAST:]) / fv if fv > 0 else 0.0
        base = sv.med_volume(w0, self.SIGMA_BARS)
        if not base:
            return no("zero_baseline_volume")
        vr = vol / (self.W * base)
        ref = max(sv.h[w0 - self.REF_BARS:w0])
        c = sv.c[t]
        ext = (c - vw) / atr
        clv = ind.clv(sv.o[t], sv.h[t], sv.l[t], c)
        if oi < self.OI_MIN:
            return no(f"imbalance_{oi:+.2f}<{self.OI_MIN}")
        if oi3 < self.OI_FAST_MIN:
            return no(f"fading_flow_last3_{oi3:+.2f}")
        if vr < self.VR_MIN:
            return no(f"volume_ratio_{vr:.2f}<{self.VR_MIN}")
        if c <= ref:
            return no("no_price_confirmation_range_not_broken")
        if c <= vw:
            return no("wrong_side_of_vwap")
        if c <= sv.c[w0 - 1]:
            return no("price_diverges_from_flow")
        if clv < 0:
            return no(f"weak_close_clv_{clv:+.2f}")
        if ext > self.MAX_EXT_ATR:
            return no(f"extended_{ext:.2f}ATR_from_vwap")
        persistence = sum(1 for x in signed if x > 0) / self.W
        er = ind.efficiency_ratio(sv.c[w0 - 1:t + 1])
        clear = (c - ref) / atr
        comps = [
            Component("imbalance", 0.25, ramp(oi, 0.30, 0.60), oi, "OI10 = sum V(2b-1)/sum V, 0.30->0, 0.60->1"),
            Component("persistence", 0.15, ramp(persistence, 0.5, 0.8), persistence,
                      "share of window bars with flow in trade direction, 0.5->0, 0.8->1"),
            Component("participation", 0.20, ramp(vr, 1.5, 3.0), vr, "window volume / (10 x median), 1.5->0, 3->1"),
            Component("price_efficiency", 0.15, ramp(er, 0.3, 0.7), er, "Kaufman ER of window closes, 0.3->0, 0.7->1"),
            Component("breakout_clearance", 0.10, ramp(clear, 0.0, 0.5), clear, "(C - REF)/ATR, 0->0, 0.5->1"),
            Component("not_extended", 0.15, ramp(ext, 3.0, 1.0), ext, "(C - VWAP)/ATR, 3->0, <=1->1"),
        ]
        word = "buy" if sv.s == 1 else "sell"
        conds = (
            f"BVC {word} imbalance OI10={oi:+.2f} (>=0.30), last-3 OI={oi3:+.2f}",
            f"window volume {vr:.2f}x normal (>=1.5)",
            f"close {sv.real(c):.2f} broke the prior 20-bar {'high' if sv.s == 1 else 'low'} {sv.real(ref):.2f}",
            f"{'above' if sv.s == 1 else 'below'} VWAP {sv.real(vw):.2f} by {ext:.2f} ATR",
            "proxy: OHLCV bulk-volume classification, not quote-level OFI",
        )
        stop = min(sv.l[t - 4:t + 1]) - 0.10 * atr
        return Setup(sv.h[t], stop, tuple(comps), f"flow{sv.ts[t]:%H%M}", conds)
