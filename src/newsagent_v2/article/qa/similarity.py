"""
Local source-similarity checks against supplied evidence text only.

This does not prove legal copyright compliance. It flags copied-looking
overlap with titles/summaries already in the evidence pack.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from newsagent_v2.article.input import evidence_text_blobs
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, SEVERITY_WARNING, issue
from newsagent_v2.article.qa.textutil import WORD_RE, split_sentences, words

EXACT_PHRASE_N = 12
HIGH_SENTENCE_SIMILARITY = 0.92
WARN_SENTENCE_SIMILARITY = 0.84
MIN_SENTENCE_WORDS = 10
QUOTE_RATIO = 0.90

STOP_OR_GENERIC = frozenset(
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
        "its",
        "their",
        "this",
        "that",
    }
)

DATE_LOCK = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "utc",
        "gmt",
        "today",
        "yesterday",
        "overnight",
    }
)

QUOTE_RE = re.compile(r"[\"“”]([^\"“”]{8,})[\"“”]")


def _ngrams(tokens: list[str], size: int) -> set[tuple[str, ...]]:
    if len(tokens) < size:
        return set()
    return {tuple(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def _mostly_generic(gram: tuple[str, ...]) -> bool:
    content = [
        token
        for token in gram
        if token not in STOP_OR_GENERIC and not re.fullmatch(r"[\d$£€%.,-]+", token)
    ]
    return len(content) <= 1


def _quote_texts(article: dict[str, Any], body: str) -> list[str]:
    texts: list[str] = []
    quotes = article.get("quotes") if isinstance(article.get("quotes"), list) else []
    for row in quotes:
        if not isinstance(row, dict):
            continue
        if str(row.get("kind") or "direct").lower() not in {"direct", "quoted"}:
            continue
        text = str(row.get("text") or "").strip()
        if len(text) >= 8:
            texts.append(text)
    for match in QUOTE_RE.finditer(body or ""):
        snippet = match.group(1).strip()
        if len(snippet) >= 8:
            texts.append(snippet)
    return texts


def _quoted_word_indices(body: str, quote_texts: list[str]) -> set[int]:
    covered: set[int] = set()
    if not body:
        return covered
    body_l = body.lower()
    matches = list(WORD_RE.finditer(body))
    for quote in quote_texts:
        needle = quote.strip().lower()
        if len(needle) < 8:
            continue
        start = 0
        while True:
            idx = body_l.find(needle, start)
            if idx < 0:
                break
            end = idx + len(needle)
            for index, match in enumerate(matches):
                if match.end() <= idx:
                    continue
                if match.start() >= end:
                    break
                covered.add(index)
            start = idx + 1
    return covered


def _factual_lock_tokens(article: dict[str, Any], article_input: dict[str, Any]) -> set[str]:
    lock = set(DATE_LOCK)
    entities = article.get("entities") if isinstance(article.get("entities"), list) else []
    for row in entities:
        if isinstance(row, dict):
            lock.update(words(str(row.get("name") or "")))
    keywords = article.get("keywords") if isinstance(article.get("keywords"), list) else []
    for item in keywords:
        lock.update(words(str(item or "")))
    for blob in evidence_text_blobs(article_input):
        # Titles and short factual strings live at the start of each blob;
        # lock individual tokens from titles/source names, not full prose.
        pass
    evidence = article_input.get("evidence") if isinstance(article_input.get("evidence"), list) else []
    for row in evidence:
        if not isinstance(row, dict):
            continue
        lock.update(words(str(row.get("source") or "")))
        lock.update(words(str(row.get("title") or "")))
    lock.update(words(str(article.get("headline") or "")))
    return {token for token in lock if token}


def _mostly_unavoidable(gram: tuple[str, ...], lock: set[str]) -> bool:
    content = [
        token
        for token in gram
        if token not in STOP_OR_GENERIC
        and token not in DATE_LOCK
        and not re.fullmatch(r"[\d$£€%.,-]+", token)
    ]
    if not content:
        return True
    return all(token in lock for token in content)


def _merge_span_starts(starts: list[int], width: int) -> int:
    if not starts:
        return 0
    ordered = sorted(set(starts))
    spans = 1
    end = ordered[0] + width
    for start in ordered[1:]:
        if start < end:
            end = max(end, start + width)
            continue
        spans += 1
        end = start + width
    return spans


def _sentence_is_marked_quote(sentence: str, quote_texts: list[str]) -> bool:
    generated = sentence.strip().lower()
    if len(generated) < 8:
        return False
    for quote in quote_texts:
        quoted = quote.strip().lower()
        if len(quoted) < 8:
            continue
        if quoted in generated or generated in quoted:
            return True
        if SequenceMatcher(None, generated, quoted).ratio() >= QUOTE_RATIO:
            return True
    return False


def check_similarity(
    article: dict[str, Any],
    article_input: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    issues: list[dict[str, str]] = []
    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""
    sources = evidence_text_blobs(article_input)
    quote_texts = _quote_texts(article, body)
    quoted_indices = _quoted_word_indices(body, quote_texts)
    lock = _factual_lock_tokens(article, article_input)
    body_sentences = [s for s in split_sentences(body) if len(s.split()) >= MIN_SENTENCE_WORDS]
    source_sentences: list[str] = []
    for blob in sources:
        source_sentences.extend(
            s for s in split_sentences(blob) if len(s.split()) >= MIN_SENTENCE_WORDS
        )
        if len(blob.split()) >= MIN_SENTENCE_WORDS and blob not in source_sentences:
            source_sentences.append(blob)

    body_tokens = words(body)
    ngram_hits = 0
    span_starts: list[int] = []
    for blob in sources:
        source_tokens = words(blob)
        grams = _ngrams(source_tokens, EXACT_PHRASE_N)
        for index in range(0, max(0, len(body_tokens) - EXACT_PHRASE_N + 1)):
            window = range(index, index + EXACT_PHRASE_N)
            if any(pos in quoted_indices for pos in window):
                continue
            gram = tuple(body_tokens[index : index + EXACT_PHRASE_N])
            if gram not in grams:
                continue
            if _mostly_generic(gram) or _mostly_unavoidable(gram, lock):
                continue
            ngram_hits += 1
            span_starts.append(index)
    exact_overlap = _merge_span_starts(span_starts, EXACT_PHRASE_N)
    if exact_overlap:
        issues.append(
            issue(
                code="exact_phrase_overlap",
                message=f"{exact_overlap} suspicious long exact phrase overlap(s) with evidence text",
                severity=SEVERITY_CRITICAL,
                module="similarity",
            )
        )

    max_sim = 0.0
    high_hits = 0
    warn_hits = 0
    for generated in body_sentences:
        if _sentence_is_marked_quote(generated, quote_texts):
            continue
        for source in source_sentences:
            ratio = SequenceMatcher(None, generated.lower(), source.lower()).ratio()
            if ratio > max_sim:
                max_sim = ratio
            if ratio >= HIGH_SENTENCE_SIMILARITY:
                high_hits += 1
            elif ratio >= WARN_SENTENCE_SIMILARITY:
                warn_hits += 1
    if high_hits:
        issues.append(
            issue(
                code="high_sentence_similarity",
                message="unusually high sentence similarity to supplied evidence text",
                severity=SEVERITY_CRITICAL,
                module="similarity",
            )
        )
    elif warn_hits:
        issues.append(
            issue(
                code="elevated_sentence_similarity",
                message="elevated sentence similarity to supplied evidence; review originality",
                severity=SEVERITY_WARNING,
                module="similarity",
            )
        )

    metrics = {
        "exact_overlap_count": exact_overlap,
        "exact_overlap_ngram_hits": ngram_hits,
        "max_similarity": round(max_sim, 4),
    }
    return issues, metrics
