"""Impact Radar - identifies likely affected areas.

Deterministic topic/entity analysis using keyword rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent, EventReport


@dataclass
class ImpactResult:
    """Impact assessment result."""
    affected_areas: list[str]
    primary_area: str | None
    confidence: float
    signals: dict[str, list[str]]


class ImpactAnalyzer:
    """Deterministic impact analysis.

    Uses keyword/entity rules to identify affected areas:
    - Bitcoin
    - Ethereum
    - Altcoins
    - Exchanges
    - Institutions
    - Regulation
    - Stablecoins
    - DeFi
    - Security
    """

    # Impact area definitions with keywords
    IMPACT_AREAS = {
        "Bitcoin": {
            "keywords": [
                "bitcoin", "btc", "btc/usd", "bitcoin etf",
                "btc etf", "bitcoin halving", "mining",
            ],
            "weight": 1.0,
        },
        "Ethereum": {
            "keywords": [
                "ethereum", "eth", "eth/usd", "ethereum etf",
                "eth etf", "ethereum upgrade", "staking",
            ],
            "weight": 1.0,
        },
        "Altcoins": {
            "keywords": [
                "altcoin", "altcoins", "solana", "sol", "cardano", "ada",
                "polygon", "pol", "avalanche", "avax", "chainlink", "link",
            ],
            "weight": 0.9,
        },
        "Exchanges": {
            "keywords": [
                "exchange", "exchanges", "binance", "coinbase", "kraken",
                "bitfinex", "gemini", "listing", "delisting",
            ],
            "weight": 0.9,
        },
        "Institutions": {
            "keywords": [
                "institutional", "blackrock", "institutional adoption",
                "corporate treasury", "wall street", "traditional finance",
            ],
            "weight": 0.85,
        },
        "Regulation": {
            "keywords": [
                "sec", "regulator", "regulatory", "regulations", "compliance",
                "court", "lawsuit", "bill", "policy", "framework", "fca",
                "cftc", "treasury",
            ],
            "weight": 1.0,
        },
        "Stablecoins": {
            "keywords": [
                "stablecoin", "stablecoins", "usdt", "usdc", "tether",
                "circle", "rlusd", "dai", "fiat-backed",
            ],
            "weight": 0.9,
        },
        "DeFi": {
            "keywords": [
                "defi", "decentralized finance", "yield", "lending",
                "borrowing", "amm", "liquidity", "protocol", "aave",
                "compound", "uniswap", "curve", "convex",
            ],
            "weight": 0.85,
        },
        "Security": {
            "keywords": [
                "hack", "hacked", "hacker", "breach", "exploit",
                "phishing", "ransom", "stolen", "drained", "vulnerability",
            ],
            "weight": 1.0,
        },
    }

    def __init__(self):
        self.stats = {"analyzed": 0}

    def _has_keyword(self, text: str, keyword: str) -> bool:
        """Check if text contains keyword with word boundaries."""
        pattern = r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"
        return re.search(pattern, text, re.IGNORECASE) is not None

    def _score_area(self, area: str, text: str) -> tuple[float, list[str]]:
        """Score an impact area.

        Returns:
            Tuple of (score, matched_keywords)
        """
        config = self.IMPACT_AREAS[area]
        matches = []

        for keyword in config["keywords"]:
            if self._has_keyword(text, keyword):
                matches.append(keyword)

        if not matches:
            return 0.0, []

        # Base score from weight
        base = config["weight"]
        # Bonus for multiple matches
        bonus = min(len(matches) * 0.1, 0.3)

        return base + bonus, matches

    def analyze(self, event: NewsEvent) -> ImpactResult:
        """Analyze impact of an event."""
        self.stats["analyzed"] += 1

        # Combine all text from event
        all_text = event.canonical_title.lower()
        for report in event.reports:
            all_text += " " + report.headline.lower()
            all_text += " " + report.description.lower()

        # Score each area
        area_scores: dict[str, float] = {}
        signals: dict[str, list[str]] = {}

        for area in self.IMPACT_AREAS:
            score, matches = self._score_area(area, all_text)
            if score > 0:
                area_scores[area] = score
                signals[area] = matches

        # Sort by score
        sorted_areas = sorted(
            area_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )

        # Get affected areas
        affected = [area for area, _ in sorted_areas]

        # Primary area is highest scoring
        primary = affected[0] if affected else None

        # Calculate overall confidence
        if area_scores:
            confidence = max(area_scores.values())
        else:
            confidence = 0.0

        return ImpactResult(
            affected_areas=affected,
            primary_area=primary,
            confidence=round(confidence, 3),
            signals=signals,
        )

    def has_impact_on(self, event: NewsEvent, area: str) -> tuple[bool, float]:
        """Quick check if event affects a specific area."""
        if area not in self.IMPACT_AREAS:
            return False, 0.0

        all_text = " ".join(r.headline for r in event.reports)
        score, _ = self._score_area(area, all_text.lower())
        return score > 0, score

    def get_stats(self) -> dict[str, Any]:
        """Get analysis statistics."""
        return dict(self.stats)
