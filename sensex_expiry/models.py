"""Plain data types shared by every layer. No logic beyond validation lives here."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum

IST = timezone(timedelta(hours=5, minutes=30))


class DataQuality(str, Enum):
    GOOD = "GOOD"
    DEGRADED = "DEGRADED"   # usable for monitoring, never for a new entry
    BAD = "BAD"


class Regime(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    STRONG_BEAR = "STRONG_BEAR"
    RANGE = "RANGE"
    COMPRESSION = "COMPRESSION"
    EXPANSION = "EXPANSION"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    REVERSAL = "REVERSAL"
    UNCLEAR = "UNCLEAR"


class Direction(str, Enum):
    LONG = "LONG"    # buy CE
    SHORT = "SHORT"  # buy PE

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


class Action(str, Enum):
    BUY = "BUY"
    NO_TRADE = "NO_TRADE"
    EXIT = "EXIT"
    HOLD = "HOLD"


class Reason(str, Enum):
    # positive
    SWEEP_DETECTED = "SWEEP_DETECTED"
    LEVEL_RECLAIMED = "LEVEL_RECLAIMED"
    STRUCTURE_SHIFT = "STRUCTURE_SHIFT"
    DISPLACEMENT = "DISPLACEMENT"
    ORB_ACCEPTANCE = "ORB_ACCEPTANCE"
    ORB_RETEST_HELD = "ORB_RETEST_HELD"
    COMPRESSION_BREAK = "COMPRESSION_BREAK"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    LEVEL_CONFLUENCE = "LEVEL_CONFLUENCE"
    RISK_REWARD_VALID = "RISK_REWARD_VALID"
    REGIME_ALIGNED = "REGIME_ALIGNED"
    # negative / blocking
    NO_EDGE = "NO_EDGE"
    NOT_EXPIRY_DAY = "NOT_EXPIRY_DAY"
    DATA_STALE = "DATA_STALE"
    DATA_BAD = "DATA_BAD"
    CHAIN_STALE = "CHAIN_STALE"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    REGIME_CONFLICT = "REGIME_CONFLICT"
    CHOP_DETECTED = "CHOP_DETECTED"
    ROOM_TOO_SMALL = "ROOM_TOO_SMALL"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    DEPTH_TOO_THIN = "DEPTH_TOO_THIN"
    PREMIUM_TOO_LOW = "PREMIUM_TOO_LOW"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    RISK_TOO_HIGH = "RISK_TOO_HIGH"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    MAX_TRADES = "MAX_TRADES"
    POSITION_OPEN = "POSITION_OPEN"
    COOLDOWN = "COOLDOWN"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    KILL_SWITCH = "KILL_SWITCH"
    LIVE_NOT_VALIDATED = "LIVE_NOT_VALIDATED"
    ENTRIES_PAUSED = "ENTRIES_PAUSED"
    NOT_ARMED = "NOT_ARMED"
    # exits
    EXIT_STOP_PREMIUM = "EXIT_STOP_PREMIUM"
    EXIT_INVALIDATION = "EXIT_INVALIDATION"
    EXIT_TRAIL = "EXIT_TRAIL"
    EXIT_TARGET = "EXIT_TARGET"
    EXIT_TIME_STOP = "EXIT_TIME_STOP"
    EXIT_MAX_HOLD = "EXIT_MAX_HOLD"
    EXIT_HARD_FLAT = "EXIT_HARD_FLAT"
    EXIT_DATA_FAILURE = "EXIT_DATA_FAILURE"


@dataclass(frozen=True)
class Tick:
    security_id: str
    ts: datetime          # exchange last-trade time, tz-aware IST
    ltp: float
    recv_ts: datetime     # local receipt time, tz-aware IST
    volume: int | None = None   # cumulative day volume when the feed provides it (never for the index)
    oi: int | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None


@dataclass(frozen=True)
class Candle:
    start: datetime       # minute label: the candle covers [start, start + 1 minute)
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    ticks: int = 0

    def is_valid(self) -> bool:
        vals = (self.open, self.high, self.low, self.close)
        return (all(v == v and v > 0 for v in vals)
                and self.high >= max(self.open, self.close, self.low)
                and self.low <= min(self.open, self.close, self.high))

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)


@dataclass(frozen=True)
class PriorDay:
    day: date
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class OptionQuote:
    strike: int
    right: str            # "CE" or "PE"
    ltp: float
    bid: float | None
    ask: float | None
    ts: datetime
    delta: float | None = None
    iv: float | None = None
    oi: int | None = None
    volume: int | None = None
    top5_bid_qty: int | None = None
    top5_ask_qty: int | None = None
    security_id: str | None = None

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2
        return None

    @property
    def spread_pct(self) -> float | None:
        m = self.mid
        return (self.ask - self.bid) / m if m else None


@dataclass
class SetupCandidate:
    setup: str
    direction: Direction
    bar_ts: datetime              # close of the bar that confirmed the trigger
    trigger_price: float          # underlying close that confirmed the trigger
    invalidation: float           # underlying level whose close-through exits the trade
    level_name: str
    level: float
    room_level: float | None      # next opposing level, for the room filter
    reasons: list[Reason] = field(default_factory=list)
    confirmations: dict[str, bool] = field(default_factory=dict)

    @property
    def stop_distance(self) -> float:
        return abs(self.trigger_price - self.invalidation)

    @property
    def key(self) -> str:
        """Identity used for duplicate suppression: one trade per setup/level/direction/day."""
        return f"{self.bar_ts.date()}|{self.setup}|{self.level_name}|{self.direction.value}"


@dataclass
class Decision:
    action: Action
    ts: datetime
    reasons: list[Reason]
    candidate: SetupCandidate | None = None
    payload: dict = field(default_factory=dict)
