"""The ten engines: positive textbook cases, exact LONG/SHORT symmetry, independently
recomputed key quantities, mandatory-gate mutations, distinctness, no look-ahead."""
from __future__ import annotations

import functools
import math
import unittest
from dataclasses import replace
from datetime import timedelta
from statistics import median

from strategies import indicators as ind
from strategies.core import (BELOW_THRESHOLD, INSUFFICIENT_DATA, LONG, NO_SETUP, OUTSIDE_WINDOW, SHORT,
                             SIGNAL, RiskConfig, SafetyConfig, round_to_tick, safe_tick)
from strategies.engines import ENGINE_CLASSES, ENGINE_NAMES, build_engines
from strategies.features import Features
from strategies.scanner import StockInput, run_scan
from strategy_930 import Candle
from tests import strategy_fixtures as fx

SYM = "TEST"


# The synthetic textbook scenarios are smooth, low-ATR price paths whose 2R targets are 6-10
# ATR away -- unreachable inside the live 30-minute hold. The engine-logic tests below therefore
# run with a whole-session horizon; the 30-minute horizon itself is tested in SafetyGates.
LOGIC_SAFETY = replace(SafetyConfig(), max_hold_minutes=24 * 60)


def scan(universe, priors, at, names=None, safety=LOGIC_SAFETY):
    engines = build_engines(names)
    for e in engines:
        e.safety = safety
    return run_scan(universe, at, engines, priors)


@functools.lru_cache(maxsize=None)
def scenario_report(name: str, mirrored: bool = False):
    u, priors, at = fx.scenario_universe(name, SYM)
    if mirrored:
        u = fx.mirror_universe(u, SYM)
        priors = {k: fx.mirror_prior(v) for k, v in priors.items()}
    return scan(u, priors, at)


def ev_of(report, name, sym=SYM):
    return report.evaluations[name][sym]


def bars_of(name):
    return fx.SCENARIOS[name]()[0]


class EveryEngine(unittest.TestCase):
    def test_ten_distinct_engines_registered(self):
        self.assertEqual(len(ENGINE_CLASSES), 10)
        self.assertEqual(len(set(ENGINE_NAMES)), 10)
        self.assertEqual(set(ENGINE_NAMES), set(fx.SCENARIOS))
        self.assertEqual(len({c.side for c in ENGINE_CLASSES}), 10)      # ten different algorithms

    def test_each_textbook_long_scenario_signals(self):
        for name in ENGINE_NAMES:
            with self.subTest(engine=name):
                ev = ev_of(scenario_report(name), name)
                self.assertEqual(ev.status, SIGNAL, ev.reason)
                sig = ev.signal
                self.assertGreaterEqual(sig.score, 90.0)
                self.assertEqual(sig.direction, LONG)
                self.assertTrue(sig.stop < sig.entry < sig.target)
                self.assertAlmostEqual(sum(c.weight for c in sig.components), 1.0)
                self.assertTrue(all(c.quality >= 0.5 for c in sig.components))
                self.assertEqual(sig.signal_time, sig.bar_time + timedelta(minutes=1))
                self.assertEqual(sig.valid_until, sig.signal_time + timedelta(minutes=3))
                self.assertTrue(sig.conditions)
                tick = safe_tick(sig.entry)
                for px in (sig.entry, sig.stop, sig.target):
                    self.assertAlmostEqual(px / tick, round(px / tick), places=6)
                if name != "Exhaustion_Mean_Reversion_Engine":
                    self.assertAlmostEqual(sig.reward_r, 2.0, delta=0.02)

    def test_mirror_image_gives_the_short_signal(self):
        for name in ENGINE_NAMES:
            with self.subTest(engine=name):
                lg = ev_of(scenario_report(name), name).signal
                ev = ev_of(scenario_report(name, mirrored=True), name)
                self.assertEqual(ev.status, SIGNAL, ev.reason)
                sh = ev.signal
                self.assertEqual(sh.direction, SHORT)
                self.assertTrue(sh.target < sh.entry < sh.stop)
                # Parkinson (log ranges) and a beta from a reflected market are not exactly
                # reflection-invariant; every other engine mirrors exactly.
                tol = 1.0 if name in ("Volatility_Compression_Expansion_Engine",
                                      "Relative_Strength_Weakness_Engine",
                                      "Regime_Filtered_ORB_Engine",
                                      "Trend_Pullback_Continuation_Engine") else 0.011
                self.assertAlmostEqual(sh.score, lg.score, delta=tol)
                if tol < 1:
                    self.assertAlmostEqual(sh.entry, fx.MIRROR - lg.entry, places=6)
                    self.assertAlmostEqual(sh.stop, fx.MIRROR - lg.stop, places=6)

    def test_each_scenario_fires_only_its_own_engine(self):
        for name in ENGINE_NAMES:
            with self.subTest(scenario=name):
                rep = scenario_report(name)
                fired = sorted(e for e in ENGINE_NAMES if ev_of(rep, e).status == SIGNAL)
                self.assertEqual(fired, [name])


