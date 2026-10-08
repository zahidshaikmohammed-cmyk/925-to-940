"""SENSEX expiry engine: data, candles, features, regime, setups, options, costs, risk,
positions, look-ahead and backtest-integrity tests."""
import math
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta

from sensex_expiry import backtest as bt
from sensex_expiry.candles import CandleBuilder, resample
from sensex_expiry.config import ABLATIONS, EngineConfig, ablate
from sensex_expiry.costs import round_trip
from sensex_expiry.engine import StrategyEngine
from sensex_expiry.features import atr, opening_range, swings
from sensex_expiry.models import IST, Action, Candle, DataQuality, Direction, OptionQuote, PriorDay, Reason, Regime, Tick
from sensex_expiry.options import choose_strike, parse_dhan_chain, premium_stop, tradability
from sensex_expiry.position import manage, open_position
from sensex_expiry.quality import TickValidator, assess
from sensex_expiry.regime import classify
from sensex_expiry.risk import DailyRiskState, pre_trade_checks, size_position
from sensex_expiry.session_calendar import is_expiry_day, weekly_expiry_for
from sensex_expiry.setups import detect
from sensex_expiry.features import snapshot
from sensex_expiry.synthetic import make_day

DAY = date(2026, 10, 8)            # a Thursday
OPEN = datetime(2026, 10, 8, 9, 15, tzinfo=IST)
PRIOR = PriorDay(date(2026, 10, 7), 81200.0, 80800.0, 81000.0)
CFG = EngineConfig()


def bar(i, o, h, lo, c, start=OPEN):
    return Candle(start + timedelta(minutes=i), o, h, lo, c)


def flat_bars(n, base=81020.0):
    out = []
    for i in range(n):
        b = base + (5 if i % 2 else -5)
        out.append(bar(i, b, b + 10, b - 10, b))
    return out


def sweep_day():
    """ORL = 81005. Bar 40 sweeps it, bar 42 reclaims with displacement, bar 43 triggers."""
    c = flat_bars(40)
    c += [bar(40, 81020, 81022, 80990, 80995), bar(41, 80995, 81000, 80985, 80998),
          bar(42, 80998, 81040, 80995, 81035), bar(43, 81035, 81060, 81030, 81055),
          bar(44, 81055, 81070, 81050, 81065)]
    return c


def mirror_about(c, pivot=81000.0):
    return [Candle(x.start, 2 * pivot - x.open, 2 * pivot - x.low, 2 * pivot - x.high, 2 * pivot - x.close) for x in c]


MIRROR_PRIOR = PriorDay(PRIOR.day, 2 * 81000 - PRIOR.low, 2 * 81000 - PRIOR.high, 2 * 81000 - PRIOR.close)


def good_quality():
    return assess(CFG.data, OPEN, [], None, OPEN, backtest=True)


class TestConfig(unittest.TestCase):
    def test_default_validates_and_hash_is_stable(self):
        CFG.validate()
        self.assertEqual(CFG.config_hash(), EngineConfig().config_hash())

    def test_any_rule_change_changes_hash_but_capital_does_not(self):
        h = CFG.config_hash()
        self.assertNotEqual(h, replace(CFG, setups=replace(CFG.setups, sweep_min_pen_atr=0.11)).config_hash())
        self.assertEqual(h, replace(CFG, risk=replace(CFG.risk, capital=1e7)).config_hash())

    def test_risk_above_one_percent_rejected(self):
        with self.assertRaises(ValueError):
            replace(CFG, risk=replace(CFG.risk, risk_per_trade_pct=0.02)).validate()

    def test_every_ablation_builds(self):
        for name in ABLATIONS:
            ablate(CFG, name).validate()


class TestCalendar(unittest.TestCase):
    def test_conventions(self):
        self.assertEqual(weekly_expiry_for(date(2026, 10, 6)), date(2026, 10, 8))      # Thursday now
        self.assertEqual(weekly_expiry_for(date(2025, 3, 5)), date(2025, 3, 4))        # Tuesday in 2025
        self.assertEqual(weekly_expiry_for(date(2024, 6, 12)), date(2024, 6, 14))      # Friday in 2024

    def test_holiday_moves_expiry_earlier(self):
        self.assertEqual(weekly_expiry_for(date(2026, 5, 25), frozenset({date(2026, 5, 28)})), date(2026, 5, 27))

    def test_broker_rule_conflict_is_not_expiry(self):
        self.assertEqual(is_expiry_day(DAY, broker_expiries=frozenset({DAY})), (True, "BROKER"))
        self.assertEqual(is_expiry_day(DAY, broker_expiries=frozenset()), (False, "CONFLICT"))


