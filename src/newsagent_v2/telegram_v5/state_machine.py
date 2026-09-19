"""Story State Machine - manages event lifecycle states.

States:
- DISCOVERED
- AVAILABLE_IN_NEWS_DESK
- SELECTED
- RESEARCHING
- GENERATING
- QA
- REVIEW
- REVISING
- APPROVED
- PUBLISHING
- PUBLISHED
- FOLLOWING
- IGNORED
- REJECTED
- FAILED
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


class EventState(Enum):
    """Event lifecycle states."""
    DISCOVERED = "DISCOVERED"
    AVAILABLE_IN_NEWS_DESK = "AVAILABLE_IN_NEWS_DESK"
    SELECTED = "SELECTED"
    RESEARCHING = "RESEARCHING"
    GENERATING = "GENERATING"
    QA = "QA"
    REVIEW = "REVIEW"
    REVISING = "REVISING"
    APPROVED = "APPROVED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FOLLOWING = "FOLLOWING"
    IGNORED = "IGNORED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


@dataclass
class StateTransition:
    """Result of a state transition attempt."""
    success: bool
    from_state: str
    to_state: str
    reason: str | None = None


class StoryStateMachine:
    """State machine for event lifecycle.

    Enforces valid state transitions.
    Guards against duplicate operations.
    """

    # Valid transitions: current_state -> {allowed_next_states}
    VALID_TRANSITIONS: dict[str, set[str]] = {
        EventState.DISCOVERED.value: {
            EventState.AVAILABLE_IN_NEWS_DESK.value,
            EventState.IGNORED.value,
        },
        EventState.AVAILABLE_IN_NEWS_DESK.value: {
            EventState.SELECTED.value,
            EventState.IGNORED.value,
            EventState.FOLLOWING.value,
        },
        EventState.FOLLOWING.value: {
            EventState.AVAILABLE_IN_NEWS_DESK.value,
            EventState.SELECTED.value,
            EventState.IGNORED.value,
        },
        EventState.SELECTED.value: {
            EventState.RESEARCHING.value,
            EventState.AVAILABLE_IN_NEWS_DESK.value,
        },
        EventState.RESEARCHING.value: {
            EventState.GENERATING.value,
            EventState.FAILED.value,
            EventState.AVAILABLE_IN_NEWS_DESK.value,
        },
        EventState.GENERATING.value: {
            EventState.QA.value,
            EventState.FAILED.value,
            EventState.REVIEW.value,
        },
        EventState.QA.value: {
            EventState.REVIEW.value,
            EventState.REVISING.value,
            EventState.REJECTED.value,
            EventState.FAILED.value,
        },
        EventState.REVIEW.value: {
            EventState.APPROVED.value,
            EventState.REJECTED.value,
            EventState.REVISING.value,
        },
        EventState.REVISING.value: {
            EventState.QA.value,
            EventState.REVIEW.value,
            EventState.FAILED.value,
        },
        EventState.APPROVED.value: {
            EventState.PUBLISHING.value,
            EventState.PUBLISHED.value,
        },
        EventState.PUBLISHING.value: {
            EventState.PUBLISHED.value,
            EventState.FAILED.value,
        },
        EventState.IGNORED.value: {
            EventState.AVAILABLE_IN_NEWS_DESK.value,  # Can un-ignore
        },
        EventState.REJECTED.value: {
            EventState.AVAILABLE_IN_NEWS_DESK.value,  # Can un-reject
        },
        EventState.FAILED.value: {
            EventState.RESEARCHING.value,  # Retry
            EventState.AVAILABLE_IN_NEWS_DESK.value,
        },
        EventState.PUBLISHED.value: set(),  # Terminal state
    }

    # Transitions that require unique operation (duplicate=reject)
    UNIQUE_ACTION_STATES: set[str] = {
        EventState.SELECTED.value,
        EventState.RESEARCHING.value,
        EventState.GENERATING.value,
        EventState.PUBLISHING.value,
    }

    def __init__(self):
        self._active_operations: set[tuple[str, str]] = set()  # (event_id, operation)

    def validate_transition(
        self,
        event_id: str,
        current_state: str,
        target_state: str,
    ) -> StateTransition:
        """Validate a state transition.

        Returns:
            StateTransition with success status and reason.
        """
        # Check for unique operation conflict
        if target_state in self.UNIQUE_ACTION_STATES:
            operation_key = (event_id, target_state)
            if operation_key in self._active_operations:
                return StateTransition(
                    success=False,
                    from_state=current_state,
                    to_state=target_state,
                    reason=f"Operation already in progress: {target_state}",
                )

        # Check valid transition
        allowed = self.VALID_TRANSITIONS.get(current_state, set())
        if target_state not in allowed:
            return StateTransition(
                success=False,
                from_state=current_state,
                to_state=target_state,
                reason=f"Invalid transition: {current_state} -> {target_state}",
            )

        return StateTransition(
            success=True,
            from_state=current_state,
            to_state=target_state,
        )

    def mark_operation_active(
        self,
        event_id: str,
        operation: str,
    ) -> bool:
        """Mark an operation as active (to prevent duplicates)."""
        key = (event_id, operation)
        if key in self._active_operations:
            return False
        self._active_operations.add(key)
        return True

    def mark_operation_complete(
        self,
        event_id: str,
        operation: str,
    ) -> None:
        """Mark an operation as complete."""
        key = (event_id, operation)
        self._active_operations.discard(key)

    def is_operation_active(self, event_id: str, operation: str) -> bool:
        """Check if an operation is currently active."""
        return (event_id, operation) in self._active_operations

    def can_run_story(self, current_state: str) -> tuple[bool, str | None]:
        """Check if RUN STORY can be triggered."""
        if current_state in {
            EventState.GENERATING.value,
            EventState.RESEARCHING.value,
            EventState.QA.value,
            EventState.PUBLISHING.value,
        }:
            return False, "Generation already in progress"
        return True, None

    def can_follow(self, current_state: str) -> tuple[bool, str | None]:
        """Check if FOLLOW can be triggered."""
        if current_state == EventState.IGNORED.value:
            return False, "Cannot follow ignored event"
        return True, None

    def can_ignore(self, current_state: str) -> tuple[bool, str | None]:
        """Check if IGNORE can be triggered."""
        terminal_states = {
            EventState.PUBLISHED.value,
            EventState.PUBLISHING.value,
            EventState.REJECTED.value,
        }
        if current_state in terminal_states:
            return False, f"Cannot ignore event in {current_state} state"
        return True, None

    def get_valid_next_states(self, current_state: str) -> list[str]:
        """Get valid next states from current state."""
        return sorted(self.VALID_TRANSITIONS.get(current_state, set()))
