"""Deterministic article planner. Does not create factual propositions."""

from __future__ import annotations

from difflib import SequenceMatcher
from dataclasses import dataclass
from typing import Any

from newsagent_v2.article.contract import ARTICLE_CATEGORIES
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim, LedgerQuote
from newsagent_v2.article.writer.controlled.proposition import assign_paragraph_relationship
from newsagent_v2.article.writer.controlled.semantic import semantic_fact_from_claim

CLAIMS_PER_PARAGRAPH = 3
PREFERRED_MIN_WORDS = 400
PREFERRED_MAX_WORDS = 550
MAX_SUBHEADING_WORDS = 4
SOURCE_FRAGMENT_RATIO = 0.72

_PURPOSES = (
    "lead",
    "key_facts",
    "attribution",
    "detail",
    "supported_context",
    "additional_facts",
    "more_detail",
    "closing_supported_fact",
)

_SUBHEADING_LABELS = {
    "key_facts": "proposal changes",
    "attribution": "official statements",
    "detail": "ethics provisions",
    "supported_context": "what happens next",
    "additional_facts": "additional facts",
    "more_detail": "further detail",
    "closing_supported_fact": "where things stand",
}


@dataclass(frozen=True)
class ParagraphPlan:
    paragraph_id: str
    editorial_purpose: str
    allowed_claim_ids: tuple[str, ...]
    allowed_quote_ids: tuple[str, ...]
    target_word_range: tuple[int, int]
    required_claim_ids: tuple[str, ...]
    optional_claim_ids: tuple[str, ...]
    subheading: str = ""
    critical: bool = False
    max_factual_assertions: int | None = None
    relationship: str = "NONE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "paragraph_id": self.paragraph_id,
            "editorial_purpose": self.editorial_purpose,
            "allowed_claim_ids": list(self.allowed_claim_ids),
            "allowed_quote_ids": list(self.allowed_quote_ids),
            "target_word_range": list(self.target_word_range),
            "required_claim_ids": list(self.required_claim_ids),
            "optional_claim_ids": list(self.optional_claim_ids),
            "subheading": self.subheading,
            "critical": self.critical,
            "max_factual_assertions": self.max_factual_assertions
            if self.max_factual_assertions is not None
            else len(self.allowed_claim_ids),
            "relationship": self.relationship,
        }


@dataclass(frozen=True)
class ArticlePlan:
    event_id: str
    headline_claim_ids: tuple[str, ...]
    dek_claim_ids: tuple[str, ...]
    paragraph_plans: tuple[ParagraphPlan, ...]
    subheading_plans: tuple[str, ...]
    seo_inputs: dict[str, Any]
    category: str
    selected_claim_ids: tuple[str, ...]
    selected_quote_ids: tuple[str, ...]
    headline_requirements: dict[str, str]
    dek_requirements: dict[str, str]
    planned_safe_words: int
    minimum_surviving_words: int
    paragraph_loss_tolerance: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "headline_facts": {"claim_ids": list(self.headline_claim_ids)},
            "dek_facts": {"claim_ids": list(self.dek_claim_ids)},
            "headline_requirements": dict(self.headline_requirements),
            "dek_requirements": dict(self.dek_requirements),
            "paragraph_plans": [row.as_dict() for row in self.paragraph_plans],
            "subheading_plans": list(self.subheading_plans),
            "seo_inputs": dict(self.seo_inputs),
            "category": self.category,
            "selected_claim_ids": list(self.selected_claim_ids),
            "selected_quote_ids": list(self.selected_quote_ids),
            "planned_safe_words": self.planned_safe_words,
            "minimum_surviving_words": self.minimum_surviving_words,
            "paragraph_loss_tolerance": self.paragraph_loss_tolerance,
        }