class TestCandles(unittest.TestCase):
    def tick(self, sec, px, vol=None):
        ts = OPEN + timedelta(seconds=sec)
        return Tick("51", ts, px, ts, volume=vol)

    def test_minute_boundaries_and_ohlc(self):
        b = CandleBuilder()
        for s, p in ((0, 100), (20, 105), (40, 95), (59.9, 101)):
            self.assertEqual(b.on_tick(self.tick(s, p)), [])
        closed = b.on_tick(self.tick(60, 102))
        self.assertEqual(len(closed), 1)
        c = closed[0]
        self.assertEqual((c.start, c.open, c.high, c.low, c.close, c.ticks), (OPEN, 100, 105, 95, 101, 4))

    def test_late_tick_never_rewrites_history(self):
        b = CandleBuilder()
        b.on_tick(self.tick(0, 100))
        b.on_tick(self.tick(61, 101))
        self.assertEqual(b.on_tick(self.tick(30, 999)), [])
        self.assertEqual(b.late_ticks, 1)
        self.assertEqual(b.closed[0].high, 100)

    def test_gap_reported_not_filled(self):
        b = CandleBuilder()
        b.on_tick(self.tick(0, 100))
        b.on_tick(self.tick(185, 101))     # minutes 1 and 2 have no ticks
        self.assertEqual(len(b.closed), 1)
        self.assertEqual(b.gaps, [OPEN + timedelta(minutes=1), OPEN + timedelta(minutes=2)])

    def test_clock_closes_after_grace_only(self):
        b = CandleBuilder(grace_s=2)
        b.on_tick(self.tick(5, 100))
        self.assertEqual(b.on_clock(OPEN + timedelta(seconds=61)), [])
        self.assertEqual(len(b.on_clock(OPEN + timedelta(seconds=62))), 1)

    def test_volume_is_differenced(self):
        b = CandleBuilder()
        b.on_tick(self.tick(0, 100, 1000))
        b.on_tick(self.tick(30, 100, 1300))
        b.on_tick(self.tick(60, 100, 1500))
        b.on_tick(self.tick(120, 100, 1600))
        self.assertEqual([c.volume for c in b.closed], [300, 200])

    def test_resample_emits_only_complete_buckets(self):
        c = flat_bars(12)
        five = resample(c, 5, OPEN)
        self.assertEqual(len(five), 2)          # 09:15 and 09:20; 09:25 bucket has 2 of 5 bars
        self.assertEqual(five[0].high, max(x.high for x in c[:5]))


class TestQuality(unittest.TestCase):
    def test_duplicate_out_of_order_and_jump(self):
        v = TickValidator(CFG.data)
        t0 = Tick("51", OPEN, 81000, OPEN)
        self.assertTrue(v.accept(t0))
        self.assertFalse(v.accept(t0))
        self.assertFalse(v.accept(Tick("51", OPEN - timedelta(seconds=1), 81001, OPEN)))
        self.assertFalse(v.accept(Tick("51", OPEN + timedelta(seconds=1), 82000, OPEN), atr=20))
        self.assertEqual((v.duplicates, v.out_of_order, v.rejected_jumps), (1, 1, 1))
        self.assertFalse(v.accept(Tick("51", OPEN + timedelta(seconds=2), float("nan"), OPEN)))

    def test_stale_live_data_is_bad(self):
        c = flat_bars(5)
        now = OPEN + timedelta(minutes=5)
        q = assess(CFG.data, now, c, now - timedelta(seconds=10), OPEN)
        self.assertEqual(q.quality, DataQuality.BAD)
        self.assertIn(Reason.DATA_STALE, q.reasons)
        q = assess(CFG.data, now, c, now - timedelta(milliseconds=500), OPEN)
        self.assertEqual(q.quality, DataQuality.GOOD)

    def test_invalid_candle_and_missing_minutes(self):
        c = flat_bars(5) + [Candle(OPEN + timedelta(minutes=5), 10, 9, 11, 10)]
        self.assertEqual(assess(CFG.data, OPEN, c, None, OPEN, backtest=True).quality, DataQuality.BAD)
        holes = flat_bars(3) + [bar(10, 81000, 81010, 80990, 81000)]
        self.assertEqual(assess(CFG.data, OPEN, holes, None, OPEN, backtest=True).quality, DataQuality.BAD)

    def test_disconnected_feed_is_bad(self):
        q = assess(CFG.data, OPEN, flat_bars(3), OPEN, OPEN, feed_connected=False)
        self.assertEqual(q.quality, DataQuality.BAD)


