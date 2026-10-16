"""sr_eval.py on SYNTHETIC recorded sessions: checks the workflow and its arithmetic, not real performance."""
import io
import contextlib
import json
import tempfile
from datetime import timedelta
from pathlib import Path

import sr
import sr_eval
from tests._compat import function_tests
from tests.test_sr import DOWN, LV_UP, UP, candles_of, now_after, path, payload, trend


def record_day(folder, raw, levels, start=25):
    for i in range(start, len(raw) + 1):
        cs = raw[:i]
        sr.save_snapshot(folder, now_after(cs), payload(cs, levels))


def test_follow_outcomes_are_conservative():
    raw = trend(24700, UP, 40)
    cs = candles_of(raw)
    sig = sr.Signal(index="NIFTY", time="09:40", expiry="", setup="PULLBACK", side="CALL", zone="", zone_price=0,
                    confluence=1, score=0, grade="", entry=cs[25].close, stop=cs[25].close - 5,
                    target1=cs[25].close + 1000, target2=cs[25].close + 2000, t1_label="", t2_label="", rr1=0, rr2=0,
                    option={}, parts={}, reasons=[], risks=[], lot_size=65, lots=1, trigger_ts=cs[25].ts.isoformat())
    o = sr_eval.follow(sig, cs, sr.Config().square_off)
    assert o.result in ("STOP", "TIME") and (o.result != "STOP" or abs(o.r + 1) < 1e-9)
    # a candle touching both stop and target counts as the stop
    both = cs[:27] + [sr.Candle(cs[26].ts + timedelta(minutes=1), 0, sig.target1 + 1, sig.stop - 1, sig.entry)]
    assert sr_eval.follow(sig, both, sr.Config().square_off).result == "STOP"


def test_oracle_and_episodes():
    cs = candles_of(trend(24700, UP, 90))
    mins = sr_eval.oracle_minutes(cs, sr.Config(), use_regime=True)
    assert mins and all(d == 1 for _, d in mins)
    eps = sr_eval.episodes(mins)
    assert 1 <= len(eps) <= len(mins)


def test_evaluate_workflow_with_split():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "snaps"
        record_day(root, trend(24700, UP, 70), LV_UP)
        # second "session": same shape on another date, bearish
        raw2 = trend(25300, DOWN, 70)
        for r in raw2:
            r["timestamp"] = r["timestamp"].replace("2026-10-15", "2026-10-16")
        lv2 = [{"name": "PDH", "price": 25350.0}, {"name": "S2", "price": 24800.0}]
        record_day(root, raw2, lv2)
        out = io.StringIO()
        js = Path(d) / "r.json"
        with contextlib.redirect_stdout(out):
            assert sr_eval.main([str(root), "NIFTY", "--lot-size", "NIFTY=65", "--min-score", "0",
                                 "--split", "2026-10-16", "--json", str(js)]) == 0
        rep = json.loads(js.read_text())
        dev, oos = rep["development (before split)"], rep["OUT-OF-SAMPLE (on/after split)"]
        assert dev["sessions"] == 1 and oos["sessions"] == 1
        assert dev["alerts"] >= 1 and oos["alerts"] >= 1
        assert dev["recall"] is not None and 0 <= dev["recall"] <= 1
        assert dev["priced_alerts"] >= 1                  # exit bids found in the recorded snapshots
        assert "=== 20261015" in out.getvalue() and "=== 20261016" in out.getvalue()


load_tests = function_tests(globals())
