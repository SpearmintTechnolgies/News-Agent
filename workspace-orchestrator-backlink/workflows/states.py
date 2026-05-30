"""Canonical workflow states and allowed transitions (v3 / Phase 1)."""
from __future__ import annotations

from enum import StrEnum

from phase_config import get_phase


class WorkflowState(StrEnum):
    NEW = "NEW"
    DISCOVERED = "DISCOVERED"
    # Phase 1 path
    SCORED = "SCORED"
    AUDITED = "AUDITED"
    CONTENT_READY = "CONTENT_READY"
    # Legacy v2 path (Phase 2)
    QUALIFIED = "QUALIFIED"
    PLACEMENT_CHECK = "PLACEMENT_CHECK"
    DRAFTED = "DRAFTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EDIT_REQUESTED = "EDIT_REQUESTED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PENDING_MODERATION = "PENDING_MODERATION"
    SUBMISSION_REJECTED = "SUBMISSION_REJECTED"
    VERIFYING = "VERIFYING"
    VERIFY_PENDING = "VERIFY_PENDING"
    VERIFIED = "VERIFIED"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


NEXT_ACTION_PHASE1: dict[WorkflowState, str | None] = {
    WorkflowState.NEW: "discovery",
    WorkflowState.DISCOVERED: "scoring",
    WorkflowState.SCORED: "audit",
    WorkflowState.AUDITED: "content",
    WorkflowState.CONTENT_READY: "send_approval_card",
    WorkflowState.PENDING_APPROVAL: None,
    WorkflowState.EDIT_REQUESTED: "content",
    WorkflowState.APPROVAL_EXPIRED: None,
    WorkflowState.APPROVED: None,
    WorkflowState.REJECTED: None,
    WorkflowState.FAILED: None,
    WorkflowState.ARCHIVED: None,
}

