from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from math import isfinite
from statistics import median
from typing import Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
START = time(9, 25)
LOCK = time(9, 40)


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None
    complete: bool = True


@dataclass(frozen=True)
class HTFContext:
    """Already-confirmed higher-timeframe state from PSYGRID."""
    trend_15m: int  # -1, 0, +1
    trend_1h: int   # -1, 0, +1


@dataclass(frozen=True)
class BenchmarkSnapshot:
    opening_price: float
    current_price: float

    @property
    def return_pct(self) -> float:
        return 100.0 * (self.current_price / self.opening_price - 1.0)


@dataclass(frozen=True)
class CandidateSnapshot:
    symbol: str
    candles: tuple[Candle, ...]
    benchmark: BenchmarkSnapshot
    sector_returns_pct: tuple[float, ...]
    htf: HTFContext
    current_price: float
    current_vwap: float
    relative_volume: Optional[float] = None


@dataclass(frozen=True)
class FeatureVector:
    direction: int
    momentum: float
    relative_strength: float
    sector_strength: float
    relative_volume: float
    vwap_quality: float
    structure: float
    htf_alignment: float
    persistence: float
    extension_vwap_atr: float
    extension_extreme_atr: float
    score: float


@dataclass(frozen=True)
class Decision:
    symbol: str
    direction: str
    score: float
    features: FeatureVector
    qualified: bool
    rejection_reasons: tuple[str, ...] = ()


@dataclass
class CandidateState:
    observations: list[int] = field(default_factory=list)

    def record(self, direction: int) -> None:
        self.observations.append(direction)

    @property
    def persistence(self) -> float:
        if not self.observations:
            return 0.0
        d = self.observations[-1]
        return sum(x == d for x in self.observations) / len(self.observations)

    @property
    def count(self) -> int:
        return len(self.observations)


@dataclass
class SessionState:
    candidates: dict[str, CandidateState] = field(default_factory=dict)
    locked: bool = False
    locked_decisions: tuple[Decision, ...] = ()

    def record(self, symbol: str, direction: int) -> None:
        if self.locked:
            raise RuntimeError("09:40 decision is already locked")
        self.candidates.setdefault(symbol, CandidateState()).record(direction)


# ------------------------- numerical primitives -------------------------


def _finite_positive(x: float) -> bool:
    return isfinite(x) and x > 0.0


def clamp01(x: float) -> float:
    if not isfinite(x):
        return 0.0
    return max(0.0, min(1.0, x))


def scale(x: float, low: float, high: float) -> float:
    if high <= low:
        raise ValueError("invalid scale interval")
    return clamp01((x - low) / (high - low))


def true_ranges(candles: Iterable[Candle]) -> list[float]:
    out: list[float] = []
    prev_close: Optional[float] = None
    for c in candles:
        if prev_close is None:
            tr = c.high - c.low
        else:
            tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        out.append(max(0.0, tr))
        prev_close = c.close
    return out


def volatility_unit(candles: tuple[Candle, ...]) -> float:
    """Robust 1-minute price unit from the observed opening bars.

    No future bars are used. A floor prevents an artificially tiny ATR-like
    denominator in a flat opening.
    """
    trs = true_ranges(candles)
    if not trs:
        return 0.0
    opening_range = max(c.high for c in candles) - min(c.low for c in candles)
    return max(median(trs), opening_range / 20.0)


def opening_range(candles: tuple[Candle, ...]) -> tuple[float, float]:
    return min(c.low for c in candles), max(c.high for c in candles)


def opening_return_pct(candles: tuple[Candle, ...]) -> float:
    return 100.0 * (candles[-1].close / candles[0].open - 1.0)


def infer_direction(candles: tuple[Candle, ...], benchmark_return_pct: float) -> int:
    r = opening_return_pct(candles)
    rs = r - benchmark_return_pct
    if r >= 0.35 and rs >= 0.20:
        return 1
    if r <= -0.35 and rs <= -0.20:
        return -1
    return 0


def sector_median(sector_returns_pct: tuple[float, ...]) -> Optional[float]:
    valid = [x for x in sector_returns_pct if isfinite(x)]
    return median(valid) if valid else None


def direction_consistent(x: float, direction: int) -> bool:
    return direction * x > 0.0


# ------------------------- strategy mathematics -------------------------


