"""Strategy engine: closed candles in, explainable decision out (spec sections 16-21, 37).

The engine decides; it never places orders. Execution, the broker and the kill switch
live elsewhere and can veto it. Identical code serves backtest, paper and live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from .config import EngineConfig
from .features import snapshot
from .models import Action, Candle, Decision, OptionQuote, PriorDay, Reason, SetupCandidate
from .options import choose_strike, premium_stop, tradability
from .quality import QualityReport
from .regime import allows, classify
from .risk import DailyRiskState, pre_trade_checks, size_position
from .setups import detect

QuoteFn = Callable[[int, str], "OptionQuote | None"]
PRIORITY = ("S1_SWEEP_RECLAIM", "S2_ORB_ACCEPT", "S3_LATE_COMPRESSION")
CONFIRMATIONS = ("displacement", "level_confluence", "room_2r", "regime_aligned", "prime_window")


def score(confirmations: dict[str, bool]) -> int:
    """Transparent 0-100: share of optional confirmations present. LOGGED ONLY.
    Spec section 20: with the sample sizes available, fitted weights or score
    thresholds would be curve-fitting, so the score gates nothing in v1."""
    return round(100 * sum(bool(confirmations.get(k)) for k in CONFIRMATIONS) / len(CONFIRMATIONS))


@dataclass
class StrategyEngine:
    cfg: EngineConfig
    day: date
    session_open: datetime
    prior: PriorDay | None
    is_expiry: bool
    expiry: date | None
    lot_size: int
    risk_state: DailyRiskState = field(default=None)  # type: ignore[assignment]

    def __post_init__(self):
        self.cfg.validate()
        if self.risk_state is None:
            self.risk_state = DailyRiskState(day=str(self.day))

    def evaluate_entry(self, candles: list[Candle], now: datetime, quality: QualityReport, quote: QuoteFn,
                       capital: float | None = None) -> Decision:
        cfg = self.cfg
        bar = candles[-1] if candles else None
        bar_close = bar.start + timedelta(minutes=1) if bar else now

        def no(*reasons: Reason, cand: SetupCandidate | None = None, extra: dict | None = None) -> Decision:
            return Decision(Action.NO_TRADE, now, list(reasons), cand,
                            self._payload(Action.NO_TRADE, now, bar, cand, list(reasons), quality, extra or {}))

        if not self.is_expiry:
            return no(Reason.NOT_EXPIRY_DAY)
        if quality.quality.value != "GOOD":
            return no(*(quality.reasons or [Reason.DATA_BAD]))
        if bar is None:
            return no(Reason.DATA_BAD)
        t = bar_close.time()
        if t > cfg.session.last_entry or (cfg.use_time_filter and t < cfg.session.no_entry_before):
            return no(Reason.OUTSIDE_WINDOW)

        snap = snapshot(candles, self.session_open, self.prior, cfg.features)
        regime = classify(candles, snap, self.prior, cfg.features)
        extra = {"regime": regime.value, "atr": snap.atr, "er": snap.er, "compression": snap.compression}

        cands = detect(candles, self.session_open, self.prior, cfg)
        if not cands:
            return no(Reason.NO_EDGE, extra=extra)
        if len({c.direction for c in cands}) > 1:
            return no(Reason.REGIME_CONFLICT, extra=extra)
        cand = sorted(cands, key=lambda c: PRIORITY.index(c.setup))[0]
        s = cand.direction.sign

        aligned = allows(cand.setup, s, regime)
        if cfg.use_regime_filter and not aligned:
            return no(Reason.REGIME_CONFLICT, cand=cand, extra=extra)
        dist = cand.stop_distance
        room_r = None if cand.room_level is None else s * (cand.room_level - cand.trigger_price) / dist
        if cfg.use_room_filter and room_r is not None and room_r < cfg.setups.min_room_r:
            return no(Reason.ROOM_TOO_SMALL, cand=cand, extra=extra | {"room_r": room_r})

        blocked = pre_trade_checks(cfg, self.risk_state, now, cand.key)
        if blocked:
            return no(*blocked, cand=cand, extra=extra)

        strike, right = choose_strike(cand.trigger_price, cand.direction, cfg.options)
        q = quote(strike, right)
        if q is None:
            return no(Reason.DATA_STALE, cand=cand, extra=extra | {"strike": strike, "right": right})
        entry_ref = q.ask if q.ask else q.ltp
        plan = premium_stop(entry_ref, dist, q.delta, cfg.options)
        if plan.reasons:
            return no(*plan.reasons, cand=cand, extra=extra | {"stop_frac_needed": plan.stop_frac})
        size = size_position(cfg, entry_ref, plan.stop, self.lot_size, capital)
        if not size.approved:
            return no(*size.reasons, cand=cand, extra=extra)
        tradable = tradability(q, size.qty, cfg.options, cfg.use_spread_filter)
        if tradable:
            return no(*tradable, cand=cand, extra=extra | {"spread_pct": q.spread_pct})

        cand.confirmations.update({"room_2r": room_r is None or room_r >= 2.0, "regime_aligned": aligned,
                                   "prime_window": cand.setup != "S2_ORB_ACCEPT" or t.hour < 11})
        reasons = list(cand.reasons) + [Reason.RISK_REWARD_VALID]
        if aligned:
            reasons.append(Reason.REGIME_ALIGNED)
        if cand.confirmations.get("displacement") and Reason.DISPLACEMENT not in reasons:
            reasons.append(Reason.DISPLACEMENT)
        if cand.confirmations.get("level_confluence"):
            reasons.append(Reason.LEVEL_CONFLUENCE)
        extra |= {"room_r": room_r, "strike": strike, "right": right, "security_id": q.security_id,
                  "option_ltp": q.ltp, "bid": q.bid, "ask": q.ask, "spread_pct": q.spread_pct,
                  "delta_used": plan.delta_used, "premium_stop": plan.stop, "stop_frac": plan.stop_frac,
                  "lots": size.lots, "qty": size.qty, "one_r_rupees": round(size.one_r, 2),
                  "outlay": round(size.outlay, 2), "est_costs": round(size.est_costs, 2),
                  "score": score(cand.confirmations)}
        return Decision(Action.BUY, now, reasons, cand,
                        self._payload(Action.BUY, now, bar, cand, reasons, quality, extra))

    def _payload(self, action: Action, now: datetime, bar: Candle | None, cand: SetupCandidate | None,
                 reasons: list[Reason], quality: QualityReport, extra: dict) -> dict:
        """Signal JSON, spec section 37. Every field is reproducible from the audit log."""
        p = {
            "schema": "sensex-expiry-signal/1",
            "engine_version": self.cfg.version,
            "config_hash": self.cfg.config_hash(),
            "symbol": "SENSEX",
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "signal_timestamp": now.isoformat(),
            "bar_timestamp": bar.start.isoformat() if bar else None,
            "underlying_close": bar.close if bar else None,
            "action": action.value,
            "setup": cand.setup if cand else None,
            "direction": cand.direction.value if cand else None,
            "level": {"name": cand.level_name, "price": cand.level} if cand else None,
            "trigger": cand.trigger_price if cand else None,
            "invalidation": cand.invalidation if cand else None,
            "confirmations": cand.confirmations if cand else {},
            "reason_codes": [r.value for r in reasons],
        }
        p |= quality.as_dict()
        p |= {k: (round(v, 4) if isinstance(v, float) else v) for k, v in extra.items()}
        if action is Action.BUY:
            p["instrument"] = f"SENSEX {self.expiry} {extra['strike']} {extra['right']}"
            p["entry"] = extra.get("ask") or extra.get("option_ltp")
            p["stop_loss"] = extra["premium_stop"]
            p["target"] = None if self.cfg.exits.policy in ("TRAIL", "TIME_ONLY") else self.cfg.exits.policy
            p["risk"] = extra["one_r_rupees"]
            p["expected_R"] = None   # unknown until validated; never invented
        return p

    def on_entry(self, key: str) -> None:
        self.risk_state.trades += 1
        self.risk_state.open_positions += 1
        self.risk_state.traded_keys.add(key)
