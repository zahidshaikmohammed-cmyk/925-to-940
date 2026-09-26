from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import StrategyConfig
from psygrid_client import Health, PsygridClient, StockData
import run_engine
from tests.test_run_engine import (
    ExplodingClient,
    mirror_short,
    rows_from_candles,
    scan_time_after,
    trending_no_retracement_fixture,
)
from tests.test_strategy_930 import valid_long_fixture

IST = ZoneInfo("Asia/Kolkata")

# "50.py" is not a valid Python identifier, so it cannot be `import`ed with a
# normal statement. This loads it the same way `python 50.py` would run it,
# without duplicating a single line of its logic.
_MODULE_PATH = Path(__file__).resolve().parent.parent / "50.py"
_SPEC = importlib.util.spec_from_file_location("nifty50_engine_under_test", _MODULE_PATH)
nifty50_engine = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = nifty50_engine
_SPEC.loader.exec_module(nifty50_engine)


NIFTY_A = "RELIANCE"
NIFTY_B = "HDFCBANK"
NOT_NIFTY = "SOME_990_ONLY_STOCK"


class ReusesExistingPipelineTests(unittest.TestCase):
    """Proves 50.py imports the 990 engine's own functions rather than
    reimplementing them -- the literal requirement 'do not duplicate their
    mathematical implementation'.
    """

    def test_uses_the_same_function_objects_as_the_990_engine(self):
        self.assertIs(nifty50_engine.build_candidates, run_engine.build_candidates)
        self.assertIs(nifty50_engine.select_global_best, run_engine.select_global_best)
        self.assertIs(nifty50_engine.parse_universe, run_engine.parse_universe)
        self.assertIs(nifty50_engine.health_failure_report, run_engine.health_failure_report)
        self.assertIs(nifty50_engine.load_sector_map, run_engine.load_sector_map)


class UniverseRestrictionTests(unittest.TestCase):
    def test_restricts_to_nifty50_symbols_only(self):
        raw = {
            NIFTY_A: {"symbol": NIFTY_A},
            NIFTY_B: {"symbol": NIFTY_B},
            NOT_NIFTY: {"symbol": NOT_NIFTY},
        }
        filtered, unavailable = nifty50_engine.restrict_to_nifty50(raw, [NIFTY_A, NIFTY_B, "MISSINGCO"])
        self.assertEqual(set(filtered.keys()), {NIFTY_A, NIFTY_B})
        self.assertNotIn(NOT_NIFTY, filtered)
        self.assertEqual(unavailable, ["MISSINGCO"])

    def test_does_not_evaluate_unrelated_990_universe_stocks(self):
        long_cs = valid_long_fixture()
        raw = {
            NIFTY_A: rows_from_candles(long_cs),
            NOT_NIFTY: rows_from_candles(long_cs),
        }
        filtered, _ = nifty50_engine.restrict_to_nifty50(raw, [NIFTY_A])
        client = PsygridClient("http://example.invalid")
        parsed = run_engine.parse_universe(client, filtered, scan_time_after(long_cs))
        self.assertEqual(set(parsed.keys()), {NIFTY_A})
        candidates = run_engine.build_candidates(parsed, {}, StrategyConfig())
        self.assertTrue(all(c.symbol == NIFTY_A for c in candidates))

    def test_missing_nifty50_stock_is_reported_not_fabricated(self):
        raw = {NIFTY_A: rows_from_candles(valid_long_fixture())}
        filtered, unavailable = nifty50_engine.restrict_to_nifty50(raw, [NIFTY_A, "GHOSTCO"])
        self.assertNotIn("GHOSTCO", filtered)
        self.assertEqual(unavailable, ["GHOSTCO"])


class ConstituentFileTests(unittest.TestCase):
    def test_loads_real_constituent_file_with_50_unique_symbols(self):
        meta = nifty50_engine.load_nifty50_constituents()
        self.assertEqual(len(meta["symbols"]), 50)
        self.assertEqual(len(set(meta["symbols"])), 50)

    def test_missing_file_is_a_reported_failure_not_a_fabricated_list(self):
        with self.assertRaises(FileNotFoundError):
            nifty50_engine.load_nifty50_constituents(Path("/nonexistent/nifty50_constituents.json"))

    def test_malformed_symbols_field_is_rejected(self, tmp_name="test_bad_constituents.json"):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"symbols": "not-a-list"}, fh)
            path = Path(fh.name)
        try:
            with self.assertRaises(ValueError):
                nifty50_engine.load_nifty50_constituents(path)
        finally:
            path.unlink(missing_ok=True)


