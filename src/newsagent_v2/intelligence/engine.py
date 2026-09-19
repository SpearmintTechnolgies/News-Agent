"""Intelligence Engine - orchestrates all intelligence analysis.

Combines:
- Momentum
- Evolution
- Novelty
- Impact
- Breaking Detection
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent

from .breaking_detector import BreakingDetector, BreakingResult
from .evolution import EvolutionAnalyzer, EvolutionSummary
from .impact import ImpactAnalyzer, ImpactResult
from .momentum import MomentumAnalyzer, MomentumScore
from .novelty import NoveltyAnalyzer, NoveltyResult


@dataclass
class EventIntelligence:
    """Complete intelligence for an event."""
    event_id: str
    momentum: MomentumScore
    evolution: EvolutionSummary
    novelty: NoveltyResult
    impact: ImpactResult
    breaking: BreakingResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "momentum": {
                "level": self.momentum.level,
                "score": self.momentum.score,
                "explanation": self.momentum.explanation,
            },
            "evolution": {
                "developing": self.evolution.developing_story,
                "first_seen": self.evolution.first_seen,
                "total_reports": self.evolution.total_reports,
                "total_developments": self.evolution.total_developments,
                "summary": self.evolution.summary,
            },
            "novelty": {
                "level": self.novelty.level,
                "score": self.novelty.score,
                "is_repeat": self.novelty.is_repeat,
            },
            "impact": {
                "primary_area": self.impact.primary_area,
                "affected_areas": self.impact.affected_areas,
                "confidence": self.impact.confidence,
            },
            "breaking": {
                "is_breaking": self.breaking.is_breaking,
                "score": self.breaking.score,
                "reason": self.breaking.reason,
            },
        }


class IntelligenceEngine:
    """Main intelligence engine.

    Orchestrates all analyzers and provides unified intelligence.
    ZERO paid model calls - all deterministic.
    """

    def __init__(self):
        self.momentum_analyzer = MomentumAnalyzer()
        self.evolution_analyzer = EvolutionAnalyzer()
        self.novelty_analyzer = NoveltyAnalyzer()
        self.impact_analyzer = ImpactAnalyzer()
        self.breaking_detector = BreakingDetector()

        self.stats = {"events_analyzed": 0}

    def analyze(
        self,
        event: NewsEvent,
        seen_event_ids: set[str] | None = None,
    ) -> EventIntelligence:
        """Analyze a single event."""
        self.stats["events_analyzed"] += 1

        return EventIntelligence(
            event_id=event.event_id,
            momentum=self.momentum_analyzer.analyze(event),
            evolution=self.evolution_analyzer.analyze(event),
            novelty=self.novelty_analyzer.analyze(event, seen_event_ids),
            impact=self.impact_analyzer.analyze(event),
            breaking=self.breaking_detector.analyze(event),
        )

    def analyze_batch(
        self,
        events: list[NewsEvent],
        seen_event_ids: set[str] | None = None,
    ) -> dict[str, EventIntelligence]:
        """Analyze multiple events."""
        results = {}
        for event in events:
            results[event.event_id] = self.analyze(event, seen_event_ids)
        return results

    def get_stats(self) -> dict[str, Any]:
        """Get analysis statistics."""
        return {
            **self.stats,
            "momentum": self.momentum_analyzer.get_stats(),
            "evolution": self.evolution_analyzer.get_stats(),
            "novelty": self.novelty_analyzer.get_stats(),
            "impact": self.impact_analyzer.get_stats(),
            "breaking": self.breaking_detector.get_stats(),
        }
