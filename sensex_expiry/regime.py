"""Mechanical market-regime classifier (spec section 7).

STATUS: HYPOTHESIS. The thresholds are round pre-registered numbers. Whether the
regime filter adds anything is tested by the `no_regime` ablation; if removing it does
not hurt out-of-sample expectancy, the constitution says it must be removed.

Precedence (first match wins):
  1 UNCLEAR          - fewer than 20 closed bars or ATR undefined
  2 FAILED_BREAKOUT  - in the last 10 bars a close went beyond ORH/ORL/PDH/PDL by
                       >= 0.10 ATR and the latest close is back inside by >= 0.25 ATR
  3 REVERSAL         - structure flipped (UP<->DOWN) within the last 30 bars
  4 STRONG_BULL      - ER30 >= 0.45, slope > 0, close > ORH, structure UP
    STRONG_BEAR      - ER30 >= 0.45, slope < 0, close < ORL, structure DOWN
  5 EXPANSION        - latest true range >= 2.0 x ATR14
  6 COMPRESSION      - compression ratio (20 bars) <= 0.60
  7 RANGE            - ER30 < 0.25
  8 UNCLEAR          - anything else
"""
from __future__ import annotations

from .config import FeatureConfig
from .features import FeatureSnapshot, structure as structure_of, swings
from .models import Candle, PriorDay, Regime

ER_TREND = 0.45
ER_RANGE = 0.25


def classify(c: list[Candle], snap: FeatureSnapshot, prior: PriorDay | None, cfg: FeatureConfig) -> Regime:
    a = snap.atr
    if len(c) < 20 or a is None or a <= 0:
        return Regime.UNCLEAR

    levels_up = [x for x in (snap.orh, prior.high if prior else None) if x is not None]
    levels_dn = [x for x in (snap.orl, prior.low if prior else None) if x is not None]
    recent = c[-11:-1]
    last = c[-1].close
    for lv in levels_up:
        if any(x.close > lv + 0.10 * a for x in recent) and last < lv - 0.25 * a:
            return Regime.FAILED_BREAKOUT
    for lv in levels_dn:
        if any(x.close < lv - 0.10 * a for x in recent) and last > lv + 0.25 * a:
            return Regime.FAILED_BREAKOUT

    if len(c) >= 50:
        before = structure_of(swings(c[:-30], cfg.swing_k))
        now = snap.structure
        if {before, now} == {"UP", "DOWN"}:
            return Regime.REVERSAL

    er = snap.er if snap.er is not None else 0.0
    if er >= ER_TREND and snap.slope > 0 and snap.orh is not None and last > snap.orh and snap.structure == "UP":
        return Regime.STRONG_BULL
    if er >= ER_TREND and snap.slope < 0 and snap.orl is not None and last < snap.orl and snap.structure == "DOWN":
        return Regime.STRONG_BEAR
    if snap.last_tr >= cfg.expansion_tr_atr * a:
        return Regime.EXPANSION
    if snap.compression is not None and snap.compression <= cfg.compression_max:
        return Regime.COMPRESSION
    if er < ER_RANGE:
        return Regime.RANGE
    return Regime.UNCLEAR


# which regimes each setup may fire in, by direction (+1 long, -1 short)
ALLOWED = {
    "S1_SWEEP_RECLAIM": {
        1: {Regime.STRONG_BULL, Regime.RANGE, Regime.FAILED_BREAKOUT, Regime.REVERSAL, Regime.COMPRESSION,
            Regime.EXPANSION, Regime.UNCLEAR},
        -1: {Regime.STRONG_BEAR, Regime.RANGE, Regime.FAILED_BREAKOUT, Regime.REVERSAL, Regime.COMPRESSION,
             Regime.EXPANSION, Regime.UNCLEAR},
    },
    "S2_ORB_ACCEPT": {
        1: {Regime.STRONG_BULL, Regime.EXPANSION, Regime.UNCLEAR},
        -1: {Regime.STRONG_BEAR, Regime.EXPANSION, Regime.UNCLEAR},
    },
    "S3_LATE_COMPRESSION": {
        1: {Regime.COMPRESSION, Regime.EXPANSION, Regime.RANGE, Regime.UNCLEAR},
        -1: {Regime.COMPRESSION, Regime.EXPANSION, Regime.RANGE, Regime.UNCLEAR},
    },
}


def allows(setup: str, direction: int, regime: Regime) -> bool:
    """A sweep against a strong trend (long in STRONG_BEAR, short in STRONG_BULL) is blocked by omission."""
    return regime in ALLOWED.get(setup, {}).get(direction, set())
