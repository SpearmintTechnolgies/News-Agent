#!/usr/bin/env python3
"""Workflow driver — read state, run next action stub, transition workflow."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))
if str(_ROOT / "tools" / "telegram") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools" / "telegram"))
if str(_ROOT / "skills" / "pipeline") not in sys.path:
    sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from states import WorkflowState  # noqa: E402
from phase_config import get_phase  # noqa: E402

# One automated step maps to this success state (unless handler chains further).
ACTION_SUCCESS_STATE: dict[str, str] = {
    "discovery": WorkflowState.DISCOVERED.value,
    "qualification": WorkflowState.QUALIFIED.value,
    "scoring": WorkflowState.SCORED.value,
    "audit": WorkflowState.AUDITED.value,
    "content": WorkflowState.CONTENT_READY.value,
    "placement": WorkflowState.PLACEMENT_CHECK.value,
    "drafting": WorkflowState.DRAFTED.value,
    "send_approval_card": WorkflowState.PENDING_APPROVAL.value,
    "publisher": WorkflowState.PUBLISHING.value,
    "verifier": WorkflowState.VERIFIED.value,
    "verify_fetch": WorkflowState.VERIFYING.value,
    "learning_record": WorkflowState.ARCHIVED.value,
    "retry": WorkflowState.DISCOVERED.value,
}


def _success_state_for(action: str) -> str:
    return ACTION_SUCCESS_STATE[action]

Handler = Callable[[str, str], workflow_manager.AgentResult]


@dataclass
class StepResult:
    workflow_id: str
    action: str | None
    status: str
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "action": self.action,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


def _run_discovery(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from discover_opportunities import enrich_workflow_opportunity  # noqa: E402

    return enrich_workflow_opportunity(workflow_id, db_path=db_path)


def _run_scoring(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    """Phase 1 scoring — same logic as qualification, lands in SCORED."""
    return _run_qualification(workflow_id, db_path)


def _run_audit(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from audit_opportunity import audit_workflow  # noqa: E402

    _, result = audit_workflow(workflow_id, db_path=db_path)
    return result


def _run_content(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from generate_content import content_workflow  # noqa: E402

    return content_workflow(workflow_id, db_path=db_path)


def _run_qualification(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from score_candidate import qualify_workflow_opportunity  # noqa: E402

    score, opp = qualify_workflow_opportunity(workflow_id, db_path=db_path)
    backlink_db.update_opportunity_fields(
        opp.id,
        relevance_score=score.relevance_score,
        spam_risk_score=score.spam_risk_score,
        final_score=score.final_score,
        status="archived" if not score.qualified else "open",
        db_path=db_path,
    )
    backlink_db.insert_log(
        workflow_id,
        "qualification",
        "Qualified" if score.qualified else f"Archived: {score.reason}",
        detail=score.to_dict(),
        db_path=db_path,
    )

    if not score.qualified:
        return workflow_manager.AgentResult(
            success=True,
            workflow_id=workflow_id,
            step="scoring" if get_phase() == 1 else "qualification",
            next_state=WorkflowState.ARCHIVED.value,
            data={**score.to_dict(), "qualified": False},
        )

    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="scoring" if get_phase() == 1 else "qualification",
        data={**score.to_dict(), "qualified": True},
    )


def _run_placement(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from detect_placement import detect_placement  # noqa: E402
    from score_candidate import _load_signals  # noqa: E402

    row = workflow_manager.load(workflow_id, db_path=db_path)
    if not row.opportunity_id:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="placement",
            data={},
            error="Workflow has no linked opportunity",
        )

    opp = backlink_db.get_opportunity(row.opportunity_id, db_path=db_path)
    if not opp:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="placement",
            data={},
            error="Opportunity not found",
        )

    signals = _load_signals(opp, db_path=db_path)
    placement = detect_placement(signals, title=opp.title, url=opp.url)
    backlink_db.update_opportunity_fields(
        opp.id,
        placement_type=placement.placement_type,
        placement_allowed=placement.placement_allowed,
        db_path=db_path,
    )
    backlink_db.insert_log(
        workflow_id,
        "placement",
        f"Placement: {placement.placement_type}",
        detail=placement.to_dict(),
        db_path=db_path,
    )

    if not placement.placement_allowed:
        return workflow_manager.AgentResult(
            success=True,
            workflow_id=workflow_id,
            step="placement",
            next_state=WorkflowState.ARCHIVED.value,
            data={**placement.to_dict(), "archived": True},
        )

    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="placement",
        data=placement.to_dict(),
    )


def _run_drafting(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from generate_draft import draft_workflow  # noqa: E402

    try:
        generated, saved = draft_workflow(workflow_id, db_path=db_path)
    except ValueError as exc:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="drafting",
            data={},
            error=str(exc),
        )

    preview = generated.draft_text
    if len(preview) > 500:
        preview = preview[:497].rstrip() + "..."

    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="drafting",
        data={
            "draft_preview": preview,
            "draft_version": saved.version,
            "tone": generated.tone,
            "confidence": generated.confidence,
            "placement_type": generated.placement_type,
            "topic": generated.topic,
        },
    )


def _stub_send_approval_card(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from send_backlink_card import send_approval_card  # noqa: E402

    card = send_approval_card(workflow_id, db_path=db_path)
    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="send_approval_card",
        data=card,
    )


def _run_publisher(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from publish_backlink import publish_backlink  # noqa: E402

    try:
        result = publish_backlink(workflow_id, db_path=db_path)
    except ValueError as exc:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="publisher",
            data={},
            error=str(exc),
        )

    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="publisher",
        data={
            **result.to_dict(),
            "outcome_state": result.outcome_state,
            "publish_status": result.status,
        },
    )


def _run_verify_fetch(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from verify_backlink import verify_fetch_workflow  # noqa: E402

    try:
        meta, html = verify_fetch_workflow(workflow_id, db_path=db_path)
    except ValueError as exc:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="verify_fetch",
            data={},
            error=str(exc),
        )

    return workflow_manager.AgentResult(
        success=html is not None or meta.dry_run,
        workflow_id=workflow_id,
        step="verify_fetch",
        data={
            **meta.to_dict(),
            "fetched": html is not None,
        },
        error=meta.fetch_error,
    )


def _run_verifier(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from verify_backlink import verify_backlink  # noqa: E402

    try:
        result = verify_backlink(workflow_id, db_path=db_path)
    except ValueError as exc:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="verifier",
            data={},
            error=str(exc),
        )

    backlink_db.insert_log(
        workflow_id,
        "verifier",
        "Verified" if result.verified else "Link not visible yet",
        detail=result.to_dict(),
        db_path=db_path,
    )

    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="verifier",
        next_state=result.outcome_state,
        data=result.to_dict(),
    )


def _run_learning_record(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    from update_learning_weights import update_learning_weights  # noqa: E402

    try:
        result = update_learning_weights(workflow_id, db_path=db_path)
    except ValueError as exc:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="learning_record",
            data={},
            error=str(exc),
        )

    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="learning_record",
        data=result,
    )


def _stub_retry(workflow_id: str, db_path: str) -> workflow_manager.AgentResult:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="retry",
        data={"retry_count": row.retry_count + 1, "note": "stub retry → discovery"},
    )


HANDLERS: dict[str, Handler] = {
    "discovery": _run_discovery,
    "scoring": _run_scoring,
    "qualification": _run_qualification,
    "audit": _run_audit,
    "content": _run_content,
    "placement": _run_placement,
    "drafting": _run_drafting,
    "send_approval_card": _stub_send_approval_card,
    "publisher": _run_publisher,
    "verifier": _run_verifier,
    "verify_fetch": _run_verify_fetch,
    "learning_record": _run_learning_record,
    "retry": _stub_retry,
}


def _chain_publish_outcome(workflow_id: str, db_path: str, outcome_state: str | None) -> None:
    """PUBLISHING → final publish outcome when handler only reached PUBLISHING."""
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state != WorkflowState.PUBLISHING.value or not outcome_state:
        return
    if outcome_state != WorkflowState.PUBLISHING.value:
        workflow_manager.transition(
            workflow_id,
            outcome_state,
            agent="publisher",
            db_path=db_path,
        )


def run_step(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> StepResult:
    """Run exactly one next action for the workflow."""
    backlink_db.expire_stale_approvals(db_path=db_path)

    action = workflow_manager.next_action_for(workflow_id, db_path=db_path)
    if action is None:
        row = workflow_manager.load(workflow_id, db_path=db_path)
        return StepResult(
            workflow_id=workflow_id,
            action=None,
            status="wait",
            message=f"No automated step for state {row.state}",
            data={"state": row.state},
        )

    handler = HANDLERS.get(action)
    if handler is None:
        return StepResult(
            workflow_id=workflow_id,
            action=action,
            status="error",
            message=f"No handler registered for action {action!r}",
            data={},
        )

    result = handler(workflow_id, db_path=db_path)
    success_state = result.next_state or _success_state_for(action)

    if result.success:
        workflow_manager.apply_agent_result(
            result,
            success_state=success_state,
            db_path=db_path,
        )
        if action == "publisher":
            outcome = result.data.get("outcome_state")
            if outcome:
                _chain_publish_outcome(workflow_id, db_path, outcome)
    else:
        workflow_manager.apply_agent_result(
            result,
            success_state=success_state,
            db_path=db_path,
        )

    row = workflow_manager.load(workflow_id, db_path=db_path)
    return StepResult(
        workflow_id=workflow_id,
        action=action,
        status="ok" if result.success else "failed",
        message=f"Ran {action} → state {row.state}",
        data={**result.to_dict(), "state": row.state, "next_action": workflow_manager.next_action_for(workflow_id, db_path=db_path)},
    )


def run_until_wait(
    workflow_id: str,
    *,
    max_steps: int = 20,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> list[StepResult]:
    """Run automated steps until wait state (e.g. PENDING_APPROVAL) or max_steps."""
    results: list[StepResult] = []
    for _ in range(max_steps):
        step = run_step(workflow_id, db_path=db_path)
        results.append(step)
        if step.status in {"wait", "error", "failed"}:
            break
    return results


def driver_summary(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    summary = workflow_manager.workflow_summary(workflow_id, db_path=db_path)
    row = workflow_manager.load(workflow_id, db_path=db_path)
    summary["opportunity_id"] = row.opportunity_id
    summary["approval_expires_at"] = row.approval_expires_at
    return summary
