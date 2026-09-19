"""Deterministic grammatical proposition frames and planner-owned relationships.

No LLM. Does not copy claim.text. Does not invent CAUSE/CONSEQUENCE/MOTIVE/
PREDICTION/SIGNIFICANCE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from newsagent_v2.article.writer.controlled.semantic import SemanticFact, semantic_fact_from_claim
from newsagent_v2.article.writer.evidence_ledger import LedgerClaim

RELATIONSHIP_NONE = "NONE"
RELATIONSHIP_ATTRIBUTION = "ATTRIBUTION"
RELATIONSHIP_SEQUENCE = "SEQUENCE"
RELATIONSHIP_CONTRAST = "CONTRAST"
RELATIONSHIP_ELABORATION = "ELABORATION"

ALLOWED_RELATIONSHIPS = frozenset(
    {
        RELATIONSHIP_NONE,
        RELATIONSHIP_ATTRIBUTION,
        RELATIONSHIP_SEQUENCE,
        RELATIONSHIP_CONTRAST,
        RELATIONSHIP_ELABORATION,
    }
)

FORBIDDEN_INFERRED_RELATIONSHIPS = frozenset(
    {
        "CAUSE",
        "CONSEQUENCE",
        "MOTIVE",
        "PREDICTION",
        "SIGNIFICANCE",
    }
)


@dataclass(frozen=True)
class PropositionFrame:
    fact_id: str
    subject: str
    predicate: str
    complement: str
    attribution: str
    time: str
    location: str
    numbers: tuple[str, ...]
    qualifiers: tuple[str, ...]
    legal_or_official_names: tuple[str, ...]
    polarity: str
    modal: str
    proper_names: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "complement": self.complement,
            "attribution": self.attribution,
            "time": self.time,
            "location": self.location,
            "numbers": list(self.numbers),
            "qualifiers": list(self.qualifiers),
            "legal_or_official_names": list(self.legal_or_official_names),
            "polarity": self.polarity,
            "modal": self.modal,
            "proper_names": list(self.proper_names),
        }


def proposition_frame_from_semantic_fact(fact: SemanticFact) -> PropositionFrame:
    complement = fact.complement or fact.object
    if not complement and fact.legal_titles:
        complement = fact.legal_titles[0]
    return PropositionFrame(
        fact_id=fact.claim_id,
        subject=fact.subject,
        predicate=fact.predicate or fact.relation,
        complement=complement,
        attribution=fact.attribution,
        time=fact.time or (fact.dates[0] if fact.dates else ""),
        location=fact.location,
        numbers=fact.numbers,
        qualifiers=fact.qualifiers,
        legal_or_official_names=fact.legal_titles,
        polarity=fact.polarity or "affirmed",
        modal=fact.modal,
        proper_names=fact.proper_names,
    )


def proposition_frame_from_claim(claim: LedgerClaim) -> PropositionFrame:
    return proposition_frame_from_semantic_fact(semantic_fact_from_claim(claim))


def _np(text: str) -> str:
    return " ".join(str(text or "").split()).strip(" ,;:")


def realize_proposition_frame(frame: PropositionFrame) -> str:
    """One grammatical sentence from roles. Offline only. Adds no new proposition."""
    subject = _np(frame.subject) or (frame.proper_names[0] if frame.proper_names else "The report")
    predicate = _np(frame.predicate) or "stated"
    complement = _np(frame.complement)
    if not complement and frame.legal_or_official_names:
        complement = frame.legal_or_official_names[0]
    if not complement and frame.numbers:
        complement = "figure " + frame.numbers[0]
    if frame.polarity == "negated":
        core = f"{subject} {predicate} not {complement}".strip()
    else:
        core = f"{subject} {predicate} {complement}".strip()
    extras: list[str] = []
    if frame.location and frame.location.lower() not in core.lower():
        extras.append(f"in {frame.location}")
    if frame.time and frame.time.lower() not in core.lower():
        extras.append(f"as of {frame.time}")
    if extras:
        core = f"{core} {' '.join(extras)}"
    if frame.qualifiers:
        qtext = "; ".join(_np(item) for item in frame.qualifiers if item)
        if qtext and qtext.lower() not in core.lower():
            core = f"{core} ({qtext})"
    text = core.strip()
    if text and not text.endswith((".", "!", "?")):
        text += "."
    return text


def assign_paragraph_relationship(
    *,
    editorial_purpose: str,
    quote_ids: tuple[str, ...] | list[str],
    claims: list[LedgerClaim] | tuple[LedgerClaim, ...],
) -> str:
    """Planner-owned. Never infers CAUSE/CONSEQUENCE/MOTIVE/PREDICTION/SIGNIFICANCE."""
    if quote_ids or editorial_purpose == "attribution":
        return RELATIONSHIP_ATTRIBUTION
    if len(claims) >= 2:
        second = str(claims[1].text or "").strip().lower()
        if second.startswith("but ") or second.startswith("however ") or second.startswith("although "):
            return RELATIONSHIP_CONTRAST
    if editorial_purpose in {"key_facts", "additional_facts"}:
        return RELATIONSHIP_SEQUENCE
    if editorial_purpose in {"supported_context", "detail", "more_detail"}:
        return RELATIONSHIP_ELABORATION
    return RELATIONSHIP_NONE


def relationship_is_allowed(value: str) -> bool:
    return str(value or "").strip().upper() in ALLOWED_RELATIONSHIPS


def relationship_is_forbidden_inference(value: str) -> bool:
    return str(value or "").strip().upper() in FORBIDDEN_INFERRED_RELATIONSHIPS

