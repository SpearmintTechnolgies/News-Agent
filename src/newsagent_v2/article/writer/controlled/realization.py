"""Deterministic prose realization rejection. Not a factual repair layer. No LLM."""

from __future__ import annotations

import re
from typing import Any

from newsagent_v2.article.qa.textutil import split_sentences, word_count
from newsagent_v2.article.writer.controlled.plan import ParagraphPlan
from newsagent_v2.article.writer.controlled.quarantine import (
    OUTCOME_FULLY_REJECTED,
    OUTCOME_FULLY_RETAINED,
    OUTCOME_PARTIALLY_RETAINED,
    AssertionUnit,
    _reassemble,
    telemetry_for_units,
)
from newsagent_v2.article.writer.controlled.renderer_contract import editorial_truncation_issues, implication_language_present

REALIZATION_INVALID = "REALIZATION_INVALID"

FINITE_VERB_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|am|do|does|did|have|has|had|"
    r"said|says|say|told|tells|reported|reports|announced|confirmed|"
    r"failed|fails|hashed|spent|includes|include|included|requires|"
    r"required|allow|allows|allowed|agreed|argued|noted|pushed|push|"
    r"reacted|gave|needs|need|leave|leaves|left|secure|secured|"
    r"disclosed|released|will|would|could|can|may|might|must|should|"
    r"stated|describes|described|comes|came|holding|divest|enforce|"
    r"governing|covering|involving|means|changes|changed|dragged|"
    r"elevated|focusing|won|wins|beat|beats)\b",
    re.IGNORECASE,
)
DANGLING_CONNECTIVE_START = frozenset(
    {"and", "but", "or", "which", "that", "because", "therefore", "however"}
)
INCOMPLETE_ENDINGS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "to",
        "for",
        "toward",
        "towards",
        "such",
        "as",
        "and",
        "or",
        "with",
        "by",
        "in",
        "on",
        "over",
        "after",
        "before",
        "including",
    }
)
CAUSAL_CONNECTIVE_RE = re.compile(
    r"\b(?:because|therefore|consequently|as a result|which means|thereby)\b",
    re.IGNORECASE,
)
PREDICTION_CONNECTIVE_RE = re.compile(
    r"\b(?:is likely to|are likely to|will now|is expected to|are expected to|"
    r"will inevitably|poised to|set to transform)\b",
    re.IGNORECASE,
)
DUPLICATED_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z-]{1,})\s+\1\b", re.IGNORECASE)
SUCH_AS_INCOMPLETE_RE = re.compile(r"\bsuch as(?:\s+the)?\s*$", re.IGNORECASE)
SLOT_GLUE_RE = re.compile(
    r"\b(?:uncertainty push|investment development jurisdictions)\b",
    re.IGNORECASE,
)


def sentence_realization_issues(text: str, *, relationship: str = "NONE") -> list[dict[str, str]]:
    stripped = str(text or "").strip()
    issues: list[dict[str, str]] = []
    if not stripped:
        return [{"code": REALIZATION_INVALID, "message": "empty realization"}]
    tokens = stripped.split()
    if len(tokens) < 4:
        issues.append({"code": REALIZATION_INVALID, "message": "obvious sentence fragment"})
    last = tokens[-1].lower().strip("\"'“”.,;:")
    if last in INCOMPLETE_ENDINGS:
        issues.append({"code": REALIZATION_INVALID, "message": f"dangling terminal {last!r}"})
    first = tokens[0].lower().strip("\"'")
    if stripped[:1].islower() and first in DANGLING_CONNECTIVE_START:
        issues.append({"code": REALIZATION_INVALID, "message": "dangling connective"})
    if DUPLICATED_TOKEN_RE.search(stripped):
        issues.append({"code": REALIZATION_INVALID, "message": "obvious duplicated tokens"})
    if re.search(r"\bsuch as(?:\s+the)?\s*$", stripped.rstrip(".,;:!?"), re.IGNORECASE):
        issues.append({"code": REALIZATION_INVALID, "message": "incomplete such as construction"})
    if SLOT_GLUE_RE.search(stripped):
        issues.append({"code": REALIZATION_INVALID, "message": "structurally malformed slot glue"})
    if not FINITE_VERB_RE.search(stripped):
        issues.append({"code": REALIZATION_INVALID, "message": "missing finite verb"})
    if not stripped.endswith((".", "!", "?", '"', "”")) and len(tokens) < 12:
        issues.append({"code": REALIZATION_INVALID, "message": "unfinished terminal phrase"})
    rel = str(relationship or "NONE").upper()
    if rel == "NONE" and CAUSAL_CONNECTIVE_RE.search(stripped):
        issues.append({"code": REALIZATION_INVALID, "message": "unsupported causal connective"})
    if PREDICTION_CONNECTIVE_RE.search(stripped) and "PREDICTION" not in rel:
        issues.append({"code": REALIZATION_INVALID, "message": "unsupported prediction"})
    if rel == "NONE" and implication_language_present(stripped):
        issues.append({"code": REALIZATION_INVALID, "message": "invented implication connective"})
    return issues


def editorial_realization_issues(text: str, *, field: str) -> list[dict[str, str]]:
    issues = list(editorial_truncation_issues(text, field=field))
    stripped = str(text or "").strip()
    if not stripped:
        return issues or [{"field": field, "code": REALIZATION_INVALID, "message": f"{field} empty"}]
    if field in {"headline", "dek"} and not FINITE_VERB_RE.search(stripped):
        issues.append(
            {
                "field": field,
                "code": REALIZATION_INVALID,
                "message": f"{field} is not a complete grammatical proposition",
            }
        )
    if field == "dek" and not stripped.endswith((".", "!", "?")) and word_count(stripped) < 8:
        issues.append({"field": field, "code": REALIZATION_INVALID, "message": "truncated dek"})
    last = stripped.rstrip(".,;:!?").split()[-1].lower() if stripped.split() else ""
    if last in INCOMPLETE_ENDINGS:
        issues.append({"field": field, "code": REALIZATION_INVALID, "message": f"{field} truncated"})
    return issues


