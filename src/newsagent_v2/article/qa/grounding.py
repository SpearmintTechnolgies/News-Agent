"""
Deterministic body/claim grounding checks.

Does not perform semantic entailment. Conservative string/token matching
only flags body assertions that cannot be confidently mapped to claims,
plus high-risk absence formulations and generic contextual rhetoric.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from newsagent_v2.article.input import evidence_index
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, issue
from newsagent_v2.article.qa.textutil import number_tokens, split_sentences, word_count, words
from newsagent_v2.article.writer.grounding_resolve import safe_quote_coverage_texts

COVER_SEQ_HIGH = 0.72
COVER_SEQ_AND_CONTAINMENT = 0.50
COVER_CONTAINMENT_HIGH = 0.70
COVER_CONTAINMENT_WITH_SEQ = 0.40
MIN_ASSERTIVE_WORDS = 8
MIN_CONTENT_TOKENS = 3

STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "from",
        "with",
        "after",
        "says",
        "said",
        "as",
        "by",
        "at",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "its",
        "their",
        "this",
        "that",
        "which",
        "who",
        "whom",
        "has",
        "have",
        "had",
        "not",
        "no",
        "into",
        "than",
        "then",
        "also",
        "over",
        "about",
        "via",
        "per",
    }
)

CONNECTIVE_RE = re.compile(
    r"^(meanwhile|separately|however|still|also|in addition|"
    r"the company also said|the firm also said)[,.]?$",
    re.IGNORECASE,
)

# Morphological families: one concept, several surface forms.
ABSENCE_FAMILIES: tuple[tuple[str, str], ...] = (
    (
        "disclose",
        r"\b(?:did not|does not|has not|have not|had not|was not|were not|"
        r"is not|are not)\s+disclos",
    ),
    (
        "announce",
        r"\b(?:did not|does not|has not|have not|had not|was not|were not|"
        r"is not|are not)\s+announc",
    ),
    (
        "confirm",
        r"\b(?:did not|does not|has not|have not|had not|was not|were not|"
        r"is not|are not)\s+confirm|\bhas yet to confirm\b",
    ),
    ("no_details_provided", r"\bno details were provided\b"),
    ("no_evidence_provided", r"\bno evidence was provided\b"),
    ("no_users_affected", r"\bno users were affected\b"),
    ("no_funds_lost", r"\bno funds were lost\b"),
    ("no_timeline_given", r"\bno timeline was given\b"),
    ("unknown", r"\bremains unknown\b|\bis unknown\b|\bare unknown\b"),
    ("declined_to_comment", r"\bdeclined to comment\b"),
    ("has_not_happened", r"\bhas not happened\b|\bdid not happen\b"),
    ("was_absent", r"\bwas absent\b|\bwere absent\b"),
    ("unavailable", r"\bwas unavailable\b|\bwere unavailable\b"),
    ("no_firm_motive", r"\bno firm motive\b"),
)

CONTEXTUAL_RHETORIC_RE = re.compile(
    r"\b("
    r"underscores(?: broader)?|"
    r"highlights growing|"
    r"reflects broader|"
    r"comes amid(?: increasing)?|"
    r"illustrates wider|"
    r"signals a broader|"
    r"shows the growing|"
    r"ongoing (?:security )?challenges|"
    r"raises wider concerns|"
    r"marks another example of|"
    r"broader industry|"
    r"across the (?:fintech|crypto|industry)|"
    r"market[- ]wide|"
    r"raises (?:fresh )?questions|"
    r"growing (?:security )?concerns|"
    r"it remains to be seen"
    r")\b",
    re.IGNORECASE,
)

CONTEXTUAL_CLAIM_TYPES = frozenset({"contextual", "analysis"})
MIN_CONTAINMENT_CHARS = 24
CLAUSE_SPLIT_RE = re.compile(
    r",\s+(?:but|while|whereas)\s+|;\s+",
    re.IGNORECASE,
)
UNION_CONTAINMENT_MIN = 0.40
UNION_MIN_CLAIMS = 2


def content_tokens(text: str) -> list[str]:
    return [
        token
        for token in words(text)
        if token not in STOPWORDS and len(token) > 2
    ]


def normalize_for_containment(text: str) -> str:
    lowered = text.lower()
    compact = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", compact).strip()


def coverage_scores(sentence: str, claim_text: str) -> tuple[float, float]:
    seq = SequenceMatcher(None, sentence.lower().strip(), claim_text.lower().strip()).ratio()
    sent_toks = set(content_tokens(sentence))
    claim_toks = set(content_tokens(claim_text))
    if not sent_toks:
        return seq, 0.0
    containment = len(sent_toks & claim_toks) / len(sent_toks)
    return seq, containment


def sentence_matches_claim(sentence: str, claim_text: str) -> bool:
    norm_sentence = normalize_for_containment(sentence)
    norm_claim = normalize_for_containment(claim_text)
    if (
        len(norm_sentence) >= MIN_CONTAINMENT_CHARS
        and len(norm_claim) >= MIN_CONTAINMENT_CHARS
        and (norm_sentence in norm_claim or norm_claim in norm_sentence)
    ):
        return True
    seq, containment = coverage_scores(sentence, claim_text)
    if seq >= COVER_SEQ_HIGH:
        return True
    if containment >= COVER_CONTAINMENT_HIGH and len(content_tokens(sentence)) >= MIN_CONTENT_TOKENS:
        return True
    if seq >= COVER_SEQ_AND_CONTAINMENT and containment >= COVER_CONTAINMENT_WITH_SEQ:
        return True
    return False


def _overlapping_claims(sentence: str, claim_texts: list[str]) -> list[str]:
    sent_toks = set(content_tokens(sentence))
    sent_nums = number_tokens(sentence)
    selected: list[str] = []
    for text in claim_texts:
        claim_toks = set(content_tokens(text))
        claim_nums = number_tokens(text)
        if (sent_nums and claim_nums & sent_nums) or len(sent_toks & claim_toks) >= 3:
            selected.append(text)
    return selected


def _union_covers_sentence(sentence: str, claim_texts: list[str]) -> bool:
    selected = _overlapping_claims(sentence, claim_texts)
    if len(selected) < UNION_MIN_CLAIMS:
        return False
    sent_nums = number_tokens(sentence)
    if not sent_nums:
        return False
    union_toks: set[str] = set()
    union_nums: set[str] = set()
    for text in selected:
        union_toks |= set(content_tokens(text))
        union_nums |= number_tokens(text)
    if not sent_nums <= union_nums:
        return False
    sent_toks = set(content_tokens(sentence))
    if not sent_toks:
        return False
    containment = len(sent_toks & union_toks) / len(sent_toks)
    joined = " ".join(selected)
    seq, _ = coverage_scores(sentence, joined)
    return containment >= UNION_CONTAINMENT_MIN or seq >= COVER_SEQ_AND_CONTAINMENT


def _clause_supported_by_claims(clause: str, claim_texts: list[str]) -> bool:
    if any(sentence_matches_claim(clause, text) for text in claim_texts):
        return True
    clause_nums = number_tokens(clause)
    clause_toks = set(content_tokens(clause))
    if not clause_nums:
        return False
    for text in claim_texts:
        if clause_nums <= number_tokens(text) and len(clause_toks & set(content_tokens(text))) >= 2:
            return True
    return False


def sentence_covered_by_claims(sentence: str, claim_texts: list[str]) -> bool:
    if any(sentence_matches_claim(sentence, text) for text in claim_texts):
        return True
    clauses = [part.strip() for part in CLAUSE_SPLIT_RE.split(sentence) if part.strip()]
    clauses = [part for part in clauses if word_count(part) >= 6]
    if len(clauses) >= 2:
        return all(_clause_supported_by_claims(clause, claim_texts) for clause in clauses)
    return _union_covers_sentence(sentence, claim_texts)


def is_connective_sentence(sentence: str) -> bool:
    text = sentence.strip()
    if word_count(text) < MIN_ASSERTIVE_WORDS:
        return True
    if CONNECTIVE_RE.match(text):
        return True
    return False


def claim_texts(article: dict[str, Any]) -> list[dict[str, Any]]:
    claims = article.get("claims") if isinstance(article.get("claims"), list) else []
    out: list[dict[str, Any]] = []
    for claim in claims:
        if isinstance(claim, dict) and isinstance(claim.get("text"), str):
            out.append(claim)
    return out


def best_claim_for_sentence(
    sentence: str,
    claims: list[dict[str, Any]],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_rank = (-1.0, -1.0)
    for claim in claims:
        text = claim.get("text")
        if not isinstance(text, str):
            continue
        seq, containment = coverage_scores(sentence, text)
        rank = (seq, containment)
        if sentence_matches_claim(sentence, text) and rank > best_rank:
            best = claim
            best_rank = rank
    return best


def _evidence_text_for_refs(
    refs: Any,
    allowed: dict[str, dict[str, Any]],
) -> str:
    blobs: list[str] = []
    if not isinstance(refs, list):
        return ""
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        url = ref.get("url")
        row = allowed.get(url) if isinstance(url, str) else None
        if not isinstance(row, dict):
            continue
        for key in ("title", "summary", "extracted_text"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                blobs.append(value)
    return " ".join(blobs)


def _family_hits(text: str) -> list[str]:
    found: list[str] = []
    if not isinstance(text, str):
        return found
    for family, pattern in ABSENCE_FAMILIES:
        if re.search(pattern, text, flags=re.IGNORECASE):
            found.append(family)
    return found



def is_faq_question_or_structural_heading(text: str) -> bool:
    """FAQ questions and closing headings are structural, not factual assertions.

    FAQ *answers* must still be grounded; only questions/headings are exempt.
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    # Collapse embedded newlines from markdown blocks treated as one sentence.
    compact = re.sub(r"\s+", " ", raw).strip()
    if re.match(
        r"^(?:#{1,3}\s*)?(?:\*\*)?(?:conclusion(?:\s*/\s*what happens next)?|what happens next|faqs?|frequently asked questions)\b",
        compact,
        flags=re.IGNORECASE,
    ):
        return True
    if re.match(r"^(?:\*\*)?Q\s*:", compact, flags=re.IGNORECASE):
        return True
    # Bold question-only lines: **What ...?**
    if re.match(r"^\*\*[^*]+\?\*\*$", compact):
        return True
    return False