NEXT_ACTION_PHASE2: dict[WorkflowState, str | None] = {
    WorkflowState.NEW: "discovery",
    WorkflowState.DISCOVERED: "qualification",
    WorkflowState.QUALIFIED: "placement",
    WorkflowState.PLACEMENT_CHECK: "drafting",
    WorkflowState.DRAFTED: "send_approval_card",
    WorkflowState.PENDING_APPROVAL: None,
    WorkflowState.EDIT_REQUESTED: "drafting",
    WorkflowState.APPROVAL_EXPIRED: None,
    WorkflowState.APPROVED: "publisher",
    WorkflowState.REJECTED: None,
    WorkflowState.PUBLISHING: None,
    WorkflowState.PUBLISHED: "verify_fetch",
    WorkflowState.PENDING_MODERATION: "verify_fetch",
    WorkflowState.SUBMISSION_REJECTED: None,
    WorkflowState.VERIFYING: "verifier",
    WorkflowState.VERIFY_PENDING: "verify_fetch",
    WorkflowState.VERIFIED: "learning_record",
    WorkflowState.RETRY_PENDING: "retry",
    WorkflowState.FAILED: None,
    WorkflowState.ARCHIVED: None,
}

ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.NEW: frozenset({
        WorkflowState.DISCOVERED,
        WorkflowState.FAILED,
        WorkflowState.ARCHIVED,
    }),
    WorkflowState.DISCOVERED: frozenset({
        WorkflowState.SCORED,
        WorkflowState.QUALIFIED,
        WorkflowState.ARCHIVED,
        WorkflowState.FAILED,
    }),
    WorkflowState.SCORED: frozenset({
        WorkflowState.AUDITED,
        WorkflowState.ARCHIVED,
        WorkflowState.FAILED,
    }),
    WorkflowState.AUDITED: frozenset({
        WorkflowState.CONTENT_READY,
        WorkflowState.ARCHIVED,
        WorkflowState.FAILED,
    }),
    WorkflowState.CONTENT_READY: frozenset({
        WorkflowState.PENDING_APPROVAL,
        WorkflowState.FAILED,
    }),
    WorkflowState.QUALIFIED: frozenset({
        WorkflowState.PLACEMENT_CHECK,
        WorkflowState.ARCHIVED,
        WorkflowState.FAILED,
    }),
    WorkflowState.PLACEMENT_CHECK: frozenset({
        WorkflowState.DRAFTED,
        WorkflowState.ARCHIVED,
        WorkflowState.FAILED,
    }),
    WorkflowState.DRAFTED: frozenset({
        WorkflowState.PENDING_APPROVAL,
        WorkflowState.FAILED,
    }),
    WorkflowState.PENDING_APPROVAL: frozenset({
        WorkflowState.APPROVED,
        WorkflowState.REJECTED,
        WorkflowState.EDIT_REQUESTED,
        WorkflowState.APPROVAL_EXPIRED,
        WorkflowState.ARCHIVED,
        WorkflowState.FAILED,
    }),
    WorkflowState.EDIT_REQUESTED: frozenset({
        WorkflowState.CONTENT_READY,
        WorkflowState.DRAFTED,
        WorkflowState.FAILED,
    }),
    WorkflowState.APPROVAL_EXPIRED: frozenset({
        WorkflowState.ARCHIVED,
    }),
    WorkflowState.APPROVED: frozenset({
        WorkflowState.PUBLISHING,
        WorkflowState.FAILED,
        WorkflowState.ARCHIVED,
    }),
    WorkflowState.REJECTED: frozenset({
        WorkflowState.ARCHIVED,
    }),
    WorkflowState.PUBLISHING: frozenset({
        WorkflowState.PUBLISHED,
        WorkflowState.PENDING_MODERATION,
        WorkflowState.SUBMISSION_REJECTED,
        WorkflowState.FAILED,
    }),
    WorkflowState.PUBLISHED: frozenset({
        WorkflowState.VERIFYING,
        WorkflowState.FAILED,
    }),
    WorkflowState.PENDING_MODERATION: frozenset({
        WorkflowState.VERIFYING,
        WorkflowState.FAILED,
        WorkflowState.ARCHIVED,
    }),
    WorkflowState.SUBMISSION_REJECTED: frozenset({
        WorkflowState.ARCHIVED,
        WorkflowState.FAILED,
    }),
    WorkflowState.VERIFYING: frozenset({
        WorkflowState.VERIFIED,
        WorkflowState.VERIFY_PENDING,
        WorkflowState.FAILED,
    }),
    WorkflowState.VERIFY_PENDING: frozenset({
        WorkflowState.VERIFYING,
        WorkflowState.VERIFIED,
        WorkflowState.FAILED,
        WorkflowState.ARCHIVED,
    }),
    WorkflowState.VERIFIED: frozenset({
        WorkflowState.ARCHIVED,
    }),
    WorkflowState.RETRY_PENDING: frozenset({
        WorkflowState.NEW,
        WorkflowState.DISCOVERED,
        WorkflowState.SCORED,
        WorkflowState.AUDITED,
        WorkflowState.CONTENT_READY,
        WorkflowState.QUALIFIED,
        WorkflowState.PLACEMENT_CHECK,
        WorkflowState.DRAFTED,
        WorkflowState.APPROVED,
        WorkflowState.PUBLISHING,
        WorkflowState.PUBLISHED,
        WorkflowState.PENDING_MODERATION,
        WorkflowState.VERIFYING,
        WorkflowState.FAILED,
        WorkflowState.ARCHIVED,
    }),
    WorkflowState.FAILED: frozenset({
        WorkflowState.RETRY_PENDING,
        WorkflowState.ARCHIVED,
    }),
    WorkflowState.ARCHIVED: frozenset(),
}


def parse_state(value: str) -> WorkflowState:
    try:
        return WorkflowState(value)
    except ValueError as exc:
        raise ValueError(f"Unknown workflow state: {value!r}") from exc


def can_transition(from_state: WorkflowState, to_state: WorkflowState) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def get_next_action(state: WorkflowState, *, phase: int | None = None) -> str | None:
    active_phase = phase if phase is not None else get_phase()
    if active_phase == 1:
        return NEXT_ACTION_PHASE1.get(state)
    return NEXT_ACTION_PHASE2.get(state)
