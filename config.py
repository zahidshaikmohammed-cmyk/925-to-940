from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    evaluation_start: str = "09:25"
    lock_time: str = "09:40"
    top_n: int = 3
    opening_move_min_pct: float = 0.35
    relative_strength_min_pct: float = 0.20
    sector_strength_min_pct: float = 0.10
    max_vwap_extension_atr: float = 1.80
    max_opening_extreme_extension_atr: float = 1.25
    min_persistence: float = 0.60
    min_observations: int = 3
    min_median_1m_traded_value: float = 2_500_000.0
    score_momentum: float = 0.22
    score_relative_strength: float = 0.22
    score_sector_strength: float = 0.16
    score_relative_volume: float = 0.14
    score_vwap: float = 0.10
    score_structure: float = 0.08
    score_htf: float = 0.05
    score_persistence: float = 0.03

    def validate(self) -> None:
        weights = (
            self.score_momentum,
            self.score_relative_strength,
            self.score_sector_strength,
            self.score_relative_volume,
            self.score_vwap,
            self.score_structure,
            self.score_htf,
            self.score_persistence,
        )
        if abs(sum(weights) - 1.0) > 1e-9:
            raise ValueError("ranking weights must sum to exactly 1.0")
        if self.top_n != 3:
            raise ValueError("strategy specification locks Top 3")
        if self.min_observations < 3:
            raise ValueError("persistence requires at least 3 observations")
