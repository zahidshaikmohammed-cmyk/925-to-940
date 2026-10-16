import io
import math
from datetime import datetime, timedelta

import sr
from sr import (
    IST,
    Candle,
    Config,
    Engine,
    atr_wilder,
    build_zones,
    ema,
    index_blocks,
    parse_candles,
    parse_chain,
    parse_snapshot,
    resample,
    rsi_wilder,
    vwap_series,
)
from tests._compat import function_tests

T0 = datetime(2026, 10, 15, 9, 15, tzinfo=IST)


def cndl(i, o, h, l, c):
    return {"timestamp": (T0 + timedelta(minutes=i)).isoformat(), "open": o, "high": h, "low": l, "close": c,
            "volume": 0}


def drift(n, start, step, wiggle=6.0):
    """n candles trending by `step` per minute with small alternating noise."""
    out, px = [], start
    for i in range(n):
        o = px
        c = px + step + (2.0 if i % 2 else -1.0)
        out.append(cndl(i, o, max(o, c) + wiggle * 0.5, min(o, c) - wiggle * 0.5, c))
        px = c
    return out


def chain(spot, ce_prev_mult=1.0, pe_prev_mult=1.0, step=50, oi=2_000_000):
    strikes = []
    for k in range(int(spot // step * step - 600), int(spot // step * step + 650), step):
        m = (spot - k) / 120.0
        dce = 1 / (1 + math.exp(-m * 1.7))           # smooth 0..1 delta proxy
        ce_px = max(1.0, (spot - k) * 0.5 + 90 * math.exp(-abs(spot - k) / 250))
        pe_px = max(1.0, (k - spot) * 0.5 + 90 * math.exp(-abs(spot - k) / 250))
        leg = lambda px, delta, prev_mult, o: {
            "last_price": round(px, 2), "oi": o, "previous_oi": int(o * prev_mult), "volume": 500_000,
            "implied_volatility": 13.0, "greeks": {"delta": delta, "gamma": 0.0012, "theta": -9.0, "vega": 11.0},
            "top_bid_price": round(px - 0.1, 2), "top_ask_price": round(px + 0.1, 2),
            "top_bid_quantity": 900, "top_ask_quantity": 900}
        strikes.append({"strike": k,
                        "ce": leg(ce_px, round(dce, 3), ce_prev_mult, oi),
                        "pe": leg(pe_px, round(dce - 1, 3), pe_prev_mult, oi)})
    return {"expiry": "2026-10-20", "expiry_list": ["2026-10-20"], "underlying_ltp": spot, "strikes": strikes,
            "analytics": {"pcr_oi": 1.0}}


def payload(candles, levels, spot=None, oc=None, depth=None):
    spot = spot if spot is not None else candles[-1]["close"]
    sup = [l for l in levels if l["price"] < spot]
    res = [l for l in levels if l["price"] >= spot]
    return {"NIFTY": {"previous_close": 24600, "today_open": candles[0]["open"], "ltp": candles[-1]["close"],
                      "candles_1m": candles, "candle_count": len(candles), "updated_at": candles[-1]["timestamp"],
                      "option_chain": oc or chain(spot), "depth": depth or [],
                      "levels": {"supports": sup, "resistances": res, "reference": {}}}}


def engine(**kw):
    out = io.StringIO()
    eng = Engine(Config(**kw), ["NIFTY"], journal=None, sound=False, explain=True, out=out)
    return eng, out


def now_after(candles):
    return datetime.fromisoformat(candles[-1]["timestamp"]) + timedelta(seconds=65)


# --------------------------------------------------------------- indicators

def test_ema_sma_seed_and_recursion():
    e = ema([1, 2, 3, 4, 5, 6], 3)
    assert e[:2] == [None, None] and e[2] == 2.0
    assert abs(e[3] - 3.0) < 1e-12 and abs(e[5] - 5.0) < 1e-12      # linear series: EMA lags by (n-1)/2


def test_rsi_extremes_and_balance():
    assert rsi_wilder([float(i) for i in range(20)])[-1] == 100.0
    assert rsi_wilder([float(-i) for i in range(20)])[-1] == 0.0
    alt = [100 + (1 if i % 2 else 0) for i in range(30)]
    assert abs(rsi_wilder(alt)[-1] - 50.0) < 4.0


def test_atr_constant_range():
    cs = [Candle(T0 + timedelta(minutes=i), 100, 102, 98, 100) for i in range(20)]
    assert abs(atr_wilder(cs)[-1] - 4.0) < 1e-12


def test_vwap_equal_weight_when_no_volume():
    cs = [Candle(T0, 10, 12, 8, 10), Candle(T0 + timedelta(minutes=1), 20, 22, 18, 20)]
    vw, sd, weighted = vwap_series(cs)
    assert not weighted and vw[-1] == 15.0 and abs(sd[-1] - 5.0) < 1e-9
    cs2 = [Candle(T0, 10, 12, 8, 10, 3), Candle(T0 + timedelta(minutes=1), 20, 22, 18, 20, 1)]
    vw2, _, weighted2 = vwap_series(cs2)
    assert weighted2 and vw2[-1] == 12.5


def test_resample_drops_forming_bar():
    cs = [Candle(T0 + timedelta(minutes=i), 100, 101, 99, 100 + i) for i in range(7)]
    bars = resample(cs, 5)
    assert len(bars) == 1 and bars[0].close == 104
    assert len(resample(cs[:5], 5)) == 1


# --------------------------------------------------------------- parsing

def test_parse_tolerant_shapes():
    epoch = int(T0.timestamp())
    rows = [[epoch + 60 * i, 100, 101, 99, 100.5, 0] for i in range(3)] + [[epoch, 1, 0, 2, 1, 0]]  # bad geometry dropped
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


def test_depth_and_levels_parse():
    c = drift(25, 24700, 3)
    dp = [{"strike": 24800, "type": "CE", "last_price": 80,
           "bid": [{"price": 79.9, "quantity": 650, "orders": 3}], "ask": [{"price": 80.1, "quantity": 65, "orders": 1},
                                                                         {"price": 80.3, "quantity": 650, "orders": 4}],
           "total_buy_quantity": 5000, "total_sell_quantity": 2500}]
    lv = [{"name": "PDH", "price": 24900, "distance": 10, "confluence": 2}]
    s = parse_snapshot("NIFTY", payload(c, lv, depth=dp)["NIFTY"])
    bk = s.depth[(24800.0, "CE")]
    assert bk.imbalance() == 2.0
    assert abs(bk.fill_price(130) - (80.1 * 65 + 80.3 * 65) / 130) < 1e-9
    assert s.levels[0].kind == "major" and s.levels[0].confluence == 2


def test_zones_merge_close_levels():
    from sr import RawLevel
    z = build_zones([RawLevel(24800, "PDH", 1, "major"), RawLevel(24803, "ORH", 1, "intraday"),
                     RawLevel(24900, "R1", 1, "major")], 24750, 10.0, Config())
    assert len(z) == 2 and z[0].confluence == 2 and "PDH" in z[0].label and "ORH" in z[0].label


# --------------------------------------------------------------- signals

LEVELS = [{"name": "PDH", "price": 24800.0, "confluence": 2},
          {"name": "DAILY_R1", "price": 24880.0, "confluence": 1},
          {"name": "PDL", "price": 24650.0, "confluence": 1}]


def breakout_candles():
    c = drift(30, 24700, 2.5)                 # 24700 -> ~24790 by 09:44, below PDH 24800
    last = c[-1]["close"]
    c.append(cndl(30, last, last + 2, last - 1, last))
    c.append(cndl(31, last, 24822, last - 1, 24820))      # strong close through 24800
    return c


def test_breakout_signal_call():
    c = breakout_candles()
    # CE writers at 24800 unwinding (prev OI higher), PE writers adding
    oc = chain(24820, ce_prev_mult=1.4, pe_prev_mult=0.6)
    eng, out = engine(min_score=55)
    sigs = eng.process(payload(c, LEVELS, spot=24820, oc=oc), now_after(c), live=False)
    assert len(sigs) == 1, out.getvalue()
    s = sigs[0]
    assert s.side == "CALL" and s.setup == "BREAKOUT" and s.option["kind"] == "CE"
    assert 0.40 <= s.option["delta"] <= 0.65
    assert s.stop <= 24806 < s.entry < s.target1 <= 24880           # big candle: stop at its midpoint
    assert any("midpoint" in r for r in s.risks)
    assert s.option["sl"] < s.option["entry"] < s.option["t1"] < s.option["t2"]


def test_breakout_blocked_without_room():
    c = breakout_candles()
    lv = LEVELS + [{"name": "ORH", "price": 24832.0, "confluence": 1}]
    eng, out = engine(min_score=0)
    sigs = eng.process(payload(c, lv, spot=24820), now_after(c), live=False)
    assert not sigs and "no room" in out.getvalue()


def test_rejection_at_support_call():
    c = drift(30, 24760, -3.0)               # sliding toward 24680 support
    last = c[-1]["close"]
    c.append(cndl(30, last, last + 1, 24679, 24686))     # long lower wick into 24680, closes back up
    lv = [{"name": "PDL", "price": 24680.0, "confluence": 2}, {"name": "VWAP_BAND", "price": 24760.0},
          {"name": "PWL", "price": 24600.0}]
    oc = chain(24686, ce_prev_mult=1.0, pe_prev_mult=0.7)   # PE writers adding at support
    eng, out = engine(min_score=0)
    sigs = eng.process(payload(c, lv, spot=24686, oc=oc), now_after(c), live=False)
    assert sigs and sigs[0].setup == "REJECTION" and sigs[0].side == "CALL", out.getvalue()
    assert sigs[0].stop < 24679


def test_no_signal_before_warmup():
    c = breakout_candles()[-12:]
    eng, out = engine(min_score=0)
    assert not eng.process(payload(c, LEVELS, spot=24820), now_after(c), live=False)
    assert "warming up" in out.getvalue()


def test_trade_management_t1_then_breakeven():
    c = breakout_candles()
    eng, out = engine(min_score=0)
    now = now_after(c)
    [s] = eng.process(payload(c, LEVELS, spot=24820), now, live=False)
    st = eng.state["NIFTY"]
    eng.process(payload(c, LEVELS, spot=s.target1 + 1), now + timedelta(seconds=5), live=False)
    assert st.trade.t1_hit and st.trade.stop == s.entry
    eng.process(payload(c, LEVELS, spot=s.entry - 1), now + timedelta(seconds=10), live=False)
    assert not st.trade.open and "STOP (breakeven)" in out.getvalue()


def test_trap_after_failed_break():
    c = breakout_candles()
    eng, out = engine(min_score=0)
    eng.process(payload(c, LEVELS, spot=24820), now_after(c), live=False)
    eng.state["NIFTY"].trade = None                  # free the slot to see the next setup
    c.append(cndl(32, 24820, 24823, 24805, 24808))
    c.append(cndl(33, 24808, 24809, 24783, 24785))   # closes back under 24800: failed break
    sigs = eng.process(payload(c, LEVELS, spot=24785), now_after(c), live=False)
    assert sigs and sigs[0].setup == "TRAP" and sigs[0].side == "PUT", out.getvalue()
    assert sigs[0].option["kind"] == "PE" and sigs[0].stop > 24809


def test_main_cli_replay(tmp=None):
    import json
    import tempfile
    from pathlib import Path
    c = breakout_candles()
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "20261015_094700.json"
        f.write_text(json.dumps(payload(c, LEVELS, spot=24820)), encoding="utf-8")
        assert sr.main(["--replay", str(f), "--no-beep", "--min-score", "0", "--journal", str(Path(d) / "j.jsonl")]) == 0
        assert (Path(d) / "j.jsonl").read_text().count('"event": "signal"') == 1


load_tests = function_tests(globals())
