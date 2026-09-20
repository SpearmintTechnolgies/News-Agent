"""Targeted Revision Controller - V6 with Evidence Injection Fix.

Revisions operate on DELTAS:
- Article feedback only: regenerate ONLY article, keep image V1
- Image feedback only: regenerate ONLY image, keep article V1
- Both: regenerate both affected artifacts
- Ratings ONLY: NO revision (ratings are learning data only)

NEVER overwrite historical versions - always create new versions.
Key principle: V2 revision MUST inject V1 evidence into story["article_input"]["evidence_units"].
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
    """Targeted revision controller with proper V1 evidence injection.
    
    CRITICAL: Article revisions MUST inject evidence_units into article_input.
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
        - Ratings ONLY → NO revision (ratings are learning data only)
        """
        event_id = event.event_id

        # Determine what needs revision
        # CRITICAL: Ratings alone do NOT trigger revision - feedback required
        has_article_feedback = bool(request.article_feedback and request.article_feedback.strip())
        has_image_feedback = bool(request.image_feedback and request.image_feedback.strip())
        
        # Only revision targets with actual feedback
        revise_article = has_article_feedback
        revise_image = has_image_feedback

        if not revise_article and not revise_image:
            # Check if only ratings were provided (for clear error message)
            has_any_rating = request.article_rating is not None or request.image_rating is not None
            error_msg = (
                "Ratings alone do not trigger revision. Add feedback to revise."
                if has_any_rating else
                "No revision feedback provided."
            )
            return RevisionResult(
                ok=False,
                event_id=event_id,
                article_revised=False,
                image_revised=False,
                error=error_msg,
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

        # Store original versions for reference
        original_article_version = article_version
        original_image_version = image_version

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

            # Article revision WITH V1 evidence injection
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
                
                # Store link from V2 back to V1's evidence
                if revision_result.get("evidence_reused"):
                    result.error = None  # Clear any errors
            else:
                result.ok = False
                result.error = revision_result.get("error") or revision_result.get("reason") or "article_revision_failed"
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

            # Get current article version (might be V2 if article was revised)
            current_article_version = result.article_version or article_version
            
            # Image revision
            new_version = self._calculate_next_version(image_version)
            revision_result = self._revise_image(
                event,
                image_data,
                new_version,
                request,
                article_version=current_article_version,
            )

            if revision_result.get("success"):
                result.image_version = new_version
                result.image_hash = revision_result.get("branded_image_hash")
                result.image_calls = revision_result.get("image_request_count", 0)
                self._increment_revision_count(event_id, "image")
            else:
                result.ok = False
                result.error = revision_result.get("reason") or revision_result.get("error") or "image_revision_failed"
                return result

        # Build success message
        if result.ok:
            message_parts = ["Revision complete:"]
            if result.article_revised:
                message_parts.append(f"  Article: {result.article_version} (NEW)")
            else:
                message_parts.append(f"  Article: {original_article_version} (reused)")
            if result.image_revised:
                message_parts.append(f"  Image: {result.image_version} (NEW)")
            else:
                message_parts.append(f"  Image: {original_image_version} (reused)")
            
            # Make sure error is None on success
            result.error = None

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

    def _facts_to_evidence_units(
        self,
        authorized_facts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        evidence_ref_map: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert V1 authorized_facts to evidence_units format for compile.
        
        THIS IS THE CRITICAL FUNCTION - evidence loss happens here if conversion fails.
        The compile_v4_article with research=False expects evidence_units
        in the article_input to build ledgers and fact_bank.
        
        CRITICAL: Uses evidence_ref_map from V1 article to get real URLs/sources.
        
        Handles both list (from JSON) and tuple (from frozen dataclass).
        
        Each evidence_unit needs:
        - evidence_id: Unique ID like "E01", "E02"
        - text: The proposition text (sentence format)
        - source: Attribution/source name
        - url: Source URL (from V1 frozen article)
        - published: Publication timestamp
        """
        units = []
        evidence_ref_map = evidence_ref_map or {}
        
        # Handle both list (JSON) and tuple (frozen dataclass)
        # Also handle case where authorized_facts is a dict (malformed packet)
        if isinstance(authorized_facts, dict):
            authorized_facts = [authorized_facts]
        
        facts_iter = authorized_facts if isinstance(authorized_facts, (list, tuple)) else []
        
        for idx, fact in enumerate(facts_iter):
            if not isinstance(fact, dict):
                continue
            
            # Try both "proposition" (WriterEvidencePacket) and "text" (legacy) fields
            proposition = fact.get("proposition") or fact.get("text", "")
            if not proposition:
                continue
            
            # Get the fact ID and provenance
            fact_id = fact.get("id", f"P{idx + 1:02d}")
            provenance = fact.get("provenance", [])
            
            # Build source info from V1 evidence_refs if available
            source_name = ""
            source_url = ""
            
            # Look up evidence_ref from V1 article using provenance ID
            if isinstance(provenance, (list, tuple)) and provenance:
                first_provenance = str(provenance[0])
                # Check if this provenance ID maps to a V1 evidence_ref
                if first_provenance in evidence_ref_map:
                    ref = evidence_ref_map[first_provenance]
                    source_name = ref.get("source", "")
                    source_url = ref.get("url", "")
                # Fallback: if provenance is itself a URL
                elif first_provenance.startswith("http"):
                    source_url = first_provenance
            
            # Fallback to attribution field if no source from evidence_ref
            if not source_name:
                source_name = fact.get("attribution", "")
            
            # Ensure proposition ends with period
            text = str(proposition).strip()
            if text and not text.endswith(('.', '!', '?')):
                text += "."
            
            unit = {
                "evidence_id": f"E{idx + 1:02d}",
                "text": text,
                "source": source_name or "V1 Evidence",
                "url": source_url,
                "published": fact.get("time", ""),
                "_revision_fact_id": fact_id,
                "_v1_provenance": provenance[0] if isinstance(provenance, (list, tuple)) and provenance else "",
                # Preserve semantic fields
                "_subject": fact.get("subject", ""),
                "_predicate": fact.get("predicate", ""),
                "_object": fact.get("object", ""),
                "_polarity": fact.get("polarity", ""),
                "_status": fact.get("status", ""),
                "_entities": fact.get("entities", []),
            }
            units.append(unit)
        
        return units

    def _revise_article(
        self,
        event: NewsEvent,
        current_article: dict[str, Any],
        new_version: str,
        request: RevisionRequest,
    ) -> dict[str, Any]:
        """Revise article using V1's frozen evidence context.

        Creates Article V2 from:
        - V1's successful evidence/authorized facts
        - V1's WriterEvidencePacket
        - CEO editorial feedback
        - Same grounding/QA thresholds
        """
        import hashlib

        event_id = event.event_id
        
        # === LOAD V1 EVIDENCE CONTEXT ===
        current_version = current_article.get("version", "v1")
        
        # Get V1's attempts_root from metadata or legacy resolution
        attempts_root_v1 = self.version_store.get_article_attempts_root(event_id, current_version)
        if not attempts_root_v1:
            # Fallback: try common pattern
            story_dir = Path(__file__).resolve().parents[3] / "output" / "v5_stories" / f"STORY-{event_id}"
            if story_dir.exists():
                attempts_root_v1 = story_dir / "generation" / "jobs"
        
        # Load V1's evidence packet
        evidence_packet = self.version_store.get_evidence_packet(event_id, current_version)
        if not evidence_packet:
            evidence_packet = self.version_store.get_attempt_artifact(event_id, current_version, "evidence_packet")
        
        # Load V1's article (contains evidence_refs with URLs)
        v1_article_record = self.version_store.get_article(event_id, current_version)
        v1_article = v1_article_record.get("article", {}) if v1_article_record else {}
        
        # Build evidence ID -> URL/source map from V1 article claims
        evidence_ref_map = {}
        for claim in v1_article.get("claims", []):
            claim_id = claim.get("id") or claim.get("claim_id", "")
            evidence_refs = claim.get("evidence_refs", [])
            evidence_ids = claim.get("evidence_ids", [])
            
            # Map each evidence_id to its ref
            for idx, eid in enumerate(evidence_ids):
                if idx < len(evidence_refs):
                    evidence_ref_map[eid] = evidence_refs[idx]
                else:
                    evidence_ref_map[eid] = {}
        
        # Load V1's article_input if available
        article_input = self.version_store.get_attempt_artifact(event_id, current_version, "article_input")
        if not article_input:
            # Build minimal from event
            article_input = {
                "event_id": event_id,
                "representative_title": event.canonical_title,
                "topic": event.topic,
                "entities": list(event.entities),
                "evidence": [],
            }
        
        # === FAIL CLOSED: Cannot revise without evidence ===
        if not evidence_packet:
            return {
                "ok": False,
                "error": "REVISION_EVIDENCE_NOT_FOUND",
                "reason": f"Cannot revise article {current_version}: evidence packet not found. "
                          "Original generation attempt may have been cleaned up or failed to persist evidence.",
            }
        
        # Get authorized_facts - handle both list and tuple (frozen dataclass)
        authorized_facts = evidence_packet.get("authorized_facts", [])
        if not authorized_facts:
            return {
                "ok": False,
                "error": "REVISION_EVIDENCE_NOT_FOUND",
                "reason": f"Evidence packet found but contains no authorized facts. Cannot revise.",
            }
        
        # === CONVERT V1 AUTHORIZED FACTS TO EVIDENCE_UNITS ===
        # THIS IS THE CRITICAL STEP - must populate evidence_units for compile
        # Pass evidence_ref_map to get real URLs from V1 article
        evidence_units = self._facts_to_evidence_units(authorized_facts, evidence_ref_map)
        
        # Ensure article_input has evidence_units populated for compile
        article_input["evidence_units"] = evidence_units
        article_input["_revision_evidence_reused"] = True
        article_input["_source_evidence_count"] = len(authorized_facts)
        
        # CRITICAL: Also populate article_input["evidence"] for QA validation
        # QA's evidence_index() only looks at article_input["evidence"], not evidence_units
        # Must include URL entries so evidence refs in claims are validatable
        evidence_list = []
        for unit in evidence_units:
            if unit.get("url"):
                evidence_list.append({
                    "url": unit["url"],
                    "source": unit.get("source", ""),
                    "title": unit.get("text", "")[:200],
                })
        article_input["evidence"] = evidence_list
        
        # === BUILD PROPER STORY WITH V1 EVIDENCE ===
        story = {
            "event_id": event_id,
            "representative_title": event.canonical_title,
            "topic": event.topic,
            "entities": list(event.entities),
            "article_input": article_input,
            "evidence": article_input.get("evidence", []),
            "source_count": len(event.reports) if hasattr(event, 'reports') else 1,
            "_revision_context": {
                "from_version": current_version,
                "to_version": new_version,
                "evidence_reused": True,
                "evidence_fact_count": len(authorized_facts),
                "evidence_unit_count": len(evidence_units),
            }
        }
        
        # Inject V1 evidence packet for reference
        story["_evidence_packet_v1"] = evidence_packet

        # Build feedback for writer
        feedback = {
            "user_feedback": request.article_feedback,
            "rating": request.article_rating,
            "target_revision": request.requested_at,
        }

        # Use existing editorial compile path with V1 evidence
        try:
            writer = build_v4_writer(
                environ=self.environ,
                enable_failover=False,
                max_calls=24,
            )

            # Get attempts root for V2
            attempts_root_v2 = Path(__file__).resolve().parents[3] / "output" / "v5_attempts" / f"{event_id}-rev-{new_version}"
            attempts_root_v2.mkdir(parents=True, exist_ok=True)

            # Add feedback metadata to story
            story["editorial_feedback"] = feedback
            
            compiled = compile_v4_article(
                story=story,
                writer=writer,
                attempts_root=attempts_root_v2,
                rank=1,
                research=False,  # KEY: Evidence already injected via evidence_units
            )

            if not compiled.ok or not compiled.article:
                return {
                    "ok": False,
                    "error": compiled.failure_class or "editorial_revision_failed",
                    "notes": compiled.notes,
                }

            # Freeze Article V2
            article = compiled.article
            article_content = article.get("article_body", "")
            article_hash = hashlib.sha256(article_content.encode("utf-8")).hexdigest()

            # Store new version with link to V1 evidence
            self.version_store.save_article(
                event_id=event_id,
                version=new_version,
                article=article,
                article_hash=article_hash,
                qa_result=compiled.qa or {},
                metadata={
                    "word_count": compiled.final_words,
                    "grounding_supported": compiled.supported,
                    "grounding_ambiguous": compiled.ambiguous,
                    "grounding_unsupported": compiled.unsupported,
                    "revision_from": current_version,
                    "revision_reason": request.article_feedback,
                    "previous_version_kept": True,
                    "evidence_reused_from": current_version,
                    "evidence_fact_count": len(authorized_facts),
                    "attempts_root": str(attempts_root_v2),
                },
            )

            return {
                "ok": True,
                "article_version": new_version,
                "article_hash": article_hash,
                "writer_calls": compiled.writer_calls,
                "evidence_reused": True,
                "evidence_unit_count": len(evidence_units),
                "evidence_source": current_version,
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
        article_version: str | None = None,
    ) -> dict[str, Any]:
        """Revise image using existing image system.

        Generates Image V2 using feedback.
        """
        import hashlib

        from newsagent_v2.image.vertex_make_image import build_vertex_make_image_fn

        # Get current article for context
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
        """Convert NewsEvent to story format expected by V4 compile/research.
        
        DEPRECATED: Use V1 evidence loading instead for revisions.
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