def compute_features(snapshot: CandidateSnapshot, persistence: float) -> FeatureVector:
    candles = tuple(c for c in snapshot.candles if c.complete)
    if len(candles) < 10:
        raise ValueError("at least 10 completed opening candles are required")

    opening_low, opening_high = opening_range(candles[:10])
    stock_ret = opening_return_pct(candles[:10])
    nifty_ret = snapshot.benchmark.return_pct
    rs = stock_ret - nifty_ret
    sec_med = sector_median(snapshot.sector_returns_pct)
    if sec_med is None:
        raise ValueError("sector return set is empty")
    sec_rs = stock_ret - sec_med

    direction = infer_direction(candles[:10], nifty_ret)
    if direction == 0:
        # During later evaluations, permit a direction established by the
        # latest completed candle while keeping the same hard thresholds.
        latest_ret = 100.0 * (snapshot.current_price / candles[0].open - 1.0)
        latest_rs = latest_ret - nifty_ret
        if latest_ret >= 0.35 and latest_rs >= 0.20:
            direction = 1
        elif latest_ret <= -0.35 and latest_rs <= -0.20:
            direction = -1

    if direction == 0:
        raise ValueError("no directional edge")

    atr = volatility_unit(candles[:10])
    if atr <= 0:
        raise ValueError("zero volatility unit")

    vwap_distance = direction * (snapshot.current_price - snapshot.current_vwap) / atr
    extreme_distance = (
        direction * (snapshot.current_price - opening_high) / atr
        if direction > 0
        else direction * (snapshot.current_price - opening_low) / atr
    )

    # Momentum: 0.35% is the qualification floor; 1.25% is full credit.
    momentum = scale(direction * stock_ret, 0.35, 1.25)
    rel_strength = scale(direction * rs, 0.20, 0.80)
    sector_strength = scale(direction * sec_rs, 0.10, 0.60)

    rv = snapshot.relative_volume
    if rv is None:
        # Conservative intraday activity proxy when true historical RVOL is
        # unavailable. This is explicitly NOT labelled as historical RVOL.
        recent_vol = [c.volume for c in candles[-5:] if _finite_positive(c.volume)]
        base_vol = [c.volume for c in candles[:5] if _finite_positive(c.volume)]
        rv = median(recent_vol) / median(base_vol) if recent_vol and base_vol else 0.0
    relative_volume = scale(rv, 1.0, 2.0)

    # VWAP quality rewards being above/below VWAP without rewarding extreme
    # extension. 1 ATR is full quality.
    vwap_quality = scale(vwap_distance, 0.0, 1.0)

    closes = [c.close for c in candles[-3:]]
    highs = [c.high for c in candles[-3:]]
    lows = [c.low for c in candles[-3:]]
    if direction > 0:
        continuation = closes[-1] > closes[-2] and highs[-1] >= highs[-2] and lows[-1] >= lows[-2]
        opening_break = snapshot.current_price >= opening_high
    else:
        continuation = closes[-1] < closes[-2] and highs[-1] <= highs[-2] and lows[-1] <= lows[-2]
        opening_break = snapshot.current_price <= opening_low
    structure = 1.0 if opening_break else (0.75 if continuation else 0.0)

    htf_alignment = (float(snapshot.htf.trend_15m == direction) + float(snapshot.htf.trend_1h == direction)) / 2.0

    score = 100.0 * (
        0.22 * momentum
        + 0.22 * rel_strength
        + 0.16 * sector_strength
        + 0.14 * relative_volume
        + 0.10 * vwap_quality
        + 0.08 * structure
        + 0.05 * htf_alignment
        + 0.03 * clamp01(persistence)
    )

    return FeatureVector(
        direction=direction,
        momentum=momentum,
        relative_strength=rel_strength,
        sector_strength=sector_strength,
        relative_volume=relative_volume,
        vwap_quality=vwap_quality,
        structure=structure,
        htf_alignment=htf_alignment,
        persistence=clamp01(persistence),
        extension_vwap_atr=abs(snapshot.current_price - snapshot.current_vwap) / atr,
        extension_extreme_atr=abs(extreme_distance),
        score=score,
    )


