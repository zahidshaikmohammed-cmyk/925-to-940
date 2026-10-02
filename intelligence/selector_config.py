"""Configuration for the 09:45 selector. Every number lives here or in the weights file."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

MODEL_NAME = "psygrid-945-linear-evidence"
MODEL_VERSION = "1.0.0"
DEFAULT_WEIGHTS_PATH = Path(__file__).with_name("selector_weights_v1.json")


@dataclass(frozen=True)
class SelectorConfig:
    session_start: time = time(9, 15)
    cutoff: time = time(9, 45)                # candles with timestamp < 09:45 (09:15..09:44)
    opening_range_end: time = time(9, 30)     # opening range = 09:15..09:29
    expected_bars: int = 30
    min_bars: int = 20                        # fewer usable bars -> ineligible
    max_stale_minutes: int = 3                # last completed bar must end within 3 min of cutoff
    min_liquidity_percentile: float = 0.25    # bottom quartile by turnover is ineligible
    sector_min_peers: int = 3
    beta_min_pairs: int = 15
    beta_shrink: float = 0.5                  # beta = shrink*raw + (1-shrink)*1.0
    baseline_lookback_days: int = 10
    baseline_min_days: int = 3
    horizons_min: tuple[int, ...] = (5, 15, 30)
    default_horizon_min: int = 15
    move_threshold_pct: float = 0.25          # "favourable/adverse move" for time-to-move
    prob_threshold_pct: float = 0.05          # probability target: P(signed return > 0.05%)
    min_calibration_obs: int = 60             # out-of-sample decisions before "calibrated"
    min_bin_obs: int = 10
    ranking_snapshot_size: int = 25
    db_path: str = "data/psygrid_945.sqlite"
    sessions_dir: str = "data/sessions"


def load_weights(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_WEIGHTS_PATH
    weights = json.loads(p.read_text(encoding="utf-8"))
    signed = weights["signed"]
    total = sum(signed.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"signed feature weights must sum to 1.0, got {total:.6f} ({p})")
    return weights


def weights_hash(weights: dict) -> str:
    blob = json.dumps(weights, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]
