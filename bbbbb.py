"""Single-command launcher for the PSYGRID 09:31 engine.

The engine reads the two-node PSYGRID Live Core by default:
    http://129.225.112.47:10000/public/live.json  (989-stock universe)

Examples:
    python bbbbb.py --self-test
    python bbbbb.py --preflight-only
    python bbbbb.py
    python bbbbb.py --base-url http://140.245.226.102:10000 --expected-universe 990   # full PSYGRID
"""

from psygrid_client import LIVE_CORE_UNIVERSE
from run_engine import LIVE_CORE_URL, main

# The full day's /public/live.json is tens of MB by the afternoon; give the download room.
LIVE_CORE_TIMEOUT_SECONDS = 20.0


def live_core_argv(argv: list[str]) -> list[str]:
    """``argv`` with the Live Core as the feed unless the caller chose another ``--base-url``."""
    argv = list(argv)
    if any(arg == "--base-url" or arg.startswith("--base-url=") for arg in argv):
        return argv  # another feed: its own universe/timeout options (or the engine defaults) apply
    argv += ["--base-url", LIVE_CORE_URL]
    if not any(arg.startswith("--expected-universe") for arg in argv):
        argv += ["--expected-universe", str(LIVE_CORE_UNIVERSE)]
    if not any(arg.startswith("--timeout") for arg in argv):
        argv += ["--timeout", str(LIVE_CORE_TIMEOUT_SECONDS)]
    return argv


if __name__ == "__main__":
    import sys
    import unittest

    if "--self-test" in sys.argv[1:]:
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite([
            loader.loadTestsFromName("tests.test_strategy_930"),
            loader.loadTestsFromName("tests.test_run_engine"),
        ])
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 10)

    raise SystemExit(main(live_core_argv(sys.argv[1:])))
