from __future__ import annotations

import unittest

import bbbbb
from psygrid_client import EXPECTED_UNIVERSE, LIVE_CORE_UNIVERSE, PsygridClient, _DuplicateAwareDict
from run_engine import BASE_URL, LIVE_CORE_URL


def live_core_payload(universe_size: int, symbols: tuple[str, ...]) -> dict:
    stocks = _DuplicateAwareDict(
        (
            symbol,
            {
                "symbol": symbol,
                "security_id": str(index),
                "previous_close": 100.0,
                "today_open": 100.5,
                "candles_1m": [
                    {
                        "timestamp": "2026-10-06 09:15:00 IST",
                        "open": 100.5,
                        "high": 101.0,
                        "low": 100.0,
                        "close": 100.8,
                        "volume": 1200,
                    }
                ],
            },
        )
        for index, symbol in enumerate(symbols)
    )
    return {
        "service": "PSYGRID",
        "schema_version": "4.0",
        "status": "OK",
        "session": {"status": "LIVE", "date": "2026-10-06", "timezone": "Asia/Kolkata"},
        "universe_size": universe_size,
        "stock_count": len(symbols),
        "coverage": {"complete": True, "expected_stock_count": universe_size},
        "stocks": stocks,
    }


class StubClient(PsygridClient):
    def __init__(self, payload: dict, **kwargs):
        super().__init__("http://stub", **kwargs)
        self.payload = payload

    def _get(self, path: str = "public/live.json"):
        return self.payload


class BbbbbUsesTheLiveCoreTests(unittest.TestCase):
    def test_default_feed_is_the_live_core(self):
        argv = bbbbb.live_core_argv([])
        self.assertEqual(argv[argv.index("--base-url") + 1], LIVE_CORE_URL)
        self.assertEqual(LIVE_CORE_URL, "http://129.225.112.47:10000")
        self.assertEqual(argv[argv.index("--expected-universe") + 1], "989")
        self.assertGreaterEqual(float(argv[argv.index("--timeout") + 1]), 15.0)

    def test_flags_are_kept_and_an_explicit_feed_is_left_alone(self):
        self.assertIn("--preflight-only", bbbbb.live_core_argv(["--preflight-only"]))
        explicit = ["--base-url", BASE_URL, "--expected-universe", "990"]
        self.assertEqual(bbbbb.live_core_argv(explicit), explicit)
        self.assertEqual(bbbbb.live_core_argv([f"--base-url={BASE_URL}"]), [f"--base-url={BASE_URL}"])

    def test_live_core_universe_passes_preflight_and_market(self):
        symbols = ("RELIANCE", "TCS", "MEESHO")
        client = StubClient(live_core_payload(LIVE_CORE_UNIVERSE, symbols), expected_universe=LIVE_CORE_UNIVERSE)
        result = client.preflight_all()["public/live.json"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(client.market()), set(symbols))
        self.assertEqual(client.last_market_errors, ())

    def test_the_full_psygrid_default_is_unchanged(self):
        self.assertEqual(EXPECTED_UNIVERSE, 990)
        client = StubClient(live_core_payload(LIVE_CORE_UNIVERSE, ("RELIANCE",)))
        result = client.preflight_all()["public/live.json"]
        self.assertFalse(result["ok"])
        self.assertIn("expected=990", result["error"])


if __name__ == "__main__":
    unittest.main()
