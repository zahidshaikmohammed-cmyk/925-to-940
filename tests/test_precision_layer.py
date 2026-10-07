"""Market regime, trend persistence, the final pick and the outcome grader."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import grade_signals
from config import StrategyConfig
from precision import Breadth, entry_window, market_breadth, persistence, rank_picks
from strategy_930 import IST, Candidate, Candle

START = datetime(2026, 10, 7, 9, 15, tzinfo=IST)


def series(steps, start_price=100.0, volume=10_000, start=START):
    """Candles from per-minute close changes; each candle opens at the prior close."""
    out, price = [], start_price
    for i, step in enumerate(steps):
        o, c = price, price + step
        out.append(Candle(start + timedelta(minutes=i), o, max(o, c) + 0.02, min(o, c) - 0.02, c,
                          volume * (2 if step > 0 else 1)))
        price = c
    return tuple(out)


def uptrend(n=90):
    # steady climb with shallow pullbacks every 5th minute
    return series([0.06 if i % 5 else -0.025 for i in range(n)])  # about +4% on the day


def chop(n=90):
    return series([0.15 if (i // 3) % 2 else -0.15 for i in range(n)])


def mirror(cs, pivot=200.0):
    return tuple(Candle(c.ts, 2 * pivot - c.open, 2 * pivot - c.low, 2 * pivot - c.high, 2 * pivot - c.close, c.volume)
                 for c in cs)


def cand(symbol="X", side="LONG", score=70.0, tier=1, rs=1.0):
    return Candidate(symbol, side, score, 110, 109, 112, 0, 0.5, 1.5, 0.5, 0.5, rs, rs, 0.5, 0.6, 0.2, 109, tier, ())


class BreadthTests(unittest.TestCase):
    def test_regimes(self):
        cfg = StrategyConfig()
        up, down = uptrend(30), mirror(uptrend(30))
        self.assertEqual(market_breadth([up] * 7 + [down] * 3, cfg).regime, "LONG_ONLY")
        self.assertEqual(market_breadth([up] * 3 + [down] * 7, cfg).regime, "SHORT_ONLY")
        self.assertEqual(market_breadth([up] * 5 + [down] * 5, cfg).regime, "NEUTRAL")
        self.assertTrue(Breadth(0.7, 10, "LONG_ONLY").allows("LONG"))
        self.assertFalse(Breadth(0.7, 10, "LONG_ONLY").allows("SHORT"))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()

    def test_a_clean_trend_outscores_chop(self):
        trend = persistence(uptrend(), "LONG", 1.0, 1.0, True, self.cfg)
        noise = persistence(chop(), "LONG", 1.0, 1.0, True, self.cfg)
        self.assertGreater(trend.score, 70, trend)
        self.assertLess(noise.score, trend.score - 20, noise)
        self.assertGreater(noise.vwap_crosses, trend.vwap_crosses)

    def test_long_and_short_are_symmetric(self):
        up = persistence(uptrend(), "LONG", 1.0, 1.0, True, self.cfg)
        down = persistence(mirror(uptrend()), "SHORT", 1.0, 1.0, True, self.cfg)
        self.assertAlmostEqual(up.score, down.score, places=6)
        self.assertLess(persistence(uptrend(), "SHORT", 1.0, 1.0, True, self.cfg).score, 40)

    def test_a_climax_candle_is_a_reversal_warning(self):
        cs = list(uptrend())
        last = cs[-1]
        cs[-1] = Candle(last.ts, last.open, last.open + 2.0, last.open - 0.1, last.open + 1.8, last.volume * 20)
        p = persistence(cs, "LONG", 1.0, 1.0, True, self.cfg)
        self.assertIn("climax_candle", p.warnings)
        self.assertGreaterEqual(p.penalty, self.cfg.penalty_climax)


class PickTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.candles = {"UP": uptrend(), "DOWN": mirror(uptrend()), "CHOP": chop()}
        self.sector = {"UP": True, "DOWN": True, "CHOP": True}
        self.morning = START + timedelta(minutes=90)

    def test_trend_beats_chop_and_tradable_comes_first(self):
        picks = rank_picks([cand("CHOP"), cand("UP")], self.candles, self.sector,
                           Breadth(0.5, 10, "NEUTRAL"), self.morning, self.cfg)
        self.assertEqual(picks[0].candidate.symbol, "UP")
        self.assertTrue(picks[0].tradable, picks[0].blockers)
        self.assertFalse(picks[1].tradable)

    def test_a_short_is_blocked_in_a_long_only_market(self):
        picks = rank_picks([cand("DOWN", "SHORT")], self.candles, self.sector,
                           Breadth(0.75, 10, "LONG_ONLY"), self.morning, self.cfg)
        self.assertIn("against_market_LONG_ONLY", picks[0].blockers)

    def test_a_stock_already_up_7_percent_is_not_bought(self):
        runner = series([0.25] * 40)  # +10% on the day
        picks = rank_picks([cand("RUN")], {"RUN": runner}, {"RUN": True},
                           Breadth(0.7, 10, "LONG_ONLY"), self.morning, self.cfg)
        self.assertTrue(any(b.startswith("already_moved_+10.0%") for b in picks[0].blockers), picks[0].blockers)
        gapped = replace(cand("UP"), gap_pct=12.0)  # NELCAST-style: the gap counts too
        picks = rank_picks([gapped], self.candles, self.sector, Breadth(0.7, 10, "LONG_ONLY"), self.morning, self.cfg)
        self.assertTrue(any(b.startswith("already_moved_") for b in picks[0].blockers))

    def test_entry_window(self):
        day = datetime(2026, 10, 7, tzinfo=IST)
        self.assertFalse(entry_window(day.replace(hour=9, minute=17), self.cfg)[0])
        self.assertEqual(entry_window(day.replace(hour=12, minute=30), self.cfg), (True, "", 10.0))
        self.assertFalse(entry_window(day.replace(hour=14, minute=50), self.cfg)[0])

    def test_tier3_and_weak_setups_never_trade(self):
        picks = rank_picks([cand("UP", tier=3), cand("UP", score=40.0)], self.candles, self.sector,
                           Breadth(0.5, 10, "NEUTRAL"), self.morning, self.cfg)
        self.assertEqual(len(picks), 1)
        self.assertFalse(picks[0].tradable)


def bars(rows, start=START + timedelta(minutes=60)):
    return [Candle(start + timedelta(minutes=i), o, h, l, c, 1000) for i, (o, h, l, c) in enumerate(rows)]


def record(side="LONG", trigger=100.0, stop=99.0, target=102.0, checkpoint=None, time_stop=None):
    timing = None
    if checkpoint or time_stop:
        timing = {"checkpoint_minutes": checkpoint, "checkpoint_price": trigger + (0.5 if side == "LONG" else -0.5),
                  "time_stop_minutes": time_stop}
    return {"event": "SIGNAL_READY", "trigger": trigger, "trigger_target": target, "exit_timing": timing,
            "selected": {"symbol": "X", "side": side, "stop": stop, "target": target, "tier": 1, "score": 70},
            "conviction": 72.0, "persistence": {"score": 75}, "scan_time": "2026-10-07T10:15:20+05:30",
            "selected_latest_candle": (START + timedelta(minutes=59)).isoformat()}


class GraderTests(unittest.TestCase):
    def test_win(self):
        r = grade_signals.simulate(record(), bars([(99.9, 100.1, 99.8, 100.0), (100.0, 102.2, 99.9, 102.0)]), 2)
        self.assertEqual((r["outcome"], r["r"]), ("WIN", 2.0))

    def test_not_filled(self):
        r = grade_signals.simulate(record(), bars([(99.5, 99.9, 99.4, 99.6), (99.6, 99.8, 99.3, 99.5)]), 2)
        self.assertEqual(r["outcome"], "NOT_FILLED")

    def test_stop_is_assumed_first_when_one_candle_touches_both(self):
        r = grade_signals.simulate(record(), bars([(99.9, 100.1, 99.8, 100.0), (100.0, 102.5, 98.5, 101.0)]), 2)
        self.assertEqual((r["outcome"], r["r"]), ("LOSS", -1.0))

    def test_checkpoint_exit_and_time_stop(self):
        flat = [(99.9, 100.1, 99.8, 100.0)] + [(100.0, 100.2, 99.9, 100.1)] * 10
        r = grade_signals.simulate(record(checkpoint=3), bars(flat), 2)
        self.assertEqual((r["outcome"], r["minutes"]), ("CHECKPOINT_EXIT", 3))
        self.assertAlmostEqual(r["r"], 0.1)
        rising = [(99.9, 100.1, 99.8, 100.0)] + [(100.0, 100.6, 99.9, 100.5)] * 10
        r = grade_signals.simulate(record(checkpoint=3, time_stop=6), bars(rising), 2)
        self.assertEqual((r["outcome"], r["minutes"]), ("TIME_STOP", 6))

    def test_short_side(self):
        r = grade_signals.simulate(record("SHORT", 100.0, 101.0, 98.0),
                                   bars([(100.1, 100.2, 99.9, 100.0), (100.0, 100.1, 97.8, 98.0)]), 2)
        self.assertEqual((r["outcome"], r["r"]), ("WIN", 2.0))

    def test_signals_are_deduplicated_and_journal_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "a.jsonl"
            rec = record()
            audit.write_text("\n".join(json.dumps({**rec, "ts": "x"}) for _ in range(3)) + "\nnot json\n")
            self.assertEqual(len(grade_signals.load_signals(audit, "2026-10-07")), 1)
            self.assertEqual(grade_signals.load_signals(audit, "2026-10-08"), [])
            journal = Path(tmp) / "j.jsonl"
            row = grade_signals.simulate(rec, bars([(99.9, 100.1, 99.8, 100.0)]), 2)
            grade_signals.merge_journal(journal, [row])
            grade_signals.merge_journal(journal, [row])
            self.assertEqual(len(journal.read_text().splitlines()), 1)


class ConfigTests(unittest.TestCase):
    def test_no_config_field_is_declared_twice(self):
        """A repeated dataclass field silently replaces the first one (min_persistence once did)."""
        import ast
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("config.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "StrategyConfig")
        names = [n.target.id for n in cls.body if isinstance(n, ast.AnnAssign)]
        self.assertEqual(len(names), len(set(names)), sorted(n for n in names if names.count(n) > 1))
        StrategyConfig().validate()


if __name__ == "__main__":
    unittest.main()
