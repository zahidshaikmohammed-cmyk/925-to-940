from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from config import StrategyConfig
from psygrid_client import Health, PsygridClient, StockData, _DuplicateAwareDict
from run_engine import build_candidates, market_return, parse_universe, select_global_best
from strategy_930 import Candle
from tests.test_strategy_930 import valid_long_fixture

IST = ZoneInfo("Asia/Kolkata")


def mirror_short(candles: tuple[Candle, ...], pivot: float = 150.0) -> tuple[Candle, ...]:
    """Reflect a LONG fixture into an equally-qualifying SHORT fixture.

    Every gate in strategy_930 is expressed as ``side * (price difference)``
    or as a ratio of magnitudes, so mirroring price around a pivot while
    flipping the side leaves every gate value unchanged. This lets the test
    suite exercise genuine SHORT-side Tier 1 qualification without hand
    re-deriving a second fixture from scratch.
    """
    return tuple(
        Candle(c.ts, 2 * pivot - c.open, 2 * pivot - c.low, 2 * pivot - c.high, 2 * pivot - c.close, c.volume)
        for c in candles
    )


def trending_no_retracement_fixture() -> tuple[Candle, ...]:
    """A smooth, monotonic trend with no qualifying pullback anywhere.

    Tier 1 and Tier 2 both require a completed impulse/retracement leg
    (`_leg`); a trend this smooth never produces one, so only Tier 3 can
    ever rank this stock.
    """
    start = datetime(2026, 9, 15, 9, 15, tzinfo=IST)
    return tuple(
        Candle(
            start + timedelta(minutes=i),
            100 + i * 0.25, 100.3 + i * 0.25, 99.9 + i * 0.25, 100.25 + i * 0.25, 1000,
        )
        for i in range(40)
    )


