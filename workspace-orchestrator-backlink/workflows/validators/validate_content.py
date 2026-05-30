#!/usr/bin/env python3
"""Validate content step completed."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
from states import WorkflowState  # noqa: E402
from validators._common import fail, load_workflow, ok, state_at_least  # noqa: E402

MIN_BODY_CHARS = 200


def _image_failed_in_logs(workflow_id: str, db_path: str) -> bool:
    logs = backlink_db.list_logs(workflow_id, db_path=db_path)
    for entry in reversed(logs):
        if entry.get("step") != "content":
            continue
        detail_raw = entry.get("detail_json")
        detail: dict = {}
        if isinstance(detail_raw, str) and detail_raw:
            try:
                detail = json.loads(detail_raw)
            except json.JSONDecodeError:
                detail = {}
        elif isinstance(detail_raw, dict):
            detail = detail_raw
        if detail.get("image_error"):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)
    args = parser.parse_args()

    try:
        row, _ = load_workflow(args.workflow_id, args.db)
    except KeyError:
        return fail(f"workflow not found: {args.workflow_id}")

    if not state_at_least(row.state, WorkflowState.CONTENT_READY):
        return fail(f"state {row.state} is below CONTENT_READY")

    asset = backlink_db.get_latest_content_asset(args.workflow_id, db_path=args.db)
    if asset is None:
        return fail("content_assets row missing")

    body = (asset.content_text or "").strip()
    if len(body) < MIN_BODY_CHARS:
        return fail(f"content too short ({len(body)} chars)")

    has_image = bool(asset.image_local_path or asset.image_url)
    image_failed = _image_failed_in_logs(args.workflow_id, args.db)
    if not has_image and not image_failed:
        return fail("image missing and no image_failed note in logs")

    note = "image_ok" if has_image else "image_failed_failopen"
    return ok(f"content v{asset.version} {note} state={row.state}")


if __name__ == "__main__":
    raise SystemExit(main())