def realization_is_invalid(text: str, *, relationship: str = "NONE") -> bool:
    return bool(sentence_realization_issues(text, relationship=relationship))


def editorial_is_invalid(text: str, *, field: str) -> bool:
    return bool(editorial_realization_issues(text, field=field))


def frame_realization_issues(text: str, plan: ParagraphPlan, ledgers: Any | None) -> list[dict[str, str]]:
    if ledgers is None:
        return []
    from newsagent_v2.article.writer.controlled.semantic import semantic_facts_for_ids

    issues: list[dict[str, str]] = []
    lowered = re.sub(r"\s+", " ", str(text or "").lower())
    ids = list(getattr(plan, "allowed_claim_ids", ()) or ())
    for fact in semantic_facts_for_ids(ledgers, ids):
        complement = re.sub(r"\s+", " ", str(fact.complement or fact.object or "").strip().lower())
        if len(complement.split()) >= 5 and complement and complement in lowered:
            issues.append({"code": REALIZATION_INVALID, "message": "obvious slot concatenation"})
        if fact.polarity == "negated" and fact.predicate and fact.predicate.lower() in lowered:
            if not re.search(r"\b(?:not|never|no|cannot|n't)\b", text or "", re.IGNORECASE):
                issues.append({"code": REALIZATION_INVALID, "message": "polarity inversion"})
    return issues


def apply_realization_filter(
    validation: Any,
    plan: ParagraphPlan,
    ledgers: Any | None = None,
) -> Any:
    from newsagent_v2.article.writer.controlled.paragraph import ParagraphValidation

    relationship = str(getattr(plan, "relationship", "NONE") or "NONE")
    new_units: list[AssertionUnit] = []
    extra_issues: list[dict[str, Any]] = []
    for unit in validation.units:
        if not unit.retained:
            new_units.append(unit)
            continue
        problems = sentence_realization_issues(unit.text, relationship=relationship)
        problems.extend(frame_realization_issues(unit.text, plan, ledgers))
        if not problems:
            new_units.append(unit)
            continue
        extra_issues.append(
            {
                "code": REALIZATION_INVALID,
                "message": problems[0]["message"],
                "sentence": unit.text,
            }
        )
        new_units.append(
            AssertionUnit(
                text=unit.text,
                status=REALIZATION_INVALID,
                claim_ids=unit.claim_ids,
                quote_ids=unit.quote_ids,
                issue_code=REALIZATION_INVALID,
                issue_message=problems[0]["message"],
                connective=unit.connective,
            )
        )
    retained = _reassemble(new_units)
    issues = list(validation.issues) + extra_issues
    mapped_claims: list[str] = []
    mapped_quotes: list[str] = []
    for unit in new_units:
        if not unit.retained:
            continue
        for cid in unit.claim_ids:
            if cid not in mapped_claims:
                mapped_claims.append(cid)
        for qid in unit.quote_ids:
            if qid not in mapped_quotes:
                mapped_quotes.append(qid)
    if not retained:
        outcome = OUTCOME_FULLY_REJECTED
    elif extra_issues or validation.outcome == OUTCOME_PARTIALLY_RETAINED:
        outcome = OUTCOME_PARTIALLY_RETAINED
    else:
        outcome = OUTCOME_FULLY_RETAINED
    stats = telemetry_for_units(
        generated_text=validation.generated_text,
        retained_text=retained,
        units=new_units,
        outcome=outcome,
    )
    stats["realization_invalid_units"] = sum(
        1 for unit in new_units if unit.status == REALIZATION_INVALID or unit.issue_code == REALIZATION_INVALID
    )
    return ParagraphValidation(
        paragraph_id=validation.paragraph_id,
        ok=outcome != OUTCOME_FULLY_REJECTED and bool(retained),
        text=retained,
        mapped_claim_ids=mapped_claims,
        mapped_quote_ids=mapped_quotes,
        issues=issues,
        outcome=outcome,
        generated_text=validation.generated_text,
        units=new_units,
        telemetry=stats,
    )


def classify_realized_sentence(text: str) -> str:
    """Offline forensic labels for persisted prose. Does not rewrite artifacts."""
    issues = sentence_realization_issues(text, relationship="NONE")
    if issues:
        return REALIZATION_INVALID
    stripped = str(text or "").strip()
    clunky = bool(
        re.search(
            r"\b(?:the summary of|figures:|timing:|polarity|failed, as)\b",
            stripped,
            re.IGNORECASE,
        )
    ) or (word_count(stripped) >= 28 and stripped.count(",") >= 3)
    if clunky:
        return "GRAMMATICAL_BUT_CLUNKY"
    return "GOOD"


def classify_article_sentences(*, headline: str, dek: str, body: str) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    for field, value in (("headline", headline), ("dek", dek)):
        label = REALIZATION_INVALID if editorial_is_invalid(value, field=field) else classify_realized_sentence(value)
        if label == "GOOD" and field == "headline" and word_count(value) >= 14:
            label = "GRAMMATICAL_BUT_CLUNKY"
        rows.append({"field": field, "text": value, "label": label})
    for sentence in split_sentences(body):
        rows.append({"field": "body", "text": sentence, "label": classify_realized_sentence(sentence)})
    counts = {"GOOD": 0, "GRAMMATICAL_BUT_CLUNKY": 0, REALIZATION_INVALID: 0}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    return {"units": rows, "counts": counts}
