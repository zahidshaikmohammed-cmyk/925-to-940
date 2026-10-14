"""Strategy-neutral types and rules shared by the ten engines.

Nothing here decides *whether* a setup exists -- that is each engine's own mathematics.
This module only defines:

* the evaluation outcome vocabulary (so a data failure is never reported as "no setup"),
* the score rule that turns an engine's components into a 0-100 number,
* NSE tick rounding and the risk / transaction-cost gates every trade plan must pass.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from edge.costs import CostModel

# ---------------------------------------------------------------------------- outcomes

SIGNAL = "SIGNAL"                        # all mandatory gates passed and score >= 90
BELOW_THRESHOLD = "BELOW_THRESHOLD"      # all mandatory gates passed, score < 90
NO_SETUP = "NO_SETUP"                    # valid data, the strategy's pattern is absent
OUTSIDE_WINDOW = "OUTSIDE_WINDOW"        # the engine does not trade at this time of day
INELIGIBLE = "INELIGIBLE"                # valid data, but too illiquid / price out of range
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # too few bars, gaps, stale, or a required input missing
INVALID_DATA = "INVALID_DATA"            # the feed marked the stock unhealthy / malformed
ENGINE_ERROR = "ENGINE_ERROR"            # the engine raised; isolated to this engine+stock

STATUSES = (SIGNAL, BELOW_THRESHOLD, NO_SETUP, OUTSIDE_WINDOW, INELIGIBLE,
            INSUFFICIENT_DATA, INVALID_DATA, ENGINE_ERROR)
DATA_FAILURES = (INSUFFICIENT_DATA, INVALID_DATA, ENGINE_ERROR)

LONG, SHORT = "LONG", "SHORT"

# ------------------------------------------------------------------------------- score

SIGNAL_THRESHOLD = 90.0
# "Q90 rule": a weighted mean can hide one failed leg behind several perfect ones. A score
# of 90+ therefore additionally requires every component to be at least this good; when one
# is not, the score is capped just below the threshold.
MIN_COMPONENT_FOR_90 = 0.5
CAPPED_SCORE = 89.99


def clamp01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def ramp(x: float, zero_at: float, full_at: float) -> float:
    """Piecewise-linear quality: 0 at ``zero_at``, 1 at ``full_at`` (either direction)."""
    if zero_at == full_at:
        raise ValueError("ramp needs two distinct anchors")
    return clamp01((x - zero_at) / (full_at - zero_at))


def band(x: float, zero_lo: float, full_lo: float, full_hi: float, zero_hi: float) -> float:
    """Trapezoid: 1 inside [full_lo, full_hi], falling linearly to 0 at zero_lo / zero_hi."""
    if not zero_lo < full_lo <= full_hi < zero_hi:
        raise ValueError("band anchors must satisfy zero_lo < full_lo <= full_hi < zero_hi")
    if x < full_lo:
        return ramp(x, zero_lo, full_lo)
    if x > full_hi:
        return ramp(x, zero_hi, full_hi)
    return 1.0


@dataclass(frozen=True)
class Component:
    name: str
    weight: float
    quality: float          # in [0, 1]
    raw: float              # the measured value the quality came from
    detail: str             # human-readable definition, printed in alerts and logs


def score_of(components: list[Component]) -> tuple[float, bool]:
    """(score, capped). score = 100 * sum(w_i q_i); weights must sum to 1.

    Capped (score set to 89.99) when the weighted score would be >= 90 but any component
    quality is below MIN_COMPONENT_FOR_90.
    """
    if not components:
        raise ValueError("no score components")
    total_w = sum(c.weight for c in components)
    if abs(total_w - 1.0) > 1e-9:
        raise ValueError(f"score weights sum to {total_w}, not 1")
    for c in components:
        if not 0.0 <= c.quality <= 1.0 or c.weight <= 0:
            raise ValueError(f"bad component {c.name}: weight {c.weight}, quality {c.quality}")
    raw = 100.0 * sum(c.weight * c.quality for c in components)
    raw = round(raw, 2)
    if raw >= SIGNAL_THRESHOLD and min(c.quality for c in components) < MIN_COMPONENT_FOR_90:
        return CAPPED_SCORE, True
    return raw, False


# ------------------------------------------------------------------------- NSE ticks

# NSE cash-market tick bands (circular 33/2025, effective 2025-04-15, reviewed monthly on the
# previous month's closing price). Ascending upper bounds -> tick in rupees.
TICK_BANDS = ((250.0, 0.01), (1000.0, 0.05), (5000.0, 0.10), (10000.0, 0.50), (20000.0, 1.00),
              (math.inf, 5.00))


def tick_size(price: float) -> float:
    for upper, tick in TICK_BANDS:
        if price < upper:
            return tick
    return TICK_BANDS[-1][1]


def safe_tick(price: float) -> float:
    """A tick valid whatever band the exchange assigned this month.

    The band is set from last month's close, so a stock can trade near a boundary with the
    neighbouring band's tick. Each tick divides the next coarser one, so the coarser tick of
    the bands at price/1.1 and price*1.1 is always a legal multiple.
    """
    return max(tick_size(price / 1.1), tick_size(price * 1.1))


def round_to_tick(price: float, tick: float, up: bool) -> float:
    n = price / tick
    k = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(k * tick, 2)


# ---------------------------------------------------------------------- trade plans

@dataclass(frozen=True)
class RiskConfig:
    min_risk_pct: float = 0.15     # below this, round-trip costs consume too much of 1R
    max_risk_pct: float = 2.0      # a wider stop is not an intraday 1-minute setup
    max_cost_r: float = 0.25       # round-trip cost must be <= 0.25 R
    reward_r: float = 2.0          # default target multiple
    min_reward_r: float = 1.5      # an explicit (structural) target must still pay this much
    cost_order_value: float = 100_000.0
    cost_slippage_bps: float = 2.0  # the BASE scenario of edge/costs.py (an assumption: no quotes)

    def round_trip_cost_pct(self) -> float:
        return CostModel(order_value=self.cost_order_value,
                         slippage_bps_per_side=self.cost_slippage_bps).round_trip_pct()


@dataclass(frozen=True)
class Trade:
    direction: str
    entry: float        # stop-entry trigger price (tick-rounded away from the market)
    stop: float
    target: float
    risk: float         # rupees per share
    risk_pct: float
    reward_r: float
    cost_r: float       # round-trip cost in R


def build_trade(direction: str, trigger: float, stop: float, cfg: RiskConfig,
                target: float | None = None) -> tuple[Trade | None, str]:
    """Tick-round a stop-entry plan and apply the risk/cost gates. (trade, "") or (None, why).

    LONG: entry one tick above ``trigger`` rounded up, stop rounded down, target rounded down.
    SHORT mirrors it. The rounding is always against the trader (conservative R).
    """
    if direction not in (LONG, SHORT):
        raise ValueError(direction)
    if not all(math.isfinite(x) and x > 0 for x in (trigger, stop)):
        return None, "non_finite_trade_inputs"
    tick = safe_tick(trigger)
    sign = 1 if direction == LONG else -1
    entry = round_to_tick(trigger + sign * tick, tick, up=direction == LONG)
    stop_r = round_to_tick(stop, tick, up=direction == SHORT)
    risk = (entry - stop_r) * sign
    if risk <= 0:
        return None, "stop_not_beyond_entry"
    risk_pct = risk / entry * 100.0
    if risk_pct < cfg.min_risk_pct:
        return None, f"risk_too_small_{risk_pct:.3f}pct"
    if risk_pct > cfg.max_risk_pct:
        return None, f"risk_too_large_{risk_pct:.3f}pct"
    cost_r = cfg.round_trip_cost_pct() / risk_pct
    if cost_r > cfg.max_cost_r:
        return None, f"costs_{cost_r:.2f}R"
    if target is None:
        tgt = round_to_tick(entry + sign * cfg.reward_r * risk, tick, up=direction == SHORT)
    else:
        if not math.isfinite(target) or target <= 0:
            return None, "non_finite_target"
        tgt = round_to_tick(target, tick, up=direction == SHORT)
    reward_r = (tgt - entry) * sign / risk
    floor_r = cfg.min_reward_r if target is not None else cfg.reward_r - 1e-6 - tick / risk
    if reward_r < floor_r:
        return None, f"reward_{reward_r:.2f}R_below_{floor_r:.2f}R"
    return Trade(direction, entry, stop_r, tgt, round(risk, 4), round(risk_pct, 4),
                 round(reward_r, 3), round(cost_r, 3)), ""


# --------------------------------------------------------------------- results

@dataclass(frozen=True)
class Signal:
    engine: str
    strategy: str
    symbol: str
    direction: str
    score: float
    entry: float
    stop: float
    target: float
    risk_pct: float
    reward_r: float
    cost_r: float
    bar_time: datetime          # start of the last completed candle used (the decision candle)
    signal_time: datetime       # bar_time + 1 minute: when the information became available
    valid_until: datetime       # cancel the stop-entry if not triggered by then
    anchor: str                 # engine-defined identity of this setup (duplicate key)
    conditions: tuple[str, ...]
    components: tuple[Component, ...]

    @property
    def key(self) -> str:
        return f"{self.engine}|{self.symbol}|{self.direction}|{self.anchor}"


@dataclass(frozen=True)
class Evaluation:
    engine: str
    symbol: str
    status: str
    reason: str = ""
    score: float | None = None
    direction: str | None = None
    signal: Signal | None = None
    components: tuple[Component, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status}")
        if (self.status == SIGNAL) != (self.signal is not None):
            raise ValueError("a SIGNAL evaluation must carry exactly one signal")


def make_signal(engine, symbol: str, trade: Trade, score: float, bar_time: datetime, anchor: str,
                conditions: list[str], components: list[Component]) -> Signal:
    signal_time = bar_time + timedelta(minutes=1)
    return Signal(engine.NAME, engine.TITLE, symbol, trade.direction, score, trade.entry, trade.stop,
                  trade.target, trade.risk_pct, trade.reward_r, trade.cost_r, bar_time, signal_time,
                  signal_time + timedelta(minutes=engine.ENTRY_VALID_MINUTES), anchor,
                  tuple(conditions), tuple(components))


def finish(engine, symbol: str, direction: str, trade: Trade, components: list[Component],
           bar_time: datetime, anchor: str, conditions: list[str]) -> Evaluation:
    """Score a setup whose mandatory gates all passed: SIGNAL at >= 90, else BELOW_THRESHOLD."""
    score, capped = score_of(components)
    if score >= SIGNAL_THRESHOLD:
        sig = make_signal(engine, symbol, trade, score, bar_time, anchor, conditions, components)
        return Evaluation(engine.NAME, symbol, SIGNAL, "", score, direction, sig, tuple(components))
    weakest = min(components, key=lambda c: c.quality)
    why = f"score_{score:.2f}" + ("_capped_weak_component_" + weakest.name if capped else "")
    return Evaluation(engine.NAME, symbol, BELOW_THRESHOLD, why, score, direction, None, tuple(components))


@dataclass(frozen=True)
class Setup:
    """A pattern found by an engine's ``side`` method, in direction-normalised prices
    (see features.Side): every mandatory gate has passed; the risk gates come next."""
    trigger: float                 # normalised price the stop-entry sits beyond
    stop: float                    # normalised protective stop
    components: tuple[Component, ...]
    anchor: str
    conditions: tuple[str, ...]
    target: float | None = None    # normalised explicit target (None = RiskConfig.reward_r)


@dataclass(frozen=True)
class Reject:
    status: str                    # NO_SETUP or INSUFFICIENT_DATA
    reason: str


class Engine:
    """Base class: identity, trading window and the evaluation contract.

    Subclasses implement ``side(stock, side_view, ctx)`` -> Setup | Reject, written once for
    LONG; the base class runs it on Side(+1) (LONG) and Side(-1) (SHORT, mirrored prices),
    applies the risk gates and the score threshold, and returns one Evaluation.
    It enforces the time window and the minimum bar count before anything else.
    """
    NAME = "Engine"
    TITLE = ""
    EARLIEST = time(9, 30)       # first decision-candle START time the engine accepts
    LATEST = time(14, 44)        # last decision-candle START time (entries close at 14:45)
    MIN_BARS = 20
    NEEDS_MARKET = False         # requires a valid cross-sectional market context
    ENTRY_VALID_MINUTES = 3
    risk = RiskConfig()

    def evaluate(self, stock, features, ctx) -> Evaluation:
        bars = stock.bars
        if not bars:
            return Evaluation(self.NAME, stock.symbol, INSUFFICIENT_DATA, "no_bars")
        t = bars[-1].ts.time()
        if not self.EARLIEST <= t <= self.LATEST:
            return Evaluation(self.NAME, stock.symbol, OUTSIDE_WINDOW, f"bar_{t:%H:%M}")
        if len(bars) < self.MIN_BARS:
            return Evaluation(self.NAME, stock.symbol, INSUFFICIENT_DATA, f"bars_{len(bars)}<{self.MIN_BARS}")
        if self.NEEDS_MARKET and not ctx.valid:
            return Evaluation(self.NAME, stock.symbol, INSUFFICIENT_DATA, "market_context_" + ctx.reason)
        return self._evaluate(stock, features, ctx)

    DIRECTIONS = (LONG, SHORT)

    def prepare(self, views: dict, feats: dict, ctx) -> None:
        """Optional per-scan cross-sectional precomputation (default: nothing)."""

    def side(self, stock, sv, ctx):  # pragma: no cover - abstract
        raise NotImplementedError

    def _evaluate(self, stock, features, ctx) -> Evaluation:
        results: list[Evaluation] = []
        rejects: list[Reject] = []
        for direction in self.DIRECTIONS:
            s = 1 if direction == LONG else -1
            sv = features.side(s)
            found = self.side(stock, sv, ctx)
            if isinstance(found, Reject):
                rejects.append(found)
                continue
            target = None if found.target is None else sv.real(found.target)
            trade, why = build_trade(direction, sv.real(found.trigger), sv.real(found.stop), self.risk, target)
            if trade is None:
                rejects.append(Reject(NO_SETUP, f"{direction.lower()}_risk_gate:{why}"))
                continue
            results.append(finish(self, stock.symbol, direction, trade, list(found.components),
                                  stock.bars[-1].ts, found.anchor, list(found.conditions)))
        if results:
            # best score wins; LONG before SHORT on an exact tie (deterministic)
            return max(results, key=lambda e: (e.score, e.direction == LONG))
        if all(r.status == INSUFFICIENT_DATA for r in rejects):
            return Evaluation(self.NAME, stock.symbol, INSUFFICIENT_DATA, "; ".join(r.reason for r in rejects))
        return Evaluation(self.NAME, stock.symbol, NO_SETUP,
                          "; ".join(r.reason for r in rejects if r.status == NO_SETUP))


def no(reason: str) -> Reject:
    return Reject(NO_SETUP, reason)


def lacking(reason: str) -> Reject:
    return Reject(INSUFFICIENT_DATA, reason)
