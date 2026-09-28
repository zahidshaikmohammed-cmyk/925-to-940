from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import run_engine
from psygrid_client import PsygridClient
from run_engine import now_ist
from strategy_930 import Candidate

IST = ZoneInfo("Asia/Kolkata")

# Neither "50.py" nor "50options.py" is a valid Python identifier, so they
# cannot be `import`ed with a normal statement. Loaded the same way `python
# 50options.py` would run it, and the same way tests/test_nifty50_engine.py
# already loads 50.py.
_OPTIONS_PATH = Path(__file__).resolve().parent.parent / "50options.py"
_OPTIONS_SPEC = importlib.util.spec_from_file_location("nifty50_options_engine_under_test", _OPTIONS_PATH)
options_engine = importlib.util.module_from_spec(_OPTIONS_SPEC)
sys.modules[_OPTIONS_SPEC.name] = options_engine
_OPTIONS_SPEC.loader.exec_module(options_engine)

nifty50_engine = options_engine.nifty50_engine  # the reused 50.py module object


def make_equity_candidate(symbol="RELIANCE", side="LONG", entry=100.0, stop=95.0, target=110.0, tier=1, score=80.0):
    return Candidate(
        symbol=symbol, side=side, score=score, entry=entry, stop=stop, target=target,
        gap_pct=0.0, impulse_pct=1.0, impulse_atr=1.5, retracement_depth=0.5,
        retracement_volume_ratio=0.5, rs_market=0.5, rs_sector=0.5, vwap_distance_atr=0.5,
        structure=0.8, atr_value=1.0, retracement_level=entry, tier=tier, reasons=("STRICT",),
    )


def make_contract(last_price, delta, bid=None, ask=None):
    return {"last_price": last_price, "greeks": {"delta": delta}, "top_bid_price": bid, "top_ask_price": ask}


def make_strike_row(strike, ce=None, pe=None):
    return {"strike": strike, "ce": ce, "pe": pe}


def make_chain(symbol="RELIANCE", underlying_ltp=100.0, status="LIVE", updated_at=None, strikes=None, expiry="2026-10-30"):
    return {
        "service": "PSYGRID", "symbol": symbol, "status": status,
        "underlying_ltp": underlying_ltp, "expiry": expiry, "expiry_list": [expiry],
        "strikes": strikes if strikes is not None else [], "updated_at": updated_at, "fetch_count": 1,
    }


class FakeOptionsClient(PsygridClient):
    """A PsygridClient whose _get() returns scripted option-chain payloads
    instead of hitting the network, so build_options_signal can be tested
    end-to-end without a live feed."""

    def __init__(self, responses):
        super().__init__("http://example.invalid")
        self._responses = list(responses)
        self.calls: list[str] = []

    def _get(self, path):
        self.calls.append(path)
        if not self._responses:
            raise RuntimeError("FakeOptionsClient: no scripted response left")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ReusesExistingPipelineTests(unittest.TestCase):
    def test_uses_the_same_function_objects_as_run_engine_and_50py(self):
        self.assertIs(options_engine.build_candidates, run_engine.build_candidates)
        self.assertIs(options_engine.select_global_best, run_engine.select_global_best)
        self.assertIs(options_engine.parse_universe, run_engine.parse_universe)
        self.assertIs(options_engine.load_sector_map, run_engine.load_sector_map)
        self.assertIs(options_engine.nifty50_engine.restrict_to_nifty50, nifty50_engine.restrict_to_nifty50)
        self.assertIs(options_engine.nifty50_engine.load_nifty50_constituents, nifty50_engine.load_nifty50_constituents)


