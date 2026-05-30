#!/usr/bin/env python3
"""update_learning_weights.py — record outcomes and tune scoring weights."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from states import WorkflowState  # noqa: E402

WEIGHTS_PATH = _ROOT / "config" / "learning_weights.json"

DEFAULT_WEIGHTS: dict[str, Any] = {
    "version": 1,
    "domain_boost": {},
    "domain_penalty": {},
    "placement_success_rate": {},
    "notes": "Auto-generated from learning table after verified workflows.",
}


@dataclass
class LearningRecord:
    learning_id: int
    workflow_id: str
    domain: str | None
    placement_type: str | None
    final_score: float | None
    approved: bool
    published: bool
    verified: bool
    failure_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _failure_reason_for_workflow(
    row: backlink_db.WorkflowRow,
    backlink: backlink_db.BacklinkRow | None,
) -> str | None:
    state = row.state
    if state == WorkflowState.VERIFIED.value:
        return None
    if backlink and backlink.status in {"publish_failed", "verify_pending"}:
        return backlink.status
    if row.last_error:
        return row.last_error
    if state in {WorkflowState.ARCHIVED.value, WorkflowState.REJECTED.value}:
        return state.lower()
    return None


def record_workflow_learning(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> LearningRecord:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state != WorkflowState.VERIFIED.value:
        raise ValueError(f"Learning record expects VERIFIED, got {row.state}")

    existing = backlink_db.get_learning_for_workflow(workflow_id, db_path=db_path)
    if existing:
        return LearningRecord(
            learning_id=int(existing["id"]),
            workflow_id=workflow_id,
            domain=existing.get("domain"),
            placement_type=existing.get("placement_type"),
            final_score=existing.get("final_score"),
            approved=bool(existing.get("approved")),
            published=bool(existing.get("published")),
            verified=bool(existing.get("verified")),
            failure_reason=existing.get("failure_reason"),
        )

    opp = (
        backlink_db.get_opportunity(row.opportunity_id, db_path=db_path)
        if row.opportunity_id
        else None
    )
    backlink = backlink_db.get_backlink(workflow_id, db_path=db_path)
    approval = backlink_db.get_latest_approval(workflow_id, db_path=db_path)
    draft = backlink_db.get_latest_draft(workflow_id, db_path=db_path)

    approved = bool(draft and draft.approved) or (
        approval is not None and approval.decision == "approve"
    )
    published = bool(
        backlink
        and backlink.status
        in {"dry_run_published", "live_published", "verified", "verify_pending"}
    )
    verified = bool(backlink and backlink.verified)

    learning_id = backlink_db.insert_learning(
        workflow_id=workflow_id,
        domain=opp.domain if opp else None,
        placement_type=opp.placement_type if opp else None,
        final_score=opp.final_score if opp else None,
        approved=approved,
        published=published,
        verified=verified,
        failure_reason=_failure_reason_for_workflow(row, backlink),
        db_path=db_path,
    )

    record = LearningRecord(
        learning_id=learning_id,
        workflow_id=workflow_id,
        domain=opp.domain if opp else None,
        placement_type=opp.placement_type if opp else None,
        final_score=opp.final_score if opp else None,
        approved=approved,
        published=published,
        verified=verified,
        failure_reason=_failure_reason_for_workflow(row, backlink),
    )
    backlink_db.insert_log(
        workflow_id,
        "learning_record",
        "Recorded learning outcome",
        detail=record.to_dict(),
        db_path=db_path,
    )
    return record


def compute_weights(db_path: str = backlink_db.DEFAULT_DB_PATH) -> dict[str, Any]:
    aggregate = backlink_db.learning_aggregate(db_path=db_path)
    weights = dict(DEFAULT_WEIGHTS)
    domain_boost: dict[str, float] = {}
    domain_penalty: dict[str, float] = {}

    for row in aggregate.get("by_domain", []):
        domain = str(row.get("domain") or "")
        if not domain:
            continue
        total = int(row.get("total") or 0)
        verified = int(row.get("verified") or 0)
        if total < 1:
            continue
        rate = verified / total
        if rate >= 0.5 and total >= 2:
            domain_boost[domain] = round(min(15.0, rate * 10.0), 2)
        elif rate == 0 and total >= 2:
            domain_penalty[domain] = round(min(20.0, total * 3.0), 2)

    placement_rates: dict[str, float] = {}
    for row in aggregate.get("by_placement", []):
        placement = str(row.get("placement_type") or "")
        total = int(row.get("total") or 0)
        verified = int(row.get("verified") or 0)
        if placement and total > 0:
            placement_rates[placement] = round(verified / total, 3)

    weights["domain_boost"] = domain_boost
    weights["domain_penalty"] = domain_penalty
    weights["placement_success_rate"] = placement_rates
    weights["aggregate"] = aggregate.get("totals", {})
    return weights


def save_weights(
    weights: dict[str, Any],
    path: Path | None = None,
) -> Path:
    target = path or WEIGHTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(weights, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def load_weights(path: Path | None = None) -> dict[str, Any]:
    target = path or WEIGHTS_PATH
    if not target.is_file():
        return dict(DEFAULT_WEIGHTS)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(DEFAULT_WEIGHTS)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_WEIGHTS)


def record_feedback_learning(
    workflow_id: str,
    *,
    action: str,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Record Phase 1 feedback (approve/reject/edit) into learning table."""
    row = workflow_manager.load(workflow_id, db_path=db_path)
    opp = backlink_db.get_opportunity(row.opportunity_id, db_path=db_path) if row.opportunity_id else None
    domain = opp.domain if opp else None
    placement_type = opp.placement_type if opp else None
    final_score = opp.final_score if opp else None

    approved = action == "approve"
    published = False
    verified = False
    failure_reason = None if approved else action

    existing = backlink_db.get_learning_for_workflow(workflow_id, db_path=db_path)
    if existing:
        return existing

    learning_id = backlink_db.insert_learning(
        workflow_id=workflow_id,
        domain=domain,
        placement_type=placement_type,
        final_score=final_score,
        approved=approved,
        published=published,
        verified=verified,
        failure_reason=failure_reason,
        db_path=db_path,
    )
    backlink_db.insert_log(
        workflow_id,
        "feedback_learning",
        f"Recorded feedback learning: {action}",
        detail={"learning_id": learning_id, "action": action},
        db_path=db_path,
    )
    return {"learning_id": learning_id, "action": action, "workflow_id": workflow_id}


