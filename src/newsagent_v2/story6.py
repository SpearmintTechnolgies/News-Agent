"""V6 story pipeline: research -> fact bank -> evidence gate -> write + QA.

Produces the article dictionary consumed by the existing image, WordPress and
Telegram review code, or a plain-language reason the story was not written.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from newsagent_v2.facts import build_fact_bank, qualify
from newsagent_v2.qa6 import STATUS_BLOCKED, STATUS_REVIEW, DraftOutcome, write_and_check
from newsagent_v2.research import deep_research
from newsagent_v2.research.dossier import ResearchDossier, display_publisher
from newsagent_v2.seo6 import finalize_seo
from newsagent_v2.write import Article, KimiClient, StoryBudget

logger = logging.getLogger(__name__)

# Recent research, so /make can reject a thin story before a card is sent
# and the writer can reuse that research instead of fetching it again.
_SCREEN_TTL_SECONDS = 2 * 60 * 60
_SCREEN_LOCK = threading.Lock()
_SCREEN_CACHE: dict[str, tuple[float, ResearchDossier | None, Any]] = {}

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


def _active_site_id() -> str:
    try:
        from newsagent_v2.wordpress import authors

        return str(authors.active_site_id or "").strip()
    except Exception:
        return ""


def _category_on_active_site(writer_category: str) -> tuple[str, int]:
    """The tapped category id, or the Coin Network name map when that site has no choice."""
    try:
        from newsagent_v2.control.site_flow import current_category_id, current_category_name
        from newsagent_v2.control.sites import COIN_NETWORK_SITE_ID

        chosen_id = current_category_id()
        chosen_name = current_category_name()
    except Exception:
        chosen_id, chosen_name = 0, ""
    if chosen_id:
        return chosen_name or SITE_CATEGORIES.get(writer_category, "News"), chosen_id
    site_id = _active_site_id()
    if site_id and site_id != COIN_NETWORK_SITE_ID:
        return "", 0
    return SITE_CATEGORIES.get(writer_category, "News"), 0


def article_record(
    article: Article,
    *,
    event_id: str,
    dossier: ResearchDossier,
    outcome: DraftOutcome,
) -> dict[str, Any]:
    """The article in the shape the image, WordPress and Telegram code read."""
    seo = article.seo
    category, category_id = _category_on_active_site(seo.category)
    report = outcome.report
    record = finalize_seo({
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
        "categories": [category] if category else [],
        "tags": list(seo.tags),
        "topic": category,
        "entities": [{"name": name} for name in dossier.entities[:8]],
        "sources": source_list(dossier),
        "body_words": article.body_words,
        "total_words": article.total_words,
        "qa_flags": [i.to_dict() for i in report.issues] if report else [],
        "structured": article.to_dict(),
    })
    if category_id:
        record["wp_category_id"] = category_id
    elif _active_site_id():
        from newsagent_v2.control.sites import COIN_NETWORK_SITE_ID

        if _active_site_id() != COIN_NETWORK_SITE_ID:
            record["category_choice_missing"] = True
    return record


def _cached_screen(event_id: str) -> tuple[ResearchDossier | None, Any] | None:
    with _SCREEN_LOCK:
        hit = _SCREEN_CACHE.get(event_id)
    if hit is None or time.monotonic() - hit[0] > _SCREEN_TTL_SECONDS:
        return None
    return hit[1], hit[2]


def _remember_screen(event_id: str, dossier: ResearchDossier | None, gate: Any) -> None:
    with _SCREEN_LOCK:
        _SCREEN_CACHE[event_id] = (time.monotonic(), dossier, gate)


def story_urls(event: Any, dossier: ResearchDossier) -> list[str]:
    urls = [str(getattr(report, "url", "") or "") for report in getattr(event, "reports", []) or []]
    urls.extend(doc.url for doc in dossier.sources)
    urls.extend(doc.url for doc in dossier.rejected)
    return [url for url in urls if url]


def screen_story(event: Any) -> tuple[bool, str]:
    """Research a story and say whether it can support a full article.

    A failing result is cached, so the same story is not offered again and a
    later RUN STORY on an old card returns the same reason without another fetch.
    """
    cached = _cached_screen(event.event_id)
    if cached is not None:
        return cached[1].passed, cached[1].summary()
    dossier = deep_research(story_from_event(event))
    bank = build_fact_bank(dossier)
    gate = qualify(dossier, bank, story_urls(event, dossier))
    _remember_screen(event.event_id, dossier if gate.passed else None, gate)
    logger.info("[STORY6] screen event=%s passed=%s %s", event.event_id, gate.passed, gate.summary())
    return gate.passed, gate.summary()


def run_story(
    event: Any,
    environ: Mapping[str, str],
    *,
    client: KimiClient | None = None,
    research_fn: Callable[[dict[str, Any]], ResearchDossier] | None = None,
    budget: StoryBudget | None = None,
    feedback: str = "",
) -> StoryResult:
    """``feedback``: the editor's REVISE note on the previous version, passed to the writer."""
    started = time.monotonic()
    budget = budget or StoryBudget()
    story = story_from_event(event)

    cached = None if research_fn else _cached_screen(event.event_id)
    if cached is not None and cached[0] is None and not cached[1].passed:
        gate = cached[1]
        result = StoryResult(
            outcome=OUTCOME_SKIPPED,
            reason=gate.summary(),
            gate=gate.to_dict(),
            seconds=time.monotonic() - started,
        )
        logger.info("[STORY6] event=%s skipped from recent screen: %s", event.event_id, result.reason)
        return result

    dossier = cached[0] if cached and cached[0] is not None else (research_fn or deep_research)(story)
    bank = build_fact_bank(dossier)
    gate = qualify(dossier, bank, story_urls(event, dossier))
    if research_fn is None:
        _remember_screen(event.event_id, dossier if gate.passed else None, gate)
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
    outcome = write_and_check(bank, dossier, client, budget, feedback=feedback)
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
