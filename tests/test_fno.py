"""fno.py: F&O-only universe and deep-retracement rules on top of the bbbbb.py engine."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
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
NIFTY FPI INDEX,NIFTYFPI,40,40,40
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
            self.assertEqual(fno.main(["--once", "--risk-rupees", "500"]), 0)
        argv = engine.call_args.args[0]
        kw = engine.call_args.kwargs
        self.assertIn("--risk-rupees", argv)
        self.assertNotIn("--once", argv)
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


class HuntTests(unittest.TestCase):
    """fno.py hunts from 09:15 and beeps only on a Tier 1 SIGNAL_READY."""

    def setUp(self):
        cs = valid_long_fixture()
        self.t1 = [c for c in evaluate_tiers("SBIN", cs, cs[-1].close, None, 0, 0, [], DEEP) if c.tier == 1][0]

    def run_hunt(self, start: datetime, results, until: str = "10:10"):
        """``results``: per scan, (status, selected) or an exception."""
        clock = [start]
        beeps, scans = [], []

        def scan(argv, symbols, note):
            item = results[min(len(scans), len(results) - 1)]
            scans.append(clock[0])
            if isinstance(item, Exception):
                raise item
            print("full engine report")
            run_engine.LAST_STATUS, run_engine.LAST_SELECTED = item
            return 0

        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = fno.hunt([], {"SBIN"}, "NOTE", until, 8, clock=lambda: clock[0], sleep=sleep,
                            beep=lambda: beeps.append(clock[0]), scan=scan)
        return code, beeps, scans, out.getvalue()

    def test_waits_for_0915_then_scans_every_minute(self):
        start = datetime(2026, 10, 13, 9, 5, tzinfo=IST)
        _, beeps, scans, out = self.run_hunt(start, [("NO_PRECISION_SETUP", None)], until="09:20")
        self.assertEqual(scans[0], datetime(2026, 10, 13, 9, 16, 8, tzinfo=IST))
        self.assertEqual(len(scans), 4)  # 09:16 .. 09:19
        self.assertEqual(beeps, [])
        self.assertIn("waiting for the open", out)

    def test_beeps_on_tier1_signal_ready_with_the_full_report(self):
        start = datetime(2026, 10, 13, 10, 0, 8, tzinfo=IST)
        results = [("NO_PRECISION_SETUP", self.t1), ("SIGNAL_READY", self.t1)]
        _, beeps, _, out = self.run_hunt(start, results)
        self.assertEqual(beeps, [start + timedelta(minutes=1)])  # once, not every minute after
        self.assertIn("TIER 1 SIGNAL @ 10:01:08 IST: SBIN LONG", out)
        self.assertEqual(out.count("full engine report"), 1)
        self.assertIn("already alerted", out)

    def test_tier2_signal_ready_does_not_beep(self):
        t2 = replace(self.t1, tier=2)
        _, beeps, _, out = self.run_hunt(datetime(2026, 10, 13, 10, 0, 8, tzinfo=IST), [("SIGNAL_READY", t2)])
        self.assertEqual(beeps, [])
        self.assertIn("closest SBIN LONG (Tier 2)", out)

    def test_same_signal_realerts_after_an_hour(self):
        start = datetime(2026, 10, 13, 10, 0, 8, tzinfo=IST)
        _, beeps, _, _ = self.run_hunt(start, [("SIGNAL_READY", self.t1)], until="11:10")
        self.assertEqual([b - start for b in beeps], [timedelta(0), timedelta(minutes=60)])

    def test_a_failed_scan_does_not_end_the_hunt(self):
        start = datetime(2026, 10, 13, 10, 0, 8, tzinfo=IST)
        _, beeps, scans, out = self.run_hunt(start, [RuntimeError("feed down"), ("SIGNAL_READY", self.t1)])
        self.assertEqual(len(scans), 10)
        self.assertEqual(len(beeps), 1)
        self.assertIn("ERROR: feed down", out)

    def test_default_is_hunt_mode(self):
        with mock.patch.object(fno, "load_fno", return_value=({"SBIN"}, "test")), \
                mock.patch.object(fno, "hunt", return_value=0) as hunt:
            fno.main(["--until", "14:30"])
        self.assertEqual(hunt.call_args.args[3], "14:30")


if __name__ == "__main__":
    unittest.main()
