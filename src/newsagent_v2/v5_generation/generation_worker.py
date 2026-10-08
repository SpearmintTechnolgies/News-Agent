"""Background generation worker.

Runs the V6 story pipeline outside the Telegram polling loop, creates or updates the event's
WordPress draft (internal links + SEO score), and sends the review card. REVISE and EDIT go
through the same path so every version the editor sees is a real, linked draft.
"""

from __future__ import annotations

import contextvars
import html
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from newsagent_v2.discovery.event_clusterer import NewsEvent
    from newsagent_v2.telegram.client import TelegramTestClient
    from newsagent_v2.telegram.config import TelegramConfig

from newsagent_v2.v5_generation.cost_ledger import CostLedger
from newsagent_v2.v5_generation.persistent_store import GenerationJob, PersistentV5Store
from newsagent_v2.v5_generation.run_story_adapter import Revision, RunStoryAdapter
from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle

MAX_TAGS = 8


class GenerationWorker:
    RETRY_SKIPPED_AFTER_HOURS = 6

    _active_jobs: dict[str, GenerationJob] = {}
    _lock = threading.Lock()
    _batch_running = False

    def __init__(
        self,
        client: TelegramTestClient,
        config: TelegramConfig,
        persistent_store: PersistentV5Store,
        environ: dict[str, str] | None = None,
        wordpress_lifecycle: WordPressDraftLifecycle | None = None,
        event_store: Any = None,
    ) -> None:
        self.client = client
        self.config = config
        self.persistent_store = persistent_store
        self.environ = environ or {}
        self.wordpress_lifecycle = wordpress_lifecycle
        self.event_store = event_store
        self.ledger = CostLedger(persist_path=Path("./data/v5_state/cost_ledger.json") if environ else None)
        self._progress_ids: dict[str, dict[str, int]] = {}

    # ----- stores -----------------------------------------------------------------------------

    def _version_store(self):
        from newsagent_v2.v5_generation.version_store import VersionStore

        root = self.environ.get("V5_VERSION_STORE_ROOT")
        return VersionStore(root=Path(root) if root else None)

    def _adapter(self, version_store) -> RunStoryAdapter:
        from newsagent_v2.approval.store import ApprovalStore

        return RunStoryAdapter(version_store=version_store, approval_store=ApprovalStore(), environ=self.environ)

    def _load_event(self, event_id: str, headline: str = "") -> NewsEvent:
        from newsagent_v2.discovery.event_clusterer import NewsEvent

        store = self.event_store
        if store is None:
            from newsagent_v2.discovery.event_store import EventStore

            store = EventStore(root=Path("./data/events"))
        event = store.get(event_id) if hasattr(store, "get") else None
        return event or NewsEvent(event_id=event_id, canonical_title=headline or event_id)

    # ----- Telegram progress -----------------------------------------------------------------

    def _update_progress(self, job: GenerationJob, stage: str, emoji: str = "🟡") -> None:
        from newsagent_v2.telegram.operators import operator_ids

        text = f"{emoji} {stage}\n\nStory: {job.event_id[:20]}..."
        progress_ids = getattr(self, "_progress_ids", None)
        if progress_ids is None:
            progress_ids = {}
            self._progress_ids = progress_ids
        targets = progress_ids.get(job.job_id) or {}
        if not targets:
            targets = {}
            for chat_id in operator_ids(self.config):
                result = self.client.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                if result.get("ok") and result.get("message_id"):
                    targets[chat_id] = result["message_id"]
            progress_ids[job.job_id] = targets
            primary = targets.get(str(self.config.test_chat_id))
            if primary:
                job.progress_message_id = primary
                self.persistent_store.update_job_state(
                    job.job_id, job.state, progress_message_id=job.progress_message_id
                )
        else:
            for chat_id, message_id in targets.items():
                self.client.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML"
                )

    def _report_failure(self, job: GenerationJob, stage: str, error: str) -> None:
        reason = html.escape(str(error or "unknown error")[:700])
        if str(error or "").startswith("Skipped:"):
            text = f"⏭ Story skipped (not enough verified evidence)\n{reason}"
        else:
            text = f"❌ Generation stopped\nStage: {stage}\n{reason}"
        targets = getattr(self, "_progress_ids", {}).get(job.job_id) or {}
        if not targets and job.progress_message_id:
            targets = {str(self.config.test_chat_id): job.progress_message_id}
        if targets:
            for chat_id, message_id in targets.items():
                self.client.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML"
                )
        else:
            from newsagent_v2.telegram.operators import broadcast_message

            broadcast_message(self.client, self.config, text=text, parse_mode="HTML")

    def _send_review_package(self, event: NewsEvent, result: dict[str, Any], job: GenerationJob) -> None:
        from .telegram_delivery import send_initial_v5_review_package

        delivery = send_initial_v5_review_package(
            client=self.client,
            config=self.config,
            version_store=self._version_store(),
            event_id=job.event_id,
            canonical_title=event.canonical_title,
            article_version=result.get("article_version") or "v1",
            image_version=result.get("image_version"),
            generation_result=result,
            environ=self.environ,
        )
        if not delivery.get("ok"):
            from newsagent_v2.telegram.operators import broadcast_message

            broadcast_message(
                self.client,
                self.config,
                text=f"❌ Review package delivery failed: {html.escape(str(delivery.get('error', 'unknown')))}",
                parse_mode="HTML",
            )

    # ----- generation -------------------------------------------------------------------------

    def run_generation(
        self, event: NewsEvent, job: GenerationJob, revision: Revision | None = None
    ) -> dict[str, Any]:
        """Write (or revise) the story, create/update the draft, send the review card."""
        try:
            self.persistent_store.update_job_state(job.job_id, "RESERVED")
            writer_entry = self.ledger.start_entry(
                provider="bedrock-mantle",
                model=self.environ.get("NEWSAGENT_V2_V4_WRITER_MODEL", "kimi"),
                operation="revise" if revision else "write",
            )
            self._update_progress(job, "Revising with your feedback..." if revision else "Researching sources...", "🔎")
            self.persistent_store.update_job_state(job.job_id, "REQUESTING")
            version_store = self._version_store()
            result = self._adapter(version_store).run_story(event, force_retry=bool(revision), revision=revision)

            if not result.get("ok"):
                error = result.get("error") or "Unknown error"
                self.persistent_store.update_job_state(
                    job.job_id, "FAILED_FINAL", error=str(error)[:200],
                    image_version=result.get("image_version"), image_hash=result.get("image_hash"),
                )
                self._report_failure(job, "revision" if revision else "generation", error)
                return result
            return self._finish(event, job, result, version_store, writer_entry)
        except Exception as exc:  # noqa: BLE001 - a worker thread must report, never die silently
            self.persistent_store.update_job_state(job.job_id, "FAILED_FINAL", error=str(exc)[:200])
            self._report_failure(job, "worker_exception", str(exc))
            return {"ok": False, "error": str(exc)}

    def _finish(self, event: NewsEvent, job: GenerationJob, result: dict[str, Any], version_store: Any,
                writer_entry: Any = None) -> dict[str, Any]:
        """Draft (create or update) -> job SUCCEEDED -> review card."""
        draft = self._create_wordpress_draft(event, result, version_store)
        if self.wordpress_lifecycle is not None:
            if not (isinstance(draft, dict) and draft.get("ok")):
                error = str((draft or {}).get("error") or "WordPress draft was not created")
                self.persistent_store.update_job_state(
                    job.job_id, "FAILED_FINAL", error=error[:200],
                    article_version=result.get("article_version"), article_hash=result.get("article_hash"),
                    image_version=result.get("image_version"), image_hash=result.get("image_hash"),
                )
                self._report_failure(job, "wordpress_draft", error)
                return {**result, "ok": False, "error": error}
            self._save_usage(event.event_id, result, version_store)
        result["wordpress_draft"] = draft
        self.persistent_store.update_job_state(
            job.job_id, "SUCCEEDED",
            article_version=result.get("article_version"), article_hash=result.get("article_hash"),
            image_version=result.get("image_version"), image_hash=result.get("image_hash"),
            metadata={"wordpress_draft": draft} if draft else None,
        )
        if writer_entry is not None:
            usage = result.get("kimi_usage") or {}
            self.ledger.finalize_entry(
                writer_entry.entry_id,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cost_inr=usage.get("cost_inr"),
            )
        self._update_progress(
            job,
            f"Ready for review\nArticle: {result.get('article_version')}\nImage: {result.get('image_version')}",
            "OK",
        )
        self._send_review_package(event, result, job)
        return result

    @staticmethod
    def _save_usage(event_id: str, result: dict[str, Any], version_store: Any) -> None:
        record = version_store.get_article(event_id, result.get("article_version"))
        if not record:
            return
        metadata = dict(record.get("metadata") or {})
        if result.get("text_usage") or result.get("kimi_usage"):
            metadata["text_usage"] = result.get("text_usage") or result.get("kimi_usage")
        if result.get("image_usage"):
            metadata["image_usage"] = result["image_usage"]
        version_store.save_article(
            event_id, result["article_version"], record.get("article") or {},
            result.get("article_hash") or record.get("article_hash") or "", record.get("qa_result") or {}, metadata,
        )

    def _create_wordpress_draft(self, event: NewsEvent, result: dict[str, Any], version_store: Any) -> dict[str, Any] | None:
        """Create or update the event's one draft, with internal links and the SEO report."""
        lifecycle = self.wordpress_lifecycle
        article_version = result.get("article_version")
        if lifecycle is None or not article_version:
            return None
        record = version_store.get_article(event.event_id, article_version)
        if not record:
            return None
        article = record.get("article", {})
        if article.get("category_choice_missing") and not article.get("wp_category_id"):
            return {
                "ok": False,
                "error": "Pick the website and a category again. Send /start.",
                "error_code": "category_not_chosen",
                "event_id": event.event_id,
            }
        categories = [str(c) for c in (article.get("categories") or []) if c] or (
            [str(article["category"])] if article.get("category") else []
        )
        if (
            not article.get("wp_category_id")
            and (not categories or [c.lower() for c in categories] == ["other"])
            and event.topic
        ):
            categories = [str(event.topic).replace("_", " ").title()]
        tags = list(dict.fromkeys(str(t) for t in (article.get("tags") or []) if t))[:MAX_TAGS]
        image_version = result.get("image_version")
        image_path = version_store.get_image_path(event.event_id, image_version) if image_version else None
        if article.get("pipeline") == "v6":
            from newsagent_v2.seo6 import prepare_for_site

            article = prepare_for_site(article, lifecycle.config.base_url, has_featured_image=bool(image_path))
        draft = lifecycle.create_or_update_draft(
            event_id=event.event_id,
            article=article,
            article_version=article_version,
            image_path=str(image_path) if image_path else None,
            image_version=image_version,
            categories=categories,
            tags=tags,
            evidence=article.get("sources") or [report.to_dict() for report in event.reports],
            topic=str(event.topic or article.get("category") or ""),
        )
        if not draft.ok:
            return {
                "ok": False,
                "error": draft.error or "WordPress draft creation failed",
                "error_code": getattr(draft, "error_code", None) or "create_failed",
                "event_id": event.event_id,
            }
        data = asdict(draft)
        data.update(
            ok=True,
            seo_status=draft.seo_validation.status if draft.seo_validation else "unavailable",
            seo_validation=asdict(draft.seo_validation) if draft.seo_validation else None,
            seo_metadata=asdict(draft.seo) if draft.seo else None,
            categories=categories,
            tags=tags,
            text_usage=result.get("text_usage") or {},
            image_usage=result.get("image_usage") or {},
            cms_html=True,
        )
        if article.get("seo"):
            data["seo_report"] = article["seo"]
            data["internal_links"] = article.get("internal_links_inline") or []
            data["related_links"] = article.get("related_links") or []
        return data

    # ----- jobs -------------------------------------------------------------------------------

    def _new_job(self, event_id: str, discovery_run_id: str = "") -> GenerationJob:
        now = datetime.now(timezone.utc)
        job = GenerationJob(
            job_id=f"job-{event_id}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
            event_id=event_id,
            discovery_run_id=discovery_run_id,
            state="RESERVED",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        self.persistent_store.save_job(job)
        with self._lock:
            GenerationWorker._active_jobs[job.job_id] = job
        return job

    def _running_job(self, event_id: str) -> GenerationJob | None:
        with self._lock:
            for job in GenerationWorker._active_jobs.values():
                if job.event_id == event_id and job.state in {"RESERVED", "REQUESTING"}:
                    return job
        return None

    def request_generation(self, event: NewsEvent, discovery_run_id: str | None = None) -> dict[str, Any]:
        """RUN STORY: start generation in the background (idempotent while one is running)."""
        running = self._running_job(event.event_id)
        if running:
            return {"ok": True, "job_id": running.job_id, "state": running.state, "new": False,
                    "message": f"Generation already {running.state.lower()}"}
        job = self._new_job(event.event_id, discovery_run_id or "")
        threading.Thread(target=contextvars.copy_context().run, args=(self.run_generation, event, job), daemon=True).start()
        return {"ok": True, "job_id": job.job_id, "state": "RESERVED", "new": True, "message": "Generation started"}

    def start_revision(self, event_id: str, revision: Revision, headline: str = "") -> dict[str, Any]:
        """REVISE: rewrite with the editor's feedback in the background; a new review card follows."""
        if self._running_job(event_id):
            return {"ok": False, "reason": "busy", "message": "This story is already being written; try again shortly."}
        event = self._load_event(event_id, headline)
        job = self._new_job(event_id)
        threading.Thread(target=contextvars.copy_context().run, args=(self.run_generation, event, job, revision), daemon=True).start()
        return {"ok": True, "job_id": job.job_id, "new": True}

    def apply_edit(self, event_id: str, article_version: str, image_version: str | None) -> dict[str, Any]:
        """EDIT: the edited version is already saved; update the draft and send a new review card."""
        event = self._load_event(event_id)
        job = self._new_job(event_id)
        result = {"ok": True, "article_version": article_version, "image_version": image_version,
                  "event_id": event_id, "revision": True}
        threading.Thread(
            target=contextvars.copy_context().run, args=(self._finish_safely, event, job, result), daemon=True
        ).start()
        return {"ok": True, "job_id": job.job_id}

    def _finish_safely(self, event: NewsEvent, job: GenerationJob, result: dict[str, Any]) -> None:
        try:
            self._finish(event, job, result, self._version_store())
        except Exception as exc:  # noqa: BLE001
            self.persistent_store.update_job_state(job.job_id, "FAILED_FINAL", error=str(exc)[:200])
            self._report_failure(job, "edit", str(exc))

    def should_auto_generate(self, event_id: str) -> bool:
        """False when the story was already written/published, or attempted recently."""
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
        ready, attempted, skipped = 0, [], []
        for event in events:
            if ready >= target_ready:
                break
            if not self.should_auto_generate(event.event_id):
                skipped.append(event.event_id)
                continue
            result = self.run_generation(event, self._new_job(event.event_id))
            attempted.append(event.event_id)
            if result.get("ok"):
                ready += 1
        from newsagent_v2.telegram.operators import broadcast_message

        broadcast_message(
            self.client,
            self.config,
            text=(
                f"🗞 Auto-generation finished: {ready}/{target_ready} articles ready for review "
                f"({len(attempted)} stories tried, {len(skipped)} already handled)."
            ),
            parse_mode="HTML",
        )
        return {"ready": ready, "attempted": attempted, "already_done": skipped, "target": target_ready}

    def start_batch(self, events: list[NewsEvent], target_ready: int) -> bool:
        """Run ``run_batch`` in one background thread; False if a batch is already running."""
        with self._lock:
            if GenerationWorker._batch_running:
                return False
            GenerationWorker._batch_running = True

        def _run() -> None:
            try:
                self.run_batch(events, target_ready)
            finally:
                GenerationWorker._batch_running = False

        threading.Thread(target=contextvars.copy_context().run, args=(_run,), daemon=True, name="v6-auto-generate").start()
        return True

    def get_active_job_count(self) -> int:
        with self._lock:
            return sum(1 for job in GenerationWorker._active_jobs.values()
                       if job.state in {"NOT_REQUESTED", "RESERVED", "REQUESTING"})

    def get_job_for_event(self, event_id: str) -> GenerationJob | None:
        with self._lock:
            for job in GenerationWorker._active_jobs.values():
                if job.event_id == event_id:
                    return job
        return None
