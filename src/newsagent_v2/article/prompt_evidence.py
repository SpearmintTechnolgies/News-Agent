"""
Compact Groq-facing evidence. Deterministic. No LLM.

Sufficiency is computed on cleaned extracted_text in enrichment.
Prompt serialization uses bounded evidence units with stable IDs.
"""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.enrich import (
    ATTRIBUTION_RE,
    MIN_EXTRACTED_WORDS,
    MIN_SNIPPET_WORDS,
    _source_rank,
)
from newsagent_v2.article.extract_clean import clean_extracted_article_text
from newsagent_v2.article.qa.textutil import NUMBER_TOKEN_RE, split_sentences, word_count, words
from newsagent_v2.providers.groq_editorial import estimate_prompt_tokens_from_bytes

MAX_PROMPT_EVIDENCE_WORDS_PER_STORY = 280
JACCARD_DUP = 0.88
GROQ_ON_DEMAND_TPM_LIMIT = 8000
GROQ_BATCH_ADMISSION_TARGET = 7000


def _jaccard(a: str, b: str) -> float:
    left = set(words(a))
    right = set(words(b))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _is_duplicate(sentence: str, kept: list[str]) -> bool:
    key = " ".join(words(sentence))
    for prior in kept:
        if " ".join(words(prior)) == key:
            return True
        if _jaccard(sentence, prior) >= JACCARD_DUP:
            return True
    return False


def _important(sentence: str) -> bool:
    return bool(NUMBER_TOKEN_RE.search(sentence) or ATTRIBUTION_RE.search(sentence))


def evidence_id_for(event_id: str, index: int) -> str:
    return f"{event_id}-e{index:02d}"


def _source_passages(row: dict[str, Any]) -> str:
    extracted = row.get("extracted_text") if isinstance(row.get("extracted_text"), str) else ""
    cleaned = clean_extracted_article_text(extracted)
    if cleaned:
        return cleaned
    summary = row.get("summary") if isinstance(row.get("summary"), str) else ""
    return clean_extracted_article_text(summary)


def story_evidence_words(story: dict[str, Any]) -> tuple[int, int]:
    article_input = story.get("article_input") if isinstance(story.get("article_input"), dict) else {}
    evidence = article_input.get("evidence") if isinstance(article_input.get("evidence"), list) else []
    before = 0
    after = 0
    for row in evidence:
        if not isinstance(row, dict):
            continue
        raw = row.get("extracted_text") if isinstance(row.get("extracted_text"), str) else ""
        snippets = row.get("factual_snippets") if isinstance(row.get("factual_snippets"), list) else []
        snippet_text = " ".join(str(item) for item in snippets if isinstance(item, str))
        before += word_count(raw) + word_count(snippet_text)
        after += word_count(_source_passages(row))
    return before, after


def compact_story_evidence(
    story: dict[str, Any],
    *,
    max_words: int = MAX_PROMPT_EVIDENCE_WORDS_PER_STORY,
) -> dict[str, Any]:
    article_input = story.get("article_input") if isinstance(story.get("article_input"), dict) else {}
    event_id = str(story.get("event_id") or article_input.get("event_id") or "")
    evidence = article_input.get("evidence") if isinstance(article_input.get("evidence"), list) else []
    ranked = sorted(
        [row for row in evidence if isinstance(row, dict)],
        key=_source_rank,
    )
    before_words, cleaned_words = story_evidence_words(story)
    units_before = sum(
        1
        for row in evidence
        if isinstance(row, dict)
        and (
            str(row.get("extracted_text") or "").strip()
            or (isinstance(row.get("factual_snippets"), list) and row.get("factual_snippets"))
        )
    )
    kept_sentences: list[str] = []
    units: list[dict[str, Any]] = []
    used_words = 0
    unit_index = 0
    reason = "within_budget"
    for row in ranked:
        passage = _source_passages(row)
        if not passage:
            continue
        unit_index += 1
        selected: list[str] = []
        for sentence in split_sentences(passage):
            if word_count(sentence) < MIN_SNIPPET_WORDS and not _important(sentence):
                continue
            if _is_duplicate(sentence, kept_sentences):
                continue
            next_count = used_words + word_count(sentence)
            if next_count > max_words and kept_sentences:
                reason = "per_story_word_budget"
                break
            selected.append(sentence)
            kept_sentences.append(sentence)
            used_words = next_count
        if not selected:
            continue
        units.append(
            {
                "evidence_id": evidence_id_for(event_id, unit_index),
                "source": row.get("source"),
                "source_role": row.get("source_role"),
                "source_authority": row.get("source_authority"),
                "url": row.get("url"),
                "published": row.get("published"),
                "text": " ".join(selected),
            }
        )
        if used_words >= max_words:
            break
    if used_words < MIN_EXTRACTED_WORDS and cleaned_words >= MIN_EXTRACTED_WORDS:
        reason = "budget_below_sufficiency_floor_but_source_sufficient"
    metrics = {
        "evidence_words_before_compaction": before_words,
        "evidence_words_after_compaction": used_words,
        "evidence_words_after_cleanup": cleaned_words,
        "evidence_units_before": units_before,
        "evidence_units_after": len(units),
        "truncation_reason": reason,
        "max_prompt_evidence_words": max_words,
    }
    return {
        "event_id": event_id,
        "representative_title": article_input.get("representative_title") or story.get("representative_title"),
        "evidence_units": units,
        "evidence_metrics": article_input.get("evidence_sufficiency")
        or story.get("evidence_sufficiency")
        or {},
        "prompt_compaction": metrics,
    }


def compact_batch_stories(
    stories: list[dict[str, Any]],
    *,
    max_words: int = MAX_PROMPT_EVIDENCE_WORDS_PER_STORY,
) -> list[dict[str, Any]]:
    return [compact_story_evidence(story, max_words=max_words) for story in stories]


def admission_token_estimate(serialized_utf8_bytes: int) -> int:
    """Offline admission estimate matching live #5 (serialized request / ~4 bytes)."""
    return estimate_prompt_tokens_from_bytes(serialized_utf8_bytes)
