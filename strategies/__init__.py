"""Ten independent intraday strategy engines for the PSYGRID 989-stock universe.

Each engine in ``strategies.engines`` implements one strategy's own mathematics, gates and
0-100 score. The shared modules hold only strategy-neutral plumbing:

    core.py        Evaluation/Signal types, the score rule, NSE tick rounding, risk and cost gates
    indicators.py  pure math (TR/ATR, VWAP and its volume-weighted s.d., EMA, Parkinson, BVC, ...)
    features.py    per-stock feature series computed once per scan (read-only tuples)
    quality.py     data-validity checks (timestamps, staleness, gaps, liquidity)
    context.py     the market context (universe median returns, breadth, cross-sectional ranks)
    history.py     prior-day levels and opening-volume baselines from saved sessions only
    scanner.py     runs every engine over every stock with per-engine fault isolation
    ledger.py      duplicate suppression and signal lifecycle
    telegram.py    Telegram Bot API alerts (stdlib urllib)

Full documentation: docs/STRATEGY_ENGINES.md.
"""
