#!/usr/bin/env python3
"""
save_google_drive_json.py — Persist Google Drive upload metadata to the run bundle.

Usage:
    python3 save_google_drive_json.py \
        --manifest /path/to/manifest.json \
        --json '{"webViewLink":"https://..."}' 

    python3 save_google_drive_json.py \
        --manifest /path/to/manifest.json \
        --web-view-link "https://docs.google.com/..."

Exit 0 + prints DRIVE_JSON_SAVED: <path>
Exit 1 + prints DRIVE_JSON_ERROR: <reason>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


def _unwrap_drive_payload(data: dict) -> dict:
    """gog drive upload --json nests metadata under a top-level file key."""
    nested = data.get("file")
    if isinstance(nested, dict):
        return nested
    return data


def _link_from_dict(data: dict) -> str | None:
    link = data.get("webViewLink") or data.get("web_view_link") or data.get("url")
    if link:
        return str(link).strip()
    return None


def extract_web_view_link(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            payload = _unwrap_drive_payload(data)
            link = _link_from_dict(payload)
            if link:
                return link
    except json.JSONDecodeError:
        pass
    match = re.search(r"https://(?:docs|drive)\.google\.com/[^\s\"'<>]+", text)
    if match:
        return match.group(0).rstrip(".,)")
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--json", default="", help="Raw JSON from gog drive upload")
    parser.add_argument("--web-view-link", default="", help="Direct Google Doc URL")
    args = parser.parse_args()

    manifest_path = os.path.realpath(args.manifest)
    if not os.path.isfile(manifest_path):
        print(f"DRIVE_JSON_ERROR: manifest not found: {manifest_path}")
        return 1

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"DRIVE_JSON_ERROR: cannot read manifest: {e}")
        return 1

    run_dir = manifest.get("run_dir") or ""
    if not run_dir:
        print("DRIVE_JSON_ERROR: manifest missing run_dir")
        return 1

    web_view_link = (args.web_view_link or "").strip()
    payload: dict = {}

    raw_json = (args.json or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                payload = _unwrap_drive_payload(parsed)
                if not web_view_link:
                    web_view_link = str(
                        _link_from_dict(payload) or ""
                    ).strip()
        except json.JSONDecodeError:
            link = extract_web_view_link(raw_json)
            if link:
                web_view_link = link

    if not web_view_link:
        link = extract_web_view_link(raw_json)
        if link:
            web_view_link = link

    if not web_view_link:
        print("DRIVE_JSON_ERROR: no webViewLink found in input")
        return 1

    payload.setdefault("webViewLink", web_view_link)
    payload["web_view_link"] = web_view_link

    out_dir = os.path.join(run_dir, "publish")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "google-drive.json")

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError as e:
        print(f"DRIVE_JSON_ERROR: cannot write {out_path}: {e}")
        return 1

    print(f"DRIVE_JSON_SAVED: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
