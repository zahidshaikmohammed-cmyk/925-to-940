"""Session clock and SENSEX expiry calendar.

FACT (verified 2026-10-08 from secondary sources, confirm with BSE): SENSEX weekly
options expire on THURSDAY since 2025-09-01; a Thursday holiday moves expiry to the
previous trading day. Earlier conventions matter for backtests, because the history
mixes them:

    2023-05-15 .. 2024-12-31   weekly expiry on FRIDAY   (relaunch of SENSEX weeklies)
    2025-01-01 .. 2025-08-31   weekly expiry on TUESDAY
    2025-09-01 .. present      weekly expiry on THURSDAY

The live engine never derives "today is expiry" from this table alone: it uses the
broker's expiry list (Dhan /optionchain/expirylist) and refuses to trade if the two
disagree. The table is the fallback for labelling historical days.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .models import IST

CONVENTIONS = (
    (date(2023, 5, 15), date(2024, 12, 31), 4),   # Friday
    (date(2025, 1, 1), date(2025, 8, 31), 1),     # Tuesday
    (date(2025, 9, 1), date(2099, 12, 31), 3),    # Thursday
)


def expiry_weekday(d: date) -> int | None:
    for start, end, wd in CONVENTIONS:
        if start <= d <= end:
            return wd
    return None


def is_trading_day(d: date, holidays: frozenset[date] = frozenset()) -> bool:
    return d.weekday() < 5 and d not in holidays


def weekly_expiry_for(d: date, holidays: frozenset[date] = frozenset()) -> date | None:
    """The weekly expiry of the week containing d, holiday-shifted to the previous trading day."""
    wd = expiry_weekday(d)
    if wd is None:
        return None
    nominal = d - timedelta(days=d.weekday()) + timedelta(days=wd)
    e = nominal
    while not is_trading_day(e, holidays):
        e -= timedelta(days=1)
        if (nominal - e).days > 6:
            return None
    return e


def is_expiry_day(d: date, holidays: frozenset[date] = frozenset(),
                  broker_expiries: frozenset[date] | None = None) -> tuple[bool, str]:
    """Return (is_expiry, source). With a broker list, both sources must agree."""
    by_rule = weekly_expiry_for(d, holidays) == d
    if broker_expiries is None:
        return by_rule, "RULE"
    by_broker = d in broker_expiries
    if by_rule != by_broker:
        # disagreement is a data problem, never a trading opportunity
        return False, "CONFLICT"
    return by_broker, "BROKER"


def at(d: date, t: time) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=IST)


def minute_floor(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def in_session(ts: datetime, open_: time = time(9, 15), close: time = time(15, 30)) -> bool:
    t = ts.astimezone(IST).time()
    return open_ <= t < close