class AtmContractSelectionTests(unittest.TestCase):
    def test_picks_nearest_strike_with_correctly_signed_delta(self):
        chain = make_chain(underlying_ltp=100.0, strikes=[
            make_strike_row(90.0, ce=make_contract(12.0, 0.80)),
            make_strike_row(100.0, ce=make_contract(5.0, 0.52)),
            make_strike_row(110.0, ce=make_contract(1.5, 0.20)),
        ])
        contract = options_engine.select_atm_contract(chain, "CE")
        self.assertIsNotNone(contract)
        self.assertEqual(contract.strike, 100.0)
        self.assertEqual(contract.delta, 0.52)

    def test_rejects_call_with_wrong_signed_delta(self):
        # A malformed/mislabeled Greeks feed reporting a negative delta for
        # a call must never be silently trusted -- it would invert SL/TP.
        chain = make_chain(underlying_ltp=100.0, strikes=[
            make_strike_row(100.0, ce=make_contract(5.0, -0.52)),
        ])
        self.assertIsNone(options_engine.select_atm_contract(chain, "CE"))

    def test_rejects_put_with_wrong_signed_delta(self):
        chain = make_chain(underlying_ltp=100.0, strikes=[
            make_strike_row(100.0, pe=make_contract(5.0, 0.48)),
        ])
        self.assertIsNone(options_engine.select_atm_contract(chain, "PE"))

    def test_rejects_non_finite_or_missing_price_and_delta(self):
        chain = make_chain(underlying_ltp=100.0, strikes=[
            make_strike_row(100.0, ce={"last_price": float("nan"), "greeks": {"delta": 0.5}}),
            make_strike_row(105.0, ce={"last_price": 5.0, "greeks": {"delta": None}}),
            make_strike_row(110.0, ce={"last_price": 0.0, "greeks": {"delta": 0.4}}),
            make_strike_row(115.0, ce=None),
        ])
        self.assertIsNone(options_engine.select_atm_contract(chain, "CE"))

    def test_no_strikes_or_invalid_underlying_returns_none(self):
        self.assertIsNone(options_engine.select_atm_contract(make_chain(strikes=[]), "CE"))
        self.assertIsNone(options_engine.select_atm_contract(make_chain(underlying_ltp=None), "CE"))
        self.assertIsNone(options_engine.select_atm_contract(make_chain(underlying_ltp=-5.0), "CE"))


class PremiumTranslationTests(unittest.TestCase):
    def test_call_premium_moves_same_direction_as_underlying(self):
        equity = make_equity_candidate(side="LONG", entry=100.0, stop=95.0, target=110.0)
        contract = options_engine.OptionContract(
            symbol="X", side="CE", strike=100.0, last_price=5.0, delta=0.5, expiry="2026-10-30",
            top_bid_price=None, top_ask_price=None,
        )
        stop_p, target_p = options_engine.translate_to_premium(equity, contract)
        self.assertLess(stop_p, contract.last_price)
        self.assertGreater(target_p, contract.last_price)
        self.assertAlmostEqual(stop_p, 5.0 + 0.5 * (95.0 - 100.0))
        self.assertAlmostEqual(target_p, 5.0 + 0.5 * (110.0 - 100.0))

    def test_put_premium_moves_opposite_direction_of_underlying(self):
        # SHORT equity signal: equity.target < equity.entry < equity.stop.
        equity = make_equity_candidate(side="SHORT", entry=100.0, stop=105.0, target=90.0)
        contract = options_engine.OptionContract(
            symbol="X", side="PE", strike=100.0, last_price=5.0, delta=-0.5, expiry="2026-10-30",
            top_bid_price=None, top_ask_price=None,
        )
        stop_p, target_p = options_engine.translate_to_premium(equity, contract)
        # Put premium must still rise toward target and fall toward stop,
        # in premium space, even though the underlying moves inversely.
        self.assertLess(stop_p, contract.last_price)
        self.assertGreater(target_p, contract.last_price)


