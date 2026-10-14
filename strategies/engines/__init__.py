"""The ten strategy engines, in the mission's order. ``build_engines()`` returns fresh
instances (engines that keep per-scan cross-sectional state hold it on the instance)."""
from __future__ import annotations

from .exhaustion_reversion import ExhaustionMeanReversionEngine
from .failed_breakout import FailedBreakoutBreakdownEngine
from .liquidity_sweep_reclaim import LiquiditySweepReclaimEngine
from .order_flow_imbalance import OrderFlowImbalanceEngine
from .regime_orb import RegimeFilteredORBEngine
from .relative_strength import RelativeStrengthWeaknessEngine
from .structural_retest import StructuralBreakRetestEngine
from .trend_pullback import TrendPullbackContinuationEngine
from .volatility_compression import VolatilityCompressionExpansionEngine
from .vwap_reclaim import VWAPReclaimRejectionEngine

ENGINE_CLASSES = (
    OrderFlowImbalanceEngine,
    LiquiditySweepReclaimEngine,
    FailedBreakoutBreakdownEngine,
    TrendPullbackContinuationEngine,
    RelativeStrengthWeaknessEngine,
    VolatilityCompressionExpansionEngine,
    RegimeFilteredORBEngine,
    VWAPReclaimRejectionEngine,
    ExhaustionMeanReversionEngine,
    StructuralBreakRetestEngine,
)
ENGINE_NAMES = tuple(c.NAME for c in ENGINE_CLASSES)


def build_engines(names: list[str] | None = None) -> list:
    if names:
        unknown = sorted(set(names) - set(ENGINE_NAMES))
        if unknown:
            raise ValueError(f"unknown engine(s): {', '.join(unknown)}")
    return [c() for c in ENGINE_CLASSES if not names or c.NAME in names]
