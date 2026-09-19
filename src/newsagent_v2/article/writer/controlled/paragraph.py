"""Paragraph validation with assertion-level quarantine. Resolver may MAP. Never invents support."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.controlled.plan import ParagraphPlan
from newsagent_v2.article.writer.controlled.realization import apply_realization_filter
from newsagent_v2.article.writer.controlled.renderer import RenderedParagraph
from newsagent_v2.article.writer.controlled.quarantine import (
    AMBIGUOUS_ASSERTION,
    DEPENDENCY_INVALID,
    INSEPARABLE_UNSUPPORTED,
    INVENTED_QUOTE,
    OUTCOME_FULLY_REJECTED,
    OUTCOME_FULLY_RETAINED,
    OUTCOME_PARTIALLY_RETAINED,
    UNAUTHORIZED_CLAIM,
    UNAUTHORIZED_QUOTE,
    UNSUPPORTED_ASSERTION,
    AssertionUnit,
    merge_telemetry,
    quarantine_paragraph,
)

__all__ = [
    "AMBIGUOUS_ASSERTION",
    "DEPENDENCY_INVALID",
    "INSEPARABLE_UNSUPPORTED",
    "INVENTED_QUOTE",
    "OUTCOME_FULLY_REJECTED",
    "OUTCOME_FULLY_RETAINED",
    "OUTCOME_PARTIALLY_RETAINED",
    "ParagraphValidation",
    "UNAUTHORIZED_CLAIM",
    "UNAUTHORIZED_QUOTE",
    "UNSUPPORTED_ASSERTION",
    "aggregate_quarantine_telemetry",
    "validate_paragraph",
]


@dataclass
class ParagraphValidation:
    paragraph_id: str
    ok: bool
    text: str
    mapped_claim_ids: list[str] = field(default_factory=list)
    mapped_quote_ids: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = ""
    generated_text: str = ""
    units: list[AssertionUnit] = field(default_factory=list)
    telemetry: dict[str, int] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return not self.ok

    @property
    def assemblable(self) -> bool:
        if self.outcome == OUTCOME_FULLY_REJECTED:
            return False
        if self.outcome in {OUTCOME_FULLY_RETAINED, OUTCOME_PARTIALLY_RETAINED}:
            return bool(str(self.text or "").strip())
        return bool(self.ok and str(self.text or "").strip())


def validate_paragraph(
    rendered: RenderedParagraph,
    plan: ParagraphPlan,
    ledgers: EvidenceLedgers,
    *,
    article_claim_ids: set[str] | None = None,
) -> ParagraphValidation:
    outcome, retained, units, issues, mapped_claims, mapped_quotes, stats = quarantine_paragraph(
        rendered, plan, ledgers, article_claim_ids=article_claim_ids
    )
    quarantined = ParagraphValidation(
        paragraph_id=plan.paragraph_id,
        ok=outcome != OUTCOME_FULLY_REJECTED and bool(retained),
        text=retained,
        mapped_claim_ids=mapped_claims,
        mapped_quote_ids=mapped_quotes,
        issues=issues,
        outcome=outcome,
        generated_text=rendered.text or "",
        units=units,
        telemetry=stats,
    )
    return apply_realization_filter(quarantined, plan, ledgers=ledgers)


def aggregate_quarantine_telemetry(rows: list[ParagraphValidation]) -> dict[str, int]:
    return merge_telemetry([row.telemetry for row in rows if row.telemetry])
