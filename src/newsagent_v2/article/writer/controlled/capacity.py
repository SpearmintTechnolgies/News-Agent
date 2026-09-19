"""Evidence capacity before prose. Does not inflate word count or weaken QA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from newsagent_v2.article.expand import story_evidence_units
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import NUMBER_TOKEN_RE, word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.controlled.config import (
    CAPACITY_SAFETY_MARGIN_WORDS,
    LEDGER_EVIDENCE_WORD_BUDGET,
)
from newsagent_v2.article.prompt_evidence import compact_story_evidence
from newsagent_v2.article.enrich import ATTRIBUTION_RE
from newsagent_v2.article.qa.grounding import MIN_ASSERTIVE_WORDS
from newsagent_v2.article.writer.controlled.plan import CLAIMS_PER_PARAGRAPH

STYLISTIC_OVERHEAD_WORDS = 0
CONNECTIVE_WORDS_PER_PARAGRAPH = MIN_ASSERTIVE_WORDS - 1

CLASS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
CLASS_BORDERLINE = "BORDERLINE_CAPACITY"
CLASS_SUFFICIENT = "SUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class EvidenceCapacity:
    claim_count: int
    quote_count: int
    numeric_fact_count: int
    attribution_count: int
    source_count: int
    estimated_safe_word_min: int
    estimated_safe_word_max: int
    sufficient_for_article: bool
    capacity_class: str
    min_planned_safe_words: int
    safety_margin_words: int
    paragraph_loss_tolerance: int
    minimum_surviving_words: int | None
    reason: str

    @property
    def estimated_safe_word_range(self) -> tuple[int, int]:
        return (self.estimated_safe_word_min, self.estimated_safe_word_max)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_count": self.claim_count,
            "quote_count": self.quote_count,
            "numeric_fact_count": self.numeric_fact_count,
            "attribution_count": self.attribution_count,
            "source_count": self.source_count,
            "estimated_safe_word_range": [self.estimated_safe_word_min, self.estimated_safe_word_max],
            "sufficient_for_article": self.sufficient_for_article,
            "capacity_class": self.capacity_class,
            "min_planned_safe_words": self.min_planned_safe_words,
            "safety_margin_words": self.safety_margin_words,
            "paragraph_loss_tolerance": self.paragraph_loss_tolerance,
            "minimum_surviving_words": self.minimum_surviving_words,
            "reason": self.reason,
        }


def article_input_for_ledgers(article_input: dict[str, Any]) -> dict[str, Any]:
    """Use frozen pack text for ledgers. Does not fetch or invent evidence."""
    pack = dict(article_input)
    units = pack.get("evidence_units")
    if isinstance(units, list) and units:
        return pack
    compact = compact_story_evidence(
        {"article_input": pack, "event_id": pack.get("event_id")},
        max_words=LEDGER_EVIDENCE_WORD_BUDGET,
    )
    pack["evidence_units"] = compact.get("evidence_units") or []
    return pack


def _classify(
    *,
    safe_max: int,
    hard_minimum_words: int,
    margin: int,
    paragraph_loss_tolerance: int,
) -> tuple[str, str]:
    min_planned = hard_minimum_words + margin
    if safe_max < hard_minimum_words:
        return CLASS_INSUFFICIENT, f"safe_words={safe_max}_below_hard_minimum={hard_minimum_words}"
    if safe_max < min_planned or paragraph_loss_tolerance < 1:
        return (
            CLASS_BORDERLINE,
            f"safe_words={safe_max}_below_margin_floor={min_planned}_or_zero_loss_tolerance",
        )
    return CLASS_SUFFICIENT, "ledger_meets_hard_minimum_plus_margin"


def analyze_evidence_capacity(
    ledgers: EvidenceLedgers,
    *,
    hard_minimum_words: int = NORMAL_ARTICLE_POLICY.hard_minimum_words,
    safety_margin_words: int = CAPACITY_SAFETY_MARGIN_WORDS,
    paragraph_loss_tolerance: int = 0,
    minimum_surviving_words: int | None = None,
) -> EvidenceCapacity:
    claim_words = 0
    numeric = 0
    attribution = 0
    sources: set[str] = set()
    for claim in ledgers.claims:
        claim_words += word_count(claim.text)
        if NUMBER_TOKEN_RE.search(claim.text):
            numeric += 1
        if ATTRIBUTION_RE.search(claim.text):
            attribution += 1
        sources.update(claim.evidence_ids)
    for quote in ledgers.quotes:
        sources.add(quote.evidence_id)
        attribution += 1
    planned_paragraphs = max(1, (len(ledgers.claims) + CLAIMS_PER_PARAGRAPH - 1) // CLAIMS_PER_PARAGRAPH)
    connective_budget = CONNECTIVE_WORDS_PER_PARAGRAPH * planned_paragraphs
    safe_min = claim_words
    safe_max = claim_words + STYLISTIC_OVERHEAD_WORDS + connective_budget
    capacity_class, reason = _classify(
        safe_max=safe_max,
        hard_minimum_words=hard_minimum_words,
        margin=safety_margin_words,
        paragraph_loss_tolerance=paragraph_loss_tolerance if minimum_surviving_words is not None else 1,
    )
    # Until a plan exists, do not treat unknown loss tolerance as borderline by itself.
    if minimum_surviving_words is None and safe_max >= hard_minimum_words:
        if safe_max < hard_minimum_words + safety_margin_words:
            capacity_class, reason = _classify(
                safe_max=safe_max,
                hard_minimum_words=hard_minimum_words,
                margin=safety_margin_words,
                paragraph_loss_tolerance=1,
            )
        else:
            capacity_class, reason = CLASS_SUFFICIENT, "ledger_meets_hard_minimum_plus_margin"
    sufficient = safe_max >= hard_minimum_words and len(ledgers.claims) > 0
    if not ledgers.claims:
        capacity_class, reason = CLASS_INSUFFICIENT, "no_ledger_claims"
        sufficient = False
    return EvidenceCapacity(
        claim_count=len(ledgers.claims),
        quote_count=len(ledgers.quotes),
        numeric_fact_count=numeric,
        attribution_count=attribution,
        source_count=len(sources),
        estimated_safe_word_min=safe_min,
        estimated_safe_word_max=safe_max,
        sufficient_for_article=sufficient,
        capacity_class=capacity_class,
        min_planned_safe_words=hard_minimum_words + safety_margin_words,
        safety_margin_words=safety_margin_words,
        paragraph_loss_tolerance=paragraph_loss_tolerance,
        minimum_surviving_words=minimum_surviving_words,
        reason=reason,
    )


def refine_capacity_with_plan(
    capacity: EvidenceCapacity,
    *,
    planned_safe_words: int,
    minimum_surviving_words: int,
    paragraph_loss_tolerance: int,
    hard_minimum_words: int = NORMAL_ARTICLE_POLICY.hard_minimum_words,
) -> EvidenceCapacity:
    """Fold paragraph-loss risk into capacity class. Does not invent evidence."""
    safe_max = max(capacity.estimated_safe_word_max, planned_safe_words)
    capacity_class, reason = _classify(
        safe_max=safe_max,
        hard_minimum_words=hard_minimum_words,
        margin=capacity.safety_margin_words,
        paragraph_loss_tolerance=paragraph_loss_tolerance,
    )
    sufficient = capacity.sufficient_for_article
    if capacity_class == CLASS_INSUFFICIENT:
        sufficient = False
    return EvidenceCapacity(
        claim_count=capacity.claim_count,
        quote_count=capacity.quote_count,
        numeric_fact_count=capacity.numeric_fact_count,
        attribution_count=capacity.attribution_count,
        source_count=capacity.source_count,
        estimated_safe_word_min=capacity.estimated_safe_word_min,
        estimated_safe_word_max=capacity.estimated_safe_word_max,
        sufficient_for_article=sufficient,
        capacity_class=capacity_class,
        min_planned_safe_words=capacity.min_planned_safe_words,
        safety_margin_words=capacity.safety_margin_words,
        paragraph_loss_tolerance=paragraph_loss_tolerance,
        minimum_surviving_words=minimum_surviving_words,
        reason=reason,
    )


def frozen_unit_count(article_input: dict[str, Any]) -> int:
    return len(story_evidence_units(article_input_for_ledgers(article_input)))
