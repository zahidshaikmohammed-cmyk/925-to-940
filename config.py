from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    # Exchange/session clock
    timezone: str = "Asia/Kolkata"
    session_start: str = "09:15"
    signal_time: str = "09:30"  # legacy field retained for compatibility
    entry_time: str = "09:31"   # legacy field retained for compatibility
    market_close: str = "15:30"

    # Feed integrity
    universe_size: int = 450
    shard_count: int = 10
    shard_size: int = 45
    # A few seconds of network/publication delay is normal. Only a snapshot
    # more than four minutes behind the local PC clock is considered stale.
    max_ltp_age_seconds: float = 240.0
    min_completed_1m: int = 5
    require_full_09_15_to_09_29_grid: bool = False
    poll_seconds: float = 1.0
    http_timeout_seconds: float = 4.0
    preflight_retry_seconds: float = 2.0

    # Opening-momentum geometry. The scan evaluates the complete current
    # session available at the moment it is run, rather than freezing at 09:30.
    min_impulse_pct: float = 0.35
    min_impulse_bars: int = 3
    min_retracement_bars: int = 3
    min_retracement_depth: float = 0.38
    max_retracement_depth: float = 0.70
    min_reclaim_ratio: float = 0.50

    # Hard anti-gap / anti-exhaustion rules for Tier 1/2.
    # Tier 3 scores these conditions as penalties so a healthy feed still
    # produces a deterministic #1 rather than a strategic NO SIGNAL.
    max_gap_pct: float = 3.0
    max_gap_z: float = 3.0
    max_impulse_atr: float = 3.50
    max_extension_from_vwap_atr: float = 2.00
    late_extreme_bar: int = 12
    exhaustion_reclaim_distance_atr: float = 0.35

    # Strict confirmation filters
    min_directional_efficiency: float = 0.45
    max_retracement_volume_ratio: float = 0.80
    min_persistence: float = 0.60
    require_vwap_confirmation: bool = True

    # Ranking weights: sum must equal 1.0
    w_impulse: float = 0.18
    w_retracement: float = 0.20
    w_relative_strength: float = 0.14
    w_sector_strength: float = 0.08
    w_volume: float = 0.10
    w_vwap: float = 0.10
    w_structure: float = 0.10
    w_volatility: float = 0.10

    # Risk engineering
    atr_period: int = 10
    stop_atr_buffer: float = 0.35
    minimum_rr: float = 2.0
    minimum_target_atr: float = 1.50
    minimum_risk_pct: float = 0.15
    maximum_risk_pct: float = 3.0

    # Tier 2 relaxation
    fallback_retrace_min: float = 0.30
    fallback_retrace_max: float = 0.75
    fallback_reclaim_min: float = 0.35
    fallback_volume_ratio_max: float = 1.20
    fallback_efficiency_min: float = 0.30
    fallback_persistence_min: float = 0.45
    fallback_extension_atr_max: float = 2.50
    fallback_vwap_tolerance_atr: float = 0.25

    def validate(self) -> None:
        weights = (
            self.w_impulse,
            self.w_retracement,
            self.w_relative_strength,
            self.w_sector_strength,
            self.w_volume,
            self.w_vwap,
            self.w_structure,
            self.w_volatility,
        )
        if abs(sum(weights) - 1.0) > 1e-9:
            raise ValueError(f"ranking weights must sum to 1.0, got {sum(weights):.12f}")
        if self.universe_size != self.shard_count * self.shard_size:
            raise ValueError("universe_size must equal shard_count * shard_size")
        if self.min_completed_1m < 5:
            raise ValueError("min_completed_1m must be >= 5")
        if self.min_retracement_depth >= self.max_retracement_depth:
            raise ValueError("invalid strict retracement interval")
        if self.fallback_retrace_min >= self.fallback_retrace_max:
            raise ValueError("invalid fallback retracement interval")
        if self.minimum_rr < 1.0:
            raise ValueError("minimum_rr must be >= 1")
        if self.atr_period < 2:
            raise ValueError("atr_period must be >= 2")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be > 0")
