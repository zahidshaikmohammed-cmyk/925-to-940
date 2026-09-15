from __future__ import annotations

from strategy import Candle


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"need at least {period} observations for EMA{period}")
    k = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = x * k + value * (1.0 - k)
    return value


def trend_sign(candles: tuple[Candle, ...], period: int = 20) -> int:
    """Confirmed higher-timeframe trend: close vs EMA20 AND EMA slope.

    +1 requires close > EMA20 and EMA20 above the EMA20 computed one bar
    earlier; -1 is the exact mirror. Otherwise the result is neutral.
    """
    if len(candles) < period + 1:
        raise ValueError(f"need at least {period + 1} completed candles")
    closes = [c.close for c in candles]
    current_ema = ema(closes, period)
    previous_ema = ema(closes[:-1], period)
    if closes[-1] > current_ema and current_ema > previous_ema:
        return 1
    if closes[-1] < current_ema and current_ema < previous_ema:
        return -1
    return 0
