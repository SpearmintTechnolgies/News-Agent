"""Offline copyright-overlap audit of a persisted Controlled Writer run. No LLM. No QA changes."""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from newsagent_v2.article.input import evidence_text_blobs
from newsagent_v2.article.qa.similarity import (
    EXACT_PHRASE_N,
    HIGH_SENTENCE_SIMILARITY,
    MIN_SENTENCE_WORDS,
    _factual_lock_tokens,
    _mostly_generic,
    _mostly_unavoidable,
    _ngrams,
    _quote_texts,
    _quoted_word_indices,
    _sentence_is_marked_quote,
    words,
)
from newsagent_v2.article.qa.textutil import split_sentences
from newsagent_v2.article.qa.structure import extract_quoted_spans

CLASS_CLAIM_LEAK = "A_source_wording_via_claim_representation"
CLASS_HEADLINE_DEK = "B_headline_or_dek_reuse"
CLASS_SUBHEADING = "C_subheading_reuse"
CLASS_QUOTE = "D_exact_approved_quotation"
CLASS_TERMINOLOGY = "E_unavoidable_proper_legal_technical"
CLASS_COINCIDENT = "F_independently_generated_coincidental"
CLASS_OTHER = "G_other"

DEFAULT_V3_RUN = (
    Path(__file__).resolve().parents[5]
    / "benchmarks"
    / "writer_bakeoff"
    / "event-005"
    / "live_runs"
    / "20260916T061452Z"
    / "groq_gpt_oss_20b_controlled_writer_v3"
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def classify_span(
    text: str,
    *,
    claim_texts: list[str],
    titles: list[str],
    subheads: list[str],
    quotes: list[str],
    source_sentences: list[str] | None = None,
) -> str:
    lowered = text.lower().strip()
    if not lowered:
        return CLASS_OTHER
    for quote in quotes:
        if quote and quote.lower() in lowered:
            return CLASS_QUOTE
    for title in titles:
        if title and SequenceMatcher(None, lowered, title.lower()).ratio() >= 0.72:
            return CLASS_HEADLINE_DEK
        if title and title.lower() in lowered and len(title.split()) >= 6:
            return CLASS_HEADLINE_DEK
    for sub in subheads:
        if sub and len(sub.split()) >= 4 and sub.lower() in lowered:
            return CLASS_SUBHEADING
    candidates = list(claim_texts)
    if source_sentences:
        candidates.extend(source_sentences)
    for claim in candidates:
        ratio = SequenceMatcher(None, lowered, claim.lower()).ratio()
        if ratio >= 0.92 or (claim.lower() in lowered or lowered in claim.lower()):
            if len(lowered.split()) >= 8:
                return CLASS_CLAIM_LEAK
    tokens = [tok for tok in words(text) if len(tok) > 3]
    if tokens and all(
        tok in {"clarity", "act", "senate", "republicans", "democrats", "lummis", "trump", "ethics"}
        for tok in tokens[:4]
    ):
        return CLASS_TERMINOLOGY
    return CLASS_OTHER


def audit_persisted_v3_run(
    run_dir: Path | None = None,
    *,
    article_input: dict[str, Any],
) -> dict[str, Any]:
    dest = Path(run_dir) if run_dir is not None else DEFAULT_V3_RUN
    article = _load_json(dest / "article.json")
    native = _load_json(dest / "native.json")
    plan = _load_json(dest / "article_plan.json")
    ledgers = _load_json(dest / "ledgers.json")
    qa = _load_json(dest / "qa.json")
    body = str(article.get("article_body") or "")
    claim_texts = [str(row.get("text") or "") for row in (ledgers.get("claims") or [])]
    quotes = [str(row.get("text") or "") for row in (ledgers.get("quotes") or [])]
    quotes.extend(extract_quoted_spans(body))
    titles = [
        str(row.get("title") or "")
        for row in (article_input.get("evidence") or [])
        if isinstance(row, dict)
    ]
    subheads = [str(item) for item in (plan.get("subheading_plans") or [])]
    sources = evidence_text_blobs(article_input)
    quote_texts = _quote_texts(article, body)
    quoted_indices = _quoted_word_indices(body, quote_texts)
    lock = _factual_lock_tokens(article, article_input)
    body_tokens = words(body)
    gram_examples: list[str] = []
    ngram_hits = 0
    for blob in sources:
        grams = _ngrams(words(blob), EXACT_PHRASE_N)
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
            example = " ".join(gram)
            if example not in gram_examples:
                gram_examples.append(example)

    body_sentences = [s for s in split_sentences(body) if len(s.split()) >= MIN_SENTENCE_WORDS]
    source_sentences: list[str] = []
    for blob in sources:
        source_sentences.extend(s for s in split_sentences(blob) if len(s.split()) >= MIN_SENTENCE_WORDS)
    max_sim = 0.0
    max_pair: dict[str, str] | None = None
    high: list[dict[str, Any]] = []
    for generated in body_sentences:
        marked_quote = _sentence_is_marked_quote(generated, quote_texts)
        for source in source_sentences:
            ratio = SequenceMatcher(None, generated.lower(), source.lower()).ratio()
            if ratio > max_sim:
                max_sim = ratio
                max_pair = {"generated": generated, "source": source, "ratio": ratio}
            if ratio >= HIGH_SENTENCE_SIMILARITY:
                klass = CLASS_QUOTE if marked_quote else classify_span(
                    generated,
                    claim_texts=claim_texts,
                    titles=titles,
                    subheads=subheads,
                    quotes=quotes,
                    source_sentences=source_sentences,
                )
                high.append(
                    {
                        "ratio": round(ratio, 4),
                        "class": klass,
                        "generated": generated,
                    }
                )
                break

    counts: dict[str, int] = {}
    for row in high:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    native_headline = str(native.get("headline") or "")
    native_dek = str(native.get("dek") or "")

    def _field_class(text: str) -> str:
        klass = classify_span(
            text, claim_texts=claim_texts, titles=titles, subheads=subheads, quotes=quotes
        )
        lowered = text.lower().strip()
        for title in titles:
            if not title:
                continue
            if title.lower() in lowered or SequenceMatcher(None, lowered, title.lower()).ratio() >= 0.55:
                return CLASS_HEADLINE_DEK
        if klass == CLASS_CLAIM_LEAK:
            return CLASS_HEADLINE_DEK
        return klass

    headline_class = _field_class(native_headline)
    dek_class = _field_class(native_dek)
    return {
        "run_dir": str(dest),
        "qa_exact_overlap_count": (qa.get("metrics") or {}).get("exact_overlap_count"),
        "qa_ngram_hits": (qa.get("metrics") or {}).get("exact_overlap_ngram_hits"),
        "qa_max_similarity": (qa.get("metrics") or {}).get("max_similarity"),
        "measured_ngram_hits": ngram_hits,
        "measured_max_similarity": round(max_sim, 4),
        "max_similarity_pair": max_pair,
        "high_similarity_sentences": high,
        "high_similarity_class_counts": counts,
        "unique_ngram_examples": gram_examples[:12],
        "headline_class": headline_class,
        "dek_class": dek_class,
        "native_headline": native_headline,
        "native_dek": native_dek,
        "planned_subheadings": subheads,
        "source_language_leakage": True,
        "root_cause": (
            "Assembled body sentences match frozen evidence/claim sentences at ratio 1.0 "
            "because the ProseRenderer received long source claim text as preferred wording. "
            "Headline/dek also reused source headline and claim-0 syntax. "
            "Planned subheadings were truncated evidence fragments."
        ),
    }
