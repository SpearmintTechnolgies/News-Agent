#!/usr/bin/env python3
"""Poll until a pipeline artifact file is non-empty and (optionally) valid JSON.

Used after sessions_spawn when sessions_yield + subagent announce is unreliable
(Windows/cron often drops the announce, so the parent must stay in-turn and poll disk).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> int:
    p = argparse.ArgumentParser(description="Wait until a file appears and is non-empty")
    p.add_argument("path", help="Absolute path to wait for")
    p.add_argument("--timeout", type=int, default=900, help="Seconds (default 900)")
    p.add_argument("--interval", type=float, default=5.0, help="Poll interval seconds")
    p.add_argument(
        "--min-bytes",
        type=int,
        default=20,
        help="Minimum size before treating as ready",
    )
    p.add_argument(
        "--json-key",
        default=None,
        help="Optional JSON key that must be truthy (e.g. status)",
    )
    p.add_argument(
        "--json-equals",
        default=None,
        help="Optional required value for --json-key (e.g. ok)",
    )
    args = p.parse_args()

    path = os.path.expanduser(args.path)
    deadline = time.time() + max(1, args.timeout)
    last_err = ""

    while time.time() < deadline:
        try:
            if os.path.isfile(path):
                size = os.path.getsize(path)
                if size >= args.min_bytes:
                    if args.json_key is None:
                        print(f"WAIT_READY: {path} size={size}")
                        return 0
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    val = data.get(args.json_key) if isinstance(data, dict) else None
                    if args.json_equals is None:
                        if val:
                            print(f"WAIT_READY: {path} size={size} {args.json_key}={val!r}")
                            return 0
                    elif str(val) == str(args.json_equals):
                        print(f"WAIT_READY: {path} size={size} {args.json_key}={val!r}")
                        return 0
                    last_err = f"json {args.json_key}={val!r} size={size}"
                else:
                    last_err = f"size={size}"
            else:
                last_err = "missing"
        except Exception as e:
            last_err = str(e)
        time.sleep(max(0.5, args.interval))

    print(f"WAIT_TIMEOUT: {path} last={last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
