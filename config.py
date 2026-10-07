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

    # Feed integrity for the canonical PSYGRID 1-minute OHLCV endpoint.
    universe_size: int = 450
    shard_count: int = 10
    shard_size: int = 45
    min_completed_1m: int = 5
    require_full_09_15_to_09_29_grid: bool = False
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
    # Previous close is optional metadata and is not required by the signal.
    max_gap_pct: float = 3.0
    max_gap_z: float = 3.0
    max_impulse_atr: float = 3.50
    max_extension_from_vwap_atr: float = 2.00
    vwap_sweet_spot_atr: float = 1.00  # VWAP score peaks here and decays toward the extension cap
    late_extreme_bar: int = 12
    exhaustion_reclaim_distance_atr: float = 0.35

    # Strict confirmation filters
    # Minimum relative strength vs the market in the trade's direction (%), all tiers.
    min_rs_market: float = 0.0
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
    fallback_extension_atr_max: float = 2.00
    fallback_vwap_tolerance_atr: float = 0.25

    # Precision screen (run_engine.precision_screen), applied before scoring.
    # A stock is skipped when its newest completed candle is older than this
    # (stale/illiquid feed), or when its typical minute turnover is too thin
    # for a clean fill. 0 disables either check.
    max_candle_age_minutes: int = 3
    # ₹2 lakh/min over a 30-minute median: a 10-minute median at ₹5 lakh flipped
    # liquid mid-caps (360ONE, 2026-10-07) in and out of the scan at midday.
    min_median_turnover_rupees: float = 200_000.0
    turnover_lookback_bars: int = 30
    # Warn when the order is bigger than this many minutes of the stock's median turnover.
    max_order_minutes_of_turnover: float = 1.0
    # Entry trigger: the signal is armed at the last completed candle's
    # low (SHORT) / high (LONG) and expires after this many candles.
    trigger_valid_candles: int = 2
    # Exit timing (run_engine.exit_timing), measured from the stock's own pace:
    # the fastest sustained directional run (5-15 bars) in the last 36 bars,
    # scaled by continuation_pace_ratio because a second leg is usually slower.
    pace_lookback_bars: int = 36
    continuation_pace_ratio: float = 0.5
    checkpoint_slack: float = 1.5   # x the expected minutes to reach +0.5R
    time_stop_multiple: float = 2.0  # x the expected minutes to reach the target
    intraday_exit_time: str = "15:15"
    # Precision layer (precision.py). Starting values; grade_signals.py measures them.
    breadth_long_only: float = 0.60    # >= this share of stocks above VWAP -> longs only
    breadth_short_only: float = 0.40   # <= this share -> shorts only
    persistence_window_bars: int = 60
    vwap_cross_window_bars: int = 30
    structure_pairs: int = 6           # 5-minute bar pairs checked for HH/HL (LL/LH)
    opening_range_end: str = "09:30"
    climax_volume_multiple: float = 4.0
    climax_range_atr: float = 2.0
    rejection_wick_ratio: float = 0.6
    failed_break_count: int = 3
    late_day_after: str = "14:00"
    late_day_extended_pct: float = 6.0
    penalty_climax: float = 15.0
    penalty_rejection: float = 10.0
    penalty_failed_breaks: float = 10.0
    penalty_late_extended: float = 15.0
    p_vwap_hold: float = 0.25
    p_vwap_crosses: float = 0.10
    p_structure: float = 0.20
    p_volume: float = 0.15
    p_opening_range: float = 0.15
    p_relative_strength: float = 0.15
    min_trend_persistence: float = 60.0
    # No new entry in a stock that has already moved this much today in the trade's
    # direction (NELCAST +17% offered as a LONG at 14:01): the easy part is gone.
    max_day_move_pct: float = 7.0
    neutral_extra_persistence: float = 10.0
    lunch_extra_persistence: float = 10.0
    no_entry_before: str = "09:20"
    no_entry_after: str = "14:45"
    lunch_start: str = "12:00"
    lunch_end: str = "13:30"
    conviction_setup_weight: float = 0.4  # conviction = 0.4 x setup score + 0.6 x persistence
    # A Tier 1/2 pick below this score is reported LOW_CONFIDENCE_WEAK, not SIGNAL_READY.
    min_signal_score: float = 55.0

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
        if not 0 < self.vwap_sweet_spot_atr < self.max_extension_from_vwap_atr - 0.25:
            raise ValueError("vwap_sweet_spot_atr must sit below max_extension_from_vwap_atr - 0.25")
        persistence_weights = (
            self.p_vwap_hold, self.p_vwap_crosses, self.p_structure,
            self.p_volume, self.p_opening_range, self.p_relative_strength,
        )
        if abs(sum(persistence_weights) - 1.0) > 1e-9:
            raise ValueError("persistence weights must sum to 1.0")
        if not 0.0 <= self.breadth_short_only < self.breadth_long_only <= 1.0:
            raise ValueError("invalid breadth thresholds")
        if self.max_candle_age_minutes < 0 or self.min_median_turnover_rupees < 0:
            raise ValueError("precision screen thresholds must be >= 0")
        if self.http_timeout_seconds <= 0:
            raise ValueError("http_timeout_seconds must be > 0")
