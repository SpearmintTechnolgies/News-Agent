"""V5 Review Callbacks - ratings, feedback, revise, approve, publish.

Wired to persistent ReviewSystem and RevisionController.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from newsagent_v2.v5_generation.persistent_review import (
    PersistentReviewStore,
    RatingRecord,
    FeedbackRecord,
    ApprovalRecord,
)
from newsagent_v2.v5_generation.persistent_store import PersistentV5Store
from newsagent_v2.v5_generation.revision_controller import RevisionController
from newsagent_v2.v5_generation.version_store import VersionStore

DATA_ROOT = Path("./data/v5_state")


class V5ReviewCallbackHandler:
    """Handle review callbacks: rate, feedback, revise, approve, publish."""
    
    def __init__(
        self,
        review_store: PersistentReviewStore,
        revision_controller: RevisionController,
        version_store: VersionStore,
        persistent_store: PersistentV5Store | None = None,
    ) -> None:
        self.review_store = review_store
        self.revision_controller = revision_controller
        self.version_store = version_store
        self.persistent_store = persistent_store
    
    def parse_callback(self, data: str) -> dict[str, str] | None:
        """Parse callback data: action:event_id:version[:extra]"""
        if not data:
            return None
        
        parts = data.split(":")
        if len(parts) < 2:
            return None
        
        action = parts[0]
        
        # V5 review actions
        valid_actions = {
            "rate_article", "rate_image",  # Show rating UI
            "save_rate_article", "save_rate_image",  # Save rating
            "feedback_article", "feedback_image",  # Enter feedback mode
            "revise", "approve", "reject", "publish",
            "view_full",  # View full article (idempotent/read-only)
        }
        
        if action not in valid_actions:
            return None
        
        result: dict[str, str] = {"action": action, "event_id": parts[1]}
        
        if len(parts) > 2:
            result["version"] = parts[2]
        if len(parts) > 3:
            result["extra"] = parts[3]
        
        return result
    
    def handle_rate_start(
        self,
        artifact_type: str,  # "article" or "image"
        event_id: str,
        version: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Show rating selection UI (1-10)."""
        import json
        
        # Build rating keyboard with callback data
        keyboard = {"inline_keyboard": []}
        row1 = []
        row2 = []
        
        for rating in range(1, 6):
            row1.append({
                "text": str(rating),
                "callback_data": f"save_rate_{artifact_type}:{event_id}:{version}:{rating}",
            })
        for rating in range(6, 11):
            row2.append({
                "text": str(rating),
                "callback_data": f"save_rate_{artifact_type}:{event_id}:{version}:{rating}",
            })
        
        keyboard["inline_keyboard"] = [row1, row2]
        
        artifact_name = "Article" if artifact_type == "article" else "Image"
        
        return {
            "ok": True,
            "action": f"rate_{artifact_type}_ui",
            "message": f"⭐ Rate {artifact_name} V{version}:\n\nSelect 1-10",
            "reply_markup": keyboard,
        }
    
    def handle_rate_save(
        self,
        artifact_type: str,
        event_id: str,
        version: str,
        rating_str: str,
        reviewer: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Handle rating saved."""
        try:
            rating = int(rating_str)
        except ValueError:
            return {"ok": False, "reason": "invalid_rating", "message": "Invalid rating"}
        
        if rating < 1 or rating > 10:
            return {"ok": False, "reason": "invalid_rating", "message": "Rating must be 1-10"}
        
        # Check version exists
        artifact = self.version_store.get_article(event_id, version) if artifact_type == "article" else \
                   self.version_store.get_image_path(event_id, version)
        if not artifact:
            return {"ok": False, "reason": "version_not_found", "message": f"{artifact_type} V{version} not found"}
        
        from datetime import datetime, timezone
        
        # Check if already rated - if so, update
        existing = self.review_store.get_rating_for_artifact(event_id, artifact_type, version)
        rating_id = existing.rating_id if existing else \
            f"rating-{event_id}-{artifact_type}-{version}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        
        rating_record = RatingRecord(
            rating_id=rating_id,
            event_id=event_id,
            job_id=job_id,
            artifact_type=artifact_type,
            version=version,
            rating=rating,
            reviewer=reviewer,
        )
        
        self.review_store.save_rating(rating_record)
        
        # Calculate average
        avg = self.review_store.get_average_rating(event_id, artifact_type)
        avg_text = f" (avg: {avg}/10)" if avg else ""
        
        emoji = "⭐" * (rating // 2)
        return {
            "ok": True,
            "action": f"rate_{artifact_type}",
            "event_id": event_id,
            "version": version,
            "rating": rating,
            "message": f"{emoji} Rated {artifact_type} V{version}: {rating}/10{avg_text}",
        }
    
    def handle_feedback_start(
        self,
        artifact_type: str,
        event_id: str,
        version: str,
        job_id: str,
        reviewer: str,
        chat_id: str = "",
    ) -> dict[str, Any]:
        """Enter feedback mode - next message becomes feedback."""
        # Store awaiting state in persistent store for cross-restart safety
        if self.persistent_store and chat_id:
            self.persistent_store.set_awaiting_feedback(
                chat_id=chat_id,
                event_id=event_id,
                artifact_type=artifact_type,
                version=version,
            )
        
        article_or_image = "article" if artifact_type == "article" else "image"
        
        # Build keyboard with cancel
        keyboard = {
            "inline_keyboard": [
                [{"text": "❌ Cancel", "callback_data": f"feedback_cancel:{event_id}"}]
            ]
        }
        
        return {
            "ok": True,
            "action": f"feedback_mode_{artifact_type}",
            "event_id": event_id,
            "version": version,
            "awaiting_feedback": True,
            "artifact_type": artifact_type,
            "message": f"✍️ Send your feedback for {article_or_image} V{version}.\n\nYour next text message will be saved as feedback.",
            "reply_markup": keyboard,
        }
    
    def capture_feedback(
        self,
        event_id: str,
        artifact_type: str,
        version: str,
        feedback_text: str,
        reviewer: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Capture feedback text."""
        from datetime import datetime, timezone
        
        # Validate version exists
        artifact = self.version_store.get_article(event_id, version) if artifact_type == "article" else \
                   self.version_store.get_image_path(event_id, version)
        if not artifact:
            return {"ok": False, "reason": "version_not_found"}
        
        feedback_id = f"feedback-{event_id}-{artifact_type}-{version}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        feedback_record = FeedbackRecord(
            feedback_id=feedback_id,
            event_id=event_id,
            job_id=job_id,
            artifact_type=artifact_type,
            version=version,
            feedback_text=feedback_text,
            reviewer=reviewer,
        )
        
        self.review_store.save_feedback(feedback_record)
        
        return {
            "ok": True,
            "action": "feedback_captured",
            "event_id": event_id,
            "version": version,
            "message": f"✅ Feedback saved for {artifact_type} V{version}.",
        }
    
    def handle_feedback_cancel(self, event_id: str, chat_id: str = "") -> dict[str, Any]:
        """Cancel feedback mode."""
        if self.persistent_store and chat_id:
            self.persistent_store.clear_awaiting_feedback(chat_id)
        return {
            "ok": True,
            "action": "feedback_cancelled",
            "message": "❌ Feedback cancelled.",
        }
    
    def handle_revise(
        self,
        event_id: str,
        version: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Handle REVISE callback."""
        from newsagent_v2.discovery.event_clusterer import NewsEvent
        from newsagent_v2.v5_generation.revision_controller import RevisionRequest
        
        # Check approval - cannot revise after approval
        if self.review_store.has_approval(event_id):
            return {
                "ok": False,
                "reason": "already_approved",
                "message": "Story already approved. Cannot revise after approval.",
            }
        
        # Get pending feedback for this event
        feedbacks = self.review_store.get_feedback_for_event(event_id)
        
        # Get ONLY feedback for current versions (not old versions)
        current_article_version = self.version_store.get_current_version(event_id, "article")
        current_image_version = self.version_store.get_current_version(event_id, "image")
        
        article_feedback = [f for f in feedbacks 
                          if f.artifact_type == "article" and f.version == current_article_version]
        image_feedback = [f for f in feedbacks 
                        if f.artifact_type == "image" and f.version == current_image_version]
        
        # Build revision request
        request = RevisionRequest(
            event_id=event_id,
            article_feedback=article_feedback[-1].feedback_text if article_feedback else "",
            image_feedback=image_feedback[-1].feedback_text if image_feedback else "",
        )
        
        # Create minimal event for revision controller
        # Get article to use for building event
        article_data = self.version_store.get_article(event_id, current_article_version or "v1")
        
        if not article_data:
            # Try to get from v1
            article_data = self.version_store.get_article(event_id, "v1")
        
        if not article_data:
            return {
                "ok": False,
                "reason": "no_article",
                "message": "No article found to revise."
            }
        
        # Build minimal event from stored data
        event = NewsEvent(
            event_id=event_id,
            canonical_title=article_data.get("article", {}).get("headline", "Unknown"),
        )
        
        result = self.revision_controller.revise(event, request)
        
        if result.ok:
            # Return compact revision result for Telegram delivery
            # The runtime will call send_revision_package with these versions
            return {
                "ok": True,
                "action": "revise_complete",
                "article_revised": result.article_revised,
                "image_revised": result.image_revised,
                "article_version": result.article_version,
                "image_version": result.image_version,
                "article_hash": result.article_hash,
                "image_hash": result.image_hash,
                "article_calls": result.article_calls,
                "image_calls": result.image_calls,
                "event_id": event_id,
                "canonical_title": event.canonical_title or "Unknown",
                # Signal to runtime that full revision package should be sent
                "send_revision_package": True,
                "compact_message": self._build_revision_summary(
                    result.article_revised,
                    result.image_revised,
                    result.article_version,
                    result.image_version,
                ),
            }
        else:
            return {
                "ok": False,
                "reason": result.error,
                "message": f"❌ Revision failed: {result.error}",
            }
    
    def _build_revision_summary(
        self,
        article_revised: bool,
        image_revised: bool,
        article_version: str | None,
        image_version: str | None,
    ) -> str:
        """Build compact revision summary for Telegram."""
        lines = []
        
        if article_version:
            status = "REVISED" if article_revised else "REUSED"
            lines.append(f"✍️ Article {article_version} — {status}")
        else:
            lines.append("✍️ Article: None")
        
        if image_version:
            status = "NEW" if image_revised else "REUSED"
            lines.append(f"🖼 Image {image_version} — {status}")
        else:
            lines.append("🖼 Image: None")
        
        return "\n".join(lines)
    
    def handle_approve(
        self,
        event_id: str,
        reviewer: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Handle APPROVE callback."""
        from datetime import datetime, timezone
        
        # Get current versions
        article_version = self.version_store.get_current_version(event_id, "article")
        image_version = self.version_store.get_current_version(event_id, "image")
        
        if not article_version:
            return {"ok": False, "reason": "no_article", "message": "No article to approve"}
        
        # Get hashes
        article_hash = self.version_store.get_article_hash(event_id, article_version) or ""
        image_hash = self.version_store.get_image_hash(event_id, image_version) or "" if image_version else ""
        
        # Create approval record
        approval_id = f"approval-{event_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        approval = ApprovalRecord(
            approval_id=approval_id,
            event_id=event_id,
            job_id=job_id,
            approved=True,
            article_version=article_version,
            article_hash=article_hash,
            image_version=image_version or "",
            image_hash=image_hash,
            approved_by=reviewer,
        )
        
        self.review_store.save_approval(approval)
        
        return {
            "ok": True,
            "action": "approve",
            "event_id": event_id,
            "article_version": article_version,
            "article_hash": article_hash,
            "image_version": image_version,
            "image_hash": image_hash,
            "message": f"✅ APPROVED:\n  Article: {article_version}\n  Image: {image_version or 'N/A'}",
        }
    
    def handle_reject(self, event_id: str, reviewer: str) -> dict[str, Any]:
        """Handle REJECT callback."""
        return {
            "ok": True,
            "action": "reject",
            "event_id": event_id,
            "message": "🚫 Story rejected.",
        }
    
    def handle_publish(
        self,
        event_id: str,
        reviewer: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Handle PUBLISH callback."""
        # Check approval exists
        approval = self.review_store.get_approval_for_event(event_id)
        if not approval:
            return {
                "ok": False,
                "reason": "not_approved",
                "message": "Story must be approved before publishing.",
            }
        
        # Check WordPress preflight
        from newsagent_v2.v5_generation.provider_preflight import ProviderPreflight
        preflight = ProviderPreflight()
        wp_status = preflight.check_wordpress()
        
        if wp_status.status != "READY":
            return {
                "ok": False,
                "reason": "wordpress_not_configured",
                "message": "WordPress is not configured. Publication package is ready.",
                "approval": {
                    "article_version": approval.article_version,
                    "article_hash": approval.article_hash,
                    "image_version": approval.image_version,
                    "image_hash": approval.image_hash,
                },
            }
        
        # Check if already published (idempotency)
        publication = self._get_publication(event_id)
        if publication:
            return {
                "ok": True,
                "action": "publish",
                "event_id": event_id,
                "url": publication.get("url"),
                "post_id": publication.get("post_id"),
                "message": f"✅ Already published:\n{publication.get('url')}",
            }
        
        # PUBLISH TO WORDPRESS
        try:
            from newsagent_v2.publish.wordpress import WordPressPublisher
            
            # Build publication package
            package = {
                "event_id": event_id,
                "article_version": approval.article_version,
                "article_hash": approval.article_hash,
                "image_version": approval.image_version,
                "image_hash": approval.image_hash,
            }
            
            # Get article content
            article = self.version_store.get_article(event_id, approval.article_version)
            if not article:
                return {"ok": False, "reason": "article_not_found"}
            
            # Initialize WordPress publisher
            wp = WordPressPublisher()
            
            # Publish
            result = wp.publish_article(
                title=article.get("title", ""),
                content=article.get("article_body", ""),
                featured_image=self.version_store.get_image_path(event_id, approval.image_version) if approval.image_version else None,
            )
            
            if result.get("ok"):
                # Save publication record
                self._save_publication(event_id, result.get("url"), result.get("post_id"))
                
                return {
                    "ok": True,
                    "action": "publish",
                    "event_id": event_id,
                    "url": result.get("url"),
                    "post_id": result.get("post_id"),
                    "message": f"✅ Published!\n{result.get('url')}",
                }
            else:
                return {
                    "ok": False,
                    "reason": "publish_failed",
                    "message": f"WordPress error: {result.get('error')}",
                }
                
        except ImportError:
            # WordPress publisher not available
            return {
                "ok": False,
                "reason": "wordpress_not_available",
                "message": "WordPress publisher not available. Package preserved.",
            }
        except Exception as e:
            return {
                "ok": False,
                "reason": "publish_exception",
                "message": f"Publish failed: {str(e)[:100]}",
            }
    
    def _get_publication(self, event_id: str) -> dict[str, Any] | None:
        """Check if already published."""
        # Load from persistent store
        path = DATA_ROOT / f"publication_{event_id}.json"
        if path.exists():
            try:
                import json
                return json.loads(path.read_text())
            except Exception:
                return None
        return None
    
    def _save_publication(self, event_id: str, url: str, post_id: str) -> None:
        """Save publication record."""
        import json
        from datetime import datetime, timezone
        
        path = DATA_ROOT / f"publication_{event_id}.json"
        record = {
            "event_id": event_id,
            "url": url,
            "post_id": post_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(record, indent=2))
    
    def handle(self, data: str, reviewer: str = "user", job_id: str = "", chat_id: str = "") -> dict[str, Any]:
        """Main dispatch for review callbacks."""
        parsed = self.parse_callback(data)
        if not parsed:
            return {"ok": False, "reason": "invalid_callback", "data": data[:50]}
        
        action = parsed["action"]
        event_id = parsed.get("event_id", "")
        version = parsed.get("version", "")
        extra = parsed.get("extra", "")
        
        if action == "rate_article":
            return self.handle_rate_start("article", event_id, version, job_id)
        elif action == "rate_image":
            return self.handle_rate_start("image", event_id, version, job_id)
        elif action == "save_rate_article":
            return self.handle_rate_save("article", event_id, version, extra, reviewer, job_id)
        elif action == "save_rate_image":
            return self.handle_rate_save("image", event_id, version, extra, reviewer, job_id)
        elif action == "feedback_article":
            return self.handle_feedback_start("article", event_id, version, job_id, reviewer, chat_id)
        elif action == "feedback_image":
            return self.handle_feedback_start("image", event_id, version, job_id, reviewer, chat_id)
        elif action == "feedback_cancel":
            return self.handle_feedback_cancel(event_id, chat_id)
        elif action == "revise":
            return self.handle_revise(event_id, version, job_id)
        elif action == "approve":
            return self.handle_approve(event_id, reviewer, job_id)
        elif action == "reject":
            return self.handle_reject(event_id, reviewer)
        elif action == "publish":
            return self.handle_publish(event_id, reviewer, job_id)
        elif action == "view_full":
            return self.handle_view_full(event_id, version, artifact_type=extra or "article")
        
        return {"ok": False, "reason": "unknown_action", "action": action}
    
    def handle_view_full(
        self,
        event_id: str,
        version: str,
        artifact_type: str = "article",
    ) -> dict[str, Any]:
        """Handle VIEW FULL callback - idempotent, read-only, ZERO provider calls.
        
        Loads persisted artifact from VersionStore and returns for Telegram display.
        Does NOT create new versions.
        """
        print(f"[VIEWFULL-DIAG-3] HANDLER: handle_view_full() ENTERED")
        print(f"[VIEWFULL-DIAG-3]   event_id={event_id}")
        print(f"[VIEWFULL-DIAG-3]   version={version}")
        print(f"[VIEWFULL-DIAG-3]   artifact_type={artifact_type}")
        
        if artifact_type == "article":
            article_data = self.version_store.get_article(event_id, version)
            print(f"[VIEWFULL-DIAG-3]   article_data found={article_data is not None}")
            if not article_data:
                return {"ok": False, "reason": "article_not_found", "message": f"Article {version} not found"}
            
            article = article_data.get("article", {})
            return {
                "ok": True,
                "action": "view_full_article",
                "event_id": event_id,
                "version": version,
                "headline": article.get("headline", "Unknown"),
                "body": article.get("article_body", ""),
                "message": f"📖 Article {version} (read-only)",
            }
        else:
            image_path = self.version_store.get_image_path(event_id, version)
            if not image_path or not image_path.exists():
                return {"ok": False, "reason": "image_not_found", "message": f"Image {version} not found"}
            
            return {
                "ok": True,
                "action": "view_full_image",
                "event_id": event_id,
                "version": version,
                "image_path": str(image_path),
                "message": f"🖼 Image {version} (read-only)",
            }
    
    def check_and_capture_feedback_text(self, chat_id: str, text: str, reviewer: str = "user") -> dict[str, Any] | None:
        """Check if chat is in feedback mode and capture the text.
        
        Returns result dict if captured, None if not in feedback mode.
        """
        if not self.persistent_store or not chat_id:
            return None
        
        awaiting = self.persistent_store.get_awaiting_feedback(chat_id)
        if not awaiting:
            return None
        
        event_id = awaiting["event_id"]
        artifact_type = awaiting["artifact_type"]
        version = awaiting["version"]
        
        # Capture the feedback
        result = self.capture_feedback(
            event_id=event_id,
            artifact_type=artifact_type,
            version=version,
            feedback_text=text,
            reviewer=reviewer,
            job_id="",
        )
        
        # Clear awaiting state
        self.persistent_store.clear_awaiting_feedback(chat_id)
        
        return result
