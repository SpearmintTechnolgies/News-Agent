"""Persistent state store for V5 Telegram and generation.

Survives bot restarts. Replaces in-memory V5TelegramStore.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_ROOT = Path("./data/v5_state")


@dataclass
class DiscoveryRun:
    """A single /make discovery run."""
    run_id: str  # make_run_id from V5BotState
    created_at: str
    event_ids: list[str]
    total_events: int
    chat_id: str
    
    # Session state
    followed_ids: set[str] = field(default_factory=set)
    ignored_ids: set[str] = field(default_factory=set)
    selected_ids: set[str] = field(default_factory=set)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "event_ids": self.event_ids,
            "total_events": self.total_events,
            "chat_id": self.chat_id,
            "followed_ids": list(self.followed_ids),
            "ignored_ids": list(self.ignored_ids),
            "selected_ids": list(self.selected_ids),
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscoveryRun:
        return cls(
            run_id=data["run_id"],
            created_at=data["created_at"],
            event_ids=data["event_ids"],
            total_events=data.get("total_events", len(data.get("event_ids", []))),
            chat_id=data.get("chat_id", ""),
            followed_ids=set(data.get("followed_ids", [])),
            ignored_ids=set(data.get("ignored_ids", [])),
            selected_ids=set(data.get("selected_ids", [])),
        )


@dataclass
class GenerationJob:
    """Persistent generation job state."""
    job_id: str
    event_id: str
    discovery_run_id: str
    state: str  # NOT_REQUESTED, RESERVED, REQUESTING, UNKNOWN_AFTER_INTERRUPTION, SUCCEEDED, FAILED_RETRYABLE, FAILED_FINAL
    created_at: str
    updated_at: str
    
    # Article
    article_version: str | None = None
    article_hash: str | None = None
    
    # Image
    image_version: str | None = None
    image_hash: str | None = None
    
    # Status
    error: str | None = None
    progress_message_id: int | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "event_id": self.event_id,
            "discovery_run_id": self.discovery_run_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "article_version": self.article_version,
            "article_hash": self.article_hash,
            "image_version": self.image_version,
            "image_hash": self.image_hash,
            "error": self.error,
            "progress_message_id": self.progress_message_id,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GenerationJob:
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


class PersistentV5Store:
    """Thread-safe persistent store for V5 state.
    
    Replaces in-memory V5TelegramStore.
    """
    
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DATA_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        
        # In-memory cache (loaded from disk)
        self._runs: dict[str, DiscoveryRun] = {}
        self._jobs: dict[str, GenerationJob] = {}
        
        self._load_all()
    
    def _run_path(self, run_id: str) -> Path:
        return self.root / f"run_{run_id}.json"
    
    def _job_path(self, job_id: str) -> Path:
        return self.root / f"job_{job_id}.json"
    
    def _load_all(self) -> None:
        """Load all persisted state."""
        for f in self.root.glob("run_*.json"):
            try:
                data = json.loads(f.read_text())
                run = DiscoveryRun.from_dict(data)
                self._runs[run.run_id] = run
            except Exception:
                pass
        
        for f in self.root.glob("job_*.json"):
            try:
                data = json.loads(f.read_text())
                job = GenerationJob.from_dict(data)
                self._jobs[job.job_id] = job
            except Exception:
                pass
    
    # ===== Discovery Runs =====
    
    def save_discovery_run(self, run: DiscoveryRun) -> None:
        """Persist a discovery run."""
        with self._lock:
            self._runs[run.run_id] = run
            self._run_path(run.run_id).write_text(
                json.dumps(run.to_dict(), indent=2)
            )
    
    def get_discovery_run(self, run_id: str) -> DiscoveryRun | None:
        """Get discovery run by ID."""
        with self._lock:
            return self._runs.get(run_id)
    
    def get_latest_run(self, chat_id: str) -> DiscoveryRun | None:
        """Get most recent discovery run for chat."""
        with self._lock:
            runs = [r for r in self._runs.values() if r.chat_id == chat_id]
            if not runs:
                return None
            return max(runs, key=lambda r: r.created_at)
    
    def mark_followed(self, run_id: str, event_id: str) -> bool:
        """Mark event as followed."""
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return False
            run.followed_ids.add(event_id)
            if event_id in run.ignored_ids:
                run.ignored_ids.remove(event_id)
            self.save_discovery_run(run)
            return True
    
    def mark_ignored(self, run_id: str, event_id: str) -> bool:
        """Mark event as ignored."""
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return False
            run.ignored_ids.add(event_id)
            if event_id in run.followed_ids:
                run.followed_ids.remove(event_id)
            self.save_discovery_run(run)
            return True
    
    def mark_selected(self, run_id: str, event_id: str) -> bool:
        """Mark event as selected."""
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return False
            is_new = event_id not in run.selected_ids
            run.selected_ids.add(event_id)
            self.save_discovery_run(run)
            return is_new
    
    def is_followed(self, run_id: str, event_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            return run is not None and event_id in run.followed_ids
    
    def is_ignored(self, run_id: str, event_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            return run is not None and event_id in run.ignored_ids
    
    def is_selected(self, run_id: str, event_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            return run is not None and event_id in run.selected_ids
    
    def get_events_slice(self, run_id: str, offset: int = 0, count: int = 5) -> list[str]:
        """Get event IDs for pagination."""
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return []
            return run.event_ids[offset:offset + count]
    
    # ===== Feedback Awaiting State =====
    
    def set_awaiting_feedback(
        self,
        chat_id: str,
        event_id: str,
        artifact_type: str,
        version: str,
    ) -> None:
        """Set awaiting feedback state for a chat."""
        with self._lock:
            path = self.root / f"awaiting_{chat_id}.json"
            record = {
                "chat_id": chat_id,
                "event_id": event_id,
                "artifact_type": artifact_type,
                "version": version,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            path.write_text(json.dumps(record, indent=2))
    
    def get_awaiting_feedback(self, chat_id: str) -> dict[str, Any] | None:
        """Get awaiting feedback state for a chat."""
        with self._lock:
            path = self.root / f"awaiting_{chat_id}.json"
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, TypeError):
                return None
    
    def clear_awaiting_feedback(self, chat_id: str) -> None:
        """Clear awaiting feedback state for a chat."""
        with self._lock:
            path = self.root / f"awaiting_{chat_id}.json"
            if path.exists():
                path.unlink()
    
    # ===== Generation Jobs =====
    
    def save_job(self, job: GenerationJob) -> None:
        """Persist generation job."""
        with self._lock:
            self._jobs[job.job_id] = job
            self._job_path(job.job_id).write_text(
                json.dumps(job.to_dict(), indent=2)
            )
    
    def get_job(self, job_id: str) -> GenerationJob | None:
        with self._lock:
            return self._jobs.get(job_id)
    
    def get_job_by_event(self, event_id: str) -> GenerationJob | None:
        """Get active job for event."""
        with self._lock:
            for job in self._jobs.values():
                if job.event_id == event_id and job.state not in {
                    "SUCCEEDED", "FAILED_FINAL", "PUBLISHED"
                }:
                    return job
            return None
    
    def get_active_job_count(self) -> int:
        """Count non-terminal jobs."""
        with self._lock:
            return sum(
                1 for job in self._jobs.values()
                if job.state not in {"SUCCEEDED", "FAILED_FINAL", "PUBLISHED"}
            )
    
    def has_active_generation(self) -> bool:
        """Check if any generation is active."""
        return self.get_active_job_count() > 0
    
    def update_job_state(
        self,
        job_id: str,
        state: str,
        article_version: str | None = None,
        article_hash: str | None = None,
        image_version: str | None = None,
        image_hash: str | None = None,
        error: str | None = None,
        progress_message_id: int | None = None,
    ) -> bool:
        """Update job state."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            
            job.state = state
            job.updated_at = datetime.now(timezone.utc).isoformat()
            
            if article_version is not None:
                job.article_version = article_version
            if article_hash is not None:
                job.article_hash = article_hash
            if image_version is not None:
                job.image_version = image_version
            if image_hash is not None:
                job.image_hash = image_hash
            if error is not None:
                job.error = error
            if progress_message_id is not None:
                job.progress_message_id = progress_message_id
            
            self.save_job(job)
            return True
    
    # ===== Restart Reconciliation =====
    
    def reconcile_jobs_on_startup(self) -> list[dict[str, Any]]:
        """Reconcile jobs on bot startup.
        
        - NOT_REQUESTED: safe (no action needed)
        - RESERVED: reconcilable (can resume)
        - REQUESTING → UNKNOWN_AFTER_INTERRUPTION (NO auto retry)
        - SUCCEEDED: never regenerate
        - FAILED_RETRYABLE: explicit retry only
        
        Returns list of jobs that need attention.
        """
        with self._lock:
            affected = []
            for job in self._jobs.values():
                if job.state == "REQUESTING":
                    # Interrupted during generation - mark as unknown
                    job.state = "UNKNOWN_AFTER_INTERRUPTION"
                    job.updated_at = datetime.now(timezone.utc).isoformat()
                    if job.error is None:
                        job.error = "Bot restarted during generation"
                    self.save_job(job)
                    affected.append({
                        "job_id": job.job_id,
                        "event_id": job.event_id,
                        "old_state": "REQUESTING",
                        "new_state": "UNKNOWN_AFTER_INTERRUPTION",
                        "action": "manual_review_required",
                    })
            return affected
    
    def get_jobs_needing_attention(self) -> list[GenerationJob]:
        """Get jobs in states that need manual attention."""
        with self._lock:
            return [
                job for job in self._jobs.values()
                if job.state in {"UNKNOWN_AFTER_INTERRUPTION", "FAILED_RETRYABLE"}
            ]
