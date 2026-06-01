#!/usr/bin/env python3
"""
manifest_paths.py — Resolve artifact paths from a run manifest.

Usage:
    python3 manifest_paths.py --manifest /tmp/crypto-run-*/manifest.json --key article_final
    python3 manifest_paths.py --manifest /tmp/pipeline-manifest.json --key docx
    python3 manifest_paths.py --manifest /tmp/pipeline-manifest.json --run-dir

Returns the resolved path on stdout (no newline) for use in bash:
    ARTICLE=$(python3 manifest_paths.py --manifest "$RUN_DIR/manifest.json" --key article_final)

Exit 0 on success, exit 1 on missing manifest or missing key.
"""
from __future__ import annotations

import json
import sys
import os
import argparse


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="Path to manifest.json")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--key", help="Artifact key to resolve (e.g. article_final)")
    group.add_argument("--run-dir", action="store_true", help="Print run_dir")
    group.add_argument("--run-id", action="store_true", help="Print run_id")
    args = parser.parse_args()

    manifest_path = os.path.realpath(args.manifest)
    if not os.path.exists(manifest_path):
        print(f"[ERROR] Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[ERROR] Cannot parse manifest: {e}", file=sys.stderr)
        return 1

    if args.run_dir:
        val = manifest.get("run_dir", "")
        if not val:
            print("[ERROR] run_dir not in manifest", file=sys.stderr)
            return 1
        print(val, end="")
        return 0

    if args.run_id:
        val = manifest.get("run_id", "")
        if not val:
            print("[ERROR] run_id not in manifest", file=sys.stderr)
            return 1
        print(val, end="")
        return 0

    artifacts = manifest.get("artifacts", {})
    path = artifacts.get(args.key, "")
    if not path:
        print(f"[ERROR] Artifact key '{args.key}' not in manifest artifacts", file=sys.stderr)
        print(f"  Available keys: {', '.join(sorted(artifacts.keys()))}", file=sys.stderr)
        return 1

    print(path, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
