"""Async generation worker for V5.

Runs generation pipeline outside the polling loop.
Reports progress via Telegram.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from newsagent_v2.telegram.client import TelegramTestClient
    from newsagent_v2.telegram.config import TelegramConfig
    from newsagent_v2.discovery.event_clusterer import NewsEvent

from newsagent_v2.v5_generation.persistent_store import (
    PersistentV5Store,
    GenerationJob,
)
from newsagent_v2.v5_generation.run_story_adapter import RunStoryAdapter
from newsagent_v2.v5_generation.cost_ledger import CostLedger


class GenerationWorker:
    """Async worker for story generation.
    
    Runs in separate thread, reports progress via Telegram.
    """
    
    def __init__(
        self,
        client: TelegramTestClient,
        config: TelegramConfig,
        persistent_store: PersistentV5Store,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.persistent_store = persistent_store
        self.environ = environ or {}
        # CostLedger expects persist_path, not environ
        ledger_path = Path("./data/v5_state/cost_ledger.json") if environ else None
        self.ledger = CostLedger(persist_path=ledger_path)
    
    def _update_progress(
        self,
        job: GenerationJob,
        stage: str,
        emoji: str = "🟡",
    ) -> None:
        """Send/update progress message to Telegram."""
        if not job.progress_message_id:
            # Send initial progress message
            result = self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=f"{emoji} {stage}\n\nStory: {job.event_id[:20]}...",
                parse_mode="HTML",
            )
            if result.get("ok"):
                job.progress_message_id = result.get("message_id")
                self.persistent_store.update_job_state(
                    job.job_id,
                    job.state,
                    progress_message_id=job.progress_message_id,
                )
        else:
            # Edit existing message
            self.client.edit_message_text(
                chat_id=self.config.test_chat_id,
                message_id=job.progress_message_id,
                text=f"{emoji} {stage}\n\nStory: {job.event_id[:20]}...",
                parse_mode="HTML",
            )
    
    def _report_failure(
        self,
        job: GenerationJob,
        stage: str,
        error: str,
    ) -> None:
        """Report failure to Telegram."""
        safe_error = str(error)[:100]  # Truncate
        text = f"❌ Generation failed\nStage: {stage}\nReason: {safe_error}"
        
        if job.progress_message_id:
            self.client.edit_message_text(
                chat_id=self.config.test_chat_id,
                message_id=job.progress_message_id,
                text=text,
                parse_mode="HTML",
            )
        else:
            self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=text,
                parse_mode="HTML",
            )
    
    def run_generation(self, event: NewsEvent, job: GenerationJob) -> dict[str, Any]:
        """Run full generation pipeline.
        
        This runs in a separate thread and updates Telegram progress.
        """
        try:
            # Reserve job
            self.persistent_store.update_job_state(job.job_id, "RESERVED")
            
            # Start cost entry (CostLedger.start_entry — not record_start)
            writer_entry = self.ledger.start_entry(
                provider="groq",
                model="qwen/qwen3.8-27b",
                operation="write",
            )
            
            # Update progress: Starting
            self._update_progress(job, "Story selected", "🟡")
            
            # Create adapter — honor V5_VERSION_STORE_ROOT when set (tests/ops)
            from newsagent_v2.v5_generation.version_store import VersionStore
            from newsagent_v2.approval.store import ApprovalStore

            version_root = self.environ.get("V5_VERSION_STORE_ROOT")
            version_store = VersionStore(
                root=Path(version_root) if version_root else None
            )
            approval_store = ApprovalStore()

            adapter = RunStoryAdapter(
                version_store=version_store,
                approval_store=approval_store,
                environ=self.environ,
            )
            
            # Run generation
            self._update_progress(job, "Researching sources...", "🔎")
            self.persistent_store.update_job_state(job.job_id, "REQUESTING")
            
            result = adapter.run_story(event)
            
            # Update final state
            if result.get("ok"):
                self.persistent_store.update_job_state(
                    job.job_id,
                    "SUCCEEDED",
                    article_version=result.get("article_version"),
                    article_hash=result.get("article_hash"),
                    image_version=result.get("image_version"),
                    image_hash=result.get("image_hash"),
                )
                
                # Finalize cost (tokens unknown at this layer → UNKNOWN)
                self.ledger.finalize_entry(
                    writer_entry.entry_id,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_inr=None,
                )
                
                # Send completion
                self._update_progress(
                    job,
                    f"Ready for review\nArticle: {result.get('article_version', 'V1')}\nImage: {result.get('image_version', 'V1')}",
                    "✅",
                )
                
                # Send review package
                self._send_review_package(event, result, job)
                
            else:
                error = result.get("error", "Unknown error")
                self.persistent_store.update_job_state(
                    job.job_id,
                    "FAILED_FINAL",
                    error=error,
                )
                self._report_failure(job, "generation", error)
            
            return result
            
        except Exception as e:
            self.persistent_store.update_job_state(
                job.job_id,
                "FAILED_FINAL",
                error=str(e)[:200],
            )
            self._report_failure(job, "worker_exception", str(e))
            return {"ok": False, "error": str(e)}
    
    def _send_review_package(
        self,
        event: NewsEvent,
        result: dict[str, Any],
        job: GenerationJob,
    ) -> None:
        """Send review package after successful generation."""
        # This would integrate with the existing review system
        # For now, send a simple confirmation with basic review controls
        
        article_version = result.get("article_version", "V1")
        
        # Build review keyboard
        from newsagent_v2.telegram.v5_cards import make_callback_data
        
        review_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "RATE ARTICLE", "callback_data": f"rate_article:{job.event_id}:{article_version}"},
                    {"text": "RATE IMAGE", "callback_data": f"rate_image:{job.event_id}:{article_version}"},
                ],
                [
                    {"text": "ARTICLE FEEDBACK", "callback_data": f"feedback_article:{job.event_id}:{article_version}"},
                    {"text": "IMAGE FEEDBACK", "callback_data": f"feedback_image:{job.event_id}:{article_version}"},
                ],
                [
                    {"text": "REVISE", "callback_data": f"revise:{job.event_id}:{article_version}"},
                    {"text": "PUBLISH", "callback_data": f"publish:{job.event_id}:{article_version}"},
                ],
                [
                    {"text": "REJECT", "callback_data": f"reject:{job.event_id}:{article_version}"},
                ],
            ]
        }
        
        self.client.send_message(
            chat_id=self.config.test_chat_id,
            text=f"✅ <b>Story Ready for Review</b>\n\n{event.canonical_title}\n\nArticle: {article_version}\nUse the buttons below to review.",
            parse_mode="HTML",
            reply_markup=review_keyboard,
        )

    # Active jobs tracking for max_active = 1 enforcement
    _active_jobs: dict[str, GenerationJob] = {}
    _lock: Any = None

    def _get_lock(self):
        """Get or create class-level lock."""
        if GenerationWorker._lock is None:
            import threading
            GenerationWorker._lock = threading.Lock()
        return GenerationWorker._lock

    def request_generation(
        self,
        event: NewsEvent,
        discovery_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Request generation for an event.

        Idempotent: returns existing job if already running.
        Returns job_id and state for tracking.
        """
        with self._get_lock():
            # Check if already have an active job for this event
            for job_id, job in GenerationWorker._active_jobs.items():
                if job.event_id == event.event_id:
                    if job.state in ("REQUESTING", "SUCCEEDED", "FAILED_RETRYABLE", "FAILED_FINAL"):
                        return {
                            "ok": True,
                            "job_id": job_id,
                            "state": job.state,
                            "new": False,
                            "message": f"Generation already {job.state.lower()}",
                        }

            # Create new job
            import threading
            from datetime import datetime, timezone
            from uuid import uuid4

            now = datetime.now(timezone.utc).isoformat()
            job_id = f"job-{event.event_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
            job = GenerationJob(
                job_id=job_id,
                event_id=event.event_id,
                discovery_run_id=discovery_run_id or "",
                state="RESERVED",
                created_at=now,
                updated_at=now,
            )

            # Save to persistent store
            self.persistent_store.save_job(job)

            # Add to active jobs
            GenerationWorker._active_jobs[job_id] = job

            # Start in background thread
            thread = threading.Thread(
                target=self.run_generation,
                args=(event, job),
                daemon=True,
            )
            thread.start()

            return {
                "ok": True,
                "job_id": job_id,
                "state": "RESERVED",
                "new": True,
                "message": "Generation started",
            }

    def get_active_job_count(self) -> int:
        """Get count of currently active generation jobs.

        For MAX_ACTIVE_GENERATION = 1 enforcement.
        """
        with self._get_lock():
            # Count jobs that are not terminal states
            active_states = {"NOT_REQUESTED", "RESERVED", "REQUESTING"}
            return sum(
                1 for job in GenerationWorker._active_jobs.values()
                if job.state in active_states
            )

    def get_job_for_event(self, event_id: str) -> GenerationJob | None:
        """Get existing job for an event (for idempotency).

        Returns existing job if found, None otherwise.
        """
        with self._get_lock():
            for job in GenerationWorker._active_jobs.values():
                if job.event_id == event_id:
                    return job
            return None
