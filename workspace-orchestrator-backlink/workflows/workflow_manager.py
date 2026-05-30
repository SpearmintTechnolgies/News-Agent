#!/usr/bin/env python3
"""Workflow Manager — state machine for backlink opportunities."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
from states import WorkflowState, can_transition, get_next_action, parse_state  # noqa: E402


@dataclass
class AgentResult:
    success: bool
    workflow_id: str
    step: str
    data: dict[str, Any]
    error: str | None = None
    next_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "success": self.success,
            "workflow_id": self.workflow_id,
            "step": self.step,
            "data": self.data,
            "error": self.error,
        }
        if self.next_state is not None:
            payload["next_state"] = self.next_state
        return payload


def new_workflow_id(prefix: str = "WF") -> str:
    import secrets

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(3)
    return f"{prefix}-{ts}-{suffix}"


def create(
    workflow_id: str | None = None,
    *,
    campaign_id: int | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> backlink_db.WorkflowRow:
    wid = workflow_id or new_workflow_id()
    existing = backlink_db.get_workflow(wid, db_path=db_path)
    if existing:
        raise ValueError(f"Workflow already exists: {wid}")
    return backlink_db.create_workflow(wid, campaign_id=campaign_id, db_path=db_path)


def load(
    workflow_id: str,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> backlink_db.WorkflowRow:
    row = backlink_db.get_workflow(workflow_id, db_path=db_path)
    if row is None:
        raise KeyError(f"Workflow not found: {workflow_id}")
    return row


def transition(
    workflow_id: str,
    to_state: str,
    *,
    agent: str | None = None,
    error: str | None = None,
    detail: dict[str, Any] | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> backlink_db.WorkflowRow:
    row = load(workflow_id, db_path=db_path)
    from_st = parse_state(row.state)
    to_st = parse_state(to_state)

    if not can_transition(from_st, to_st):
        raise ValueError(
            f"Invalid transition for {workflow_id}: {from_st.value} -> {to_st.value}"
        )

    updated = backlink_db.update_workflow_state(
        workflow_id,
        to_st.value,
        current_agent=agent,
        last_error=error,
        db_path=db_path,
    )
    backlink_db.insert_log(
        workflow_id,
        "transition",
        f"{from_st.value} -> {to_st.value}",
        detail={"agent": agent, "error": error, **(detail or {})},
        db_path=db_path,
    )
    return updated


def next_action_for(
    workflow_id: str,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> str | None:
    row = load(workflow_id, db_path=db_path)
    return get_next_action(parse_state(row.state))


def apply_agent_result(
    result: AgentResult,
    *,
    success_state: str,
    failure_state: str = WorkflowState.FAILED.value,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> backlink_db.WorkflowRow:
    if result.success:
        return transition(
            result.workflow_id,
            success_state,
            agent=result.step,
            detail=result.data,
            db_path=db_path,
        )
    return transition(
        result.workflow_id,
        failure_state,
        agent=result.step,
        error=result.error or "agent step failed",
        detail=result.data,
        db_path=db_path,
    )


def workflow_summary(
    workflow_id: str,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    row = load(workflow_id, db_path=db_path)
    state = parse_state(row.state)
    return {
        "workflow_id": row.workflow_id,
        "state": row.state,
        "current_agent": row.current_agent,
        "last_error": row.last_error,
        "retry_count": row.retry_count,
        "next_action": get_next_action(state),
        "logs": backlink_db.list_logs(workflow_id, db_path=db_path),
    }
