"""Coverage Gap Analyzer.

Compares discovered events against CoinNetwork publication history to determine:
- CoinNetwork already covered this event
- CoinNetwork covered an older development
- This event appears meaningfully uncovered
- UNKNOWN (when history unavailable)

Deterministic analysis only - no LLM calls.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent


@dataclass
class CoverageResult:
    """Coverage gap analysis result."""
    status: str  # "COVERED", "OLDER_COVERED", "UNCOVERED", "UNKNOWN"
    confidence: float
    matched_event_id: str | None = None
    matched_article_id: str | None = None
    matched_published_at: str | None = None
    reason: str = ""
    match_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "matched_event_id": self.matched_event_id,
            "matched_article_id": self.matched_article_id,
            "matched_published_at": self.matched_published_at,
            "reason": self.reason,
            "match_score": self.match_score,
        }


class CoverageGapAnalyzer:
    """Analyzes whether an event has been covered by CoinNetwork.

    Uses local publication records if available.
    Returns UNKNOWN if no publication history exists.
    """

    def __init__(
        self,
        publication_history_path: Path | None = None,
    ):
        """Initialize with optional publication history.

        Args:
            publication_history_path: Path to JSON file with CoinNetwork articles
        """
        self.publication_history: list[dict[str, Any]] = []
        self._loaded = False

        if publication_history_path:
            self._load_history(publication_history_path)

    def _load_history(self, path: Path) -> None:
        """Load publication history from file."""
        if not path.is_file():
            self._loaded = False
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.publication_history = data
            elif isinstance(data, dict) and "articles" in data:
                self.publication_history = data["articles"]
            else:
                self.publication_history = [data]
            self._loaded = True
        except (json.JSONDecodeError, TypeError):
            self._loaded = False

    def _normalize_entities(self, entities: list[str]) -> set[str]:
        """Normalize entity names for comparison."""
        return set(e.lower().strip() for e in entities if e)

    def _headline_similarity(self, h1: str, h2: str) -> float:
        """Calculate headline similarity (0.0-1.0)."""
        # Simple token overlap
        t1 = set(re.findall(r'\w+', h1.lower()))
        t2 = set(re.findall(r'\w+', h2.lower()))

        if not t1 or not t2:
            return 0.0

        intersection = t1 & t2
        union = t1 | t2

        return len(intersection) / len(union)

    def _entity_overlap(self, e1: list[str], e2: list[str]) -> float:
        """Calculate entity overlap ratio."""
        s1 = self._normalize_entities(e1)
        s2 = self._normalize_entities(e2)

        if not s1 or not s2:
            return 0.0

        intersection = s1 & s2
        union = s1 | s2

        return len(intersection) / len(union)

    def _is_same_event(
        self,
        event: NewsEvent,
        article: dict[str, Any],
    ) -> tuple[bool, float, str]:
        """Check if event matches a published article.

        Returns:
            Tuple of (is_match, score, reason)
        """
        article_title = article.get("headline", "")
        article_entities = article.get("entities", [])

        # Headline similarity threshold
        title_sim = self._headline_similarity(event.canonical_title, article_title)

        # Entity overlap
        entity_overlap = self._entity_overlap(
            list(event.entities),
            article_entities,
        )

        # Combined score
        score = (title_sim * 0.6) + (entity_overlap * 0.4)

        if score >= 0.7:
            return True, score, "strong_match"
        elif score >= 0.5:
            return True, score, "moderate_match"
        elif score >= 0.3:
            return False, score, "weak_match"
        else:
            return False, score, "no_match"

    def _is_older_development_covered(
        self,
        event: NewsEvent,
        article: dict[str, Any],
    ) -> tuple[bool, float, str]:
        """Check if an older development of this event was covered."""
        article_title = article.get("headline", "")
        article_entities = article.get("entities", [])
        article_date_str = article.get("published_at") or article.get("created_at")

        # Entity overlap (stronger than title for older developments)
        entity_overlap = self._entity_overlap(
            list(event.entities),
            article_entities,
        )

        # Title similarity
        title_sim = self._headline_similarity(event.canonical_title, article_title)

        # Check if article is older than event
        try:
            article_date = datetime.fromisoformat(article_date_str.replace("Z", "+00:00"))

            # Parse event first_seen
            event_date_str = event.first_seen
            event_date = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))

            # Article must be older to be "older development"
            if article_date >= event_date:
                return False, 0.0, "article_newer_than_event"
        except (ValueError, TypeError):
            # Skip date comparison if parsing fails
            pass

        score = (entity_overlap * 0.7) + (title_sim * 0.3)

        # Different headline but same entities = older development
        if entity_overlap >= 0.6 and title_sim < 0.5:
            return True, score, "older_development_same_entities"

        return False, score, "no_match"

    def analyze(self, event: NewsEvent) -> CoverageResult:
        """Analyze coverage gap for an event.

        Returns:
            CoverageResult with status:
            - "COVERED" if same event was published
            - "OLDER_COVERED" if older development was published
            - "UNCOVERED" if no match found
            - "UNKNOWN" if no publication history available
        """
        if not self._loaded or not self.publication_history:
            return CoverageResult(
                status="UNKNOWN",
                confidence=0.0,
                reason="no_publication_history_available",
            )

        best_match: dict[str, Any] | None = None
        best_score: float = 0.0
        best_reason: str = ""

        # Check each published article
        for article in self.publication_history:
            if not isinstance(article, dict):
                continue

            is_match, score, reason = self._is_same_event(event, article)

            if is_match and score > best_score:
                best_match = article
                best_score = score
                best_reason = reason

        if best_match:
            return CoverageResult(
                status="COVERED",
                confidence=min(best_score, 1.0),
                matched_article_id=best_match.get("id") or best_match.get("article_id"),
                matched_published_at=best_match.get("published_at") or best_match.get("created_at"),
                reason=f"same_event: {best_reason}",
                match_score=best_score,
            )

        # Check for older developments
        best_older: dict[str, Any] | None = None
        best_older_score: float = 0.0
        best_older_reason: str = ""

        for article in self.publication_history:
            if not isinstance(article, dict):
                continue

            is_older, score, reason = self._is_older_development_covered(event, article)

            if is_older and score > best_older_score:
                best_older = article
                best_older_score = score
                best_older_reason = reason

        if best_older:
            return CoverageResult(
                status="OLDER_COVERED",
                confidence=min(best_older_score, 1.0),
                matched_article_id=best_older.get("id") or best_older.get("article_id"),
                matched_published_at=best_older.get("published_at") or best_older.get("created_at"),
                reason=f"older_development: {best_older_reason}",
                match_score=best_older_score,
            )

        # No match found
        return CoverageResult(
            status="UNCOVERED",
            confidence=0.8,  # High confidence if we have history and no match
            reason="no_matching_publication_found",
        )

    def analyze_batch(
        self,
        events: list[NewsEvent],
    ) -> dict[str, CoverageResult]:
        """Analyze coverage for multiple events."""
        return {e.event_id: self.analyze(e) for e in events}

    def get_stats(self) -> dict[str, Any]:
        """Get analyzer statistics."""
        return {
            "history_loaded": self._loaded,
            "articles_in_history": len(self.publication_history),
        }
