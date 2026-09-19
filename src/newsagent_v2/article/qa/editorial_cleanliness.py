"""
Deterministic editorial cleanliness gate.

Detects body contamination / scraper debris / embedded headline-dek /
duplicated prose. Does NOT call an LLM. Does NOT change grounding or
copyright policy.

Failure code: EDITORIAL_CLEANLINESS_FAILED
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, issue
from newsagent_v2.article.qa.textutil import split_sentences, word_count

FAILURE_CODE = "EDITORIAL_CLEANLINESS_FAILED"
MODULE = "editorial_cleanliness"

# Strong similarity thresholds — conservative by design.
HEADLINE_EMBED_RATIO = 0.86
DEK_EMBED_RATIO = 0.86
NEAR_DUP_SENTENCE_RATIO = 0.92
NEAR_DUP_PARAGRAPH_RATIO = 0.88
MIN_HEADLINE_MATCH_WORDS = 6
MIN_DEK_MATCH_WORDS = 8
MIN_DUP_SENTENCE_WORDS = 8
MIN_DUP_PARAGRAPH_CHARS = 60

ARTIFACT_LABELS = (
    "source",
    "image",
    "image credit",
    "photo",
    "photo credit",
    "credit",
    "courtesy",
    "read more",
    "related",
    "advertisement",
    "sponsored",
    "author",
    "byline",
)

# Standalone / injected metadata labels followed by a colon.
_ARTIFACT_LABEL_ALT = "|".join(re.escape(label) for label in sorted(ARTIFACT_LABELS, key=len, reverse=True))
ARTIFACT_RE = re.compile(
    rf"(?:^|[\n\r]|[.!?][\"')\]]*\s+|,\s+)"
    rf"(?P<label>{_ARTIFACT_LABEL_ALT})\s*:\s*",
    re.IGNORECASE | re.MULTILINE,
)

# Mid-prose concatenation without space: "...publication.Source:"
CONCAT_ARTIFACT_RE = re.compile(
    rf"(?P<label>{_ARTIFACT_LABEL_ALT})\s*:\s*",
    re.IGNORECASE,
)

FEED_DEBRIS_EXACT = frozenset(
    {
        "home",
        "menu",
        "subscribe",
        "share",
        "tweet",
        "follow us",
        "sign up",
        "log in",
        "login",
        "newsletter",
        "cookie policy",
        "privacy policy",
        "terms of service",
        "advertisement",
        "sponsored content",
        "related articles",
        "read more",
        "skip to content",
        "main menu",
    }
)

PUNCT_RE = re.compile(r"[^\w\s$£€%.-]+", re.UNICODE)
SPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    raw = PUNCT_RE.sub(" ", raw)
    return SPACE_RE.sub(" ", raw).strip()


def token_set(text: str) -> set[str]:
    return {tok for tok in normalize_text(text).split() if tok}


def similarity_ratio(a: str, b: str) -> float:
    na = normalize_text(a)
    nb = normalize_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _paragraphs(body: str) -> list[str]:
    parts = re.split(r"\n\s*\n", str(body or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _looks_like_attribution_prose(snippet: str) -> bool:
    """Legitimate journalistic attribution — must not fail Check 1 alone."""
    low = snippet.lower()
    return bool(
        re.search(
            r"\b(according to|reported|reports|said|says|told|stated|announced)\b",
            low,
        )
    )


def check_source_artifacts(body: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    text = str(body or "")
    for match in ARTIFACT_RE.finditer(text):
        label = match.group("label")
        start = match.start("label")
        # Window around the match for context.
        window = text[max(0, start - 40) : min(len(text), match.end() + 80)]
        # If the whole window is clearly "according to X said" style without a
        # metadata colon-label role, skip — but ARTIFACT_RE already requires colon.
        # Extra guard: "source code" / "image quality" without colon won't match.
        fragment = text[start : min(len(text), match.end() + 60)].split("\n", 1)[0].strip()
        if _looks_like_attribution_prose(fragment) and ":" not in fragment[: len(label) + 2]:
            continue
        hits.append(
            {
                "label": label.lower(),
                "fragment": fragment[:160],
                "offset": start,
            }
        )
    # Deduplicate identical fragments.
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        key = normalize_text(hit["fragment"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def check_embedded_headline_dek(
    body: str,
    *,
    headline: str | None,
    dek: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    headline_hits: list[dict[str, Any]] = []
    dek_hits: list[dict[str, Any]] = []
    hl = str(headline or "").strip()
    dk = str(dek or "").strip()
    sentences = [s.strip() for s in split_sentences(body) if s.strip()]
    paragraphs = _paragraphs(body)
    seen_h: set[str] = set()
    seen_d: set[str] = set()

    def _consider(
        unit: str,
        *,
        target: str,
        min_words: int,
        threshold: float,
        hits: list[dict[str, Any]],
        seen: set[str],
        kind: str,
        allow_lede: bool,
    ) -> None:
        if word_count(target) < min_words:
            return
        if word_count(unit) < min_words:
            return
        # Unit should be headline/dek-sized, not a long prose paragraph.
        if word_count(unit) > max(word_count(target) + 8, int(word_count(target) * 1.6)):
            return
        ratio = similarity_ratio(unit, target)
        if ratio < threshold:
            return
        # Lede often echoes the headline — only fail lede on near-exact copy.
        if not allow_lede and ratio < 0.97:
            return
        key = normalize_text(unit)
        if key in seen:
            return
        seen.add(key)
        hits.append({"text": unit[:240], "ratio": round(ratio, 4), "kind": kind})

    if hl:
        for idx, unit in enumerate(sentences):
            _consider(
                unit,
                target=hl,
                min_words=MIN_HEADLINE_MATCH_WORDS,
                threshold=HEADLINE_EMBED_RATIO,
                hits=headline_hits,
                seen=seen_h,
                kind="headline",
                allow_lede=idx > 0,
            )
        for idx, unit in enumerate(paragraphs):
            _consider(
                unit,
                target=hl,
                min_words=MIN_HEADLINE_MATCH_WORDS,
                threshold=HEADLINE_EMBED_RATIO,
                hits=headline_hits,
                seen=seen_h,
                kind="headline",
                allow_lede=idx > 0,
            )

    if dk:
        for idx, unit in enumerate(sentences):
            _consider(
                unit,
                target=dk,
                min_words=MIN_DEK_MATCH_WORDS,
                threshold=DEK_EMBED_RATIO,
                hits=dek_hits,
                seen=seen_d,
                kind="dek",
                allow_lede=idx > 0,
            )
        for idx, unit in enumerate(paragraphs):
            _consider(
                unit,
                target=dk,
                min_words=MIN_DEK_MATCH_WORDS,
                threshold=DEK_EMBED_RATIO,
                hits=dek_hits,
                seen=seen_d,
                kind="dek",
                allow_lede=idx > 0,
            )

    return headline_hits, dek_hits


def check_duplicate_prose(body: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sentences = [s.strip() for s in split_sentences(body) if s.strip()]
    paragraphs = _paragraphs(body)
    dup_sentences: list[dict[str, Any]] = []
    dup_paragraphs: list[dict[str, Any]] = []

    # Exact + near-duplicate sentences.
    norm_to_first: dict[str, str] = {}
    for sent in sentences:
        if word_count(sent) < MIN_DUP_SENTENCE_WORDS:
            continue
        norm = normalize_text(sent)
        if not norm:
            continue
        if norm in norm_to_first:
            dup_sentences.append(
                {
                    "text": sent[:240],
                    "match": norm_to_first[norm][:240],
                    "ratio": 1.0,
                    "kind": "exact_sentence",
                }
            )
            continue
        # Near-duplicate against prior long sentences.
        for prior_norm, prior_text in list(norm_to_first.items()):
            ratio = SequenceMatcher(None, norm, prior_norm).ratio()
            if ratio >= NEAR_DUP_SENTENCE_RATIO:
                dup_sentences.append(
                    {
                        "text": sent[:240],
                        "match": prior_text[:240],
                        "ratio": round(ratio, 4),
                        "kind": "near_sentence",
                    }
                )
                break
        else:
            norm_to_first[norm] = sent

    # Adjacent / near-duplicate paragraphs.
    for i, para in enumerate(paragraphs):
        if len(para) < MIN_DUP_PARAGRAPH_CHARS:
            continue
        for j in range(i):
            other = paragraphs[j]
            if len(other) < MIN_DUP_PARAGRAPH_CHARS:
                continue
            # Prefer adjacent for "substantially duplicated adjacent paragraphs"
            adjacent = j == i - 1
            ratio = similarity_ratio(para, other)
            threshold = NEAR_DUP_PARAGRAPH_RATIO if adjacent else 0.94
            if ratio >= threshold or normalize_text(para) == normalize_text(other):
                dup_paragraphs.append(
                    {
                        "text": para[:240],
                        "match": other[:240],
                        "ratio": round(ratio, 4),
                        "adjacent": adjacent,
                        "kind": "paragraph",
                    }
                )
                break

    return dup_sentences, dup_paragraphs


def check_feed_debris(body: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    paragraphs = _paragraphs(body)
    for para in paragraphs:
        norm = normalize_text(para)
        if norm in FEED_DEBRIS_EXACT:
            hits.append({"fragment": para[:160], "kind": "nav_or_chrome"})
            continue
        # Isolated caption/source-like short fragment with a label colon.
        if word_count(para) <= 12 and CONCAT_ARTIFACT_RE.search(para):
            # Already covered by artifacts; still record as debris if very short.
            hits.append({"fragment": para[:160], "kind": "metadata_line"})
            continue
        # Headline + dek block: two short consecutive paragraphs where first is
        # title-case-ish and second is longer summary — only when first has no
        # terminal period and second looks like a dek (1 sentence, 8–30 words).
    for i in range(len(paragraphs) - 1):
        a, b = paragraphs[i], paragraphs[i + 1]
        a_words = word_count(a)
        b_words = word_count(b)
        if not (4 <= a_words <= 18 and 8 <= b_words <= 35):
            continue
        if a.rstrip().endswith((".", "!", "?")):
            continue
        if len(split_sentences(b)) > 2:
            continue
        # Mid-body only (not first paragraph pair — ledes are allowed).
        if i == 0:
            continue
        # Avoid flagging normal short transitional paragraphs: require Title Case density.
        caps = sum(1 for tok in a.split() if tok[:1].isupper())
        if caps < max(2, int(a_words * 0.5)):
            continue
        hits.append(
            {
                "fragment": f"{a[:80]} || {b[:120]}",
                "kind": "headline_dek_block",
            }
        )
    # Dedup
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for hit in hits:
        key = normalize_text(hit["fragment"])
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def evaluate_editorial_cleanliness(
    article: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Run all cleanliness checks on a CanonicalArticle-like dict.

    Returns diagnostics; does not mutate the article.
    """
    if not isinstance(article, dict):
        return {
            "editorial_cleanliness": "FAIL",
            "artifact_fragments": [],
            "embedded_headline_matches": [],
            "embedded_dek_matches": [],
            "duplicate_sentences": [],
            "duplicate_paragraphs": [],
            "feed_debris": [],
            "critical_count": 1,
            "failure_code": FAILURE_CODE,
            "reason": "article_not_object",
        }

    body = str(article.get("article_body") or "")
    headline = str(article.get("headline") or "")
    dek = str(article.get("dek") or "")

    artifacts = check_source_artifacts(body)
    hl_hits, dek_hits = check_embedded_headline_dek(body, headline=headline, dek=dek)
    dup_sents, dup_paras = check_duplicate_prose(body)
    debris = check_feed_debris(body)

    # Feed debris that duplicates artifact hits is fine; headline_dek_block is additive.
    failed = bool(artifacts or hl_hits or dek_hits or dup_sents or dup_paras or debris)
    critical_count = 1 if failed else 0
    return {
        "editorial_cleanliness": "FAIL" if failed else "PASS",
        "artifact_fragments": artifacts,
        "embedded_headline_matches": hl_hits,
        "embedded_dek_matches": dek_hits,
        "duplicate_sentences": dup_sents,
        "duplicate_paragraphs": dup_paras,
        "feed_debris": debris,
        "critical_count": critical_count,
        "failure_code": FAILURE_CODE if failed else None,
    }


