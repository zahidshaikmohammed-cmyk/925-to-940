"""4. Trend_Pullback_Continuation_Engine -- Trend Pullback + Continuation.

Mechanism (evidence + hypothesis): intraday return continuation is documented at the market
level (Gao, Han, Li & Zhou 2018, JFE: first half-hour predicts last half-hour; Baltussen et
al. 2021, JFE across 60+ futures) -- evidence of intraday trend persistence, though at the
index level and for specific windows. Short-horizon reversals driven by temporary liquidity
imbalances (Heston, Korajczyk & Sadka 2010, JF) are what a pullback is assumed to be: a
low-volume, partial retracement inside an established trend. That a stock-level pullback
on lighter volume resolves in the trend direction is a practitioner hypothesis to be tested.

Definitions (LONG; SHORT mirrored):
  Trend gates: EMA20_t > EMA50_t on 1-minute closes; EMA50_t > EMA50_{t-10};
    OLS slope t-statistic of the last 12 complete 5-minute closes >= 2.5;
    share of the last 60 bars closing above VWAP >= 0.80.
  Swing high SH = max High of the last 30 bars at index p (latest), p <= t-3.
  Impulse origin A = min Low of the 30 bars before p.  Pullback low PL = min Low_{p+1..t}.
  Depth d = (SH - PL)/(SH - A) in [0.236, 0.618]; PL >= VWAP(at PL) - 0.25 ATR.
  Pullback volume ratio = mean V_{p+1..t-1} / mean V_{a..p} <= 0.90.
  Continuation trigger: C_t > max(H_{t-3..t-1}) while C_{t-1} <= max(H_{t-4..t-2}) (fresh),
    PL made within the last 6 bars, CLV_t >= 0.3.
  Market not opposing: market return since open (universe median) >= -0.30% (LONG).
Trade: stop-entry above H_t; stop = PL - 0.10 ATR; target 2R.
"""
from __future__ import annotations

from datetime import time

from ..core import Component, Engine, Setup, band, lacking, no, ramp
from .. import indicators as ind
from ..features import five_minute_bars


