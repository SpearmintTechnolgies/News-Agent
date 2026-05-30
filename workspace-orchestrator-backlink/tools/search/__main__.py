#!/usr/bin/env python3
"""CLI for backlink web search."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.search.search import APPROVED_BACKENDS, SearchError, search  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Web search for backlink discovery")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--limit", type=int, default=10, help="Max results (1-25)")
    parser.add_argument("--no-cache", action="store_true", help="Skip SQLite cache")
    parser.add_argument("--backend", choices=APPROVED_BACKENDS, help="Force a specific backend")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    try:
        results = search(
            args.query,
            limit=args.limit,
            use_cache=not args.no_cache,
            backend=args.backend,
        )
    except SearchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    payload = [r.to_dict() for r in results]

    if args.json:
        print(json.dumps({"query": args.query, "count": len(payload), "results": payload}, indent=2))
    else:
        for i, result in enumerate(payload, 1):
            print(f"{i}. {result['title']} [{result['source']}]")
            print(f"   {result['url']}")
            if result["snippet"]:
                print(f"   {result['snippet'][:200]}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
