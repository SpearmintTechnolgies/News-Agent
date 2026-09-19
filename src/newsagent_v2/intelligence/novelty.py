"""Novelty Analyzer - determines what changed in an event.

Compares against earlier developments and previous reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent


@dataclass
class NoveltyResult:
    """Novelty assessment result."""
    level: str  # HIGH, MEDIUM, LOW
    score: float  # 0.0-1.0
    reasons: list[str]
    is_repeat: bool


class NoveltyAnalyzer:
    """Analyzes novelty of event updates.

    Compares against:
    - Earlier event developments
    - Previous reports
    - Publication history (if available)
    """

    def __init__(self):
        self.stats = {"analyzed": 0}

    def analyze(
        self,
        event: NewsEvent,
        seen_event_ids: set[str] | None = None,
    ) -> NoveltyResult:
        """Analyze novelty of current event state.

        Args:
            event: The event to analyze
            seen_event_ids: Set of previously processed event IDs
        """
        self.stats["analyzed"] += 1

        seen_event_ids = seen_event_ids or set()
        reasons: list[str] = []
        score = 0.0

        # 1. Check if event is new
        is_new = event.event_id not in seen_event_ids
        if is_new:
            reasons.append("new_event")
            score += 0.4

        # 2. Check developments count
        dev_count = len(event.developments)
        if dev_count == 0:
            # No developments = just basic coverage
            dev_score = 0.0
        elif dev_count == 1:
            dev_score = 0.2
            reasons.append("one_development")
        elif dev_count <= 3:
            dev_score = 0.35
            reasons.append("multiple_developments")
        else:
            dev_score = 0.4
            reasons.append("significant_development_activity")

        score += dev_score

        # 3. Check source growth
        source_count = event.source_count
        if source_count == 1:
            source_score = 0.0
        elif source_count == 2:
            source_score = 0.1
            reasons.append("multi_source")
        elif source_count <= 4:
            source_score = 0.15
            reasons.append("wide_sources")
        else:
            source_score = 0.2
            reasons.append("broad_coverage")

        score += source_score

        # 4. Check for primary source evidence
        if event.has_primary_evidence:
            score += 0.1
            reasons.append("primary_evidence")

        # Determine level
        if score >= 0.7:
            level = "HIGH"
        elif score >= 0.4:
            level = "MEDIUM"
        else:
            level = "LOW"

        # Check if this is just a repeat
        is_repeat = (not is_new and dev_count == 0 and source_count <= 1)

        return NoveltyResult(
            level=level,
            score=round(min(score, 1.0), 3),
            reasons=reasons,
            is_repeat=is_repeat,
        )

    def assess_update_novelty(
        self,
        event: NewsEvent,
        previous_report_count: int,
        previous_dev_count: int,
    ) -> NoveltyResult:
        """Assess novelty of an update compared to previous state."""
        self.stats["analyzed"] += 1

        reasons: list[str] = []
        score = 0.0

        # New reports since last check
        new_reports = len(event.reports) - previous_report_count
        if new_reports > 0:
            score += min(new_reports * 0.1, 0.3)
            reasons.append(f"{new_reports}_new_reports")

        # New developments
        new_devs = len(event.developments) - previous_dev_count
        if new_devs > 0:
            score += min(new_devs * 0.15, 0.45)
            reasons.append(f"{new_devs}_new_developments")

        # Determine level
        if score >= 0.5:
            level = "HIGH"
        elif score >= 0.2:
            level = "MEDIUM"
        else:
            level = "LOW"

        is_repeat = (new_reports == 0 and new_devs == 0)

        return NoveltyResult(
            level=level,
            score=round(min(score, 1.0), 3),
            reasons=reasons,
            is_repeat=is_repeat,
        )

    def get_stats(self) -> dict[str, Any]:
        """Get analysis statistics."""
        return dict(self.stats)