def hard_gate_reasons(snapshot: CandidateSnapshot, features: FeatureVector, state: CandidateState) -> list[str]:
    candles = tuple(c for c in snapshot.candles if c.complete)
    reasons: list[str] = []
    if len(candles) < 10:
        reasons.append("insufficient_opening_history")
        return reasons

    if not _finite_positive(snapshot.current_price):
        reasons.append("invalid_price")

    traded_values = [c.close * c.volume for c in candles[:10] if _finite_positive(c.close) and _finite_positive(c.volume)]
    if not traded_values or median(traded_values) < 2_500_000.0:
        reasons.append("insufficient_liquidity")

    opening_low, opening_high = opening_range(candles[:10])
    stock_ret = opening_return_pct(candles[:10])
    nifty_ret = snapshot.benchmark.return_pct
    rs = stock_ret - nifty_ret
    sec_med = sector_median(snapshot.sector_returns_pct)
    sec_rs = stock_ret - sec_med if sec_med is not None else 0.0
    d = features.direction

    if d * stock_ret < 0.35:
        reasons.append("opening_move_below_0_35_pct")
    if d * rs < 0.20:
        reasons.append("relative_strength_below_0_20_pct")
    if d * sec_rs < 0.10:
        reasons.append("sector_relative_strength_below_0_10_pct")
    if d * (snapshot.current_price - snapshot.current_vwap) <= 0:
        reasons.append("wrong_side_of_vwap")
    if features.extension_vwap_atr > 1.80:
        reasons.append("extended_from_vwap")
    if features.extension_extreme_atr > 1.25:
        reasons.append("extended_from_opening_extreme")

    if features.structure <= 0:
        reasons.append("no_directional_structure_confirmation")
    if features.htf_alignment < 1.0:
        reasons.append("higher_timeframe_misalignment")
    if state.count < 3:
        reasons.append("less_than_3_observations")
    if state.persistence < 0.60:
        reasons.append("persistence_below_0_60")

    return reasons


def evaluate(snapshot: CandidateSnapshot, state: CandidateState) -> Decision:
    candles = tuple(c for c in snapshot.candles if c.complete)
    if len(candles) < 10:
        raise ValueError("10 completed opening candles required")

    # Record the current directional state before final persistence is scored.
    d = infer_direction(candles[:10], snapshot.benchmark.return_pct)
    if d == 0:
        latest_ret = 100.0 * (snapshot.current_price / candles[0].open - 1.0)
        latest_rs = latest_ret - snapshot.benchmark.return_pct
        d = 1 if latest_ret >= 0.35 and latest_rs >= 0.20 else -1 if latest_ret <= -0.35 and latest_rs <= -0.20 else 0
    if d:
        state.record(d)

    features = compute_features(snapshot, state.persistence)
    reasons = hard_gate_reasons(snapshot, features, state)
    return Decision(
        symbol=snapshot.symbol,
        direction="LONG" if features.direction > 0 else "SHORT",
        score=features.score,
        features=features,
        qualified=not reasons,
        rejection_reasons=tuple(reasons),
    )


def rank_and_lock(decisions: Iterable[Decision], now: datetime, top_n: int = 3) -> tuple[Decision, ...]:
    """Final 09:40 lock. The returned tuple is immutable and deterministic."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(IST)
    if local.time() < LOCK:
        raise ValueError("final decisions cannot be locked before 09:40 IST")
    if top_n <= 0:
        return ()

    qualified = [d for d in decisions if d.qualified]
    qualified.sort(
        key=lambda d: (
            -d.score,
            -d.features.relative_strength,
            -d.features.sector_strength,
            -d.features.persistence,
            d.features.extension_vwap_atr,
            d.symbol,
        )
    )
    return tuple(qualified[:top_n])


def in_evaluation_window(ts: datetime) -> bool:
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    local = ts.astimezone(IST)
    return START <= local.time() <= LOCK


def parse_ohlcv(payload: Mapping[str, object]) -> Candle:
    """Strict parser for a PSYGRID-like candle object; no defaults for OHLCV."""
    required = ("timestamp", "open", "high", "low", "close", "volume")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"missing candle fields: {','.join(missing)}")
    ts = payload["timestamp"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if not isinstance(ts, datetime) or ts.tzinfo is None:
        raise ValueError("candle timestamp must be timezone-aware")
    values = {k: float(payload[k]) for k in required[1:]}
    if not all(isfinite(v) for v in values.values()):
        raise ValueError("non-finite OHLCV")
    if values["open"] <= 0 or values["high"] <= 0 or values["low"] <= 0 or values["close"] <= 0 or values["volume"] < 0:
        raise ValueError("invalid OHLCV values")
    if values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]):
        raise ValueError("invalid OHLC geometry")
    return Candle(
        timestamp=ts,
        open=values["open"],
        high=values["high"],
        low=values["low"],
        close=values["close"],
        volume=values["volume"],
        vwap=float(payload["vwap"]) if payload.get("vwap") is not None else None,
        complete=bool(payload.get("complete", True)),
    )