def check_editorial_cleanliness(article: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """QA-module adapter: returns (issues, metrics_fragment)."""
    result = evaluate_editorial_cleanliness(article if isinstance(article, dict) else None)
    metrics = {
        "editorial_cleanliness": result["editorial_cleanliness"],
        "editorial_cleanliness_detail": {
            "artifact_fragments": result["artifact_fragments"],
            "embedded_headline_matches": result["embedded_headline_matches"],
            "embedded_dek_matches": result["embedded_dek_matches"],
            "duplicate_sentences": result["duplicate_sentences"],
            "duplicate_paragraphs": result["duplicate_paragraphs"],
            "feed_debris": result["feed_debris"],
        },
    }
    issues: list[dict[str, Any]] = []
    if result["editorial_cleanliness"] == "FAIL":
        issues.append(
            issue(
                code=FAILURE_CODE,
                message=(
                    "Article body failed editorial cleanliness: contamination, "
                    "embedded headline/dek, duplicated prose, or feed debris detected"
                ),
                severity=SEVERITY_CRITICAL,
                module=MODULE,
                detail={
                    "artifact_count": len(result["artifact_fragments"]),
                    "embedded_headline_count": len(result["embedded_headline_matches"]),
                    "embedded_dek_count": len(result["embedded_dek_matches"]),
                    "duplicate_sentence_count": len(result["duplicate_sentences"]),
                    "duplicate_paragraph_count": len(result["duplicate_paragraphs"]),
                    "feed_debris_count": len(result["feed_debris"]),
                },
            )
        )
    return issues, metrics
