"""V5 /make bridge: Discovery-only, Top 5 → Telegram.

This module provides the V5 discovery pipeline that:
1. Runs Collector V2 (zero cost)
2. Ranks events by intelligence
3. Sends Top 5 to Telegram as separate cards
4. Supports SEE NEXT 5 pagination

RUN STORY only marks selection, does NOT invoke writer/image.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from time import perf_counter
from typing import Any, Callable

from pathlib import Path

from newsagent_v2.discovery.collector_v2 import CollectorV2
from newsagent_v2.discovery.deduplicator import DeduplicationEngine
from newsagent_v2.discovery.event_clusterer import EventClusterer, EventReport, NewsEvent, ClusterConfig
from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.discovery.freshness import FreshnessEngine, FreshnessConfig
from newsagent_v2.intelligence.engine import IntelligenceEngine
from newsagent_v2.discovery.niche_filter import NicheFilter
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.article_readiness import preflight_article_readiness
from newsagent_v2.v5_generation.depth_cost_helpers import evidence_supports_publishable_depth
from newsagent_v2.cluster import EventCluster
from newsagent_v2.models import NewsItem
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
from newsagent_v2.telegram.v5_cards import render_card_from_event, seepage_keyboard
from newsagent_v2.v5_generation.source_expansion_adapter import expand_sources_for_event
from newsagent_v2.v5_generation.evidence_dimensions import (
    MAX_EXPANSION_ROUNDS,
    MAX_TOTAL_EVIDENCE_ROWS,
    evaluate_evidence_dimensions,
    dimension_targeted_queries,
)


MAX_SOURCE_EXPANSION_ROUNDS = MAX_EXPANSION_ROUNDS
logger = logging.getLogger(__name__)


def _log_discovery_timing(stage: str, started: float, **extra: Any) -> float:
    ms = (perf_counter() - started) * 1000.0
    extras = " ".join(f"{k}={v}" for k, v in extra.items())
    if extras:
        logger.info("[DISCOVERY_TIMING] stage=%s ms=%.1f %s", stage, ms, extras)
    else:
        logger.info("[DISCOVERY_TIMING] stage=%s ms=%.1f", stage, ms)
    return ms


class V5DiscoveryPipeline:
    """V5 discovery pipeline: Collector → Rank → Send Top N.
    
    Zero paid model costs.
    """
    
    def __init__(
        self,
        event_store: EventStore | None = None,
        source_registry: SourceRegistry | None = None,
        telegram_store: V5TelegramStore | None = None,
        max_workers: int = 8,
    ) -> None:
        self.event_store = event_store or EventStore(root=Path("./data/events"))
        self.source_registry = source_registry or SourceRegistry()
        self.telegram_store = telegram_store or V5TelegramStore()
        self.max_workers = max_workers
        
        # Discovery cost tracking
        self._discovery_llm_calls = 0
        self._writer_calls = 0
        self._image_calls = 0
        self._research_calls = 0
        self._cost_inr = 0.0
        self._start_time = 0.0
        
        # Results
        self._ranked_events: list[NewsEvent] = []
        self._source_expansion_diagnostics: dict[str, Any] = {}
        self._run_collector: CollectorV2 | None = None
        self._discovery_timings: dict[str, float] = {}

    @staticmethod
    def _readiness_cluster(event: NewsEvent) -> EventCluster:
        return EventCluster(
            event_id=event.event_id,
            representative=NewsItem(
                source=event.reports[0].source,
                title=event.canonical_title,
                url=event.reports[0].url,
                published=event.reports[0].published_at,
                summary=event.reports[0].description,
                source_type="newsroom",
                source_role="discovery",
                source_authority=event.reports[0].source_authority,
            ),
            members=[
                NewsItem(
                    source=report.source,
                    title=report.headline,
                    url=report.url,
                    published=report.published_at,
                    summary=report.description,
                    source_type="newsroom",
                    source_role="discovery",
                    source_authority=report.source_authority,
                )
                for report in event.reports
            ],
        )

    @staticmethod
    def _append_expansion_reports(event: NewsEvent, rows: list[dict[str, Any]]) -> int:
        existing_urls = {
            str(report.url or "").strip().lower().rstrip("/")
            for report in event.reports
            if str(report.url or "").strip()
        }
        added = 0
        for row in rows:
            url = str(row.get("url") or "").strip()
            key = url.lower().rstrip("/")
            if not key or key in existing_urls:
                continue
            report_id = "exp-" + hashlib.sha1(
                f"{event.event_id}:{key}".encode("utf-8")
            ).hexdigest()[:16]
            event.reports.append(
                EventReport(
                    report_id=report_id,
                    source=str(row.get("source") or "").strip(),
                    source_id=str(row.get("source_id") or "").strip(),
                    source_authority=float(row.get("source_authority") or 0.0),
                    headline=str(row.get("title") or "").strip(),
                    url=url,
                    published_at=row.get("published"),
                    retrieved_at=str(row.get("retrieved_at") or ""),
                    description=str(row.get("summary") or "").strip(),
                    entities=[],
                    raw_item_id=report_id,
                )
            )
            existing_urls.add(key)
            added += 1
        return added

    def _expand_until_ready(self, event: NewsEvent, *, collector: CollectorV2 | None = None) -> tuple[bool, dict[str, Any]]:
        initial_sources = len({report.source for report in event.reports if report.source})
        initial_urls = len({report.url.lower().rstrip("/") for report in event.reports if report.url})
        attempts: list[dict[str, Any]] = []
        expansion_rounds: list[dict[str, Any]] = []

        for attempt in range(MAX_SOURCE_EXPANSION_ROUNDS + 1):
            cluster = self._readiness_cluster(event)
            ready_clusters, audit = preflight_article_readiness([cluster])
            result = audit[event.event_id]
            attempts.append({
                "attempt": attempt,
                "stage": "initial" if attempt == 0 else "after_expansion",
                "eligible": result["eligible"],
                "reasons": result["reasons"],
                "metrics": result["metrics"],
                "source_count": len({report.source for report in event.reports if report.source}),
                "url_count": len({report.url.lower().rstrip("/") for report in event.reports if report.url}),
            })
            if ready_clusters:
                depth_ok, depth_reasons = evidence_supports_publishable_depth(result.get("metrics"))
                attempts[-1]["publishable_depth_ok"] = depth_ok
                attempts[-1]["publishable_depth_reasons"] = depth_reasons
                if depth_ok:
                    break
                # Ready for thin readiness but not publishable depth — keep expanding.
                attempts[-1]["eligible"] = False
                attempts[-1]["reasons"] = list(result.get("reasons") or []) + depth_reasons
            if attempt >= MAX_SOURCE_EXPANSION_ROUNDS:
                break

            try:
                report_dicts = [report.to_dict() for report in event.reports]
                dim = evaluate_evidence_dimensions(
                    report_dicts, event_title=event.canonical_title
                )
                missing = list(dim.get("missing") or [])
                hints = (
                    dimension_targeted_queries(
                        event.canonical_title, list(event.entities), missing
                    )
                    if missing
                    else None
                )
                expansion = expand_sources_for_event(
                    event={
                        "event_id": event.event_id,
                        "representative_title": event.canonical_title,
                        "canonical_title": event.canonical_title,
                        "topic": event.topic,
                        "entities": list(event.entities),
                    },
                    event_entities=list(event.entities),
                    event_topic=event.topic,
                    event_reports=report_dicts,
                    source_registry=self.source_registry,
                    collector=collector or self._run_collector,
                    target_dimensions=missing or None,
                    query_hints=hints,
                )
                room = max(0, MAX_TOTAL_EVIDENCE_ROWS - len(event.reports))
                rows = expansion.sources_added[:room]
                added = self._append_expansion_reports(event, rows)
                post_dim = evaluate_evidence_dimensions(
                    [report.to_dict() for report in event.reports],
                    event_title=event.canonical_title,
                )
                expansion_rounds.append({
                    "round": attempt + 1,
                    "candidates_considered": expansion.candidates_considered,
                    "sources_discovered": expansion.sources_matched,
                    "sources_added": added,
                    "deduped_source_count": len({report.source for report in event.reports if report.source}),
                    "dimension_coverage_before": dim,
                    "dimension_coverage_after": post_dim,
                    "target_dimensions": missing,
                    "diagnostics": expansion.diagnostics,
                })
                if added == 0 or post_dim.get("sufficient"):
                    # Stop when no new material or dimension coverage satisfied (final readiness below).
                    break
            except Exception as exc:
                expansion_rounds.append({
                    "round": attempt + 1,
                    "sources_added": 0,
                    "error": str(exc)[:300],
                })
                break

        final_cluster = self._readiness_cluster(event)
        final_ready, final_audit = preflight_article_readiness([final_cluster])
        final_result = final_audit[event.event_id]
        depth_ok, depth_reasons = evidence_supports_publishable_depth(final_result.get("metrics"))
        if final_ready and not depth_ok:
            final_ready = []
            final_result = dict(final_result)
            final_result["eligible"] = False
            final_result["reasons"] = list(final_result.get("reasons") or []) + depth_reasons
            final_result["publishable_depth_ok"] = False
            final_result["publishable_depth_reasons"] = depth_reasons
        else:
            final_result = dict(final_result)
            final_result["publishable_depth_ok"] = depth_ok
            final_result["publishable_depth_reasons"] = depth_reasons
        return bool(final_ready), {
            "event_id": event.event_id,
            "initial_source_count": initial_sources,
            "initial_url_count": initial_urls,
            "sources_discovered": sum(row.get("sources_discovered", 0) for row in expansion_rounds),
            "deduped_source_count": len({report.source for report in event.reports if report.source}),
            "deduped_url_count": len({report.url.lower().rstrip("/") for report in event.reports if report.url}),
            "readiness_attempts": attempts,
            "expansion_rounds": expansion_rounds,
            "final_status": "PASS" if final_ready else "FAIL",
            "final_readiness": final_result,
            "final_reasons": final_result["reasons"],
        }
    
    def run_discovery(self) -> list[NewsEvent]:
        """Run full V5 discovery pipeline.
        
        Returns ranked list of NewsEvents.
        """
        total_start = perf_counter()
        self._start_time = total_start
        self._discovery_timings = {}

        # 1. Collect from sources once (run-scoped cache shared with expansion).
        t0 = perf_counter()
        sources = self.source_registry.get_enabled()
        collector = CollectorV2(max_workers=self.max_workers, enable_run_cache=True)
        collector.begin_run()
        self._run_collector = collector
        raw_items, diagnostics = collector.collect(sources)
        self._discovery_timings["registry_feed_collection"] = _log_discovery_timing(
            "registry_feed_collection",
            t0,
            feeds=len(sources),
            items=len(raw_items),
            cache_hits=getattr(diagnostics, "cache_hits", 0),
            cache_misses=getattr(diagnostics, "cache_misses", 0),
        )

        # 2. FreshnessEngine and NicheFilter expect RawNewsItem
        normalized = raw_items

        # 3. Freshness filter
        t1 = perf_counter()
        freshness = FreshnessEngine(FreshnessConfig(max_age_hours=48))
        fresh_items = []
        for item in normalized:
            result = freshness.check(item)
            if result.accepted:
                fresh_items.append(item)

        # 4. Niche filter
        from newsagent_v2.discovery.niche_filter import RelevanceDecision
        niche = NicheFilter()
        relevant_items = []
        for item in fresh_items:
            decision, reason, confidence = niche.classify(item)
            if decision in (RelevanceDecision.KEEP, RelevanceDecision.CONDITIONAL):
                relevant_items.append(item)

        # 5. Deduplicate
        deduper = DeduplicationEngine()
        unique_items, _ = deduper.dedupe_batch(relevant_items)
        self._discovery_timings["candidate_creation_dedupe"] = _log_discovery_timing(
            "candidate_creation_dedupe",
            t1,
            fresh=len(fresh_items),
            relevant=len(relevant_items),
            unique=len(unique_items),
        )

        # 6. Cluster into events
        t2 = perf_counter()
        clusterer = EventClusterer(ClusterConfig())
        events = clusterer.cluster_batch(unique_items)

        # 7. Analyze with intelligence
        intelligence = IntelligenceEngine()
        for event in events:
            event.intelligence = intelligence.analyze(event)

        # 8. Rank by combined score (breaking + momentum) — highest first.
        ranked_events = self._rank_events(events)
        self._discovery_timings["ranking"] = _log_discovery_timing(
            "ranking",
            t2,
            events=len(events),
            ranked=len(ranked_events),
        )

        # Discovery path does not HTTP-fetch article bodies (RSS summaries only).
        self._discovery_timings["http_evidence_fetch_extraction"] = _log_discovery_timing(
            "http_evidence_fetch_extraction",
            perf_counter(),
            skipped=1,
            reason="rss_only_at_discovery",
        )

        # 9. Expand evidence before applying the unchanged readiness gate.
        # Deeper expansion only for candidates that may enter Top-5; stop once 5
        # satisfy the 600+ evidence-capacity requirement.
        selected: list[NewsEvent] = []
        diag_rows: list[dict[str, Any]] = []
        expansion_ms = 0.0
        dimension_gap_ms = 0.0
        readiness_ms = 0.0
        backfill_considered = 0
        t_expand_all = perf_counter()
        for event in ranked_events:
            if not event.reports:
                continue
            if len(selected) >= 5:
                break
            backfill_considered += 1
            t_ev = perf_counter()
            ready, event_diagnostics = self._expand_until_ready(
                event, collector=collector
            )
            ev_ms = (perf_counter() - t_ev) * 1000.0
            expansion_ms += ev_ms
            # Approximate split from expansion round diagnostics when present.
            rounds = event_diagnostics.get("expansion_rounds") or []
            for row in rounds:
                d = row.get("diagnostics") or {}
                dimension_gap_ms += float(d.get("collection_ms") or 0.0)
            attempts = event_diagnostics.get("readiness_attempts") or []
            # readiness evaluations are cheap relative to feed I/O; attribute remainder.
            readiness_ms += max(0.0, ev_ms - sum(
                float((r.get("diagnostics") or {}).get("collection_ms") or 0.0) for r in rounds
            ))
            logger.info(
                "[DISCOVERY_TIMING] stage=candidate_expand_until_ready ms=%.1f event_id=%s ready=%s rounds=%s",
                ev_ms,
                event.event_id,
                ready,
                len(rounds),
            )
            diag_rows.append(event_diagnostics)
            if ready:
                selected.append(event)
            if len(selected) >= 5:
                logger.info(
                    "[DISCOVERY_TIMING] stage=early_stop_top5 ms=%.1f selected=5 considered=%s",
                    (perf_counter() - t_expand_all) * 1000.0,
                    backfill_considered,
                )
                break

        self._discovery_timings["initial_source_expansion"] = round(expansion_ms, 1)
        self._discovery_timings["dimension_gap_expansion"] = round(dimension_gap_ms, 1)
        self._discovery_timings["readiness_depth_evaluation"] = round(readiness_ms, 1)
        self._discovery_timings["thin_story_backfill"] = _log_discovery_timing(
            "thin_story_backfill",
            t_expand_all,
            considered=backfill_considered,
            selected=len(selected),
            failed=sum(1 for row in diag_rows if row.get("final_status") == "FAIL"),
        )
        logger.info(
            "[DISCOVERY_TIMING] stage=initial_source_expansion ms=%.1f",
            expansion_ms,
        )
        logger.info(
            "[DISCOVERY_TIMING] stage=dimension_gap_expansion ms=%.1f",
            dimension_gap_ms,
        )
        logger.info(
            "[DISCOVERY_TIMING] stage=readiness_depth_evaluation ms=%.1f",
            readiness_ms,
        )

        self._ranked_events = selected
        self._source_expansion_diagnostics = {
            "candidate_count_before_readiness": len(ranked_events),
            "final_pass_count": len(selected),
            "final_fail_count": sum(row["final_status"] == "FAIL" for row in diag_rows),
            "candidates": diag_rows,
            "collector_cache": collector.cache_stats(),
            "timings_ms": dict(self._discovery_timings),
        }
        Path("output").mkdir(parents=True, exist_ok=True)
        Path("output/article_readiness.json").write_text(
            json.dumps(
                {row["event_id"]: row["final_readiness"] for row in diag_rows},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        Path("output/source_expansion_readiness.json").write_text(
            json.dumps(self._source_expansion_diagnostics, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 10. Save to store
        self.event_store.save_batch(self._ranked_events)

        # 11. Save to telegram store for pagination
        self.telegram_store.save_batch(self._ranked_events)

        self._discovery_timings["total_make_discovery"] = _log_discovery_timing(
            "total_make_discovery",
            total_start,
            selected=len(selected),
            feeds=len(sources),
            cache=collector.cache_stats().get("cached_feeds"),
        )
        return self._ranked_events

    def _rank_events(self, events: list[NewsEvent]) -> list[NewsEvent]:
        """Rank events by intelligence signals."""
        def score(event: NewsEvent) -> float:
            if event.intelligence is None:
                return 0.0
            # Breaking score + momentum + novelty
            s = event.intelligence.breaking.score * 3.0  # Weight breaking heavily
            s += event.intelligence.momentum.score * 2.0
            s += event.intelligence.novelty.score * 1.0
            return s
        
        return sorted(events, key=score, reverse=True)
    
    def get_top_events(self, count: int = 5, offset: int = 0) -> list[NewsEvent]:
        """Get events by rank position."""
        return self._ranked_events[offset:offset + count]
    
    def send_to_telegram(
        self,
        client: TelegramTestClient,
        config: TelegramConfig,
        count: int = 5,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Send ranked events to Telegram as separate cards."""
        events = self.get_top_events(count=count, offset=offset)
        total = len(self._ranked_events)
        
        results = []
        
        for i, event in enumerate(events):
            rank = offset + i + 1
            card = render_card_from_event(event, rank, total)
            
            # Send as message with keyboard
            result = client.send_message(
                chat_id=config.test_chat_id,
                text=card["text"],
                parse_mode=card["parse_mode"],
                reply_markup=card["reply_markup"],
                disable_web_page_preview=True,
            )
            results.append(result)
        
        # After sending last card, add SEE NEXT if there are more
        if offset + count < total and count > 0:
            next_result = client.send_message(
                chat_id=config.test_chat_id,
                text=f"📄 Showing {min(offset + count, total)}/{total} events",
                parse_mode="HTML",
                reply_markup=seepage_keyboard(offset=offset + count),
            )
            results.append(next_result)
        
        return results
    
    def get_cost_report(self) -> dict[str, Any]:
        """Discovery cost report (should be zero)."""
        return {
            "discovery_llm_calls": self._discovery_llm_calls,
            "research_llm_calls": self._research_calls,
            "writer_calls": self._writer_calls,
            "image_network_calls": self._image_calls,
            "estimated_cost_inr": self._cost_inr,
        }
    
    def callback_handler(self) -> Callable[..., dict[str, Any]]:
        """Get callback handler for this pipeline."""
        handler = V5CallbackHandler(
            event_store=self.event_store,
            telegram_store=self.telegram_store,
        )
        return handler.handle


