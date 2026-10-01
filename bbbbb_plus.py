"""bbbbb_plus.py -- bbbbb.py's signal, unchanged, plus better exits.

This file does NOT modify bbbbb.py, run_engine.py or strategy_930.py. It runs
the exact same engine (same scan, same scoring, same #1, same printout) and
only listens in on which candidate the engine selected. Then it adds:

    TP1        +1R, or the day's high/low if that is closer (>= 0.5R)
               -> book 50% and move SL to entry (breakeven)
    TP2        the day's high/low when it sits between 1R and 2R,
               otherwise bbbbb.py's own 2R target
    TIME STOP  exit if not +0.5R within 20 minutes; max hold 60 minutes
    MANAGER    optional live trade manager (only with --manage)

Usage:
    python bbbbb_plus.py              bbbbb.py output + exit plan, then stops
    python bbbbb_plus.py --manage     also ask for your fill and run the trade manager
    python bbbbb_plus.py --self-test
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime

import run_engine
from old import OldConfig, Trade, ask_fill, run_manager
from psygrid_client import PsygridClient
from strategy_930 import Candle, Candidate


@dataclass(frozen=True)
class ExitPlan:
    tp1: float
    tp2: float
    tp1_basis: str
    tp2_basis: str
    risk: float
    note: str = ""


def exit_plan(c: Candidate, candles: tuple[Candle, ...] | list[Candle]) -> ExitPlan:
    """TP1/TP2 for a bbbbb.py candidate. Entry and SL are never changed."""
    d = 1 if c.side == "LONG" else -1
    risk = abs(c.entry - c.stop)
    one_r = c.entry + d * risk
    if not candles or risk <= 0:
        return ExitPlan(one_r, c.target, "+1R", "bbbbb 2R target", risk)
    extreme = max(x.high for x in candles) if d == 1 else min(x.low for x in candles)
    name = "day high" if d == 1 else "day low"
    dist = d * (extreme - c.entry) / risk
    if 0.5 <= dist < 1.0:
        return ExitPlan(extreme, c.target, f"{name} (in the way before 1R)", "bbbbb 2R target", risk,
                        f"{name} {extreme:.2f} is only {dist:.2f}R away -- expect a stall there")
    if 1.0 <= dist <= 2.0:
        return ExitPlan(one_r, extreme, "+1R", f"{name} ({dist:.2f}R)", risk)
    note = ""
    if dist < 0.5:
        note = (f"entry is within {max(dist, 0):.2f}R of the {name} -- TP2 needs a breakout; "
                f"TP1 is the realistic exit")
    return ExitPlan(one_r, c.target, "+1R", "bbbbb 2R target", risk, note)


def print_exit_plan(c: Candidate, plan: ExitPlan, cfg: OldConfig) -> None:
    d = 1 if c.side == "LONG" else -1
    r = lambda p: d * (p - c.entry) / plan.risk
    print("\n" + "-" * 96)
    print("EXIT PLAN (bbbbb_plus) -- same entry and SL as above, better exits")
    print("-" * 96)
    print(f"ENTRY        : Rs {c.entry:.2f}")
    print(f"STOP LOSS    : Rs {c.stop:.2f}  (unchanged)")
    print(f"TP1          : Rs {plan.tp1:.2f}  ({r(plan.tp1):.2f}R, {plan.tp1_basis}) -> book 50%, move SL to entry")
    print(f"TP2          : Rs {plan.tp2:.2f}  ({r(plan.tp2):.2f}R, {plan.tp2_basis}) -> exit the rest")
    print(f"TIME STOP    : exit if not +{cfg.time_stop_min_r}R within {cfg.time_stop_minutes} min | "
          f"max hold {cfg.max_hold_minutes} min | square off by {cfg.square_off}")
    if plan.note:
        print(f"NOTE         : {plan.note}")


class _Spy:
    """Records what run_engine.main() selects, without changing anything."""

    def __init__(self):
        self.best: Candidate | None = None
        self.parsed: dict = {}
        self.scan_time: datetime | None = None

    def __enter__(self):
        self._select, self._parse = run_engine.select_global_best, run_engine.parse_universe

        def select(candidates):
            self.best = self._select(candidates)
            return self.best

        def parse(client, raw, scan_time):
            self.parsed = self._parse(client, raw, scan_time)
            self.scan_time = scan_time
            return self.parsed

        run_engine.select_global_best, run_engine.parse_universe = select, parse
        return self

    def __exit__(self, *exc):
        run_engine.select_global_best, run_engine.parse_universe = self._select, self._parse
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bbbbb.py with better exits")
    parser.add_argument("--manage", action="store_true", help="ask for your fill and run the trade manager")
    parser.add_argument("--no-manage", action="store_true", help=argparse.SUPPRESS)   # old flag, now the default
    parser.add_argument("--self-test", action="store_true")
    args, engine_args = parser.parse_known_args(argv)
    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_bbbbb_plus")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 10

    with _Spy() as spy:
        code = run_engine.main(engine_args)            # exactly what bbbbb.py runs
    best = spy.best
    if code != 0 or best is None:
        return code

    data = spy.parsed.get(best.symbol)
    candles = data.candles if data else ()
    cfg = OldConfig()
    plan = exit_plan(best, candles)
    print_exit_plan(best, plan, cfg)

    if not args.manage or not candles:
        return code
    fill = ask_fill(best)
    if fill is None:
        return code
    tr = Trade(best.symbol, best.side, fill, best.stop, plan.tp1, plan.tp2,
               run_engine.now_ist().isoformat(), candles[-1].ts.isoformat(), best.stop)
    base_url = engine_args[engine_args.index("--base-url") + 1] if "--base-url" in engine_args else run_engine.BASE_URL
    run_manager(tr, PsygridClient(base_url), cfg, run_engine.Audit())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
