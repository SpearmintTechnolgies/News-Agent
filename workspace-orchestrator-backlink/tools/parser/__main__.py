#!/usr/bin/env python3
"""CLI for HTML parser."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.page_fetch.page_fetch import page_fetch  # noqa: E402
from tools.parser.parser import parse  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse HTML for backlink signals")
    parser.add_argument("source", help="HTML file path or URL (fetches with Playwright if http)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    source = args.source.strip()
    base_url = None
    if source.startswith("http://") or source.startswith("https://"):
        fetched = page_fetch(source)
        html = fetched.html
        base_url = fetched.final_url
    else:
        html = Path(source).read_text(encoding="utf-8")

    result = parse(html, base_url=base_url)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"title: {result.title}")
        print(f"text_length: {len(result.text)}")
        print(f"links: {len(result.links)}")
        print(f"forms: {len(result.forms)}")
        print(f"signals: {result.signals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
