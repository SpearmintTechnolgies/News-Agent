#!/usr/bin/env python3
"""Smoke-test approved search backends before discovery runs."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ddgs import DDGS  # noqa: E402
from ddgs.exceptions import DDGSException  # noqa: E402

from tools.search.search import APPROVED_BACKENDS  # noqa: E402

PROBE_QUERIES = (
    "cryptography blog guest post",
    "write for us cryptography blog",
    "guest post cryptocurrency",
)

PRIMARY_BACKENDS = ("google", "bing", "auto")


def probe_backend(backend: str, queries: tuple[str, ...]) -> tuple[bool, str]:
    failures: list[str] = []
    for query in queries:
        try:
            raw = list(DDGS().text(query, max_results=3, backend=backend))
        except DDGSException as exc:
            failures.append(f"{query!r}: {exc}")
            continue
        if not raw:
            failures.append(f"{query!r}: 0 results")
    if failures:
        return False, "; ".join(failures)
    return True, f"count_ok queries={len(queries)}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify search backends for backlink discovery")
    parser.add_argument(
        "--queries",
        nargs="+",
        default=list(PROBE_QUERIES),
        help="Probe queries (all must return results on at least one primary backend)",
    )
    args = parser.parse_args()
    queries = tuple(args.queries)

    primary_ok = False
    any_ok = False

    for backend in APPROVED_BACKENDS:
        ok, detail = probe_backend(backend, queries)
        print(f"SEARCH_{'OK' if ok else 'FAIL'}: {backend} {detail}")
        if ok:
            any_ok = True
            if backend in PRIMARY_BACKENDS:
                primary_ok = True

    if primary_ok:
        print("SEARCH_GATE: PASS (primary backend verified)")
        return 0

    if any_ok:
        print("SEARCH_GATE: WARN (only fallback backends verified; discovery may be flaky)")
        return 0

    print("SEARCH_GATE: FAIL (no approved backends returned results)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
