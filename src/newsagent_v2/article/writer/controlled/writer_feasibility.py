"""Pre-writer feasibility. Distinct from capacity class and final QA length."""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.enrich import evidence_sufficiency
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.controlled.capacity import EvidenceCapacity
from newsagent_v2.article.writer.controlled.plan import ArticlePlan
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers

WRITER_FEASIBLE = "WRITER_FEASIBLE"
WRITER_INFEASIBLE = "WRITER_INFEASIBLE"

# Editorial attempt target (not final QA).
EDITORIAL_TARGET_MIN_WORDS = 400
EDITORIAL_TARGET_MAX_WORDS = 550

# Hopeless RSS-only / thin discovery packets.
MIN_EXTRACTED_WORDS_FOR_ATTEMPT = 120
MIN_EVIDENCE_WORDS_FOR_ATTEMPT = 200
MIN_DISTINCT_FACTS_FOR_ATTEMPT = 6
MIN_LEDGER_CLAIMS_FOR_ATTEMPT = 6
# Advisory capacity floor — below final QA 350 is allowed.
MIN_ADVISORY_SAFE_WORDS = 200


def _evidence_word_total(article_input: dict[str, Any]) -> int:
    evidence = article_input.get("evidence") if isinstance(article_input.get("evidence"), list) else []
    parts: list[str] = []
    for row in evidence:
        if not isinstance(row, dict):
            continue
        parts.append(str(row.get("extracted_text") or ""))
        for sn in row.get("factual_snippets") or []:
            if isinstance(sn, str):
                parts.append(sn)
        parts.append(str(row.get("summary") or ""))
        parts.append(str(row.get("title") or ""))
        parts.append(str(row.get("description") or ""))
    return word_count(" ".join(parts))


def evaluate_writer_feasibility(
    *,
    article_input: dict[str, Any],
    ledgers: EvidenceLedgers,
    capacity: EvidenceCapacity,
    plan: ArticlePlan | None = None,
) -> dict[str, Any]:
    """Decide whether writing is worth attempting. Not final QA."""
    del plan
    suff = evidence_sufficiency(article_input)
    extracted = int(suff.get("extracted_evidence_words") or 0)
    distinct = int(suff.get("distinct_fact_count") or 0)
    evidence_words = _evidence_word_total(article_input)
    claims = len(ledgers.claims)
    quotes = len(ledgers.quotes)
    attributions = int(suff.get("attribution_count") or 0)
    bodies = int(suff.get("independent_extracted_source_count") or 0)
    safe_max = int(capacity.estimated_safe_word_max)
    reasons: list[str] = []

    if extracted < 80 and claims <= 2:
        reasons.append("rss_only_or_thin_packet")
    if extracted < MIN_EXTRACTED_WORDS_FOR_ATTEMPT and bodies < 1:
        reasons.append("no_usable_researched_body")
    if evidence_words < MIN_EVIDENCE_WORDS_FOR_ATTEMPT and extracted < MIN_EXTRACTED_WORDS_FOR_ATTEMPT:
        reasons.append("evidence_volume_too_low")
    if distinct < MIN_DISTINCT_FACTS_FOR_ATTEMPT and claims < MIN_LEDGER_CLAIMS_FOR_ATTEMPT:
        reasons.append("too_few_grounded_facts")
    if claims < MIN_LEDGER_CLAIMS_FOR_ATTEMPT:
        reasons.append("ledger_claim_count_too_low")
    if safe_max < MIN_ADVISORY_SAFE_WORDS:
        reasons.append("advisory_capacity_hopeless")

    feasible = not reasons
    return {
        "writer_feasible": feasible,
        "status": WRITER_FEASIBLE if feasible else WRITER_INFEASIBLE,
        "reasons": reasons,
        "metrics": {
            "evidence_words": evidence_words,
            "extracted_evidence_words": extracted,
            "distinct_fact_count": distinct,
            "claim_count": claims,
            "quote_count": quotes,
            "attribution_count": attributions,
            "researched_body_count": bodies,
            "advisory_safe_word_max": safe_max,
            "capacity_class": capacity.capacity_class,
        },
        "editorial_target_min_words": EDITORIAL_TARGET_MIN_WORDS,
        "editorial_target_max_words": EDITORIAL_TARGET_MAX_WORDS,
    }
