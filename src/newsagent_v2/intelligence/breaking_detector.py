"""Breaking Signal Detector.

Uses combinations of signals to detect breaking news.
NO automatic article generation - just flagging.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent


@dataclass
class BreakingResult:
    """Breaking signal assessment."""
    is_breaking: bool
    confidence: float
    score: float
    signals: list[str]
    reason: str


class BreakingDetector:
    """Deterministic breaking signal detector.

    Combines:
    - Freshness (recent publication)
    - Coverage acceleration
    - Independent confirmations
    - Official-source appearance
    - Meaningful new developments
    """

    def __init__(self):
        self.stats = {"checked": 0, "flagged": 0}

    def analyze(
        self,
        event: NewsEvent,
        now: datetime | None = None,
    ) -> BreakingResult:
        """Analyze if event has breaking signal.

        Does NOT automatically generate articles.
        """
        self.stats["checked"] += 1

        now = now or datetime.now(timezone.utc)

        signals: list[str] = []
        score = 0.0

        # 1. Freshness score (most important)
        age_hours = event.age_hours
        if age_hours is None:
            age_score = 0.0
        elif age_hours <= 1:
            age_score = 0.35
            signals.append("published_within_1h")
        elif age_hours <= 3:
            age_score = 0.25
            signals.append("published_within_3h")
        elif age_hours <= 6:
            age_score = 0.15
            signals.append("published_within_6h")
        elif age_hours <= 12:
            age_score = 0.05
            signals.append("published_within_12h")
        else:
            age_score = 0.0
        score += age_score

        # 2. Source coverage (independent confirmations)
        source_count = event.source_count
        if source_count >= 4:
            score += 0.20
            signals.append("4+ sources")
        elif source_count >= 3:
            score += 0.15
            signals.append("3 sources")
        elif source_count >= 2:
            score += 0.08
            signals.append("2 sources")

        # 3. Official/regulator source
        if event.has_primary_evidence:
            score += 0.15
            signals.append("official_source")

        # 4. Coverage velocity
        # With minimum 1-hour observation window:
        # - 1 report = 1.0 reports/hr (now just at threshold)
        # - 2 reports = 2.0 reports/hr
        # - 3+ reports = 3.0+ reports/hr
        if event.coverage_velocity >= 3.0 and event.source_count >= 2:
            score += 0.15
            signals.append("fast_coverage_3+_per_hr_multi_source")
        elif event.coverage_velocity >= 2.0 and event.source_count >= 2:
            score += 0.10
            signals.append("active_coverage_2+_per_hr_multi_source")
        elif event.coverage_velocity >= 3.0:
            score += 0.05
            signals.append("fast_coverage_3+_single_source")
        elif event.coverage_velocity >= 1.0 and event.source_count >= 3:
            score += 0.03
            signals.append("steady_coverage_multi_source")

        # 5. Development activity
        if len(event.developments) >= 2:
            score += 0.10
            signals.append("2+ developments")
        elif len(event.developments) >= 1:
            score += 0.05
            signals.append("developing_story")

        # Determine if breaking
        is_breaking = score >= 0.50

        if is_breaking:
            self.stats["flagged"] += 1

        # Calculate confidence
        if score >= 0.70:
            confidence = 0.90
        elif score >= 0.50:
            confidence = 0.75
        elif score >= 0.30:
            confidence = 0.55
        else:
            confidence = 0.30

        # Build reason
        if is_breaking:
            reason = f"Breaking: {', '.join(signals[:3])}" if signals else "Breaking signal detected"
        else:
            if not signals:
                reason = "No significant breaking indicators"
            elif age_score < 0.1:
                reason = "Older story"
            else:
                reason = "Emerging, but not breaking level"

        return BreakingResult(
            is_breaking=is_breaking,
            confidence=round(confidence, 3),
            score=round(score, 3),
            signals=signals,
            reason=reason,
        )

    def _get_top_signals(self, event: NewsEvent) -> list[str]:
        """Get top scoring signals for an event."""
        signals: list[str] = []

        age_hours = event.age_hours
        if age_hours is not None and age_hours <= 3:
            signals.append("fresh")

        if event.source_count >= 3:
            signals.append("confirmed")

        if event.has_primary_evidence:
            signals.append("official")

        if event.coverage_velocity >= 0.5:
            signals.append("accelerating")

        return signals

    def get_stats(self) -> dict[str, Any]:
        """Get detector statistics."""
        return {
            **self.stats,
            "break_rate": round(self.stats["flagged"] / max(self.stats["checked"], 1), 3),
        }
