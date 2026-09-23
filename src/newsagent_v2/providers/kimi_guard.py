"""Kimi safety guard - pre-request validation and token estimation.

Provides budget-aware guard layer before HTTP dispatch.
Fails closed on missing context or exceeded limits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .kimi_budget import KimiBudgetError, KimiBudgetManager, get_budget_manager


@dataclass
class GuardResult:
    """Result of guard check."""

    allowed: bool
    reservation: dict[str, Any] | None = None
    error: str | None = None
    reason: str | None = None


def estimate_tokens(messages: list[dict[str, str]], max_completion: int) -> tuple[int, int]:
    """Conservative token estimation without external dependencies.

    Uses simple heuristic: ~4 chars per token + message overhead.
    This is intentionally conservative (over-estimates slightly).

    Returns: (estimated_prompt_tokens, estimated_completion_tokens)
    """
    # Message overhead (role markers, delimiters)
    MESSAGE_OVERHEAD = 4

    prompt_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        prompt_chars += len(content) + MESSAGE_OVERHEAD

    # Conservative: 3 chars per token (rounds up more than 4)
    estimated_prompt = (prompt_chars // 3) + 10

    # Completion estimate = requested max
    estimated_completion = max_completion

    return estimated_prompt, estimated_completion


def analyze_prompt_composition(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Analyze prompt without storing full content.

    Returns token estimates by section for observability.
    """
    system_tokens = 0
    instruction_tokens = 0
    evidence_tokens = 0
    other_tokens = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        estimated = (len(content) // 3) + 4  # Same estimator

        if role == "system":
            system_tokens += estimated
        elif role == "user":
            # Try to identify instruction vs evidence in user message
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    if "instruction" in parsed:
                        instruction_tokens += (len(str(parsed["instruction"])) // 3) + 2
                    if "evidence_packet" in parsed:
                        evidence_tokens += (len(json.dumps(parsed["evidence_packet"])) // 3) + 2
                    # Everything else in the user message
                    other_tokens += max(0, estimated - instruction_tokens - evidence_tokens)
                else:
                    other_tokens += estimated
            except json.JSONDecodeError:
                instruction_tokens += estimated
        else:
            other_tokens += estimated

    total = system_tokens + instruction_tokens + evidence_tokens + other_tokens

    return {
        "system_tokens": system_tokens,
        "instruction_tokens": instruction_tokens,
        "evidence_tokens": evidence_tokens,
        "other_tokens": other_tokens,
        "total_estimated": total,
    }


class KimiGuard:
    """Pre-request safety guard for Kimi API calls.

    Usage:
        guard = KimiGuard(event_id="evt-123")
        result = guard.check_before_request(messages, max_completion_tokens, stage="initial_writer")
        if result.allowed:
            # Make HTTP request...
            response = requests.post(...)
            usage = response.json().get("usage", {})
            guard.reconcile_after_request(
                reservation=result.reservation,
                success=True,
                usage=usage,
            )
        else:
            # Fail closed - do not dispatch
            raise KimiBudgetError(result.reason)
    """

    def __init__(
        self,
        event_id: str | None = None,
        manager: KimiBudgetManager | None = None,
    ):
        self.event_id = event_id
        self.manager = manager or get_budget_manager()
        self._reservation: dict[str, Any] | None = None

    def check_before_request(
        self,
        messages: list[dict[str, str]],
        max_completion_tokens: int,
        stage: str,
        event_id: str | None = None,
    ) -> GuardResult:
        """Validate and reserve before HTTP dispatch.

        Args:
            messages: Chat completion messages
            max_completion_tokens: Requested completion limit
            stage: Operation stage (initial_writer, schema_fallback, etc.)
            event_id: Optional event_id (uses instance default if not provided)

        Returns:
            GuardResult with allowed=True and reservation if check passes
        """
        effective_event_id = event_id or self.event_id
        if not effective_event_id:
            return GuardResult(
                allowed=False,
                error="KIMI_BUDGET_EXCEEDED",
                reason="MISSING_EVENT_BUDGET_CONTEXT",
            )

        # Estimate tokens
        estimated_prompt, estimated_completion = estimate_tokens(
            messages, max_completion_tokens
        )

        # Analyze composition for observability
        composition = analyze_prompt_composition(messages)

        try:
            reservation = self.manager.check_and_reserve(
                event_id=effective_event_id,
                stage=stage,
                estimated_prompt=estimated_prompt,
                requested_completion=estimated_completion,
            )

            # Add composition to reservation for telemetry
            reservation["prompt_composition"] = composition

            self._reservation = reservation
            return GuardResult(allowed=True, reservation=reservation)

        except KimiBudgetError as e:
            return GuardResult(
                allowed=False,
                error="KIMI_BUDGET_EXCEEDED",
                reason=e.reason,
            )

    def reconcile_after_request(
        self,
        success: bool,
        usage: dict[str, Any] | None = None,
        http_status: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile with actual provider-reported usage.

        Args:
            success: Whether HTTP request succeeded
            usage: Provider "usage" dict with prompt_tokens, completion_tokens, total_tokens
            http_status: HTTP status code if available
            error: Error message if failed

        Returns:
            Reconciliation result
        """
        if self._reservation is None:
            return {"ok": False, "error": "no_active_reservation"}

        actual_prompt = None
        actual_completion = None
        actual_total = None

        if usage and isinstance(usage, dict):
            actual_prompt = usage.get("prompt_tokens")
            actual_completion = usage.get("completion_tokens")
            actual_total = usage.get("total_tokens")

        blocked = not success and error is not None
        block_reason = error if blocked else None

        reservation = self._reservation
        self._reservation = None  # Clear after reconciliation

        return self.manager.reconcile_usage(
            reservation=reservation,
            actual_prompt=actual_prompt,
            actual_completion=actual_completion,
            actual_total=actual_total,
            success=success,
            http_status=http_status,
            blocked=blocked,
            block_reason=block_reason,
        )


def guarded_kimi_complete(
    event_id: str,
    messages: list[dict[str, str]],
    max_completion_tokens: int,
    stage: str,
    complete_fn: callable,
) -> tuple[bool, Any, dict[str, Any] | None]:
    """Wrapper for complete Kimi request with full guard.

    Args:
        event_id: Story identifier
        messages: Chat messages
        max_completion_tokens: Requested completion limit
        stage: Operation stage
        complete_fn: Function that makes actual HTTP request and returns (success, result, usage)

    Returns:
        (success, result_or_error, budget_info)
    """
    guard = KimiGuard(event_id=event_id)

    # Pre-check
    check = guard.check_before_request(messages, max_completion_tokens, stage)
    if not check.allowed:
        return False, check.reason, {"blocked": True, "reason": check.reason}

    # Make actual request
    try:
        success, result, usage = complete_fn()
        http_status = None
        if isinstance(result, dict):
            http_status = result.get("status_code")
    except Exception as e:
        # Reconcile even on exception
        reconcile = guard.reconcile_after_request(
            success=False,
            usage=None,
            http_status=None,
            error=str(e),
        )
        return False, str(e), reconcile

    # Reconcile
    reconcile = guard.reconcile_after_request(
        success=success,
        usage=usage,
        http_status=http_status,
    )

    return success, result, reconcile


def format_usage_summary(story_summary: dict[str, Any]) -> str:
    """Format story usage summary for display."""
    lines = [
        "KIMI USAGE",
        f"Requests: {story_summary.get('requests', 0)}",
        f"Prompt: {story_summary.get('prompt_tokens', 0):,}",
        f"Completion: {story_summary.get('completion_tokens', 0):,}",
        f"Total: {story_summary.get('total_tokens', 0):,}",
        f"Story hard limit: {story_summary.get('hard_limit', 40000):,}",
        f"Usage: {story_summary.get('percent_used', '0%')}",
        f"Status: {story_summary.get('status', 'UNKNOWN')}",
    ]

    largest = story_summary.get("largest_request", {})
    if largest:
        lines.append(
            f"Largest request: {largest.get('stage', 'unknown')} — "
            f"{largest.get('total_tokens', 0):,}"
        )

    if story_summary.get("legacy_unknown"):
        lines.append("WARNING: legacy_budget_unknown - usage may be incomplete")

    return "\n".join(lines)
