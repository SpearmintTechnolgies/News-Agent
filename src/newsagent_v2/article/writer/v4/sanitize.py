"""Deterministic cleanup of mechanical editorial artifacts before final QA."""

import re
from difflib import SequenceMatcher
from typing import Any

from newsagent_v2.article.qa.editorial_cleanliness import normalize_text
from newsagent_v2.article.qa.textutil import split_paragraphs, split_sentences, word_count


_SOURCE_LABEL_RE = re.compile(
    r"(?im)(^|(?<=\n))(\s*)(?:source|image|photo|credit|author|byline|read more|related)\s*:\s*"
)


def _similarity(a: str, b: str) -> float:
    na = normalize_text(a)
    nb = normalize_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def sanitize_editorial_artifacts(article: dict[str, Any]) -> dict[str, Any]:
    """Strip feed metadata, embedded headline/dek blocks, and duplicate prose before QA."""
    body = str(article.get("article_body") or "")
    headline = str(article.get("headline") or "")
    dek = str(article.get("dek") or "")
    removals: list[dict[str, str]] = []

    def normalize_source_label(match: re.Match[str]) -> str:
        removals.append({"type": "source_label_normalized", "fragment": match.group(0).strip()})
        return f"{match.group(1)}{match.group(2)}"

    cleaned = _SOURCE_LABEL_RE.sub(normalize_source_label, body)

    paragraphs = [p.strip() for p in split_paragraphs(cleaned) if p.strip()]
    filtered: list[str] = []
    seen_norm: set[str] = set()
    for para in paragraphs:
        norm = normalize_text(para)
        if not norm:
            continue
        if re.match(r"^(?:source|image|photo|credit|author|byline|read more|related)\s*:\s*", para, re.I):
            removals.append({"type": "artifact_removed", "fragment": para[:160]})
            continue
        if norm in seen_norm:
            removals.append({"type": "duplicate_paragraph_removed", "fragment": para[:160]})
            continue
        if headline and word_count(headline) >= 6 and _similarity(para, headline) >= 0.86:
            removals.append({"type": "headline_embedded_removed", "fragment": para[:160]})
            continue
        if dek and word_count(dek) >= 8 and _similarity(para, dek) >= 0.86:
            removals.append({"type": "dek_embedded_removed", "fragment": para[:160]})
            continue
        for prev in filtered:
            if _similarity(para, prev) >= 0.92:
                removals.append({"type": "duplicate_prose_removed", "fragment": para[:160]})
                break
        else:
            filtered.append(para)
            seen_norm.add(norm)

    if filtered:
        cleaned = "\n\n".join(filtered).strip()
        sentence_norms: set[str] = set()
        kept_sentences: list[str] = []
        for sent in split_sentences(cleaned):
            s = sent.strip()
            if not s:
                continue
            snorm = normalize_text(s)
            if snorm in sentence_norms:
                removals.append({"type": "duplicate_sentence_removed", "fragment": s[:160]})
                continue
            sentence_norms.add(snorm)
            kept_sentences.append(s)
        if kept_sentences:
            cleaned = " ".join(kept_sentences).strip()

    article["article_body"] = cleaned.strip()
    article["editorial_sanitation"] = {
        "applied": bool(removals),
        "change_count": len(removals),
        "changes": removals,
    }
    return article
