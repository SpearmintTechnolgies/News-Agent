"""RUN STORY Adapter.

Connects V5 NewsEvent to the existing V4 generation pipeline.

NewsEvent → RunStoryAdapter → Research → FactBank → Writer → Article Freeze V1
                                                    → Image Freeze V1
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.article.writer.v4.compile import compile_v4_article
from newsagent_v2.article.writer.v4.writer import (
    V4NaturalProseWriter,
    build_v4_writer,
)
from newsagent_v2.discovery.event_clusterer import NewsEvent, EventReport
from newsagent_v2.image.vertex_make_image import build_vertex_make_image_fn
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.article.writer.v4.recovery import USER_FAILURE_MESSAGE

from newsagent_v2.v5_generation.depth_cost_helpers import apply_depth_fallback
from .source_expansion_adapter import (
    expand_sources_for_event,
    ExpansionResult,
    default_search_fn_for_story,
)
from .version_store import VersionStore


@dataclass
class GenerationJob:
    """Generation job with idempotency."""
    job_id: str
    event_id: str
    event: dict[str, Any]
    state: str = "INIT"  # INIT, SELECTED, RESEARCHING, GENERATING, QA, REVIEW, FAILED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    article_version: str | None = None
    image_version: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize job including metadata."""
        return {
            "job_id": self.job_id,
            "event_id": self.event_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "article_version": self.article_version,
            "image_version": self.image_version,
            "error": self.error,
            "metadata": self.metadata,
        }


