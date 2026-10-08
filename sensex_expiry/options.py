"""Option-contract layer (spec sections 11, 15): which contract expresses a signal,
whether it is tradable, and where its premium stop sits.

Instrument decision (spec 15): the system BUYS a weekly SENSEX option (CE for LONG,
PE for SHORT). It never sells options and never buys deep OTM "lottery" strikes.
Option-chain structure (OI walls, PCR, max pain) is LOGGED for research but is not a
decision input in v1: no evidence was found that it predicts intraday direction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .config import OptionConfig
from .models import Direction, OptionQuote, Reason


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, vol: float, right: str, r: float = 0.0) -> float:
    """Black-Scholes price; used by the synthetic generator and as a delta fallback only."""
    intrinsic = max(0.0, spot - strike) if right == "CE" else max(0.0, strike - spot)
    if t_years <= 0 or vol <= 0:
        return intrinsic
    sd = vol * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t_years) / sd
    d2 = d1 - sd
    if right == "CE":
        return spot * norm_cdf(d1) - strike * math.exp(-r * t_years) * norm_cdf(d2)
    return strike * math.exp(-r * t_years) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(spot: float, strike: float, t_years: float, vol: float, right: str) -> float:
    if t_years <= 0 or vol <= 0:
        itm = spot > strike if right == "CE" else spot < strike
        return (1.0 if itm else 0.0) * (1 if right == "CE" else -1)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * t_years) / (vol * math.sqrt(t_years))
    return norm_cdf(d1) if right == "CE" else norm_cdf(d1) - 1.0


def choose_strike(spot: float, direction: Direction, cfg: OptionConfig) -> tuple[int, str]:
    """ATM = nearest strike to spot; moneyness +k moves k strikes in-the-money."""
    atm = int(round(spot / cfg.strike_step) * cfg.strike_step)
    right = "CE" if direction is Direction.LONG else "PE"
    shift = cfg.moneyness * cfg.strike_step
    strike = atm - shift if right == "CE" else atm + shift
    return strike, right


def tradability(q: OptionQuote, order_qty: int, cfg: OptionConfig, use_spread_filter: bool = True) -> list[Reason]:
    """Blocking reasons for this quote; empty list = tradable."""
    out: list[Reason] = []
    if q.ltp < cfg.min_premium:
        out.append(Reason.PREMIUM_TOO_LOW)
    if use_spread_filter:
        sp = q.spread_pct
        if sp is None or sp > cfg.max_spread_pct:
            out.append(Reason.SPREAD_TOO_WIDE)
        if q.top5_ask_qty is not None and q.top5_ask_qty < cfg.min_top5_depth_mult * order_qty:
            out.append(Reason.DEPTH_TOO_THIN)
    return out


@dataclass(frozen=True)
class PremiumPlan:
    entry: float
    stop: float
    delta_used: float
    stop_frac: float
    reasons: tuple[Reason, ...]


def premium_stop(entry: float, underlying_stop_distance: float, delta: float | None, cfg: OptionConfig) -> PremiumPlan:
    """Premium stop = entry - |delta| x underlying stop distance x 1.2, floored at 10% and
    capped at 50% of premium. A structure that needs more than 50% is not traded."""
    d = abs(delta) if delta is not None and 0.05 <= abs(delta) <= 1.0 else cfg.default_atm_delta
    raw = d * underlying_stop_distance * cfg.stop_delta_mult
    frac = raw / entry if entry > 0 else 1.0
    reasons: tuple[Reason, ...] = ()
    if frac > cfg.max_stop_frac:
        reasons = (Reason.STOP_TOO_WIDE,)
    frac = min(max(frac, cfg.min_stop_frac), cfg.max_stop_frac)
    return PremiumPlan(entry, round(entry * (1 - frac), 2), d, frac, reasons)


def parse_dhan_chain(payload: dict, now: datetime) -> tuple[float | None, dict[tuple[int, str], OptionQuote]]:
    """Parse a Dhan /v2/optionchain response defensively.

    Expected shape (from the dhanhq SDK docstring and this repo's 50options.py):
    {"data": {"last_price": <underlying>, "oc": {"<strike>": {"ce": {...}, "pe": {...}}}}}
    with per-side keys last_price, top_bid_price, top_ask_price, oi, volume,
    implied_volatility and greeks.delta. Any key that is absent stays None; an
    unrecognised payload returns (None, {}) and the caller must treat that as NO_TRADE.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict) or not isinstance(data.get("oc"), dict):
        return None, {}
    spot = data.get("last_price")
    spot = float(spot) if isinstance(spot, (int, float)) and spot > 0 else None
    out: dict[tuple[int, str], OptionQuote] = {}
    for k, row in data["oc"].items():
        try:
            strike = int(round(float(k)))
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        for side, right in (("ce", "CE"), ("pe", "PE")):
            s = row.get(side)
            if not isinstance(s, dict):
                continue
            ltp = s.get("last_price")
            if not isinstance(ltp, (int, float)) or ltp <= 0:
                continue
            g = s.get("greeks") if isinstance(s.get("greeks"), dict) else {}

            def num(v):
                return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None

            out[(strike, right)] = OptionQuote(
                strike=strike, right=right, ltp=float(ltp), bid=num(s.get("top_bid_price")),
                ask=num(s.get("top_ask_price")), ts=now, delta=num(g.get("delta")),
                iv=num(s.get("implied_volatility")), oi=s.get("oi") if isinstance(s.get("oi"), int) else None,
                volume=s.get("volume") if isinstance(s.get("volume"), int) else None)
    return spot, out


def chain_research_features(chain: dict[tuple[int, str], OptionQuote], spot: float, step: int = 100) -> dict:
    """Logged only (spec 11). Not used by any decision in v1."""
    atm = int(round(spot / step) * step)
    near = [k for k in chain if abs(k[0] - atm) <= 5 * step]
    ce_oi = sum(chain[k].oi or 0 for k in near if k[1] == "CE")
    pe_oi = sum(chain[k].oi or 0 for k in near if k[1] == "PE")
    best_ce = max((k for k in chain if k[1] == "CE"), key=lambda k: chain[k].oi or 0, default=None)
    best_pe = max((k for k in chain if k[1] == "PE"), key=lambda k: chain[k].oi or 0, default=None)
    return {"atm": atm, "pcr_near": (pe_oi / ce_oi) if ce_oi else None,
            "max_ce_oi_strike": best_ce[0] if best_ce else None,
            "max_pe_oi_strike": best_pe[0] if best_pe else None}
