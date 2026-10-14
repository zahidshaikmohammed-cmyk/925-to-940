"""Pure, deterministic indicator mathematics on completed 1-minute bars.

Conventions (all functions):
* Inputs are sequences ordered oldest -> newest; index i only ever reads indices <= i
  (functions taking an ``end`` index read nothing after it).
* Prices are rupees, volumes are shares, returns are fractions unless a name says ``pct``.
* An undefined value (too little data, zero volume, zero range) is returned as ``None`` or as
  the documented neutral value -- never silently invented.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Sequence

LN2x4 = 4.0 * math.log(2.0)


def true_ranges(h: Sequence[float], l: Sequence[float], c: Sequence[float]) -> tuple[float, ...]:
    """TR_0 = H_0 - L_0 (the overnight gap is deliberately excluded from the first bar);
    TR_i = max(H_i - L_i, |H_i - C_{i-1}|, |L_i - C_{i-1}|)."""
    out = []
    for i in range(len(c)):
        if i == 0:
            out.append(h[0] - l[0])
        else:
            pc = c[i - 1]
            out.append(max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc)))
    return tuple(out)


def rolling_mean(x: Sequence[float], n: int) -> tuple[float | None, ...]:
    """Simple trailing mean; None until n values exist. ATR here = rolling_mean(TR, 14)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    out: list[float | None] = []
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        out.append(s / n if i >= n - 1 else None)
    return tuple(out)


def vwap_series(h, l, c, v) -> tuple[tuple[float | None, ...], tuple[float | None, ...]]:
    """Session VWAP_t = sum(TP_i V_i)/sum(V_i), TP = (H+L+C)/3, i = 0..t, and its
    volume-weighted standard deviation sd_t = sqrt(sum V_i TP_i^2 / sum V_i - VWAP_t^2).
    Both are None while cumulative volume is zero."""
    vw, sd = [], []
    sv = spv = sp2v = 0.0
    for i in range(len(c)):
        tp = (h[i] + l[i] + c[i]) / 3.0
        sv += v[i]
        spv += tp * v[i]
        sp2v += tp * tp * v[i]
        if sv <= 0:
            vw.append(None)
            sd.append(None)
            continue
        m = spv / sv
        var = sp2v / sv - m * m
        vw.append(m)
        # catastrophic cancellation can make a true zero slightly negative
        sd.append(math.sqrt(var) if var > 1e-12 * m * m else 0.0)
    return tuple(vw), tuple(sd)


def ema(x: Sequence[float], n: int) -> tuple[float | None, ...]:
    """EMA with alpha = 2/(n+1), seeded with the SMA of the first n values; None before."""
    if n < 1:
        raise ValueError("n must be >= 1")
    a = 2.0 / (n + 1.0)
    out: list[float | None] = []
    e = None
    for i, val in enumerate(x):
        if i < n - 1:
            out.append(None)
            continue
        if i == n - 1:
            e = sum(x[:n]) / n
        else:
            e = a * val + (1 - a) * e
        out.append(e)
    return tuple(out)


def parkinson_sigma(h: Sequence[float], l: Sequence[float]) -> float | None:
    """Parkinson (1980) range volatility per bar: sqrt(mean(ln(H/L)^2) / (4 ln 2))."""
    if not h or len(h) != len(l):
        return None
    s = 0.0
    for hi, lo in zip(h, l):
        if hi <= 0 or lo <= 0 or hi < lo:
            return None
        r = math.log(hi / lo)
        s += r * r
    return math.sqrt(s / (len(h) * LN2x4))


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bvc_buy_fraction(dp: float, sigma: float) -> float:
    """Bulk Volume Classification (Easley, Lopez de Prado & O'Hara): share of a bar's volume
    attributed to buyers = Phi(dP / sigma_dP). sigma <= 0 -> 0.5 for dP == 0, else 0 or 1."""
    if sigma > 0:
        return normal_cdf(dp / sigma)
    return 0.5 if dp == 0 else (1.0 if dp > 0 else 0.0)


def stdev(x: Sequence[float]) -> float | None:
    """Sample standard deviation (n-1); None for fewer than 2 values."""
    n = len(x)
    if n < 2:
        return None
    m = sum(x) / n
    return math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))


def efficiency_ratio(closes: Sequence[float]) -> float:
    """Kaufman efficiency: |C_n - C_0| / sum|C_i - C_{i-1}|; 0 when the path length is 0."""
    if len(closes) < 2:
        return 0.0
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return abs(closes[-1] - closes[0]) / path if path > 0 else 0.0


def rsi_wilder(closes: Sequence[float], n: int = 14) -> float | None:
    """Wilder RSI of the last close; needs n+1 closes. 100 if no losses, 50 if flat."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 50.0 if ag == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def ols_slope_t(y: Sequence[float]) -> tuple[float, float]:
    """OLS of y on x = 0..n-1: (slope, t-statistic of the slope). Needs n >= 3.
    A perfect fit returns t = +/-1e9 (sign of slope); a zero slope with zero error gives 0."""
    n = len(y)
    if n < 3:
        raise ValueError("need at least 3 points")
    mx = (n - 1) / 2.0
    my = sum(y) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    sxy = sum((i - mx) * (y[i] - my) for i in range(n))
    b = sxy / sxx
    a = my - b * mx
    sse = sum((y[i] - (a + b * i)) ** 2 for i in range(n))
    if sse <= 1e-18 * max(1.0, sum(v * v for v in y)):
        return b, (0.0 if b == 0 else math.copysign(1e9, b))
    se = math.sqrt(sse / (n - 2) / sxx)
    return b, b / se


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n != len(y) or n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def clv(o: float, h: float, l: float, c: float) -> float:
    """Close location value in [-1, 1]: +1 close at the high, -1 at the low, 0 for zero range."""
    rng = h - l
    return ((c - l) - (h - c)) / rng if rng > 0 else 0.0


def body_ratio(o: float, h: float, l: float, c: float) -> float:
    rng = h - l
    return abs(c - o) / rng if rng > 0 else 0.0


def upper_wick_ratio(o, h, l, c) -> float:
    rng = h - l
    return (h - max(o, c)) / rng if rng > 0 else 0.0


def lower_wick_ratio(o, h, l, c) -> float:
    rng = h - l
    return (min(o, c) - l) / rng if rng > 0 else 0.0


def safe_median(x: Sequence[float]) -> float | None:
    return median(x) if len(x) else None


def swing_points(h: Sequence[float], l: Sequence[float], k: int, last: int) -> tuple[list, list]:
    """Confirmed fractal pivots using bars 0..last only.

    Swing high at i: H_i > max(H_{i-k..i-1}) and H_i >= max(H_{i+1..i+k}); likewise lows.
    A pivot at i is only known once bar i+k has closed, so i <= last - k (no look-ahead).
    Returns ([(i, price), ...] highs, [(i, price), ...] lows).
    """
    highs, lows = [], []
    for i in range(k, last - k + 1):
        left_h, right_h = h[i - k:i], h[i + 1:i + k + 1]
        if h[i] > max(left_h) and h[i] >= max(right_h):
            highs.append((i, h[i]))
        left_l, right_l = l[i - k:i], l[i + 1:i + k + 1]
        if l[i] < min(left_l) and l[i] <= min(right_l):
            lows.append((i, l[i]))
    return highs, lows
