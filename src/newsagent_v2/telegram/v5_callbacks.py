"""V5 Telegram callbacks for RUN STORY, FOLLOW, IGNORE, SEE NEXT 5.

RUN STORY now invokes the generation pipeline via GenerationWorker.
FOLLOW/IGNORE/SEE NEXT remain discovery-only.
"""

from __future__ import annotations

import os
from typing import Any, Callable, TYPE_CHECKING

from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.discovery.event_clusterer import NewsEvent

if TYPE_CHECKING:
    from newsagent_v2.v5_generation.generation_worker import GenerationWorker
    from newsagent_v2.v5_generation.provider_preflight import ProviderPreflight

# Callback prefixes
RUN_PREFIX = "run"
FOLLOW_PREFIX = "flw"
IGNORE_PREFIX = "ign"
SEENEXT_PREFIX = "snext"
SEENEXT5_ACTION = "next5"

# Generation confirmation actions (controlled E2E mode)
GENERATE_NOW_PREFIX = "gen"
CANCEL_GENERATION_PREFIX = "cancel"

# Batch/storage keys
V5_BATCH_KEY = "v5_discovery_batch"

# Controlled E2E mode
CONTROLLED_E2E_ENV = "NEWSAGENT_V5_CONTROLLED_E2E"


class V5TelegramStore:
    """Minimal in-memory store for V5 Telegram session state.
    
    In production, this would persist to disk. For now, keeps
    last /make results to support SEE NEXT 5 without discovery.
    """
    
    def __init__(self) -> None:
        self._last_batch: dict[str, Any] | None = None
        self._ranked_events: list[NewsEvent] | None = None
        self._selected_event_ids: set[str] = set()
        self._followed_event_ids: set[str] = set()
        self._ignored_event_ids: set[str] = set()
    
    def save_batch(self, events: list[NewsEvent]) -> None:
        """Save complete ranked batch."""
        self._ranked_events = events
        self._last_batch = {"saved_at": "now", "count": len(events)}
    
    def get_ranked_events(self) -> list[NewsEvent] | None:
        return self._ranked_events
    
    def get_events_slice(self, offset: int = 0, count: int = 5) -> list[NewsEvent]:
        """Get events by rank position (0-indexed)."""
        if self._ranked_events is None:
            return []
        return self._ranked_events[offset:offset + count]
    
    def mark_selected(self, event_id: str) -> bool:
        """Mark event as selected for RUN STORY. Idempotent."""
        if event_id in self._selected_event_ids:
            return False  # Already selected
        self._selected_event_ids.add(event_id)
        return True
    
    def mark_followed(self, event_id: str) -> bool:
        """Mark event as followed."""
        self._followed_event_ids.add(event_id)
        if event_id in self._ignored_event_ids:
            self._ignored_event_ids.remove(event_id)
        return True
    
    def mark_ignored(self, event_id: str) -> bool:
        """Mark event as ignored."""
        self._ignored_event_ids.add(event_id)
        if event_id in self._followed_event_ids:
            self._followed_event_ids.remove(event_id)
        return True
    
    def is_selected(self, event_id: str) -> bool:
        return event_id in self._selected_event_ids
    
    def is_followed(self, event_id: str) -> bool:
        return event_id in self._followed_event_ids
    
    def is_ignored(self, event_id: str) -> bool:
        return event_id in self._ignored_event_ids


