from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from htf import trend_sign
from strategy import Candle

IST = ZoneInfo("Asia/Kolkata")


def series(values):
    start = datetime(2026, 9, 15, 9, 15, tzinfo=IST)
    return tuple(Candle(start + timedelta(minutes=i), v, v + 0.1, v - 0.1, v, 1000) for i, v in enumerate(values))


def test_htf_uptrend():
    values = [100 + i * 0.5 for i in range(22)]
    assert trend_sign(series(values)) == 1


def test_htf_downtrend():
    values = [120 - i * 0.5 for i in range(22)]
    assert trend_sign(series(values)) == -1


def test_htf_requires_warmup():
    with pytest.raises(ValueError):
        trend_sign(series([100 + i for i in range(20)]))
