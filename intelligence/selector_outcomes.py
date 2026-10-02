"""Post-decision outcome evaluation.

Strictly separated from the decision path: selector_features / selector_scoring /
selector_945.decide never import this module (enforced by a test). It receives an
already-published decision plus the full RawSession and only reads candles in
[cutoff, cutoff + horizon).

Per horizon (minutes after the 09:45 decision), with entry = the decision's
reference price (last completed close at 09:44) and d = +1 UP / -1 DOWN:
    forward_return_pct   d * (close at end of window / entry - 1) * 100
    mfe_pct / mfe_minute best favourable excursion inside the window (>= 0) and the minute
                         (1-based, after 09:45) it first occurred
    mae_pct / mae_minute worst adverse excursion inside the window (<= 0) and its minute
    direction_hit        forward_return_pct > 0
    minutes_to_favorable first minute whose favourable excursion >= move threshold
    minutes_to_adverse   first minute whose adverse excursion <= -move threshold
    outcome              WIN / LOSS / FLAT vs the probability threshold; INCOMPLETE if
                         the window is not fully available yet; NO_DATA_FINAL /
                         INCOMPLETE_FINAL once the session can no longer complete it
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from .selector_data import RawSession, future_bars


@dataclass(frozen=True)
class HorizonOutcome:
    horizon_min: int
    bars: int
    complete: bool
    forward_return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    mfe_minute: int | None
    mae_minute: int | None
    direction_hit: bool | None
    minutes_to_favorable: int | None
    minutes_to_adverse: int | None
    outcome: str

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_horizon(entry: float, direction: str, bars, horizon: int, move_threshold: float,
                     hit_threshold: float, final: bool = False) -> HorizonOutcome:
    d = 1 if direction == "UP" else -1
    n = len(bars) if bars is not None else 0
    if n == 0 or entry <= 0:
        return HorizonOutcome(horizon, 0, False, None, None, None, None, None, None, None, None,
                              "NO_DATA_FINAL" if final else "NO_DATA")
    mfe = mae = 0.0
    mfe_min = mae_min = None
    t_fav = t_adv = None
    for i in range(n):
        fav = d * ((bars.h[i] if d == 1 else bars.l[i]) / entry - 1) * 100.0
        adv = d * ((bars.l[i] if d == 1 else bars.h[i]) / entry - 1) * 100.0
        if fav > mfe:
            mfe, mfe_min = fav, i + 1
        if adv < mae:
            mae, mae_min = adv, i + 1
        if t_fav is None and fav >= move_threshold:
            t_fav = i + 1
        if t_adv is None and adv <= -move_threshold:
            t_adv = i + 1
    fwd = d * (bars.c[-1] / entry - 1) * 100.0
    complete = n >= horizon
    if not complete:
        label = "INCOMPLETE_FINAL" if final else "INCOMPLETE"
    elif fwd > hit_threshold:
        label = "WIN"
    elif fwd < -hit_threshold:
        label = "LOSS"
    else:
        label = "FLAT"
    return HorizonOutcome(horizon, n, complete, fwd, mfe, mae, mfe_min, mae_min, fwd > 0, t_fav, t_adv, label)


def evaluate_decision(decision: dict, raw: RawSession, horizons, move_threshold: float,
                      hit_threshold: float, final: bool = False) -> list[HorizonOutcome]:
    """`decision` is DecisionSnapshot.to_dict() (or the stored JSON)."""
    if raw.session_date.isoformat() != decision["decision_date"]:
        raise ValueError(f"session {raw.session_date} does not match decision {decision['decision_date']}")
    cutoff = datetime.fromisoformat(decision["cutoff"])
    entry = float(decision["reference_price"])
    out = []
    for h in horizons:
        bars = future_bars(raw, decision["selected_symbol"], cutoff, h)
        out.append(evaluate_horizon(entry, decision["direction"], bars, h, move_threshold, hit_threshold, final))
    return out


def forward_returns_universe(raw: RawSession, cutoff: datetime, horizon: int, reference: dict) -> dict:
    """Unsigned forward return for every stock (feature-validation / IC only).
    `reference` = symbol -> price at the decision (pre-cutoff last close)."""
    out = {}
    for sym, ref in reference.items():
        bars = future_bars(raw, sym, cutoff, horizon)
        if bars is not None and len(bars) >= horizon and ref:     # complete windows only
            out[sym] = (bars.c[-1] / ref - 1) * 100.0
    return out
