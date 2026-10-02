"""Immutable, fingerprinted 09:45 decision.

DecisionSnapshot is a frozen dataclass whose nested mappings are read-only
(MappingProxyType) and nested sequences are tuples, so no field can be changed after
publication. `decision_fingerprint` is the sha256 of the canonical JSON of every
decision-relevant field (it excludes only the wall-clock `published_at`), so the same
input + model always produces the same fingerprint.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from types import MappingProxyType


def deep_freeze(x):
    if isinstance(x, dict):
        return MappingProxyType({k: deep_freeze(v) for k, v in x.items()})
    if isinstance(x, (list, tuple)):
        return tuple(deep_freeze(v) for v in x)
    if isinstance(x, float) and x != x:
        return None
    return x


def thaw(x):
    if isinstance(x, MappingProxyType):
        return {k: thaw(v) for k, v in x.items()}
    if isinstance(x, tuple):
        return [thaw(v) for v in x]
    return x


def canonical_json(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


@dataclass(frozen=True)
class DecisionSnapshot:
    decision_id: str
    decision_date: str
    cutoff: str                       # information-set cutoff (ISO, IST)
    published_at: str                 # wall clock (excluded from fingerprint)
    mode: str                         # live | replay | backtest
    universe_size: int
    eligible_count: int
    excluded_count: int
    exclusion_reasons: object         # reason -> count
    selected_symbol: str
    direction: str
    selection_score: float
    raw_score: float
    reference_price: float
    estimated_probability: float
    probability_status: str
    probability_definition: str
    expected_return_pct: float | None
    expected_return_status: str
    expected_horizon_min: int
    horizon_status: str
    explanation: tuple
    feature_snapshot: object
    contributions: tuple
    ranking_snapshot: tuple
    market_context: object
    data_quality: object
    model_name: str
    model_version: str
    weights_hash: str
    input_fingerprint: str
    decision_fingerprint: str

    def to_dict(self) -> dict:
        return {f.name: thaw(getattr(self, f.name)) for f in fields(self)}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


FINGERPRINT_EXCLUDE = {"published_at", "decision_id", "decision_fingerprint", "mode"}


def build_snapshot(**kw) -> DecisionSnapshot:
    body = {k: v for k, v in kw.items() if k not in FINGERPRINT_EXCLUDE}
    fp = hashlib.sha256(canonical_json(thaw(deep_freeze(body))).encode()).hexdigest()
    kw["decision_fingerprint"] = fp
    kw["decision_id"] = f"{kw['decision_date']}-{kw['mode']}-{fp[:12]}"
    return DecisionSnapshot(**{k: deep_freeze(v) for k, v in kw.items()})


def snapshot_from_dict(d: dict) -> DecisionSnapshot:
    return DecisionSnapshot(**{f.name: deep_freeze(d[f.name]) for f in fields(DecisionSnapshot)})


def verify_fingerprint(snap: DecisionSnapshot) -> bool:
    body = {k: v for k, v in snap.to_dict().items() if k not in FINGERPRINT_EXCLUDE}
    return hashlib.sha256(canonical_json(body).encode()).hexdigest() == snap.decision_fingerprint
