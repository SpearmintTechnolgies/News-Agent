"""Story Evolution Analyzer.

Tracks how a story evolves over time with meaningful developments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent


@dataclass
class EvolutionSnapshot:
    """Snapshot of story state at a point in time."""
    timestamp: str
    source_count: int
    report_count: int
    development_count: int
    momentum: str
    breaking: bool


@dataclass
class EvolutionSummary:
    """Summary of story evolution."""
    first_seen: str
    latest_update: str
    total_reports: int
    total_developments: int
    developing_story: bool
    timeline: list[dict[str, Any]]
    summary: str


class EvolutionAnalyzer:
    """Analyzes story evolution over time.

    Exposes:
    - Initial report
    - Later reports
    - Meaningful developments
    - Latest change
    """

    def __init__(self):
        self.stats = {"analyzed": 0}

    def analyze(self, event: NewsEvent) -> EvolutionSummary:
        """Analyze evolution of an event."""
        self.stats["analyzed"] += 1

        # Build timeline from developments and reports
        timeline: list[dict[str, Any]] = []

        # Initial discovery
        if event.reports:
            first_report = event.reports[0]
            timeline.append({
                "type": "initial_report",
                "timestamp": event.first_seen,
                "source": first_report.source,
                "description": f"First report from {first_report.source}",
            })

        # Add developments
        for dev in event.developments:
            timeline.append({
                "type": "development",
                "timestamp": dev.timestamp,
                "source": dev.source,
                "reason": dev.reason,
                "description": dev.description,
            })

        # Sort timeline
        timeline.sort(key=lambda x: x["timestamp"])

        # Determine if developing
        developing = len(event.developments) >= 1 or event.source_count >= 2

        # Build summary
        if timeline:
            first = timeline[0]
            latest = timeline[-1]
            summary_parts = [
                f"First reported {len(timeline)} event points ago by {first.get('source', 'unknown')}",
            ]
            if event.developments:
                summary_parts.append(f"{len(event.developments)} meaningful developments")
            summary = "; ".join(summary_parts)
        else:
            summary = "No timeline data available"

        return EvolutionSummary(
            first_seen=event.first_seen,
            latest_update=event.last_seen,
            total_reports=len(event.reports),
            total_developments=len(event.developments),
            developing_story=developing,
            timeline=timeline,
            summary=summary,
        )

    def extract_key_changes(self, event: NewsEvent) -> list[dict[str, Any]]:
        """Extract the key changes/chapters in the story."""
        changes: list[dict[str, Any]] = []

        # Initial report
        if event.reports:
            first = event.reports[0]
            changes.append({
                "type": "initial",
                "source": first.source,
                "time": event.first_seen,
                "headline": first.headline[:80] + "..." if len(first.headline) > 80 else first.headline,
            })

        # Developments
        for dev in event.developments:
            changes.append({
                "type": "development",
                "source": dev.source,
                "time": dev.timestamp,
                "reason": dev.reason,
                "description": dev.description,
            })

        return changes

    def is_developing_story(self, event: NewsEvent) -> bool:
        """Quick check if event is still developing."""
        return len(event.developments) > 0 or event.source_count >= 2

    def get_latest_change(self, event: NewsEvent) -> dict[str, Any] | None:
        """Get the most recent change to the event."""
        if event.developments:
            dev = event.developments[-1]
            return {
                "type": "development",
                "timestamp": dev.timestamp,
                "source": dev.source,
                "reason": dev.reason,
            }
        if event.reports:
            report = event.reports[-1]
            return {
                "type": "new_report",
                "timestamp": report.published_at or event.last_seen,
                "source": report.source,
                "url": report.url,
            }
        return None

    def get_stats(self) -> dict[str, Any]:
        """Get analysis statistics."""
        return dict(self.stats)
