"""5. Relative_Strength_Weakness_Engine -- Relative-Strength Leader / Relative-Weakness Laggard.

Mechanism (evidence + hypothesis): cross-sectional momentum is well documented at monthly
horizons (Jegadeesh & Titman 1993); intraday, Heston, Korajczyk & Sadka (2010, JF) find
cross-sectional return continuation at daily-periodic half-hour lags but REVERSAL at lags
under an hour (liquidity imbalances, bid-ask bounce). So intraday relative strength is NOT
an established continuation edge: this engine demands persistent, beta-adjusted leadership
over the whole session AND the last hour, plus a fresh breakout, and its edge is a
hypothesis to be tested. Beta is shrunk toward 1 following Blume (1975), whose estimate
is beta_next ~= 0.343 + 0.677 beta.

Definitions (computed in ``prepare`` for every valid stock, then ranked):
  market minute return m_k = median over valid stocks of C_k/C_{k-1} - 1 (context.py)
  beta_hat = cov(r_i, m)/var(m) over the session's consecutive minutes (n >= 30),
  beta = clamp(1/3 + 2/3 beta_hat, 0, 3)
  RS_open = (C_t / C_0915 - 1) - beta * M(09:15 close -> t)
  RS_60   = (C_t / C_{t-60} - 1) - beta * M(t-60 -> t)
  P_open, P_60 = cross-sectional percentiles in [0, 1] (>= 100 stocks ranked)
  consistency = number of the last six 10-bar blocks with positive excess return
Gates (LONG leader; SHORT laggard mirrored): P_open >= 0.95, P_60 >= 0.80, consistency
  >= 4/6, own return since 09:15 close > 0, C_t > VWAP_t, z = (C_t - VWAP_t)/sd_t <= 3.0
  (sd = volume-weighted s.d. of typical price: a leader's distance from VWAP is judged
  against the session's own dispersion, not 1-minute ATR. A perfectly linear trend with
  uniform volume has VWAP at the midpoint and sd = range/sqrt(12), so z = sqrt(3) ~ 1.73:
  full credit up to 1.75, zero at 3.0 = parabolic acceleration),
  fresh 15-bar breakout: C_t > max H_{t-15..t-1} and C_{t-1} <= max H_{t-16..t-2}.
Trade: stop-entry above H_t; stop = min Low of the last 15 bars - 0.10 ATR; target 2R.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from ..core import Component, Engine, Setup, lacking, no, ramp
from .. import indicators as ind


@dataclass(frozen=True)
class RSRecord:
    beta: float
    own: float          # C_t / C_0915close - 1
    rs_open: float
    rs_60: float
    blocks: tuple       # six excess returns, oldest first
    p_open: float = 0.0
    p_60: float = 0.0


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    """Rank percentile in [0, 1]; ties share the mean rank (deterministic)."""
    items = sorted(values.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(items)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        pct = ((i + j) / 2) / (n - 1) if n > 1 else 0.5
        for k in range(i, j + 1):
            out[items[k][0]] = pct
        i = j + 1
    return out


class RelativeStrengthWeaknessEngine(Engine):
    NAME = "Relative_Strength_Weakness_Engine"
    TITLE = "Relative-Strength Leader / Relative-Weakness Laggard"
    EARLIEST = time(10, 15)
    MIN_BARS = 61
    NEEDS_MARKET = True
    COOLDOWN_MINUTES = 60
    MIN_RANKED = 100
    P_OPEN_MIN, P_60_MIN, BLOCKS_MIN, MAX_Z = 0.95, 0.80, 4, 3.0

    def __init__(self):
        self._rs: dict[str, RSRecord] = {}

    @staticmethod
    def record(f, ctx) -> RSRecord | None:
        if f.n < 61:
            return None
        xs, ys = [], []
        for i in range(1, f.n):
            if (f.ts[i] - f.ts[i - 1]).total_seconds() == 60 and f.ts[i] in ctx.minute_returns:
                xs.append(ctx.minute_returns[f.ts[i]])
                ys.append(f.c[i] / f.c[i - 1] - 1.0)
        if len(xs) < 30:
            return None
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = sum((x - mx) ** 2 for x in xs)
        beta_hat = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var > 0 else 1.0
        beta = min(3.0, max(0.0, 1.0 / 3.0 + 2.0 / 3.0 * beta_hat))
        t = f.n - 1
        m_open = ctx.market_return_between(f.ts[0], f.ts[t])
        m_60 = ctx.market_return_between(f.ts[t - 60], f.ts[t])
        if m_open is None or m_60 is None:
            return None
        blocks = []
        for j in range(6):
            s, e = t - 60 + 10 * j, t - 50 + 10 * j
            m = ctx.market_return_between(f.ts[s], f.ts[e])
            if m is None:
                return None
            blocks.append((f.c[e] / f.c[s] - 1.0) - beta * m)
        own = f.c[t] / f.c[0] - 1.0
        return RSRecord(beta, own, own - beta * m_open, (f.c[t] / f.c[t - 60] - 1.0) - beta * m_60, tuple(blocks))

    def prepare(self, views, feats, ctx) -> None:
        self._rs = {}
        if not ctx.valid:
            return
        recs = {}
        for sym in sorted(views):
            r = self.record(feats[sym], ctx)
            if r is not None:
                recs[sym] = r
        if len(recs) < self.MIN_RANKED:
            return
        po = _percentiles({s: r.rs_open for s, r in recs.items()})
        p6 = _percentiles({s: r.rs_60 for s, r in recs.items()})
        self._rs = {s: RSRecord(r.beta, r.own, r.rs_open, r.rs_60, r.blocks, po[s], p6[s]) for s, r in recs.items()}

    def side(self, stock, sv, ctx):
        rec = self._rs.get(stock.symbol)
        if rec is None:
            return lacking("not_ranked_(too_few_ranked_stocks_or_missing_market_minutes)")
        t = sv.t
        atr, vw = sv.atr[t], sv.vwap[t]
        if atr is None or atr <= 0 or vw is None:
            return lacking("atr_or_vwap_undefined")
        s = sv.s
        p_open = rec.p_open if s == 1 else 1.0 - rec.p_open
        p_60 = rec.p_60 if s == 1 else 1.0 - rec.p_60
        blocks = sum(1 for b in rec.blocks if s * b > 0)
        own = s * rec.own
        c = sv.c[t]
        sd = sv.vwap_sd[t]
        if not sd or sd <= 0:
            return lacking("vwap_dispersion_undefined")
        z = (c - vw) / sd
        if p_open < self.P_OPEN_MIN:
            return no(f"session_rs_percentile_{p_open:.3f}")
        if p_60 < self.P_60_MIN:
            return no(f"hour_rs_percentile_{p_60:.3f}")
        if blocks < self.BLOCKS_MIN:
            return no(f"inconsistent_{blocks}/6_blocks")
        if own <= 0:
            return no("own_return_not_in_trade_direction")
        if c <= vw:
            return no("wrong_side_of_vwap")
        if z > self.MAX_Z:
            return no(f"stretched_{z:.2f}_vwap_sd")
        ref = max(sv.h[t - 15:t])
        if c <= ref or sv.c[t - 1] > max(sv.h[t - 16:t - 1]):
            return no("no_fresh_15_bar_breakout")
        recent = sum(sv.v[t - 29:t + 1]) / 30
        med = ind.safe_median(sv.v[:t + 1])
        part = recent / med if med else 0.0
        clv = ind.clv(sv.o[t], sv.h[t], sv.l[t], c)
        comps = [
            Component("session_rs_rank", 0.20, ramp(p_open, 0.95, 0.99), p_open, "beta-adj RS since open percentile, 0.95->0, 0.99->1"),
            Component("hour_rs_rank", 0.15, ramp(p_60, 0.80, 0.95), p_60, "beta-adj RS last 60 min percentile, 0.80->0, 0.95->1"),
            Component("consistency", 0.15, ramp(blocks, 3, 6), blocks, "10-min blocks with excess return in direction: 3->0, 6->1"),
            Component("absolute_move", 0.15, ramp(own, 0.0, 0.01), own, "own return since 09:15 close, 0->0, 1%->1"),
            Component("not_stretched", 0.10, ramp(z, 3.0, 1.75), z, "(C - VWAP)/VWAP s.d., 3->0, <=1.75->1"),
            Component("participation", 0.15, ramp(part, 1.0, 2.0), part, "mean V last 30 / session median V, 1->0, 2->1"),
            Component("breakout_close", 0.10, ramp(clv, 0.2, 0.7), clv, "breakout bar close location, 0.2->0, 0.7->1"),
        ]
        word = "leader" if s == 1 else "laggard"
        conds = (
            f"relative-{'strength' if s == 1 else 'weakness'} {word}: session RS pct {p_open:.1%}, last-hour pct {p_60:.1%}",
            f"beta {rec.beta:.2f}; excess return since open {s * rec.rs_open:+.2%} in direction; {blocks}/6 blocks agree",
            f"fresh 15-bar {'high' if s == 1 else 'low'}: close {sv.real(c):.2f} vs {sv.real(ref):.2f}",
        )
        stop = min(sv.l[t - 14:t + 1]) - 0.10 * atr
        return Setup(sv.h[t], stop, tuple(comps), f"rs{sv.ts[t]:%H%M}", conds)