class TestFeatures(unittest.TestCase):
    def test_atr_constant_range(self):
        c = [bar(i, 100, 110, 90, 100) for i in range(20)]
        self.assertAlmostEqual(atr(c, 14), 20.0)

    def test_opening_range_hidden_until_complete(self):
        c = flat_bars(14)
        self.assertIsNone(opening_range(c, OPEN, 15))
        self.assertIsNotNone(opening_range(flat_bars(15), OPEN, 15))

    def test_swings_are_causal(self):
        day = make_day(DAY, seed=4)
        full = swings(day.underlying, 3)
        for t in (60, 150, 300):
            part = swings(day.underlying[:t + 1], 3)
            self.assertTrue(all(s.confirmed_at <= t for s in part))
            self.assertEqual(part, [s for s in full if s.confirmed_at <= t])


class TestSetups(unittest.TestCase):
    def test_sweep_reclaim_long_fires_once_on_trigger_bar(self):
        c = sweep_day()
        fired = {t: detect(c[:t + 1], OPEN, PRIOR, CFG) for t in range(38, 45)}
        hits = {t: v for t, v in fired.items() if v}
        self.assertEqual(list(hits), [43])
        cand = hits[43][0]
        self.assertEqual((cand.setup, cand.direction, cand.level_name), ("S1_SWEEP_RECLAIM", Direction.LONG, "ORL"))
        self.assertLess(cand.invalidation, 80985)

    def test_mirror_gives_short(self):
        m = mirror_about(sweep_day())
        hits = [t for t in range(38, 45) if detect(m[:t + 1], OPEN, MIRROR_PRIOR, CFG)]
        self.assertEqual(hits, [43])
        cand = detect(m[:44], OPEN, MIRROR_PRIOR, CFG)[0]
        self.assertEqual((cand.direction, cand.level_name), (Direction.SHORT, "ORH"))
        self.assertGreater(cand.invalidation, 2 * 81000 - 80985)

    def test_too_deep_is_a_breakdown_not_a_sweep(self):
        c = sweep_day()
        c[41] = bar(41, 80995, 81000, 80900, 80998)        # 105 points below the level: > 1.5 ATR
        c[42] = bar(42, 80998, 81040, 80995, 81035)
        self.assertFalse(any(detect(c[:t + 1], OPEN, PRIOR, CFG) for t in range(40, 45)))

    def test_no_reclaim_no_signal(self):
        c = sweep_day()[:42] + [bar(42, 80998, 81004, 80990, 81000), bar(43, 81000, 81003, 80995, 81002)]
        self.assertFalse(any(detect(c[:t + 1], OPEN, PRIOR, CFG) for t in range(40, 44)))

    def test_orb_acceptance_and_retest(self):
        c = flat_bars(20)          # ORH = 81035
        c += [bar(20, 81030, 81070, 81028, 81065), bar(21, 81065, 81080, 81060, 81075),
              bar(22, 81075, 81078, 81040, 81050), bar(23, 81050, 81090, 81048, 81085)]
        res = detect(c, OPEN, PRIOR, CFG)
        self.assertEqual([(x.setup, x.direction) for x in res], [("S2_ORB_ACCEPT", Direction.LONG)])
        self.assertFalse(detect(c[:23], OPEN, PRIOR, CFG))

    def test_orb_blocked_after_cutoff(self):
        late = OPEN + timedelta(hours=3)      # 12:15
        c = [Candle(x.start + timedelta(hours=3), x.open, x.high, x.low, x.close) for x in flat_bars(24)]
        self.assertTrue(all(x.start > late - timedelta(minutes=1) for x in c[:1]))
        self.assertFalse([x for x in detect(c, OPEN, PRIOR, CFG) if x.setup == "S2_ORB_ACCEPT"])


