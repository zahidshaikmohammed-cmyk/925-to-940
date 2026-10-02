"""Single-command launcher for the PSYGRID 09:31 engine.

Examples:
    python bbbbb.py --self-test
    python bbbbb.py --preflight-only
    python bbbbb.py
"""

from run_engine import main


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

    raise SystemExit(main())