class TrendPullbackContinuationEngine(Engine):
    NAME = "Trend_Pullback_Continuation_Engine"
    TITLE = "Trend Pullback + Continuation"
    EARLIEST = time(10, 15)
    MIN_BARS = 61
    NEEDS_MARKET = True
    COOLDOWN_MINUTES = 30
    T_MIN, VWAP_HOLD, DEPTH_LO, DEPTH_HI, PB_VOL_MAX = 2.5, 0.80, 0.236, 0.618, 0.90
    MARKET_FLOOR = -0.003

    def side(self, stock, sv, ctx):
        t = sv.t
        atr = sv.atr[t]
        if atr is None or atr <= 0 or sv.vwap[t] is None:
            return lacking("atr_or_vwap_undefined")
        e20, e50 = ind.ema(sv.c, 20), ind.ema(sv.c, 50)
        if e50[t] is None or e50[t - 10] is None:
            return lacking("ema50_undefined")
        if not (e20[t] > e50[t] and e50[t] > e50[t - 10]):
            return no("no_ema_uptrend")
        five = five_minute_bars(stock.bars)
        if len(five) < 12:
            return lacking(f"only_{len(five)}_complete_5m_bars")
        closes5 = [sv.s * b.close for b in five[-12:]]
        _, tstat = ind.ols_slope_t(closes5)
        if tstat < self.T_MIN:
            return no(f"trend_t_{tstat:.2f}<{self.T_MIN}")
        hold = sum(1 for i in range(t - 59, t + 1) if sv.vwap[i] is not None and sv.c[i] > sv.vwap[i]) / 60
        if hold < self.VWAP_HOLD:
            return no(f"vwap_hold_{hold:.2f}")
        win = range(t - 29, t + 1)
        sh = max(sv.h[i] for i in win)
        p = max(i for i in win if sv.h[i] == sh)
        if p > t - 3:
            return no("no_pullback_yet")
        a_lo = max(0, p - 30)
        a_val = min(sv.l[a_lo:p])
        a = max(i for i in range(a_lo, p) if sv.l[i] == a_val)
        leg = sh - a_val
        if leg <= 0:
            return no("no_impulse_leg")
        pl = min(sv.l[p + 1:t + 1])
        pl_i = max(i for i in range(p + 1, t + 1) if sv.l[i] == pl)
        depth = (sh - pl) / leg
        if not self.DEPTH_LO <= depth <= self.DEPTH_HI:
            return no(f"pullback_depth_{depth:.2f}")
        if sv.vwap[pl_i] is None or pl < sv.vwap[pl_i] - 0.25 * atr:
            return no("pullback_lost_vwap")
        if pl_i < t - 5:                     # pullback low among the last 6 bars (t-5..t)
            return no("pullback_low_too_old")
        imp_vol = sum(sv.v[a:p + 1]) / (p + 1 - a)
        pb_bars = sv.v[p + 1:t]
        if not pb_bars or imp_vol <= 0:
            return no("pullback_too_short")
        pbv = (sum(pb_bars) / len(pb_bars)) / imp_vol
        if pbv > self.PB_VOL_MAX:
            return no(f"pullback_volume_{pbv:.2f}")
        trig_ref = max(sv.h[t - 3:t])
        if sv.c[t] <= trig_ref or sv.c[t - 1] > max(sv.h[t - 4:t - 1]):
            return no("no_fresh_continuation_close")
        clv = ind.clv(sv.o[t], sv.h[t], sv.l[t], sv.c[t])
        if clv < 0.3:
            return no(f"weak_trigger_close_{clv:+.2f}")
        mkt = sv.s * ctx.ret_open
        if mkt < self.MARKET_FLOOR:
            return no(f"market_against_{ctx.ret_open:+.4f}")
        med = sv.med_volume(t, 20)
        tv = sv.v[t] / med if med else 0.0
        er = ind.efficiency_ratio(sv.c[t - 59:t + 1])
        comps = [
            Component("trend_t", 0.20, ramp(tstat, 2.5, 6.0), tstat, "5-min OLS slope t, 2.5->0, 6->1"),
            Component("vwap_hold", 0.10, ramp(hold, 0.80, 0.95), hold, "share of 60 closes beyond VWAP, 0.80->0, 0.95->1"),
            Component("depth", 0.15, band(depth, 0.20, 0.382, 0.50, 0.65), depth, "retracement: full 0.382-0.50"),
            Component("pullback_dryness", 0.15, ramp(pbv, 0.90, 0.50), pbv, "pullback/impulse mean volume, 0.9->0, 0.5->1"),
            Component("trigger_volume", 0.10, ramp(tv, 0.8, 1.5), tv, "trigger V / median(20), 0.8->0, 1.5->1"),
            Component("trigger_close", 0.10, ramp(clv, 0.3, 0.8), clv, "trigger close location, 0.3->0, 0.8->1"),
            Component("efficiency", 0.10, ramp(er, 0.15, 0.40), er, "60-bar Kaufman ER, 0.15->0, 0.40->1"),
            Component("market_alignment", 0.10, ramp(mkt, -0.003, 0.003), mkt, "market return since open in trade direction"),
        ]
        word = "up" if sv.s == 1 else "down"
        conds = (
            f"{word}trend: EMA20>EMA50 (rising), 5-min slope t={tstat:.1f}, {hold:.0%} of 60 closes beyond VWAP",
            f"pullback {depth:.0%} of the {sv.ts[a]:%H:%M}-{sv.ts[p]:%H:%M} leg on {pbv:.2f}x the impulse volume",
            f"continuation close {sv.real(sv.c[t]):.2f} beyond the 3-bar {'high' if sv.s == 1 else 'low'} {sv.real(trig_ref):.2f}",
            f"market since open {ctx.ret_open:+.2%}",
        )
        return Setup(sv.h[t], pl - 0.10 * atr, tuple(comps), f"SH{sv.ts[p]:%H%M}", conds)
