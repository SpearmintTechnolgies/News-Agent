"""Assertion-level quarantine. Removes unsafe units. Does not rewrite or repair."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from newsagent_v2.article.qa.grounding import (
    CLAUSE_SPLIT_RE,
    _overlapping_claims,
    is_connective_sentence,
    sentence_covered_by_claims,
)
from newsagent_v2.article.qa.structure import extract_quoted_spans
from newsagent_v2.article.qa.textutil import split_sentences, word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.ledger_resolve import matching_ledger_claims, matching_ledger_quotes
from newsagent_v2.article.writer.controlled.plan import ParagraphPlan
from newsagent_v2.article.writer.controlled.renderer import RenderedParagraph

OUTCOME_FULLY_RETAINED = "FULLY_RETAINED"
OUTCOME_PARTIALLY_RETAINED = "PARTIALLY_RETAINED"
OUTCOME_FULLY_REJECTED = "FULLY_REJECTED"

STATUS_GROUNDED = "GROUNDED"
STATUS_CONNECTIVE = "CONNECTIVE"
STATUS_UNSUPPORTED = "UNSUPPORTED"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_UNAUTHORIZED = "UNAUTHORIZED"
STATUS_INVENTED_QUOTE = "INVENTED_QUOTE"
STATUS_UNAUTHORIZED_QUOTE = "UNAUTHORIZED_QUOTE"
STATUS_INSEPARABLE = "INSEPARABLE_UNSUPPORTED"
STATUS_DEPENDENCY = "DEPENDENCY_INVALID"

UNAUTHORIZED_CLAIM = "unauthorized_paragraph_claim"
UNSUPPORTED_ASSERTION = "unsupported_paragraph_assertion"
AMBIGUOUS_ASSERTION = "ambiguous_paragraph_assertion"
INVENTED_QUOTE = "invented_or_modified_quote"
UNAUTHORIZED_QUOTE = "unauthorized_paragraph_quote"
DEPENDENCY_INVALID = "dependency_invalid_after_quarantine"
INSEPARABLE_UNSUPPORTED = "inseparable_unsupported_assertion"

# Anaphora that cannot stand if their antecedent was removed. No LLM.
_ANAPHORA_RE = re.compile(
    r"^(?:however|nevertheless|still|meanwhile)[,.]?\s+"
    r"(?:this|that|these|those|it|they|the latter|the former)\b"
    r"|^(?:this|that)\s+(?:proposal|change|bill|measure|restriction|amendment|"
    r"rule|text|offer|provision|decision)"
    r"|^(?:these|those)\s+(?:measures|restrictions|provisions|changes|rules|"
    r"bans|penalties)"
    r"|^(?:the latter|the former|this followed|that followed|it also|they also)\b",
    re.IGNORECASE,
)

_QUOTE_STATUS = {STATUS_INVENTED_QUOTE, STATUS_UNAUTHORIZED_QUOTE}


@dataclass
class AssertionUnit:
    text: str
    status: str
    claim_ids: tuple[str, ...] = ()
    quote_ids: tuple[str, ...] = ()
    issue_code: str | None = None
    issue_message: str | None = None
    connective: bool = False

    @property
    def retained(self) -> bool:
        return self.status in {STATUS_GROUNDED, STATUS_CONNECTIVE}

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "claim_ids": list(self.claim_ids),
            "quote_ids": list(self.quote_ids),
            "issue_code": self.issue_code,
            "retained": self.retained,
        }


def _issue(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload = {"code": code, "message": message}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def _safe_clauses(sentence: str) -> list[str] | None:
    """Return independently testable clauses, or None if they cannot be separated safely."""
    parts = [part.strip() for part in CLAUSE_SPLIT_RE.split(sentence) if part.strip()]
    if len(parts) < 2:
        return None
    if any(word_count(part) < 6 for part in parts):
        return None
    return parts


def _anaphoric(text: str) -> bool:
    return bool(_ANAPHORA_RE.match(text.strip()))


def _quote_status(
    sentence: str,
    *,
    allowed_quotes: set[str],
    ledgers: EvidenceLedgers,
) -> tuple[str | None, tuple[str, ...], str | None, str | None]:
    quote_ids: list[str] = []
    for span in extract_quoted_spans(sentence):
        quotes = matching_ledger_quotes(span, ledgers)
        if not quotes:
            return (
                STATUS_INVENTED_QUOTE,
                (),
                INVENTED_QUOTE,
                "quoted span is not an exact QuoteLedger quote",
            )
        authorized = [row for row in quotes if row.quote_id in allowed_quotes]
        if not authorized:
            return (
                STATUS_UNAUTHORIZED_QUOTE,
                tuple(row.quote_id for row in quotes),
                UNAUTHORIZED_QUOTE,
                "exact quote is not authorized for this paragraph",
            )
        for row in authorized:
            if row.quote_id not in quote_ids:
                quote_ids.append(row.quote_id)
    return None, tuple(quote_ids), None, None


def _classify_assertive(
    sentence: str,
    *,
    allowed_claims: set[str],
    ledgers: EvidenceLedgers,
) -> tuple[str, tuple[str, ...], str | None, str | None]:
    clauses = _safe_clauses(sentence)
    if clauses is not None:
        clause_ids: list[str] = []
        for clause in clauses:
            matches = matching_ledger_claims(clause, ledgers)
            if not matches:
                texts = ledgers.claim_texts()
                if _overlapping_claims(clause, texts) and not sentence_covered_by_claims(clause, texts):
                    return (
                        STATUS_INSEPARABLE,
                        (),
                        AMBIGUOUS_ASSERTION,
                        "inseparable multi-clause sentence contains an ambiguous clause",
                    )
                return (
                    STATUS_INSEPARABLE,
                    (),
                    INSEPARABLE_UNSUPPORTED,
                    "inseparable multi-clause sentence contains an unsupported assertion",
                )
            authorized = [row for row in matches if row.claim_id in allowed_claims]
            if not authorized:
                return (
                    STATUS_INSEPARABLE,
                    tuple(row.claim_id for row in matches),
                    UNAUTHORIZED_CLAIM,
                    "inseparable multi-clause sentence contains an unauthorized assertion",
                )
            for row in authorized:
                if row.claim_id not in clause_ids:
                    clause_ids.append(row.claim_id)
        return STATUS_GROUNDED, tuple(clause_ids), None, None

    matches = matching_ledger_claims(sentence, ledgers)
    if matches:
        authorized = [row for row in matches if row.claim_id in allowed_claims]
        if authorized:
            return STATUS_GROUNDED, tuple(row.claim_id for row in authorized), None, None
        return (
            STATUS_UNAUTHORIZED,
            tuple(row.claim_id for row in matches),
            UNAUTHORIZED_CLAIM,
            "sentence maps to a ledger claim that this paragraph is not authorized to use",
        )
    texts = ledgers.claim_texts()
    if _overlapping_claims(sentence, texts) and not sentence_covered_by_claims(sentence, texts):
        return (
            STATUS_AMBIGUOUS,
            (),
            AMBIGUOUS_ASSERTION,
            "assertive sentence only partially overlaps ledger claims",
        )
    return (
        STATUS_UNSUPPORTED,
        (),
        UNSUPPORTED_ASSERTION,
        "assertive sentence is not mapped to an EvidenceClaimLedger claim",
    )


def _text_without_quotes(sentence: str) -> str:
    stripped = sentence
    for span in extract_quoted_spans(sentence):
        stripped = stripped.replace(f'"{span}"', " ").replace(f"“{span}”", " ")
    return re.sub(r"\s+", " ", stripped).strip(" ,;:")


def classify_sentence(
    sentence: str,
    plan: ParagraphPlan,
    ledgers: EvidenceLedgers,
    *,
    article_claim_ids: set[str] | None = None,
) -> AssertionUnit:
    locality = str(getattr(plan, "relationship", "") or "") in {"ATTRIBUTION", "CONTRAST"}
    if locality or not article_claim_ids:
        allowed_claims = set(plan.allowed_claim_ids)
    else:
        allowed_claims = set(article_claim_ids)
    allowed_quotes = set(plan.allowed_quote_ids)
    q_status, quote_ids, q_code, q_msg = _quote_status(
        sentence, allowed_quotes=allowed_quotes, ledgers=ledgers
    )
    if q_status is not None:
        return AssertionUnit(
            text=sentence,
            status=q_status,
            quote_ids=quote_ids,
            issue_code=q_code,
            issue_message=q_msg,
        )
    remainder = _text_without_quotes(sentence)
    if quote_ids and (not remainder or is_connective_sentence(remainder)):
        return AssertionUnit(
            text=sentence,
            status=STATUS_GROUNDED,
            quote_ids=quote_ids,
        )
    if is_connective_sentence(sentence):
        return AssertionUnit(
            text=sentence,
            status=STATUS_CONNECTIVE,
            quote_ids=quote_ids,
            connective=True,
        )
    status, claim_ids, code, message = _classify_assertive(
        remainder or sentence, allowed_claims=allowed_claims, ledgers=ledgers
    )
    return AssertionUnit(
        text=sentence,
        status=status,
        claim_ids=claim_ids,
        quote_ids=quote_ids,
        issue_code=code,
        issue_message=message,
    )


def apply_dependency_quarantine(units: list[AssertionUnit]) -> list[AssertionUnit]:
    """Drop anaphoric survivors whose antecedent was quarantined. No bridging prose."""
    out: list[AssertionUnit] = []
    seen_grounded = False
    for unit in units:
        if unit.status not in {STATUS_GROUNDED, STATUS_CONNECTIVE}:
            out.append(unit)
            continue
        if _anaphoric(unit.text) and not seen_grounded:
            out.append(
                AssertionUnit(
                    text=unit.text,
                    status=STATUS_DEPENDENCY,
                    claim_ids=unit.claim_ids,
                    quote_ids=unit.quote_ids,
                    issue_code=DEPENDENCY_INVALID,
                    issue_message="unit depends on quarantined antecedent",
                    connective=unit.connective,
                )
            )
            continue
        if unit.status == STATUS_GROUNDED:
            seen_grounded = True
        out.append(unit)
    if not any(item.status == STATUS_GROUNDED for item in out):
        repaired: list[AssertionUnit] = []
        for unit in out:
            if unit.status == STATUS_CONNECTIVE:
                repaired.append(
                    AssertionUnit(
                        text=unit.text,
                        status=STATUS_DEPENDENCY,
                        issue_code=DEPENDENCY_INVALID,
                        issue_message="connective unit has no surviving grounded assertion",
                        connective=True,
                    )
                )
            else:
                repaired.append(unit)
        return repaired
    return out


def _reassemble(units: list[AssertionUnit]) -> str:
    return " ".join(unit.text for unit in units if unit.retained).strip()


def empty_telemetry() -> dict[str, int]:
    return {
        "generated_paragraphs": 0,
        "generated_assertions": 0,
        "grounded_assertions_retained": 0,
        "unsupported_assertions_quarantined": 0,
        "ambiguous_assertions_quarantined": 0,
        "unauthorized_assertions_quarantined": 0,
        "quote_units_quarantined": 0,
        "dependency_invalidated_units": 0,
        "generated_words": 0,
        "retained_words": 0,
        "quarantined_words": 0,
        "paragraphs_fully_retained": 0,
        "paragraphs_partially_retained": 0,
        "paragraphs_fully_rejected": 0,
        "words_saved_by_assertion_level_quarantine": 0,
        "realization_invalid_units": 0,
    }


def telemetry_for_units(
    *,
    generated_text: str,
    retained_text: str,
    units: list[AssertionUnit],
    outcome: str,
) -> dict[str, int]:
    generated_words = word_count(generated_text)
    retained_words = word_count(retained_text)
    assertions = [row for row in units if not row.connective or row.status == STATUS_DEPENDENCY]
    grounded = sum(1 for row in units if row.status == STATUS_GROUNDED)
    data = empty_telemetry()
    data["generated_paragraphs"] = 1
    data["generated_assertions"] = sum(1 for row in units if not row.connective)
    data["grounded_assertions_retained"] = grounded
    data["unsupported_assertions_quarantined"] = sum(
        1
        for row in units
        if row.status == STATUS_UNSUPPORTED
        or (row.status == STATUS_INSEPARABLE and row.issue_code == INSEPARABLE_UNSUPPORTED)
    )
    data["ambiguous_assertions_quarantined"] = sum(
        1 for row in units if row.status == STATUS_AMBIGUOUS or row.issue_code == AMBIGUOUS_ASSERTION
    )
    data["unauthorized_assertions_quarantined"] = sum(
        1
        for row in units
        if row.status == STATUS_UNAUTHORIZED
        or (row.status == STATUS_INSEPARABLE and row.issue_code == UNAUTHORIZED_CLAIM)
    )
    data["quote_units_quarantined"] = sum(1 for row in units if row.status in _QUOTE_STATUS)
    data["dependency_invalidated_units"] = sum(1 for row in units if row.status == STATUS_DEPENDENCY)
    data["generated_words"] = generated_words
    data["retained_words"] = retained_words
    data["quarantined_words"] = max(0, generated_words - retained_words)
    if outcome == OUTCOME_FULLY_RETAINED:
        data["paragraphs_fully_retained"] = 1
    elif outcome == OUTCOME_PARTIALLY_RETAINED:
        data["paragraphs_partially_retained"] = 1
        data["words_saved_by_assertion_level_quarantine"] = retained_words
    else:
        data["paragraphs_fully_rejected"] = 1
    del assertions
    return data


def merge_telemetry(rows: list[dict[str, int]]) -> dict[str, int]:
    merged = empty_telemetry()
    for row in rows:
        for key in merged:
            merged[key] += int(row.get(key) or 0)
    return merged


def quarantine_paragraph(
    rendered: RenderedParagraph,
    plan: ParagraphPlan,
    ledgers: EvidenceLedgers,
    *,
    article_claim_ids: set[str] | None = None,
) -> tuple[str, str, list[AssertionUnit], list[dict[str, Any]], list[str], list[str], dict[str, int]]:
    generated = rendered.text or ""
    raw_units = [
        classify_sentence(sentence, plan, ledgers, article_claim_ids=article_claim_ids)
        for sentence in split_sentences(generated)
    ]
    units = apply_dependency_quarantine(raw_units)
    retained = _reassemble(units)
    issues: list[dict[str, Any]] = []
    mapped_claims: list[str] = []
    mapped_quotes: list[str] = []
    for unit in units:
        if unit.retained:
            for cid in unit.claim_ids:
                if cid not in mapped_claims:
                    mapped_claims.append(cid)
            for qid in unit.quote_ids:
                if qid not in mapped_quotes:
                    mapped_quotes.append(qid)
            continue
        issues.append(
            _issue(
                unit.issue_code or UNSUPPORTED_ASSERTION,
                unit.issue_message or "unit quarantined",
                sentence=unit.text,
                claim_ids=list(unit.claim_ids) or None,
                quote_ids=list(unit.quote_ids) or None,
            )
        )
    if not retained:
        outcome = OUTCOME_FULLY_REJECTED
    elif issues:
        outcome = OUTCOME_PARTIALLY_RETAINED
    else:
        outcome = OUTCOME_FULLY_RETAINED
    stats = telemetry_for_units(
        generated_text=generated,
        retained_text=retained,
        units=units,
        outcome=outcome,
    )
    return outcome, retained, units, issues, mapped_claims, mapped_quotes, stats


def paragraph_plan_from_dict(row: dict[str, Any]) -> ParagraphPlan:
    return ParagraphPlan(
        paragraph_id=str(row.get("paragraph_id") or ""),
        editorial_purpose=str(row.get("editorial_purpose") or ""),
        allowed_claim_ids=tuple(str(item) for item in (row.get("allowed_claim_ids") or ())),
        allowed_quote_ids=tuple(str(item) for item in (row.get("allowed_quote_ids") or ())),
        target_word_range=tuple(row.get("target_word_range") or (0, 0)),  # type: ignore[arg-type]
        required_claim_ids=tuple(str(item) for item in (row.get("required_claim_ids") or ())),
        optional_claim_ids=tuple(str(item) for item in (row.get("optional_claim_ids") or ())),
        subheading=str(row.get("subheading") or ""),
        critical=bool(row.get("critical")),
        max_factual_assertions=row.get("max_factual_assertions"),
        relationship=str(row.get("relationship") or "NONE"),
    )


def replay_persisted_v3_generation(
    run_dir: Path,
    *,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    assemble_fn: Any | None = None,
    plan: Any | None = None,
) -> dict[str, Any]:
    """Offline replay of a persisted V3 native generation. Does not write artifacts or call models."""
    from newsagent_v2.article.writer.controlled.paragraph import ParagraphValidation, validate_paragraph
    from newsagent_v2.article.writer.controlled.plan import plan_article
    from newsagent_v2.article.writer.controlled.assembler import assemble_canonical_article
    from newsagent_v2.article.qa import run_article_qa

    dest = Path(run_dir)
    native = json.loads((dest / "native.json").read_text(encoding="utf-8"))
    plan_raw = json.loads((dest / "article_plan.json").read_text(encoding="utf-8"))
    old_article = json.loads((dest / "article.json").read_text(encoding="utf-8"))
    plans = {row["paragraph_id"]: paragraph_plan_from_dict(row) for row in plan_raw.get("paragraph_plans") or []}
    validations: list[ParagraphValidation] = []
    paragraph_reports: dict[str, Any] = {}
    for row in native.get("paragraphs") or []:
        pid = str(row.get("paragraph_id") or "")
        para_plan = plans.get(pid)
        if para_plan is None:
            continue
        rendered = RenderedParagraph(paragraph_id=pid, text=str(row.get("text") or ""), subheading=para_plan.subheading)
        validated = validate_paragraph(
            rendered,
            para_plan,
            ledgers,
            article_claim_ids=set(str(x) for x in (plan_raw.get("selected_claim_ids") or [])),
        )
        validations.append(validated)
        units = validated.units
        paragraph_reports[pid] = {
            "original_words": word_count(str(row.get("text") or "")),
            "assertions": sum(1 for unit in units if not unit.connective),
            "grounded_retained": sum(1 for unit in units if unit.status == STATUS_GROUNDED),
            "unsupported_quarantined": sum(
                1
                for unit in units
                if unit.status in {STATUS_UNSUPPORTED, STATUS_INSEPARABLE}
                and unit.issue_code != AMBIGUOUS_ASSERTION
            ),
            "ambiguous_quarantined": sum(
                1 for unit in units if unit.status == STATUS_AMBIGUOUS or unit.issue_code == AMBIGUOUS_ASSERTION
            ),
            "dependency_invalidated": sum(1 for unit in units if unit.status == STATUS_DEPENDENCY),
            "retained_words": word_count(validated.text),
            "quarantined_words": max(0, word_count(str(row.get("text") or "")) - word_count(validated.text)),
            "outcome": validated.outcome,
            "words_saved_vs_old_v3": validated.telemetry.get("words_saved_by_assertion_level_quarantine", 0),
            "retained_text": validated.text,
            "units": [unit.as_dict() for unit in units],
        }
    article_plan = plan or plan_article(ledgers, article_input)
    assemble = assemble_fn or assemble_canonical_article
    article = assemble(
        plan=article_plan,
        ledgers=ledgers,
        article_input=article_input,
        validated=validations,
        headline_text=str(native.get("headline") or ""),
        dek_text=str(native.get("dek") or ""),
        seo_title=str(native.get("seo_title") or ""),
        meta_description=str(native.get("meta_description") or ""),
        slug=str(native.get("slug") or ""),
        entities=native.get("entities") if isinstance(native.get("entities"), list) else None,
        keywords=native.get("keywords") if isinstance(native.get("keywords"), list) else None,
    )
    qa = run_article_qa(article, article_input, article_mode="normal")
    old_words = word_count(str(old_article.get("article_body") or ""))
    new_words = word_count(str(article.get("article_body") or ""))
    return {
        "label": "V3.2 offline replay of V3 generation — not an autonomous winner",
        "paragraphs": paragraph_reports,
        "p01": paragraph_reports.get("p01") or {},
        "old_assembled_word_count": old_words,
        "replay_assembled_word_count": new_words,
        "replay_meets_hard_minimum": new_words >= 350,
        "qa_passed": bool(qa.get("qa_passed")),
        "qa_publishable": bool(qa.get("publishable")),
        "qa_critical_codes": [item.get("code") for item in (qa.get("critical_failures") or [])],
        "telemetry": merge_telemetry([row.telemetry for row in validations]),
        "historical_artifacts_modified": False,
    }
