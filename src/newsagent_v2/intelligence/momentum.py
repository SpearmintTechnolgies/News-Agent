"""Story Momentum Analyzer.

Deterministic momentum scoring using measurable signals.
NO arbitrary GPT importance score.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent


@dataclass
class MomentumScore:
    """Momentum classification."""
    level: str  # LOW, STABLE, RISING, HIGH
    score: float
    factors: dict[str, float]
    explanation: str


class MomentumAnalyzer:
    """Analyzes story momentum using deterministic signals.

    Signals:
    - Source count
    - Independent-source growth
    - Coverage velocity (reports per hour)
    - Time since first report
    - Official-source appearance
    - New developments
    """

    def __init__(self):
        self.stats = {"analyzed": 0}

    def analyze(self, event: NewsEvent, now: datetime | None = None) -> MomentumScore:
        """Analyze momentum of an event."""
        self.stats["analyzed"] += 1

        now = now or datetime.now(timezone.utc)
        factors: dict[str, float] = {}

        # 1. Source count (more sources = higher momentum)
        source_count = event.source_count
        source_score = min(source_count / 4.0, 1.5)  # Max 1.5 at 6+ sources
        factors["source_count"] = round(source_score, 3)

        # 2. Independent-source growth (primary sources bonus)
        primary_count = len(event.primary_sources)
        primary_score = primary_count * 0.3  # 0.3 per primary source
        factors["primary_source_bonus"] = round(primary_score, 3)

        # 3. Coverage velocity
        velocity = event.coverage_velocity
        # Velocity scoring: <0.1=0.2, 0.1-0.5=0.5, 0.5-1.0=0.8, >1.0=1.0
        if velocity < 0.1:
            velocity_score = 0.2
        elif velocity < 0.5:
            velocity_score = 0.5
        elif velocity < 1.0:
            velocity_score = 0.8
        else:
            velocity_score = 1.0
        factors["coverage_velocity"] = round(velocity_score, 3)

        # 4. Time decay (older = lower momentum, but not zero)
        age_hours = event.age_hours
        if age_hours < 2:
            time_score = 1.0
        elif age_hours < 6:
            time_score = 0.9
        elif age_hours < 12:
            time_score = 0.75
        elif age_hours < 24:
            time_score = 0.6
        else:
            time_score = 0.4
        factors["time_freshness"] = round(time_score, 3)

        # 5. Development count
        dev_count = len(event.developments)
        dev_score = min(dev_count * 0.15, 0.6)  # Max 0.6 for 4+ developments
        factors["development_activity"] = round(dev_score, 3)

        # 6. Official source bonus
        has_official = event.has_primary_evidence
        official_score = 0.3 if has_official else 0.0
        factors["official_source"] = round(official_score, 3)

        # Calculate total score
        total_score = sum(factors.values())

        # Determine level
        if total_score >= 3.0:
            level = "HIGH"
        elif total_score >= 2.0:
            level = "RISING"
        elif total_score >= 1.0:
            level = "STABLE"
        else:
            level = "LOW"

        # Build explanation
        parts = []
        if source_count > 1:
            parts.append(f"{source_count} sources")
        if has_official:
            parts.append("official source")
        if dev_count > 0:
            parts.append(f"{dev_count} developments")
        if velocity > 0.5:
            parts.append(f"fast coverage ({velocity:.1f} reports/hr)")

        explanation = ", ".join(parts) if parts else "basic coverage"

        return MomentumScore(
            level=level,
            score=round(total_score, 3),
            factors=factors,
            explanation=explanation,
        )

    def analyze_batch(self, events: list[NewsEvent]) -> dict[str, MomentumScore]:
        """Analyze momentum for multiple events."""
        return {e.event_id: self.analyze(e) for e in events}

    def get_stats(self) -> dict[str, Any]:
        """Get analysis statistics."""
        return dict(self.stats)
