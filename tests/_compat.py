"""Lets pytest-style test modules (plain test_* functions, pytest.raises) also run under
the standard-library unittest runner used by CI, without requiring pytest."""
from __future__ import annotations

import contextlib
import unittest


@contextlib.contextmanager
def raises(exc_type):
    try:
        yield
    except exc_type:
        return
    raise AssertionError(f"{exc_type.__name__} was not raised")


def function_tests(namespace: dict):
    """unittest load_tests hook that wraps every module-level test_* function."""
    def load_tests(loader, standard_tests, pattern):
        suite = unittest.TestSuite()
        for name, fn in sorted(namespace.items()):
            if name.startswith("test_") and callable(fn):
                suite.addTest(unittest.FunctionTestCase(fn, description=f"{namespace['__name__']}.{name}"))
        return suite
    return load_tests
