#!/usr/bin/env python3
"""update_manifest_batch.py - Set batch target_count and pick_run_id in manifest.json."""
import argparse
import json
import os
import sys

def main():
    parser = argparse.ArgumentParser(description="Update manifest batch section")
    parser.add_argument("--manifest", required=True, help="Path to manifest.json")
    parser.add_argument("--target", type=int, required=True, help="Batch target count N")
    parser.add_argument("--pick-run-id", required=True, help="Pick run ID")
    args = parser.parse_args()

    if not os.path.isfile(args.manifest):
        print(f"[update_manifest_batch] Error: manifest not found at {args.manifest}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(args.manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[update_manifest_batch] Error reading manifest: {e}", file=sys.stderr)
        sys.exit(1)

    data.setdefault("batch", {})
    data["batch"]["target_count"] = args.target
    data["batch"]["pick_run_id"] = args.pick_run_id
    data["batch"]["current_pick"] = 0
    data["batch"].setdefault("completed_picks", [])

    tmp = args.manifest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, args.manifest)

    print(f"[MANIFEST] batch target_count={args.target} pick_run_id={args.pick_run_id}")

if __name__ == "__main__":
    main()