class RunStoryAdapter:
    """Adapter connecting V5 NewsEvent to existing V4 generation pipeline.

    Features:
    - Idempotent job creation (duplicate RUN STORY = return existing job)
    - State machine transitions
    - Article V1 + Image V1 freezing
    - Version tracking with SHA-256
    """

    # Active jobs: event_id -> GenerationJob
    _active_jobs: dict[str, GenerationJob] = {}
    _lock = threading.RLock()

    def __init__(
        self,
        version_store: VersionStore | None,
        approval_store: ApprovalStore | None,
        environ: dict[str, str] | None,
    ):
        self.version_store = version_store
        self.approval_store = approval_store
        self.environ = environ or {}

    def _generate_job_id(self, event_id: str) -> str:
        """Generate unique job ID."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{event_id}-{ts}"

    def _event_to_story(self, event: NewsEvent) -> dict[str, Any]:
        """Convert NewsEvent to V4 story format.

        V4 research/compile expect evidence under ``article_input.evidence``
        (see ``research_event`` / ``enrich_story``). Top-level ``evidence`` is
        retained for adapters that still read it directly.
        
        Now includes SOURCE EXPANSION via CollectorV2 + SourceRegistry to
        find corroborating sources when the original source is inaccessible.
        """
        # Build base evidence from event reports
        evidence = [
            {
                "source": r.source,
                "source_id": r.source_id,
                "source_authority": r.source_authority,
                "url": r.url,
                "title": r.headline,
                "published": r.published_at,
                "summary": r.description,
            }
            for r in event.reports
        ]
        
        # EXPAND SOURCES: Try to find corroborating sources for this event
        # This is the ZERO-LLM deterministic expansion that was missing in V5
        event_reports = [r.to_dict() for r in event.reports]
        try:
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
                event_reports=event_reports,
            )
            
            # Add expanded sources to evidence
            if expansion.sources_added:
                evidence.extend(expansion.sources_added)
        except Exception:
            # Expansion failure should not block the pipeline
            expansion = None

        article_input = {
            "event_id": event.event_id,
            "representative_title": event.canonical_title,
            "topic": event.topic,
            "entities": list(event.entities),
            "evidence": evidence,
        }

        return {
            "event_id": event.event_id,
            "representative_title": event.canonical_title,
            "topic": event.topic,
            "entities": list(event.entities),
            "evidence": evidence,
            "article_input": article_input,
            "source_count": event.source_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "developments": [d.to_dict() for d in event.developments],
            "source_expansion": expansion.as_dict() if expansion else None,
        }

    def _create_job(self, event: NewsEvent) -> GenerationJob:
        """Create a new generation job."""
        job = GenerationJob(
            job_id=self._generate_job_id(event.event_id),
            event_id=event.event_id,
            event=self._event_to_story(event),
            state="SELECTED",
        )

        with self._lock:
            self._active_jobs[event.event_id] = job

        # Persist job state
        if self.version_store:
            self.version_store.save_generation_job(job)

        return job

    def _get_or_create_job(self, event: NewsEvent) -> tuple[bool, GenerationJob]:
        """Get existing job or create new."""
        with self._lock:
            # Check for active job
            if event.event_id in self._active_jobs:
                job = self._active_jobs[event.event_id]
                # Check if job is in terminal state
                if job.state not in {"PUBLISHED", "REJECTED", "FAILED"}:
                    return False, job  # Existing job returned

            # Create new job
            return True, self._create_job(event)

    def _is_job_duplicate(self, event_id: str) -> bool:
        """Check if a non-terminal job exists."""
        with self._lock:
            if event_id not in self._active_jobs:
                return False
            job = self._active_jobs[event_id]
            return job.state not in {"PUBLISHED", "REJECTED", "FAILED"}

    def run_story(
        self,
        event: NewsEvent,
        force_retry: bool = False,
    ) -> dict[str, Any]:
        """Run generation for an event.

        Idempotent: returns existing job if already running.
        """
        # Check for duplicate
        if not force_retry and self._is_job_duplicate(event.event_id):
            with self._lock:
                job = self._active_jobs[event.event_id]
            return {
                "ok": True,
                "new": False,
                "job_id": job.job_id,
                "state": job.state,
                "message": "Generation already in progress",
            }

        # Create or get job
        is_new, job = self._get_or_create_job(event)

        # Transition: SELECTED → RESEARCHING
        job.state = "RESEARCHING"
        self._update_job(job)

        try:
            # Run generation
            result = self._run_generation(event, job)

            if result.get("ok"):
                job.state = "REVIEW"
                job.article_version = result.get("article_version")
                job.image_version = result.get("image_version")
            else:
                job.state = "FAILED"
                job.error = USER_FAILURE_MESSAGE
                
                # PERSIST FULL FAILURE DIAGNOSTICS
                # Store all available failure telemetry for forensic analysis
                job.metadata["failure_details"] = {
                    "failure_class": result.get("failure_class"),
                    "error": result.get("error"),
                    "notes": result.get("notes"),
                    "critical_codes": result.get("critical_codes", []),
                    "event_id": event.event_id,
                    "job_id": job.job_id,
                    "stage": "generation",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    # Evidence metrics
                    "sources_retrieved": result.get("sources_retrieved"),
                    "independent_sources": result.get("independent_sources"),
                    "primary_sources": result.get("primary_sources"),
                    "unique_propositions": result.get("unique_propositions"),
                    "evidence_capacity": result.get("evidence_capacity"),
                    "article_type": result.get("article_type"),
                    # Article metrics (if produced)
                    "native_words": result.get("native_words"),
                    "final_words": result.get("final_words"),
                    "writer_calls": result.get("writer_calls"),
                    "repair_calls": result.get("repair_calls"),
                    "expansion_calls": result.get("expansion_calls"),
                    # Grounding metrics
                    "supported": result.get("supported"),
                    "ambiguous": result.get("ambiguous"),
                    "unsupported": result.get("unsupported"),
                    # Provider info
                    "writer_model": result.get("writer_model"),
                    "writer_provider": result.get("writer_provider"),
                    "kimi_calls": result.get("kimi_calls"),
                    "vertex_calls": result.get("vertex_calls"),
                    # Attempt path for forensics
                    "attempt_path": result.get("attempt_path"),
                }

            self._update_job(job)

            return {
                "ok": result.get("ok", False),
                "new": is_new,
                "job_id": job.job_id,
                "state": job.state,
                "article_version": job.article_version,
                "image_version": job.image_version,
                "error": job.error,
                "result": result,
            }

        except Exception as e:
            job.state = "FAILED"
            job.error = USER_FAILURE_MESSAGE
            failure_details = {
                "failure_class": "WORKER_EXCEPTION",
                "exception_type": type(e).__name__,
                "exception_message": str(e)[:300],
                "event_id": event.event_id,
                "job_id": job.job_id,
                "stage": "generation",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            job.metadata["failure_details"] = failure_details
            if self.version_store:
                self.version_store.save_generation_diagnostics(
                    event.event_id,
                    failure_details,
                )
            self._update_job(job)

            return {
                "ok": False,
                "new": is_new,
                "job_id": job.job_id,
                "state": "FAILED",
                "error": job.error,
                "internal_error": f"{type(e).__name__}: {str(e)[:200]}",
            }

    def _update_job(self, job: GenerationJob) -> None:
        """Update job state."""
        job.updated_at = datetime.now(timezone.utc).isoformat()
        if self.version_store:
            self.version_store.save_generation_job(job)

    def _run_generation(
        self,
        event: NewsEvent,
        job: GenerationJob,
    ) -> dict[str, Any]:
        """Execute generation pipeline.

        Uses existing V4 components:
        - research_event (multi-source research)
        - build_fact_bank (fact extraction)
        - compile_v4_article (writer + QA)
        - vertex_make_image (image generation)
        """
        from newsagent_v2.article.writer.v4.event_research import research_event
        from newsagent_v2.article.writer.v4.factbank import build_fact_bank

        # Transition: RESEARCHING → GENERATING
        job.state = "GENERATING"
        self._update_job(job)

        env = self.environ

        # Initialize Kimi budget tracking BEFORE writer creation
        # This ensures event_id has a budget context for the transport guard
        from newsagent_v2.providers.kimi_budget import KimiBudgetStore, KimiBudgetManager

        budget_store = KimiBudgetStore(
            self.version_store.root if self.version_store else None
        )
        budget_manager = KimiBudgetManager(budget_store)

        # Create fresh budget for new story (this is initial generation, not revision)
        story_budget = budget_manager.store.create_for_new_story(event.event_id)

        # Build story format (includes source expansion)
        story = self._event_to_story(event)

        # Initialize writer with event_id for budget tracking
        # Reduced max_calls from 24 to safer defensive secondary value (5)
        writer = build_v4_writer(
            environ=env,
            enable_failover=False,
            max_calls=5,
            event_id=event.event_id,
        )

        # Run multi-source research
        research = research_event(story, search_fn=default_search_fn_for_story(story))
        pack = research.pack

        # Build fact bank
        bank = build_fact_bank(event_id=event.event_id, pack=pack)

        # Assess evidence capacity
        from newsagent_v2.article.writer.v4.evidence_depth import assess_evidence_capacity
        depth = assess_evidence_capacity(bank, research=research)
        
        # PRE-WRITER EVIDENCE GATE
        # Must block before any writer calls if evidence is insufficient
        diagnostics = {
            "queries_generated": getattr(research, 'search_queries', []),
            "sources_discovered": research.sources_discovered if hasattr(research, 'sources_discovered') else 0,
            "sources_retrieved": research.sources_retrieved if hasattr(research, 'sources_retrieved') else 0,
            "sources_failed": [],
            "independent_sources": research.independent_sources if hasattr(research, 'independent_sources') else 0,
            "primary_sources": research.primary_sources if hasattr(research, 'primary_sources') else 0,
            "raw_research_words": research.raw_research_words if hasattr(research, 'raw_research_words') else 0,
            "unique_propositions": bank.unique_proposition_count if hasattr(bank, 'unique_proposition_count') else 0,
            "evidence_capacity": depth.evidence_capacity if depth else "UNKNOWN",
            "pre_writer_gate_passed": False,
        }
        
        # Check for failed sources (403, timeouts, etc.)
        evidence_rows = pack.get("evidence", []) if pack else []
        for row in evidence_rows:
            if isinstance(row, dict):
                extraction = row.get("extraction_method", "")
                if "blocked" in extraction or "failed" in extraction or "error" in extraction:
                    diagnostics["sources_failed"].append({
                        "url": row.get("url"),
                        "source": row.get("source"),
                        "reason": extraction,
                    })
        
        # GATE: Must have at least one successfully retrieved source
        if research.sources_retrieved == 0:
            diagnostics["pre_writer_gate_passed"] = False
            diagnostics["gate_failure_reason"] = "NO_SOURCES_RETRIEVED"
            return {
                "ok": False,
                "error": "INSUFFICIENT_EVIDENCE",
                "notes": "Pre-writer evidence gate blocked: sources_retrieved == 0",
                "diagnostics": diagnostics,
                "writer_calls": 0,
                "kimi_calls": 0,
                "image_calls": 0,
            }
        
        # GATE: Single proposition is not sufficient for full article
        unique_props = bank.unique_proposition_count if hasattr(bank, 'unique_proposition_count') else 0
        if unique_props <= 1 and depth and depth.evidence_limited:
            diagnostics["pre_writer_gate_passed"] = False
            diagnostics["gate_failure_reason"] = "INSUFFICIENT_PROPOSITIONS"
            return {
                "ok": False,
                "error": "INSUFFICIENT_EVIDENCE",
                "notes": f"Pre-writer evidence gate blocked: {unique_props} propositions, insufficient for full article",
                "diagnostics": diagnostics,
                "writer_calls": 0,
                "kimi_calls": 0,
                "image_calls": 0,
            }
        
        # GATE: Must have RICH or MEDIUM capacity for normal article generation
        # LIMITED depth briefs gate separately if configured
        if depth and depth.evidence_capacity == "LIMITED":
            diagnostics["pre_writer_gate_passed"] = False
            diagnostics["gate_failure_reason"] = "LIMITED_EVIDENCE_CAPACITY"
            return {
                "ok": False,
                "error": "INSUFFICIENT_EVIDENCE",
                "notes": f"Pre-writer evidence gate blocked: LIMITED capacity, {unique_props} propositions",
                "diagnostics": diagnostics,
                "writer_calls": 0,
                "kimi_calls": 0,
                "image_calls": 0,
            }
        
        diagnostics["pre_writer_gate_passed"] = True

        # Compile V4 article (includes writer + QA + grounding)
        attempts_root = self._attempts_root(job.job_id)
        compiled = compile_v4_article(
            story,
            writer=writer,
            attempts_root=attempts_root,
            rank=1,
            research=True,
        )

        recovery_history: list[dict[str, Any]] = []
        if not compiled.ok and compiled.article and compiled.article_input:
            from newsagent_v2.article.writer.v4.recovery import recover_article

            def _targeted_research(input_data: dict[str, Any], gaps: list[str]) -> Any:
                targeted = dict(story)
                targeted["article_input"] = dict(input_data)
                targeted["article_input"]["recovery_gaps"] = list(gaps)
                current_evidence = targeted["article_input"].get("evidence") or []
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
                    event_reports=current_evidence,
                )
                seen_urls = {
                    str(row.get("url") or "").strip().lower().rstrip("/")
                    for row in current_evidence
                    if isinstance(row, dict) and row.get("url")
                }
                for row in expansion.sources_added:
                    key = str(row.get("url") or "").strip().lower().rstrip("/")
                    if key and key not in seen_urls:
                        current_evidence.append(row)
                        seen_urls.add(key)
                return research_event(targeted, search_fn=default_search_fn_for_story(targeted))

            def _rebuild_mapping(article: dict[str, Any], input_data: dict[str, Any]) -> None:
                article["evidence_used"] = [
                    row.get("url") or row.get("source")
                    for row in input_data.get("evidence", [])
                    if isinstance(row, dict) and (row.get("url") or row.get("source"))
                ]

            recovered = recover_article(
                compiled.article,
                compiled.article_input,
                compiled.qa or {},
                research_fn=_targeted_research,
                rebuild_claim_mapping=_rebuild_mapping,
                usage_fn=lambda: dict(compiled.kimi_usage or {}),
            )
            recovery_history = recovered.history
            compiled.article = recovered.article
            compiled.article_input = recovered.article_input
            compiled.qa = recovered.qa
            compiled.ok = recovered.succeeded
            if recovered.succeeded:
                compiled.failure_class = None
                compiled.notes = "v4_ok_after_bounded_recovery"
            if self.version_store and recovery_history:
                self.version_store.save_recovery_history(event.event_id, recovery_history)

        # Audited depth fallback AFTER bounded recovery only (never on first short draft).
        depth_meta: dict = {"depth_status": None}
        body_for_depth = ""
        if compiled.article:
            body_for_depth = str(compiled.article.get("article_body") or "")
        from newsagent_v2.article.qa.textutil import word_count as _wc
        words_now = int(compiled.final_words or _wc(body_for_depth) or 0)
        if compiled.article and (not compiled.ok or words_now < 600):
            depth_meta = apply_depth_fallback(
                article=compiled.article,
                qa=compiled.qa,
                word_count=words_now,
                recovery_history=recovery_history,
                recovery_succeeded=bool(compiled.ok),
            )
            if depth_meta.get("depth_status") == "DEPTH_FALLBACK_PASS" and depth_meta.get("ok"):
                compiled.qa = depth_meta.get("qa") or compiled.qa
                compiled.ok = True
                compiled.failure_class = None
                compiled.notes = "DEPTH_FALLBACK_PASS_after_exhausted_recovery"
                compiled.final_words = words_now

        if not compiled.ok or not compiled.article:
            failure_result = {
                "ok": False,
                "error": "Article could not be completed reliably after automatic verification. Please retry.",
                "failure_class": compiled.failure_class,
                "critical_codes": compiled.critical_codes,
                "notes": compiled.notes,
                "recovery_history": recovery_history,
                "sources_retrieved": compiled.research.get("sources_retrieved"),
                "independent_sources": compiled.independent_sources,
                "primary_sources": compiled.research.get("primary_sources"),
                "unique_propositions": compiled.unique_propositions,
                "evidence_capacity": compiled.evidence_capacity,
                "article_type": compiled.article_type,
                "native_words": compiled.native_words,
                "final_words": compiled.final_words,
                "writer_calls": compiled.writer_calls,
                "repair_calls": compiled.repair_calls,
                "expansion_calls": compiled.expansion_calls,
                "supported": compiled.supported,
                "ambiguous": compiled.ambiguous,
                "unsupported": compiled.unsupported,
                "writer_model": compiled.writer_model,
                "writer_provider": compiled.writer_provider,
                "kimi_usage": compiled.kimi_usage,
            }
            if self.version_store:
                self.version_store.save_generation_diagnostics(
                    event.event_id,
                    {
                        key: value
                        for key, value in failure_result.items()
                        if key not in {"error", "recovery_history"}
                    },
                )
            return failure_result

        # Freeze Article V1
        article_v1 = compiled.article
        article_content = article_v1.get("article_body", "")
        article_hash = self._calculate_sha256(article_content)

        # Store Article V1
        if self.version_store:
            self.version_store.save_article(
                event_id=event.event_id,
                version="v1",
                article=article_v1,
                article_hash=article_hash,
                qa_result=compiled.qa or {},
                metadata={
                    "word_count": compiled.final_words,
                    "grounding_supported": compiled.supported,
                    "grounding_ambiguous": compiled.ambiguous,
                    "grounding_unsupported": compiled.unsupported,
                    "attempts_root": str(attempts_root),  # KEY: Persist for revision evidence reuse
                    "depth_status": depth_meta.get("depth_status") or ("DEPTH_NORMAL_PASS" if (compiled.final_words or 0) >= 500 else None),
                    "depth_fallback": {
                        "final_word_count": depth_meta.get("final_word_count", compiled.final_words),
                        "recovery_exhausted": depth_meta.get("recovery_exhausted"),
                        "suppressed_critical_code": depth_meta.get("suppressed_critical_code"),
                    } if depth_meta.get("depth_status") == "DEPTH_FALLBACK_PASS" else None,
                    "billable_usage": {"text": None, "image": None},
                },
            )

        text_usage = dict(compiled.kimi_usage or {})
        if text_usage:
            from newsagent_v2.article.writer.v4.article_cost_telemetry import (
                compute_cost_from_tokens,
                extract_token_usage,
                resolve_verified_kimi_pricing,
            )
            tokens = extract_token_usage(text_usage)
            pricing = resolve_verified_kimi_pricing(self.environ, usage=text_usage)
            cost = compute_cost_from_tokens(
                input_tokens=tokens["input_tokens"],
                output_tokens=tokens["output_tokens"],
                pricing=pricing,
            )
            text_usage.update({
                "provider": compiled.writer_provider,
                "model": compiled.writer_model,
                "cost_usd": cost.get("article_generation_cost_usd"),
                "pricing_source": cost.get("pricing_source"),
            })

        # Generate Image V1
        image_result = self._generate_image(
            event_id=event.event_id,
            article=article_v1,
            article_hash=article_hash,
            job_id=job.job_id,
        )

        if not image_result.get("success"):
            # Image failed - still return article
            return {
                "ok": True,
                "article_version": "v1",
                "image_version": None,
                "image_failed": True,
                "article_hash": article_hash,
                "text_usage": compiled.kimi_usage,
                "image_usage": image_result,
            }

        image_path = image_result.get("final_path")
        image_hash = image_result.get("branded_image_hash") or self._calculate_sha256("")

        # Store Image V1
        if self.version_store:
            self.version_store.save_image(
                event_id=event.event_id,
                version="v1",
                image_path=image_path,
                image_hash=image_hash,
                metadata={
                    "raw_image_hash": image_result.get("raw_image_hash"),
                    "provider": image_result.get("provider"),
                    "model": image_result.get("model"),
                },
            )

        image_usage = {
            "provider": image_result.get("provider"),
            "model": image_result.get("model"),
            "requests": image_result.get("image_request_count") or 1,
            "provider_reported_usage": image_result.get("provider_reported_usage"),
            "provider_reported_cost_usd": image_result.get("provider_reported_cost_usd"),
            "provider_reported_cost": image_result.get("provider_reported_cost"),
        }
        if self.version_store:
            article_record = self.version_store.get_article(event.event_id, "v1")
            if article_record:
                meta = dict(article_record.get("metadata") or {})
                meta["billable_usage"] = {"text": text_usage, "image": image_usage}
                meta["text_usage"] = text_usage
                meta["image_usage"] = image_usage
                if depth_meta.get("depth_status"):
                    meta["depth_status"] = depth_meta.get("depth_status")
                self.version_store.save_article(
                    event.event_id, "v1", article_record.get("article") or {},
                    article_hash, article_record.get("qa_result") or {}, meta,
                )
        return {
            "ok": True,
            "article_version": "v1",
            "image_version": "v1",
            "article_hash": article_hash,
            "image_hash": image_hash,
            "kimi_usage": text_usage,
            "text_usage": text_usage,
            "image_usage": image_usage,
            "depth_status": depth_meta.get("depth_status") or "DEPTH_NORMAL_PASS",
            "depth_fallback": depth_meta if depth_meta.get("depth_status") == "DEPTH_FALLBACK_PASS" else None,
            "event_id": event.event_id,
        }

    def _generate_image(
        self,
        event_id: str,
        article: dict[str, Any],
        article_hash: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Generate image using existing infrastructure."""
        image_fn = build_vertex_make_image_fn(self.environ, batch_id=job_id)

        job = {
            "event_id": event_id,
            "article": article,
            "canonical_body_hash": article_hash,
            "article_hash": article_hash,
        }

        return image_fn(job)

    def _attempts_root(self, job_id: str) -> Path:
        """Get attempts root path."""
        return Path(__file__).resolve().parents[3] / "output" / "v5_attempts" / job_id

    def _calculate_sha256(self, content: str) -> str:
        """Calculate SHA-256 hash of content."""
        import hashlib

        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def get_job(self, event_id: str) -> GenerationJob | None:
        """Get active job for an event."""
        with self._lock:
            return self._active_jobs.get(event_id)

    def get_job_state(self, event_id: str) -> str | None:
        """Get current job state."""
        job = self.get_job(event_id)
        return job.state if job else None
