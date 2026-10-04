"""V6 story pipeline: research -> fact bank -> evidence gate -> write + QA.

Produces the article dictionary consumed by the existing image, WordPress and
Telegram review code, or a plain-language reason the story was not written.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from newsagent_v2.facts import assess_evidence, build_fact_bank
from newsagent_v2.qa6 import STATUS_BLOCKED, STATUS_REVIEW, DraftOutcome, write_and_check
from newsagent_v2.research import deep_research
from newsagent_v2.research.dossier import ResearchDossier, display_publisher
from newsagent_v2.write import Article, KimiClient, StoryBudget

logger = logging.getLogger(__name__)

OUTCOME_READY = "ready"
OUTCOME_SKIPPED = "skipped"
OUTCOME_BLOCKED = "blocked"
OUTCOME_FAILED = "failed"

# Writer categories -> existing categories on the WordPress site.
SITE_CATEGORIES = {
    "Bitcoin": "Bitcoin",
    "Ethereum": "Ethereum",
    "Altcoins": "Altcoin News",
    "Markets": "Market News",
    "Regulation": "Policy & Regulations",
    "Policy": "Policy & Regulations",
    "Business": "Business",
    "DeFi": "DeFi",
    "Stablecoins": "Market News",
    "AI": "Tech",
}


@dataclass
class StoryResult:
    outcome: str
    reason: str = ""
    article: dict[str, Any] | None = None
    qa: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)
    research: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome == OUTCOME_READY

    def diagnostics(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "gate": self.gate,
            "research": self.research,
            "qa_summary": self.qa.get("summary"),
            "budget": {k: v for k, v in self.budget.items() if k != "log"},
            "history": self.history,
            "seconds": round(self.seconds, 1),
        }


def story_from_event(event: Any) -> dict[str, Any]:
    rows = [
        {"source": r.source, "url": r.url, "title": r.headline, "published": r.published_at, "summary": r.description}
        for r in event.reports
    ]
    return {
        "event_id": event.event_id,
        "representative_title": event.canonical_title,
        "entities": sorted(str(e) for e in event.entities or []),
        "article_input": {"evidence": rows},
    }


def source_list(dossier: ResearchDossier) -> list[dict[str, str]]:
    return [
        {"url": doc.url, "source": display_publisher(doc.publisher or doc.host), "title": doc.title or ""}
        for doc in dossier.full_sources
    ]


def article_record(
    article: Article,
    *,
    event_id: str,
    dossier: ResearchDossier,
    outcome: DraftOutcome,
) -> dict[str, Any]:
    """The article in the shape the image, WordPress and Telegram code read."""
    seo = article.seo
    category = SITE_CATEGORIES.get(seo.category, "News")
    report = outcome.report
    return {
        "event_id": event_id,
        "pipeline": "v6",
        "preserve_structure": True,
        "headline": article.headline,
        "dek": article.dek,
        "article_body": article.to_markdown(),
        "seo_title": seo.meta_title,
        "meta_description": seo.meta_description,
        "focus_keyphrase": seo.focus_keyword,
        "keywords": [seo.focus_keyword, *seo.tags],
        "slug": seo.slug,
        "category": category,
        "categories": [category],
        "tags": list(seo.tags),
        "topic": category,
        "entities": [{"name": name} for name in dossier.entities[:8]],
        "sources": source_list(dossier),
        "body_words": article.body_words,
        "total_words": article.total_words,
        "qa_flags": [i.to_dict() for i in report.issues] if report else [],
        "structured": article.to_dict(),
    }


def run_story(
    event: Any,
    environ: Mapping[str, str],
    *,
    client: KimiClient | None = None,
    research_fn: Callable[[dict[str, Any]], ResearchDossier] | None = None,
    budget: StoryBudget | None = None,
) -> StoryResult:
    started = time.monotonic()
    budget = budget or StoryBudget()
    story = story_from_event(event)

    dossier = (research_fn or deep_research)(story)
    bank = build_fact_bank(dossier)
    gate = assess_evidence(dossier, bank)
    result = StoryResult(
        outcome=OUTCOME_SKIPPED,
        gate={"passed": gate.passed, "reasons": list(gate.reasons), "metrics": dict(gate.metrics)},
        research=dossier.stats(),
    )
    if not gate.passed:
        result.reason = gate.summary()
        result.seconds = time.monotonic() - started
        logger.info("[STORY6] event=%s skipped: %s", event.event_id, result.reason)
        return result

    client = client or KimiClient.from_env(environ)
    outcome = write_and_check(bank, dossier, client, budget)
    result.budget = outcome.budget
    result.history = outcome.history
    if outcome.report:
        result.qa = {**outcome.report.to_dict(), "summary": outcome.report.summary()}

    if outcome.status == STATUS_REVIEW and outcome.article:
        result.outcome = OUTCOME_READY
        result.article = article_record(outcome.article, event_id=event.event_id, dossier=dossier, outcome=outcome)
    elif outcome.status == STATUS_BLOCKED:
        result.outcome = OUTCOME_BLOCKED
        blocking = [i for i in outcome.report.issues if i.severity == "block"] if outcome.report else []
        details = "; ".join(f"{i.location}: {i.message}" for i in blocking[:3])
        result.reason = f"Blocked by QA after {outcome.revisions} revision(s): {details}"
    else:
        result.outcome = OUTCOME_FAILED
        result.reason = f"Writer failed: {outcome.error or 'no article returned'}"
    result.seconds = time.monotonic() - started
    logger.info("[STORY6] event=%s outcome=%s %s", event.event_id, result.outcome, result.reason)
    return result