def single(name, bars, pc=100.0, prior=None, n_filler=150, drift=0.0002, today_open=None):
    u = fx.filler_universe(n_filler, len(bars), drift=drift)
    u[SYM] = StockInput(SYM, tuple(bars), pc, today_open or bars[0].open)
    return ev_of(scan(u, {SYM: prior} if prior else {}, fx.scan_after(len(bars)), [name]), name)


def with_bar(bars, i, **changes):
    b = list(bars)
    b[i] = replace(b[i], **changes)
    return tuple(b)


class ShortAlertText(unittest.TestCase):
    def test_short_conditions_show_real_signs_and_ordered_ranges(self):
        import re
        n = "Order_Flow_Imbalance_Engine"
        sh = ev_of(scenario_report(n, mirrored=True), n).signal
        self.assertRegex(sh.conditions[0], r"sell imbalance OI10=-0\.\d+")
        n = "Volatility_Compression_Expansion_Engine"
        txt = ev_of(scenario_report(n, mirrored=True), n).signal.conditions[0]
        lo, hi = map(float, re.search(r"box ([\d.]+)-([\d.]+)", txt).groups())
        self.assertLess(lo, hi)


class OrderFlow(unittest.TestCase):
    N = "Order_Flow_Imbalance_Engine"

    def test_imbalance_recomputed_by_hand(self):
        bars = bars_of(self.N)
        c = [b.close for b in bars]
        t = len(c) - 1
        dps = [c[i] - c[i - 1] for i in range(t - 9 - 30, t - 9)]
        m = sum(dps) / 30
        sigma = math.sqrt(sum((d - m) ** 2 for d in dps) / 29)
        phi = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
        num = sum(bars[i].volume * (2 * phi((c[i] - c[i - 1]) / sigma) - 1) for i in range(t - 9, t + 1))
        oi = num / sum(b.volume for b in bars[t - 9:t + 1])
        comp = {x.name: x for x in ev_of(scenario_report(self.N), self.N).components}
        self.assertAlmostEqual(comp["imbalance"].raw, oi, places=12)
        self.assertAlmostEqual(comp["participation"].raw, 4.0)       # 40k vs median 10k

    def test_stop_entry_target_by_hand(self):
        bars = bars_of(self.N)
        f = Features(bars)
        sig = ev_of(scenario_report(self.N), self.N).signal
        self.assertEqual(sig.entry, round_to_tick(bars[-1].high + 0.01, 0.01, True))
        self.assertEqual(sig.stop, round_to_tick(min(b.low for b in bars[-5:]) - 0.1 * f.atr[-1], 0.01, False))
        self.assertEqual(sig.target, round_to_tick(sig.entry + 2 * (sig.entry - sig.stop), 0.01, False))

    def test_gates(self):
        bars = bars_of(self.N)
        weak = tuple(replace(b, volume=12_000) if i >= len(bars) - 10 else b for i, b in enumerate(bars))
        self.assertIn("volume_ratio", single(self.N, weak).reason)
        self.assertEqual(single(self.N, bars[:40]).status, OUTSIDE_WINDOW)  # 09:54 < 09:55 start


