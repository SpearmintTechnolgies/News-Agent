"""Kimi budget accounting - thread-safe per-story and global limits.

Persistent state:
- Per-story budget: STORY-<event_id>/kimibudget/budget.json
- Per-request audit: STORY-<event_id>/kimibudget/requests/<request_id>.json
- Global circuit: <store_root>/global_kimi_circuit.json

Security:
- Never persists API keys, auth headers, full prompts, or evidence
- Atomic file writes (temp + rename)
- Thread-safe per-event locking + process-level global lock
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from newsagent_v2.benchmark.input import write_json_utf8

# Environment configuration - centralized defaults
ENV_MAX_PROMPT_TOKENS = "NEWSAGENT_V2_KIMI_MAX_PROMPT_TOKENS_PER_REQUEST"
ENV_MAX_COMPLETION_TOKENS = "NEWSAGENT_V2_KIMI_MAX_COMPLETION_TOKENS_PER_REQUEST"
ENV_MAX_REQUESTS_PER_STORY = "NEWSAGENT_V2_KIMI_MAX_REQUESTS_PER_STORY"
ENV_MAX_TOKENS_PER_STORY = "NEWSAGENT_V2_KIMI_MAX_TOTAL_TOKENS_PER_STORY"
ENV_GLOBAL_MAX_REQUESTS = "NEWSAGENT_V2_KIMI_GLOBAL_MAX_REQUESTS"
ENV_GLOBAL_MAX_TOKENS = "NEWSAGENT_V2_KIMI_GLOBAL_MAX_TOKENS"

DEFAULT_MAX_PROMPT_TOKENS = 15000
DEFAULT_MAX_COMPLETION_TOKENS = 1500
DEFAULT_MAX_REQUESTS_PER_STORY = 5
DEFAULT_MAX_TOKENS_PER_STORY = 40000
DEFAULT_GLOBAL_MAX_REQUESTS = 50
DEFAULT_GLOBAL_MAX_TOKENS = 250000

# Hard safety constants (not configurable via env - these are the guaranteed ceilings)
HARD_MAX_PROMPT_TOKENS = 15000
HARD_MAX_COMPLETION_TOKENS = 1500
HARD_MAX_REQUESTS_PER_STORY = 5
HARD_MAX_TOKENS_PER_STORY = 40000
HARD_GLOBAL_MAX_REQUESTS = 50
HARD_GLOBAL_MAX_TOKENS = 250000


class KimiBudgetError(Exception):
    """Budget error with machine-readable reason."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        self.reason = reason
        self.details = details or {}
        super().__init__(f"KIMI_BUDGET_EXCEEDED: {reason}")


