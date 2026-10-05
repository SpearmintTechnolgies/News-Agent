"""Async generation worker for V5.

Runs generation pipeline outside the polling loop.
Reports progress via Telegram.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from newsagent_v2.telegram.client import TelegramTestClient
    from newsagent_v2.telegram.config import TelegramConfig
    from newsagent_v2.discovery.event_clusterer import NewsEvent

from newsagent_v2.v5_generation.persistent_store import (
    PersistentV5Store,
    GenerationJob,
)
from newsagent_v2.v5_generation.run_story_adapter import RunStoryAdapter
from newsagent_v2.v5_generation.cost_ledger import CostLedger
from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle


class GenerationWorker:
    """Async worker for story generation.
    
    Runs in separate thread, reports progress via Telegram.
    """
    
    def __init__(
        self,
        client: TelegramTestClient,
        config: TelegramConfig,
        persistent_store: PersistentV5Store,
        environ: dict[str, str] | None = None,
        wordpress_lifecycle: WordPressDraftLifecycle | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.persistent_store = persistent_store
        self.environ = environ or {}
        self.wordpress_lifecycle = wordpress_lifecycle
        # CostLedger expects persist_path, not environ
        ledger_path = Path("./data/v5_state/cost_ledger.json") if environ else None
        self.ledger = CostLedger(persist_path=ledger_path)
    
    def _update_progress(
        self,
        job: GenerationJob,
        stage: str,
        emoji: str = "🟡",
    ) -> None:
        """Send/update progress message to Telegram."""
        if not job.progress_message_id:
            # Send initial progress message
            result = self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=f"{emoji} {stage}\n\nStory: {job.event_id[:20]}...",
                parse_mode="HTML",
            )
            if result.get("ok"):
                job.progress_message_id = result.get("message_id")
                self.persistent_store.update_job_state(
                    job.job_id,
                    job.state,
                    progress_message_id=job.progress_message_id,
                )
        else:
            # Edit existing message
            self.client.edit_message_text(
                chat_id=self.config.test_chat_id,
                message_id=job.progress_message_id,
                text=f"{emoji} {stage}\n\nStory: {job.event_id[:20]}...",
                parse_mode="HTML",
            )
    
    def _report_failure(
        self,
        job: GenerationJob,
        stage: str,
        error: str,
    ) -> None:
        """Report failure to Telegram with the actual reason."""
        import html as _html

        reason = _html.escape(str(error or "unknown error")[:700])
        if str(error or "").startswith("Skipped:"):
            text = f"⏭ Story skipped (not enough verified evidence)\n{reason}"
        else:
            text = f"❌ Generation stopped\nStage: {stage}\n{reason}"
        
        if job.progress_message_id:
            self.client.edit_message_text(
                chat_id=self.config.test_chat_id,
                message_id=job.progress_message_id,
                text=text,
                parse_mode="HTML",
            )
        else:
            self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=text,
                parse_mode="HTML",
            )

    def _send_partial_image(
        self,
        event: NewsEvent,
        result: dict[str, Any],
        job: GenerationJob,
    ) -> None:
        """Send a generated hero image even when article generation failed."""
        from pathlib import Path

        from newsagent_v2.v5_generation.version_store import VersionStore

        image_path = result.get("image_path")
        if not image_path and result.get("image_version"):
            version_root = self.environ.get("V5_VERSION_STORE_ROOT")
            store = VersionStore(root=Path(version_root) if version_root else None)
            path_obj = store.get_image_path(event.event_id, str(result.get("image_version")))
            image_path = str(path_obj) if path_obj else None
        if not image_path:
            usage = result.get("image_usage") if isinstance(result.get("image_usage"), dict) else {}
            image_path = usage.get("final_path")
        path = Path(str(image_path or ""))
        if not path.is_file():
            return
        try:
            self.client.send_photo(
                chat_id=self.config.test_chat_id,
                photo_name=path.name,
                photo_bytes=path.read_bytes(),
                caption=f"🖼 Image ready (article failed)\n{event.canonical_title}",
            )
        except Exception:
            # Delivery must never crash the worker thread.
            return
    
    def run_generation(self, event: NewsEvent, job: GenerationJob) -> dict[str, Any]:
        """Run full generation pipeline.
        
        This runs in a separate thread and updates Telegram progress.
        """
        try:
            # Reserve job
            self.persistent_store.update_job_state(job.job_id, "RESERVED")
            
            # Start cost entry (CostLedger.start_entry — not record_start)
            writer_entry = self.ledger.start_entry(
                provider="bedrock-mantle",
                model=self.environ.get("NEWSAGENT_V2_V4_WRITER_MODEL", "kimi"),
                operation="write",
            )
            
            # Update progress: Starting
            self._update_progress(job, "Story selected", "🟡")
            
            # Create adapter — honor V5_VERSION_STORE_ROOT when set (tests/ops)
            from newsagent_v2.v5_generation.version_store import VersionStore
            from newsagent_v2.approval.store import ApprovalStore

            version_root = self.environ.get("V5_VERSION_STORE_ROOT")
            version_store = VersionStore(
                root=Path(version_root) if version_root else None
            )
            approval_store = ApprovalStore()

            adapter = RunStoryAdapter(
                version_store=version_store,
                approval_store=approval_store,
                environ=self.environ,
            )
            
            # Run generation
            self._update_progress(job, "Researching sources...", "🔎")
            self.persistent_store.update_job_state(job.job_id, "REQUESTING")
            
            result = adapter.run_story(event)
            
            # Update final state
            if result.get("ok"):
                article_record = version_store.get_article(event.event_id, result.get("article_version"))
                article_body = str((article_record or {}).get("article", {}).get("article_body") or "")
                from newsagent_v2.article.qa.textutil import word_count
                from newsagent_v2.article.qa.policy import resolve_article_hard_minimum_words

                words = word_count(article_body)
                meta = (article_record or {}).get("metadata") or {}
                depth_status = result.get("depth_status") or meta.get("depth_status")
                evidence_limited = bool(
                    result.get("evidence_limited")
                    or meta.get("evidence_limited")
                    or result.get("evidence_capacity") == "LIMITED"
                    or meta.get("evidence_capacity") == "LIMITED"
                    or result.get("article_type") == "LIMITED_DEPTH_BRIEF"
                    or meta.get("article_type") == "LIMITED_DEPTH_BRIEF"
                )
                hard_min, demo_length_floor = resolve_article_hard_minimum_words(self.environ)
                # Production keeps the 500 post-recovery gate; demo length floor
                # (NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS) is honored when active.
                target_floor = int(hard_min) if demo_length_floor else 500
                hard_floor = 200 if evidence_limited else 400
                if words < hard_floor:
                    result = {
                        **result,
                        "ok": False,
                        "error": (
                            f"Article did not meet the {hard_floor} grounded-word "
                            "floor after recovery."
                        ),
                    }
                    self.persistent_store.update_job_state(
                        job.job_id, "FAILED_FINAL", error=result["error"],
                    )
                    self._report_failure(job, "article_depth", result["error"])
                    return result
                if words < target_floor and depth_status != "DEPTH_FALLBACK_PASS":
                    result = {
                        **result,
                        "ok": False,
                        "error": (
                            f"Article did not meet the {target_floor} grounded-word "
                            "minimum after recovery."
                        ),
                    }
                    self.persistent_store.update_job_state(
                        job.job_id, "FAILED_FINAL", error=result["error"],
                    )
                    self._report_failure(job, "article_depth", result["error"])
                    return result
                draft_data = self._create_wordpress_draft(event, result, version_store)
                if self.wordpress_lifecycle is not None:
                    result["wordpress_draft"] = draft_data or {
                        "ok": False,
                        "error": "WordPress draft was not created",
                        "error_code": "draft_missing",
                    }
                    if not (isinstance(draft_data, dict) and draft_data.get("ok")):
                        wp_error = str((draft_data or {}).get("error") or "WordPress draft creation failed")
                        self.persistent_store.update_job_state(
                            job.job_id,
                            "FAILED_FINAL",
                            error=wp_error[:200],
                            article_version=result.get("article_version"),
                            article_hash=result.get("article_hash"),
                            image_version=result.get("image_version"),
                            image_hash=result.get("image_hash"),
                        )
                        self._report_failure(job, "wordpress_draft", wp_error)
                        return {**result, "ok": False, "error": wp_error}
                    article_record = version_store.get_article(event.event_id, result.get("article_version"))
                    if article_record:
                        metadata = dict(article_record.get("metadata") or {})
                        metadata["text_usage"] = result.get("text_usage") or result.get("kimi_usage") or {}
                        metadata["image_usage"] = result.get("image_usage") or {}
                        version_store.save_article(
                            event.event_id, result["article_version"], article_record.get("article") or {},
                            result.get("article_hash") or "", article_record.get("qa_result") or {}, metadata,
                        )
                elif draft_data:
                    result["wordpress_draft"] = draft_data

                self.persistent_store.update_job_state(
                    job.job_id,
                    "SUCCEEDED",
                    article_version=result.get("article_version"),
                    article_hash=result.get("article_hash"),
                    image_version=result.get("image_version"),
                    image_hash=result.get("image_hash"),
                    metadata={"wordpress_draft": result.get("wordpress_draft")} if result.get("wordpress_draft") else None,
                )

                usage = result.get("kimi_usage") or {}
                self.ledger.finalize_entry(
                    writer_entry.entry_id,
                    prompt_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
                    cost_inr=usage.get("cost_inr"),
                )

                # Ready / review only after successful WP draft (or when WP lifecycle is unset in tests)
                self._update_progress(
                    job,
                    (
                        "Ready for review\n"
                        f"Article: {result.get('article_version', 'V1')}\n"
                        f"Image: {result.get('image_version', 'V1')}"
                    ),
                    "OK",
                )
                self._send_review_package(event, result, job)
                
            else:
                error = result.get("error", "Unknown error")
                self.persistent_store.update_job_state(
                    job.job_id,
                    "FAILED_FINAL",
                    error=error,
                    image_version=result.get("image_version"),
                    image_hash=result.get("image_hash"),
                )
                # Deliver hero image even when article QA/provider failed.
                if result.get("image_version") or result.get("image_path"):
                    self._send_partial_image(event, result, job)
                self._report_failure(job, "generation", error)
            
            return result
            
        except Exception as e:
            self.persistent_store.update_job_state(
                job.job_id,
                "FAILED_FINAL",
                error=str(e)[:200],
            )
            self._report_failure(job, "worker_exception", str(e))
            return {"ok": False, "error": str(e)}
    
    def _send_review_package(
        self,
        event: NewsEvent,
        result: dict[str, Any],
        job: GenerationJob,
    ) -> None:
        """Send review package after successful generation.
        
        Uses telegram_delivery module - ZERO generation/provider calls.
        """
        from .telegram_delivery import send_initial_v5_review_package
        from .version_store import VersionStore, DEFAULT_STORE_ROOT
        from pathlib import Path
        
        article_version = result.get("article_version", "V1")
        image_version = result.get("image_version", "V1")
        
        # Initialize VersionStore
        version_root = self.environ.get("V5_VERSION_STORE_ROOT")
        version_store = VersionStore(
            root=Path(version_root) if version_root else None
        )
        
        # Send using delivery module (NO generation calls)
        delivery_result = send_initial_v5_review_package(
            client=self.client,
            config=self.config,
            version_store=version_store,
            event_id=job.event_id,
            canonical_title=event.canonical_title,
            article_version=article_version,
            image_version=image_version,
            generation_result=result,
            environ=self.environ,
        )
        
        if not delivery_result.get("ok"):
            # Send error notification
            self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=f"❌ Review package delivery failed: {delivery_result.get('error', 'unknown')}",
                parse_mode="HTML",
            )

    def _create_wordpress_draft(self, event: NewsEvent, result: dict[str, Any], version_store: Any) -> dict[str, Any] | None:
        """Create the one event-keyed draft after the existing generation/QA gate."""
        lifecycle = self.wordpress_lifecycle
        if lifecycle is None:
            return None
        article_version = result.get("article_version")
        if not article_version:
            return None
        article_record = version_store.get_article(event.event_id, article_version)
        if not article_record:
            return None
        article = article_record.get("article", {})
        from newsagent_v2.article.qa.textutil import word_count
        from newsagent_v2.article.qa.policy import resolve_article_hard_minimum_words

        words = word_count(str(article.get("article_body") or ""))
        depth_status = result.get("depth_status") or (article_record.get("metadata") or {}).get("depth_status")
        hard_min, demo_length_floor = resolve_article_hard_minimum_words(self.environ)
        target_floor = int(hard_min) if demo_length_floor else 500
        if words < 400:
            return None
        if words < target_floor and depth_status != "DEPTH_FALLBACK_PASS":
            return None
        categories = article.get("categories") if isinstance(article.get("categories"), list) else []
        if article.get("category") and not categories:
            categories = [article["category"]]
        if (not categories or [str(item).strip().lower() for item in categories] == ["other"]) and event.topic:
            categories = [str(event.topic).replace("_", " ").title()]
        tags = article.get("tags") if isinstance(article.get("tags"), list) else []
        extra_tags = [] if article.get("pipeline") == "v6" else sorted(str(item) for item in (event.entities or []) if item)
        tags = list(dict.fromkeys([str(item) for item in tags if item] + extra_tags))[:8]
        image_path = version_store.get_image_path(event.event_id, result.get("image_version")) if result.get("image_version") else None
        if article.get("pipeline") == "v6":
            from newsagent_v2.seo6 import prepare_for_site

            article = prepare_for_site(article, lifecycle.config.base_url, has_featured_image=bool(image_path))
        draft = lifecycle.create_or_update_draft(
            event_id=event.event_id,
            article=article,
            article_version=article_version,
            image_path=str(image_path) if image_path else None,
            image_version=result.get("image_version"),
            categories=[str(item) for item in categories if item],
            tags=[str(item) for item in tags if item],
            evidence=article.get("sources") or [report.to_dict() for report in event.reports],
            topic=str(event.topic or article.get("category") or ""),
        )
        from dataclasses import asdict
        if not draft.ok:
            return {
                "ok": False,
                "error": draft.error or "WordPress draft creation failed",
                "error_code": getattr(draft, "error_code", None) or "create_failed",
                "event_id": event.event_id,
            }
        if draft.seo_validation and draft.seo_validation.status != "PASS" and not article.get("preserve_structure"):
            reasons = "; ".join(draft.seo_validation.recommendations[:3])
            return {
                "ok": False,
                "error": f"SEO validation did not pass: {reasons}",
                "error_code": "seo_validation_failed",
                "event_id": event.event_id,
                "seo_validation": asdict(draft.seo_validation),
            }
        data = asdict(draft)
        data["ok"] = True
        data["seo_status"] = draft.seo_validation.status if draft.seo_validation else "unavailable"
        data["seo_validation"] = asdict(draft.seo_validation) if draft.seo_validation else None
        data["seo_metadata"] = asdict(draft.seo) if draft.seo else None
        data["categories"] = [str(item) for item in categories if item]
        data["tags"] = [str(item) for item in tags if item]
        data["text_usage"] = result.get("text_usage") or {}
        data["image_usage"] = result.get("image_usage") or {}
        data["cms_html"] = True
        if article.get("seo"):
            data["seo_report"] = article["seo"]
            data["internal_links"] = article.get("internal_links_inline") or []
            data["related_links"] = article.get("related_links") or []
        return data

    # Active jobs tracking for max_active = 1 enforcement
    _active_jobs: dict[str, GenerationJob] = {}
    _lock: Any = None

    def _get_lock(self):
        """Get or create class-level lock."""
        if GenerationWorker._lock is None:
            import threading
            GenerationWorker._lock = threading.Lock()
        return GenerationWorker._lock

    def request_generation(
        self,
        event: NewsEvent,
        discovery_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Request generation for an event.

        Idempotent: returns existing job if already running.
        Returns job_id and state for tracking.
        """
        with self._get_lock():
            # Check if already have an active job for this event
            for job_id, job in GenerationWorker._active_jobs.items():
                if job.event_id == event.event_id:
                    if job.state in ("REQUESTING", "SUCCEEDED", "FAILED_RETRYABLE", "FAILED_FINAL"):
                        return {
                            "ok": True,
                            "job_id": job_id,
                            "state": job.state,
                            "new": False,
                            "message": f"Generation already {job.state.lower()}",
                        }

            # Create new job
            import threading
            from datetime import datetime, timezone
            from uuid import uuid4

            now = datetime.now(timezone.utc).isoformat()
            job_id = f"job-{event.event_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
            job = GenerationJob(
                job_id=job_id,
                event_id=event.event_id,
                discovery_run_id=discovery_run_id or "",
                state="RESERVED",
                created_at=now,
                updated_at=now,
            )

            # Save to persistent store
            self.persistent_store.save_job(job)

            # Add to active jobs
            GenerationWorker._active_jobs[job_id] = job

            # Start in background thread
            thread = threading.Thread(
                target=self.run_generation,
                args=(event, job),
                daemon=True,
            )
            thread.start()

            return {
                "ok": True,
                "job_id": job_id,
                "state": "RESERVED",
                "new": True,
                "message": "Generation started",
            }

    RETRY_SKIPPED_AFTER_HOURS = 6

    def should_auto_generate(self, event_id: str) -> bool:
        """False when the story was already written/published, or attempted recently."""
        from datetime import datetime, timedelta, timezone

        for job in self.persistent_store.jobs_for_event(event_id):
            if job.state in {"SUCCEEDED", "PUBLISHED"}:
                return False
            if job.state not in {"FAILED_FINAL", "FAILED_RETRYABLE"}:
                return False
            try:
                updated = datetime.fromisoformat(str(job.updated_at))
            except ValueError:
                continue
            if datetime.now(timezone.utc) - updated < timedelta(hours=self.RETRY_SKIPPED_AFTER_HOURS):
                return False
        return True

    def run_batch(self, events: list[NewsEvent], target_ready: int) -> dict[str, Any]:
        """Generate stories one at a time until ``target_ready`` reach review."""
        from datetime import datetime, timezone
        from uuid import uuid4

        ready, attempted, skipped = 0, [], []
        for event in events:
            if ready >= target_ready:
                break
            if not self.should_auto_generate(event.event_id):
                skipped.append(event.event_id)
                continue
            now = datetime.now(timezone.utc)
            job = GenerationJob(
                job_id=f"job-{event.event_id}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
                event_id=event.event_id,
                discovery_run_id="",
                state="RESERVED",
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
            self.persistent_store.save_job(job)
            with self._get_lock():
                GenerationWorker._active_jobs[job.job_id] = job
            result = self.run_generation(event, job)
            attempted.append(event.event_id)
            if result.get("ok"):
                ready += 1
        summary = {"ready": ready, "attempted": attempted, "already_done": skipped, "target": target_ready}
        self.client.send_message(
            chat_id=self.config.test_chat_id,
            text=(
                f"🗞 Auto-generation finished: {ready}/{target_ready} articles ready for review "
                f"({len(attempted)} stories tried, {len(skipped)} already handled)."
            ),
            parse_mode="HTML",
        )
        return summary

    def start_batch(self, events: list[NewsEvent], target_ready: int) -> bool:
        """Run ``run_batch`` in one background thread; False if a batch is already running."""
        import threading

        with self._get_lock():
            if getattr(GenerationWorker, "_batch_running", False):
                return False
            GenerationWorker._batch_running = True

        def _run() -> None:
            try:
                self.run_batch(events, target_ready)
            finally:
                GenerationWorker._batch_running = False

        threading.Thread(target=_run, daemon=True, name="v6-auto-generate").start()
        return True

    def get_active_job_count(self) -> int:
        """Get count of currently active generation jobs.

        For MAX_ACTIVE_GENERATION = 1 enforcement.
        """
        with self._get_lock():
            # Count jobs that are not terminal states
            active_states = {"NOT_REQUESTED", "RESERVED", "REQUESTING"}
            return sum(
                1 for job in GenerationWorker._active_jobs.values()
                if job.state in active_states
            )

    def get_job_for_event(self, event_id: str) -> GenerationJob | None:
        """Get existing job for an event (for idempotency).

        Returns existing job if found, None otherwise.
        """
        with self._get_lock():
            for job in GenerationWorker._active_jobs.values():
                if job.event_id == event_id:
                    return job
            return None
