#!/usr/bin/env python3
"""Unified workflow step validator entry point."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS = {
    "discover": "validate_discovery.py",
    "score": "validate_score.py",
    "audit": "validate_audit.py",
    "content": "validate_content.py",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a backlink pipeline step")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--step", required=True, choices=sorted(VALIDATORS))
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    script = Path(__file__).resolve().parent / VALIDATORS[args.step]
    cmd = [sys.executable, str(script), "--workflow-id", args.workflow_id]
    if args.db:
        cmd.extend(["--db", args.db])

    result = subprocess.run(cmd, check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
