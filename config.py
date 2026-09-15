from dataclasses import dataclass
@dataclass(frozen=True)
class StrategyConfig:
    session_start:str='09:15'; signal_time:str='09:30'; entry_time:str='09:31'
    max_ltp_age_seconds:float=10.0; min_completed_1m:int=15; require_full_09_15_to_09_29_grid:bool=True
    min_impulse_pct:float=0.35; min_impulse_bars:int=3; min_retracement_bars:int=3; min_retracement_depth:float=0.38; max_retracement_depth:float=0.70; min_reclaim_ratio:float=0.50
    max_gap_pct:float=3.0; max_gap_z:float=3.0; max_impulse_atr:float=3.5; max_extension_from_vwap_atr:float=2.0; late_extreme_bar:int=12; exhaustion_reclaim_distance_atr:float=0.35
    min_directional_efficiency:float=0.45; max_retracement_volume_ratio:float=0.80; min_persistence:float=0.60
    w_impulse:float=0.18; w_retracement:float=0.24; w_relative_strength:float=0.12; w_sector_strength:float=0.06; w_volume:float=0.10; w_vwap:float=0.10; w_structure:float=0.10; w_volatility:float=0.10
    atr_period:int=10; stop_atr_buffer:float=0.35; minimum_rr:float=2.0; minimum_target_atr:float=1.50
    def validate(self)->None:
        weights=(self.w_impulse,self.w_retracement,self.w_relative_strength,self.w_sector_strength,self.w_volume,self.w_vwap,self.w_structure,self.w_volatility)
        if abs(sum(weights)-1)>1e-9:raise ValueError('ranking weights must sum to exactly 1.0')
        if self.min_retracement_depth>=self.max_retracement_depth:raise ValueError('invalid retracement interval')
        if self.minimum_rr<1:raise ValueError('minimum_rr must be >= 1')
