"""V5 Source Expansion Adapter - deterministic zero-LLM source expansion.

Expands research for an event by collecting from SourceRegistry and
matching entries deterministically against the event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from newsagent_v2.discovery.collector_v2 import CollectorV2
from newsagent_v2.discovery.raw_news_item import RawNewsItem
from newsagent_v2.discovery.source_registry import SourceRegistry, SourceRole


MAX_CANDIDATE_SOURCES = 12
MIN_RELEVANCE_SCORE = 0.3


def _token_overlap(text1: str, text2: str) -> float:
    """Calculate token overlap ratio between two texts."""
    tokens1 = set(re.findall(r'\b[a-z]+\b', text1.lower()))
    tokens2 = set(re.findall(r'\b[a-z]+\b', text2.lower()))
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    return len(intersection) / max(len(tokens1), len(tokens2))


def _entity_overlap(entities1: list[str], entities2: list[str]) -> float:
    """Calculate entity overlap ratio."""
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
) -> float:
    """Calculate relevance score for an item against the event.
    
    Score range: 0.0 - 1.0
    Higher is more relevant.
    """
    scores = []
    
    # Title overlap with event title
    title_overlap = _token_overlap(event_title, item.headline)
    scores.append(("title", title_overlap, 0.4))
    
    # Entity overlap
    entity_score = _entity_overlap(event_entities, item.entities)
    scores.append(("entity", entity_score, 0.35))
    
    # Topic match
    topic_match = 0.0
    if event_topic and item.topics:
        event_topic_lower = event_topic.lower()
        for topic in item.topics:
            if event_topic_lower in topic.lower() or topic.lower() in event_topic_lower:
                topic_match = 1.0
                break
    scores.append(("topic", topic_match, 0.15))
    
    # Keyword overlap
    keyword_score = _token_overlap(" ".join(event_entities), " ".join(item.keywords))
    scores.append(("keyword", keyword_score, 0.1))
    
    # Calculate weighted score
    total_weight = sum(weight for _, _, weight in scores)
    weighted_score = sum(score * weight for _, score, weight in scores)
    
    return weighted_score / total_weight if total_weight > 0 else 0.0


def _is_primary_source(item: RawNewsItem) -> bool:
    """Check if item is from a primary evidence source."""
    return item.source_role == SourceRole.PRIMARY_EVIDENCE.value


def _get_source_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


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
    min_relevance: float = MIN_RELEVANCE_SCORE,
) -> ExpansionResult:
    """Expand sources for an event using CollectorV2 + SourceRegistry.
    
    This is a ZERO-LLM deterministic expansion:
    1. Collect recent entries from enabled sources
    2. Match entries deterministically against event
    3. Use title/entity/topic/token overlap
    4. Keep only strongly relevant results
    5. Deduplicate URLs
    6. Prefer independent domains
    7. Prefer PRIMARY_EVIDENCE
    8. Hard-cap candidate count
    
    Args:
        event: Event data dict from NewsEvent
        event_entities: List of entities from the event
        event_topic: Primary topic of the event
        event_reports: Current reports in the event
        max_candidates: Maximum candidate sources to return
        min_relevance: Minimum relevance score to include
        
    Returns:
        ExpansionResult with matched sources
    """
    event_title = str(event.get("representative_title") or event.get("canonical_title") or "").strip()
    event_id = str(event.get("event_id") or "")
    
    diagnostics: dict[str, Any] = {
        "queries_generated": [],
        "collection_started_at": datetime.now(timezone.utc).isoformat(),
    }
    
    # Build search queries from event
    queries = []
    if event_title:
        queries.append(event_title)
    if event_entities:
        queries.append(" ".join(event_entities[:3]))
    if event_topic:
        queries.append(f"{event_topic} {' '.join(event_entities[:2])}")
    
    diagnostics["queries_generated"] = queries
    
    # Collect from source registry
    registry = SourceRegistry()
    enabled_sources = registry.get_enabled()
    
    collector = CollectorV2(max_workers=4, per_feed_limit=30)
    all_items, collection_diagnostics = collector.collect(enabled_sources)
    
    diagnostics["collection_completed_at"] = datetime.now(timezone.utc).isoformat()
    diagnostics["sources_attempted"] = collection_diagnostics.sources_attempted
    diagnostics["sources_succeeded"] = collection_diagnostics.sources_succeeded
    diagnostics["sources_failed"] = collection_diagnostics.sources_failed
    diagnostics["raw_items_collected"] = collection_diagnostics.raw_items_collected
    diagnostics["collection_errors"] = [
        {"source_id": e.get("source_id"), "error": e.get("error")}
        for e in collection_diagnostics.errors
    ]
    
    # Get existing URLs to avoid duplicates
    existing_urls = {
        str(r.get("url") or "").lower().rstrip("/")
        for r in event_reports
        if r.get("url")
    }
    
    # Score and filter candidates
    scored_candidates: list[tuple[RawNewsItem, float, bool]] = []
    for item in all_items:
        url_lower = item.canonical_url.lower().rstrip("/")
        if url_lower in existing_urls:
            continue
        
        score = _calculate_relevance_score(event_title, event_entities, event_topic, item)
        if score >= min_relevance:
            is_primary = _is_primary_source(item)
            scored_candidates.append((item, score, is_primary))
    
    # Sort by: primary source first, then score descending
    scored_candidates.sort(key=lambda x: (-int(x[2]), -x[1]))
    
    # Take top candidates with deduplication by domain
    selected: list[tuple[RawNewsItem, float]] = []
    seen_domains: set[str] = set()
    
    # First pass: prefer independent domains and primary sources
    for item, score, is_primary in scored_candidates:
        if len(selected) >= max_candidates:
            break
        
        domain = _get_source_domain(item.canonical_url)
        
        # Allow same domain if it's a primary source and we have room
        if domain in seen_domains and not is_primary:
            continue
            
        selected.append((item, score))
        seen_domains.add(domain)
    
    # Convert to evidence rows
    sources_added: list[dict[str, Any]] = []
    primary_count = 0
    failed_sources: list[dict[str, Any]] = []
    
    for item, score in selected:
        evidence_row = {
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
            "event_id": event_id,
        }
        
        # Track primary sources
        if _is_primary_source(item):
            primary_count += 1
            evidence_row["primary_evidence"] = True
        
        sources_added.append(evidence_row)
    
    diagnostics["min_relevance_threshold"] = min_relevance
    diagnostics["max_candidates_limit"] = max_candidates
    diagnostics["scored_before_filter"] = len(scored_candidates)
    
    return ExpansionResult(
        candidates_considered=len(all_items),
        sources_matched=len(scored_candidates),
        sources_added=sources_added,
        primary_sources_retained=primary_count,
        independent_domains=list(seen_domains),
        failed_sources=failed_sources,
        diagnostics=diagnostics,
    )


def build_search_fn_for_event(
    event_entities: list[str],
    event_topic: str,
    event_reports: list[dict[str, Any]],
) -> callable:
    """Build a search_fn compatible with event_research.research_event.
    
    Returns a function that matches the SearchFn protocol:
    Callable[[list[str], dict[str, Any]], list[dict[str, Any]]]
    """
    def _search_fn(queries: list[str], working: dict[str, Any]) -> list[dict[str, Any]]:
        """Search function compatible with event_research.SearchFn."""
        result = expand_sources_for_event(
            event=working,
            event_entities=event_entities,
            event_topic=event_topic,
            event_reports=event_reports,
        )
        return result.sources_added
    
    return _search_fn
