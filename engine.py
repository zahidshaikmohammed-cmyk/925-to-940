from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

from strategy import CandidateSnapshot, CandidateState, Decision, SessionState, evaluate, rank_and_lock

EVALUATION_START = time(9, 25)
LOCK_TIME = time(9, 40)


@dataclass
class OpeningEngine:
    """Stateful 09:25–09:40 evaluator with a one-way 09:40 lock."""
    session: SessionState = field(default_factory=SessionState)
    latest: dict[str, Decision] = field(default_factory=dict)

    def evaluate_minute(self, snapshot: CandidateSnapshot, now: datetime) -> Decision:
        if self.session.locked:
            raise RuntimeError("session already locked at 09:40")
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        local = now.astimezone(__import__("strategy").IST)
        if local.time() < EVALUATION_START or local.time() > LOCK_TIME:
            raise ValueError("evaluation is allowed only from 09:25 through 09:40 IST")
        decision = evaluate(snapshot, self.session.candidates.setdefault(snapshot.symbol, CandidateState()))
        self.latest[snapshot.symbol] = decision
        return decision

    def lock(self, now: datetime) -> tuple[Decision, ...]:
        if self.session.locked:
            return self.session.locked_decisions
        locked = rank_and_lock(self.latest.values(), now, top_n=3)
        self.session.locked = True
        self.session.locked_decisions = locked
        return locked

    def decisions(self) -> tuple[Decision, ...]:
        return tuple(self.latest.values())
