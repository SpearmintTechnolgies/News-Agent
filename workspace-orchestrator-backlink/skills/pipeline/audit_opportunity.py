#!/usr/bin/env python3
"""audit_opportunity.py — scanner/audit step for backlink placement."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))
if str(_ROOT / "skills" / "pipeline") not in sys.path:
    sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from detect_placement import detect_placement  # noqa: E402
from score_candidate import _load_signals  # noqa: E402

DEFAULT_AUDIT_THRESHOLD = 0.45

IMAGE_HINTS = (
    "featured image",
    "upload image",
    "add a photo",
    "thumbnail",
    "cover image",
    "og:image",
)


@dataclass
class AuditResult:
    placement_type: str
    placement_allowed: bool
    image_required: bool
    audit_score: float
    passed: bool
    rationale: str
    checklist: list[str]
    signals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _detect_image_required(signals: dict[str, Any], title: str | None, snippet: str | None) -> bool:
    haystack = f"{title or ''} {snippet or ''} {' '.join(str(v) for v in signals.values())}".lower()
    if any(hint in haystack for hint in IMAGE_HINTS):
        return True
    if signals.get("has_file_input"):
        return True
    placement = signals.get("placement_type") or ""
    return placement in {"image_post", "guest_post_with_image"}


def _build_checklist(placement_type: str, signals: dict[str, Any]) -> list[str]:
    items = [f"Placement type: {placement_type}"]
    if signals.get("has_form"):
        items.append("Submission form detected")
    if signals.get("has_textarea_form"):
        items.append("Long-form textarea available")
    if signals.get("guest_post_language"):
        items.append("Guest-post language present")
    if not items:
        items.append("Manual review recommended")
    return items


def audit_opportunity(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    threshold: float = DEFAULT_AUDIT_THRESHOLD,
) -> AuditResult:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if not row.opportunity_id:
        raise ValueError(f"Workflow has no linked opportunity: {workflow_id}")

    opp = backlink_db.get_opportunity(row.opportunity_id, db_path=db_path)
    if not opp:
        raise ValueError(f"Opportunity not found for workflow: {workflow_id}")

    signals = _load_signals(opp, db_path=db_path)
    placement = detect_placement(signals, title=opp.title, url=opp.url)
    image_required = True  # Phase 1: every opportunity requires a feature image

    audit_score = round(
        min(1.0, placement.confidence * 0.7 + (0.2 if opp.final_score and opp.final_score >= 60 else 0.05)),
        2,
    )
    passed = placement.placement_allowed and audit_score >= threshold
    checklist = _build_checklist(placement.placement_type, signals)
    checklist.append("Featured image required (pipeline policy)")

    result = AuditResult(
        placement_type=placement.placement_type,
        placement_allowed=placement.placement_allowed,
        image_required=image_required,
        audit_score=audit_score,
        passed=passed,
        rationale=placement.rationale,
        checklist=checklist,
        signals=signals,
    )

    backlink_db.update_opportunity_fields(
        opp.id,
        placement_type=placement.placement_type,
        placement_allowed=placement.placement_allowed,
        db_path=db_path,
    )
    backlink_db.save_audit(
        workflow_id,
        result.to_dict(),
        opportunity_id=opp.id,
        placement_type=placement.placement_type,
        image_required=image_required,
        audit_score=audit_score,
        passed=passed,
        db_path=db_path,
    )
    backlink_db.insert_log(
        workflow_id,
        "audit",
        "Audit pass" if passed else f"Audit fail: {placement.rationale}",
        detail=result.to_dict(),
        db_path=db_path,
    )
    return result


def audit_workflow(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> tuple[AuditResult, workflow_manager.AgentResult]:
    """Run audit and return AgentResult for workflow driver."""
    from states import WorkflowState  # noqa: E402

    try:
        result = audit_opportunity(workflow_id, db_path=db_path)
    except ValueError as exc:
        return (
            AuditResult(
                placement_type="unknown",
                placement_allowed=False,
                image_required=False,
                audit_score=0.0,
                passed=False,
                rationale=str(exc),
                checklist=[],
                signals={},
            ),
            workflow_manager.AgentResult(
                success=False,
                workflow_id=workflow_id,
                step="audit",
                data={},
                error=str(exc),
            ),
        )

    if not result.passed:
        return result, workflow_manager.AgentResult(
            success=True,
            workflow_id=workflow_id,
            step="audit",
            next_state=WorkflowState.ARCHIVED.value,
            data={**result.to_dict(), "archived": True},
        )

    return result, workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="audit",
        data=result.to_dict(),
    )


def main() -> int:
    import argparse

    from worker_cli import run_audit  # noqa: E402

    parser = argparse.ArgumentParser(description="Run audit for one workflow")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)
    args = parser.parse_args()
    return run_audit(args.workflow_id, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
