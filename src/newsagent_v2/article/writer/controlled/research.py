"""Research enrichment: retrieved source bodies become ledgers. No model-generated facts."""

from __future__ import annotations

from typing import Any, Callable

from newsagent_v2.article.enrich import enrich_story
from newsagent_v2.article.writer.controlled.capacity import (
    analyze_evidence_capacity,
    article_input_for_ledgers,
)
from newsagent_v2.article.writer.controlled.plan import plan_article
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers

EnrichFn = Callable[..., dict[str, Any]]


def research_story(story: dict[str, Any], *, enrich_fn: EnrichFn | None = None) -> dict[str, Any]:
    """Fetch/extract already-selected coverage, then build ledgers from retrieved text only."""
    enriched = (enrich_fn or enrich_story)(story)
    raw = enriched.get("article_input") if isinstance(enriched.get("article_input"), dict) else {}
    pack = article_input_for_ledgers(raw)
    units = pack.get("evidence_units") if isinstance(pack.get("evidence_units"), list) else []
    retrieved = [
        item
        for item in (pack.get("evidence") or [])
        if isinstance(item, dict) and str(item.get("extracted_text") or item.get("summary") or "").strip()
    ]
    ledgers = build_evidence_ledgers(pack)
    capacity = analyze_evidence_capacity(ledgers)
    plan = plan_article(ledgers, pack)
    return {
        "story": enriched,
        "pack": pack,
        "ledgers": ledgers,
        "capacity": capacity,
        "plan": plan,
        "retrieved_source_count": len(retrieved),
        "evidence_unit_count": len(units),
        "claim_count": len(ledgers.claims),
        "quote_count": len(ledgers.quotes),
        "model_generated_evidence": False,
        "discovery_is_not_writing_evidence": True,
    }
