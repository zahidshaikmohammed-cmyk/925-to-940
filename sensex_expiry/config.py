"""Locked parameters of the SENSEX expiry engine.

Every number here is part of the constitution (SENSEX_EXPIRY_SPEC.md section 39).
Changing any of them changes `config_hash()`, which invalidates every validation
report produced under the old hash: the live gate (validation_gate.py) then refuses
to trade until the change has gone back through backtest, walk-forward, out-of-sample
and paper stages.

Status of the values: they are PRE-REGISTERED RESEARCH DEFAULTS, chosen as round,
structurally motivated numbers before any data was examined. None of them is a
backtest result.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import time


@dataclass(frozen=True)
class SessionConfig:
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    no_entry_before: time = time(9, 35)      # OR15 complete + 5 minutes of settling
    s2_last_entry: time = time(12, 0)
    s3_window_start: time = time(13, 0)
    s3_window_end: time = time(14, 30)
    last_entry: time = time(14, 45)
    hard_flat: time = time(15, 10)          # every position flat; never held into settlement
    candle_close_grace_s: float = 2.0       # a minute candle closes this long after its boundary


@dataclass(frozen=True)
class DataQualityConfig:
    max_underlying_age_ms: int = 3_000
    max_option_quote_age_ms: int = 5_000
    max_chain_age_ms: int = 90_000
    max_tick_jump_atr: float = 6.0          # single-tick jump above this many 1m ATRs is quarantined
    max_missing_minutes: int = 2            # more missing closed candles today than this -> NO_TRADE


@dataclass(frozen=True)
class FeatureConfig:
    atr_period: int = 14
    or_minutes: int = 15
    swing_k: int = 3                        # swing confirmed k bars after the pivot (causal)
    er_window: int = 30
    compression_window: int = 20
    compression_max: float = 0.60
    expansion_tr_atr: float = 2.0
    displacement_range_atr: float = 1.5
    displacement_body_frac: float = 0.60


@dataclass(frozen=True)
class SetupConfig:
    enabled: tuple[str, ...] = ("S1_SWEEP_RECLAIM", "S2_ORB_ACCEPT", "S3_LATE_COMPRESSION")
    # S1 sweep and reclaim
    sweep_min_pen_atr: float = 0.10
    sweep_max_pen_atr: float = 1.50
    sweep_reclaim_bars: int = 5
    sweep_reclaim_buffer_atr: float = 0.05
    sweep_min_rejection_atr: float = 0.75
    sweep_trigger_bars: int = 3
    level_min_age_bars: int = 10
    # S2 opening-range acceptance + retest
    orb_accept_buffer_atr: float = 0.10
    orb_accept_closes: int = 2
    orb_retest_bars: int = 10
    orb_retest_tol_atr: float = 0.25
    # S3 late compression -> expansion
    s3_break_buffer_atr: float = 0.10
    s3_stop: str = "MID"                    # MID or OPPOSITE (pre-registered alternatives)
    # common
    stop_buffer_atr: float = 0.25
    min_room_r: float = 1.5                 # distance to next opposing level / stop distance


@dataclass(frozen=True)
class OptionConfig:
    strike_step: int = 100
    moneyness: int = 0                      # 0 = ATM, +1 = one strike ITM, -1 = one strike OTM
    min_premium: float = 20.0
    max_spread_pct: float = 0.02
    min_top5_depth_mult: float = 5.0        # top-5 opposite-side qty >= this x order qty
    default_atm_delta: float = 0.5
    stop_delta_mult: float = 1.2
    min_stop_frac: float = 0.10             # premium stop at least 10% below entry
    max_stop_frac: float = 0.50             # never risk more than 50% of premium per unit
    max_outlay_r: float = 3.0               # premium outlay <= 3 x 1R: total-failure loss is bounded


@dataclass(frozen=True)
class ExitConfig:
    policy: str = "TRAIL"                   # TRAIL | FIXED_2R | FIXED_3R | TIME_ONLY
    breakeven_at_r: float = 1.0
    trail_atr_mult: float = 2.0
    trail_after_r: float = 1.0
    time_stop_bars: int = 15
    time_stop_min_r: float = 0.5
    max_hold_bars: int = 60


@dataclass(frozen=True)
class RiskConfig:
    capital: float = 500_000.0
    risk_per_trade_pct: float = 0.005
    max_trades_per_day: int = 2
    max_daily_loss_r: float = 2.0
    max_consecutive_losses: int = 2
    max_open_positions: int = 1
    max_lots: int = 10
    max_outlay_pct: float = 0.10
    cooldown_minutes: int = 5
    slippage_ticks: int = 2
    tick_size: float = 0.05


@dataclass(frozen=True)
class CostConfig:
    """Rates as researched on 2026-10-08 (see spec section 31). VERIFY before live."""
    brokerage_per_order: float = 20.0       # Dhan F&O flat per executed order (verify on contract note)
    stt_sell_premium: float = 0.0015        # 0.15% of sell-side premium from 2026-04-01 (Budget 2026)
    exchange_txn_premium: float = 0.000325  # BSE SENSEX options Rs 3,250/crore premium (2024 rate; verify)
    sebi_fee: float = 0.000001              # Rs 10/crore
    stamp_buy: float = 0.00003              # 0.003% buy side
    gst: float = 0.18                       # on brokerage + exchange + SEBI fees


@dataclass(frozen=True)
class EngineConfig:
    version: str = "1.0.0"
    session: SessionConfig = field(default_factory=SessionConfig)
    data: DataQualityConfig = field(default_factory=DataQualityConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    setups: SetupConfig = field(default_factory=SetupConfig)
    options: OptionConfig = field(default_factory=OptionConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    # ablation switches: every one is True in the locked system
    use_regime_filter: bool = True
    use_room_filter: bool = True
    use_time_filter: bool = True
    use_spread_filter: bool = True

    def validate(self) -> None:
        r, o, s = self.risk, self.options, self.session
        problems = []
        if not 0 < r.risk_per_trade_pct <= 0.01:
            problems.append("risk_per_trade_pct must be in (0, 1%]")
        if r.max_open_positions != 1:
            problems.append("v1 allows exactly one open position")
        if not 0 < o.min_stop_frac < o.max_stop_frac <= 0.5:
            problems.append("premium stop fractions must satisfy 0 < min < max <= 0.5")
        if not 1.0 <= o.max_outlay_r <= 1.0 / o.min_stop_frac:
            problems.append("max_outlay_r must be in [1, 1/min_stop_frac]")
        if not s.no_entry_before < s.last_entry < s.hard_flat < s.market_close:
            problems.append("session times must be ordered open < entries < hard_flat < close")
        if self.exits.policy not in ("TRAIL", "FIXED_2R", "FIXED_3R", "TIME_ONLY"):
            problems.append(f"unknown exit policy {self.exits.policy}")
        if self.setups.s3_stop not in ("MID", "OPPOSITE"):
            problems.append("s3_stop must be MID or OPPOSITE")
        if problems:
            raise ValueError("; ".join(problems))

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))

    def config_hash(self) -> str:
        """Hash of the strategy-defining parameters (capital excluded: sizing scales, rules do not)."""
        d = self.to_dict()
        d["risk"].pop("capital", None)
        blob = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def ablate(cfg: EngineConfig, name: str) -> EngineConfig:
    """Return the config with one component removed (spec section 29)."""
    if name == "no_regime":
        return replace(cfg, use_regime_filter=False)
    if name == "no_room":
        return replace(cfg, use_room_filter=False)
    if name == "no_time":
        return replace(cfg, use_time_filter=False)
    if name == "no_spread":
        return replace(cfg, use_spread_filter=False)
    if name.startswith("only_"):
        return replace(cfg, setups=replace(cfg.setups, enabled=(name[5:],)))
    if name.startswith("exit_"):
        return replace(cfg, exits=replace(cfg.exits, policy=name[5:]))
    raise ValueError(f"unknown ablation {name}")


ABLATIONS = ("no_regime", "no_room", "no_time", "no_spread",
             "only_S1_SWEEP_RECLAIM", "only_S2_ORB_ACCEPT", "only_S3_LATE_COMPRESSION",
             "exit_FIXED_2R", "exit_FIXED_3R", "exit_TIME_ONLY")


def field_names(dc) -> list[str]:
    return [f.name for f in fields(dc)]