class Sweep(unittest.TestCase):
    N = "Liquidity_Sweep_Reclaim_Engine"

    def test_values_by_hand(self):
        bars = bars_of(self.N)
        f = Features(bars)
        comp = {x.name: x for x in ev_of(scenario_report(self.N), self.N).components}
        self.assertAlmostEqual(comp["penetration"].raw, (99.5 - 99.35) / f.atr[-1])
        self.assertAlmostEqual(comp["sweep_volume"].raw, 70_000 / 20_000)
        self.assertEqual(comp["level_significance"].raw, 0.9)            # the opening-range low
        sig = ev_of(scenario_report(self.N), self.N).signal
        self.assertEqual(sig.stop, round_to_tick(99.35 - 0.1 * f.atr[-1], 0.01, False))
        self.assertTrue(sig.anchor.startswith("ORL@99.50|sw"))

    def test_no_reclaim_no_setup(self):
        bars = bars_of(self.N)
        bad = with_bar(bars, -1, close=99.45, open=99.85, high=99.88, low=99.35)
        self.assertEqual(single(self.N, bad).status, NO_SETUP)

    def test_accepted_closes_belong_to_failed_breakout_not_sweep(self):
        fb = bars_of("Failed_Breakout_Breakdown_Engine")
        self.assertNotEqual(single(self.N, fb).status, SIGNAL)


class FailedBreakout(unittest.TestCase):
    N = "Failed_Breakout_Breakdown_Engine"

    def test_values_by_hand(self):
        comp = {x.name: x for x in ev_of(scenario_report(self.N), self.N).components}
        self.assertEqual(comp["acceptance"].raw, 3)
        self.assertAlmostEqual(comp["weak_break_volume"].raw, (16_000 + 15_000 + 15_000) / 3 / 20_000)
        self.assertAlmostEqual(comp["failure_volume"].raw, 45_000 / 20_000)
        self.assertEqual(comp["speed"].raw, 3)

    def test_single_close_beyond_is_not_acceptance(self):
        bars = list(bars_of(self.N))
        bars[-3] = replace(bars[-3], close=99.55, high=99.6)         # back above ORL after one close
        bars[-2] = replace(bars[-2], open=99.55, high=99.6)          # then a single close below again
        ev = single(self.N, tuple(bars))
        self.assertEqual(ev.status, NO_SETUP, ev.reason)


class FailedBreakoutDayLevel(unittest.TestCase):
    def test_session_low_level_is_used(self):
        # regression: the DL level used to be created with since=b and was always skipped
        t = fx.Tape(100.0)
        t.flat(15, 100.0, amp=0.1, v=30_000, wick=0.15)
        t.flat(5, 99.6, amp=0.1, v=20_000, wick=0.1)
        t.bar(99.3, v=20_000, up=0.05, l=99.0)               # day low 99.00 (below the ORL)
        t.flat(15, 99.6, amp=0.12, v=20_000, wick=0.12)
        t.bar(98.88, v=16_000, up=0.05, dn=0.05)
        t.bar(98.85, v=15_000, up=0.05, dn=0.10)
        t.bar(98.87, v=15_000, up=0.05, dn=0.05)
        t.bar(99.35, v=45_000, up=0.02, dn=0.03)
        ev = single("Failed_Breakout_Breakdown_Engine", t.done(), pc=99.6)
        self.assertEqual(ev.status, SIGNAL, ev.reason)
        self.assertTrue(ev.signal.anchor.startswith("DL@99.00"))