class TierAndPipelineParityTests(unittest.TestCase):
    """Same tier/LONG-SHORT/global-#1 invariants as the 990 engine, now
    exercised through the NIFTY-50-restricted path.
    """

    def setUp(self):
        self.cfg = StrategyConfig()
        self.cfg.validate()

    def test_tier1_behavior_is_preserved_through_the_nifty50_path(self):
        cs = valid_long_fixture()
        raw = {NIFTY_A: rows_from_candles(cs), NOT_NIFTY: rows_from_candles(cs)}
        filtered, _ = nifty50_engine.restrict_to_nifty50(raw, [NIFTY_A])
        client = PsygridClient("http://example.invalid")
        parsed = run_engine.parse_universe(client, filtered, scan_time_after(cs))
        candidates = run_engine.build_candidates(parsed, {}, self.cfg)
        tier1 = [c for c in candidates if c.tier == 1]
        self.assertTrue(tier1)
        self.assertEqual(tier1[0].side, "LONG")

    def test_tier2_fallback_is_preserved_through_the_nifty50_path(self):
        cs = valid_long_fixture(retrace_volume=1300)
        raw = {NIFTY_A: rows_from_candles(cs)}
        filtered, _ = nifty50_engine.restrict_to_nifty50(raw, [NIFTY_A])
        client = PsygridClient("http://example.invalid")
        parsed = run_engine.parse_universe(client, filtered, scan_time_after(cs))
        candidates = run_engine.build_candidates(parsed, {}, self.cfg)
        self.assertTrue(any(c.tier > 1 for c in candidates))

    def test_tier3_is_the_mandatory_fallback_through_the_nifty50_path(self):
        cs = trending_no_retracement_fixture()
        data = {NIFTY_A: StockData(NIFTY_A, cs, cs[-1].close, None, Health(NIFTY_A, True))}
        candidates = run_engine.build_candidates(data, {}, self.cfg)
        self.assertTrue(candidates)
        self.assertTrue(all(c.tier == 3 for c in candidates))
        selected = run_engine.select_global_best(candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.tier, 3)

    def test_tier1_and_tier2_zero_still_yields_tier3_number_one_in_nifty50_universe(self):
        cs = trending_no_retracement_fixture()
        raw = {NIFTY_A: rows_from_candles(cs)}
        filtered, _ = nifty50_engine.restrict_to_nifty50(raw, [NIFTY_A])
        client = PsygridClient("http://example.invalid")
        parsed = run_engine.parse_universe(client, filtered, scan_time_after(cs))
        candidates = run_engine.build_candidates(parsed, {}, self.cfg)
        self.assertTrue(all(c.tier == 3 for c in candidates), [(c.tier, c.reasons) for c in candidates])
        selected = run_engine.select_global_best(candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.tier, 3)

    def test_both_long_and_short_are_evaluated_in_nifty50_universe(self):
        long_cs = valid_long_fixture()
        short_cs = mirror_short(long_cs)
        raw = {NIFTY_A: rows_from_candles(long_cs), NIFTY_B: rows_from_candles(short_cs)}
        filtered, _ = nifty50_engine.restrict_to_nifty50(raw, [NIFTY_A, NIFTY_B])
        client = PsygridClient("http://example.invalid")
        parsed = run_engine.parse_universe(client, filtered, scan_time_after(long_cs))
        candidates = run_engine.build_candidates(parsed, {}, self.cfg)
        sides = {c.side for c in candidates}
        self.assertIn("LONG", sides)
        self.assertIn("SHORT", sides)

    def test_a_malformed_nifty50_stock_does_not_terminate_the_scan(self):
        cs = valid_long_fixture()
        raw = {
            NIFTY_A: rows_from_candles(cs),
            "BROKENNIFTY": rows_from_candles(cs),
        }
        filtered, _ = nifty50_engine.restrict_to_nifty50(raw, [NIFTY_A, "BROKENNIFTY"])
        client = ExplodingClient("http://example.invalid", boom_symbol="BROKENNIFTY")
        parsed = run_engine.parse_universe(client, filtered, scan_time_after(cs))
        self.assertTrue(parsed[NIFTY_A].health.healthy)
        self.assertFalse(parsed["BROKENNIFTY"].health.healthy)
        candidates = run_engine.build_candidates(parsed, {}, self.cfg)
        self.assertTrue(candidates)
        selected = run_engine.select_global_best(candidates)
        self.assertEqual(selected.symbol, NIFTY_A)

    def test_exactly_one_global_number_one_when_at_least_one_healthy_candidate_exists(self):
        long_cs = valid_long_fixture()
        short_cs = mirror_short(long_cs)
        trend_cs = trending_no_retracement_fixture()
        raw = {
            NIFTY_A: rows_from_candles(long_cs),
            NIFTY_B: rows_from_candles(short_cs),
            "NIFTYTREND": rows_from_candles(trend_cs),
            "NIFTYEMPTY": {"symbol": "NIFTYEMPTY", "candles_1m": []},
        }
        constituents = [NIFTY_A, NIFTY_B, "NIFTYTREND", "NIFTYEMPTY", "NIFTYGHOST"]
        filtered, unavailable = nifty50_engine.restrict_to_nifty50(raw, constituents)
        self.assertEqual(unavailable, ["NIFTYGHOST"])
        client = PsygridClient("http://example.invalid")
        parsed = run_engine.parse_universe(client, filtered, scan_time_after(long_cs))
        candidates = run_engine.build_candidates(parsed, {}, self.cfg)
        selected = run_engine.select_global_best(candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.tier, 1)
        self.assertIn(selected.symbol, {NIFTY_A, NIFTY_B})

    def test_no_nifty50_coverage_in_feed_is_a_genuine_failure_not_a_fabricated_signal(self):
        raw = {NOT_NIFTY: rows_from_candles(valid_long_fixture())}
        filtered, unavailable = nifty50_engine.restrict_to_nifty50(raw, [NIFTY_A, NIFTY_B])
        self.assertEqual(filtered, {})
        self.assertEqual(set(unavailable), {NIFTY_A, NIFTY_B})


if __name__ == "__main__":
    unittest.main(verbosity=2)
