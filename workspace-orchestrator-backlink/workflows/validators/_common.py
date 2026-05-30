#!/usr/bin/env python3
"""Shared helpers for workflow step validators."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from states import WorkflowState  # noqa: E402

PHASE1_ORDER = (
    WorkflowState.NEW,
    WorkflowState.DISCOVERED,
    WorkflowState.SCORED,
    WorkflowState.AUDITED,
    WorkflowState.CONTENT_READY,
    WorkflowState.PENDING_APPROVAL,
    WorkflowState.APPROVED,
)


def state_at_least(current: str, minimum: WorkflowState) -> bool:
    try:
        cur = WorkflowState(current)
    except ValueError:
        return False
    if cur in {WorkflowState.ARCHIVED, WorkflowState.FAILED, WorkflowState.REJECTED}:
        return False
    try:
        return PHASE1_ORDER.index(cur) >= PHASE1_ORDER.index(minimum)
    except ValueError:
        return False


def load_workflow(workflow_id: str, db_path: str) -> tuple[backlink_db.WorkflowRow, backlink_db.OpportunityRow | None]:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    opp = None
    if row.opportunity_id:
        opp = backlink_db.get_opportunity(row.opportunity_id, db_path=db_path)
    return row, opp


def fail(message: str) -> int:
    print(f"STEP_FAIL: {message}", file=sys.stderr)
    return 1


def ok(message: str) -> int:
    print(f"STEP_OK: {message}")
    return 0
