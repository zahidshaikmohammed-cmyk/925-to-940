"""Single-command launcher for the PSYGRID 09:31 engine.

Examples:
    python bbbbb.py --self-test
    python bbbbb.py --preflight-only
    python bbbbb.py
"""

from run_engine import main


if __name__ == "__main__":
    raise SystemExit(main())