@dataclass
class StoryBudget:
    """Per-story cumulative budget state."""

    event_id: str
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    hard_limit: int = HARD_MAX_TOKENS_PER_STORY
    status: str = "NORMAL"  # NORMAL, HIGH, BLOCKED
    largest_request: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    legacy_unknown: bool = False  # True if budget was created for pre-existing story

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "request_count": self.request_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "hard_limit": self.hard_limit,
            "status": self.status,
            "largest_request": self.largest_request,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "legacy_unknown": self.legacy_unknown,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoryBudget:
        return cls(
            event_id=d["event_id"],
            request_count=d.get("request_count", 0),
            prompt_tokens=d.get("prompt_tokens", 0),
            completion_tokens=d.get("completion_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
            hard_limit=d.get("hard_limit", HARD_MAX_TOKENS_PER_STORY),
            status=d.get("status", "NORMAL"),
            largest_request=d.get("largest_request", {}),
            created_at=d.get("created_at", "")
            or datetime.now(timezone.utc).isoformat(),
            updated_at=d.get("updated_at", "")
            or datetime.now(timezone.utc).isoformat(),
            legacy_unknown=d.get("legacy_unknown", False),
        )

    def percent_used(self) -> float:
        if self.hard_limit <= 0:
            return 0.0
        return round((self.total_tokens / self.hard_limit) * 100, 1)

    def update_status(self) -> None:
        """Update status based on cumulative usage."""
        if self.total_tokens > self.hard_limit:
            self.status = "BLOCKED"
        elif self.total_tokens > 10000:
            self.status = "HIGH"
        else:
            self.status = "NORMAL"


@dataclass
class GlobalCircuit:
    """Global emergency circuit state."""

    total_requests: int = 0
    total_tokens: int = 0
    is_open: bool = False
    opened_at: str | None = None
    opened_reason: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "is_open": self.is_open,
            "opened_at": self.opened_at,
            "opened_reason": self.opened_reason,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GlobalCircuit:
        return cls(
            total_requests=d.get("total_requests", 0),
            total_tokens=d.get("total_tokens", 0),
            is_open=d.get("is_open", False),
            opened_at=d.get("opened_at"),
            opened_reason=d.get("opened_reason"),
            updated_at=d.get("updated_at", "")
            or datetime.now(timezone.utc).isoformat(),
        )


class KimiBudgetStore:
    """Persistent budget storage with thread-safe access."""

    _global_lock = threading.Lock()
    _event_locks: dict[str, threading.Lock] = {}
    _locks_lock = threading.Lock()

    def __init__(self, root: Path | None = None):
        if root is None:
            # Default to v5_stories output
            self.root = Path(__file__).resolve().parents[3] / "output" / "v5_stories"
        else:
            self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _story_dir(self, event_id: str) -> Path:
        safe_id = "".join(c for c in event_id if c.isalnum() or c in "-_.")
        return self.root / f"STORY-{safe_id}"

    def _budget_dir(self, event_id: str) -> Path:
        d = self._story_dir(event_id) / "kimibudget"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _global_circuit_path(self) -> Path:
        return self.root / "global_kimi_circuit.json"

    def _lock_for_event(self, event_id: str) -> threading.Lock:
        with self._locks_lock:
            if event_id not in self._event_locks:
                self._event_locks[event_id] = threading.Lock()
            return self._event_locks[event_id]

    def load_story_budget(self, event_id: str) -> StoryBudget | None:
        """Load story budget if exists."""
        path = self._budget_dir(event_id) / "budget.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return StoryBudget.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def save_story_budget(self, budget: StoryBudget) -> None:
        """Save story budget atomically."""
        budget.updated_at = datetime.now(timezone.utc).isoformat()
        budget.update_status()

        budget_dir = self._budget_dir(budget.event_id)
        path = budget_dir / "budget.json"
        temp_path = budget_dir / f"budget.{uuid.uuid4().hex[:8]}.tmp"

        write_json_utf8(temp_path, budget.to_dict())
        temp_path.replace(path)

    def create_for_new_story(self, event_id: str) -> StoryBudget:
        """Create fresh budget for new story generation."""
        budget = StoryBudget(event_id=event_id)
        self.save_story_budget(budget)
        return budget

    def get_or_create_for_story(self, event_id: str, is_new_story: bool) -> StoryBudget:
        """Get existing budget or create with appropriate legacy flag.

        Args:
            event_id: Story identifier
            is_new_story: True if this is initial generation, False if revision
                         of existing story

        Returns:
            StoryBudget (existing or newly created)
        """
        existing = self.load_story_budget(event_id)
        if existing is not None:
            return existing

        # No existing budget - create with appropriate legacy flag
        budget = StoryBudget(event_id=event_id)
        if not is_new_story:
            # Revision of story that predates budget tracking
            budget.legacy_unknown = True
        self.save_story_budget(budget)
        return budget

    def load_global_circuit(self) -> GlobalCircuit:
        """Load global circuit state."""
        path = self._global_circuit_path()
        if not path.is_file():
            return GlobalCircuit()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return GlobalCircuit.from_dict(data)
        except (json.JSONDecodeError, TypeError):
            return GlobalCircuit()

    def save_global_circuit(self, circuit: GlobalCircuit) -> None:
        """Save global circuit atomically."""
        circuit.updated_at = datetime.now(timezone.utc).isoformat()

        temp_path = self.root / f"global_circuit.{uuid.uuid4().hex[:8]}.tmp"
        final_path = self._global_circuit_path()

        write_json_utf8(temp_path, circuit.to_dict())
        temp_path.replace(final_path)

    def save_request_audit(
        self,
        event_id: str,
        request_id: str,
        audit: dict[str, Any],
    ) -> None:
        """Save individual request audit (no secrets, no prompt content)."""
        budget_dir = self._budget_dir(event_id)
        requests_dir = budget_dir / "requests"
        requests_dir.mkdir(parents=True, exist_ok=True)

        path = requests_dir / f"{request_id}.json"
        write_json_utf8(path, audit)


class KimiBudgetManager:
    """High-level budget manager with atomic reservations."""

    def __init__(self, store: KimiBudgetStore | None = None):
        self.store = store or KimiBudgetStore()

    def _get_config(self) -> dict[str, int]:
        """Load configuration from environment with safe defaults."""
        return {
            "max_prompt_tokens": min(
                int(os.getenv(ENV_MAX_PROMPT_TOKENS, DEFAULT_MAX_PROMPT_TOKENS)),
                HARD_MAX_PROMPT_TOKENS,
            ),
            "max_completion_tokens": min(
                int(os.getenv(ENV_MAX_COMPLETION_TOKENS, DEFAULT_MAX_COMPLETION_TOKENS)),
                HARD_MAX_COMPLETION_TOKENS,
            ),
            "max_requests_per_story": min(
                int(os.getenv(ENV_MAX_REQUESTS_PER_STORY, DEFAULT_MAX_REQUESTS_PER_STORY)),
                HARD_MAX_REQUESTS_PER_STORY,
            ),
            "max_tokens_per_story": min(
                int(os.getenv(ENV_MAX_TOKENS_PER_STORY, DEFAULT_MAX_TOKENS_PER_STORY)),
                HARD_MAX_TOKENS_PER_STORY,
            ),
            "global_max_requests": min(
                int(os.getenv(ENV_GLOBAL_MAX_REQUESTS, DEFAULT_GLOBAL_MAX_REQUESTS)),
                HARD_GLOBAL_MAX_REQUESTS,
            ),
            "global_max_tokens": min(
                int(os.getenv(ENV_GLOBAL_MAX_TOKENS, DEFAULT_GLOBAL_MAX_TOKENS)),
                HARD_GLOBAL_MAX_TOKENS,
            ),
        }

    def check_and_reserve(
        self,
        event_id: str,
        stage: str,
        estimated_prompt: int,
        requested_completion: int,
    ) -> dict[str, Any]:
        """Atomic check and reservation before HTTP dispatch.

        Returns:
            Reservation dict with request_id for later reconciliation.

        Raises:
            KimiBudgetError: If any limit would be exceeded.
        """
        if not event_id:
            raise KimiBudgetError(
                "MISSING_EVENT_BUDGET_CONTEXT",
                {"event_id": event_id, "stage": stage},
            )

        cfg = self._get_config()

        # Pre-flight checks (fail fast before locking)
        if estimated_prompt > cfg["max_prompt_tokens"]:
            raise KimiBudgetError(
                "MAX_PROMPT_TOKENS_PER_REQUEST",
                {
                    "estimated": estimated_prompt,
                    "limit": cfg["max_prompt_tokens"],
                    "hard_limit": HARD_MAX_PROMPT_TOKENS,
                },
            )

        if requested_completion > cfg["max_completion_tokens"]:
            raise KimiBudgetError(
                "MAX_COMPLETION_TOKENS_PER_REQUEST",
                {
                    "requested": requested_completion,
                    "limit": cfg["max_completion_tokens"],
                    "hard_limit": HARD_MAX_COMPLETION_TOKENS,
                },
            )

        # Acquire event lock + global lock (deterministic order to prevent deadlock)
        event_lock = self.store._lock_for_event(event_id)
        request_id = str(uuid.uuid4())[:16]
        timestamp = datetime.now(timezone.utc).isoformat()

        with KimiBudgetStore._global_lock:
            with event_lock:
                # Load both budgets under full lock
                story = self.store.load_story_budget(event_id)
                if story is None:
                    # Fail closed - should have been created earlier
                    raise KimiBudgetError(
                        "MISSING_EVENT_BUDGET_CONTEXT",
                        {"event_id": event_id, "reason": "budget_not_initialized"},
                    )

                global_circuit = self.store.load_global_circuit()

                # Check global emergency circuit
                if global_circuit.is_open:
                    raise KimiBudgetError(
                        "GLOBAL_EMERGENCY_CIRCUIT_OPEN",
                        {
                            "opened_at": global_circuit.opened_at,
                            "reason": global_circuit.opened_reason,
                            "global_requests": global_circuit.total_requests,
                            "global_tokens": global_circuit.total_tokens,
                        },
                    )

                # Check story request limit
                if story.request_count >= cfg["max_requests_per_story"]:
                    story.status = "BLOCKED"
                    self.store.save_story_budget(story)
                    raise KimiBudgetError(
                        "MAX_REQUESTS_PER_STORY",
                        {
                            "story_requests": story.request_count,
                            "limit": cfg["max_requests_per_story"],
                        },
                    )

                # Check story token limit (conservative projection)
                projected_total = story.total_tokens + estimated_prompt + requested_completion
                if projected_total > cfg["max_tokens_per_story"]:
                    story.status = "BLOCKED"
                    self.store.save_story_budget(story)
                    raise KimiBudgetError(
                        "MAX_TOTAL_TOKENS_PER_STORY",
                        {
                            "current": story.total_tokens,
                            "projected": projected_total,
                            "limit": cfg["max_tokens_per_story"],
                        },
                    )

                # Check global limits
                if global_circuit.total_requests + 1 > cfg["global_max_requests"]:
                    global_circuit.is_open = True
                    global_circuit.opened_at = timestamp
                    global_circuit.opened_reason = "global_max_requests_exceeded"
                    self.store.save_global_circuit(global_circuit)
                    raise KimiBudgetError(
                        "GLOBAL_EMERGENCY_CIRCUIT_OPEN",
                        {"reason": "global_max_requests_exceeded"},
                    )

                if global_circuit.total_tokens + estimated_prompt + requested_completion > cfg["global_max_tokens"]:
                    global_circuit.is_open = True
                    global_circuit.opened_at = timestamp
                    global_circuit.opened_reason = "global_max_tokens_exceeded"
                    self.store.save_global_circuit(global_circuit)
                    raise KimiBudgetError(
                        "GLOBAL_EMERGENCY_CIRCUIT_OPEN",
                        {"reason": "global_max_tokens_exceeded"},
                    )

                # All checks passed - reserve pessimistically
                story.request_count += 1

                # Reserve pessimistic tokens (will be reconciled after actual response)
                story.total_tokens += estimated_prompt + requested_completion

                global_circuit.total_requests += 1
                global_circuit.total_tokens += estimated_prompt + requested_completion

                self.store.save_story_budget(story)
                self.store.save_global_circuit(global_circuit)

                # Return reservation for completion
                return {
                    "request_id": request_id,
                    "event_id": event_id,
                    "stage": stage,
                    "timestamp": timestamp,
                    "estimated_prompt": estimated_prompt,
                    "requested_completion": requested_completion,
                    "pessimistic_total": estimated_prompt + requested_completion,
                }

    def reconcile_usage(
        self,
        reservation: dict[str, Any],
        actual_prompt: int | None,
        actual_completion: int | None,
        actual_total: int | None,
        success: bool,
        http_status: int | None = None,
        blocked: bool = False,
        block_reason: str | None = None,
        model: str = "",
    ) -> dict[str, Any]:
        """Reconcile reservation with actual provider-reported usage.

        Called after HTTP response (success or failure).
        """
        event_id = reservation["event_id"]
        request_id = reservation["request_id"]
        stage = reservation["stage"]
        estimated_prompt = reservation["estimated_prompt"]
        requested_completion = reservation["requested_completion"]
        pessimistic_total = reservation["pessimistic_total"]

        # Determine actual values
        if actual_prompt is not None and actual_completion is not None and actual_total is not None:
            usage_missing = False
            final_prompt = actual_prompt
            final_completion = actual_completion
            final_total = actual_total
        else:
            # Provider usage missing - keep conservative reservation
            usage_missing = True
            final_prompt = estimated_prompt
            final_completion = requested_completion
            final_total = pessimistic_total

        timestamp = datetime.now(timezone.utc).isoformat()

        # Build audit record (no secrets, no prompt content)
        audit = {
            "request_id": request_id,
            "event_id": event_id,
            "timestamp": timestamp,
            "stage": stage,
            "request_number": 0,  # Will be set from story budget
            "model": model,
            "estimated_prompt_tokens": estimated_prompt,
            "requested_max_completion_tokens": requested_completion,
            "actual_prompt_tokens": final_prompt,
            "actual_completion_tokens": final_completion,
            "actual_total_tokens": final_total,
            "usage_missing": usage_missing,
            "success": success,
            "http_status": http_status,
            "blocked": blocked,
            "block_reason": block_reason,
        }

        # Lock and reconcile
        event_lock = self.store._lock_for_event(event_id)
        with KimiBudgetStore._global_lock:
            with event_lock:
                story = self.store.load_story_budget(event_id)
                if story is None:
                    # Should never happen, but handle gracefully
                    return {"ok": False, "error": "story_budget_missing"}

                global_circuit = self.store.load_global_circuit()

                # Set request number for audit
                audit["request_number"] = story.request_count

                # Save audit BEFORE adjusting totals
                self.store.save_request_audit(event_id, request_id, audit)

                # Reconcile: subtract pessimistic, add actual
                story.total_tokens = story.total_tokens - pessimistic_total + final_total
                story.prompt_tokens += final_prompt
                story.completion_tokens += final_completion

                global_circuit.total_tokens = global_circuit.total_tokens - pessimistic_total + final_total

                # Update largest request
                current_largest = story.largest_request.get("total_tokens", 0)
                if final_total > current_largest:
                    story.largest_request = {
                        "stage": stage,
                        "prompt_tokens": final_prompt,
                        "completion_tokens": final_completion,
                        "total_tokens": final_total,
                        "request_id": request_id,
                    }

                story.update_status()
                self.store.save_story_budget(story)
                self.store.save_global_circuit(global_circuit)

        return {
            "ok": True,
            "request_id": request_id,
            "actual_prompt": final_prompt,
            "actual_completion": final_completion,
            "actual_total": final_total,
            "usage_missing": usage_missing,
        }

    def build_story_usage_summary(self, event_id: str) -> dict[str, Any]:
        """Build final usage summary for story."""
        story = self.store.load_story_budget(event_id)
        if story is None:
            return {"error": "story_not_found", "event_id": event_id}

        pct = story.percent_used()
        return {
            "event_id": event_id,
            "requests": story.request_count,
            "prompt_tokens": story.prompt_tokens,
            "completion_tokens": story.completion_tokens,
            "total_tokens": story.total_tokens,
            "hard_limit": story.hard_limit,
            "percent_used": f"{pct}%",
            "status": story.status,
            "largest_request": story.largest_request,
            "legacy_unknown": story.legacy_unknown,
        }


# Singleton for import convenience
_default_store: KimiBudgetStore | None = None
_default_manager: KimiBudgetManager | None = None


def get_budget_store() -> KimiBudgetStore:
    global _default_store
    if _default_store is None:
        _default_store = KimiBudgetStore()
    return _default_store


def get_budget_manager() -> KimiBudgetManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = KimiBudgetManager(get_budget_store())
    return _default_manager


def reset_for_testing() -> None:
    """Reset singletons for test isolation."""
    global _default_store, _default_manager
    _default_store = None
    _default_manager = None
