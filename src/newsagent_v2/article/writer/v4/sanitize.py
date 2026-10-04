"""Deterministic cleanup of mechanical editorial artifacts before final QA."""

import re
from difflib import SequenceMatcher
from typing import Any

from newsagent_v2.article.qa.editorial_cleanliness import (
    NEAR_DUP_SENTENCE_RATIO,
    normalize_text,
)
from newsagent_v2.article.qa.structure import extract_quoted_spans
from newsagent_v2.article.qa.textutil import split_paragraphs, split_sentences, word_count


_SOURCE_LABEL_RE = re.compile(
    r"(?im)(^|(?<=\n))(\s*)(?:source|image|photo|credit|author|byline|read more|related)\s*:\s*"
)

# Mid-sentence injected metadata labels (e.g. "...announced. Source: Truth Social
# - @realDonaldTrump Clayton led..."). QA flags these via ARTIFACT_RE; sanitize
# must strip them too or the same artifact is re-detected after recovery.
_MID_SOURCE_LABEL_RE = re.compile(
    r"(?i)(?<=[.!?]\s)(?:source|image|photo|credit|author|byline|read more|related)\s*:\s*"
    r"[^\n.!?]*?(?=\s*[A-Z][a-z]+\s|\s*$|\s*\.?\s*$)"
)

# Round-up / feed chrome that Kimi sometimes pastes into body/FAQ.
_FEED_CHROME_RE = re.compile(
    r"(?im)\b(?:"
    r"here'?s what happened in crypto today|"
    r"need to know what happened in crypto today\??|"
    r"here is the latest news on daily trends and events impacting bitcoin price[^.!?]*[.!?]?"
    r")\b[^.!?\n]*[.!?]?"
)

_QUOTE_WRAP_RE = re.compile(
    r"[\"“”‘’'](?P<span>[^\"“”‘’'\n]{1,40})[\"“”‘’']"
)


def _similarity(a: str, b: str) -> float:
    na = normalize_text(a)
    nb = normalize_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _unwrap_quote_span(body: str, span: str) -> tuple[str, int]:
    """Remove quote marks around span; keep the words."""
    pattern = re.compile(r"[\"“”‘’']\s*" + re.escape(span) + r"\s*[\"“”‘’']")
    return pattern.subn(span, body)


def _strip_unmapped_quote_marks(body: str, quotes: list[Any], removals: list[dict[str, str]]) -> str:
    """Turn unmapped short/nickname quotes into plain text (keeps words, drops marks)."""
    mapped = {
        str(row.get("text") or "").strip()
        for row in quotes
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    }
    unmapped = [span for span in extract_quoted_spans(body) if span not in mapped]
    if not unmapped:
        return body

    cleaned = body
    for span in unmapped:
        # Only unwrap short nickname/token spans; long quotes stay for quote recovery.
        if word_count(span) > 4 and len(span) > 40:
            continue
        cleaned, n = _unwrap_quote_span(cleaned, span)
        if n:
            removals.append({"type": "unmapped_quote_unwrapped", "fragment": span[:160]})
    return cleaned


