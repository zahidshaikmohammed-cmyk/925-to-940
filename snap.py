"""Save the PSYGRID index option-chain endpoints to ./snapshots for inspection.

Run during market hours (e.g. 09:35):
    python snap.py
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE_URL = "http://140.245.226.102:10000"
ENDPOINTS = (
    "public/nifty-options.json",
    "public/banknifty-options.json",
    "public/sensex-options.json",
)


def fetch(path: str) -> bytes:
    req = Request(f"{BASE_URL}/{path}", headers={"Accept-Encoding": "gzip", "Cache-Control": "no-cache"})
    with urlopen(req, timeout=10) as response:
        body = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return body


def main() -> int:
    stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d_%H%M%S")
    out = Path("snapshots")
    out.mkdir(exist_ok=True)
    for path in ENDPOINTS:
        name = path.split("/")[-1].replace(".json", "")
        try:
            body = fetch(path)
            payload = json.loads(body)
            target = out / f"{name}_{stamp}.json"
            target.write_bytes(body)
            keys = ", ".join(list(payload)[:20]) if isinstance(payload, dict) else type(payload).__name__
            print(f"SAVED  {target} ({len(body) // 1024} KB) | top-level keys: {keys}")
        except Exception as exc:
            print(f"FAILED {path}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