def _select_claims_for_target(
    claims: tuple[LedgerClaim, ...],
    *,
    target_max: int,
    preferred_min: int,
    preferred_max: int,
) -> list[LedgerClaim]:
    selected: list[LedgerClaim] = []
    used = 0
    cap = min(target_max, preferred_max)
    floor_target = min(preferred_min, cap)
    for claim in claims:
        words = word_count(claim.text)
        if selected and used >= floor_target and used + words > cap:
            break
        selected.append(claim)
        used += words
        if used >= cap:
            break
    return selected


def _quote_for_claims(
    claims: list[LedgerClaim],
    quotes: tuple[LedgerQuote, ...],
    used_quote_ids: set[str],
) -> list[LedgerQuote]:
    blob = " ".join(row.text for row in claims).lower()
    found: list[LedgerQuote] = []
    for quote in quotes:
        if quote.quote_id in used_quote_ids:
            continue
        if quote.text.lower() in blob:
            found.append(quote)
    return found


def _source_titles(article_input: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    for row in article_input.get("evidence") or []:
        if isinstance(row, dict) and isinstance(row.get("title"), str) and row["title"].strip():
            titles.append(row["title"].strip())
    rep = article_input.get("representative_title")
    if isinstance(rep, str) and rep.strip():
        titles.append(rep.strip())
    return titles


def is_source_fragment(text: str, *, claim_texts: list[str], titles: list[str]) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if word_count(stripped) > MAX_SUBHEADING_WORDS:
        return True
    if stripped.endswith(("...", "…")):
        return True
    lowered = stripped.lower()
    # Short lowercase editorial labels ("what happens next") are allowed.
    # Lowercase sentence fragments copied from evidence are not.
    if stripped[:1].islower() and word_count(stripped) >= 5:
        return True
    for claim in claim_texts:
        if not claim:
            continue
        if word_count(stripped) >= 4 and lowered in claim.lower():
            return True
        if word_count(stripped) >= 6 and SequenceMatcher(
            None, lowered, claim.lower()[: max(len(stripped), 40)]
        ).ratio() >= SOURCE_FRAGMENT_RATIO:
            return True
    for title in titles:
        if not title:
            continue
        if SequenceMatcher(None, lowered, title.lower()).ratio() >= SOURCE_FRAGMENT_RATIO:
            return True
        if word_count(stripped) >= 6 and lowered in title.lower():
            return True
    return False


def editorial_subheading(purpose: str, claims: list[LedgerClaim], article_input: dict[str, Any]) -> str:
    if purpose == "lead":
        return ""
    label = _SUBHEADING_LABELS.get(purpose, "")
    blob = " ".join(row.text.lower() for row in claims)
    if purpose == "detail" and "ethic" in blob:
        label = "ethics provisions"
    elif purpose == "key_facts" and "vote" in blob:
        label = "proposal changes"
    elif purpose == "attribution":
        label = "official statements"
    elif purpose == "supported_context" or purpose == "closing_supported_fact":
        label = "what happens next"
    if word_count(label) > MAX_SUBHEADING_WORDS:
        label = " ".join(label.split()[:MAX_SUBHEADING_WORDS])
    if is_source_fragment(
        label,
        claim_texts=[row.text for row in claims],
        titles=_source_titles(article_input),
    ):
        return purpose.replace("_", " ")
    return label


def _requirements_from_claim(claim: LedgerClaim | None) -> dict[str, str]:
    if claim is None:
        return {}
    fact = semantic_fact_from_claim(claim)
    req = {
        "primary_actor": fact.subject,
        "primary_action": fact.relation,
        "primary_object": fact.object,
    }
    if fact.dates:
        req["timing"] = fact.dates[0]
    if fact.numbers:
        req["numbers"] = ", ".join(fact.numbers)
    if fact.polarity:
        req["polarity"] = fact.polarity
    if fact.complement:
        req["complement"] = fact.complement
    if fact.location:
        req["location"] = fact.location
    return {key: value for key, value in req.items() if value}


def _category(article_input: dict[str, Any]) -> str:
    raw = article_input.get("category")
    if raw in ARTICLE_CATEGORIES:
        return str(raw)
    return "other"


def plan_article(
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    *,
    hard_minimum_words: int = NORMAL_ARTICLE_POLICY.hard_minimum_words,
    target_min_words: int = NORMAL_ARTICLE_POLICY.target_min_words,
    target_max_words: int = NORMAL_ARTICLE_POLICY.target_max_words,
) -> ArticlePlan:
    del target_min_words
    allowed_ids = {row.claim_id for row in ledgers.claims}
    allowed_quotes = {row.quote_id for row in ledgers.quotes}
    selected = _select_claims_for_target(
        ledgers.claims,
        target_max=target_max_words,
        preferred_min=PREFERRED_MIN_WORDS,
        preferred_max=PREFERRED_MAX_WORDS,
    )
    paragraphs: list[ParagraphPlan] = []
    used_quotes: set[str] = set()
    chunks: list[list[LedgerClaim]] = []
    current: list[LedgerClaim] = []
    for claim in selected:
        current.append(claim)
        if len(current) >= CLAIMS_PER_PARAGRAPH:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)

    for index, group in enumerate(chunks):
        purpose = _PURPOSES[index] if index < len(_PURPOSES) else "additional_facts"
        claim_ids = tuple(row.claim_id for row in group if row.claim_id in allowed_ids)
        quotes = _quote_for_claims(group, ledgers.quotes, used_quotes)
        quote_ids = tuple(row.quote_id for row in quotes if row.quote_id in allowed_quotes)
        used_quotes.update(quote_ids)
        words = sum(word_count(row.text) for row in group)
        subheading = editorial_subheading(purpose, group, article_input)
        relationship = assign_paragraph_relationship(
            editorial_purpose=purpose,
            quote_ids=quote_ids,
            claims=group,
        )
        paragraphs.append(
            ParagraphPlan(
                paragraph_id=f"p{index + 1:02d}",
                editorial_purpose=purpose,
                allowed_claim_ids=claim_ids,
                allowed_quote_ids=quote_ids,
                target_word_range=(max(1, words), words),
                required_claim_ids=claim_ids,
                optional_claim_ids=(),
                subheading=subheading,
                critical=purpose == "lead",
                max_factual_assertions=len(claim_ids),
                relationship=relationship,
            )
        )

    planned_safe = sum(max(row.target_word_range) for row in paragraphs)
    noncritical = [max(row.target_word_range) for row in paragraphs if not row.critical]
    drop = max(noncritical) if noncritical else 0
    minimum_surviving = max(0, planned_safe - drop)
    loss_tolerance = 1 if noncritical and minimum_surviving >= hard_minimum_words else 0

    headline_ids = tuple(selected[0].claim_id for _ in [0] if selected)
    dek_ids = tuple(selected[1].claim_id for _ in [0] if len(selected) > 1)
    selected_claim_ids = tuple(row.claim_id for row in selected)
    seo_inputs = {
        "headline_claim_ids": list(headline_ids),
        "dek_claim_ids": list(dek_ids),
        "keyword_claim_ids": list(selected_claim_ids[:3]),
        "slug_claim_ids": list(headline_ids),
        "do_not_copy_source_headline": True,
    }
    return ArticlePlan(
        event_id=str(ledgers.event_id or article_input.get("event_id") or ""),
        headline_claim_ids=headline_ids,
        dek_claim_ids=dek_ids,
        paragraph_plans=tuple(paragraphs),
        subheading_plans=tuple(row.subheading for row in paragraphs if row.subheading),
        seo_inputs=seo_inputs,
        category=_category(article_input),
        selected_claim_ids=selected_claim_ids,
        selected_quote_ids=tuple(sorted(used_quotes)),
        headline_requirements=_requirements_from_claim(selected[0] if selected else None),
        dek_requirements=_requirements_from_claim(selected[1] if len(selected) > 1 else None),
        planned_safe_words=planned_safe,
        minimum_surviving_words=minimum_surviving,
        paragraph_loss_tolerance=loss_tolerance,
    )
