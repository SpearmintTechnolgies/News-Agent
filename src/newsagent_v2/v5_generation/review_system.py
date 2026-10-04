"""Review System - ratings, feedback, and approval/rejection.

Features:
- Independent article/image ratings (1-10)
- Article feedback binding
- Image feedback binding
- Safe state management
- Review record preservation
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent

from .version_store import VersionStore


@dataclass
class Rating:
    """Artifact rating."""
    artifact_type: str  # "article" or "image"
    version: str
    rating: int  # 1-10
    reviewer: str
    event_id: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Feedback:
    """Artifact feedback."""
    artifact_type: str  # "article" or "image"
    version: str
    feedback: str
    reviewer: str
    event_id: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    has_rating: bool = False
    rating: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Approval:
    """Artifact approval/rejection."""
    event_id: str
    approved: bool
    article_version: str
    article_hash: str
    image_version: str
    image_hash: str
    approved_version: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    approved_by: str = ""
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReviewState:
    """Temporary review state for awaiting feedback."""

    def __init__(self):
        # event_id -> {artifact_type, version, reviewer, awaiting_since}
        self._awaiting: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def await_feedback(
        self,
        event_id: str,
        artifact_type: str,
        version: str,
        reviewer: str,
    ) -> dict[str, Any]:
        """Put review in awaiting state."""
        with self._lock:
            self._awaiting[event_id] = {
                "artifact_type": artifact_type,
                "version": version,
                "reviewer": reviewer,
                "awaiting_since": datetime.now(timezone.utc).isoformat(),
            }
            return self._awaiting[event_id].copy()

    def should_capture(self, event_id: str) -> tuple[bool, dict[str, Any] | None]:
        """Check if next message should be captured as feedback."""
        with self._lock:
            if event_id in self._awaiting:
                return True, self._awaiting[event_id].copy()
            return False, None

    def clear(self, event_id: str) -> bool:
        """Clear awaiting state."""
        with self._lock:
            if event_id in self._awaiting:
                del self._awaiting[event_id]
                return True
            return False

    def get_state(self, event_id: str) -> dict[str, Any] | None:
        """Get current awaiting state."""
        with self._lock:
            return self._awaiting.get(event_id)


class ReviewSystem:
    """Complete review system for article/image artifacts."""

    def __init__(
        self,
        version_store: VersionStore,
    ):
        self.version_store = version_store
        self.review_state = ReviewState()
        self._ratings: list[Rating] = []
        self._feedback: list[Feedback] = []
        self._approvals: list[Approval] = []
        # RLock: get_stats() holds the lock and calls get_ratings/get_feedback/etc.
        self._lock = threading.RLock()

    # Ratings

    def rate_article(
        self,
        event_id: str,
        version: str,
        rating: int,
        reviewer: str,
    ) -> Rating:
        """Rate an article version."""
        if rating < 1 or rating > 10:
            raise ValueError("Rating must be 1-10")

        r = Rating(
            artifact_type="article",
            version=version,
            rating=rating,
            reviewer=reviewer,
            event_id=event_id,
        )

        with self._lock:
            self._ratings.append(r)

        # Store in version store
        self.version_store.save_review(
            event_id, "article", version, {
                "rating": rating,
                "reviewer": reviewer,
                "type": "rating",
            }
        )

        return r

    def rate_image(
        self,
        event_id: str,
        version: str,
        rating: int,
        reviewer: str,
    ) -> Rating:
        """Rate an image version."""
        if rating < 1 or rating > 10:
            raise ValueError("Rating must be 1-10")

        r = Rating(
            artifact_type="image",
            version=version,
            rating=rating,
            reviewer=reviewer,
            event_id=event_id,
        )

        with self._lock:
            self._ratings.append(r)

        # Store in version store
        self.version_store.save_review(
            event_id, "image", version, {
                "rating": rating,
                "reviewer": reviewer,
                "type": "rating",
            }
        )

        return r

    def get_ratings(self, event_id: str) -> list[Rating]:
        """Get all ratings for an event."""
        with self._lock:
            return [r for r in self._ratings if r.event_id == event_id]

    def get_average_rating(self, event_id: str, artifact_type: str) -> float | None:
        """Get average rating for artifact type."""
        with self._lock:
            ratings = [
                r.rating for r in self._ratings
                if r.event_id == event_id and r.artifact_type == artifact_type
            ]
        if not ratings:
            return None
        return round(sum(ratings) / len(ratings), 2)

    # Feedback

    def await_article_feedback(
        self,
        event_id: str,
        version: str,
        reviewer: str,
    ) -> dict[str, Any]:
        """Enter awaiting state for article feedback."""
        return self.review_state.await_feedback(
            event_id, "article", version, reviewer,
        )

    def await_image_feedback(
        self,
        event_id: str,
        version: str,
        reviewer: str,
    ) -> dict[str, Any]:
        """Enter awaiting state for image feedback."""
        return self.review_state.await_feedback(
            event_id, "image", version, reviewer,
        )

    def capture_feedback(
        self,
        event_id: str,
        text: str,
    ) -> Feedback | None:
        """Capture feedback if awaiting.

        Returns:
            Feedback object or None if not awaiting.
        """
        should_capture, state = self.review_state.should_capture(event_id)
        if not should_capture or not state:
            return None

        # Get any existing rating
        with self._lock:
            existing_rating = next(
                (r for r in self._ratings
                 if r.event_id == event_id and r.artifact_type == state["artifact_type"]),
                None
            )

        f = Feedback(
            artifact_type=state["artifact_type"],
            version=state["version"],
            feedback=text,
            reviewer=state["reviewer"],
            event_id=event_id,
            has_rating=existing_rating is not None,
            rating=existing_rating.rating if existing_rating else None,
        )

        with self._lock:
            self._feedback.append(f)

        # Store in version store
        self.version_store.save_review(
            event_id, state["artifact_type"], state["version"],
            f.to_dict()
        )

        # Clear awaiting state
        self.review_state.clear(event_id)

        return f

    def get_feedback(self, event_id: str) -> list[Feedback]:
        """Get all feedback for an event."""
        with self._lock:
            return [f for f in self._feedback if f.event_id == event_id]

    def cancel_awaiting(self, event_id: str) -> bool:
        """Cancel awaiting feedback."""
        return self.review_state.clear(event_id)

    def is_awaiting_feedback(self, event_id: str) -> bool:
        """Check if awaiting feedback."""
        state = self.review_state.get_state(event_id)
        return state is not None

    # Approval/Rejection

    def approve(
        self,
        event: NewsEvent,
        article_version: str,
        image_version: str,
        approved_by: str,
    ) -> Approval:
        """Approve story for publication."""
        # Get hashes
        article_hash = self.version_store.get_article_hash(event.event_id, article_version)
        image_hash = self.version_store.get_image_hash(event.event_id, image_version)

        if not article_hash:
            raise ValueError(f"Article version {article_version} not found")

        if not image_hash:
            raise ValueError(f"Image version {image_version} not found")

        a = Approval(
            event_id=event.event_id,
            approved=True,
            article_version=article_version,
            article_hash=article_hash,
            image_version=image_version,
            image_hash=image_hash,
            approved_by=approved_by,
        )

        with self._lock:
            self._approvals.append(a)

        # Store in version store
        self.version_store.save_publication(event.event_id, {
            "approval": a.to_dict(),
            "state": "APPROVED",
        })

        return a

    def reject(
        self,
        event: NewsEvent,
        reason: str = "",
        rejected_by: str = "",
    ) -> Approval:
        """Reject story."""
        a = Approval(
            event_id=event.event_id,
            approved=False,
            article_version="",
            article_hash="",
            image_version="",
            image_hash="",
            approved_by=rejected_by,
            reason=reason,
        )

        with self._lock:
            self._approvals.append(a)

        # Store in version store
        self.version_store.save_publication(event.event_id, {
            "approval": a.to_dict(),
            "state": "REJECTED",
            "reason": reason,
        })

        return a

    def get_approval(self, event_id: str) -> Approval | None:
        """Get approval for an event."""
        with self._lock:
            # Get latest
            for a in reversed(self._approvals):
                if a.event_id == event_id:
                    return a
            return None

    def is_approved(self, event_id: str) -> bool:
        """Check if story is approved."""
        approval = self.get_approval(event_id)
        return approval is not None and approval.approved

    def is_rejected(self, event_id: str) -> bool:
        """Check if story is rejected."""
        approval = self.get_approval(event_id)
        return approval is not None and not approval.approved

    # Statistics for editorial learning

    def get_stats(self, event_id: str | None = None) -> dict[str, Any]:
        """Get review statistics."""
        with self._lock:
            if event_id:
                ratings = self.get_ratings(event_id)
                feedback = self.get_feedback(event_id)
                approval = self.get_approval(event_id)

                return {
                    "ratings": len(ratings),
                    "feedback": len(feedback),
                    "approved": approval.approved if approval else None,
                    "article_rating": self.get_average_rating(event_id, "article"),
                    "image_rating": self.get_average_rating(event_id, "image"),
                }

            # Global stats
            return {
                "total_ratings": len(self._ratings),
                "total_feedback": len(self._feedback),
                "total_approvals": len([a for a in self._approvals if a.approved]),
                "total_rejections": len([a for a in self._approvals if not a.approved]),
                "avg_article_rating": (
                    round(sum(r.rating for r in self._ratings if r.artifact_type == "article") /
                          len([r for r in self._ratings if r.artifact_type == "article"]), 2)
                    if any(r.artifact_type == "article" for r in self._ratings) else None
                ),
                "avg_image_rating": (
                    round(sum(r.rating for r in self._ratings if r.artifact_type == "image") /
                          len([r for r in self._ratings if r.artifact_type == "image"]), 2)
                    if any(r.artifact_type == "image" for r in self._ratings) else None
                ),
            }
