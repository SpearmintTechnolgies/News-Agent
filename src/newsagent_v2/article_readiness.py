"""Deterministic article-readiness preflight for collected event clusters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlparse

from .cluster import EventCluster

INTENTIONAL_ARTICLE_WORDS = 600
MIN_EVIDENCE_ITEMS = 2
MIN_DISTINCT_SOURCES = 2
MIN_EVIDENCE_WORDS = 100
MIN_CORE_SUPPORTING_ITEMS = 2
MIN_FACT_MARKERS = 1
MIN_QUALITY_ITEMS = 2

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")
_FACT_MARKER_RE = re.compile(
    r"\b(?:19|20)\d{2}\b|\b\d+(?:\.\d+)?%?\b|\$\s?\d|"
    r"\b(?:said|says|according to|reported|announced|confirmed|stated)\b",
    re.IGNORECASE,
)
_STOP_WORDS = {
    "about", "after", "amid", "been", "being", "could", "from", "into",
    "more", "over", "that", "their", "this", "will", "with", "would",
}


@dataclass(frozen=True)
class ArticleReadiness:
    eligible: bool
    reasons: tuple[str, ...]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


def _usable_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _words(value: Any) -> set[str]:
    return {
        word.lower()
        for word in _WORD_RE.findall(str(value or ""))
        if len(word) >= 4 and word.lower() not in _STOP_WORDS
    }


def assess_article_readiness(cluster: EventCluster) -> ArticleReadiness:
    """Assess whether a cluster has enough grounded material for a publishable article.

    Intended length is the production ~600-800 grounded body-word capacity target. This examines only collector
    (and post-expansion) evidence text. It does not fetch URLs or call an LLM;
    downstream research and claim-level QA remain separate gates.
    """
    members = [item for item in cluster.members if item is not None]
    sources = {str(item.source).strip() for item in members if str(item.source).strip()}
    urls = {str(item.url).strip() for item in members if _usable_url(item.url)}
    evidence_text = [
        f"{item.title} {item.summary}".strip()
        for item in members
        if str(item.title or "").strip() or str(item.summary or "").strip()
    ]
    evidence_words = sum(len(_WORD_RE.findall(text)) for text in evidence_text)
    quality_items = sum(
        1
        for item in members
        if len(_WORD_RE.findall(f"{item.title} {item.summary}")) >= 12
        and (
            str(item.source_role or "") == "primary_evidence"
            or float(item.source_authority or 0.0) >= 0.5
        )
    )
    representative_terms = _words(cluster.representative.title)
    supporting = sum(
        1
        for text in evidence_text
        if representative_terms and len(representative_terms & _words(text)) >= 2
    )
    fact_markers = sum(1 for text in evidence_text if _FACT_MARKER_RE.search(text))

    reasons: list[str] = []
    if len(members) < MIN_EVIDENCE_ITEMS:
        reasons.append("evidence_quantity_below_minimum")
    if len(sources) < MIN_DISTINCT_SOURCES:
        reasons.append("source_diversity_below_minimum")
    if len(urls) < MIN_EVIDENCE_ITEMS:
        reasons.append("usable_url_count_below_minimum")
    if evidence_words < MIN_EVIDENCE_WORDS:
        reasons.append("factual_material_below_minimum")
    if quality_items < MIN_QUALITY_ITEMS:
        reasons.append("evidence_quality_below_minimum")
    if supporting < MIN_CORE_SUPPORTING_ITEMS:
        reasons.append("core_event_support_below_minimum")
    if fact_markers < MIN_FACT_MARKERS:
        reasons.append("fact_markers_missing")

    metrics = {
        "intended_article_words": INTENTIONAL_ARTICLE_WORDS,
        "evidence_item_count": len(members),
        "distinct_source_count": len(sources),
        "usable_url_count": len(urls),
        "evidence_word_count": evidence_words,
        "quality_evidence_item_count": quality_items,
        "core_supporting_item_count": supporting,
        "fact_marker_count": fact_markers,
    }
    return ArticleReadiness(not reasons, tuple(reasons), metrics)


def preflight_article_readiness(
    clusters: Sequence[EventCluster],
) -> tuple[list[EventCluster], dict[str, dict[str, Any]]]:
    """Return eligible clusters and an internal audit for every candidate."""
    eligible: list[EventCluster] = []
    audit: dict[str, dict[str, Any]] = {}
    for cluster in clusters:
        result = assess_article_readiness(cluster)
        audit[cluster.event_id] = result.to_dict()
        if result.eligible:
            eligible.append(cluster)
    return eligible, audit