"""V4 EvidenceCapacity + article-type selection.

Researcher owns facts. Writer owns language. Length follows evidence capacity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from newsagent_v2.article.qa.policy import ARTICLE_MODE_BRIEF, ARTICLE_MODE_NORMAL
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.v4.event_research import EventResearchResult
from newsagent_v2.article.writer.v4.factbank import FactBank

CAPACITY_RICH = "RICH"
CAPACITY_MEDIUM = "MEDIUM"
CAPACITY_LIMITED = "LIMITED"

ARTICLE_FULL = "FULL_ARTICLE"
ARTICLE_STANDARD = "STANDARD_BRIEF"
ARTICLE_LIMITED = "LIMITED_DEPTH_BRIEF"

FULL_RANGE = (250, 400)
FULL_PREFER = (280, 330)
STANDARD_RANGE = (150, 249)
LIMITED_RANGE = (120, 149)
LIMITED_RANGE_FLOOR = LIMITED_RANGE[0]
ABSOLUTE_PUBLICATION_MINIMUM = 120


@dataclass(frozen=True)
class DepthDecision:
    evidence_capacity: str
    article_type: str
    recommended_word_min: int
    recommended_word_max: int
    prefer_min: int
    prefer_max: int
    unique_propositions: int
    independent_sources: int
    primary_sources: int
    numeric_fact_count: int
    attribution_count: int
    evidence_limited: bool
    qa_article_mode: str
    reason: str
    research: dict[str, Any]

    @property
    def recommended_word_range(self) -> tuple[int, int]:
        return (self.recommended_word_min, self.recommended_word_max)

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_capacity": self.evidence_capacity,
            "article_type": self.article_type,
            "recommended_word_range": [
                self.recommended_word_min,
                self.recommended_word_max,
            ],
            "prefer_word_range": [self.prefer_min, self.prefer_max],
            "unique_propositions": self.unique_propositions,
            "independent_sources": self.independent_sources,
            "primary_sources": self.primary_sources,
            "numeric_fact_count": self.numeric_fact_count,
            "attribution_count": self.attribution_count,
            "evidence_limited": self.evidence_limited,
            "qa_article_mode": self.qa_article_mode,
            "reason": self.reason,
            "research": dict(self.research),
        }


def _richness_score(bank: FactBank, research: EventResearchResult | None) -> dict[str, int]:
    numeric = sum(1 for row in bank.propositions if row.numbers)
    attributed = sum(1 for row in bank.propositions if row.attribution)
    primary = sum(1 for row in bank.propositions if row.primary_source_support)
    entity_rich = sum(1 for row in bank.propositions if len(row.entities) >= 1)
    independent = int(getattr(research, "independent_sources", 0) or 0) if research else 0
    primary_sources = int(getattr(research, "primary_sources", 0) or 0) if research else 0
    # Informational richness: unique props + structure, not raw source word volume.
    score = (
        bank.unique_proposition_count * 3
        + numeric * 2
        + attributed * 2
        + primary * 2
        + entity_rich
        + min(independent, 4) * 2
        + min(primary_sources, 2) * 3
        + len(bank.quotes)
    )
    return {
        "score": score,
        "numeric": numeric,
        "attributed": attributed,
        "primary_props": primary,
        "independent": independent,
        "primary_sources": primary_sources,
    }


def assess_evidence_capacity(
    bank: FactBank,
    *,
    research: EventResearchResult | None = None,
) -> DepthDecision:
    """Conservative heuristic bands. Abstraction ready for later refinement."""
    stats = _richness_score(bank, research)
    n = bank.unique_proposition_count
    score = stats["score"]
    research_dict = research.as_dict() if research is not None else {}

    # RICH: enough independent factual material for a natural 250–400 article.
    if n >= 10 and score >= 40 and (stats["independent"] >= 2 or stats["primary_sources"] >= 1):
        return DepthDecision(
            evidence_capacity=CAPACITY_RICH,
            article_type=ARTICLE_FULL,
            recommended_word_min=FULL_RANGE[0],
            recommended_word_max=FULL_RANGE[1],
            prefer_min=FULL_PREFER[0],
            prefer_max=FULL_PREFER[1],
            unique_propositions=n,
            independent_sources=stats["independent"],
            primary_sources=stats["primary_sources"],
            numeric_fact_count=stats["numeric"],
            attribution_count=stats["attributed"],
            evidence_limited=False,
            qa_article_mode=ARTICLE_MODE_NORMAL,
            reason="rich_unique_coverage",
            research=research_dict,
        )

    # MEDIUM: useful story, not confidently full-length.
    if n >= 7 and score >= 22:
        return DepthDecision(
            evidence_capacity=CAPACITY_MEDIUM,
            article_type=ARTICLE_STANDARD,
            recommended_word_min=STANDARD_RANGE[0],
            recommended_word_max=STANDARD_RANGE[1],
            prefer_min=170,
            prefer_max=220,
            unique_propositions=n,
            independent_sources=stats["independent"],
            primary_sources=stats["primary_sources"],
            numeric_fact_count=stats["numeric"],
            attribution_count=stats["attributed"],
            evidence_limited=False,
            qa_article_mode=ARTICLE_MODE_BRIEF,
            reason="medium_unique_coverage",
            research=research_dict,
        )

    # LIMITED: broad research may have run; unique verified facts remain thin.
    return DepthDecision(
        evidence_capacity=CAPACITY_LIMITED,
        article_type=ARTICLE_LIMITED,
        recommended_word_min=LIMITED_RANGE[0],
        recommended_word_max=LIMITED_RANGE[1],
        prefer_min=LIMITED_RANGE[0],
        prefer_max=LIMITED_RANGE[1],
        unique_propositions=n,
        independent_sources=stats["independent"],
        primary_sources=stats["primary_sources"],
        numeric_fact_count=stats["numeric"],
        attribution_count=stats["attributed"],
        evidence_limited=True,
        qa_article_mode=ARTICLE_MODE_BRIEF,
        reason="limited_unique_coverage",
        research=research_dict,
    )


def is_writer_underproduced(body_words: int, depth: DepthDecision) -> bool:
    """
    True only when output is substantially below what EvidenceCapacity supports.

    LIMITED coherent briefs are NOT underproduction.
    RICH must not treat a 70-word stub as an acceptable limited brief.
    """
    if depth.evidence_capacity == CAPACITY_LIMITED:
        return False
    if depth.evidence_capacity == CAPACITY_MEDIUM:
        return body_words < int(depth.recommended_word_min * 0.55)
    # RICH: severe shortfall vs FULL floor (250).
    return body_words < int(depth.recommended_word_min * 0.55)


def in_recommended_band(body_words: int, depth: DepthDecision) -> bool:
    return depth.recommended_word_min <= body_words <= depth.recommended_word_max


def slightly_below_recommended(body_words: int, depth: DepthDecision) -> bool:
    if body_words >= depth.recommended_word_min:
        return False
    if depth.evidence_capacity == CAPACITY_LIMITED:
        return False
    floor = int(depth.recommended_word_min * 0.55)
    return floor <= body_words < depth.recommended_word_min


def check_v4_article_depth(
    article: dict[str, Any],
    depth: DepthDecision | None,
) -> list[dict[str, Any]]:
    """V4 publication depth gate. Absolute floor 120; type floors enforced."""
    from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, issue

    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""
    words = word_count(body)
    issues: list[dict[str, Any]] = []
    if words < ABSOLUTE_PUBLICATION_MINIMUM:
        issues.append(
            issue(
                code="below_absolute_publication_minimum",
                message=(
                    f"article has {words} words; absolute publication minimum is "
                    f"{ABSOLUTE_PUBLICATION_MINIMUM}"
                ),
                severity=SEVERITY_CRITICAL,
                module="depth",
            )
        )
        return issues
    if depth is None:
        return issues
    floor = depth.recommended_word_min
    label = depth.article_type
    if words < floor:
        issues.append(
            issue(
                code="below_article_type_minimum",
                message=(
                    f"{label} has {words} words; minimum for this EvidenceCapacity/"
                    f"article type is {floor}"
                ),
                severity=SEVERITY_CRITICAL,
                module="depth",
            )
        )
    # RICH underproduction must never pass as a limited stub.
    if depth.evidence_capacity == CAPACITY_RICH and words < FULL_RANGE[0]:
        if not any(item.get("code") == "below_article_type_minimum" for item in issues):
            issues.append(
                issue(
                    code="rich_capacity_underproduced",
                    message=(
                        f"RICH evidence produced {words} words; "
                        f"FULL_ARTICLE requires at least {FULL_RANGE[0]}"
                    ),
                    severity=SEVERITY_CRITICAL,
                    module="depth",
                )
            )
    return issues


def provider_error_is_infrastructure(error: str | None) -> bool:
    text = str(error or "").lower()
    return any(
        token in text
        for token in (
            "rate limit",
            "rate_limited",
            "429",
            "itpm",
            "otpm",
            "tokens per minute",
            "same_org_groq_failover_skipped",
            "timeout",
            "provider_error",
        )
    )