class TestRegime(unittest.TestCase):
    def test_trend_and_range(self):
        up = []
        p = 81000.0
        for i in range(90):
            step = 15 if i % 9 < 6 else -6          # six bars up, three down: real swing points
            up.append(bar(i, p, max(p, p + step) + 4, min(p, p + step) - 4, p + step))
            p += step
        snap = snapshot(up, OPEN, PRIOR, CFG.features)
        self.assertIn(classify(up, snap, PRIOR, CFG.features), (Regime.STRONG_BULL, Regime.EXPANSION))
        flat = flat_bars(60)
        self.assertIn(classify(flat, snapshot(flat, OPEN, PRIOR, CFG.features), PRIOR, CFG.features),
                      (Regime.RANGE, Regime.COMPRESSION))

    def test_too_few_bars_is_unclear(self):
        c = flat_bars(10)
        self.assertEqual(classify(c, snapshot(c, OPEN, PRIOR, CFG.features), PRIOR, CFG.features), Regime.UNCLEAR)


class TestOptionsAndCosts(unittest.TestCase):
    def test_strike_selection(self):
        o = CFG.options
        self.assertEqual(choose_strike(81049, Direction.LONG, o), (81000, "CE"))
        self.assertEqual(choose_strike(81051, Direction.SHORT, o), (81100, "PE"))
        itm = replace(o, moneyness=1)
        self.assertEqual(choose_strike(81000, Direction.LONG, itm), (80900, "CE"))
        self.assertEqual(choose_strike(81000, Direction.SHORT, itm), (81100, "PE"))

    def test_premium_stop_bounds(self):
        p = premium_stop(100, 20, 0.5, CFG.options)                 # 0.5*20*1.2 = 12 -> 12%
        self.assertAlmostEqual(p.stop, 88.0)
        self.assertEqual(premium_stop(100, 2, 0.5, CFG.options).stop, 90.0)   # floored at 10%
        wide = premium_stop(100, 200, 0.5, CFG.options)
        self.assertIn(Reason.STOP_TOO_WIDE, wide.reasons)

    def test_tradability(self):
        q = OptionQuote(81000, "CE", 100, 99.9, 100.1, OPEN, top5_ask_qty=1000)
        self.assertEqual(tradability(q, 60, CFG.options), [])
        wide = OptionQuote(81000, "CE", 100, 95, 105, OPEN)
        self.assertIn(Reason.SPREAD_TOO_WIDE, tradability(wide, 60, CFG.options))
        cheap = OptionQuote(81000, "CE", 5, 4.95, 5.05, OPEN)
        self.assertIn(Reason.PREMIUM_TOO_LOW, tradability(cheap, 60, CFG.options))
        thin = OptionQuote(81000, "CE", 100, 99.9, 100.1, OPEN, top5_ask_qty=100)
        self.assertIn(Reason.DEPTH_TOO_THIN, tradability(thin, 60, CFG.options))

    def test_chain_parsing_good_and_bad(self):
        payload = {"status": "success", "data": {"last_price": 81012.5, "oc": {
            "81000.000000": {"ce": {"last_price": 120.5, "top_bid_price": 120.4, "top_ask_price": 120.6, "oi": 5000,
                                    "volume": 900, "implied_volatility": 13.1, "greeks": {"delta": 0.52}},
                             "pe": {"last_price": 0}}}}}
        spot, ch = parse_dhan_chain(payload, OPEN)
        self.assertEqual(spot, 81012.5)
        self.assertEqual(list(ch), [(81000, "CE")])
        self.assertEqual(ch[(81000, "CE")].delta, 0.52)
        self.assertEqual(parse_dhan_chain({"status": "failure"}, OPEN), (None, {}))
        self.assertEqual(parse_dhan_chain({"data": {"oc": "garbage"}}, OPEN), (None, {}))

    def test_round_trip_costs(self):
        c = round_trip(100, 120, 100, CFG.costs)
        self.assertAlmostEqual(c.brokerage, 40)
        self.assertAlmostEqual(c.stt, 120 * 100 * 0.0015)
        self.assertAlmostEqual(c.stamp, 100 * 100 * 0.00003)
        self.assertAlmostEqual(c.gst, (40 + 22000 * 0.000325 + 22000 * 0.000001) * 0.18)


