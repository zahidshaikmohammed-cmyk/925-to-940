"""Reference levels in direction-normalised prices (see features.Side).

In the LONG frame "support" lies below price and "resistance" above. For the SHORT frame
(prices negated) yesterday's HIGH becomes the normalised support and yesterday's LOW the
normalised resistance, so engines written for LONG need no mirrored copy of this logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

OR_BARS = 15                       # 09:15 .. 09:29


@dataclass(frozen=True)
class Level:
    name: str          # PDH, PDL, PDC, ORH, ORL, SWING
    price: float       # normalised price
    since: int         # first bar index at which the level is known (no look-ahead)
    significance: float


def opening_range_ok(sv) -> bool:
    """All 15 opening minutes 09:15..09:29 present (validated bars start at 09:15)."""
    if sv.n <= OR_BARS:
        return False
    return sv.ts[OR_BARS - 1].time() == time(9, 29) and sv.ts[OR_BARS].time() >= time(9, 30)


def opening_range(sv) -> tuple[float, float] | None:
    """(high, low) of the 09:15-09:29 bars in normalised prices; known from bar index 15."""
    if not opening_range_ok(sv):
        return None
    return max(sv.h[:OR_BARS]), min(sv.l[:OR_BARS])


def predefined_levels(stock, sv, or_significance: float, pd_significance: float,
                      pdc_significance: float | None = None) -> tuple[list[Level], list[Level]]:
    """(supports, resistances) known before the session or fixed at 09:30.

    PDH/PDL come from the saved previous session only (strategies/history.py); PDC from that
    session's close, else the feed's previous_close. ORH/ORL from the opening range.
    """
    sup: list[Level] = []
    res: list[Level] = []
    s = sv.s
    p = stock.prior
    if p is not None and p.pdh is not None and p.pdl is not None:
        hi, lo = (p.pdh, p.pdl) if s == 1 else (-p.pdl, -p.pdh)
        res.append(Level("PDH" if s == 1 else "PDL", hi, 0, pd_significance))
        sup.append(Level("PDL" if s == 1 else "PDH", lo, 0, pd_significance))
    pdc = (p.pdc if p is not None and p.pdc else None) or stock.previous_close
    if pdc_significance is not None and pdc:
        lv = Level("PDC", s * pdc, 0, pdc_significance)
        sup.append(lv)
        res.append(lv)
    orr = opening_range(sv)
    if orr is not None:
        res.append(Level("ORH" if s == 1 else "ORL", orr[0], OR_BARS, or_significance))
        sup.append(Level("ORL" if s == 1 else "ORH", orr[1], OR_BARS, or_significance))
    return sup, res
