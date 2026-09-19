"""Persistent review ratings, feedback, and approvals.

Replaces in-memory lists in ReviewSystem with JSON persistence.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_ROOT = Path("./data/v5_state")


@dataclass
class RatingRecord:
    """Artifact rating - persistent."""
    rating_id: str
    event_id: str
    job_id: str
    artifact_type: str  # "article" or "image"
    version: str
    rating: int  # 1-10
    reviewer: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "rating_id": self.rating_id,
            "event_id": self.event_id,
            "job_id": self.job_id,
            "artifact_type": self.artifact_type,
            "version": self.version,
            "rating": self.rating,
            "reviewer": self.reviewer,
            "created_at": self.created_at,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RatingRecord:
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


@dataclass
class FeedbackRecord:
    """Artifact feedback - persistent."""
    feedback_id: str
    event_id: str
    job_id: str
    artifact_type: str
    version: str
    feedback_text: str
    reviewer: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    has_rating: bool = False
    rating: int | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "event_id": self.event_id,
            "job_id": self.job_id,
            "artifact_type": self.artifact_type,
            "version": self.version,
            "feedback_text": self.feedback_text,
            "reviewer": self.reviewer,
            "created_at": self.created_at,
            "has_rating": self.has_rating,
            "rating": self.rating,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeedbackRecord:
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


@dataclass
class ApprovalRecord:
    """Artifact approval - persistent and immutable."""
    approval_id: str
    event_id: str
    job_id: str
    approved: bool
    article_version: str
    article_hash: str
    image_version: str
    image_hash: str
    approved_by: str
    approved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    reason: str | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "event_id": self.event_id,
            "job_id": self.job_id,
            "approved": self.approved,
            "article_version": self.article_version,
            "article_hash": self.article_hash,
            "image_version": self.image_version,
            "image_hash": self.image_hash,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "reason": self.reason,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRecord:
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


class PersistentReviewStore:
    """Persistent review store - ratings, feedback, approvals."""
    
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or DATA_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        
        # In-memory cache
        self._ratings: dict[str, RatingRecord] = {}
        self._feedback: dict[str, FeedbackRecord] = {}
        self._approvals: dict[str, ApprovalRecord] = {}
        
        self._load_all()
    
    def _rating_path(self, rating_id: str) -> Path:
        return self.root / f"rating_{rating_id}.json"
    
    def _feedback_path(self, feedback_id: str) -> Path:
        return self.root / f"feedback_{feedback_id}.json"
    
    def _approval_path(self, approval_id: str) -> Path:
        return self.root / f"approval_{approval_id}.json"
    
    def _load_all(self) -> None:
        """Load persisted state."""
        for f in self.root.glob("rating_*.json"):
            try:
                data = json.loads(f.read_text())
                rating = RatingRecord.from_dict(data)
                self._ratings[rating.rating_id] = rating
            except Exception:
                pass
        
        for f in self.root.glob("feedback_*.json"):
            try:
                data = json.loads(f.read_text())
                feedback = FeedbackRecord.from_dict(data)
                self._feedback[feedback.feedback_id] = feedback
            except Exception:
                pass
        
        for f in self.root.glob("approval_*.json"):
            try:
                data = json.loads(f.read_text())
                approval = ApprovalRecord.from_dict(data)
                self._approvals[approval.approval_id] = approval
            except Exception:
                pass
    
    def save_rating(self, rating: RatingRecord) -> None:
        """Persist rating."""
        with self._lock:
            self._ratings[rating.rating_id] = rating
            self._rating_path(rating.rating_id).write_text(
                json.dumps(rating.to_dict(), indent=2)
            )
    
    def get_rating(self, rating_id: str) -> RatingRecord | None:
        """Get rating by ID."""
        with self._lock:
            return self._ratings.get(rating_id)
    
    def get_ratings_for_event(self, event_id: str) -> list[RatingRecord]:
        """Get all ratings for an event."""
        with self._lock:
            return [r for r in self._ratings.values() if r.event_id == event_id]
    
    def get_average_rating(self, event_id: str, artifact_type: str) -> float | None:
        """Get average rating for artifact type."""
        with self._lock:
            ratings = [r.rating for r in self._ratings.values() 
                      if r.event_id == event_id and r.artifact_type == artifact_type]
            if not ratings:
                return None
            return round(sum(ratings) / len(ratings), 2)
    
    def get_rating_for_artifact(self, event_id: str, artifact_type: str, version: str) -> RatingRecord | None:
        """Get rating for specific artifact version."""
        with self._lock:
            for r in self._ratings.values():
                if r.event_id == event_id and r.artifact_type == artifact_type and r.version == version:
                    return r
            return None
    
    def save_feedback(self, feedback: FeedbackRecord) -> None:
        """Persist feedback."""
        with self._lock:
            self._feedback[feedback.feedback_id] = feedback
            self._feedback_path(feedback.feedback_id).write_text(
                json.dumps(feedback.to_dict(), indent=2)
            )
    
    def get_feedback_for_event(self, event_id: str) -> list[FeedbackRecord]:
        """Get all feedback for an event."""
        with self._lock:
            return [f for f in self._feedback.values() if f.event_id == event_id]
    
    def get_feedback_for_artifact(self, event_id: str, artifact_type: str, version: str) -> list[FeedbackRecord]:
        """Get feedback for specific artifact version."""
        with self._lock:
            return [
                f for f in self._feedback.values()
                if f.event_id == event_id and f.artifact_type == artifact_type and f.version == version
            ]
    
    def save_approval(self, approval: ApprovalRecord) -> None:
        """Persist approval."""
        with self._lock:
            self._approvals[approval.approval_id] = approval
            self._approval_path(approval.approval_id).write_text(
                json.dumps(approval.to_dict(), indent=2)
            )
    
    def get_approval_for_event(self, event_id: str) -> ApprovalRecord | None:
        """Get latest approval for an event."""
        with self._lock:
            approvals = [a for a in self._approvals.values() if a.event_id == event_id]
            if not approvals:
                return None
            return max(approvals, key=lambda a: a.approved_at)
    
    def has_approval(self, event_id: str) -> bool:
        """Check if event has approval."""
        with self._lock:
            return any(a.event_id == event_id for a in self._approvals.values())
    
    def get_approval_count(self) -> int:
        """Count total approvals."""
        with self._lock:
            return len(self._approvals)