class TestRisk(unittest.TestCase):
    def test_size_floors_and_never_rounds_up(self):
        s = size_position(CFG, 100.0, 70.0, 20)              # budget 2500, ~30.2*20+costs per lot
        self.assertGreaterEqual(s.lots, 1)
        self.assertLessEqual(s.one_r, CFG.risk.capital * CFG.risk.risk_per_trade_pct)
        tiny = size_position(replace(CFG, risk=replace(CFG.risk, capital=50_000)), 100.0, 70.0, 20)
        self.assertEqual(tiny.lots, 0)
        self.assertIn(Reason.RISK_TOO_HIGH, tiny.reasons)

    def test_outlay_capped_at_three_r_budget(self):
        s = size_position(CFG, 100.0, 95.0, 20)               # tight stop would allow many lots
        budget = CFG.risk.capital * CFG.risk.risk_per_trade_pct
        self.assertLessEqual(s.outlay, CFG.options.max_outlay_r * budget + 1e-6)

    def test_pre_trade_rules(self):
        st = DailyRiskState("d")
        now = OPEN + timedelta(hours=2)
        self.assertEqual(pre_trade_checks(CFG, st, now, "k"), [])
        st.record_exit(-1.0, now - timedelta(minutes=1))
        self.assertIn(Reason.COOLDOWN, pre_trade_checks(CFG, st, now, "k"))
        st.record_exit(-1.0, now - timedelta(minutes=10))
        r = pre_trade_checks(CFG, st, now, "k")
        self.assertIn(Reason.CONSECUTIVE_LOSSES, r)
        self.assertIn(Reason.DAILY_LOSS_LIMIT, r)
        st2 = DailyRiskState("d", traded_keys={"k"}, trades=2)
        r2 = pre_trade_checks(CFG, st2, now, "k")
        self.assertIn(Reason.DUPLICATE_SIGNAL, r2)
        self.assertIn(Reason.MAX_TRADES, r2)


class TestPosition(unittest.TestCase):
    def pos(self):
        return open_position(CFG, "k", "S1_SWEEP_RECLAIM", Direction.LONG, 81000, "CE", 60, OPEN, 100.0, 0.30,
                             80950.0, 20.0, 81000.0)

    def test_gap_through_stop_fills_at_open_not_stop(self):
        p = self.pos()
        manage(CFG, p, bar(1, 81000, 81010, 80990, 81000), Candle(OPEN + timedelta(minutes=1), 60, 61, 55, 58), 20.0)
        self.assertEqual(p.exit_reason, Reason.EXIT_STOP_PREMIUM)
        self.assertAlmostEqual(p.exit_price, 60 - 0.10)

    def test_stop_beats_target_in_same_bar(self):
        cfg = replace(CFG, exits=replace(CFG.exits, policy="FIXED_2R"))
        p = open_position(cfg, "k", "S1", Direction.LONG, 81000, "CE", 60, OPEN, 100.0, 0.30, 80950.0, 20.0, 81000.0)
        manage(cfg, p, bar(1, 81000, 81010, 80990, 81000), Candle(OPEN + timedelta(minutes=1), 100, 190, 60, 150), 20.0)
        self.assertEqual(p.exit_reason, Reason.EXIT_STOP_PREMIUM)

    def test_breakeven_ratchets_once_and_never_widens(self):
        p = self.pos()
        for i in range(1, 5):
            manage(CFG, p, bar(i, 81000, 81100, 81000, 81090), Candle(OPEN + timedelta(minutes=i), 140, 150, 139, 145), 20.0)
        moves = [e for e in p.log if e["event"] == "STOP_RATCHET"]
        self.assertEqual(len(moves), 1)
        self.assertGreater(p.stop, 100.0)
        self.assertLess(p.stop, 102.0)

    def test_invalidation_exits_at_next_open(self):
        p = self.pos()
        manage(CFG, p, bar(1, 81000, 81000, 80940, 80945), Candle(OPEN + timedelta(minutes=1), 100, 101, 80, 82), 20.0)
        self.assertTrue(p.open)
        self.assertEqual(p.pending_exit, Reason.EXIT_INVALIDATION)
        manage(CFG, p, bar(2, 80945, 80950, 80940, 80945), Candle(OPEN + timedelta(minutes=2), 81, 83, 79, 80), 20.0)
        self.assertEqual((p.exit_reason, p.exit_price), (Reason.EXIT_INVALIDATION, 80.9))

    def test_hard_flat(self):
        p = self.pos()
        t = (datetime(2026, 10, 8, 15, 9, tzinfo=IST) - OPEN).seconds // 60
        manage(CFG, p, bar(t, 81000, 81010, 80990, 81000), Candle(OPEN + timedelta(minutes=t), 100, 101, 99, 100), 20.0)
        self.assertEqual(p.pending_exit, Reason.EXIT_HARD_FLAT)

    def test_missing_option_bar_forces_exit(self):
        p = self.pos()
        manage(CFG, p, bar(1, 81000, 81010, 80990, 81000), None, 20.0)
        self.assertEqual(p.pending_exit, Reason.EXIT_DATA_FAILURE)