class BuildOptionsSignalTests(unittest.TestCase):
    def test_returns_signal_for_a_live_call_chain(self):
        equity = make_equity_candidate(side="LONG", entry=100.0, stop=95.0, target=110.0)
        chain = make_chain(underlying_ltp=100.0, updated_at=now_ist().isoformat(), strikes=[
            make_strike_row(100.0, ce=make_contract(5.0, 0.5, bid=4.9, ask=5.1)),
        ])
        client = FakeOptionsClient([chain])
        signal, reasons = options_engine.build_options_signal(client, equity)
        self.assertEqual(reasons, ())
        self.assertIsNotNone(signal)
        self.assertEqual(signal.contract.side, "CE")
        self.assertEqual(signal.entry_premium, 5.0)
        self.assertLess(signal.stop_premium, 5.0)
        self.assertGreater(signal.target_premium, 5.0)
        self.assertEqual(client.calls, ["public/stock-options/RELIANCE.json"])

    def test_returns_signal_for_a_live_put_chain(self):
        equity = make_equity_candidate(symbol="TCS", side="SHORT", entry=100.0, stop=105.0, target=90.0)
        chain = make_chain(symbol="TCS", underlying_ltp=100.0, updated_at=now_ist().isoformat(), strikes=[
            make_strike_row(100.0, pe=make_contract(4.0, -0.5)),
        ])
        client = FakeOptionsClient([chain])
        signal, reasons = options_engine.build_options_signal(client, equity)
        self.assertEqual(reasons, ())
        self.assertIsNotNone(signal)
        self.assertEqual(signal.contract.side, "PE")

    def test_non_live_status_is_reported_not_fabricated(self):
        for status in ("PENDING", "ERROR", "UNRESOLVED", "UNKNOWN_SYMBOL"):
            with self.subTest(status=status):
                equity = make_equity_candidate()
                client = FakeOptionsClient([make_chain(status=status)])
                signal, reasons = options_engine.build_options_signal(client, equity)
                self.assertIsNone(signal)
                self.assertTrue(any(f"options_status={status}" in r for r in reasons))

    def test_endpoint_exception_is_reported_not_fabricated(self):
        equity = make_equity_candidate()
        client = FakeOptionsClient([RuntimeError("boom")])
        signal, reasons = options_engine.build_options_signal(client, equity)
        self.assertIsNone(signal)
        self.assertTrue(any("options_endpoint_unreachable" in r for r in reasons))

    def test_no_qualifying_contract_is_reported_not_fabricated(self):
        equity = make_equity_candidate(side="LONG")
        chain = make_chain(underlying_ltp=100.0, strikes=[])
        client = FakeOptionsClient([chain])
        signal, reasons = options_engine.build_options_signal(client, equity)
        self.assertIsNone(signal)
        self.assertTrue(any("no_valid_atm" in r for r in reasons))

    def test_stale_chain_is_flagged_not_rejected(self):
        equity = make_equity_candidate(side="LONG", entry=100.0, stop=95.0, target=110.0)
        stale_time = (now_ist() - timedelta(seconds=500)).isoformat()
        chain = make_chain(underlying_ltp=100.0, updated_at=stale_time, strikes=[
            make_strike_row(100.0, ce=make_contract(5.0, 0.5)),
        ])
        client = FakeOptionsClient([chain])
        signal, reasons = options_engine.build_options_signal(client, equity)
        self.assertIsNotNone(signal)  # staleness disclosed, never silently blocks a real signal
        self.assertTrue(any("WARNING_STALE_OPTION_CHAIN" in r for r in signal.reasons))

    def test_max_wait_seconds_zero_fetches_exactly_once(self):
        equity = make_equity_candidate()
        client = FakeOptionsClient([make_chain(status="PENDING")])
        signal, reasons = options_engine.build_options_signal(client, equity, max_wait_seconds=0.0)
        self.assertIsNone(signal)
        self.assertEqual(len(client.calls), 1)

    def test_max_wait_seconds_polls_until_live(self):
        equity = make_equity_candidate(side="LONG", entry=100.0, stop=95.0, target=110.0)
        live_chain = make_chain(underlying_ltp=100.0, updated_at=now_ist().isoformat(), strikes=[
            make_strike_row(100.0, ce=make_contract(5.0, 0.5)),
        ])
        client = FakeOptionsClient([make_chain(status="PENDING"), make_chain(status="PENDING"), live_chain])
        with patch.object(options_engine.time, "sleep", return_value=None):
            signal, reasons = options_engine.build_options_signal(client, equity, max_wait_seconds=5.0)
        self.assertIsNotNone(signal)
        self.assertEqual(len(client.calls), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
