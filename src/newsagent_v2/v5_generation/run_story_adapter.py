"""RUN STORY Adapter.

Connects V5 NewsEvent to the existing V4 generation pipeline.

NewsEvent → RunStoryAdapter → Research → FactBank → Writer → Article Freeze V1
                                                    → Image Freeze V1
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.article.writer.v4.compile import compile_v4_article
from newsagent_v2.article.writer.v4.writer import (
    V4NaturalProseWriter,
    build_v4_writer,
)
from newsagent_v2.discovery.event_clusterer import NewsEvent
from newsagent_v2.image.vertex_make_image import build_vertex_make_image_fn
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig

from .version_store import VersionStore


@dataclass
class GenerationJob:
    """Generation job with idempotency."""
    job_id: str
    event_id: str
    event: dict[str, Any]
    state: str = "INIT"  # INIT, SELECTED, RESEARCHING, GENERATING, QA, REVIEW, FAILED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    article_version: str | None = None
    image_version: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class RunStoryAdapter:
    """Adapter connecting V5 NewsEvent to existing V4 generation pipeline.

    Features:
    - Idempotent job creation (duplicate RUN STORY = return existing job)
    - State machine transitions
    - Article V1 + Image V1 freezing
    - Version tracking with SHA-256
    """

    # Active jobs: event_id -> GenerationJob
    _active_jobs: dict[str, GenerationJob] = {}
    _lock = threading.RLock()

    def __init__(
        self,
        version_store: VersionStore | None,
        approval_store: ApprovalStore | None,
        environ: dict[str, str] | None,
    ):
        self.version_store = version_store
        self.approval_store = approval_store
        self.environ = environ or {}

    def _generate_job_id(self, event_id: str) -> str:
        """Generate unique job ID."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{event_id}-{ts}"

    def _event_to_story(self, event: NewsEvent) -> dict[str, Any]:
        """Convert NewsEvent to V4 story format.

        V4 research/compile expect evidence under ``article_input.evidence``
        (see ``research_event`` / ``enrich_story``). Top-level ``evidence`` is
        retained for adapters that still read it directly.
        """
        evidence = [
            {
                "source": r.source,
                "source_id": r.source_id,
                "source_authority": r.source_authority,
                "url": r.url,
                "title": r.headline,
                "published": r.published_at,
                "summary": r.description,
            }
            for r in event.reports
        ]

        article_input = {
            "event_id": event.event_id,
            "representative_title": event.canonical_title,
            "topic": event.topic,
            "entities": list(event.entities),
            "evidence": evidence,
        }

        return {
            "event_id": event.event_id,
            "representative_title": event.canonical_title,
            "topic": event.topic,
            "entities": list(event.entities),
            "evidence": evidence,
            "article_input": article_input,
            "source_count": event.source_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "developments": [d.to_dict() for d in event.developments],
        }

    def _create_job(self, event: NewsEvent) -> GenerationJob:
        """Create a new generation job."""
        job = GenerationJob(
            job_id=self._generate_job_id(event.event_id),
            event_id=event.event_id,
            event=self._event_to_story(event),
            state="SELECTED",
        )

        with self._lock:
            self._active_jobs[event.event_id] = job

        # Persist job state
        if self.version_store:
            self.version_store.save_generation_job(job)

        return job

    def _get_or_create_job(self, event: NewsEvent) -> tuple[bool, GenerationJob]:
        """Get existing job or create new."""
        with self._lock:
            # Check for active job
            if event.event_id in self._active_jobs:
                job = self._active_jobs[event.event_id]
                # Check if job is in terminal state
                if job.state not in {"PUBLISHED", "REJECTED", "FAILED"}:
                    return False, job  # Existing job returned

            # Create new job
            return True, self._create_job(event)

    def _is_job_duplicate(self, event_id: str) -> bool:
        """Check if a non-terminal job exists."""
        with self._lock:
            if event_id not in self._active_jobs:
                return False
            job = self._active_jobs[event_id]
            return job.state not in {"PUBLISHED", "REJECTED", "FAILED"}

    def run_story(
        self,
        event: NewsEvent,
        force_retry: bool = False,
    ) -> dict[str, Any]:
        """Run generation for an event.

        Idempotent: returns existing job if already running.
        """
        # Check for duplicate
        if not force_retry and self._is_job_duplicate(event.event_id):
            with self._lock:
                job = self._active_jobs[event.event_id]
            return {
                "ok": True,
                "new": False,
                "job_id": job.job_id,
                "state": job.state,
                "message": "Generation already in progress",
            }

        # Create or get job
        is_new, job = self._get_or_create_job(event)

        # Transition: SELECTED → RESEARCHING
        job.state = "RESEARCHING"
        self._update_job(job)

        try:
            # Run generation
            result = self._run_generation(event, job)

            if result.get("ok"):
                job.state = "REVIEW"
                job.article_version = result.get("article_version")
                job.image_version = result.get("image_version")
            else:
                job.state = "FAILED"
                job.error = result.get("error", "Unknown error")

            self._update_job(job)

            return {
                "ok": result.get("ok", False),
                "new": is_new,
                "job_id": job.job_id,
                "state": job.state,
                "article_version": job.article_version,
                "image_version": job.image_version,
                "error": job.error,
                "result": result,
            }

        except Exception as e:
            job.state = "FAILED"
            job.error = f"{type(e).__name__}: {str(e)[:200]}"
            self._update_job(job)

            return {
                "ok": False,
                "new": is_new,
                "job_id": job.job_id,
                "state": "FAILED",
                "error": job.error,
            }

    def _update_job(self, job: GenerationJob) -> None:
        """Update job state."""
        job.updated_at = datetime.now(timezone.utc).isoformat()
        if self.version_store:
            self.version_store.save_generation_job(job)

    def _run_generation(
        self,
        event: NewsEvent,
        job: GenerationJob,
    ) -> dict[str, Any]:
        """Execute generation pipeline.

        Uses existing V4 components:
        - research_event (multi-source research)
        - build_fact_bank (fact extraction)
        - compile_v4_article (writer + QA)
        - vertex_make_image (image generation)
        """
        from newsagent_v2.article.writer.v4.event_research import research_event
        from newsagent_v2.article.writer.v4.factbank import build_fact_bank

        # Transition: RESEARCHING → GENERATING
        job.state = "GENERATING"
        self._update_job(job)

        env = self.environ

        # Build story format
        story = self._event_to_story(event)

        # Initialize writer
        writer = build_v4_writer(
            environ=env,
            enable_failover=False,
            max_calls=24,
        )

        # Run multi-source research
        research = research_event(story)
        pack = research.pack

        # Build fact bank
        bank = build_fact_bank(event_id=event.event_id, pack=pack)

        # Assess evidence capacity
        from newsagent_v2.article.writer.v4.evidence_depth import assess_evidence_capacity
        depth = assess_evidence_capacity(bank, research=research)

        # Compile V4 article (includes writer + QA + grounding)
        attempts_root = self._attempts_root(job.job_id)
        compiled = compile_v4_article(
            story,
            writer=writer,
            attempts_root=attempts_root,
            rank=1,
            research=True,
        )

        if not compiled.ok or not compiled.article:
            return {
                "ok": False,
                "error": compiled.failure_class or "article_generation_failed",
                "notes": compiled.notes,
            }

        # Freeze Article V1
        article_v1 = compiled.article
        article_content = article_v1.get("article_body", "")
        article_hash = self._calculate_sha256(article_content)

        # Store Article V1
        if self.version_store:
            self.version_store.save_article(
                event_id=event.event_id,
                version="v1",
                article=article_v1,
                article_hash=article_hash,
                qa_result=compiled.qa or {},
                metadata={
                    "word_count": compiled.final_words,
                    "grounding_supported": compiled.supported,
                    "grounding_ambiguous": compiled.ambiguous,
                    "grounding_unsupported": compiled.unsupported,
                },
            )

        # Generate Image V1
        image_result = self._generate_image(
            event_id=event.event_id,
            article=article_v1,
            article_hash=article_hash,
            job_id=job.job_id,
        )

        if not image_result.get("success"):
            # Image failed - still return article
            return {
                "ok": True,
                "article_version": "v1",
                "image_version": None,
                "image_failed": True,
                "article_hash": article_hash,
            }

        image_path = image_result.get("final_path")
        image_hash = image_result.get("branded_image_hash") or self._calculate_sha256("")

        # Store Image V1
        if self.version_store:
            self.version_store.save_image(
                event_id=event.event_id,
                version="v1",
                image_path=image_path,
                image_hash=image_hash,
                metadata={
                    "raw_image_hash": image_result.get("raw_image_hash"),
                    "provider": image_result.get("provider"),
                    "model": image_result.get("model"),
                },
            )

        return {
            "ok": True,
            "article_version": "v1",
            "image_version": "v1",
            "article_hash": article_hash,
            "image_hash": image_hash,
        }

    def _generate_image(
        self,
        event_id: str,
        article: dict[str, Any],
        article_hash: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Generate image using existing infrastructure."""
        image_fn = build_vertex_make_image_fn(self.environ, batch_id=job_id)

        job = {
            "event_id": event_id,
            "article": article,
            "canonical_body_hash": article_hash,
            "article_hash": article_hash,
        }

        return image_fn(job)

    def _attempts_root(self, job_id: str) -> Path:
        """Get attempts root path."""
        return Path(__file__).resolve().parents[3] / "output" / "v5_attempts" / job_id

    def _calculate_sha256(self, content: str) -> str:
        """Calculate SHA-256 hash of content."""
        import hashlib

        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def get_job(self, event_id: str) -> GenerationJob | None:
        """Get active job for an event."""
        with self._lock:
            return self._active_jobs.get(event_id)

    def get_job_state(self, event_id: str) -> str | None:
        """Get current job state."""
        job = self.get_job(event_id)
        return job.state if job else None