def update_learning_weights(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    record = record_workflow_learning(workflow_id, db_path=db_path)
    weights = compute_weights(db_path=db_path)
    weights_path = save_weights(weights)
    return {
        "record": record.to_dict(),
        "weights_path": str(weights_path),
        "weights": weights,
    }


def generate_backlink_report(
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    domain: str | None = None,
    learning_limit: int = 20,
) -> dict[str, Any]:
    aggregate = backlink_db.learning_aggregate(db_path=db_path)
    recent = backlink_db.list_learning(domain=domain, limit=learning_limit, db_path=db_path)
    workflows = backlink_db.list_workflows(db_path=db_path)[:learning_limit]
    weights = load_weights()

    totals = aggregate.get("totals") or {}
    total = int(totals.get("total") or 0)
    verified = int(totals.get("verified") or 0)
    success_rate = round(verified / total, 3) if total else 0.0

    return {
        "summary": {
            "learning_rows": total,
            "verified_count": verified,
            "published_count": int(totals.get("published") or 0),
            "approved_count": int(totals.get("approved") or 0),
            "verify_success_rate": success_rate,
        },
        "workflows_by_state": aggregate.get("workflows_by_state", []),
        "by_domain": aggregate.get("by_domain", []),
        "by_placement": aggregate.get("by_placement", []),
        "recent_learning": recent,
        "recent_workflows": workflows,
        "active_weights": {
            "domain_boost": weights.get("domain_boost", {}),
            "domain_penalty": weights.get("domain_penalty", {}),
            "placement_success_rate": weights.get("placement_success_rate", {}),
        },
    }
