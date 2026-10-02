"""PSYGRID intelligence package: the 09:45 intraday selector (945.py) and its parts.

Module map:
    selector_config       configuration + explicit model weights
    selector_data         feed parsing and the 09:45 information-set cut (the ONLY
                          path by which pre-decision code sees market data)
    selector_features     per-stock + cross-sectional feature matrix (pre-cutoff only)
    selector_scoring      transparent directional ranking model (replaceable)
    selector_calibration  walk-forward probability / expected-return estimates
    selector_snapshot     immutable, fingerprinted DecisionSnapshot
    selector_store        SQLite persistence (decisions are insert-only)
    selector_outcomes     post-decision outcome evaluation (never used by features)
    selector_backtest     historical walk-forward replay + reports
    selector_945          decision pipeline + terminal publication
"""
