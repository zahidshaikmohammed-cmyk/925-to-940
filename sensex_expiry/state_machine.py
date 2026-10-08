"""Formal trade lifecycle (spec section 26). Any transition not listed raises; the
engine catches the error, logs it and trips the kill switch, because an impossible
transition means the engine's picture of the world is wrong."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class State(str, Enum):
    WAITING = "WAITING"
    DATA_VALID = "DATA_VALID"
    REGIME_IDENTIFIED = "REGIME_IDENTIFIED"
    SETUP_FORMING = "SETUP_FORMING"
    SETUP_CONFIRMED = "SETUP_CONFIRMED"
    RISK_APPROVED = "RISK_APPROVED"
    ORDER_PENDING = "ORDER_PENDING"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_MANAGEMENT = "POSITION_MANAGEMENT"
    EXIT_PENDING = "EXIT_PENDING"
    COOLDOWN = "COOLDOWN"
    DAY_DONE = "DAY_DONE"
    KILLED = "KILLED"


S = State
TRANSITIONS: dict[State, frozenset[State]] = {
    S.WAITING: frozenset({S.DATA_VALID, S.DAY_DONE}),
    S.DATA_VALID: frozenset({S.REGIME_IDENTIFIED, S.WAITING, S.DAY_DONE}),
    S.REGIME_IDENTIFIED: frozenset({S.SETUP_FORMING, S.SETUP_CONFIRMED, S.WAITING, S.DAY_DONE}),
    S.SETUP_FORMING: frozenset({S.SETUP_CONFIRMED, S.WAITING, S.DAY_DONE}),
    S.SETUP_CONFIRMED: frozenset({S.RISK_APPROVED, S.WAITING}),
    S.RISK_APPROVED: frozenset({S.ORDER_PENDING, S.WAITING}),
    S.ORDER_PENDING: frozenset({S.POSITION_OPEN, S.WAITING}),          # filled, or rejected/cancelled/timed out
    S.POSITION_OPEN: frozenset({S.POSITION_MANAGEMENT, S.EXIT_PENDING}),
    S.POSITION_MANAGEMENT: frozenset({S.POSITION_MANAGEMENT, S.EXIT_PENDING}),
    S.EXIT_PENDING: frozenset({S.COOLDOWN, S.EXIT_PENDING}),            # retried until the broker shows flat
    S.COOLDOWN: frozenset({S.WAITING, S.DAY_DONE}),
    S.DAY_DONE: frozenset(),
    S.KILLED: frozenset(),
}
# KILLED is reachable from every state; nothing leaves it without a restart + manual unlock


class InvalidTransition(RuntimeError):
    pass


@dataclass
class TradeStateMachine:
    state: State = State.WAITING
    history: list[tuple[datetime, State, State, str]] = field(default_factory=list)

    def can(self, new: State) -> bool:
        return new is State.KILLED or new in TRANSITIONS[self.state]

    def go(self, new: State, when: datetime, why: str = "") -> None:
        if not self.can(new):
            raise InvalidTransition(f"{self.state.value} -> {new.value} ({why})")
        self.history.append((when, self.state, new, why))
        self.state = new

    @property
    def flat(self) -> bool:
        return self.state not in (State.ORDER_PENDING, State.POSITION_OPEN, State.POSITION_MANAGEMENT, State.EXIT_PENDING)

    @property
    def accepts_entries(self) -> bool:
        return self.state in (State.WAITING, State.DATA_VALID, State.REGIME_IDENTIFIED, State.SETUP_FORMING,
                              State.SETUP_CONFIRMED)
