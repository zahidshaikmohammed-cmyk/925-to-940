"""The six pre-registered strategy families (rules fixed BEFORE any result was seen).

Every rule below reads only bars 0..k of the current day (k = the bar whose close forms the
signal) plus reference values computed from earlier days. Common definitions:

    ATR(k)   median high-low of the up-to-12 present bars before k (needs >= 3), same day only
    MKT(k)   median over the day's eligible universe of close(k)/open(09:15) - 1
    window   signal bars k = 3 .. 65 (entry by 14:45); one trade per symbol per variant per day
    risk     |entry - stop| must be 0.10% .. 3% of the entry price (else no trade)

Family / variants (15 in total):

1 SWEEP     Liquidity-sweep rejection. Level = highest high (lowest low) of the L bars before k.
            Bar k trades >= 0.1 ATR beyond the level and CLOSES back inside -> fade it.
            Stop: bar k's extreme +- 0.1 ATR. Target 1.5R. Hold 6 bars (30 min).  L in {6, 12}.
2 IMPULSE   Impulse-pullback continuation. Impulse: 3 bars in one direction (each closes in it)
            moving >= 3 ATR, ending at bar j in k-4..k-2. Pullback bars j+1..k-1 retrace a
            fraction f of the impulse. Bar k confirms: closes beyond bar k-1's extreme in the
            impulse direction. Stop: the pullback extreme. Target 1.5R. Hold 6.
            f in {[0.382, 0.618], [0.25, 0.50]}.
3 SQUEEZE   Compression -> expansion. Box = bars k-6..k-1; width <= q x the median width of the
            same 6 slots over the previous 10 days. Bar k closes >= 0.1 ATR outside the box with a
            body >= 60% of its range -> trade the break. Stop: box midpoint (a return to the
            middle = false breakout). Target 2R. Hold 6.  q in {0.5, 0.7}.
4 XSRS      Cross-sectional relative strength. At k = 5, 11, ..., 59 (every 30 min) rank the
            universe by 30-min return minus the market's 30-min return. Trade the 5 strongest
            long and 5 weakest short (CONT) or the opposite (REV). Stop 1.5% (catastrophe only),
            no target, time exit.  CONT hold 3, CONT hold 6, REV hold 6.
5 VOLSURP   Volume surprise. Surprise = volume(k) / median volume of slot k over the previous 10
            days (>= 5 values). Range(k) >= 0.5 ATR.
            CONT: body >= 60% of range and close in the outer 20% -> with the candle;
                  stop the candle's other extreme.
            REV : wick >= 60% of range against the close's side (rejection) -> against the wick's
                  push; stop beyond the wick extreme + 0.05%.
            Target 1.5R, hold 6.  surprise >= {3, 5} x {CONT, REV}.
6 FAILBO    Failed-breakout trap. Range = the first N bars. A bar after the range closes beyond
            it (first breakout); within the next 3 bars a bar k closes back inside -> trade back
            in. Stop: the extreme since the breakout + 0.05 ATR. Target 1.5R. Hold 6.  N in {3, 6}.
"""
from __future__ import annotations

from statistics import median

from .data import SLOTS, DayBars

K_MIN, K_MAX = 3, 65


# --------------------------------------------------------------------------- helpers

def atr(day: DayBars, k: int, n: int = 12):
    rng = [day.h[s] - day.l[s] for s in range(max(0, k - n), k) if day.c[s] is not None]
    return median(rng) if len(rng) >= 3 else None


def full(day: DayBars, a: int, b: int) -> bool:
    """All candles a..b (inclusive) present."""
    return a >= 0 and all(day.c[s] is not None for s in range(a, b + 1))


# --------------------------------------------------------------------------- 1 SWEEP

def sweep(day: DayBars, k: int, L: int):
    if k < L or not full(day, k - L, k):
        return None
    a = atr(day, k)
    if not a:
        return None
    hi, lo = max(day.h[k - L:k]), min(day.l[k - L:k])
    if day.h[k] >= hi + 0.1 * a and day.c[k] < hi:
        return -1, day.h[k] + 0.1 * a
    if day.l[k] <= lo - 0.1 * a and day.c[k] > lo:
        return 1, day.l[k] - 0.1 * a
    return None


# --------------------------------------------------------------------------- 2 IMPULSE

