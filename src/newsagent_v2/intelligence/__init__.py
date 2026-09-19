"""News Intelligence Engine for V5 Discovery.

Deterministic intelligence on NewsEvents:
- Story Momentum
- Story Evolution
- Novelty/What Changed
- Impact Radar
- Coverage Gap
- Breaking Signal
"""

from __future__ import annotations

from .momentum import MomentumAnalyzer
from .evolution import EvolutionAnalyzer
from .novelty import NoveltyAnalyzer
from .impact import ImpactAnalyzer
from .breaking_detector import BreakingDetector
from .engine import IntelligenceEngine

__all__ = [
    "MomentumAnalyzer",
    "EvolutionAnalyzer",
    "NoveltyAnalyzer",
    "ImpactAnalyzer",
    "BreakingDetector",
    "IntelligenceEngine",
]
