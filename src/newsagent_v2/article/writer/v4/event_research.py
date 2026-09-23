"""V4 multi-source event research. Discovery URL is a starting point only.

RAW WEB MATERIAL → extraction only. Never crosses into writer generation input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from newsagent_v2.article.enrich import (
    ATTRIBUTION_RE,
    FetchFn,
    enrich_story,
    evidence_sufficiency,
)
from newsagent_v2.article.qa.textutil import NUMBER_TOKEN_RE, word_count
from newsagent_v2.article.writer.controlled.capacity import article_input_for_ledgers
from newsagent_v2.discovery.provenance import (
    PRIMARY_ROLES as _PRIMARY_ROLES,
    PRIMARY_SOURCE_TYPES as _PRIMARY_SOURCE_TYPES,
    normalize_source_role,
    normalize_source_type,
)

# V4 fetches more independent pack sources than the shared default (2).
V4_MAX_SOURCES_PER_EVENT = 6

PRIMARY_SOURCE_TYPES = frozenset(set(_PRIMARY_SOURCE_TYPES) | {"regulator", "company", "official", "government", "official_regulator", "crypto_company", "exchange"})
PRIMARY_ROLES = frozenset(set(_PRIMARY_ROLES) | {"primary_evidence"})

SearchFn = Callable[[list[str], dict[str, Any]], list[dict[str, Any]]]


@dataclass
class EventResearchResult:
    pack: dict[str, Any]
    story: dict[str, Any]
    search_queries: list[str] = field(default_factory=list)
    sources_discovered: int = 0
    sources_retrieved: int = 0
    independent_sources: int = 0
    primary_sources: int = 0
    news_sources: int = 0
    raw_research_words: int = 0
    mode: str = "multi_source_event_research"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "search_queries": list(self.search_queries),
            "sources_discovered": self.sources_discovered,
            "sources_retrieved": self.sources_retrieved,
            "independent_sources": self.independent_sources,
            "primary_sources": self.primary_sources,
            "news_sources": self.news_sources,
            "raw_research_words": self.raw_research_words,
            "raw_research_retained_for": [
                "fact_extraction",
                "provenance",
                "quote_verification",
                "conflict_checking",
                "copyright_comparison",
            ],
            "writer_receives_raw_source_prose": False,
        }


def build_event_search_queries(story: dict[str, Any], pack: dict[str, Any]) -> list[str]:
    """Deterministic search queries from entities / action / asset / date / org."""
    title = str(
        story.get("representative_title")
        or pack.get("representative_title")
        or pack.get("title")
        or ""
    ).strip()
    entities: list[str] = []
    for row in pack.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        for key in ("source", "title"):
            token = str(row.get(key) or "").strip()
            if token and token not in entities and len(token.split()) <= 6:
                entities.append(token)
        blob = " ".join(
            str(row.get(k) or "")
            for k in ("title", "summary", "extracted_text")
        )
        for match in re.finditer(
            r"\b([A-Z][A-Za-z0-9&.-]*(?:\s+[A-Z][A-Za-z0-9&.-]*){0,3})\b",
            blob,
        ):
            name = match.group(1).strip()
            if len(name) > 2 and name not in entities and name.lower() not in {
                "the", "and", "for", "with"
            }:
                entities.append(name)
            if len(entities) >= 8:
                break
    action_terms = []
    for pattern in (
        r"\b(approv\w+|licen[cs]e\w*|custody|launch\w*|disclos\w*|hack\w*|"
        r"exploit\w*|filing|settlement|partnership|acquire\w*)\b",
    ):
        action_terms.extend(re.findall(pattern, title, flags=re.I))
    assets = re.findall(
        r"\b(Bitcoin|BTC|Ether|Ethereum|ETH|USDT|USDC|stablecoin\w*)\b",
        title + " " + " ".join(entities),
        flags=re.I,
    )
    queries: list[str] = []
    if title:
        queries.append(title)
    head_entities = entities[:3]
    if head_entities and action_terms:
        queries.append(" ".join(head_entities[:2] + [action_terms[0]]))
    if head_entities and assets:
        queries.append(" ".join(head_entities[:1] + list(assets[:2])))
    if head_entities:
        queries.append(" ".join(head_entities[:3]) + " official announcement")
        queries.append(" ".join(head_entities[:2]) + " regulator")
    # Dedupe preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        key = re.sub(r"\s+", " ", q.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(q.strip())
    return out[:8]


def _classify_source_row(row: dict[str, Any]) -> str:
    role = normalize_source_role(row.get("source_role"), source_type=row.get("source_type"))
    stype = normalize_source_type(row.get("source_type"))
    raw_type = str(row.get("source_type") or "").strip().lower()
    if role in PRIMARY_ROLES or stype in PRIMARY_SOURCE_TYPES or raw_type in PRIMARY_SOURCE_TYPES:
        return "primary"
    if role == "discovery" or stype in {"crypto_publication", "financial_news", "newsroom", "news", "press"} or raw_type in {"newsroom", "news", "press"}:
        return "news"
    return "other"


def _merge_search_hits(
    evidence: list[dict[str, Any]],
    hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_url = {
        str(row.get("url") or "").strip(): dict(row)
        for row in evidence
        if isinstance(row, dict) and str(row.get("url") or "").strip()
    }
    ordered = [dict(row) for row in evidence if isinstance(row, dict)]
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url = str(hit.get("url") or "").strip()
        if not url or url in by_url:
            continue
        row = dict(hit)
        row["source_role"] = normalize_source_role(hit.get("source_role") or row.get("source_role") or "search_hit", source_type=hit.get("source_type"))
        row["source_type"] = normalize_source_type(hit.get("source_type") or row.get("source_type"))
        if hit.get("source_id"):
            row["source_id"] = hit.get("source_id")
        if hit.get("provenance"):
            row["provenance"] = hit.get("provenance")
        elif not row.get("provenance"):
            row["provenance"] = "search_fn"
        row.setdefault("research_only", True)
        by_url[url] = row
        ordered.append(row)
    return ordered


def _source_census(evidence: list[dict[str, Any]]) -> dict[str, int]:
    hosts: set[str] = set()
    primary = 0
    news = 0
    retrieved = 0
    raw_words = 0
    for row in evidence:
        if not isinstance(row, dict):
            continue
        host = urlparse(str(row.get("url") or "")).netloc.lower()
        src = str(row.get("source") or "").strip()
        if host or src:
            hosts.add(host or src)
        kind = _classify_source_row(row)
        if kind == "primary":
            primary += 1
        elif kind == "news":
            news += 1
        text = str(row.get("extracted_text") or "").strip()
        if text:
            retrieved += 1
            raw_words += word_count(text)
        elif str(row.get("summary") or "").strip():
            # Summary counts as discovered but not full retrieval.
            pass
    return {
        "sources_discovered": len(evidence),
        "sources_retrieved": retrieved,
        "independent_sources": len(hosts),
        "primary_sources": primary,
        "news_sources": news,
        "raw_research_words": raw_words,
    }


def research_event(
    story: dict[str, Any],
    *,
    fetch: FetchFn | None = None,
    search_fn: SearchFn | None = None,
    max_sources: int = V4_MAX_SOURCES_PER_EVENT,
) -> EventResearchResult:
    """
    Discovery item → canonical event queries → multi-source retrieve/extract.

    Optional search_fn injects additional URL rows (tests / future adapters).
    Default path uses ranked discovery evidence with primary preference.
    """
    working = dict(story)
    raw_pack = dict(working.get("article_input") or {})
    queries = build_event_search_queries(working, raw_pack)
    evidence = [
        dict(row) for row in (raw_pack.get("evidence") or []) if isinstance(row, dict)
    ]
    if search_fn is not None:
        hits = search_fn(queries, working) or []
        evidence = _merge_search_hits(evidence, list(hits))
    raw_pack["evidence"] = evidence
    working["article_input"] = raw_pack

    enriched = enrich_story(working, fetch=fetch, max_sources=max_sources)
    pack = article_input_for_ledgers(
        enriched.get("article_input")
        if isinstance(enriched.get("article_input"), dict)
        else raw_pack
    )
    pack["evidence_is_research_only"] = True
    pack["event_research_mode"] = "multi_source_event_research"
    pack["search_queries"] = list(queries)
    # Ensure sufficiency metrics refreshed after merge.
    pack["evidence_sufficiency"] = evidence_sufficiency(pack)
    census = _source_census(
        [row for row in (pack.get("evidence") or []) if isinstance(row, dict)]
    )
    return EventResearchResult(
        pack=pack,
        story=enriched if isinstance(enriched, dict) else working,
        search_queries=queries,
        **census,
    )


def pack_excludes_generation_source_prose(generation_payload: dict[str, Any]) -> bool:
    """Hard boundary check for writer-facing payloads."""
    banned = {
        "extracted_text",
        "factual_snippets",
        "source_article_body",
        "raw_html",
        "html",
    }
    blob_keys = set(generation_payload.keys())
    if banned & blob_keys:
        return False

    def _walk(obj: Any) -> bool:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in banned:
                    return False
                if not _walk(value):
                    return False
        elif isinstance(obj, list):
            for item in obj:
                if not _walk(item):
                    return False
        return True

    return _walk(generation_payload)