def impulse(day: DayBars, k: int, band):
    if k < 4 or not full(day, max(0, k - 7), k):
        return None
    a = atr(day, k - 3) or atr(day, k)
    if not a:
        return None
    for j in range(k - 2, k - 5, -1):            # most recent impulse first
        if j - 2 < 0:
            continue
        legs = [day.c[s] - day.o[s] for s in range(j - 2, j + 1)]
        move = day.c[j] - day.o[j - 2]
        if abs(move) < 3 * a:
            continue
        d = 1 if move > 0 else -1
        if any(d * x <= 0 for x in legs):
            continue
        pull = range(j + 1, k)
        if not len(pull):
            continue
        ext = min(day.l[s] for s in pull) if d > 0 else max(day.h[s] for s in pull)
        retr = d * (day.c[j] - ext) / abs(move)
        if not band[0] <= retr <= band[1]:
            continue
        prev_ext = day.h[k - 1] if d > 0 else day.l[k - 1]
        if d * (day.c[k] - prev_ext) > 0 and d * (day.c[k] - day.o[k]) > 0:
            stop = min(ext, day.l[k]) if d > 0 else max(ext, day.h[k])
            return d, stop
        return None
    return None


# --------------------------------------------------------------------------- 3 SQUEEZE

def squeeze(day: DayBars, k: int, q: float, base_r6):
    if k < 6 or not full(day, k - 6, k) or base_r6 is None or base_r6[k - 1] is None:
        return None
    a = atr(day, k)
    if not a:
        return None
    hi, lo = max(day.h[k - 6:k]), min(day.l[k - 6:k])
    if hi - lo > q * base_r6[k - 1]:
        return None
    rng = day.h[k] - day.l[k]
    body = abs(day.c[k] - day.o[k])
    if rng <= 0 or body < 0.6 * rng:
        return None
    mid = (hi + lo) / 2
    if day.c[k] >= hi + 0.1 * a and day.c[k] > day.o[k]:
        return 1, mid
    if day.c[k] <= lo - 0.1 * a and day.c[k] < day.o[k]:
        return -1, mid
    return None


# --------------------------------------------------------------------------- 4 XSRS (cross-sectional)

XS_SLOTS = list(range(5, 60, 6))                 # 09:40, 10:10, ... 14:10 bar closes


def xsrs(bars: dict, universe, k: int, top: int = 5, sign: int = 1):
    """[(symbol, side, stop)] at bar k. Uses only bars k-6..k of every symbol."""
    rel = []
    for s in universe:
        d = bars.get(s)
        # 30 minutes back; at 09:45 (k=5) that is the 09:15 open. Never a negative index:
        # Python would silently wrap c[-1] to the day's LAST candle (future data).
        ref = d.o[0] if d is not None and k - 6 < 0 else (d.c[k - 6] if d is not None else None)
        if d is None or d.c[k] is None or ref is None:
            continue
        rel.append((d.c[k] / ref - 1, s))
    if len(rel) < 4 * top:
        return []
    mkt = median(r for r, _ in rel)
    rel = sorted(((r - mkt, s) for r, s in rel), key=lambda x: (x[0], x[1]))
    out = []
    for r, s in rel[-top:]:
        side = sign
        out.append((s, side, bars[s].c[k] * (1 - side * 0.015)))
    for r, s in rel[:top]:
        side = -sign
        out.append((s, side, bars[s].c[k] * (1 - side * 0.015)))
    return out


# --------------------------------------------------------------------------- 5 VOLSURP

def volsurp(day: DayBars, k: int, threshold: float, mode: str, base_v):
    if day.c[k] is None or base_v is None or not base_v[k] or day.v[k] is None:
        return None
    if day.v[k] / base_v[k] < threshold:
        return None
    a = atr(day, k)
    rng = day.h[k] - day.l[k]
    if not a or rng < 0.5 * a or rng <= 0:
        return None
    o, h, lo, c = day.o[k], day.h[k], day.l[k], day.c[k]
    body = abs(c - o)
    if mode == "CONT":
        if body < 0.6 * rng:
            return None
        if c > o and (h - c) <= 0.2 * rng:
            return 1, lo
        if c < o and (c - lo) <= 0.2 * rng:
            return -1, h
        return None
    upper, lower = h - max(o, c), min(o, c) - lo
    if upper >= 0.6 * rng and c <= lo + 0.5 * rng:
        return -1, h * 1.0005
    if lower >= 0.6 * rng and c >= h - 0.5 * rng:
        return 1, lo * 0.9995
    return None


