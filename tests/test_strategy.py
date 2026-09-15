from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from strategy import (
    BenchmarkSnapshot,
    CandidateSnapshot,
    Candle,
    CandidateState,
    HTFContext,
    evaluate,
    rank_and_lock,
)

IST = ZoneInfo("Asia/Kolkata")


def candles_up():
    start = datetime(2026, 9, 15, 9, 15, tzinfo=IST)
    out = []
    closes = [500.5, 501.0, 501.4, 501.8, 502.2, 502.8, 503.3, 504.0, 504.5, 505.0]
    for i, close in enumerate(closes):
        op = 500.0 if i == 0 else closes[i - 1]
        out.append(Candle(start + timedelta(minutes=i), op, close + 0.2, op - 0.2, close, 10000))
    return tuple(out)


def snapshot(symbol="ABC", htf=(1, 1), nifty=0.40):
    cs = candles_up()
    return CandidateSnapshot(
        symbol=symbol,
        candles=cs,
        benchmark=BenchmarkSnapshot(100.0, 100.0 * (1 + nifty / 100.0)),
        sector_returns_pct=(0.55, 0.60, 0.70, 0.65),
        htf=HTFContext(*htf),
        current_price=505.0,
        current_vwap=504.7,
        relative_volume=1.5,
    )


def test_candidate_is_qualified_when_all_gates_pass():
    state = CandidateState()
    state.observations = [1, 1, 1]
    d = evaluate(snapshot(), state)
    assert d.qualified
    assert d.direction == "LONG"
    assert 0 <= d.score <= 100


def test_higher_timeframe_misalignment_is_hard_gate():
    state = CandidateState()
    state.observations = [1, 1, 1]
    d = evaluate(snapshot(htf=(1, -1)), state)
    assert not d.qualified
    assert "higher_timeframe_misalignment" in d.rejection_reasons


def test_extension_is_hard_gate():
    state = CandidateState()
    state.observations = [1, 1, 1]
    s = snapshot()
    s = CandidateSnapshot(**{**s.__dict__, "current_price": 510.0, "current_vwap": 500.0})
    d = evaluate(s, state)
    assert not d.qualified
    assert "extended_from_vwap" in d.rejection_reasons


def test_persistence_requires_three_observations():
    state = CandidateState()
    state.observations = [1, 1]
    d = evaluate(snapshot(), state)
    assert not d.qualified
    assert "less_than_3_observations" in d.rejection_reasons


def test_final_lock_requires_0940_or_later():
    d = DecisionFixture.qualified("ABC", 90)
    with pytest.raises(ValueError):
        rank_and_lock([d], datetime(2026, 9, 15, 9, 39, tzinfo=IST))


def test_final_lock_returns_at_most_three_and_is_deterministic():
    decisions = [DecisionFixture.qualified(x, score) for x, score in [("A", 70), ("B", 90), ("C", 80), ("D", 95)]]
    locked = rank_and_lock(decisions, datetime(2026, 9, 15, 9, 40, tzinfo=IST))
    assert [x.symbol for x in locked] == ["D", "B", "C"]


class DecisionFixture:
    @staticmethod
    def qualified(symbol, score):
        from strategy import Decision, FeatureVector
        f = FeatureVector(1, 1, 1, 1, 1, 1, 1, 1, 1, 0.5, 0.5, score)
        return Decision(symbol, "LONG", score, f, True, ())
