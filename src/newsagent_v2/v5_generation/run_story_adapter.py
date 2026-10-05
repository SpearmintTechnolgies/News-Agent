"""RUN STORY adapter: one news event -> V6 story pipeline -> frozen article and image versions.

NewsEvent -> story6 (research -> fact bank -> gate -> write + QA) -> Article vN
                                                                  -> Image vN (Vertex hero + logo)

A ``Revision`` (REVISE) rewrites the article with the editor's feedback and/or regenerates the
image with image feedback; whatever is not revised is reused from the current version.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.discovery.event_clusterer import NewsEvent
from newsagent_v2.image.vertex_make_image import build_vertex_make_image_fn

from .version_store import VersionStore

USER_FAILURE_MESSAGE = "Article could not be completed. Please retry."


def next_version(version: str | None) -> str:
    if not version:
        return "v1"
    try:
        return f"v{int(str(version).lstrip('vV')) + 1}"
    except ValueError:
        return f"{version}-rev"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Revision:
    """What REVISE should redo, relative to the current versions."""

    article_feedback: str = ""
    image_feedback: str = ""
    article_version: str | None = None  # current (base) versions
    image_version: str | None = None


@dataclass
class GenerationJob:
    """Adapter-side job record (persisted next to the article versions)."""

    job_id: str
    event_id: str
    event: dict[str, Any]
    state: str = "SELECTED"  # SELECTED, GENERATING, REVIEW, FAILED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    article_version: str | None = None
    image_version: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "event_id": self.event_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "article_version": self.article_version,
            "image_version": self.image_version,
            "error": self.error,
            "metadata": self.metadata,
        }


class RunStoryAdapter:
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

    @staticmethod
    def _event_record(event: NewsEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "representative_title": event.canonical_title,
            "topic": event.topic,
            "entities": list(event.entities),
            "reports": [{"source": r.source, "url": r.url, "title": r.headline} for r in event.reports],
        }

    def _start_job(self, event: NewsEvent) -> GenerationJob:
        job = GenerationJob(
            job_id=f"{event.event_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
            event_id=event.event_id,
            event=self._event_record(event),
        )
        with self._lock:
            self._active_jobs[event.event_id] = job
        self._update_job(job)
        return job

    def _busy(self, event_id: str) -> GenerationJob | None:
        with self._lock:
            job = self._active_jobs.get(event_id)
        return job if job and job.state in {"SELECTED", "GENERATING"} else None

    def run_story(
        self,
        event: NewsEvent,
        force_retry: bool = False,
        revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Generate (or revise) the event's article and image. Idempotent while a job is running."""
        running = None if force_retry else self._busy(event.event_id)
        if running:
            return {"ok": True, "new": False, "job_id": running.job_id, "state": running.state,
                    "message": "Generation already in progress"}
        job = self._start_job(event)
        try:
            result = self._run_generation(event, job, revision)
        except Exception as exc:  # noqa: BLE001 - surfaced to the editor, never crashes the worker
            job.state, job.error = "FAILED", USER_FAILURE_MESSAGE
            job.metadata["failure_details"] = {
                "failure_class": "WORKER_EXCEPTION",
                "exception": f"{type(exc).__name__}: {str(exc)[:300]}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if self.version_store:
                self.version_store.save_generation_diagnostics(event.event_id, job.metadata["failure_details"])
            self._update_job(job)
            return {"ok": False, "new": True, "job_id": job.job_id, "state": "FAILED", "error": job.error,
                    "internal_error": job.metadata["failure_details"]["exception"]}

        job.article_version = result.get("article_version")
        job.image_version = result.get("image_version")
        if result.get("ok"):
            job.state = "REVIEW"
        else:
            job.state = "FAILED"
            job.error = result.get("user_message") or USER_FAILURE_MESSAGE
            job.metadata["failure_details"] = {
                "failure_class": result.get("failure_class"),
                "error": result.get("error"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        self._update_job(job)
        return {
            **result,
            "ok": bool(result.get("ok")),
            "new": True,
            "job_id": job.job_id,
            "state": job.state,
            "error": job.error,
            "result": result,
        }

    def _update_job(self, job: GenerationJob) -> None:
        job.updated_at = datetime.now(timezone.utc).isoformat()
        if self.version_store:
            self.version_store.save_generation_job(job)

    def _text_usage(self, budget: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": "bedrock-mantle",
            "model": self.environ.get("NEWSAGENT_V2_V4_WRITER_MODEL", ""),
            "prompt_tokens": int(budget.get("prompt_tokens") or 0),
            "completion_tokens": int(budget.get("completion_tokens") or 0),
            "calls": int(budget.get("calls") or 0),
            "log": list(budget.get("log") or []),
        }

    def _write_article(self, event: NewsEvent, job: GenerationJob, version: str, feedback: str) -> dict[str, Any]:
        from newsagent_v2.story6 import run_story as run_v6_story

        job.state = "GENERATING"
        self._update_job(job)
        story = run_v6_story(event, self.environ, feedback=feedback)
        diagnostics = story.diagnostics()
        if self.version_store:
            self.version_store.save_generation_diagnostics(event.event_id, diagnostics)
        text_usage = self._text_usage(story.budget)
        from newsagent_v2.v5_generation.article_spend import record_text_spend

        record_text_spend(event.event_id, text_usage)
        if not story.ok or not story.article:
            return {"ok": False, "error": story.reason, "user_message": story.reason,
                    "failure_class": story.outcome.upper(), "kimi_usage": text_usage, "diagnostics": diagnostics}
        article = story.article
        article_hash = sha256(article["article_body"])
        if self.version_store:
            self.version_store.save_article(
                event_id=event.event_id,
                version=version,
                article=article,
                article_hash=article_hash,
                qa_result=story.qa,
                metadata={
                    "pipeline": "v6",
                    "word_count": article["body_words"],
                    "total_words": article["total_words"],
                    "gate": story.gate,
                    "research": story.research,
                    "text_usage": text_usage,
                    **({"revision_feedback": feedback} if feedback else {}),
                },
            )
        return {"ok": True, "article": article, "article_hash": article_hash, "kimi_usage": text_usage}

    def _make_image(self, event_id: str, article: dict[str, Any], article_hash: str, version: str,
                    batch_id: str, feedback: str = "") -> dict[str, Any]:
        job = {"event_id": event_id, "article": article, "canonical_body_hash": article_hash,
               "article_hash": article_hash}
        if feedback:
            job.update(image_feedback=feedback, target_revision=version)
        result = build_vertex_make_image_fn(self.environ, batch_id=batch_id)(job)
        if result.get("success") and self.version_store:
            self.version_store.save_image(
                event_id=event_id,
                version=version,
                image_path=result.get("final_path"),
                image_hash=result.get("branded_image_hash") or sha256(""),
                metadata={
                    "raw_image_hash": result.get("raw_image_hash"),
                    "provider": result.get("provider"),
                    "model": result.get("model"),
                    **({"revision_reason": feedback} if feedback else {}),
                },
            )
        return result

    def _run_generation(self, event: NewsEvent, job: GenerationJob, revision: Revision | None) -> dict[str, Any]:
        rewrite = revision is None or bool(revision.article_feedback.strip())
        article_version = "v1" if revision is None else (
            next_version(revision.article_version) if rewrite else revision.article_version
        )
        text_usage: dict[str, Any] = {}
        if rewrite:
            written = self._write_article(event, job, article_version, revision.article_feedback if revision else "")
            if not written.get("ok"):
                return written
            article, article_hash, text_usage = written["article"], written["article_hash"], written["kimi_usage"]
        else:
            record = (self.version_store.get_article(event.event_id, article_version) if self.version_store else None) or {}
            article = record.get("article") or {}
            article_hash = record.get("article_hash") or sha256(str(article.get("article_body") or ""))
            if not article:
                return {"ok": False, "error": "No article to revise.", "user_message": "No article to revise.",
                        "failure_class": "NO_ARTICLE"}

        new_image = revision is None or bool(revision.image_feedback.strip()) or not revision.image_version
        image_version = (
            ("v1" if revision is None else next_version(revision.image_version)) if new_image else revision.image_version
        )
        image_usage: dict[str, Any] = {}
        image_path = None
        if new_image:
            image = self._make_image(event.event_id, article, article_hash, image_version, job.job_id,
                                     feedback=revision.image_feedback if revision else "")
            if not image.get("success"):
                code = str(image.get("image_failure_code") or image.get("reason") or "image_generation_failed")
                message = f"Article is ready but the hero image failed ({code}). Retry to regenerate."
                return {"ok": False, "error": message, "user_message": message,
                        "failure_class": "IMAGE_GENERATION_FAILED", "article_version": article_version,
                        "image_version": None, "image_failed": True, "article_hash": article_hash,
                        "kimi_usage": text_usage, "image_usage": image, "image_failure_code": code}
            image_path = image.get("final_path")
            image_usage = {
                "provider": image.get("provider"),
                "model": image.get("model"),
                "requests": image.get("image_request_count") or 1,
                "provider_reported_cost_usd": image.get("provider_reported_cost_usd"),
            }
        elif self.version_store:
            path = self.version_store.get_image_path(event.event_id, image_version)
            image_path = str(path) if path else None

        return {
            "ok": True,
            "article_version": article_version,
            "image_version": image_version,
            "article_hash": article_hash,
            "image_hash": self.version_store.get_image_hash(event.event_id, image_version) if self.version_store else None,
            "image_path": image_path,
            "kimi_usage": text_usage,
            "text_usage": text_usage,
            "image_usage": image_usage,
            "qa_flags": article.get("qa_flags") or [],
            "event_id": event.event_id,
            "revision": bool(revision),
        }

    def get_job(self, event_id: str) -> GenerationJob | None:
        with self._lock:
            return self._active_jobs.get(event_id)

    def get_job_state(self, event_id: str) -> str | None:
        job = self.get_job(event_id)
        return job.state if job else None
