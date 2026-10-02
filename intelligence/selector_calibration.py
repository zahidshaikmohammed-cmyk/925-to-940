"""Walk-forward probability / expected-return / horizon estimates.

Built ONLY from (decision, outcome) pairs dated strictly before the session being
decided. Until `min_calibration_obs` such pairs exist, the probability is a clearly
labelled UNCALIBRATED heuristic of the raw score -- never presented as a frequency.

Target:  P(signed forward return at horizon H > prob_threshold_pct | 09:45 state)
Method:  score bins [0,50), [50,70), [70,85), [85,100]; Laplace-smoothed hit rate in the
         decision's bin when the bin has >= min_bin_obs, else the pooled rate.
Horizon: the horizon with the best mean signed forward return in history (needs data),
         else the configured default, labelled PRIOR.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import tanh

from .selector_config import SelectorConfig

BINS = ((0.0, 50.0), (50.0, 70.0), (70.0, 85.0), (85.0, 100.01))


@dataclass(frozen=True)
class Estimate:
    probability: float
    probability_status: str
    expected_return_pct: float | None
    expected_return_status: str
    horizon_min: int
    horizon_status: str
    n_history: int


def _bin(score: float) -> int:
    for i, (lo, hi) in enumerate(BINS):
        if lo <= score < hi:
            return i
    return len(BINS) - 1


class Calibrator:
    def __init__(self, history: list[tuple[dict, dict]], cfg: SelectorConfig):
        """history: (decision_dict, {horizon: outcome_dict}) all dated before the target day."""
        self.cfg = cfg
        self.rows = []
        for d, outs in history:
            for h, o in outs.items():
                if o.get("complete") and o.get("forward_return_pct") is not None:
                    self.rows.append((int(h), float(d["selection_score"]), float(o["forward_return_pct"])))

    def _rows(self, h: int):
        return [(s, r) for hh, s, r in self.rows if hh == h]

    def best_horizon(self) -> tuple[int, str]:
        means = {}
        for h in self.cfg.horizons_min:
            rows = self._rows(h)
            if len(rows) >= self.cfg.min_calibration_obs:
                means[h] = sum(r for _, r in rows) / len(rows)
        if not means:
            return self.cfg.default_horizon_min, "PRIOR (not yet empirical)"
        h = max(sorted(means), key=lambda k: means[k])
        return h, f"EMPIRICAL_WALK_FORWARD (best mean return over {len(self._rows(h))} decisions)"

    def estimate(self, score: float, raw: float) -> Estimate:
        h, h_status = self.best_horizon()
        rows = self._rows(h)
        n = len(rows)
        thr = self.cfg.prob_threshold_pct
        if n >= self.cfg.min_calibration_obs:
            in_bin = [r for s, r in rows if _bin(s) == _bin(score)]
            pool = in_bin if len(in_bin) >= self.cfg.min_bin_obs else [r for _, r in rows]
            hits = sum(1 for r in pool if r > thr)
            p = (hits + 1) / (len(pool) + 2)
            exp_ret = sum(pool) / len(pool)
            where = "score bin" if pool is in_bin else "all scores"
            return Estimate(p, f"EMPIRICAL_WALK_FORWARD (n={len(pool)}, {where})", exp_ret,
                            f"EMPIRICAL_WALK_FORWARD (n={len(pool)})", h, h_status, n)
        p = 0.5 + 0.15 * tanh(raw / 2.0)
        return Estimate(p, f"UNCALIBRATED_HEURISTIC (only {n} out-of-sample outcomes; need "
                           f"{self.cfg.min_calibration_obs})", None,
                        "UNAVAILABLE (needs walk-forward history)", h, h_status, n)
