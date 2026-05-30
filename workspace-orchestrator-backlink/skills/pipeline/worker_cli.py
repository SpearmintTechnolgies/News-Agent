#!/usr/bin/env python3
"""Shared CLI helpers for worker agents — run one step + apply DB transitions."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))
if str(_ROOT / "skills" / "pipeline") not in sys.path:
    sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from workflow_driver import ACTION_SUCCESS_STATE, _run_qualification  # noqa: E402


Handler = Callable[[str, str], workflow_manager.AgentResult]


def _success_state_for(action: str) -> str:
    return ACTION_SUCCESS_STATE[action]


def run_and_finalize(
    workflow_id: str,
    action: str,
    handler: Handler,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> int:
    try:
        result = handler(workflow_id, db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"SKILL_FAIL: {action} workflow_id={workflow_id} error={exc}", file=sys.stderr)
        return 1

    success_state = result.next_state or _success_state_for(action)

    if result.success:
        workflow_manager.apply_agent_result(
            result,
            success_state=success_state,
            db_path=db_path,
        )
        print(f"SKILL_OK: {action} workflow_id={workflow_id} state={success_state}")
        return 0

    workflow_manager.apply_agent_result(
        result,
        success_state=success_state,
        db_path=db_path,
    )
    err = result.error or "step failed"
    print(f"SKILL_FAIL: {action} workflow_id={workflow_id} error={err}", file=sys.stderr)
    return 1


def run_discovery(workflow_id: str, db_path: str) -> int:
    from discover_opportunities import enrich_workflow_opportunity  # noqa: E402

    return run_and_finalize(
        workflow_id,
        "discovery",
        lambda wid, db: enrich_workflow_opportunity(wid, db_path=db),
        db_path=db_path,
    )


def run_score(workflow_id: str, db_path: str) -> int:
    return run_and_finalize(
        workflow_id,
        "scoring",
        lambda wid, db: _run_qualification(wid, db),
        db_path=db_path,
    )


def run_audit(workflow_id: str, db_path: str) -> int:
    from audit_opportunity import audit_workflow  # noqa: E402

    def _handler(wid: str, db: str) -> workflow_manager.AgentResult:
        _, result = audit_workflow(wid, db_path=db)
        return result

    return run_and_finalize(workflow_id, "audit", _handler, db_path=db_path)


def run_content(workflow_id: str, db_path: str) -> int:
    from generate_content import content_workflow  # noqa: E402

    return run_and_finalize(
        workflow_id,
        "content",
        lambda wid, db: content_workflow(wid, db_path=db),
        db_path=db_path,
    )