# --------------------------------------------------------------------------- 6 FAILBO

def failbo(day: DayBars, k: int, N: int):
    if k <= N or not full(day, 0, k):
        return None
    a = atr(day, k)
    if not a:
        return None
    hi, lo = max(day.h[0:N]), min(day.l[0:N])
    first = None
    for b in range(N, k):
        if day.c[b] > hi or day.c[b] < lo:
            first = b
            break
    if first is None or k - first > 3 or k == first:
        return None
    if any((day.c[x] <= hi and day.c[x] >= lo) for x in range(first + 1, k)):
        return None                              # already failed earlier: only the first return counts
    if day.c[first] > hi and day.c[k] <= hi:
        return -1, max(day.h[first:k + 1]) + 0.05 * a
    if day.c[first] < lo and day.c[k] >= lo:
        return 1, min(day.l[first:k + 1]) - 0.05 * a
    return None


# --------------------------------------------------------------------------- registry
# name -> (family, kind, params, target_r, hold)

VARIANTS = {
    "SWEEP_L6": ("SWEEP", "single", {"L": 6}, 1.5, 6),
    "SWEEP_L12": ("SWEEP", "single", {"L": 12}, 1.5, 6),
    "IMPULSE_382": ("IMPULSE", "single", {"band": (0.382, 0.618)}, 1.5, 6),
    "IMPULSE_250": ("IMPULSE", "single", {"band": (0.25, 0.50)}, 1.5, 6),
    "SQUEEZE_Q50": ("SQUEEZE", "single", {"q": 0.5}, 2.0, 6),
    "SQUEEZE_Q70": ("SQUEEZE", "single", {"q": 0.7}, 2.0, 6),
    "XSRS_CONT_H3": ("XSRS", "cross", {"sign": 1}, None, 3),
    "XSRS_CONT_H6": ("XSRS", "cross", {"sign": 1}, None, 6),
    "XSRS_REV_H6": ("XSRS", "cross", {"sign": -1}, None, 6),
    "VOLSURP_CONT_3": ("VOLSURP", "single", {"threshold": 3.0, "mode": "CONT"}, 1.5, 6),
    "VOLSURP_CONT_5": ("VOLSURP", "single", {"threshold": 5.0, "mode": "CONT"}, 1.5, 6),
    "VOLSURP_REV_3": ("VOLSURP", "single", {"threshold": 3.0, "mode": "REV"}, 1.5, 6),
    "VOLSURP_REV_5": ("VOLSURP", "single", {"threshold": 5.0, "mode": "REV"}, 1.5, 6),
    "FAILBO_N3": ("FAILBO", "single", {"N": 3}, 1.5, 6),
    "FAILBO_N6": ("FAILBO", "single", {"N": 6}, 1.5, 6),
}
FAMILIES = ["SWEEP", "IMPULSE", "SQUEEZE", "XSRS", "VOLSURP", "FAILBO"]
DEFAULT_VARIANT = {"SWEEP": "SWEEP_L12", "IMPULSE": "IMPULSE_382", "SQUEEZE": "SQUEEZE_Q50",
                   "XSRS": "XSRS_CONT_H6", "VOLSURP": "VOLSURP_CONT_3", "FAILBO": "FAILBO_N6"}


def single_signal(name: str, day: DayBars, k: int, base_v=None, base_r6=None):
    family, _, p, _, _ = VARIANTS[name]
    if family == "SWEEP":
        return sweep(day, k, p["L"])
    if family == "IMPULSE":
        return impulse(day, k, p["band"])
    if family == "SQUEEZE":
        return squeeze(day, k, p["q"], base_r6)
    if family == "VOLSURP":
        return volsurp(day, k, p["threshold"], p["mode"], base_v)
    if family == "FAILBO":
        return failbo(day, k, p["N"])
    raise ValueError(name)


assert len(VARIANTS) == 15 and all(v[0] in FAMILIES for v in VARIANTS.values())
assert SLOTS == 75
