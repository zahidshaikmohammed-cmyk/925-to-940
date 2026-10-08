"""The live gate (spec sections 36, 39-40). Real orders are refused unless a validation
report exists for the EXACT config hash in use and every acceptance criterion passes.

The thresholds are written down before any data was examined. They are deliberately
hard to meet with the ~170 SENSEX weekly expiries that exist since May 2023: if the
history cannot clear them, the honest conclusion is "no demonstrated edge" and the
engine stays in paper mode.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

STAGES = ("BACKTEST", "WALK_FORWARD", "LIVE_DATA_SIM", "PAPER", "TINY_LIVE", "PRODUCTION")

# criteria to LEAVE each stage (i.e. to be allowed into the next one)
CRITERIA = {
    "WALK_FORWARD": {
        "min_oos_trades": 60,
        "min_oos_expectancy_r": 0.15,         # after costs, concatenated out-of-sample folds
        "max_prob_mean_le_zero": 0.10,         # bootstrap
        "min_profit_factor": 1.20,
        "min_edge_over_random_r": 0.15,
        "max_perm_p_value": 0.10,
        "min_expectancy_ex_top5_r": 0.0,       # not carried by a handful of outliers
        "min_expectancy_costs_1_5x_r": 0.0,
        "min_expectancy_slippage_2x_r": 0.0,
        "max_drawdown_r": 12.0,
        "min_positive_neighbour_share": 0.70,  # parameter-neighbourhood stability
        "min_positive_folds_share": 0.60,
    },
    "PAPER": {
        "min_expiry_days": 12,
        "min_trades": 15,
        "max_slippage_vs_model": 1.5,          # realised / modelled
        "max_signal_mismatch_vs_replay": 0,    # live decisions must equal offline replay
        "max_high_severity_incidents": 0,
        "min_expectancy_r": -0.25,             # paper is for plumbing, not for proving edge
    },
    "TINY_LIVE": {
        "min_trades": 30,
        "min_expectancy_r": 0.0,
        "max_slippage_vs_model": 1.5,
        "max_high_severity_incidents": 0,
        "max_drawdown_r": 8.0,
    },
}


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    stage: str
    failures: tuple[str, ...]


def _cmp(name: str, limit: float, value) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return f"{name}: missing"
    if name.startswith("min_") and value < limit:
        return f"{name}: {value} < {limit}"
    if name.startswith("max_") and value > limit:
        return f"{name}: {value} > {limit}"
    return None


def check_stage(stage: str, observed: dict) -> list[str]:
    crit = CRITERIA[stage]
    return [f for f in (_cmp(k, v, observed.get(k)) for k, v in crit.items()) if f]


def live_allowed(report_path: Path, config_hash: str, requested_stage: str) -> GateResult:
    """requested_stage is TINY_LIVE or PRODUCTION. Every earlier stage must have passed
    under the same config hash."""
    p = Path(report_path)
    if not p.exists():
        return GateResult(False, requested_stage, ("no validation report",))
    try:
        rep = json.loads(p.read_text())
    except (OSError, ValueError):
        return GateResult(False, requested_stage, ("unreadable validation report",))
    if rep.get("config_hash") != config_hash:
        return GateResult(False, requested_stage, (f"report is for config {rep.get('config_hash')}, engine runs {config_hash}",))
    needed = ["WALK_FORWARD", "PAPER"] + (["TINY_LIVE"] if requested_stage == "PRODUCTION" else [])
    failures: list[str] = []
    for st in needed:
        failures += [f"{st}: {f}" for f in check_stage(st, rep.get("stages", {}).get(st, {}))]
    return GateResult(not failures, requested_stage, tuple(failures))