def rows_from_candles(candles: tuple[Candle, ...], previous_close: float | None = None) -> dict:
    payload = {
        "symbol": "X",
        "security_id": "1",
        "candles_1m": [
            {
                "timestamp": c.ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
    }
    if previous_close is not None:
        payload["previous_close"] = previous_close
    return payload


def scan_time_after(candles: tuple[Candle, ...]) -> datetime:
    return candles[-1].ts.astimezone(IST) + timedelta(minutes=1)


class ExplodingClient(PsygridClient):
    """A client whose .stock() raises for a chosen symbol, to prove the
    per-symbol try/except in run_engine.parse_universe actually protects
    the rest of the scan -- regardless of what specific malformation a real
    feed might someday send."""

    def __init__(self, *args, boom_symbol: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.boom_symbol = boom_symbol

    def stock(self, symbol, payload, now=None):
        if symbol == self.boom_symbol:
            raise ValueError("simulated per-stock corruption")
        return super().stock(symbol, payload, now)


class PartialFeedAndMalformedStockTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.cfg.validate()
        self.client = PsygridClient("http://example.invalid")

    def test_non_finite_ohlcv_is_rejected_not_fabricated(self):
        cs = valid_long_fixture()
        payload = rows_from_candles(cs)
        payload["candles_1m"][7]["close"] = "NaN"
        d = self.client.stock("NANTEST", payload, scan_time_after(cs))
        self.assertTrue(all(isfinite(c.close) for c in d.candles))
        self.assertEqual(len(d.candles), len(cs) - 1)
        self.assertTrue(d.health.healthy, d.health.reason)

    def test_infinite_ohlcv_is_rejected_not_fabricated(self):
        cs = valid_long_fixture()
        payload = rows_from_candles(cs)
        payload["candles_1m"][3]["high"] = "Infinity"
        d = self.client.stock("INFTEST", payload, scan_time_after(cs))
        self.assertTrue(all(isfinite(c.high) for c in d.candles))
        self.assertEqual(len(d.candles), len(cs) - 1)

    def test_non_finite_candle_never_corrupts_market_return(self):
        # A single NaN close must not be able to poison the healthy-universe
        # market-return benchmark used to score every other stock.
        good_cs = valid_long_fixture()
        good_payload = rows_from_candles(good_cs)
        bad_payload = rows_from_candles(good_cs)
        bad_payload["candles_1m"][-1]["close"] = "NaN"
        now = scan_time_after(good_cs)
        data = {
            "GOOD": self.client.stock("GOOD", good_payload, now),
            "BAD": self.client.stock("BAD", bad_payload, now),
        }
        mkt = market_return(data)
        self.assertTrue(isfinite(mkt))

    def test_market_snapshot_skips_malformed_top_level_entries_without_raising(self):
        cs = valid_long_fixture()
        raw_payload = {
            "universe_size": 990,
            "stock_count": 4,
            "status": "OPEN",
            "stocks": {
                "GOOD": rows_from_candles(cs),
                "BADTYPE": "not-a-dict-payload",
                "BADNUMBER": 12345,
                "": rows_from_candles(cs),
            },
        }
        self.client._get = lambda path: raw_payload
        result = self.client.market()
        self.assertEqual(list(result.keys()), ["GOOD"])
        self.assertTrue(self.client.last_market_errors)

    def test_empty_candles_stock_is_skipped_without_crash(self):
        payload = {"symbol": "EMPTY", "candles_1m": []}
        d = self.client.stock("EMPTY", payload, datetime(2026, 9, 15, 9, 30, tzinfo=IST))
        self.assertFalse(d.health.healthy)
        self.assertIn("insufficient_completed_1m_0", d.health.reason)

    def test_stale_previous_day_candles_are_excluded_not_reused(self):
        cs = valid_long_fixture()  # dated 2026-09-15
        payload = rows_from_candles(cs)
        # "Now" is the next trading day: none of these candles belong to
        # today's session, so the stock must be treated as having no data,
        # never as if yesterday's candles were today's.
        stale_now = datetime(2026, 9, 16, 9, 30, tzinfo=IST)
        d = self.client.stock("STALE", payload, stale_now)
        self.assertEqual(len(d.candles), 0)
        self.assertFalse(d.health.healthy)

    def test_duplicate_symbol_keys_in_feed_are_reported_and_last_wins(self):
        raw_text = (
            '{"universe_size": 990, "stock_count": 1, "status": "OPEN", "stocks": {'
            '"DUPX": {"symbol": "DUPX", "candles_1m": [], "previous_close": 1.0},'
            '"DUPX": {"symbol": "DUPX", "candles_1m": [], "previous_close": 2.0}'
            "}}"
        )
        payload = json.loads(raw_text, object_pairs_hook=_DuplicateAwareDict)
        self.client._get = lambda path: payload
        result = self.client.market()
        self.assertEqual(list(result.keys()), ["DUPX"])
        self.assertEqual(result["DUPX"]["previous_close"], 2.0)
        self.assertIn("DUPX", self.client.last_market_duplicates)

    def test_parse_universe_survives_a_single_exploding_stock(self):
        cs = valid_long_fixture()
        raw = {
            "GOOD": rows_from_candles(cs),
            "BROKEN": rows_from_candles(cs),
        }
        client = ExplodingClient("http://example.invalid", boom_symbol="BROKEN")
        parsed = parse_universe(client, raw, scan_time_after(cs))
        self.assertEqual(set(parsed.keys()), {"GOOD", "BROKEN"})
        self.assertTrue(parsed["GOOD"].health.healthy)
        self.assertFalse(parsed["BROKEN"].health.healthy)
        self.assertIn("parse_exception", parsed["BROKEN"].health.reason)


class TierFallbackAndGlobalSelectionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.cfg.validate()

    def test_tier1_and_tier2_zero_candidates_still_yields_tier3_number_one(self):
        cs = trending_no_retracement_fixture()
        data = {
            "TREND": StockData(
                symbol="TREND",
                candles=cs,
                ltp=cs[-1].close,
                previous_close=None,
                health=Health("TREND", True),
            )
        }
        candidates = build_candidates(data, {}, self.cfg)
        self.assertTrue(candidates)
        self.assertTrue(all(c.tier == 3 for c in candidates), [(c.tier, c.reasons) for c in candidates])
        selected = select_global_best(candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.tier, 3)

    def test_both_long_and_short_are_evaluated_and_the_stronger_side_wins(self):
        long_cs = valid_long_fixture()
        short_cs = mirror_short(long_cs)
        data = {
            "LONGSTOCK": StockData("LONGSTOCK", long_cs, long_cs[-1].close, None, Health("LONGSTOCK", True)),
            "SHORTSTOCK": StockData("SHORTSTOCK", short_cs, short_cs[-1].close, None, Health("SHORTSTOCK", True)),
        }
        candidates = build_candidates(data, {}, self.cfg)
        sides = {c.side for c in candidates}
        self.assertIn("LONG", sides)
        self.assertIn("SHORT", sides)
        selected = select_global_best(candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.tier, 1)

    def test_engine_always_selects_a_number_one_across_a_hostile_mixed_universe(self):
        """One universe combining every hazard the doctrine must survive:
        a clean Tier-1 LONG, a clean Tier-1 SHORT (mirrored), a Tier-3-only
        trend stock, an empty stock, a stale (previous-day) stock, and a
        stock whose parse explodes. The engine must still land on exactly
        one #1, drawn from a genuinely healthy, evaluable stock, and must
        never raise.
        """
        long_cs = valid_long_fixture()
        short_cs = mirror_short(long_cs)
        trend_cs = trending_no_retracement_fixture()
        now = scan_time_after(long_cs)

        raw = {
            "GOODLONG": rows_from_candles(long_cs),
            "GOODSHORT": rows_from_candles(short_cs),
            "TREND3": rows_from_candles(trend_cs),
            "EMPTY": {"symbol": "EMPTY", "candles_1m": []},
            "STALE": rows_from_candles(long_cs),  # dated 2026-09-15, "now" below is 2026-09-16
            "BROKEN": rows_from_candles(long_cs),
        }
        client = ExplodingClient("http://example.invalid", boom_symbol="BROKEN")

        # Use "now" the next day so STALE truly has zero usable candles,
        # while parsing the rest at their own natural post-session instant.
        stale_now = datetime(2026, 9, 16, 9, 30, tzinfo=IST)
        parsed = {}
        for symbol, payload in raw.items():
            scan_at = stale_now if symbol == "STALE" else now
            try:
                parsed[symbol] = client.stock(symbol, payload, scan_at)
            except Exception as exc:
                parsed[symbol] = StockData(symbol, (), 0.0, None, Health(symbol, False, f"parse_exception:{exc}"))

        healthy = {s: d for s, d in parsed.items() if d.health.healthy}
        self.assertEqual(set(healthy.keys()), {"GOODLONG", "GOODSHORT", "TREND3"})
        self.assertFalse(parsed["EMPTY"].health.healthy)
        self.assertFalse(parsed["STALE"].health.healthy)
        self.assertFalse(parsed["BROKEN"].health.healthy)

        candidates = build_candidates(parsed, {}, self.cfg)
        self.assertTrue(candidates)
        selected = select_global_best(candidates)
        self.assertIsNotNone(selected)
        self.assertIn(selected.symbol, {"GOODLONG", "GOODSHORT"})
        self.assertEqual(selected.tier, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