def create_v5_discovery_pipeline(
    event_store: EventStore | None = None,
    source_registry: SourceRegistry | None = None,
    telegram_store: V5TelegramStore | None = None,
) -> V5DiscoveryPipeline:
    """Factory for V5 discovery pipeline."""
    return V5DiscoveryPipeline(
        event_store=event_store,
        source_registry=source_registry,
        telegram_store=telegram_store or V5TelegramStore(),
    )


def execute_v5_make(
    telegram_config: TelegramConfig,
    client: TelegramTestClient,
    event_store: EventStore | None = None,
    source_registry: SourceRegistry | None = None,
) -> dict[str, Any]:
    """Execute V5 /make discovery-only command.
    
    This is the function passed to Telegram listener's execute_make.
    """
    pipeline = create_v5_discovery_pipeline(
        event_store=event_store or EventStore(root=Path("./data/events")),
        source_registry=source_registry,
    )
    
    # Run discovery
    events = pipeline.run_discovery()
    
    # Send Top 5 to Telegram
    results = pipeline.send_to_telegram(
        client=client,
        config=telegram_config,
        count=5,
        offset=0,
    )
    
    return {
        "ok": True,
        "events_count": len(events),
        "cards_sent": len([r for r in results if r.get("ok")]),
        "pipeline": pipeline,  # Return for state preservation
    }
