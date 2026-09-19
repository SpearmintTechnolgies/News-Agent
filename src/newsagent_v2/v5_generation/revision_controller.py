"""Targeted Revision Controller.

Revisions operate on DELTAS:
- Article feedback only: regenerate ONLY article, keep image V1
- Image feedback only: regenerate ONLY image, keep article V1
- Both: regenerate both affected artifacts

Never overwrite historical versions - always create new versions.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from newsagent_v2.article.writer.v4.compile import compile_v4_article
from newsagent_v2.article.writer.v4.writer import build_v4_writer
from newsagent_v2.discovery.event_clusterer import NewsEvent

from .version_store import VersionStore


@dataclass
class RevisionRequest:
    """Revision request from user feedback."""
    event_id: str
    article_feedback: str = ""
    image_feedback: str = ""
    article_rating: int | None = None
    image_rating: int | None = None
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class RevisionResult:
    """Revision result."""
    ok: bool
    event_id: str
    article_revised: bool
    image_revised: bool
    article_version: str | None = None
    image_version: str | None = None
    article_hash: str | None = None
    image_hash: str | None = None
    article_calls: int = 0
    image_calls: int = 0
    error: str | None = None


class RevisionController:
    """Targeted revision controller with delta-only regeneration.

    Tracks revision counts and respects provider budget guards.
    """

    def __init__(
        self,
        version_store: VersionStore,
        environ: dict[str, str] | None,
    ):
        self.version_store = version_store
        self.environ = environ or {}
        self._lock = threading.RLock()

        # Revision tracking: event_id -> {article_count, image_count}
        self._revision_counts: dict[str, dict[str, int]] = {}

        # Budget configuration
        self.max_article_revisions = int(self.environ.get("V5_MAX_ARTICLE_REVISIONS", "3"))
        self.max_image_revisions = int(self.environ.get("V5_MAX_IMAGE_REVISIONS", "2"))

    def _get_current_artifact(
        self,
        event_id: str,
        artifact_type: str,
    ) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        """Get current version of artifact."""
        version = self.version_store.get_current_version(event_id, artifact_type)
        if not version:
            return None, None

        if artifact_type == "article":
            data = self.version_store.get_article(event_id, version)
        elif artifact_type == "image":
            # Image path + hash
            data = {
                "path": self.version_store.get_image_path(event_id, version),
                "hash": self.version_store.get_image_hash(event_id, version),
            }
        else:
            return None, None

        return version, data

    def _get_revision_count(self, event_id: str, artifact_type: str) -> int:
        """Get current revision count for an artifact type."""
        with self._lock:
            return self._revision_counts.get(event_id, {}).get(artifact_type, 0)

    def _increment_revision_count(self, event_id: str, artifact_type: str) -> None:
        """Increment revision count."""
        with self._lock:
            if event_id not in self._revision_counts:
                self._revision_counts[event_id] = {"article": 0, "image": 0}
            self._revision_counts[event_id][artifact_type] += 1

    def revision_allowed(
        self,
        event_id: str,
        artifact_type: str,
    ) -> tuple[bool, str]:
        """Check if revision is allowed.

        Returns:
            Tuple of (allowed, reason)
        """
        current = self._get_revision_count(event_id, artifact_type)

        if artifact_type == "article":
            if current >= self.max_article_revisions:
                return False, f"max_article_revisions_exceeded ({self.max_article_revisions})"
            return True, ""

        if artifact_type == "image":
            if current >= self.max_image_revisions:
                return False, f"max_image_revisions_exceeded ({self.max_image_revisions})"
            return True, ""

        return False, "unknown_artifact_type"

    def revise(
        self,
        event: NewsEvent,
        request: RevisionRequest,
    ) -> RevisionResult:
        """Execute targeted revision based on feedback.

        Determines what needs regeneration based on provided feedback:
        - Article feedback only → regenerate article
        - Image feedback only → regenerate image
        - Both → regenerate both
        """
        event_id = event.event_id

        # Determine what needs revision
        revise_article = bool(request.article_feedback or request.article_rating is not None)
        revise_image = bool(request.image_feedback or request.image_rating is not None)

        if not revise_article and not revise_image:
            return RevisionResult(
                ok=False,
                event_id=event_id,
                article_revised=False,
                image_revised=False,
                error="no_revision_feedback_provided",
            )

        # Track results
        result = RevisionResult(
            ok=True,
            event_id=event_id,
            article_revised=revise_article,
            image_revised=revise_image,
        )

        # Get current versions
        article_version, article_data = self._get_current_artifact(event_id, "article")
        image_version, image_data = self._get_current_artifact(event_id, "image")

        # Revisions create NEW versions
        if revise_article:
            allowed, reason = self.revision_allowed(event_id, "article")
            if not allowed:
                result.ok = False
                result.error = f"article_revision_not_allowed: {reason}"
                return result

            if not article_data:
                result.ok = False
                result.error = "no_article_to_revise"
                return result

            # Article revision
            new_version = self._calculate_next_version(article_version)
            revision_result = self._revise_article(
                event,
                article_data,
                new_version,
                request,
            )

            if revision_result.get("ok"):
                result.article_version = new_version
                result.article_hash = revision_result.get("article_hash")
                result.article_calls = revision_result.get("writer_calls", 0)
                self._increment_revision_count(event_id, "article")
            else:
                result.ok = False
                result.error = revision_result.get("error", "article_revision_failed")
                return result

        if revise_image:
            allowed, reason = self.revision_allowed(event_id, "image")
            if not allowed:
                # If article succeeded but image failed, mark as partial
                result.image_revised = False
                result.error = f"image_revision_not_allowed: {reason}"
                # Don't fail completely - article might have succeeded
                return result

            if not image_data:
                result.ok = False
                result.error = "no_image_to_revise"
                return result

            # Image revision
            new_version = self._calculate_next_version(image_version)
            revision_result = self._revise_image(
                event,
                image_data,
                new_version,
                request,
            )

            if revision_result.get("success"):
                result.image_version = new_version
                result.image_hash = revision_result.get("branded_image_hash")
                result.image_calls = revision_result.get("image_request_count", 0)
                self._increment_revision_count(event_id, "image")
            else:
                result.ok = False
                result.error = revision_result.get("reason", "image_revision_failed")
                return result

        return result

    def _calculate_next_version(self, current_version: str | None) -> str:
        """Calculate next version string.

        v1 → v2 → v3, etc.
        """
        if not current_version:
            return "v1"

        try:
            # Parse version number
            if current_version.startswith("v"):
                num = int(current_version[1:])
            else:
                num = int(current_version)
            return f"v{num + 1}"
        except ValueError:
            # Fallback
            return f"{current_version}-rev-1"

    def _revise_article(
        self,
        event: NewsEvent,
        current_article: dict[str, Any],
        new_version: str,
        request: RevisionRequest,
    ) -> dict[str, Any]:
        """Revise article using existing writer/revision path.

        Creates Article V2 from Article V1 + feedback.
        """
        import hashlib

        # Get original story format
        story = self._event_to_story(event)

        # Build feedback for writer
        feedback = {
            "user_feedback": request.article_feedback,
            "rating": request.article_rating,
            "target_revision": request.requested_at,
        }

        # Use existing editorial compile path
        # This runs all required QA/grounding again
        try:
            writer = build_v4_writer(
                environ=self.environ,
                enable_failover=False,
                max_calls=24,
            )

            # Compile with feedback - reuse standard compile with feedback in event
            # Note: editorial compile path uses existing compile with story modifications

            # Get attempts root
            attempts_root = Path(__file__).resolve().parents[3] / "output" / "v5_attempts" / f"{event.event_id}-rev-{new_version}"
            attempts_root.mkdir(parents=True, exist_ok=True)

            # Add feedback metadata to story for writer
            story["editorial_feedback"] = feedback
            
            compiled = compile_v4_article(
                story=story,
                writer=writer,
                attempts_root=attempts_root,
                rank=1,
                research=False,  # Reuse existing research
            )

            if not compiled.ok or not compiled.article:
                return {
                    "ok": False,
                    "error": compiled.failure_class or "editorial_revision_failed",
                }

            # Freeze Article V2
            article = compiled.article
            article_content = article.get("article_body", "")
            article_hash = hashlib.sha256(article_content.encode("utf-8")).hexdigest()

            # Store new version
            self.version_store.save_article(
                event_id=event.event_id,
                version=new_version,
                article=article,
                article_hash=article_hash,
                qa_result=compiled.qa or {},
                metadata={
                    "word_count": compiled.final_words,
                    "grounding_supported": compiled.supported,
                    "grounding_ambiguous": compiled.ambiguous,
                    "grounding_unsupported": compiled.unsupported,
                    "revision_from": current_article.get("version", "v1"),
                    "revision_reason": request.article_feedback,
                    "previous_version_kept": True,
                },
            )

            return {
                "ok": True,
                "article_version": new_version,
                "article_hash": article_hash,
                "writer_calls": compiled.writer_calls,
            }

        except Exception as e:
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            }

    def _revise_image(
        self,
        event: NewsEvent,
        current_image: dict[str, Any],
        new_version: str,
        request: RevisionRequest,
    ) -> dict[str, Any]:
        """Revise image using existing image system.

        Generates Image V2 using feedback.
        """
        import hashlib

        from newsagent_v2.image.vertex_make_image import build_vertex_make_image_fn

        # Get article for context
        _, article_data = self._get_current_artifact(event.event_id, "article")

        if not article_data:
            return {
                "success": False,
                "reason": "no_article_for_image_context",
            }

        article = article_data.get("article", {})
        article_hash = hashlib.sha256(
            article.get("article_body", "").encode("utf-8")
        ).hexdigest()

        # Build job for image generation with feedback
        job = {
            "event_id": event.event_id,
            "article": article,
            "canonical_body_hash": article_hash,
            "article_hash": article_hash,
            "image_feedback": request.image_feedback,
            "target_revision": new_version,
        }

        # Use existing image generation
        image_fn = build_vertex_make_image_fn(
            self.environ,
            batch_id=f"{event.event_id}-rev-{new_version}",
        )

        result = image_fn(job)

        if result.get("success") and result.get("final_path"):
            # Freeze Image V2
            image_path = result["final_path"]
            image_hash = result.get("branded_image_hash", "")

            # Store new version
            self.version_store.save_image(
                event_id=event.event_id,
                version=new_version,
                image_path=image_path,
                image_hash=image_hash,
                metadata={
                    "raw_image_hash": result.get("raw_image_hash"),
                    "provider": result.get("provider"),
                    "model": result.get("model"),
                    "revision_from": current_image.get("version", "v1"),
                    "revision_reason": request.image_feedback,
                    "previous_version_kept": True,
                },
            )

        return result

    def _event_to_story(self, event: NewsEvent) -> dict[str, Any]:
        """Convert NewsEvent to story format expected by V4 compile/research."""
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
        }

    def get_revision_stats(self, event_id: str) -> dict[str, Any]:
        """Get revision statistics for an event."""
        with self._lock:
            counts = self._revision_counts.get(event_id, {"article": 0, "image": 0})

        return {
            "article_revisions": counts["article"],
            "image_revisions": counts["image"],
            "max_article_revisions": self.max_article_revisions,
            "max_image_revisions": self.max_image_revisions,
            "article_allowed": counts["article"] < self.max_article_revisions,
            "image_allowed": counts["image"] < self.max_image_revisions,
        }
