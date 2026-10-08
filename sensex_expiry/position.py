"""Open-position management and exits (spec sections 17-19).

Order of checks on every closed bar, most conservative first:
  1 premium stop      - resting exchange SL order; in a bar that touches both stop and
                        target, the STOP is assumed to fill first
  2 fixed target      - only for FIXED_2R / FIXED_3R policies
  3 hard flat         - the bar that closes at/after hard_flat - 1 min forces an exit
  4 invalidation      - underlying CLOSE beyond the setup invalidation level
  5 trailing exit     - TRAIL policy, after MFE >= trail_after_r: underlying close
                        beyond (best close - trail_atr_mult x ATR)
  6 time stop         - after time_stop_bars, MFE still < time_stop_min_r
  7 max hold          - max_hold_bars
Close-based exits (3-7) fill at the NEXT bar's open, minus slippage.
The premium stop only ever moves toward profit (breakeven at +1R); it never widens.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import EngineConfig
from .costs import round_trip
from .models import Candle, Direction, Reason


@dataclass
class Position:
    key: str
    setup: str
    direction: Direction
    strike: int
    right: str
    qty: int
    entry_ts: datetime
    entry_price: float
    stop: float
    invalidation: float
    one_r: float                 # rupees, including modelled costs
    atr_at_entry: float
    initial_stop: float = 0.0
    bars_held: int = 0
    best_close: float = 0.0      # best underlying close in trade direction
    peak_premium: float = 0.0
    pending_exit: Reason | None = None
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: Reason | None = None
    log: list[dict] = field(default_factory=list)

    @property
    def per_unit_risk(self) -> float:
        return self.one_r / self.qty if self.qty else 0.0

    def r_at(self, premium: float) -> float:
        return (premium - self.entry_price) * self.qty / self.one_r if self.one_r else 0.0

    @property
    def open(self) -> bool:
        return self.exit_ts is None


def open_position(cfg: EngineConfig, key: str, setup: str, direction: Direction, strike: int, right: str, qty: int,
                  fill_ts: datetime, fill_price: float, stop_frac: float, invalidation: float, atr_value: float,
                  underlying_close: float) -> Position:
    """Re-anchor the premium stop on the ACTUAL fill (same fraction), and recompute 1R."""
    stop = round(fill_price * (1 - stop_frac), 2)
    slip = cfg.risk.slippage_ticks * cfg.risk.tick_size
    one_r = (fill_price - (stop - slip)) * qty + round_trip(fill_price, stop - slip, qty, cfg.costs).total
    return Position(key, setup, direction, strike, right, qty, fill_ts, fill_price, stop, invalidation, one_r,
                    atr_value, initial_stop=stop, best_close=underlying_close, peak_premium=fill_price)


def manage(cfg: EngineConfig, pos: Position, und: Candle, opt: Candle | None, atr_value: float | None) -> None:
    """Advance the position by one closed bar. Mutates pos; sets exit fields when it closes."""
    if not pos.open:
        return
    slip = cfg.risk.slippage_ticks * cfg.risk.tick_size
    ex = cfg.exits

    if opt is None:
        # no option print this minute: cannot verify the stop. Exit at the next available bar.
        pos.pending_exit = pos.pending_exit or Reason.EXIT_DATA_FAILURE
        pos.log.append({"ts": und.start.isoformat(), "event": "OPTION_BAR_MISSING"})
        return

    # 0 fill a pending close-based exit at this bar's open
    if pos.pending_exit is not None:
        _close(pos, opt.start, max(opt.open - slip, 0.05), pos.pending_exit)
        return

    pos.bars_held += 1
    # 1 premium stop
    if opt.low <= pos.stop:
        fill = min(pos.stop, opt.open) - slip
        _close(pos, opt.start, max(fill, 0.05), Reason.EXIT_STOP_PREMIUM)
        return
    # 2 fixed target
    if ex.policy in ("FIXED_2R", "FIXED_3R"):
        k = 2.0 if ex.policy == "FIXED_2R" else 3.0
        target = pos.entry_price + k * pos.per_unit_risk
        if opt.high >= target:
            _close(pos, opt.start, max(target, opt.open) if opt.open >= target else target, Reason.EXIT_TARGET)
            return

    pos.peak_premium = max(pos.peak_premium, opt.high)
    mfe_r = pos.r_at(pos.peak_premium)
    s = pos.direction.sign
    if s * (und.close - pos.best_close) > 0:
        pos.best_close = und.close

    # breakeven ratchet (never loosens)
    if mfe_r >= ex.breakeven_at_r:
        # entry + per-unit costs and slippage (= 1R per unit minus the initial stop distance)
        be = round(pos.entry_price + (pos.one_r / pos.qty - (pos.entry_price - pos.initial_stop)) + slip, 2)
        be = max(be, pos.entry_price)
        if be > pos.stop:
            pos.log.append({"ts": und.start.isoformat(), "event": "STOP_RATCHET", "from": pos.stop, "to": be})
            pos.stop = be

    bar_close = und.start + timedelta(minutes=1)
    if bar_close.time() >= cfg.session.hard_flat:
        pos.pending_exit = Reason.EXIT_HARD_FLAT
    elif s * (und.close - pos.invalidation) < 0:
        pos.pending_exit = Reason.EXIT_INVALIDATION
    elif (ex.policy == "TRAIL" and mfe_r >= ex.trail_after_r and atr_value
          and s * (und.close - (pos.best_close - s * ex.trail_atr_mult * atr_value)) < 0):
        pos.pending_exit = Reason.EXIT_TRAIL
    elif pos.bars_held >= ex.time_stop_bars and mfe_r < ex.time_stop_min_r:
        pos.pending_exit = Reason.EXIT_TIME_STOP
    elif pos.bars_held >= ex.max_hold_bars:
        pos.pending_exit = Reason.EXIT_MAX_HOLD


def force_exit(cfg: EngineConfig, pos: Position, ts: datetime, price: float, reason: Reason) -> None:
    slip = cfg.risk.slippage_ticks * cfg.risk.tick_size
    _close(pos, ts, max(price - slip, 0.05), reason)


def _close(pos: Position, ts: datetime, price: float, reason: Reason) -> None:
    pos.exit_ts, pos.exit_price, pos.exit_reason = ts, round(price, 2), reason
    pos.pending_exit = None


def realized(cfg: EngineConfig, pos: Position) -> dict:
    assert pos.exit_price is not None
    costs = round_trip(pos.entry_price, pos.exit_price, pos.qty, cfg.costs)
    gross = (pos.exit_price - pos.entry_price) * pos.qty
    net = gross - costs.total
    return {"gross": round(gross, 2), "costs": round(costs.total, 2), "net": round(net, 2),
            "r_gross": round(gross / pos.one_r, 4), "r_net": round(net / pos.one_r, 4),
            "mfe_r": round(pos.r_at(pos.peak_premium), 4)}