class TrendPullback(unittest.TestCase):
    N = "Trend_Pullback_Continuation_Engine"

    def test_values_by_hand(self):
        bars = bars_of(self.N)
        comp = {x.name: x for x in ev_of(scenario_report(self.N), self.N).components}
        t = len(bars) - 1
        sh = max(b.high for b in bars[t - 29:t + 1])
        p = max(i for i in range(t - 29, t + 1) if bars[i].high == sh)
        a_val = min(b.low for b in bars[p - 30:p])
        pl = min(b.low for b in bars[p + 1:t + 1])
        self.assertAlmostEqual(comp["depth"].raw, (sh - pl) / (sh - a_val))
        a = max(i for i in range(p - 30, p) if bars[i].low == a_val)
        imp = sum(b.volume for b in bars[a:p + 1]) / (p + 1 - a)
        self.assertAlmostEqual(comp["pullback_dryness"].raw, 8_000 / imp)

    def test_market_against_blocks(self):
        bars = bars_of(self.N)
        ev = single(self.N, bars, drift=-0.0004)                     # falling market
        self.assertEqual(ev.status, NO_SETUP)
        self.assertIn("market_against", ev.reason)

    def test_needs_market_context(self):
        bars = bars_of(self.N)
        ev = single(self.N, bars, n_filler=20)                       # 21 stocks: no market
        self.assertEqual(ev.status, INSUFFICIENT_DATA)


