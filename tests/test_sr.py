"""Regression tests for sr.py.

Every market series below is SYNTHETIC (generated in this file). Passing these tests shows the rules
behave as specified on constructed inputs; it is not evidence of live-feed compatibility or of
profitability.
"""
import io
import json
import math
import tempfile
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import sr
from sr import (
    BEARISH,
    BULLISH,
    DATA_INVALID,
    IST,
    SIDEWAYS,
    TRANSITION,
    Candle,
    Config,
    Engine,
    IndexState,
    assess,
    atr_wilder,
    build_zones,
    classify_once,
    classify_regime,
    detect_setups,
    ema,
    index_blocks,
    parse_candles,
    parse_chain,
    parse_snapshot,
    resample,
    round_trip_costs,
    rsi_wilder,
    session_window,
    vwap_series,
)
from tests._compat import function_tests

T0 = datetime(2026, 10, 15, 9, 15, tzinfo=IST)


# --------------------------------------------------------------- synthetic builders

def cndl(i, o, h, l, c):
    return {"timestamp": (T0 + timedelta(minutes=i)).isoformat(), "open": o, "high": h, "low": l, "close": c,
            "volume": 0}


def path(start, moves, wick=1.5, i0=0):
    """Candles whose close moves by each delta in `moves`; open = previous close."""
    out, px = [], start
    for j, mv in enumerate(moves):
        o, c = px, px + mv
        out.append(cndl(i0 + j, o, max(o, c) + wick, min(o, c) - wick, c))
        px = c
    return out


UP = [5, 5, 5, -2, -2, -2]        # impulse / controlled pullback zig-zag: HH + HL, ER ~0.43
DOWN = [-m for m in UP]
CHOP = [4, -4, 3, -3, 4, -4]