class V5CallbackHandler:
    """Handle V5 Telegram callbacks with generation support."""
    
    def __init__(
        self,
        event_store: EventStore | None = None,
        telegram_store: V5TelegramStore | None = None,
        generation_worker: GenerationWorker | None = None,
        preflight: ProviderPreflight | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.event_store = event_store
        self.telegram_store = telegram_store or V5TelegramStore()
        self.generation_worker = generation_worker
        self.preflight = preflight
        self.environ = environ or dict(os.environ)
        self._controlled_e2e = self.environ.get(CONTROLLED_E2E_ENV, "").lower() == "true"
    
    def parse_callback(self, data: str) -> dict[str, str] | None:
        """Parse V5 callback data: action:event_id[:extra]"""
        parts = data.split(":")
        if len(parts) < 2:
            return None
        
        action = parts[0]
        valid_actions = {
            RUN_PREFIX, FOLLOW_PREFIX, IGNORE_PREFIX, SEENEXT_PREFIX,
            GENERATE_NOW_PREFIX, CANCEL_GENERATION_PREFIX,
        }
        if action not in valid_actions:
            return None

        result: dict[str, str] = {"action": action, "event_id": parts[1]}
        if len(parts) > 2:
            result["extra"] = parts[2]
        return result
    
    def _check_providers_ready(self) -> tuple[bool, str]:
        """Check if providers are ready for generation.
        
        Returns (ready, message)
        """
        if self.preflight is None:
            return False, "Provider preflight not configured"
        
        summary = self.preflight.get_readiness_summary()
        if summary["can_write"]:
            return True, "Ready"
        return False, "No providers configured (need GROQ_API_KEY or Vertex)"
    
    def handle_run_story(self, event_id: str) -> dict[str, Any]:
        """RUN STORY: Check providers, persist state, and invoke generation if ready.
        
        This now:
        1. Checks controlled E2E mode
        2. Validates provider readiness
        3. Persists job state to disk
        4. Invokes async generation if configured
        5. Returns progress message immediately (non-blocking)
        """
        # Load event from store
        if self.event_store is None:
            return {"ok": False, "reason": "no_event_store"}
        
        event = self.event_store.get(event_id)
        if event is None:
            return {"ok": False, "reason": "unknown_event", "event_id": event_id}
        
        # Check controlled E2E mode
        if self._controlled_e2e:
            # In controlled mode, mark selected but don't auto-start
            # Instead, show GENERATE NOW / CANCEL confirmation
            is_new = self.telegram_store.mark_selected(event_id)

            # Build confirmation keyboard
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🚀 GENERATE NOW", "callback_data": f"{GENERATE_NOW_PREFIX}:{event_id}"},
                        {"text": "❌ CANCEL", "callback_data": f"{CANCEL_GENERATION_PREFIX}:{event_id}"},
                    ]
                ]
            }

            return {
                "ok": True,
                "action": "run_story",
                "event_id": event_id,
                "headline": event.canonical_title,
                "is_new_selection": is_new,
                "controlled_e2e": True,
                "started": False,
                "awaiting_confirmation": True,
                "message": f"✅ SELECTED: {event.canonical_title}\n\n⚠️ Controlled E2E Mode\nReview the selection, then confirm generation.",
                "reply_markup": keyboard,
            }
        
        # Check provider readiness
        providers_ready, providers_msg = self._check_providers_ready()
        if not providers_ready:
            # Mark selected but cannot generate
            is_new = self.telegram_store.mark_selected(event_id)
            return {
                "ok": True,
                "action": "run_story",
                "event_id": event_id,
                "headline": event.canonical_title,
                "is_new_selection": is_new,
                "providers_ready": False,
                "started": False,
                "message": f"⚠️ SELECTED ({event.canonical_title}): {providers_msg}",
            }
        
        # Mark as selected (idempotent)
        is_new = self.telegram_store.mark_selected(event_id)
        
        # If generation worker is configured, start async generation
        if self.generation_worker is not None:
            result = self.generation_worker.request_generation(
                event=event,
                discovery_run_id=None,  # Could be passed from discovery context
            )
            
            return {
                "ok": True,
                "action": "run_story",
                "event_id": event_id,
                "headline": event.canonical_title,
                "is_new_selection": is_new,
                "providers_ready": True,
                "started": result.get("ok", False),
                "job_id": result.get("job_id"),
                "job_state": result.get("state"),
                "message": self._format_run_message(event.canonical_title, result),
                "job_result": result,
            }
        
        # No generator configured - just selected
        return {
            "ok": True,
            "action": "run_story",
            "event_id": event_id,
            "headline": event.canonical_title,
            "is_new_selection": is_new,
            "providers_ready": True,
            "started": False,
            "message": f"✅ SELECTED: {event.canonical_title}",
        }
    
    def _format_run_message(self, headline: str, result: dict[str, Any]) -> str:
        """Format the message based on generation result."""
        state = result.get("state", "")
        
        if result.get("new") is False:
            return f"♻️ GENERATION IN PROGRESS: {headline}"
        
        if result.get("ok"):
            if result.get("new"):
                return f"🚀 GENERATION STARTED: {headline}"
            return f"✅ SELECTED: {headline}"
        
        if result.get("reason") == "controlled_e2e_active":
            return f"✅ SELECTED ({headline}): Waiting for manual generation start"
        
        if result.get("reason") == "at_capacity":
            return f"⚠️ SELECTED ({headline}): Max active stories reached, queued"
        
        return f"⚠️ SELECTED ({headline}): {result.get('message', 'Unknown status')}"
    
    def handle_follow(self, event_id: str) -> dict[str, Any]:
        """FOLLOW: Persist followed state."""
        if self.event_store is None:
            return {"ok": False, "reason": "no_event_store"}
        
        event = self.event_store.get(event_id)
        if event is None:
            return {"ok": False, "reason": "unknown_event", "event_id": event_id}
        
        self.telegram_store.mark_followed(event_id)
        
        return {
            "ok": True,
            "action": "follow",
            "event_id": event_id,
            "message": f"📌 FOLLOWED: {event.canonical_title}",
        }
    
    def handle_ignore(self, event_id: str) -> dict[str, Any]:
        """IGNORE: Persist ignored state."""
        if self.event_store is None:
            return {"ok": False, "reason": "no_event_store"}
        
        event = self.event_store.get(event_id)
        if event is None:
            return {"ok": False, "reason": "unknown_event", "event_id": event_id}
        
        self.telegram_store.mark_ignored(event_id)
        
        return {
            "ok": True,
            "action": "ignore",
            "event_id": event_id,
            "message": f"🚫 IGNORED: {event.canonical_title}",
        }
    
    def handle_see_next(self, offset: int = 5) -> dict[str, Any]:
        """SEE NEXT 5: Return next batch for rendering."""
        events = self.telegram_store.get_events_slice(offset=offset, count=5)
        
        if not events:
            return {
                "ok": True,
                "action": "see_next",
                "events": [],
                "offset": offset,
                "message": "No more events available.",
            }
        
        return {
            "ok": True,
            "action": "see_next",
            "events": events,
            "offset": offset,
            "has_more": len(self.telegram_store.get_ranked_events() or []) > offset + 5,
            "message": f"Showing events {offset + 1}-{offset + len(events)}",
        }
    
    def handle_generate_now(self, event_id: str) -> dict[str, Any]:
        """GENERATE NOW: Trigger actual generation in controlled E2E mode.

        Requirements:
        - ProviderPreflight passes (writer + image READY)
        - Max active generation = 1 enforced
        - Persistent job reservation
        - Idempotent: duplicate calls return existing job
        """
        if self.event_store is None:
            return {"ok": False, "reason": "no_event_store"}

        event = self.event_store.get(event_id)
        if event is None:
            return {"ok": False, "reason": "unknown_event", "event_id": event_id}

        # Check if event is selected
        if event_id not in self.telegram_store._selected_event_ids:
            return {"ok": False, "reason": "event_not_selected", "message": "RUN STORY first"}

        # ProviderPreflight check
        if self.preflight is not None:
            writer_status = self.preflight.check_writer()
            image_status = self.preflight.check_image()

            if writer_status.status != "READY":
                return {"ok": False, "reason": "writer_not_ready", "message": "Writer not ready"}
            if image_status.status != "READY":
                return {"ok": False, "reason": "image_not_ready", "message": "Image not ready"}

        # Max active generation = 1 enforcement
        if self.generation_worker is not None:
            active_count = self.generation_worker.get_active_job_count()
            if active_count >= 1:
                return {
                    "ok": False,
                    "reason": "max_active_reached",
                    "message": "Another generation is currently active. Complete or cancel it first.",
                    "active_jobs": active_count,
                }

        # Start generation via GenerationWorker
        if self.generation_worker is not None:
            # Check if already have a job for this event (idempotency)
            existing_job = self.generation_worker.get_job_for_event(event_id)
            if existing_job is not None:
                return {
                    "ok": True,
                    "action": "generate_now",
                    "event_id": event_id,
                    "headline": event.canonical_title,
                    "started": True,
                    "job_id": existing_job.job_id,
                    "job_state": existing_job.state,
                    "message": f"Generation already {existing_job.state.lower()}",
                    "new": False,
                }

            result = self.generation_worker.request_generation(
                event=event,
                discovery_run_id=None,
            )

            return {
                "ok": result.get("ok", False),
                "action": "generate_now",
                "event_id": event_id,
                "headline": event.canonical_title,
                "started": result.get("ok", False),
                "job_id": result.get("job_id"),
                "job_state": result.get("state"),
                "message": result.get("message", "Generation started"),
                "new": result.get("new", True),
            }

        return {"ok": False, "reason": "no_generation_worker", "message": "Generation worker not configured"}

    def handle_cancel_generation(self, event_id: str) -> dict[str, Any]:
        """CANCEL: Cancel generation for this event.

        Zero provider calls guaranteed.
        """
        if self.event_store is None:
            return {"ok": False, "reason": "no_event_store"}

        event = self.event_store.get(event_id)
        if event is None:
            return {"ok": False, "reason": "unknown_event", "event_id": event_id}

        # Remove from selected (cleanup)
        if event_id in self.telegram_store._selected_event_ids:
            self.telegram_store._selected_event_ids.discard(event_id)

        return {
            "ok": True,
            "action": "cancel_generation",
            "event_id": event_id,
            "headline": event.canonical_title,
            "message": f"❌ CANCELLED: {event.canonical_title}",
            "provider_calls": 0,
        }

    def handle(self, data: str) -> dict[str, Any]:
        """Main dispatch for V5 callbacks."""
        parsed = self.parse_callback(data)
        if parsed is None:
            return {"ok": False, "reason": "invalid_callback", "data": data[:50]}

        action = parsed["action"]
        event_id = parsed["event_id"]

        if action == RUN_PREFIX:
            return self.handle_run_story(event_id)
        if action == GENERATE_NOW_PREFIX:
            return self.handle_generate_now(event_id)
        if action == CANCEL_GENERATION_PREFIX:
            return self.handle_cancel_generation(event_id)
        elif action == FOLLOW_PREFIX:
            return self.handle_follow(event_id)
        elif action == IGNORE_PREFIX:
            return self.handle_ignore(event_id)
        elif action == SEENEXT_PREFIX:
            # event_id contains the offset
            try:
                offset = int(event_id)
            except ValueError:
                offset = 5
            return self.handle_see_next(offset=offset)
        elif action == GENERATE_NOW_PREFIX:
            return self.handle_generate_now(event_id)
        elif action == CANCEL_GENERATION_PREFIX:
            return self.handle_cancel_generation(event_id)

        return {"ok": False, "reason": "unknown_action", "action": action}


def make_v5_callback_handler(
    event_store: EventStore | None = None,
    telegram_store: V5TelegramStore | None = None,
    generation_worker: GenerationWorker | None = None,
    preflight: ProviderPreflight | None = None,
    environ: dict[str, str] | None = None,
) -> Callable[..., dict[str, Any]]:
    """Factory for V5 callback handler compatible with listener."""
    handler = V5CallbackHandler(
        event_store=event_store,
        telegram_store=telegram_store,
        generation_worker=generation_worker,
        preflight=preflight,
        environ=environ or dict(os.environ),
    )
    return handler.handle
