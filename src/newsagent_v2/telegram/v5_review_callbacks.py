"""Review card callbacks: rate, feedback, revise, edit, reject, approve & publish.

REVISE and EDIT hand off to the GenerationWorker, which updates the WordPress draft and sends
a new review card in the background.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from newsagent_v2.publication.master_index import MasterIndexStore
from newsagent_v2.v5_generation.persistent_review import (
    ApprovalRecord,
    FeedbackRecord,
    PersistentReviewStore,
    RatingRecord,
)
from newsagent_v2.v5_generation.persistent_store import PersistentV5Store
from newsagent_v2.v5_generation.run_story_adapter import Revision, next_version
from newsagent_v2.v5_generation.version_store import VersionStore
from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle

DATA_ROOT = Path("./data/v5_state")
SITE_INDEX_CACHE = Path("data/v6_site_index.json")


class V5ReviewCallbackHandler:
    """Handle review callbacks: rate, feedback, revise, edit, approve, publish."""

    def __init__(
        self,
        review_store: PersistentReviewStore,
        version_store: VersionStore,
        persistent_store: PersistentV5Store | None = None,
        wordpress_lifecycle: WordPressDraftLifecycle | None = None,
        master_index: MasterIndexStore | None = None,
        environ: dict[str, str] | None = None,
        generation_worker: Any = None,
    ) -> None:
        self.review_store = review_store
        self.version_store = version_store
        self.persistent_store = persistent_store
        self.wordpress_lifecycle = wordpress_lifecycle
        self.master_index = master_index or MasterIndexStore()
        self.environ = environ or {}
        self.generation_worker = generation_worker
        self.max_article_revisions = int(self.environ.get("V5_MAX_ARTICLE_REVISIONS", "3"))
    
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
            "revise", "edit", "approve", "reject", "publish", "unpublish",
            "author", "set_author", "site_author",
            "view_full",  # View full article (idempotent/read-only)
        }
        
        if action not in valid_actions:
            return None
        
        result: dict[str, str] = {"action": action, "event_id": parts[1]}
        
        if len(parts) > 2:
            result["version"] = parts[2]
        if len(parts) > 3:
            result["extra"] = parts[3]
        if len(parts) > 4:
            result["image_version"] = parts[4]
        
        return result
    
    def handle_rate_start(
        self,
        artifact_type: str,  # "article" or "image"
        event_id: str,
        version: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Show rating selection UI (1-10)."""
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

    def handle_edit_start(self, event_id: str, version: str, chat_id: str) -> dict[str, Any]:
        """Collect one precise edit instruction for the current article."""
        if self.persistent_store and chat_id:
            self.persistent_store.set_awaiting_feedback(
                chat_id=chat_id, event_id=event_id, artifact_type="edit", version=version,
            )
        return {
            "ok": True, "action": "edit_mode", "event_id": event_id, "version": version,
            "awaiting_edit": True,
            "message": "✍️ Tell me the exact change to make to the current article.",
        }

    def handle_edit_instruction(self, event_id: str, version: str, instruction: str) -> dict[str, Any]:
        """Apply a precise edit grounded in the current text, save it as a new version, refresh the draft."""
        article_data = self.version_store.get_article(event_id, version)
        if not article_data:
            return {"ok": False, "reason": "article_not_found", "message": "Current article was not found."}
        if self.version_store.get_current_version(event_id, "article") not in {None, version}:
            return {"ok": False, "reason": "stale_version",
                    "message": "A newer version of this article exists; use EDIT on the latest review card."}
        updated = self._apply_precise_edit(dict(article_data.get("article") or {}), instruction)
        if updated is None:
            return {"ok": False, "reason": "unsupported_edit",
                    "message": "That edit is not a supported precise change or is not grounded in the current article."}
        if self.generation_worker is None:
            return {"ok": False, "reason": "worker_unavailable", "message": "Editing is unavailable right now."}

        new_version = next_version(version)
        body = str(updated.get("article_body") or "")
        self.version_store.save_article(
            event_id, new_version, updated, hashlib.sha256(body.encode("utf-8")).hexdigest(),
            article_data.get("qa_result") or {},
            {**(article_data.get("metadata") or {}), "edit_instruction": instruction, "edited_from": version},
        )
        image_version = self.version_store.get_current_version(event_id, "image")
        self.generation_worker.apply_edit(event_id, new_version, image_version)
        return {
            "ok": True, "action": "edit_complete", "event_id": event_id, "article_version": new_version,
            "message": f"✍️ Edit saved as {new_version}. Updating the WordPress draft; a new review card follows.",
        }

    def _apply_precise_edit(self, article: dict[str, Any], instruction: str) -> dict[str, Any] | None:
        text = instruction.strip()
        body = str(article.get("article_body") or "")
        candidate = dict(article)
        match = re.match(r"change\s+the\s+headline\s+to\s*[:\-]?\s*(.+)$", text, re.I)
        if match:
            value = match.group(1).strip().strip('"')
            if value and self._grounded_edit_text(value, body):
                candidate["headline"] = value
                return candidate
            return None
        match = re.match(r"replace\s+(.+?)\s+with\s+(.+)$", text, re.I)
        if match:
            old, new = match.group(1).strip().strip('"'), match.group(2).strip().strip('"')
            if old and old in body and self._grounded_edit_text(new, body):
                candidate["article_body"] = body.replace(old, new, 1)
                return candidate
            return None
        match = re.match(r"change\s+the\s+second\s+sentence\s+of\s+the\s+intro\s+to\s*[:\-]?\s*(.+)$", text, re.I)
        if match:
            paragraphs = body.split("\n\n")
            sentences = re.split(r"(?<=[.!?])\s+", paragraphs[0].strip())
            value = match.group(1).strip().strip('"')
            if len(sentences) >= 2 and value and self._grounded_edit_text(value, body):
                sentences[1] = value
                paragraphs[0] = " ".join(sentences)
                candidate["article_body"] = "\n\n".join(paragraphs)
                return candidate
            return None
        match = re.match(r"remove\s+the\s+last\s+sentence\s+from\s+paragraph\s+(\d+)", text, re.I)
        if match:
            index = int(match.group(1)) - 1
            paragraphs = body.split("\n\n")
            if 0 <= index < len(paragraphs):
                sentences = re.split(r"(?<=[.!?])\s+", paragraphs[index].strip())
                if len(sentences) > 1:
                    paragraphs[index] = " ".join(sentences[:-1]).strip()
                    candidate["article_body"] = "\n\n".join(paragraphs)
                    return candidate
        return None

    @staticmethod
    def _grounded_edit_text(value: str, body: str) -> bool:
        words = [word.lower() for word in value.split() if len(word) > 2]
        source = body.lower()
        return bool(words) and all(word in source for word in words)

    def handle_revise(self, event_id: str, version: str, job_id: str) -> dict[str, Any]:
        """REVISE: rewrite with the saved ARTICLE/IMAGE FEEDBACK in the background."""
        if self.review_store.has_approval(event_id):
            return {"ok": False, "reason": "already_approved",
                    "message": "Story already approved. Cannot revise after approval."}
        article_version = self.version_store.get_current_version(event_id, "article")
        image_version = self.version_store.get_current_version(event_id, "image")
        if not article_version:
            return {"ok": False, "reason": "no_article", "message": "No article found to revise."}
        feedbacks = self.review_store.get_feedback_for_event(event_id)
        article_feedback = [f.feedback_text for f in feedbacks
                            if f.artifact_type == "article" and f.version == article_version]
        image_feedback = [f.feedback_text for f in feedbacks
                          if f.artifact_type == "image" and f.version == image_version]
        if not article_feedback and not image_feedback:
            return {"ok": False, "reason": "no_feedback",
                    "message": "Add ARTICLE FEEDBACK or IMAGE FEEDBACK first, then press REVISE."}
        if article_feedback and len(self.version_store.list_article_versions(event_id)) > self.max_article_revisions:
            return {"ok": False, "reason": "revision_limit",
                    "message": f"This story already has {self.max_article_revisions} article revisions."}
        if self.generation_worker is None:
            return {"ok": False, "reason": "worker_unavailable", "message": "Revising is unavailable right now."}
        record = self.version_store.get_article(event_id, article_version) or {}
        started = self.generation_worker.start_revision(
            event_id,
            Revision(
                article_feedback=article_feedback[-1] if article_feedback else "",
                image_feedback=image_feedback[-1] if image_feedback else "",
                article_version=article_version,
                image_version=image_version,
            ),
            headline=str((record.get("article") or {}).get("headline") or ""),
        )
        if not started.get("ok"):
            return {"ok": False, "reason": started.get("reason"), "message": started.get("message")}
        parts = [p for p, wanted in (("article", article_feedback), ("image", image_feedback)) if wanted]
        return {
            "ok": True, "action": "revise", "event_id": event_id, "job_id": started.get("job_id"),
            "message": (
                f"✍️ Revising the {' and '.join(parts)} with your feedback. "
                "The WordPress draft will be updated and a new review card will follow in a few minutes."
            ),
        }

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
    
    def handle_approve_and_publish(
        self, event_id: str, reviewer: str, job_id: str, version: str = ""
    ) -> dict[str, Any]:
        """Approval publishes immediately; the approval is kept even if publishing fails.

        ``version`` is the article version on the pressed card; an outdated card is refused.
        """
        current = self.version_store.get_current_version(event_id, "article")
        if version and current and version != current:
            return {"ok": False, "reason": "stale_version", "action": "approve",
                    "message": f"This card is for {version}, but {current} is the latest. "
                               "Approve from the latest review card."}
        approved = self.handle_approve(event_id, reviewer, job_id)
        if not approved.get("ok"):
            return approved
        published = self.handle_publish(event_id, reviewer, job_id)
        message = published.get("message") or (
            "✅ Published" if published.get("ok") else "⚠️ Approved, but publishing failed. Press PUBLISH to retry."
        )
        if not published.get("ok"):
            message = f"✅ Approved, but publishing failed: {message}\nPress PUBLISH to retry."
        return {**published, "action": "publish" if published.get("ok") else "approve", "approval": approved,
                "message": message}

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
        preflight = ProviderPreflight(environ=self.environ or None)
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
            post_id = publication.get("post_id")
            return {
                "ok": True,
                "action": "publish",
                "event_id": event_id,
                "url": publication.get("url"),
                "post_id": int(post_id) if str(post_id or "").isdigit() else post_id,
                "message": f"✅ Already published:\n{publication.get('url')}",
            }
        
        if self.wordpress_lifecycle is None:
            return {"ok": False, "reason": "wordpress_not_configured", "message": "WordPress is not configured."}
        try:
            draft_result = self.wordpress_lifecycle.publish_draft(event_id)
        except Exception as exc:  # noqa: BLE001 - shown to the editor
            return {"ok": False, "reason": "publish_exception", "message": f"WordPress publish failed: {str(exc)[:100]}"}
        if not draft_result.ok:
            if getattr(draft_result, "error_code", None) == "draft_not_found":
                return {"ok": False, "reason": "draft_not_found",
                        "message": "No WordPress draft exists for this story; run it again with RUN STORY."}
            return {"ok": False, "reason": "publish_failed", "message": draft_result.error or "WordPress publish failed"}
        self._save_publication(event_id, draft_result.wp_url, str(draft_result.wp_post_id))
        self.master_index.record_publication(
            event_id=event_id, canonical_url=draft_result.wp_url or "",
            wp_post_id=draft_result.wp_post_id,
            article_version=approval.article_version,
            image_version=approval.image_version,
        )
        SITE_INDEX_CACHE.unlink(missing_ok=True)  # new post becomes a link target for the next drafts
        return {"ok": True, "action": "publish", "event_id": event_id,
                "url": draft_result.wp_url, "post_id": draft_result.wp_post_id,
                "message": f"✅ Published\n{draft_result.wp_url}"}

    def _site_authors(self) -> list[Any]:
        if self.wordpress_lifecycle is None:
            return []
        from newsagent_v2.wordpress.authors import list_site_authors

        return list_site_authors(self.wordpress_lifecycle.config, self.wordpress_lifecycle.transport)

    def _author_card(self, event_id: str, authors: list[Any], author_name: str, article_version: str, image_version: str, *, selected_id: int | None, picking: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "action": "author" if picking else "set_author",
            "edit_card": True,
            "picking": picking,
            "event_id": event_id,
            "author_name": author_name,
            "selected_id": selected_id,
            "authors": authors,
            "article_version": article_version or "v1",
            "image_version": image_version or "v1",
            "message": "Tap an author on the card." if picking else f"Author is {author_name}.",
        }

    def handle_author_menu(self, event_id: str, article_version: str = "v1", image_version: str = "v1") -> dict[str, Any]:
        """Put the site's authors onto this review card."""
        from newsagent_v2.wordpress.authors import author_for

        authors = self._site_authors()
        if not authors:
            return {"ok": False, "action": "author", "message": "WordPress did not return any authors."}
        current = author_for(event_id)
        return self._author_card(
            event_id, authors, current.name if current else "not chosen",
            article_version, image_version, selected_id=current.user_id if current else None, picking=True,
        )

    def handle_set_author(self, event_id: str, user_id: str, article_version: str = "v1", image_version: str = "v1") -> dict[str, Any]:
        from newsagent_v2.wordpress.authors import find_author, remember_event

        authors = self._site_authors()
        author = find_author(authors, user_id)
        if author is None:
            return {"ok": False, "action": "set_author", "message": "That author is not on the site."}
        remember_event(event_id, author)
        if self.wordpress_lifecycle is not None:
            result = self.wordpress_lifecycle.assign_author(event_id, author.user_id)
            if not result.ok and result.error_code != "draft_not_found":
                return {"ok": False, "action": "set_author", "message": result.error or "Could not change the author."}
        return self._author_card(
            event_id, authors, author.name, article_version, image_version,
            selected_id=author.user_id, picking=False,
        )

    def handle_site_author(self, user_id: str) -> dict[str, Any]:
        from newsagent_v2.wordpress.authors import find_author, remember_default

        author = find_author(self._site_authors(), user_id)
        if author is None:
            return {"ok": False, "action": "site_author", "message": "That author is not on the site."}
        remember_default(author)
        return {
            "ok": True,
            "action": "site_author",
            "message": f"Next articles will be by {author.name}. Open a review card and tap CHANGE AUTHOR to switch one story.",
        }

    def handle_unpublish(self, event_id: str) -> dict[str, Any]:
        """Revert a published post and remove it from internal-link selection."""
        if self.wordpress_lifecycle is None:
            return {"ok": False, "reason": "wordpress_not_configured"}
        result = self.wordpress_lifecycle.unpublish_draft(event_id)
        if not result.ok:
            return {"ok": False, "reason": "unpublish_failed", "message": result.error or "Unpublish failed"}
        self._remove_publication(event_id)
        return {
            "ok": True,
            "action": "unpublish",
            "event_id": event_id,
            "post_id": result.wp_post_id,
            "status": "draft",
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

    def _remove_publication(self, event_id: str) -> None:
        self.master_index.remove_publication(event_id)
    
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
        elif action == "edit":
            return self.handle_edit_start(event_id, version, chat_id)
        elif action == "approve":
            return self.handle_approve_and_publish(event_id, reviewer, job_id, version)
        elif action == "reject":
            return self.handle_reject(event_id, reviewer)
        elif action == "publish":
            return self.handle_publish(event_id, reviewer, job_id)
        elif action == "unpublish":
            return self.handle_unpublish(event_id)
        elif action == "author":
            return self.handle_author_menu(event_id, version, extra)
        elif action == "set_author":
            return self.handle_set_author(event_id, version, extra, parsed.get("image_version", ""))
        elif action == "site_author":
            return self.handle_site_author(event_id)
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
        
        if artifact_type == "article":
            article_data = self.version_store.get_article(event_id, version)
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

        if artifact_type == "edit":
            self.persistent_store.clear_awaiting_feedback(chat_id)
            return self.handle_edit_instruction(event_id, version, text)
        
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