class TestEngine(unittest.TestCase):
    def engine(self, is_expiry=True):
        return StrategyEngine(CFG, DAY, OPEN, PRIOR, is_expiry, DAY, 20)

    def quote(self, strike, right):
        return OptionQuote(strike, right, 150.0, 149.9, 150.1, OPEN, delta=0.5, top5_ask_qty=10_000, security_id="X1")

    def test_not_expiry_day_never_trades(self):
        d = self.engine(False).evaluate_entry(sweep_day()[:44], OPEN + timedelta(minutes=44), good_quality(), self.quote)
        self.assertEqual((d.action, d.reasons), (Action.NO_TRADE, [Reason.NOT_EXPIRY_DAY]))

    def test_bad_data_never_trades(self):
        q = assess(CFG.data, OPEN, sweep_day(), None, OPEN, feed_connected=False)
        d = self.engine().evaluate_entry(sweep_day()[:44], OPEN + timedelta(minutes=44), q, self.quote)
        self.assertEqual(d.action, Action.NO_TRADE)

    def test_sweep_produces_explainable_buy(self):
        d = self.engine().evaluate_entry(sweep_day()[:44], OPEN + timedelta(minutes=44), good_quality(), self.quote)
        self.assertEqual(d.action, Action.BUY, d.payload)
        p = d.payload
        for k in ("symbol", "expiry", "signal_timestamp", "data_timestamp", "data_age_ms", "data_quality", "regime",
                  "setup", "direction", "instrument", "score", "entry", "stop_loss", "risk", "reason_codes",
                  "config_hash", "qty", "invalidation"):
            self.assertIn(k, p)
        self.assertEqual((p["right"], p["strike"]), ("CE", 81100))
        self.assertIn("SWEEP_DETECTED", p["reason_codes"])
        self.assertIsNone(p["expected_R"])
        self.assertLess(p["stop_loss"], p["entry"])

    def test_missing_quote_is_no_trade(self):
        d = self.engine().evaluate_entry(sweep_day()[:44], OPEN + timedelta(minutes=44), good_quality(), lambda s, r: None)
        self.assertEqual((d.action, d.reasons), (Action.NO_TRADE, [Reason.DATA_STALE]))

    def test_entries_stop_at_last_entry_time(self):
        late = [Candle(x.start + timedelta(hours=5, minutes=30), x.open, x.high, x.low, x.close) for x in sweep_day()]
        d = self.engine().evaluate_entry(late[:44], late[43].start + timedelta(minutes=1), good_quality(), self.quote)
        self.assertEqual(d.reasons, [Reason.OUTSIDE_WINDOW])


class TestBacktestIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.day = make_day(DAY, seed=21, inject_sweep=True)

    def test_truncation_gives_identical_decisions(self):
        """Look-ahead test: decisions up to bar T must not change when later bars are removed."""
        full, part = [], []
        bt.run_day(CFG, self.day, trace=full)
        cut = 200
        short = bt.DayData(self.day.day, self.day.prior, True, self.day.expiry, self.day.underlying[:cut],
                           self.day.options, 20)
        bt.run_day(CFG, short, trace=part)
        ts_cut = self.day.underlying[cut - 1].start.isoformat()
        a = [p for p in full if p["bar_timestamp"] <= ts_cut]
        self.assertEqual(a, part[:len(a)])

    def test_future_option_prices_cannot_change_entry_decisions(self):
        """Corrupting every option print after bar T leaves decisions at or before T unchanged."""
        cut = self.day.underlying[180].start
        poisoned = {k: {ts: (c if ts <= cut else Candle(c.start, c.open * 3, c.high * 3, c.low * 3, c.close * 3))
                        for ts, c in s.items()} for k, s in self.day.options.items()}
        d2 = bt.DayData(self.day.day, self.day.prior, True, self.day.expiry, self.day.underlying, poisoned, 20)
        a, b = [], []
        bt.run_day(CFG, self.day, trace=a)
        bt.run_day(CFG, d2, trace=b)
        iso = cut.isoformat()
        self.assertEqual([p for p in a if p["bar_timestamp"] <= iso], [p for p in b if p["bar_timestamp"] <= iso])

    def test_non_expiry_day_trades_nothing(self):
        d = bt.DayData(self.day.day, self.day.prior, False, None, self.day.underlying, self.day.options, 20)
        trades, reasons, _ = bt.run_day(CFG, d)
        self.assertEqual(trades, [])
        self.assertIn("NOT_EXPIRY_DAY", reasons)

    def test_trades_are_flat_before_hard_flat_and_respect_limits(self):
        trades, _, _ = bt.run_day(CFG, self.day)
        self.assertLessEqual(len(trades), CFG.risk.max_trades_per_day)
        for t in trades:
            self.assertLessEqual(t.exit_ts.time(), CFG.session.hard_flat)
            self.assertGreaterEqual(t.r_net, -1.6)        # gap/slippage can exceed 1R, not by much

    def test_missing_fill_bar_cancels_entry(self):
        empty = bt.DayData(self.day.day, self.day.prior, True, self.day.expiry, self.day.underlying, {}, 20)
        trades, _, _ = bt.run_day(CFG, empty)
        self.assertEqual(trades, [])


class TestStatistics(unittest.TestCase):
    def trades(self, rs):
        t0 = OPEN
        return [bt.Trade(DAY, "S1", "LONG", t0, t0 + timedelta(minutes=10), 100, 100, 20, 1000, r, r, max(r, 0), "X", 10, {})
                for r in rs]

    def test_metrics_known_values(self):
        m = bt.metrics(self.trades([-1, -1, 2, 3, -1]))
        self.assertAlmostEqual(m["expectancy_r"], 0.4)
        self.assertAlmostEqual(m["win_rate"], 0.4)
        self.assertAlmostEqual(m["profit_factor"], 5 / 3)
        self.assertAlmostEqual(m["max_drawdown_r"], 2.0)
        self.assertEqual(m["worst_losing_streak"], 2)

    def test_bootstrap_and_permutation(self):
        lo, hi, p0 = bt.bootstrap_mean_ci([1.0] * 30)
        self.assertEqual((lo, hi, p0), (1.0, 1.0, 0.0))
        self.assertLess(bt.permutation_vs_baseline([1.0] * 30, [-1.0] * 30), 0.01)

    def test_monte_carlo_all_losers_is_ruin(self):
        mc = bt.monte_carlo([-1.0] * 50, n_paths=200, risk_pct=0.005)
        self.assertEqual(mc["prob_ruin"], 1.0)
        self.assertTrue(math.isclose(mc["worst_dd"], 1 - 0.995 ** 50, rel_tol=1e-9))

    def test_walk_forward_folds_never_overlap(self):
        days = [DAY + timedelta(days=7 * i) for i in range(30)]
        folds = bt.walk_forward_folds(days, 10, 5, 5)
        self.assertEqual(len(folds), 3)
        for f in folds:
            self.assertLess(max(f["train"]), min(f["validate"]))
            self.assertLess(max(f["validate"]), min(f["test"]))
        tests = [d for f in folds for d in f["test"]]
        self.assertEqual(len(tests), len(set(tests)))


if __name__ == "__main__":
    unittest.main()