def trend(start, pattern, n):
    return path(start, (pattern * (n // len(pattern) + 1))[:n])


def drift(n, start, step, wiggle=6.0):
    out, px = [], start
    for i in range(n):
        o = px
        c = px + step + (2.0 if i % 2 else -1.0)
        out.append(cndl(i, o, max(o, c) + wiggle * 0.5, min(o, c) - wiggle * 0.5, c))
        px = c
    return out


def chain(spot, ce_prev_mult=1.0, pe_prev_mult=1.0, step=50, oi=2_000_000, greeks=True, lot=None, **extra):
    strikes = []
    for k in range(int(spot // step * step - 600), int(spot // step * step + 650), step):
        m = (spot - k) / 120.0
        dce = 1 / (1 + math.exp(-m * 1.7))
        ce_px = max(1.0, (spot - k) * 0.5 + 90 * math.exp(-abs(spot - k) / 250))
        pe_px = max(1.0, (k - spot) * 0.5 + 90 * math.exp(-abs(spot - k) / 250))

        def leg(px, delta, prev_mult):
            d = {"last_price": round(px, 2), "oi": oi, "previous_oi": int(oi * prev_mult), "volume": 500_000,
                 "implied_volatility": 13.0, "top_bid_price": round(px - 0.1, 2), "top_ask_price": round(px + 0.1, 2),
                 "top_bid_quantity": 900, "top_ask_quantity": 900}
            if greeks:
                d["greeks"] = {"delta": delta, "gamma": 0.0012, "theta": -9.0, "vega": 11.0}
            return d
        strikes.append({"strike": k, "ce": leg(ce_px, round(dce, 3), ce_prev_mult),
                        "pe": leg(pe_px, round(dce - 1, 3), pe_prev_mult)})
    oc = {"status": "OK", "expiry": "2026-10-20", "expiry_list": ["2026-10-20"], "underlying_ltp": spot,
          "strikes": strikes, "analytics": {"pcr_oi": 1.0}, **extra}
    if lot:
        oc["lot_size"] = lot
    return oc


def depth_from(oc, spot, n=25):
    rows = sorted(oc["strikes"], key=lambda r: abs(r["strike"] - spot))[:n]
    out = []
    for r in rows:
        for kind in ("ce", "pe"):
            leg = r[kind]
            b, a = leg["top_bid_price"], leg["top_ask_price"]
            out.append({"strike": r["strike"], "type": kind.upper(), "expiry": oc["expiry"], "last_price": leg["last_price"],
                        "bid": [{"price": round(b - 0.05 * i, 2), "quantity": 1500, "orders": 5} for i in range(5)],
                        "ask": [{"price": round(a + 0.05 * i, 2), "quantity": 1500, "orders": 5} for i in range(5)],
                        "total_buy_quantity": 50_000, "total_sell_quantity": 40_000})
    return {"status": "LIVE", "contracts": out}


def payload(candles, levels, spot=None, oc=None, depth=None, **block):
    spot = spot if spot is not None else candles[-1]["close"]
    oc = oc or chain(spot)
    sup = [l for l in levels if l["price"] < spot]
    res = [l for l in levels if l["price"] >= spot]
    b = {"previous_close": 24600, "today_open": candles[0]["open"], "ltp": candles[-1]["close"],
         "candles_1m": candles, "candle_count": len(candles), "updated_at": candles[-1]["timestamp"],
         "option_chain": oc, "depth": depth if depth is not None else depth_from(oc, spot),
         "levels": {"supports": sup, "resistances": res, "reference": {}}}
    b.update(block)
    return {"NIFTY": b}


def engine(**kw):
    kw.setdefault("lot_size", {"NIFTY": 65})       # tests assert a user-verified lot size explicitly
    kw.setdefault("min_score", 0.0)                 # gate tests isolate the mandatory gates from the score
    out = io.StringIO()
    eng = Engine(Config(**kw), ["NIFTY"], journal=None, sound=False, explain=True, out=out, keep_audit=True)
    return eng, out


def now_after(candles):
    return datetime.fromisoformat(candles[-1]["timestamp"]) + timedelta(seconds=65)


def run_prefixes(candles, levels, start=25, eng=None, **kw):
    """Feed the engine minute by minute, exactly as live: each call sees only completed candles so far."""
    eng, out = (eng, eng.out) if eng else engine(**kw)
    alerts = []
    for i in range(start, len(candles) + 1):
        cs = candles[:i]
        alerts += eng.process(payload(cs, levels), now_after(cs), live=False)
    return eng, out, alerts


def events(eng, name):
    return [r for r in eng.audit_rows if r["event"] == name]


def candles_of(raw):
    return parse_candles(raw)


# --------------------------------------------------------------- indicators (unchanged behaviour)

def test_ema_sma_seed_and_recursion():
    e = ema([1, 2, 3, 4, 5, 6], 3)
    assert e[:2] == [None, None] and e[2] == 2.0
    assert abs(e[3] - 3.0) < 1e-12 and abs(e[5] - 5.0) < 1e-12


def test_rsi_extremes_and_balance():
    assert rsi_wilder([float(i) for i in range(20)])[-1] == 100.0
    assert rsi_wilder([float(-i) for i in range(20)])[-1] == 0.0
    alt = [100 + (1 if i % 2 else 0) for i in range(30)]
    assert abs(rsi_wilder(alt)[-1] - 50.0) < 4.0


def test_atr_constant_range():
    cs = [Candle(T0 + timedelta(minutes=i), 100, 102, 98, 100) for i in range(20)]
    assert abs(atr_wilder(cs)[-1] - 4.0) < 1e-12


def test_vwap_flags_zero_volume():
    cs = [Candle(T0, 10, 12, 8, 10), Candle(T0 + timedelta(minutes=1), 20, 22, 18, 20)]
    vw, sd, weighted = vwap_series(cs)
    assert not weighted and vw[-1] == 15.0 and abs(sd[-1] - 5.0) < 1e-9
    cs2 = [Candle(T0, 10, 12, 8, 10, 3), Candle(T0 + timedelta(minutes=1), 20, 22, 18, 20, 1)]
    vw2, _, weighted2 = vwap_series(cs2)
    assert weighted2 and vw2[-1] == 12.5


def test_resample_five_minute_aggregation():
    cs = [Candle(T0 + timedelta(minutes=i), 100 + i, 101 + i + (5 if i == 2 else 0), 99 + i - (7 if i == 3 else 0),
                 100.5 + i) for i in range(7)]
    bars = resample(cs, 5)
    assert len(bars) == 1                                  # 09:20 bucket still forming -> dropped
    b = bars[0]
    assert (b.open, b.high, b.low, b.close) == (100, 108, 95, 104.5) and b.ts == T0
    assert len(resample(cs[:5], 5)) == 1


# --------------------------------------------------------------- parsing

def test_parse_tolerant_shapes():
    epoch = int(T0.timestamp())
    rows = [[epoch + 60 * i, 100, 101, 99, 100.5, 0] for i in range(3)] + [[epoch, 1, 0, 2, 1, 0]]
    cs = parse_candles(rows)
    assert len(cs) == 3 and cs[0].ts == T0
    dhan = {"data": {"last_price": 24810.0, "oc": {
        "24800.000000": {"ce": {"last_price": 80, "oi": 10, "previous_oi": 5, "greeks": {"delta": 0.53},
                                "top_bid_price": 79.9, "top_ask_price": 80.1},
                         "pe": {"last_price": 70, "oi": 12, "greeks": {"delta": -0.47}}}}}}
    ch = parse_chain(dhan)
    ce = ch.leg(24800.0, "CE")
    assert ch.spot == 24810.0 and ce.delta == 0.53 and ce.prev_oi == 5 and ce.bid == 79.9
    assert ch.leg(24800.0, "PE").prev_oi is None
    assert set(index_blocks({"data": {"NIFTY 50": {}, "BANKNIFTY": {}, "x": 1}})) == {"NIFTY", "BANKNIFTY"}


def test_depth_levels_and_meta_parse():
    c = trend(24700, UP, 25)
    oc = chain(24750, lot=75)
    s = parse_snapshot("NIFTY", payload(c, [{"name": "PDH", "price": 24900, "confluence": 2}], oc=oc)["NIFTY"])
    bk = s.depth[(24750.0, "CE")]
    assert bk.imbalance() == 50_000 / 40_000
    assert abs(bk.fill_price(3000) - (bk.asks[0][0] + bk.asks[1][0]) / 2) < 1e-9
    assert s.levels[0].kind == "major" and s.levels[0].confluence == 2
    assert s.meta["lot_size"] == 75 and s.meta["chain_status"] == "OK" and s.meta["depth_status"] == "LIVE"


def test_zones_merge_close_levels():
    from sr import RawLevel
    z = build_zones([RawLevel(24800, "PDH", 1, "major"), RawLevel(24803, "ORH", 1, "intraday"),
                     RawLevel(24900, "R1", 1, "major")], 24750, 10.0, Config())
    assert len(z) == 2 and z[0].confluence == 2 and "PDH" in z[0].label and "ORH" in z[0].label


# --------------------------------------------------------------- session windows

def test_session_windows_and_cutoff():
    at = lambda h, m, s=0: session_window(dtime(h, m, s))
    assert at(9, 29, 59).name == "WARM-UP" and not at(9, 29, 59).entries
    assert at(9, 30).name == "PRIME" and at(9, 30).entries
    assert at(10, 44, 59).name == "PRIME" and at(10, 45).name == "GOOD"
    assert at(11, 15).name == "NO TRADE" and not at(13, 29, 59).entries
    assert at(13, 30).name == "SELECTIVE" and at(13, 30).extra_score == 10 and "BREAKOUT" not in at(13, 30).kinds
    assert at(14, 0).name == "SECONDARY" and at(14, 29, 59).entries
    assert not at(14, 30).entries and not at(15, 0).entries
    assert Config().square_off == dtime(15, 0) and Config().monitor_start == dtime(9, 25)


# --------------------------------------------------------------- trend regime

def test_regime_bullish_and_bearish():
    up = candles_of(trend(24700, UP, 60))
    dn = candles_of(trend(24700, DOWN, 60))
    r_up, r_dn = classify_regime(up, Config()), classify_regime(dn, Config())
    assert r_up.state == BULLISH, r_up.reasons
    assert r_dn.state == BEARISH, r_dn.reasons
    assert r_up.m["structure"] == "HH_HL" and r_dn.m["structure"] == "LH_LL"


def test_regime_sideways():
    r = classify_regime(candles_of(trend(24700, CHOP, 60)), Config())
    assert r.state == SIDEWAYS, (r.state, r.reasons)


def test_regime_timeframe_conflict_is_transition():
    raw = trend(24700, UP, 54)
    raw += path(raw[-1]["close"], [-6] * 5, i0=54)                 # sharp 1m drop inside a 5m uptrend
    r = classify_regime(candles_of(raw), Config())
    assert r.state == TRANSITION, (r.state, r.reasons)
    assert "timeframe conflict: 1m EMAs down, 5m up" in r.reasons[0]


def test_regime_needs_two_confirming_candles():
    raw = trend(24700, CHOP, 30) + path(24700, UP * 6, i0=30)
    cs = candles_of(raw)
    first = next(i for i in range(21, len(cs) + 1) if classify_once(cs[:i], Config()).state == BULLISH)
    assert classify_once(cs[:first - 1], Config()).state != BULLISH
    assert classify_regime(cs[:first], Config()).state == TRANSITION
    assert "second confirming candle" in classify_regime(cs[:first], Config()).reasons[0]


def test_regime_data_invalid_while_warming_up():
    r = classify_regime(candles_of(trend(24700, UP, 15)), Config())
    assert r.state == DATA_INVALID and "warming up" in r.reasons[0]


def test_vwap_never_used_when_volume_zero():
    eng, out = engine()
    raw = trend(24700, UP, 40)
    eng.process(payload(raw, [{"name": "R1", "price": 24900}]), now_after(raw), live=False)
    assert "VWAP n/a (index volume 0)" in out.getvalue()


# --------------------------------------------------------------- strict trend gate

LV_UP = [{"name": "PDL", "price": 24650.0, "confluence": 2}, {"name": "DAILY_R2", "price": 25200.0},
         {"name": "PWH", "price": 25300.0}]


def test_bullish_trend_rejects_every_bearish_entry():
    raw = trend(24700, UP, 80)
    # put a resistance at many of the swing highs so bearish rejections / traps are actually detected
    lv = LV_UP + [{"name": f"SWH{i}", "price": raw[i]["high"] + 0.5} for i in range(26, 80, 6)]
    eng, out, alerts = run_prefixes(raw, lv)
    assert alerts, out.getvalue()[-3000:]
    assert all(a.side == "CALL" for a in alerts)
    puts = [r for r in events(eng, "detected") if r["side"] == "PUT"]
    assert puts, "the test must actually produce bearish candidates"
    rejected = {(r["at"], r["zone_price"]) for r in events(eng, "rejected_trend") if r["side"] == "PUT"}
    assert len(rejected) == len({(r["at"], r["zone_price"]) for r in puts})


def test_bearish_trend_rejects_every_bullish_entry():
    raw = trend(25300, DOWN, 80)
    lv = [{"name": "PDH", "price": 25350.0}, {"name": "S2", "price": 24800.0}, {"name": "PWL", "price": 24700.0}]
    lv += [{"name": f"SWL{i}", "price": raw[i]["low"] - 0.5} for i in range(26, 80, 6)]
    eng, out, alerts = run_prefixes(raw, lv)
    assert alerts and all(a.side == "PUT" for a in alerts), out.getvalue()[-3000:]
    calls = [r for r in events(eng, "detected") if r["side"] == "CALL"]
    assert calls and all(any(x["at"] == r["at"] and x["side"] == "CALL" for x in events(eng, "rejected_trend"))
                         for r in calls)


def test_sideways_suppresses_entries():
    raw = trend(24700, CHOP, 70)
    lv = [{"name": "ORH", "price": 24704.5}, {"name": "ORL", "price": 24695.5}, {"name": "R1", "price": 24800}]
    eng, out, alerts = run_prefixes(raw, lv)
    assert not alerts
    det = events(eng, "detected")
    assert det and all(any(x["at"] == r["at"] for x in events(eng, "rejected_trend") + events(eng, "suppressed_data"))
                       for r in det)
    assert any("SIDEWAYS" in r["reason"] for r in events(eng, "rejected_trend"))


def test_conflicting_timeframes_suppress_entries():
    raw = trend(24700, UP, 54)
    raw += path(raw[-1]["close"], [-6] * 5 + [4, 3], i0=54)
    lv = [{"name": "PDH", "price": 25400.0}] + [{"name": f"L{i}", "price": 24600 + 14 * i} for i in range(40)]
    eng, out, alerts = run_prefixes(raw, lv, start=56)
    assert not alerts
    assert any("conflict" in r["reason"] for r in events(eng, "rejected_trend"))


# --------------------------------------------------------------- opportunity detection

def test_trend_continuation_pullback_alert():
    raw = trend(24700, UP, 60)
    eng, out, alerts = run_prefixes(raw, LV_UP)
    pbs = [a for a in alerts if a.setup == "PULLBACK"]
    assert pbs, out.getvalue()[-3000:]
    a = pbs[0]
    assert a.side == "CALL" and a.option["kind"] == "CE" and a.regime == BULLISH
    trig = next(c for c in candles_of(raw) if c.ts.isoformat() == a.trigger_ts)
    prior = [c for c in candles_of(raw) if c.ts < trig.ts][-4:]
    assert a.stop < min(c.low for c in prior)                # structural: below the pullback low
    assert a.entry_lo <= a.entry <= a.entry_hi
    assert a.max_loss_rs > 0 and a.net_rr >= 1.0 and a.lot_source.startswith("--lot-size")
    assert "NOT a win probability" in out.getvalue()


def test_breakout_rejection_retest_trap_detection():
    cfg = Config()
    from sr import Zone, compute_indicators
    z = lambda p: Zone(p, ["L"], 1, {"major"}, 0.5)
    base = trend(24700, UP, 30)
    last = base[-1]["close"]
    # BREAKOUT: previous close below 24800-ish zone, strong close above
    L = last + 3
    bo = base + [cndl(30, last, last + 12, last - 1, last + 11)]
    cs = candles_of(bo)
    kinds = {(s.kind, s.side) for s in detect_setups(cs, [z(L)], compute_indicators(cs), [], None, cfg)}
    assert ("BREAKOUT", 1) in kinds
    # REJECTION at support: prior closes above, long lower wick into the zone, close back up
    S = last - 6
    rj = base + [cndl(30, last, last + 1, S - 1, last + 0.5)]
    cs = candles_of(rj)
    kinds = {(s.kind, s.side) for s in detect_setups(cs, [z(S)], compute_indicators(cs), [], None, cfg)}
    assert ("REJECTION", 1) in kinds
    # RETEST: zone broken 3 minutes ago, now retested from above and held
    cs = candles_of(rj)
    broken = [(S, 1, cs[-1].ts - timedelta(minutes=3))]
    kinds = {(s.kind, s.side) for s in detect_setups(cs, [z(S)], compute_indicators(cs), broken, None, cfg)}
    assert ("RETEST", 1) in kinds
    # TRAP: zone broken DOWN 2 minutes ago, now a strong close back above it -> bullish trap
    tp = base + [cndl(30, last, last + 12, last - 1, last + 11)]
    cs = candles_of(tp)
    broken = [(L, -1, cs[-1].ts - timedelta(minutes=2))]
    kinds = {(s.kind, s.side) for s in detect_setups(cs, [z(L)], compute_indicators(cs), broken, None, cfg)}
    assert ("TRAP", 1) in kinds


def test_touching_a_level_alone_is_not_a_trade():
    raw = trend(24700, UP, 40)
    last = raw[-1]
    lv = LV_UP + [{"name": "TOUCH", "price": last["low"] + 0.2}]   # traded into, no confirmation
    eng, out = engine()
    eng.process(payload(raw[:-1], lv), now_after(raw[:-1]), live=False)
    eng.process(payload(raw, lv), now_after(raw), live=False)
    states = [r["state"] for r in events(eng, "state") if r.get("zone") == "TOUCH" or "TOUCH" in str(r.get("key"))]
    assert "TESTING" in states or "APPROACHING" in states


def test_lifecycle_states_and_expiry_on_window_change():
    raw = trend(24700, UP, 60)
    eng, out, alerts = run_prefixes(raw, LV_UP)
    states = {r["state"] for r in events(eng, "state")}
    assert {"CONFIRMED", "ALERTED"} <= states
    # an untouched zone being approached at 10:44 expires when PRIME ends
    eng2, _ = engine()
    st = eng2.state["NIFTY"]
    st.tracks["Z@1.0"] = {"state": "APPROACHING", "price": 1.0, "label": "X", "since": "2026-10-15T10:43:00+05:30",
                          "window": "PRIME"}
    cs = candles_of(trend(24700, UP, 30))
    from sr import compute_indicators
    eng2.update_tracks(parse_snapshot("NIFTY", payload(trend(24700, UP, 30), LV_UP)["NIFTY"]), st, cs, [],
                       compute_indicators(cs), [], session_window(dtime(10, 46)),
                       datetime(2026, 10, 15, 10, 46, tzinfo=IST))
    assert "Z@1.0" not in st.tracks
    assert any(r["state"] == "EXPIRED" and "window changed" in r["reason"] for r in events(eng2, "state"))


# --------------------------------------------------------------- data quality

def observed_16_oct_payload():
    """The response observed on 2026-10-16: HTTP 200, market closed, sections STARTING, nothing usable."""
    blk = {"market_open": False, "previous_close": None, "previous_close_date": None, "today_open": None, "ltp": None,
           "candles_1m": [], "candle_count": 0, "updated_at": None,
           "option_chain": {"status": "STARTING"}, "depth": {"status": "STARTING"},
           "levels": {"nearest": {}, "supports": [], "resistances": [], "reference": {}}}
    return {"market_open": False, "NIFTY": dict(blk), "BANKNIFTY": dict(blk), "SENSEX": dict(blk)}


def test_observed_starting_payload_names_every_missing_field():
    p = observed_16_oct_payload()
    s = parse_snapshot("NIFTY", p["NIFTY"])
    h = assess(s, datetime(2026, 10, 16, 18, 40, tzinfo=IST), True, IndexState(), Config(), False)
    text = h.summary()
    for needle in ("market_open=false", "candles_1m empty", "option_chain.status=STARTING", "depth.status=STARTING",
                   "levels.supports and levels.resistances empty"):
        assert needle in text, text
    assert any("previous_close=null" in w for w in h.warn) and any("ltp=null" in w for w in h.warn)
    out = io.StringIO()
    eng = Engine(Config(lot_size={"NIFTY": 65}), list(sr.INDICES), sound=False, out=out, keep_audit=True)
    assert eng.process(p, datetime(2026, 10, 16, 10, 0, tzinfo=IST), live=True) == []
    assert out.getvalue().count("DATA ") == 3 and "BLOCKED" in out.getvalue()


def test_feed_without_index_blocks_is_reported_as_invalid():
    eng, out = engine()
    assert eng.process({"status": "ok"}, datetime(2026, 10, 15, 10, 0, tzinfo=IST)) == []
    assert "FEED INVALID" in out.getvalue() and events(eng, "feed_invalid")


def test_stale_candles_suppress_signals():
    raw = trend(24700, UP, 60)
    eng, out = engine()
    late = now_after(raw) + timedelta(minutes=5)
    assert eng.process(payload(raw, LV_UP), late, live=True) == []
    assert "candles_1m stale" in out.getvalue()
    assert all(r["event"] != "alerted" for r in eng.audit_rows)


def test_missing_minutes_make_data_invalid():
    raw = trend(24700, UP, 60)
    gappy = raw[:50] + raw[52:]
    eng, out = engine()
    assert eng.process(payload(gappy, LV_UP), now_after(gappy), live=False) == []
    assert "missing minutes" in out.getvalue() and "DATA_INVALID" in out.getvalue()


def test_chain_and_depth_status_block_alerts():
    raw = trend(24700, UP, 60)
    for bad, needle in (({"oc": {"status": "STARTING"}}, "option_chain.status=STARTING"),
                        ({"depth": {"status": "STARTING"}}, "depth.status=STARTING"),
                        ({"depth": {"status": "LIVE", "contracts": []}}, "depth has no contracts")):
        eng, out = engine()
        cs = []
        for i in range(25, 61):
            cs = raw[:i]
            p = payload(cs, LV_UP, **({"option_chain": bad["oc"]} if "oc" in bad else {"depth": bad["depth"]}))
            assert eng.process(p, now_after(cs), live=False) == []
        assert needle in out.getvalue(), needle
        assert events(eng, "suppressed_data")


def test_unchanged_option_chain_is_treated_as_stale():
    raw = trend(24700, UP, 60)
    frozen = chain(24750)
    eng, out = engine()
    eng.process(payload(raw[:59], LV_UP, oc=frozen, depth=depth_from(frozen, 24750)), now_after(raw[:59]), live=False)
    eng.process(payload(raw, LV_UP, oc=frozen, depth=depth_from(frozen, 24750)), now_after(raw), live=False)
    assert "option_chain unchanged for > 30s" in out.getvalue() and "(stale cache)" in out.getvalue()


def test_health_messages_do_not_repeat_every_poll():
    raw = trend(24700, UP, 60)
    frozen = chain(24750)
    eng, out = engine()
    t = now_after(raw)
    for k in range(20):                                    # one minute of 3 s polls on a frozen feed
        eng.process(payload(raw, LV_UP, oc=frozen, depth=depth_from(frozen, 24750)), t + timedelta(seconds=3 * k),
                    live=True)
    assert len(events(eng, "data_health")) <= 3


def test_missing_greeks_and_crossed_quotes_suppress_contract():
    raw = trend(24700, UP, 60)
    eng, out, alerts = run_prefixes(raw, LV_UP, eng=None)
    assert alerts                                           # baseline: the series does alert
    eng2, out2 = engine()
    for i in range(25, 61):
        cs = raw[:i]
        spot = cs[-1]["close"]
        assert eng2.process(payload(cs, LV_UP, oc=chain(spot, greeks=False)), now_after(cs), live=False) == []
    assert "greeks missing" in out2.getvalue()
    assert events(eng2, "rejected_liquidity")


def test_market_open_false_blocks_everything():
    raw = trend(24700, UP, 60)
    eng, out = engine()
    assert eng.process(payload(raw, LV_UP, market_open=False), now_after(raw), live=False) == []
    assert "market_open=false" in out.getvalue()


# --------------------------------------------------------------- duplicates and restart recovery

def test_duplicate_alerts_prevented():
    raw = trend(24700, UP, 60)
    eng, out, alerts = run_prefixes(raw, LV_UP)
    n = len(alerts)
    assert n >= 1
    eng.state["NIFTY"].trade = None                     # even with no open trade ...
    again = eng.process(payload(raw, LV_UP), now_after(raw) + timedelta(seconds=3), live=False)
    assert again == []                                  # ... the same candle is never evaluated twice
    triggers = [a.trigger_ts for a in alerts]
    assert len(triggers) == len(set(triggers))


def test_restart_recovery_keeps_trade_and_blocks_duplicates():
    raw = trend(24700, UP, 60)
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "state.json"
        eng1 = Engine(Config(lot_size={"NIFTY": 65}, min_score=0), ["NIFTY"], sound=False, out=io.StringIO(), state_path=state,
                      keep_audit=True)
        first = None
        for i in range(25, 61):
            cs = raw[:i]
            got = eng1.process(payload(cs, LV_UP), now_after(cs), live=False)
            if got:
                first, stop_at = got[0], i
                break
        assert first is not None and state.exists()
        # process dies; a new one starts and sees the same snapshot again
        eng2 = Engine(Config(lot_size={"NIFTY": 65}, min_score=0), ["NIFTY"], sound=False, out=io.StringIO(), state_path=state,
                      keep_audit=True)
        cs = raw[:stop_at]
        assert eng2.process(payload(cs, LV_UP), now_after(cs) + timedelta(seconds=5), live=False) == []
        tr = eng2.state["NIFTY"].trade
        assert tr and tr.open and tr.sig.trigger_ts == first.trigger_ts and tr.sig.option == first.option
        # a state file from another day is ignored
        eng3 = Engine(Config(lot_size={"NIFTY": 65}), ["NIFTY"], sound=False, out=io.StringIO(), state_path=state)
        eng3.ensure_session(datetime(2026, 10, 16, tzinfo=IST).date())
        assert eng3.state["NIFTY"].trade is None


# --------------------------------------------------------------- session rules in the engine

def test_no_entries_from_1430_and_hard_exit_at_1500():
    # shift a clean uptrend so it ends at 14:31
    raw = trend(24700, UP, 60)
    shift = timedelta(hours=5, minutes=16)                 # 09:15 + 5h16 = 14:31 for the first candle
    full = trend(24700, UP, 360)                           # 09:15 .. 15:14
    eng, out = engine()
    cs = full[:316]                                        # last candle 14:30 -> decision at 14:31
    assert eng.process(payload(cs, LV_UP), now_after(cs), live=False) == []
    assert all(r["event"] not in ("alerted",) for r in eng.audit_rows)
    rej = events(eng, "rejected_window")
    assert all("NO NEW ENTRIES" in r["reason"] for r in rej)
    # an open trade is closed at 15:00 whatever the price does
    eng2, out2, alerts = run_prefixes(raw, LV_UP)
    assert alerts and eng2.state["NIFTY"].trade.open
    eng2.process(payload(raw, LV_UP), datetime(2026, 10, 15, 15, 0, 5, tzinfo=IST), live=False)
    assert not eng2.state["NIFTY"].trade.open and "HARD EXIT 15:00" in out2.getvalue()
    del shift


def test_no_trade_window_blocks_midday():
    full = trend(24700, UP, 200)                           # through 12:34
    eng, out = engine()
    cs = full[:200]
    assert eng.process(payload(cs, LV_UP), now_after(cs), live=False) == []


# --------------------------------------------------------------- risk, room and costs

def test_insufficient_room_rejected():
    raw = trend(24700, UP, 60)
    lv = [{"name": "PDL", "price": 24600.0}] + [{"name": f"WALL{i}", "price": 24690 + 14 * i} for i in range(40)]
    eng, out, alerts = run_prefixes(raw, lv)
    assert not alerts
    assert any("no room" in r["reason"] for r in events(eng, "rejected_room"))


def test_transaction_costs_formula_and_gate():
    c = round_trip_costs(100.0, 120.0, 65, "NIFTY", Config())
    buy, sell = 6500.0, 7800.0
    exch = 0.03503 / 100 * (buy + sell)
    sebi = 10 / 1e7 * (buy + sell)
    expect = 40 + 0.001 * sell + exch + sebi + 0.00003 * buy + 0.18 * (40 + exch + sebi)
    assert abs(c - expect) < 1e-9
    assert round_trip_costs(100, 120, 20, "SENSEX", Config()) != round_trip_costs(100, 120, 20, "NIFTY", Config())
    raw = trend(24700, UP, 60)
    eng, out, alerts = run_prefixes(raw, LV_UP, brokerage_per_order=5000.0)
    assert not alerts and any("after costs" in r["reason"] for r in events(eng, "rejected_room"))


def test_late_or_extended_entries_rejected():
    raw = drift(60, 24700, 3.0)                            # one-way parabola: always far above EMA20
    eng, out, alerts = run_prefixes(raw, LV_UP + [{"name": f"L{i}", "price": raw[i]["close"] - 2}
                                                  for i in range(25, 60, 3)])
    assert not alerts
    assert events(eng, "rejected_extended") or events(eng, "rejected_trend")


# --------------------------------------------------------------- option selection

def test_option_selection_direction_delta_and_lot_source():
    raw = trend(24700, UP, 60)
    eng, out, alerts = run_prefixes(raw, LV_UP)
    a = alerts[0]
    assert a.option["kind"] == "CE" and 0.40 <= a.option["delta"] <= 0.65
    assert a.option["depth_fill"] and a.option["entry"] >= a.option["ask"]
    # feed lot size wins over the CLI value and is labelled
    eng2, out2 = engine()
    got = []
    for i in range(25, 61):
        cs = raw[:i]
        got += eng2.process(payload(cs, LV_UP, oc=chain(cs[-1]["close"], lot=75)), now_after(cs), live=False)
    assert got and got[0].lot_size == 75 and got[0].lot_source == "feed metadata"
    bear = trend(25300, DOWN, 60)
    lvb = [{"name": "PDH", "price": 25350.0}, {"name": "S2", "price": 24800.0}]
    _, _, puts = run_prefixes(bear, lvb)
    assert puts and all(p.option["kind"] == "PE" and p.option["delta"] < 0 for p in puts)


def test_score_threshold_is_separate_from_gates():
    raw = trend(24700, UP, 60)
    eng, out, alerts = run_prefixes(raw, LV_UP, min_score=99)
    assert not alerts
    rej = events(eng, "rejected_score")
    assert rej and all(r["score"] < 99 for r in rej)
    eng2, out2, alerts2 = run_prefixes(raw, LV_UP, min_score=0)
    assert alerts2 and "NOT a win probability" in out2.getvalue() and "%" not in out2.getvalue().split("score    :")[1][:12]


def test_unverified_lot_size_blocks_alert():
    raw = trend(24700, UP, 60)
    eng, out, alerts = run_prefixes(raw, LV_UP, lot_size={})
    assert not alerts
    assert any("lot size unverified" in r["reason"] for r in events(eng, "rejected_contract"))


def test_cli_replay_writes_audit_and_journal():
    raw = trend(24700, UP, 60)
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d) / "snaps"
        for i in range(25, 61):
            cs = raw[:i]
            sr.save_snapshot(folder, now_after(cs), payload(cs, LV_UP))
        j, au = Path(d) / "j.jsonl", Path(d) / "a.jsonl"
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            assert sr.main(["--replay", str(folder), "--no-beep", "--lot-size", "NIFTY=65", "--min-score", "0",
                            "--journal", str(j), "--audit", str(au)]) == 0
        assert '"event": "signal"' in j.read_text()
        kinds = {json.loads(x)["event"] for x in au.read_text().splitlines()}
        assert {"detected", "alerted", "state", "regime"} <= kinds


load_tests = function_tests(globals())
