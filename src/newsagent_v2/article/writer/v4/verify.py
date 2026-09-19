"""V4 post-generation assertion verification via atomic propositions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from newsagent_v2.article.qa.structure import extract_quoted_spans
from newsagent_v2.article.qa.textutil import number_tokens, split_sentences, word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.ledger_resolve import matching_ledger_quotes
from newsagent_v2.article.writer.v4.atomic_grounding import (
    STATUS_AMBIGUOUS as ATOMIC_AMBIGUOUS,
)
from newsagent_v2.article.writer.v4.atomic_grounding import (
    STATUS_CONNECTIVE as ATOMIC_CONNECTIVE,
)
from newsagent_v2.article.writer.v4.atomic_grounding import (
    STATUS_SUPPORTED as ATOMIC_SUPPORTED,
)
from newsagent_v2.article.writer.v4.atomic_grounding import (
    STATUS_UNSUPPORTED as ATOMIC_UNSUPPORTED,
)
from newsagent_v2.article.writer.v4.atomic_grounding import (
    build_authorized_proposition_set,
    verify_atomic_article,
)
from newsagent_v2.article.writer.v4.packet import WriterEvidencePacket
from newsagent_v2.article.writer.v4.writer import V4NativeArticle

STATUS_SUPPORTED = "SUPPORTED"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_UNSUPPORTED = "UNSUPPORTED"
STATUS_CONNECTIVE = "CONNECTIVE"
STATUS_QUOTE_OK = "AUTHORIZED_QUOTE"
STATUS_QUOTE_BAD = "FABRICATED_QUOTE"


@dataclass
class AssertionRow:
    text: str
    status: str
    claim_ids: tuple[str, ...] = ()
    quote_ids: tuple[str, ...] = ()
    issue_code: str | None = None
    issue_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "claim_ids": list(self.claim_ids),
            "quote_ids": list(self.quote_ids),
            "issue_code": self.issue_code,
            "issue_message": self.issue_message,
        }


@dataclass
class VerificationReport:
    rows: list[AssertionRow] = field(default_factory=list)
    supported: int = 0
    ambiguous: int = 0
    unsupported: int = 0
    connective: int = 0
    number_issues: list[dict[str, str]] = field(default_factory=list)
    quote_issues: list[dict[str, str]] = field(default_factory=list)
    ok: bool = False
    total_factual_propositions: int = 0
    supported_factual_propositions: int = 0
    ambiguous_factual_propositions: int = 0
    unsupported_factual_propositions: int = 0
    proposition_coverage: float = 1.0
    atomic: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "ambiguous": self.ambiguous,
            "unsupported": self.unsupported,
            "connective": self.connective,
            "ok": self.ok,
            "number_issues": list(self.number_issues),
            "quote_issues": list(self.quote_issues),
            "total_factual_propositions": self.total_factual_propositions,
            "supported_factual_propositions": self.supported_factual_propositions,
            "ambiguous_factual_propositions": self.ambiguous_factual_propositions,
            "unsupported_factual_propositions": self.unsupported_factual_propositions,
            "proposition_coverage": self.proposition_coverage,
            "atomic": dict(self.atomic),
            "assertions": [row.as_dict() for row in self.rows],
        }


def _authorized_numbers(packet: WriterEvidencePacket, ledgers: EvidenceLedgers) -> set[str]:
    found: set[str] = set()
    for fact in packet.authorized_facts:
        found |= {token.lower() for token in fact.numbers}
        found |= {token.lower() for token in number_tokens(fact.proposition)}
    for claim in ledgers.claims:
        found |= {token.lower() for token in number_tokens(claim.text)}
    return found


def _check_numbers(text: str, allowed: set[str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for token in number_tokens(text):
        if token.lower() not in allowed:
            digits = re.sub(r"[^\d]", "", token)
            if digits and any(digits in item for item in allowed):
                continue
            issues.append(
                {
                    "code": "unsupported_number",
                    "message": f"number {token!r} is not authorized",
                    "token": token,
                    "text": text[:240],
                }
            )
    return issues


def verify_v4_native(
    native: V4NativeArticle,
    *,
    packet: WriterEvidencePacket,
    ledgers: EvidenceLedgers,
) -> VerificationReport:
    """Classify assertive content via atomic propositions against authorized union."""
    authorized = build_authorized_proposition_set(ledgers=ledgers, packet=packet)
    atomic = verify_atomic_article(
        headline=native.headline,
        dek=native.dek,
        article_body=native.article_body,
        authorized=authorized,
    )
    allowed_numbers = _authorized_numbers(packet, ledgers)
    rows: list[AssertionRow] = []
    number_issues: list[dict[str, str]] = []
    quote_issues: list[dict[str, str]] = []

    # Quote checks remain sentence-scoped.
    for blob in (native.headline, native.dek, native.article_body):
        for sentence in split_sentences(blob or ""):
            quote_ids: list[str] = []
            bad_quote = False
            for span in extract_quoted_spans(sentence):
                matches = matching_ledger_quotes(span, ledgers)
                if not matches:
                    bad_quote = True
                    quote_issues.append(
                        {
                            "code": "fabricated_quote",
                            "message": "quoted span is not an authorized QuoteLedger quote",
                            "text": sentence[:240],
                        }
                    )
                else:
                    quote_ids.extend(row.quote_id for row in matches)
            number_issues.extend(_check_numbers(sentence, allowed_numbers))
            if bad_quote:
                rows.append(
                    AssertionRow(
                        text=sentence,
                        status=STATUS_QUOTE_BAD,
                        issue_code="fabricated_quote",
                        issue_message="fabricated or modified quote",
                    )
                )
                continue
            if quote_ids and word_count(sentence) <= 14:
                rows.append(
                    AssertionRow(
                        text=sentence,
                        status=STATUS_QUOTE_OK,
                        quote_ids=tuple(dict.fromkeys(quote_ids)),
                    )
                )

    # Primary grounding rows from atomic engine (skip duplicates already quote-failed).
    quote_bad = {row.text for row in rows if row.status == STATUS_QUOTE_BAD}
    for grounded in atomic.sentences:
        if grounded.text in quote_bad:
            continue
        # Already recorded short quote-ok rows.
        if any(row.text == grounded.text for row in rows):
            continue
        status_map = {
            ATOMIC_SUPPORTED: STATUS_SUPPORTED,
            ATOMIC_AMBIGUOUS: STATUS_AMBIGUOUS,
            ATOMIC_UNSUPPORTED: STATUS_UNSUPPORTED,
            ATOMIC_CONNECTIVE: STATUS_CONNECTIVE,
        }
        rows.append(
            AssertionRow(
                text=grounded.text,
                status=status_map.get(grounded.status, STATUS_UNSUPPORTED),
                claim_ids=grounded.claim_ids,
                issue_code=grounded.issue_code,
                issue_message=grounded.issue_message,
            )
        )

    supported = sum(1 for row in rows if row.status in {STATUS_SUPPORTED, STATUS_QUOTE_OK})
    ambiguous = sum(1 for row in rows if row.status == STATUS_AMBIGUOUS)
    unsupported = sum(
        1 for row in rows if row.status in {STATUS_UNSUPPORTED, STATUS_QUOTE_BAD}
    )
    connective = sum(1 for row in rows if row.status == STATUS_CONNECTIVE)
    ok = (
        atomic.ok
        and unsupported == 0
        and ambiguous == 0
        and not number_issues
        and not quote_issues
        and atomic.unsupported_factual_propositions == 0
        and atomic.ambiguous_factual_propositions == 0
    )
    return VerificationReport(
        rows=rows,
        supported=supported,
        ambiguous=ambiguous,
        unsupported=unsupported,
        connective=connective,
        number_issues=number_issues,
        quote_issues=quote_issues,
        ok=ok,
        total_factual_propositions=atomic.total_factual_propositions,
        supported_factual_propositions=atomic.supported_factual_propositions,
        ambiguous_factual_propositions=atomic.ambiguous_factual_propositions,
        unsupported_factual_propositions=atomic.unsupported_factual_propositions,
        proposition_coverage=atomic.proposition_coverage,
        atomic=atomic.as_dict(),
    )


def verification_failure_codes(report: VerificationReport) -> list[str]:
    codes: list[str] = []
    if report.unsupported or report.unsupported_factual_propositions:
        codes.append("unsupported_assertion")
    if report.ambiguous or report.ambiguous_factual_propositions:
        codes.append("ambiguous_assertion")
    if report.number_issues:
        codes.append("unsupported_number")
    if report.quote_issues:
        codes.append("fabricated_quote")
    return codes