def check_body_claim_coverage(
    article: dict[str, Any],
    article_input: dict[str, Any] | None = None,
    *,
    other_article_inputs: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    # V4: proposition-level grounding is the truth criterion.
    if (
        str(article.get("architecture") or "") == "v4"
        or str(article.get("generation_notes") or "") == "v4_natural_prose"
        or isinstance(article.get("authorized_propositions"), dict)
    ):
        return _check_body_atomic_coverage(article)

    issues: list[dict[str, str]] = []
    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""
    sentences = split_sentences(body)
    claims = claim_texts(article)
    texts = [str(claim.get("text")) for claim in claims if isinstance(claim.get("text"), str)]
    texts.extend(
        safe_quote_coverage_texts(
            article,
            article_input,
            other_article_inputs=other_article_inputs,
        )
    )
    uncovered: list[str] = []
    assertive = 0
    covered = 0
    for sentence in sentences:
        if is_connective_sentence(sentence):
            continue
        if is_faq_question_or_structural_heading(sentence):
            continue
        assertive += 1
        if sentence_covered_by_claims(sentence, texts):
            covered += 1
        else:
            uncovered.append(sentence)
            issues.append(
                issue(
                    code="body_assertion_not_in_claims",
                    message="assertive body sentence is not represented in claims",
                    severity=SEVERITY_CRITICAL,
                    module="grounding",
                    sentence=sentence,
                )
            )

    coverage = (covered / assertive) if assertive else 1.0
    metrics = {
        "body_sentence_count": len(sentences),
        "assertive_sentence_count": assertive,
        "claim_covered_sentence_count": covered,
        "uncovered_assertive_sentence_count": len(uncovered),
        "uncovered_assertive_sentences": uncovered,
        "body_claim_coverage": round(coverage, 4),
    }
    return issues, metrics


def _check_body_atomic_coverage(
    article: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """V4 grounding: unsupported/ambiguous factual propositions are critical."""
    from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
    from newsagent_v2.article.writer.v4.atomic_grounding import (
        build_authorized_proposition_set,
        verify_atomic_article,
    )

    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""
    headline = str(article.get("headline") or "")
    dek = str(article.get("dek") or "")
    claim_rows = article.get("claims") if isinstance(article.get("claims"), list) else []
    ledger_claims: list[LedgerClaim] = []
    for row in claim_rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("claim_id") or row.get("id") or "").strip()
        text = str(row.get("text") or "").strip()
        if not cid or not text:
            continue
        evidence_ids = row.get("evidence_ids") if isinstance(row.get("evidence_ids"), list) else []
        ledger_claims.append(
            LedgerClaim(
                claim_id=cid,
                text=text,
                claim_type=str(row.get("claim_type") or "fact"),
                evidence_ids=tuple(str(x) for x in evidence_ids),
            )
        )
    ledgers = EvidenceLedgers(
        event_id=str(article.get("event_id") or ""),
        claims=tuple(ledger_claims),
        quotes=(),
    )
    authorized = build_authorized_proposition_set(ledgers=ledgers)
    atomic = verify_atomic_article(
        headline=headline,
        dek=dek,
        article_body=body,
        authorized=authorized,
    )
    body_set = set(split_sentences(body))
    issues: list[dict[str, str]] = []
    uncovered: list[str] = []
    body_assertive = 0
    body_covered = 0
    for sentence in atomic.sentences:
        if sentence.text not in body_set:
            continue
        if sentence.status == "CONNECTIVE":
            continue
        if is_faq_question_or_structural_heading(sentence.text):
            continue
        body_assertive += 1
        if sentence.status == "SUPPORTED":
            body_covered += 1
            continue
        uncovered.append(sentence.text)
        code = (
            "unsupported_inference"
            if (sentence.issue_code or "") == "unsupported_inference"
            else "body_assertion_not_in_claims"
        )
        issues.append(
            issue(
                code=code,
                message=sentence.issue_message
                or "assertive body proposition is not supported by authorized facts",
                severity=SEVERITY_CRITICAL,
                module="grounding",
                sentence=sentence.text,
            )
        )
    prop_ok = (
        atomic.unsupported_factual_propositions == 0
        and atomic.ambiguous_factual_propositions == 0
    )
    coverage = atomic.proposition_coverage if atomic.total_factual_propositions else 1.0
    metrics = {
        "body_sentence_count": len(split_sentences(body)),
        "assertive_sentence_count": body_assertive,
        "claim_covered_sentence_count": body_covered,
        "uncovered_assertive_sentence_count": len(uncovered),
        "uncovered_assertive_sentences": uncovered,
        "body_claim_coverage": round(
            (body_covered / body_assertive) if body_assertive else 1.0, 4
        ),
        "total_factual_propositions": atomic.total_factual_propositions,
        "supported_factual_propositions": atomic.supported_factual_propositions,
        "ambiguous_factual_propositions": atomic.ambiguous_factual_propositions,
        "unsupported_factual_propositions": atomic.unsupported_factual_propositions,
        "proposition_coverage": coverage,
        "proposition_grounding_ok": prop_ok,
    }
    if prop_ok:
        return [], metrics
    return issues, metrics


def _ref_urls(refs: Any) -> list[str]:
    urls: list[str] = []
    if not isinstance(refs, list):
        return urls
    for ref in refs:
        if isinstance(ref, dict) and isinstance(ref.get("url"), str):
            urls.append(ref["url"])
    return urls


def _absence_issue(
    *,
    message: str,
    sentence: str,
    families: list[str],
    claim: dict[str, Any] | None = None,
    evidence_urls: list[str] | None = None,
    evidence_families: list[str] | None = None,
) -> dict[str, Any]:
    return issue(
        code="unsupported_absence_claim",
        message=message,
        severity=SEVERITY_CRITICAL,
        module="grounding",
        sentence=sentence,
        absence_families=families,
        claim_id=claim.get("claim_id") if isinstance(claim, dict) else None,
        claim_text=claim.get("text") if isinstance(claim, dict) else None,
        evidence_urls=evidence_urls or [],
        evidence_support_families=evidence_families or [],
    )


def check_absence_claims(
    article: dict[str, Any],
    article_input: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""
    sentences = split_sentences(body)
    claims = claim_texts(article)
    allowed = evidence_index(article_input)

    for sentence in sentences:
        families = _family_hits(sentence)
        if not families:
            continue
        matched_claim = None
        for claim in claims:
            text = claim.get("text")
            if not isinstance(text, str):
                continue
            if not sentence_matches_claim(sentence, text) and not (
                set(_family_hits(text)) & set(families)
                and coverage_scores(sentence, text)[1] >= 0.35
            ):
                continue
            matched_claim = claim
            break
        if matched_claim is None:
            issues.append(
                _absence_issue(
                    message=(
                        "absence/negative formulation in article_body is not "
                        "represented in structured claims"
                    ),
                    sentence=sentence,
                    families=families,
                )
            )
            continue
        refs = matched_claim.get("evidence_refs")
        urls = _ref_urls(refs)
        if not isinstance(refs, list) or not refs:
            issues.append(
                _absence_issue(
                    message="absence/negative claim has no evidence_refs",
                    sentence=sentence,
                    families=families,
                    claim=matched_claim,
                    evidence_urls=urls,
                )
            )
            continue
        evidence_text = _evidence_text_for_refs(refs, allowed)
        claim_families = _family_hits(str(matched_claim.get("text") or ""))
        needed = set(families) | set(claim_families)
        supported = set(_family_hits(evidence_text))
        if not (needed & supported):
            issues.append(
                _absence_issue(
                    message=(
                        "absence/negative formulation is not explicitly present "
                        "in referenced evidence text; missing evidence is not "
                        "evidence of absence"
                    ),
                    sentence=sentence,
                    families=families,
                    claim=matched_claim,
                    evidence_urls=urls,
                    evidence_families=sorted(supported),
                )
            )
    return issues


def check_contextual_assertions(
    article: dict[str, Any],
    article_input: dict[str, Any],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""
    sentences = split_sentences(body)
    claims = claim_texts(article)
    allowed = evidence_index(article_input)

    for sentence in sentences:
        if not CONTEXTUAL_RHETORIC_RE.search(sentence):
            continue
        if is_connective_sentence(sentence):
            continue
        matched = None
        for claim in claims:
            text = claim.get("text")
            if not isinstance(text, str):
                continue
            if claim.get("claim_type") not in CONTEXTUAL_CLAIM_TYPES:
                continue
            if sentence_matches_claim(sentence, text) or (
                coverage_scores(sentence, text)[1] >= 0.45
                and CONTEXTUAL_RHETORIC_RE.search(text)
            ):
                matched = claim
                break
        if matched is None:
            issues.append(
                issue(
                    code="ungrounded_contextual_assertion",
                    message=(
                        "generic contextual/analytical assertion is not a "
                        "grounded contextual/analysis claim"
                    ),
                    severity=SEVERITY_CRITICAL,
                    module="grounding",
                    sentence=sentence,
                )
            )
            continue
        refs = matched.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            issues.append(
                issue(
                    code="ungrounded_contextual_assertion",
                    message="contextual/analysis claim has no evidence_refs",
                    severity=SEVERITY_CRITICAL,
                    module="grounding",
                )
            )
            continue
        evidence_text = _evidence_text_for_refs(refs, allowed)
        sent_toks = set(content_tokens(sentence)) - {
            "underscores",
            "highlights",
            "growing",
            "reflects",
            "broader",
            "amid",
            "ongoing",
            "challenges",
            "industry",
        }
        ev_toks = set(content_tokens(evidence_text))
        if sent_toks and len(sent_toks & ev_toks) / len(sent_toks) < 0.25:
            issues.append(
                issue(
                    code="ungrounded_contextual_assertion",
                    message=(
                        "contextual/analysis claim evidence does not "
                        "deterministically support the body assertion"
                    ),
                    severity=SEVERITY_CRITICAL,
                    module="grounding",
                )
            )
    return issues


def check_grounding(
    article: dict[str, Any],
    article_input: dict[str, Any],
    *,
    other_article_inputs: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    coverage_issues, metrics = check_body_claim_coverage(
        article,
        article_input,
        other_article_inputs=other_article_inputs,
    )
    issues = list(coverage_issues)
    issues.extend(check_absence_claims(article, article_input))
    issues.extend(check_contextual_assertions(article, article_input))
    return issues, metrics
