"""Chronological walk-forward, frozen final holdout, uncertainty and multiple-testing control.

Design (fixed before results):
* Evaluation sessions = sessions after the 10-day warm-up. The LAST 20% (at least 8 sessions)
  is the final holdout: it is not used for any choice and is evaluated exactly once.
* The rest is split into 4 contiguous folds. For fold f = 1..3 the training set is folds
  0..f-1 (expanding window); the variant with the best mean net return per trade in training
  (>= 30 trades, else the pre-registered default) is evaluated on fold f. Fold 0 only trains.
* Trades never span sessions (all exits by 15:15), so forward-return windows cannot overlap
  across a train/test boundary; an embargo of N sessions can still be set and is applied by
  dropping the last N training sessions before each test fold.
* Uncertainty: day-clustered bootstrap (resample sessions, 2,000 draws, fixed seed) of the
  mean net return per trade; one-sided p = share of draws <= 0. Holm correction across the
  six families' walk-forward p-values.
"""
from __future__ import annotations

import random
from collections import defaultdict

from . import families as F

FOLDS = 4
MIN_TRAIN_TRADES = 30
BOOT = 2000


def split(sessions_eval, holdout_share: float = 0.2, min_holdout: int = 8):
    n = len(sessions_eval)
    h = max(min_holdout, round(holdout_share * n))
    wf, hold = sessions_eval[:n - h], sessions_eval[n - h:]
    size = len(wf) / FOLDS
    folds = [wf[round(i * size):round((i + 1) * size)] for i in range(FOLDS)]
    adequate = len(hold) >= 15 and len(wf) >= 40
    return folds, hold, adequate


def net_of(t, cost):
    return t.gross_pct - cost


def summary(trades, cost, seed: int = 11, boot: int = BOOT):
    if not trades:
        return {"n": 0, "days": 0}
    nets = [net_of(t, cost) for t in trades]
    gross = [t.gross_pct for t in trades]
    wins = [x for x in nets if x > 0]
    loss = -sum(x for x in nets if x <= 0)
    by_day = defaultdict(list)
    for t, x in zip(trades, nets):
        by_day[t.day].append(x)
    ordered = sorted(zip(trades, nets), key=lambda z: (z[0].day, z[0].entry_slot, z[0].symbol))
    eq = peak = dd = 0.0
    for _, x in ordered:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    rnd = random.Random(seed)
    days = list(by_day)
    means = []
    for _ in range(boot):
        tot = cnt = 0
        for _ in days:
            xs = by_day[days[rnd.randrange(len(days))]]
            tot += sum(xs)
            cnt += len(xs)
        means.append(tot / cnt if cnt else 0.0)
    means.sort()
    return {"n": len(nets), "days": len(by_day), "win": len(wins) / len(nets),
            "gross": sum(gross) / len(gross), "net": sum(nets) / len(nets), "total": sum(nets),
            "pf": (sum(wins) / loss) if loss > 0 else float("inf"), "max_dd": dd,
            "ci": (means[int(0.025 * boot)], means[int(0.975 * boot) - 1]),
            "p": sum(1 for m in means if m <= 0) / boot,
            "ambiguous": sum(1 for t in trades if t.ambiguous),
            "net_optimistic": sum(t.gross_pct_optimistic - cost for t in trades) / len(trades)}


def holm(pvals: dict) -> dict:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m, out, run = len(items), {}, 0.0
    for i, (k, p) in enumerate(items):
        run = max(run, min(1.0, (m - i) * p))
        out[k] = run
    return out


def select_variant(family, trades, train_days, cost):
    train = set(train_days)
    best, best_net = F.DEFAULT_VARIANT[family], None
    for name, v in F.VARIANTS.items():
        if v[0] != family:
            continue
        rows = [t for t in trades if t.variant == name and t.day in train]
        if len(rows) < MIN_TRAIN_TRADES:
            continue
        net = sum(net_of(t, cost) for t in rows) / len(rows)
        if best_net is None or net > best_net:
            best, best_net = name, net
    return best


def walk_forward(trades, folds, cost, embargo: int = 0):
    """-> {family: {"oos": [trades], "folds": [(fold_idx, variant, summary)]}}"""
    out = {}
    for family in F.FAMILIES:
        oos, per_fold = [], []
        for f in range(1, len(folds)):
            train = [d for fold in folds[:f] for d in fold]
            if embargo:
                train = train[:-embargo]
            variant = select_variant(family, trades, train, cost)
            test = set(folds[f])
            rows = [t for t in trades if t.variant == variant and t.day in test]
            oos += rows
            per_fold.append((f, variant, summary(rows, cost, boot=200)))
        out[family] = {"oos": oos, "folds": per_fold}
    return out


def final_holdout(trades, folds, holdout, cost):
    """Variant frozen on ALL walk-forward sessions, evaluated once on the holdout."""
    train = [d for fold in folds for d in fold]
    test = set(holdout)
    out = {}
    for family in F.FAMILIES:
        variant = select_variant(family, trades, train, cost)
        out[family] = (variant, [t for t in trades if t.variant == variant and t.day in test])
    return out
