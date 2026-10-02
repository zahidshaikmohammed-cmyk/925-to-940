"""Directional ranking model.

Any model implementing `RankingModel.rank(table) -> list[Candidate]` can replace the
v1 linear model (logistic regression, gradient boosting, ensembles...) without
touching data, features, snapshot, storage or backtest code.

v1 = LinearEvidenceModel:
    z_i      = cross-sectional robust z-score of signed feature i among eligible
               stocks: (x - median) / (1.4826 * MAD), clipped to +/- z_clip.
               Missing value -> contributes 0 and is reported.
    UP raw   = sum_i w_i * z_i            DOWN raw = sum_i w_i * (-z_i)
    activity = floor + (1-floor) * weighted mean of activity percentiles
    raw      = evidence * activity - penalties (spike overextension vs day range, gap exhaustion,
               short-term reversal against the direction)
    score    = 100 / (1 + exp(-(raw - center) / scale))      (0..100, NOT a probability)
Selection = argmax over every eligible stock x {UP, DOWN}; ties broken by symbol.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import exp
from statistics import median
from typing import Protocol

from .selector_features import FeatureTable


@dataclass(frozen=True)
class Contribution:
    feature: str
    value: float | None
    z: float | None
    weight: float
    contribution: float


@dataclass(frozen=True)
class Candidate:
    symbol: str
    direction: str            # UP | DOWN
    raw: float
    score: float              # 0..100 ranking score
    evidence: float
    activity: float
    penalty: float
    contributions: tuple[Contribution, ...]
    penalties: tuple[tuple[str, float], ...]


class RankingModel(Protocol):
    name: str
    version: str

    def rank(self, table: FeatureTable) -> list[Candidate]: ...


def robust_z(values: dict, clip: float) -> dict:
    present = [v for v in values.values() if v is not None]
    if len(present) < 3:
        return {k: None for k in values}
    m = median(present)
    mad = median(abs(v - m) for v in present) * 1.4826
    if mad == 0:
        sd = (sum((v - m) ** 2 for v in present) / len(present)) ** 0.5
        mad = sd if sd > 0 else None
    out = {}
    for k, v in values.items():
        if v is None or mad is None:
            out[k] = None if v is None else 0.0
        else:
            out[k] = max(-clip, min(clip, (v - m) / mad))
    return out


class LinearEvidenceModel:
    name = "linear-evidence"
    version = "1"

    def __init__(self, weights: dict):
        self.w = weights

    def rank(self, table: FeatureTable) -> list[Candidate]:
        elig = table.eligible
        feats = table.features
        clip = float(self.w.get("z_clip", 3.0))
        signed = self.w["signed"]
        zs = {name: robust_z({s: feats[s].get(name) for s in elig}, clip) for name in signed}
        act_cfg = self.w["activity"]
        pen = self.w["penalties"]
        mapping = self.w["score_mapping"]
        out: list[Candidate] = []
        for sym in elig:
            f = feats[sym]
            parts = []
            for name, weight in signed.items():
                z = zs[name][sym]
                parts.append((name, f.get(name), z, weight))
            act_w = act_cfg["features"]
            vals = [(f.get(k), w) for k, w in act_w.items() if f.get(k) is not None]
            act = (sum(v * w for v, w in vals) / sum(w for _, w in vals)) if vals else 0.5
            activity = act_cfg["floor"] + (1 - act_cfg["floor"]) * act
            for direction, d in (("UP", 1), ("DOWN", -1)):
                contribs = tuple(Contribution(n, v, z, w, (d * z * w) if z is not None else 0.0)
                                 for n, v, z, w in parts)
                evidence = sum(c.contribution for c in contribs)
                penalties = []
                ext = f.get("extension_vs_range")
                if ext is not None:
                    over = d * ext - pen["overextension_range_threshold"]
                    if over > 0:      # price far from EMA20 relative to the whole day's range = spike
                        penalties.append(("overextended_spike_from_ema20", pen["overextension_weight"] * over))
                gap = f.get("gap_pct")
                if gap is not None and d * gap > pen["gap_exhaustion_pct"]:
                    penalties.append(("gap_exhaustion", pen["gap_exhaustion_weight"]
                                      * (d * gap - pen["gap_exhaustion_pct"])))
                r5 = f.get("return_5m")
                if r5 is not None and d * r5 < 0 and f.get("reversal_flag"):
                    penalties.append(("short_term_reversal_against", pen["short_term_reversal_weight"]))
                penalty = sum(p for _, p in penalties)
                raw = evidence * activity - penalty
                score = 100.0 / (1.0 + exp(-(raw - mapping["center"]) / mapping["scale"]))
                out.append(Candidate(sym, direction, raw, score, evidence, activity, penalty,
                                     contribs, tuple(penalties)))
        out.sort(key=lambda c: (-c.raw, c.symbol, c.direction))
        return out


def select(ranked: list[Candidate]) -> Candidate:
    if not ranked:
        raise RuntimeError("no eligible stock could be scored -- refusing to fabricate a selection")
    return ranked[0]
