#!/usr/bin/env python3
"""CLI for Playwright page fetch."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.page_fetch.page_fetch import page_fetch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a page with Playwright")
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--html-out", help="Write HTML to file instead of stdout metadata")
    parser.add_argument("--json", action="store_true", help="Output JSON metadata")
    args = parser.parse_args()

    result = page_fetch(args.url, timeout_ms=args.timeout_ms)
    if args.html_out:
        Path(args.html_out).write_text(result.html, encoding="utf-8")
        meta = result.to_dict()
        meta.pop("html")
        meta["html_out"] = args.html_out
        print(json.dumps(meta, indent=2))
    elif args.json:
        payload = result.to_dict()
        payload["html_length"] = len(payload.pop("html"))
        print(json.dumps(payload, indent=2))
    else:
        print(f"status: {result.status}")
        print(f"final_url: {result.final_url}")
        print(f"html_length: {len(result.html)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