def drop_unsupported_quotes(article: dict[str, Any], article_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drop quote rows that fail evidence support and unwrap them in the body.

    Keeps prose (no length cliff) while clearing quote_unsupported_evidence /
    quote_body_unmapped criticals that block publish.
    """
    from newsagent_v2.article.input import evidence_index
    from newsagent_v2.article.qa.claims import _quote_supported_by_evidence

    quotes = article.get("quotes") if isinstance(article.get("quotes"), list) else []
    if not quotes:
        return article

    allowed = evidence_index(article_input if isinstance(article_input, dict) else {})
    body = str(article.get("article_body") or "")
    kept: list[Any] = []
    removals: list[dict[str, str]] = []
    for row in quotes:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        refs = row.get("evidence_refs") if isinstance(row.get("evidence_refs"), list) else []
        supported = bool(refs) and _quote_supported_by_evidence(text, refs, allowed)
        in_body = text in extract_quoted_spans(body) or text in body
        if supported and in_body:
            kept.append(row)
            continue
        # Unsupported or orphaned: unwrap marks in body and drop the row.
        body, n = _unwrap_quote_span(body, text)
        removals.append(
            {
                "type": "unsupported_quote_dropped",
                "fragment": text[:160],
                "unwrapped": bool(n),
            }
        )
    article["quotes"] = kept
    article["article_body"] = body
    prior = article.get("editorial_sanitation") if isinstance(article.get("editorial_sanitation"), dict) else {}
    changes = list(prior.get("changes") or [])
    changes.extend(removals)
    article["editorial_sanitation"] = {
        "applied": bool(changes),
        "change_count": len(changes),
        "changes": changes,
    }
    return article


def sanitize_editorial_artifacts(article: dict[str, Any]) -> dict[str, Any]:
    """Strip feed metadata, embedded headline/dek blocks, and duplicate prose before QA."""
    body = str(article.get("article_body") or "")
    headline = str(article.get("headline") or "")
    dek = str(article.get("dek") or "")
    removals: list[dict[str, str]] = []

    # Keep Conclusion/FAQ in the published body (length floor). Editorial QA
    # already excludes them via _body_without_closing_sections; here we only
    # unwrap bad quotes / strip chrome / near-dups.

    def normalize_source_label(match: re.Match[str]) -> str:
        removals.append({"type": "source_label_normalized", "fragment": match.group(0).strip()})
        return f"{match.group(1)}{match.group(2)}"

    cleaned = _SOURCE_LABEL_RE.sub(normalize_source_label, body)

    # Strip mid-sentence "Source: X - @handle" injections that QA flags.
    mid_hits = list(_MID_SOURCE_LABEL_RE.finditer(cleaned))
    if mid_hits:
        for hit in mid_hits:
            removals.append({"type": "mid_source_label_removed", "fragment": hit.group(0)[:160]})
        cleaned = _MID_SOURCE_LABEL_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)

    chrome_hits = list(_FEED_CHROME_RE.finditer(cleaned))
    if chrome_hits:
        for hit in chrome_hits:
            removals.append({"type": "feed_chrome_removed", "fragment": hit.group(0)[:160]})
        cleaned = _FEED_CHROME_RE.sub(" ", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    quotes = article.get("quotes") if isinstance(article.get("quotes"), list) else []
    cleaned = _strip_unmapped_quote_marks(cleaned, quotes, removals)

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
        kept_sentences: list[str] = []
        for sent in split_sentences(cleaned):
            s = sent.strip()
            if not s:
                continue
            snorm = normalize_text(s)
            if not snorm:
                continue
            # Exact + near-dup (same threshold as editorial QA).
            if any(
                snorm == normalize_text(prev) or _similarity(s, prev) >= NEAR_DUP_SENTENCE_RATIO
                for prev in kept_sentences
            ):
                removals.append({"type": "duplicate_sentence_removed", "fragment": s[:160]})
                continue
            kept_sentences.append(s)
        if kept_sentences:
            cleaned = " ".join(kept_sentences).strip()

    article["article_body"] = cleaned.strip()
    article["editorial_sanitation"] = {
        "applied": bool(removals),
        "change_count": len(removals),
        "changes": removals,
    }
    # #region agent log
    try:
        import json as _json, time as _time
        from pathlib import Path as _Path
        from newsagent_v2.article.qa.editorial_cleanliness import check_duplicate_prose as _cdp
        _near, _ = _cdp(str(article.get("article_body") or ""))
        _payload = {
            "sessionId": "7f9dc8",
            "hypothesisId": "B",
            "location": "sanitize.py:sanitize_editorial_artifacts",
            "message": "sanitize_result",
            "data": {
                "change_count": len(removals),
                "applied": bool(removals),
                "near_dup_remaining": len(_near),
                "removal_types": [r.get("type") for r in removals[:20]],
            },
            "timestamp": int(_time.time() * 1000),
        }
        with (_Path("debug-7f9dc8.log")).open("a", encoding="utf-8") as _f:
            _f.write(_json.dumps(_payload) + "\n")
    except Exception:
        pass
    # #endregion
    return article
