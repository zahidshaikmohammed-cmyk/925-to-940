"""fno.py: F&O-only universe and deep-retracement rules on top of the bbbbb.py engine."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import fno
import run_engine
from config import StrategyConfig
from strategy_930 import IST, Candle, evaluate_tiers
from tests.test_strategy_930 import valid_long_fixture

DEEP = replace(StrategyConfig(), **fno.DEEP_RETRACEMENT)

MKTLOTS = """UNDERLYING,SYMBOL,OCT-26,NOV-26,DEC-26
Derivatives on Individual Securities,,,,
NIFTY 50,NIFTY,75,75,75
NIFTY BANK,BANKNIFTY,35,35,35
RELIANCE INDUSTRIES LTD,RELIANCE,500,500,500
MAHINDRA & MAHINDRA LTD,M&M,200,200,200
BAJAJ AUTO LIMITED,BAJAJ-AUTO,75,75,75
"""


def pulled_back(shift: float):
    """valid_long_fixture with the pullback lifted by ``shift`` rupees (a shallower retracement)."""
    cs = valid_long_fixture()
    return tuple(c if i < 5 else Candle(c.ts, c.open + (shift if i > 5 else 0), c.high + (shift if i > 5 else 0),
                                        c.low + shift, c.close + shift, c.volume) for i, c in enumerate(cs))


class DeepRetracementTests(unittest.TestCase):
    def test_config_is_valid(self):
        DEEP.validate()

    def test_deep_pullback_is_still_a_strict_setup(self):
        cs = valid_long_fixture()  # 55% retracement
        tiers = [c.tier for c in evaluate_tiers("X", cs, cs[-1].close, None, 0, 0, [], DEEP) if c.side == "LONG"]
        self.assertIn(1, tiers)

    def test_shallow_pullback_is_not(self):
        cs = pulled_back(0.6)  # 39%: Tier 1 in bbbbb.py, nothing above a forced pick here
        default = {c.tier for c in evaluate_tiers("X", cs, cs[-1].close, None, 0, 0, [], StrategyConfig()) if c.side == "LONG"}
        deep = {c.tier for c in evaluate_tiers("X", cs, cs[-1].close, None, 0, 0, [], DEEP) if c.side == "LONG"}
        self.assertIn(1, default)
        self.assertEqual(deep, {3})

    def test_score_peaks_at_the_configured_depth(self):
        def score(cs, cfg):
            return max(c.score for c in evaluate_tiers("X", cs, cs[-1].close, None, 0, 0, [], cfg)
                       if c.side == "LONG" and c.tier == 1)
        cs = valid_long_fixture()  # 55%
        at_618 = replace(DEEP, retracement_sweet_spot=0.55)
        self.assertGreater(score(cs, at_618), score(cs, DEEP))


class FnoListTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cache = self.dir / "data" / "fno_cache.json"

    def load(self, download, refresh=False, today=date(2026, 10, 13)):
        return fno.load_fno(refresh=refresh, today=today, download=download, cache_path=self.cache)

    def test_parse_mktlots_keeps_stocks_drops_indices(self):
        self.assertEqual(fno.parse_mktlots(MKTLOTS), {"RELIANCE", "M&M", "BAJAJ-AUTO"})

    def test_download_is_cached_for_a_week(self):
        calls = []

        def download(timeout):
            calls.append(1)
            return {"RELIANCE", "TCS"}

        self.assertEqual(self.load(download)[0], {"RELIANCE", "TCS"})
        symbols, source = self.load(download, today=date(2026, 10, 19))
        self.assertEqual((symbols, len(calls)), ({"RELIANCE", "TCS"}, 1))
        self.assertIn("cached", source)
        self.load(download, today=date(2026, 10, 21))
        self.assertEqual(len(calls), 2)

    def test_failed_download_falls_back_to_stale_cache_then_builtin(self):
        def boom(timeout):
            raise OSError("blocked")

        symbols, source = self.load(boom)
        self.assertGreater(len(symbols), 150)
        self.assertIn("built-in", source)
        self.assertIn("RELIANCE", symbols)
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text(json.dumps({"date": "2026-09-01", "symbols": ["SBIN"]}))
        symbols, source = self.load(boom)
        self.assertEqual(symbols, {"SBIN"})
        self.assertIn("stale", source)

    def test_builtin_list_has_no_indices_or_duplicates(self):
        listed = json.loads(fno.BUILTIN_PATH.read_text(encoding="utf-8"))["symbols"]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertFalse(fno.INDEX_UNDERLYINGS & set(listed))


class WiringTests(unittest.TestCase):
    def test_main_runs_the_engine_on_fno_with_deep_rules(self):
        with mock.patch.object(fno, "load_fno", return_value=({"SBIN"}, "test")), \
                mock.patch.object(run_engine, "main", return_value=0) as engine:
            self.assertEqual(fno.main(["--risk-rupees", "500"]), 0)
        argv = engine.call_args.args[0]
        kw = engine.call_args.kwargs
        self.assertIn("--risk-rupees", argv)
        self.assertIn("--base-url", argv)  # Live Core, like bbbbb.py
        self.assertEqual(kw["universe"], {"SBIN"})
        self.assertEqual(kw["cfg_overrides"], fno.DEEP_RETRACEMENT)
        self.assertEqual(kw["rerun_hint"], "fno.py")

    def test_engine_scans_only_the_given_universe(self):
        now = datetime(2026, 10, 13, 10, 0, 20, tzinfo=IST)
        raw = {"SBIN": {"candles_1m": []}, "TINYCO": {"candles_1m": []}}
        client = mock.Mock(last_market_errors=(), last_market_duplicates=(), last_market_meta={"status": "OK"})
        client.market.return_value = raw
        seen = {}

        def parse(c, payload, scan_time):
            seen.update(payload)
            return {}

        with mock.patch.object(run_engine, "now_ist", return_value=now), \
                mock.patch.object(run_engine, "PsygridClient", return_value=client), \
                mock.patch.object(run_engine, "parse_universe", side_effect=parse), \
                mock.patch.object(run_engine.Audit, "event"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            run_engine.main([], universe={"SBIN", "INFY"}, mode_note="F&O ONLY test")
        self.assertEqual(set(seen), {"SBIN"})
        self.assertIn("UNIVERSE FILTER: 1 of 2 listed symbols found in the feed | not in feed: INFY", out.getvalue())
        self.assertIn("F&O ONLY test", out.getvalue())


if __name__ == "__main__":
    unittest.main()
