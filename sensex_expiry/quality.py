"""Data-integrity layer (spec section 5). Never guesses: anything it cannot vouch for
downgrades quality, and the engine treats anything but GOOD as NO_TRADE for entries."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import DataQualityConfig
from .models import Candle, DataQuality, Reason, Tick


@dataclass
class TickValidator:
    """Per-instrument tick screening: duplicates, out-of-order, impossible jumps."""
    cfg: DataQualityConfig
    last: dict[str, Tick] = field(default_factory=dict)
    duplicates: int = 0
    out_of_order: int = 0
    rejected_jumps: int = 0
    invalid: int = 0

    def accept(self, tick: Tick, atr: float | None = None) -> bool:
        if not (tick.ltp == tick.ltp and tick.ltp > 0) or tick.ts.tzinfo is None:
            self.invalid += 1
            return False
        prev = self.last.get(tick.security_id)
        if prev is not None:
            if tick.ts == prev.ts and tick.ltp == prev.ltp and tick.volume == prev.volume:
                self.duplicates += 1
                return False
            if tick.ts < prev.ts:
                self.out_of_order += 1
                return False
            if atr and abs(tick.ltp - prev.ltp) > self.cfg.max_tick_jump_atr * atr:
                # quarantined, not applied; repeated jumps will surface as staleness
                self.rejected_jumps += 1
                return False
        self.last[tick.security_id] = tick
        return True


@dataclass
class QualityReport:
    quality: DataQuality
    reasons: list[Reason]
    data_ts: datetime | None
    data_age_ms: int | None
    detail: dict

    def as_dict(self) -> dict:
        return {"data_quality": self.quality.value, "data_age_ms": self.data_age_ms,
                "data_timestamp": self.data_ts.isoformat() if self.data_ts else None,
                "quality_reasons": [r.value for r in self.reasons], **self.detail}


def assess(cfg: DataQualityConfig, now: datetime, candles: list[Candle], last_underlying_tick: datetime | None,
           session_open: datetime, chain_ts: datetime | None = None, option_quote_ts: datetime | None = None,
           feed_connected: bool = True, backtest: bool = False) -> QualityReport:
    """Combine every check into one verdict for the decision at `now`."""
    reasons: list[Reason] = []
    detail: dict = {}
    bad = False

    if not feed_connected:
        reasons.append(Reason.DATA_STALE)
        detail["feed"] = "DISCONNECTED"
        bad = True

    invalid = [c.start.isoformat() for c in candles if not c.is_valid()]
    if invalid:
        detail["invalid_candles"] = invalid[:5]
        reasons.append(Reason.DATA_BAD)
        bad = True

    starts = [c.start for c in candles]
    if starts != sorted(starts) or len(set(starts)) != len(starts):
        detail["candle_order"] = "BROKEN"
        reasons.append(Reason.DATA_BAD)
        bad = True

    # missing minutes since the open, up to the last minute that should be closed
    if candles:
        expected = int((candles[-1].start - session_open).total_seconds() // 60) + 1
        missing = expected - len(candles)
        detail["missing_minutes"] = missing
        if missing > cfg.max_missing_minutes:
            reasons.append(Reason.DATA_BAD)
            bad = True

    data_ts = last_underlying_tick
    age_ms = None
    if not backtest:
        if data_ts is None:
            reasons.append(Reason.DATA_STALE)
            bad = True
        else:
            age_ms = int((now - data_ts).total_seconds() * 1000)
            if age_ms > cfg.max_underlying_age_ms or age_ms < -1000:
                reasons.append(Reason.DATA_STALE)
                bad = True
        if chain_ts is not None and (now - chain_ts) > timedelta(milliseconds=cfg.max_chain_age_ms):
            reasons.append(Reason.CHAIN_STALE)
            bad = True
        if option_quote_ts is not None and (now - option_quote_ts) > timedelta(milliseconds=cfg.max_option_quote_age_ms):
            reasons.append(Reason.DATA_STALE)
            bad = True
    else:
        age_ms = 0

    q = DataQuality.BAD if bad else DataQuality.GOOD
    return QualityReport(q, list(dict.fromkeys(reasons)), data_ts, age_ms, detail)
