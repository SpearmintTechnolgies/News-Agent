"""V5 Source Expansion Adapter - deterministic zero-LLM source expansion.

Expands research for an event by collecting from SourceRegistry and
matching entries deterministically against the event.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from newsagent_v2.discovery.collector_v2 import CollectorV2
from newsagent_v2.discovery.provenance import (
    is_primary_provenance,
    normalize_source_role,
    normalize_source_type,
    stable_source_id,
)
from newsagent_v2.discovery.raw_news_item import RawNewsItem
from newsagent_v2.discovery.source_registry import SourceRegistry, SourceRole, default_sources_config_path
from newsagent_v2.v5_generation.evidence_dimensions import (
    MAX_CANDIDATES_PER_ROUND,
    MAX_SEARCH_HITS,
    MIN_RELEVANCE_SCORE,
    dimension_targeted_queries,
    evaluate_evidence_dimensions,
)


MAX_CANDIDATE_SOURCES = MAX_CANDIDATES_PER_ROUND
logger = logging.getLogger(__name__)
MIN_RELEVANCE_SCORE_DEFAULT = MIN_RELEVANCE_SCORE


def _token_overlap(text1: str, text2: str) -> float:
    tokens1 = set(re.findall(r"\b[a-z]+\b", text1.lower()))
    tokens2 = set(re.findall(r"\b[a-z]+\b", text2.lower()))
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    return len(intersection) / max(len(tokens1), len(tokens2))


def _entity_overlap(entities1: list[str], entities2: list[str]) -> float:
    if not entities1 or not entities2:
        return 0.0
    set1 = {e.lower() for e in entities1}
    set2 = {e.lower() for e in entities2}
    intersection = set1 & set2
    return len(intersection) / max(len(set1), len(set2))


def _calculate_relevance_score(
    event_title: str,
    event_entities: list[str],
    event_topic: str,
    item: RawNewsItem,
    *,
    query_boost: str = "",
) -> float:
    scores = []
    title_overlap = _token_overlap(event_title, item.headline)
    scores.append(("title", title_overlap, 0.35))
    entity_score = _entity_overlap(event_entities, item.entities)
    scores.append(("entity", entity_score, 0.30))
    topic_match = 0.0
    if event_topic and item.topics:
        event_topic_lower = event_topic.lower()
        for topic in item.topics:
            if event_topic_lower in topic.lower() or topic.lower() in event_topic_lower:
                topic_match = 1.0
                break
    scores.append(("topic", topic_match, 0.15))
    keyword_score = _token_overlap(" ".join(event_entities), " ".join(item.keywords))
    scores.append(("keyword", keyword_score, 0.1))
    boost = 0.0
    if query_boost:
        boost = _token_overlap(query_boost, f"{item.headline} {item.description}")
    scores.append(("query_boost", boost, 0.1))
    total_weight = sum(weight for _, _, weight in scores)
    weighted_score = sum(score * weight for _, score, weight in scores)
    return weighted_score / total_weight if total_weight > 0 else 0.0


def _is_primary_source(item: RawNewsItem) -> bool:
    return is_primary_provenance(item.source_type, item.source_role) or (
        item.source_role == SourceRole.PRIMARY_EVIDENCE.value
    )


def _get_source_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _normalize_evidence_row(row: dict[str, Any], *, event_id: str = "") -> dict[str, Any]:
    out = dict(row)
    stype = normalize_source_type(out.get("source_type"))
    role = normalize_source_role(out.get("source_role") or out.get("role"), source_type=stype)
    out["source_type"] = stype
    out["source_role"] = role
    out["source_id"] = stable_source_id(out.get("source") or out.get("name"), out.get("url"), out.get("source_id"))
    if event_id and not out.get("event_id"):
        out["event_id"] = event_id
    if is_primary_provenance(stype, role):
        out["primary_evidence"] = True
    return out


@dataclass
class ExpansionResult:
    """Result of source expansion."""
    candidates_considered: int
    sources_matched: int
    sources_added: list[dict[str, Any]]
    primary_sources_retained: int
    independent_domains: list[str]
    failed_sources: list[dict[str, Any]]
    diagnostics: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates_considered": self.candidates_considered,
            "sources_matched": self.sources_matched,
            "sources_added": self.sources_added,
            "primary_sources_retained": self.primary_sources_retained,
            "independent_domains": self.independent_domains,
            "failed_sources": self.failed_sources,
            "diagnostics": self.diagnostics,
        }


def expand_sources_for_event(
    event: dict[str, Any],
    event_entities: list[str],
    event_topic: str,
    event_reports: list[dict[str, Any]],
    *,
    max_candidates: int = MAX_CANDIDATE_SOURCES,
    min_relevance: float = MIN_RELEVANCE_SCORE_DEFAULT,
    source_registry: SourceRegistry | None = None,
    collector: CollectorV2 | None = None,
    target_dimensions: list[str] | None = None,
    query_hints: list[str] | None = None,
) -> ExpansionResult:
    """Expand sources for an event using CollectorV2 + SourceRegistry (zero-LLM)."""
    import os

    # Offline / unit-test escape hatch — never touch the network.
    if os.environ.get("NEWSAGENT_V5_SKIP_SOURCE_EXPANSION", "").lower() in {
        "1", "true", "yes",
    }:
        return ExpansionResult(
            candidates_considered=0,
            sources_matched=0,
            sources_added=[],
            primary_sources_retained=0,
            independent_domains=[],
            failed_sources=[],
            diagnostics={
                "skipped": True,
                "reason": "NEWSAGENT_V5_SKIP_SOURCE_EXPANSION",
            },
        )

    event_title = str(event.get("representative_title") or event.get("canonical_title") or "").strip()
    event_id = str(event.get("event_id") or "")

    diagnostics: dict[str, Any] = {
        "queries_generated": [],
        "collection_started_at": datetime.now(timezone.utc).isoformat(),
        "target_dimensions": list(target_dimensions or []),
        "config_path": str(default_sources_config_path()),
    }

    queries: list[str] = []
    if query_hints:
        queries.extend(query_hints)
    elif target_dimensions:
        queries.extend(
            dimension_targeted_queries(event_title, event_entities, list(target_dimensions))
        )
    else:
        if event_title:
            queries.append(event_title)
        if event_entities:
            queries.append(" ".join(event_entities[:3]))
        if event_topic:
            queries.append(f"{event_topic} {' '.join(event_entities[:2])}")

    diagnostics["queries_generated"] = queries
    query_boost = " ".join(queries)

    registry = source_registry or SourceRegistry()
    enabled_sources = registry.get_enabled()

    # Prefer a shared CollectorV2 (run-scoped cache) so each feed is fetched once
    # per /make discovery across all candidates/expansion rounds.
    active_collector = collector or CollectorV2(max_workers=8, per_feed_limit=30)
    t_collect = time.perf_counter()
    all_items, collection_diagnostics = active_collector.collect(enabled_sources)
    collect_ms = (time.perf_counter() - t_collect) * 1000.0

    # Dedupe raw items by canonical URL before scoring/selection.
    deduped_items: list[RawNewsItem] = []
    seen_item_urls: set[str] = set()
    for item in all_items:
        key = (item.canonical_url or "").lower().rstrip("/")
        if not key or key in seen_item_urls:
            continue
        seen_item_urls.add(key)
        deduped_items.append(item)
    all_items = deduped_items

    diagnostics["collection_completed_at"] = datetime.now(timezone.utc).isoformat()
    diagnostics["sources_attempted"] = collection_diagnostics.sources_attempted
    diagnostics["sources_succeeded"] = collection_diagnostics.sources_succeeded
    diagnostics["sources_failed"] = collection_diagnostics.sources_failed
    diagnostics["raw_items_collected"] = collection_diagnostics.raw_items_collected
    diagnostics["registry_enabled"] = len(enabled_sources)
    diagnostics["collection_ms"] = round(collect_ms, 2)
    diagnostics["cache_hits"] = getattr(collection_diagnostics, "cache_hits", 0)
    diagnostics["cache_misses"] = getattr(collection_diagnostics, "cache_misses", 0)
    diagnostics["collection_errors"] = [
        {"source_id": e.get("source_id"), "error": e.get("error")}
        for e in collection_diagnostics.errors
    ]
    logger.info(
        "[DISCOVERY_TIMING] stage=expansion_feed_collect ms=%.1f cache_hits=%s cache_misses=%s items=%s",
        collect_ms,
        diagnostics["cache_hits"],
        diagnostics["cache_misses"],
        len(all_items),
    )

    existing_urls = {
        str(r.get("url") or "").lower().rstrip("/")
        for r in event_reports
        if r.get("url")
    }

    # Keep expansion in the same freshness window as /make discovery.
    from newsagent_v2.control.make_v5_bridge import discovery_max_age_hours
    from newsagent_v2.discovery.freshness import FreshnessConfig, FreshnessEngine

    expansion_freshness = FreshnessEngine(
        FreshnessConfig(max_age_hours=discovery_max_age_hours())
    )

    scored_candidates: list[tuple[RawNewsItem, float, bool]] = []
    for item in all_items:
        url_lower = item.canonical_url.lower().rstrip("/")
        if url_lower in existing_urls:
            continue
        if not expansion_freshness.check(item).accepted:
            continue
        score = _calculate_relevance_score(
            event_title, event_entities, event_topic, item, query_boost=query_boost
        )
        if score >= min_relevance:
            scored_candidates.append((item, score, _is_primary_source(item)))

    scored_candidates.sort(key=lambda x: (-int(x[2]), -x[1]))

    selected: list[tuple[RawNewsItem, float]] = []
    seen_domains: set[str] = set()
    for item, score, is_primary in scored_candidates:
        if len(selected) >= max_candidates:
            break
        domain = _get_source_domain(item.canonical_url)
        if domain in seen_domains and not is_primary:
            continue
        selected.append((item, score))
        if domain:
            seen_domains.add(domain)

    sources_added: list[dict[str, Any]] = []
    primary_count = 0
    for item, score in selected:
        evidence_row = _normalize_evidence_row(
            {
                "source": item.source,
                "source_id": item.source_id,
                "source_authority": item.source_authority,
                "url": item.canonical_url,
                "title": item.headline,
                "published": item.published_at,
                "summary": item.description,
                "source_role": item.source_role,
                "source_type": item.source_type,
                "relevance_score": round(score, 3),
                "expansion_match": True,
                "provenance": "registry_expansion",
                "event_id": event_id,
            },
            event_id=event_id,
        )
        if evidence_row.get("primary_evidence"):
            primary_count += 1
        sources_added.append(evidence_row)

    dim = evaluate_evidence_dimensions(
        list(event_reports) + sources_added, event_title=event_title
    )
    diagnostics["min_relevance_threshold"] = min_relevance
    diagnostics["max_candidates_limit"] = max_candidates
    diagnostics["scored_before_filter"] = len(scored_candidates)
    diagnostics["dimension_coverage"] = dim

    return ExpansionResult(
        candidates_considered=len(all_items),
        sources_matched=len(scored_candidates),
        sources_added=sources_added,
        primary_sources_retained=primary_count,
        independent_domains=list(seen_domains),
        failed_sources=[],
        diagnostics=diagnostics,
    )


def build_search_fn_for_event(
    event_entities: list[str],
    event_topic: str,
    event_reports: list[dict[str, Any]],
    *,
    source_registry: SourceRegistry | None = None,
    collector: CollectorV2 | None = None,
) -> Callable[[list[str], dict[str, Any]], list[dict[str, Any]]]:
    """Build a search_fn compatible with event_research.research_event.

    Uses registry/RSS expansion only (no X/Reddit/paid SERP).
    """
    registry = source_registry or SourceRegistry()

    def _search_fn(queries: list[str], working: dict[str, Any]) -> list[dict[str, Any]]:
        current_reports = list(event_reports)
        pack = working.get("article_input") if isinstance(working.get("article_input"), dict) else {}
        for row in pack.get("evidence") or []:
            if isinstance(row, dict) and row.get("url"):
                current_reports.append(row)

        dim = evaluate_evidence_dimensions(
            current_reports,
            event_title=str(
                working.get("representative_title")
                or working.get("canonical_title")
                or ""
            ),
        )
        hints = list(queries or [])
        if dim.get("missing"):
            hints.extend(
                dimension_targeted_queries(
                    str(working.get("representative_title") or ""),
                    event_entities,
                    list(dim["missing"]),
                )
            )
        result = expand_sources_for_event(
            event=working,
            event_entities=event_entities,
            event_topic=event_topic,
            event_reports=current_reports,
            source_registry=registry,
            collector=collector,
            target_dimensions=list(dim.get("missing") or []),
            query_hints=hints[:8],
            max_candidates=min(MAX_CANDIDATE_SOURCES, MAX_SEARCH_HITS),
        )
        # Preserve URL/source/provenance; cap total hits.
        return [_normalize_evidence_row(r) for r in result.sources_added[:MAX_SEARCH_HITS]]

    return _search_fn


def default_search_fn_for_story(
    story: dict[str, Any],
    *,
    source_registry: SourceRegistry | None = None,
) -> Callable[[list[str], dict[str, Any]], list[dict[str, Any]]]:
    """Production helper: build registry search_fn from a V4/V5 story dict."""
    entities = [str(e) for e in (story.get("entities") or []) if str(e).strip()]
    topic = str(story.get("topic") or "").strip()
    reports: list[dict[str, Any]] = []
    for key in ("evidence",):
        for row in story.get(key) or []:
            if isinstance(row, dict):
                reports.append(row)
    pack = story.get("article_input")
    if isinstance(pack, dict):
        for row in pack.get("evidence") or []:
            if isinstance(row, dict):
                reports.append(row)
    return build_search_fn_for_event(
        entities,
        topic,
        reports,
        source_registry=source_registry,
    )
