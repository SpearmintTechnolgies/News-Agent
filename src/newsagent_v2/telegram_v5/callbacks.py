"""Callback handler for V5 Telegram buttons.

Handles:
- RUN STORY
- FOLLOW
- IGNORE
- (APPROVE/REJECT handled by existing system)
"""

from __future__ import annotations

from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent
from newsagent_v2.discovery.event_store import EventStore

from .state_machine import EventState, StoryStateMachine, StateTransition


class CallbackHandler:
    """Handler for V5 Telegram callbacks.

    Features:
    - Duplicate RUN STORY protection
    - State validation
    - Event persistence
    - Zero paid calls
    """

    def __init__(
        self,
        event_store: EventStore,
        state_machine: StoryStateMachine,
    ):
        self.event_store = event_store
        self.state_machine = state_machine
        self._stats = {"run_story": 0, "follow": 0, "ignore": 0, "errors": 0}

    def parse_callback(self, data: str) -> tuple[str, str, str]:
        """Parse callback data.

        Format: v5:<action>:<event_id>
        """
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "v5":
            raise ValueError(f"Invalid callback format: {data}")
        return parts[0], parts[1], parts[2]

    def handle_run_story(self, event_id: str) -> dict[str, Any]:
        """Handle RUN STORY callback.

        Validates state, protects against duplicates, prepares for generation.
        """
        self._stats["run_story"] += 1

        # Load event
        event = self.event_store.get(event_id)
        if not event:
            return {
                "ok": False,
                "error": "event_not_found",
                "event_id": event_id,
            }

        current_state = event.state

        # Check if can run
        can_run, reason = self.state_machine.can_run_story(current_state)
        if not can_run:
            return {
                "ok": False,
                "error": "already_in_progress",
                "event_id": event_id,
                "current_state": current_state,
                "reason": reason,
            }

        # Mark operation active
        if not self.state_machine.mark_operation_active(event_id, EventState.SELECTED.value):
            return {
                "ok": False,
                "error": "operation_in_progress",
                "event_id": event_id,
            }

        # Validate transition
        transition = self.state_machine.validate_transition(
            event_id,
            current_state,
            EventState.SELECTED.value,
        )

        if not transition.success:
            self.state_machine.mark_operation_complete(event_id, EventState.SELECTED.value)
            return {
                "ok": False,
                "error": "invalid_transition",
                "event_id": event_id,
                "reason": transition.reason,
            }

        # Update state
        event.state = EventState.SELECTED.value
        self.event_store.save(event)

        # Prepare for generation (adapter to existing pipeline)
        prepared = self._prepare_for_generation(event)

        return {
            "ok": True,
            "action": "RUN_STORY",
            "event_id": event_id,
            "can_proceed": can_run,
            "prepared": prepared,
            "message": "Story selected. Ready for generation.",
        }

    def handle_follow(self, event_id: str) -> dict[str, Any]:
        """Handle FOLLOW callback.

        Marks event as followed for future priority.
        """
        self._stats["follow"] += 1

        # Load event
        event = self.event_store.get(event_id)
        if not event:
            return {
                "ok": False,
                "error": "event_not_found",
                "event_id": event_id,
            }

        current_state = event.state

        # Check if can follow
        can_follow, reason = self.state_machine.can_follow(current_state)
        if not can_follow:
            return {
                "ok": False,
                "error": "cannot_follow",
                "event_id": event_id,
                "reason": reason,
            }

        # Validate transition
        transition = self.state_machine.validate_transition(
            event_id,
            current_state,
            EventState.FOLLOWING.value,
        )

        if not transition.success:
            return {
                "ok": False,
                "error": "invalid_transition",
                "event_id": event_id,
                "reason": transition.reason,
            }

        # Update event
        event.followed = True
        event.ignored = False
        event.state = EventState.FOLLOWING.value

        self.event_store.mark_followed(event_id, followed=True)

        return {
            "ok": True,
            "action": "FOLLOW",
            "event_id": event_id,
            "message": "Event followed. Future developments will be prioritized.",
        }

    def handle_ignore(self, event_id: str) -> dict[str, Any]:
        """Handle IGNORE callback.

        Marks event as ignored to avoid repeated presentation.
        """
        self._stats["ignore"] += 1

        # Load event
        event = self.event_store.get(event_id)
        if not event:
            return {
                "ok": False,
                "error": "event_not_found",
                "event_id": event_id,
            }

        current_state = event.state

        # Check if can ignore
        can_ignore, reason = self.state_machine.can_ignore(current_state)
        if not can_ignore:
            return {
                "ok": False,
                "error": "cannot_ignore",
                "event_id": event_id,
                "reason": reason,
            }

        # Validate transition
        transition = self.state_machine.validate_transition(
            event_id,
            current_state,
            EventState.IGNORED.value,
        )

        if not transition.success:
            return {
                "ok": False,
                "error": "invalid_transition",
                "event_id": event_id,
                "reason": transition.reason,
            }

        # Update event
        event.ignored = True
        event.followed = False
        event.state = EventState.IGNORED.value

        self.event_store.mark_ignored(event_id, ignored=True)

        return {
            "ok": True,
            "action": "IGNORE",
            "event_id": event_id,
            "message": "Event ignored. Will not be presented again unless significant new developments.",
        }

    def _prepare_for_generation(self, event: NewsEvent) -> dict[str, Any]:
        """Prepare event for generation pipeline.

        Creates adapter format compatible with existing generation engine.
        """
        # Extract representative report (highest authority)
        representative = max(
            event.reports,
            key=lambda r: r.source_authority,
        )

        # Build evidence pack format
        evidence = [
            {
                "source": r.source,
                "source_type": "primary" if r.source_authority >= 0.9 else "secondary",
                "url": r.url,
                "title": r.headline,
                "published": r.published_at,
                "extracted_text": r.description[:1000] if r.description else "",
                "source_authority": r.source_authority,
            }
            for r in event.reports[:10]  # Top 10 sources
        ]

        return {
            "event_id": event.event_id,
            "headline": event.canonical_title,
            "topic": event.topic,
            "entities": list(event.entities),
            "source_count": event.source_count,
            "primary_sources": event.primary_sources,
            "evidence": evidence,
            "sources": [r.source for r in event.reports],
        }

    def handle_callback(self, data: str) -> dict[str, Any]:
        """Main callback handler.

        Args:
            data: Callback data string (format: v5:<action>:<event_id>)

        Returns:
            Result dict with ok status
        """
        try:
            _, action, event_id = self.parse_callback(data)
        except ValueError as e:
            self._stats["errors"] += 1
            return {
                "ok": False,
                "error": "invalid_callback_format",
                "message": str(e),
            }

        if action == "run":
            return self.handle_run_story(event_id)
        elif action == "follow":
            return self.handle_follow(event_id)
        elif action == "ignore":
            return self.handle_ignore(event_id)
        else:
            self._stats["errors"] += 1
            return {
                "ok": False,
                "error": "unknown_action",
                "action": action,
            }

    def get_stats(self) -> dict[str, Any]:
        """Get handler statistics."""
        return dict(self._stats)