class RelativeStrength(unittest.TestCase):
    N = "Relative_Strength_Weakness_Engine"

    def test_percentiles_and_ties(self):
        from strategies.engines.relative_strength import _percentiles
        self.assertEqual(_percentiles({"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0}),
                         {"a": 0.0, "b": 0.5, "c": 0.5, "d": 1.0})

    def test_beta_and_excess_return_by_hand(self):
        u, priors, at = fx.scenario_universe(self.N, SYM)
        rep = scan(u, priors, at, [self.N])
        eng = build_engines([self.N])[0]
        eng.prepare(rep.views, rep.features, rep.context)
        rec = eng._rs[SYM]
        f = rep.features[SYM]
        xs, ys = [], []
        for i in range(1, f.n):
            xs.append(rep.context.minute_returns[f.ts[i]])
            ys.append(f.c[i] / f.c[i - 1] - 1)
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        bh = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        self.assertAlmostEqual(rec.beta, min(3, max(0, 1 / 3 + 2 / 3 * bh)))
        m_open = math.prod(1 + rep.context.minute_returns[f.ts[i]] for i in range(1, f.n)) - 1
        self.assertAlmostEqual(rec.rs_open, (f.c[-1] / f.c[0] - 1) - rec.beta * m_open)
        self.assertGreaterEqual(rec.p_open, 0.95)

    def test_too_few_stocks_to_rank(self):
        ev = single(self.N, bars_of(self.N), n_filler=60)
        self.assertEqual(ev.status, INSUFFICIENT_DATA)


class Compression(unittest.TestCase):
    N = "Volatility_Compression_Expansion_Engine"

    def test_compression_ratio_by_independent_parkinson(self):
        bars = bars_of(self.N)
        h, l = [b.high for b in bars], [b.low for b in bars]
        e = len(bars) - 2
        b0 = e - 15
        box = ind.parkinson_sigma(h[b0:e], l[b0:e])
        base = median(ind.parkinson_sigma(h[i:i + 15], l[i:i + 15]) for i in range(15, b0 - 15 + 1))
        comp = {x.name: x for x in ev_of(scenario_report(self.N), self.N).components}
        self.assertAlmostEqual(comp["compression"].raw, box / base, places=9)
        sig = ev_of(scenario_report(self.N), self.N).signal
        mid = (max(h[b0:e]) + min(l[b0:e])) / 2
        self.assertEqual(sig.stop, round_to_tick(mid, 0.01, False))

    def test_expansion_without_volume_is_not_confirmed(self):
        bars = bars_of(self.N)
        weak = with_bar(bars, -2, volume=15_000)
        self.assertIn("expansion_volume", single(self.N, weak).reason)


class ORB(unittest.TestCase):
    N = "Regime_Filtered_ORB_Engine"

    def test_rvol_and_stop_by_hand(self):
        bars, _, _ = fx.sc_orb()
        sig = ev_of(scenario_report(self.N), self.N).signal
        comp = {x.name: x for x in sig.components}
        self.assertAlmostEqual(comp["in_play"].raw, sum(b.volume for b in bars[:15]) / 300_000)
        self.assertEqual(sig.stop, round_to_tick(min(b.low for b in bars[:15]), 0.01, False))
        self.assertEqual(sig.anchor, "ORB20261013")

    def test_yahoo_volume_baseline_cannot_carry_90(self):
        from strategies.features import PriorLevels
        bars, pc, _ = fx.sc_orb()
        ev = single(self.N, bars, pc, PriorLevels("2026-10-12", None, None, None, 300_000.0, 14, "yahoo"))
        self.assertNotEqual(ev.status, SIGNAL)
        self.assertLessEqual(ev.score or 0, 87.5)

    def test_proxy_without_history_cannot_reach_90(self):
        bars, pc, _ = fx.sc_orb()
        big = tuple(replace(b, volume=b.volume * 50) for b in bars)  # top opening turnover
        ev = single(self.N, big, pc)
        self.assertIn(ev.status, (BELOW_THRESHOLD, NO_SETUP))
        self.assertLessEqual(ev.score or 0, 87.5)

    def test_market_against_and_window(self):
        bars, pc, prior = fx.sc_orb()
        self.assertIn("market_against", single(self.N, bars, pc, prior, drift=-0.0004).reason)
        late = list(bars)
        while len(late) < 140:
            late.append(Candle(fx.ts(len(late)), 100.5, 100.55, 100.45, 100.5, 30_000))
        self.assertEqual(single(self.N, tuple(late), pc, prior).status, OUTSIDE_WINDOW)


class VWAPReclaim(unittest.TestCase):
    N = "VWAP_Reclaim_Rejection_Engine"

    def test_retest_precision_by_hand(self):
        bars = bars_of(self.N)
        f = Features(bars)
        r = len(bars) - 2
        comp = {x.name: x for x in ev_of(scenario_report(self.N), self.N).components}
        self.assertAlmostEqual(comp["retest_precision"].raw, (bars[r].low - f.vwap[r]) / f.atr[r])

    def test_losing_vwap_after_the_cross_invalidates(self):
        bars = bars_of(self.N)
        f = Features(bars)
        bad = with_bar(bars, -3, close=f.vwap[-3] - 0.3, low=f.vwap[-3] - 0.35)
        self.assertEqual(single(self.N, bad, 101.0).status, NO_SETUP)


class Exhaustion(unittest.TestCase):
    N = "Exhaustion_Mean_Reversion_Engine"

    def test_target_is_vwap_and_stretch_by_hand(self):
        bars = bars_of(self.N)
        f = Features(bars)
        sig = ev_of(scenario_report(self.N), self.N).signal
        self.assertEqual(sig.target, round_to_tick(f.vwap[-1], 0.01, False))
        x = len(bars) - 3
        comp = {c.name: c for c in sig.components}
        self.assertAlmostEqual(comp["stretch"].raw, (f.vwap[x] - bars[x].low) / f.vwap_sd[x])
        self.assertGreaterEqual(sig.reward_r, 1.5)

    def test_no_climax_volume_no_trade(self):
        bars = bars_of(self.N)
        quiet = with_bar(with_bar(bars, -3, volume=90_000), -4, volume=20_000)
        self.assertIn("climax_volume", single(self.N, quiet).reason)


class Structural(unittest.TestCase):
    N = "Structural_Break_Retest_Engine"

    def test_values_by_hand(self):
        sig = ev_of(scenario_report(self.N), self.N).signal
        comp = {c.name: c for c in sig.components}
        self.assertEqual(comp["prior_respect"].raw, 3)
        self.assertAlmostEqual(comp["break_volume"].raw, 70_000 / 20_000)
        self.assertTrue(sig.anchor.startswith("PDH@101.00|b"))

    def test_without_prior_levels_no_pdh_trade(self):
        bars, pc, _ = fx.sc_structural()
        self.assertNotEqual(single(self.N, bars, pc, None).status, SIGNAL)

    def test_failed_break_is_not_a_retest(self):
        bars, pc, prior = fx.sc_structural()
        bad = with_bar(bars, -2, close=100.85, low=100.8, open=101.55, high=101.6)
        self.assertEqual(single(self.N, bad, pc, prior).status, NO_SETUP)


class SafetyGates(unittest.TestCase):
    OFI = "Order_Flow_Imbalance_Engine"

    def test_aartipharm_pattern_long_on_a_red_day_is_blocked(self):
        bars, _, _ = fx.sc_order_flow()              # LONG burst; open 102, now ~101.1
        ev = single(self.OFI, bars, pc=103.2)        # -2.0% vs previous close, -0.9% vs open
        self.assertEqual(ev.status, NO_SETUP)
        self.assertIn("against_the_day", ev.reason)

    def test_genuine_reversal_from_the_open_is_allowed(self):
        bars, _, _ = fx.sc_order_flow()
        ev = single(self.OFI, bars, pc=103.2, today_open=99.9)   # red on the day but +1.2% from open
        self.assertEqual(ev.status, SIGNAL, ev.reason)

    def test_against_the_market_is_blocked(self):
        bars, _, _ = fx.sc_order_flow()
        ev = single(self.OFI, bars, drift=-0.0004)
        self.assertIn("against_the_market", ev.reason)

    def test_news_or_circuit_move_is_blocked(self):
        bars, _, _ = fx.sc_order_flow()
        self.assertIn("news_or_circuit_risk", single(self.OFI, bars, pc=92.0).reason)

    def test_signal_shows_day_and_market_context(self):
        sig = ev_of(scenario_report(self.OFI), self.OFI).signal
        self.assertTrue(any(c.startswith("context: day") for c in sig.conditions))

    def test_fades_blocked_on_big_moves_and_labelled(self):
        n = "Exhaustion_Mean_Reversion_Engine"
        bars, _, _ = fx.sc_exhaustion()
        self.assertIn("fade_blocked_day_move", single(n, bars, pc=102.0).reason)      # -5.3% day
        sig = ev_of(scenario_report(n), n).signal
        self.assertIn("counter-trend setup by design", sig.conditions[-1])

    def test_compression_rejects_shock_candles_and_chasing(self):
        n = "Volatility_Compression_Expansion_Engine"
        bars = list(fx.sc_compression()[0])
        e = bars[-2]
        shock = list(bars)
        shock[-2] = replace(e, high=e.high + 3.0, close=e.high + 2.9)
        shock[-1] = replace(bars[-1], open=e.high + 2.9, high=e.high + 3.0, low=e.high + 2.8, close=e.high + 2.95)
        self.assertIn("shock_candle", single(n, tuple(shock)).reason)
        chase = list(bars)
        c = bars[-1]
        chase[-1] = replace(c, high=c.high + 0.6, close=c.high + 0.55)
        self.assertIn("chasing", single(n, tuple(chase)).reason)

    def test_unreachable_target_late_in_the_day(self):
        from strategies.core import Trade
        eng = build_engines([self.OFI])[0]
        bars = tuple(Candle(fx.ts(i), 100, 100.05, 99.95, 100, 1000) for i in range(296))  # last 14:10
        f = Features(bars)
        self.assertAlmostEqual(f.atr[-1], 0.1)
        trade = lambda move: Trade(LONG, 100.0, 100.0 - move / 2, 100.0 + move, move / 2, 0.5, 2.0, 0.2)
        # 30-minute hold -> reach = 0.1 * sqrt(30) = 0.548
        self.assertIsNone(eng.reachable(trade(0.54), f, bars[-1].ts))
        self.assertEqual(eng.reachable(trade(0.56), f, bars[-1].ts), "target_unreachable_5.6ATR_in_30min")
        # whole-session horizon: deadline 14:14 -> 61 minutes to 15:15 -> reach = 0.1 * sqrt(61) = 0.78
        eng.safety = LOGIC_SAFETY
        self.assertIsNone(eng.reachable(trade(0.7), f, bars[-1].ts))
        self.assertIn("target_unreachable", eng.reachable(trade(1.0), f, bars[-1].ts))
        # near the close 15:15 binds before the 30 minutes: last bar 14:50 -> deadline 14:54 -> 21 min
        late = tuple(Candle(fx.ts(i), 100, 100.05, 99.95, 100, 1000) for i in range(336))
        eng.safety = SafetyConfig()
        self.assertEqual(eng.reachable(trade(0.5), Features(late), late[-1].ts), "target_unreachable_5.0ATR_in_21min")

    def test_textbook_targets_too_far_for_a_30_minute_hold_are_rejected(self):
        for n in ("Trend_Pullback_Continuation_Engine", "Volatility_Compression_Expansion_Engine",
                  "Regime_Filtered_ORB_Engine"):
            u, priors, at = fx.scenario_universe(n, SYM)
            ev = ev_of(scan(u, priors, at, [n], safety=SafetyConfig()), n)
            self.assertEqual(ev.status, NO_SETUP)
            self.assertIn("target_unreachable", ev.reason)
            self.assertIn("in_30min", ev.reason)

    def test_no_new_entries_after_1415_and_orb_window(self):
        for c in ENGINE_CLASSES:
            self.assertLessEqual(c.LATEST, __import__("datetime").time(14, 15))
        from strategies.engines.regime_orb import RegimeFilteredORBEngine
        self.assertEqual(RegimeFilteredORBEngine.LATEST.strftime("%H:%M"), "09:59")

    def test_thin_stocks_get_adverse_slippage(self):
        eng = build_engines([self.OFI])[0]
        thin = Features(tuple(Candle(fx.ts(i), 100, 100.1, 99.9, 100, 5_000) for i in range(40)))   # 5 lakh/min
        deep = Features(tuple(Candle(fx.ts(i), 100, 100.1, 99.9, 100, 50_000) for i in range(40)))  # 50 lakh/min
        self.assertEqual(eng.risk_for(thin).cost_slippage_bps, 5.0)
        self.assertEqual(eng.risk_for(deep).cost_slippage_bps, 2.0)

    def test_lunch_volume_floor(self):
        vols = [100_000] * 90 + [10_000] * 40          # busy morning, dead lunch
        f = Features(tuple(Candle(fx.ts(i), 100, 100.1, 99.9, 100, v) for i, v in enumerate(vols)))
        self.assertEqual(f.med_volume(130, 20), 60_000)  # 0.6 x 100k day median, not the 10k lunch median
        early = Features(tuple(Candle(fx.ts(i), 100, 100.1, 99.9, 100, 1000 + i) for i in range(30)))
        self.assertEqual(early.med_volume(30, 20), median(1000 + i for i in range(10, 30)))


class NoLookAheadAndDeterminism(unittest.TestCase):
    def test_future_bars_never_change_a_decision(self):
        from strategy_engines import series_inputs

        class Ser:
            def __init__(self, bars, pc, to):
                self.ts, self.o, self.h = [b.ts for b in bars], [b.open for b in bars], [b.high for b in bars]
                self.l, self.c, self.v = [b.low for b in bars], [b.close for b in bars], [b.volume for b in bars]
                self.previous_close, self.today_open = pc, to

            def __len__(self):
                return len(self.ts)

        class Raw:
            pass
        u, priors, at = fx.scenario_universe("Structural_Break_Retest_Engine", SYM)
        base = scan(u, priors, at)
        # the same session continuing for 40 more (wild) minutes, cut at the same scan time
        stocks = {}
        for s, si in u.items():
            extra = [Candle(fx.ts(len(si.bars) + k), 50.0, 400.0, 10.0, 300.0, 9e9) for k in range(40)]
            stocks[s] = Ser(si.bars + tuple(extra), si.previous_close, si.today_open)
        raw = Raw()
        raw.stocks, raw.unparseable, raw.session_date = stocks, {}, fx.DAY.date()
        cut = series_inputs(raw, at)
        again = scan(cut, priors, at)
        self.assertEqual([s.key for s in base.signals], [s.key for s in again.signals])
        self.assertEqual(base.signals, again.signals)

    def test_identical_inputs_identical_outputs_any_order(self):
        u, priors, at = fx.scenario_universe("Liquidity_Sweep_Reclaim_Engine", SYM)
        a = scan(u, priors, at)
        b = scan(dict(reversed(list(u.items()))), priors, at)
        self.assertEqual(a.signals, b.signals)
        for e in ENGINE_NAMES:
            self.assertEqual({s: (v.status, v.score) for s, v in a.evaluations[e].items()},
                             {s: (v.status, v.score) for s, v in b.evaluations[e].items()})


if __name__ == "__main__":
    unittest.main()
