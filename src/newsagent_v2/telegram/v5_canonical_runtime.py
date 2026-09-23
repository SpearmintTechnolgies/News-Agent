"""Canonical V5 Runtime - glues hardened control runtime with V5 functionality.

Reuses:
- build_runtime() for singleton, offsets, transport
- V5TelegramIntegration for /make, callbacks
- GenerationWorker for generation pipeline
- ProviderPreflight for readiness
- V5ReviewCallbackHandler for review callbacks (rate, feedback, approve, etc.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig, load_telegram_config
from newsagent_v2.telegram.live_transport import create_live_transport
from newsagent_v2.telegram.singleton import acquire_singleton_lock, release_singleton_lock
from newsagent_v2.telegram.state import V5BotState
from newsagent_v2.telegram.contract import ACK_MAKE_TEXT
from newsagent_v2.telegram.listener import is_make_command, poll_once
from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
from newsagent_v2.telegram.v5_cards import render_card_from_event, seepage_keyboard
from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler
from newsagent_v2.control.make_v5_bridge import create_v5_discovery_pipeline
from newsagent_v2.v5_generation.generation_worker import GenerationWorker
from newsagent_v2.v5_generation.persistent_store import PersistentV5Store
from newsagent_v2.v5_generation.provider_preflight import ProviderPreflight
from newsagent_v2.v5_generation.persistent_review import PersistentReviewStore
from newsagent_v2.v5_generation.revision_controller import RevisionController
from newsagent_v2.v5_generation.version_store import VersionStore
from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle
from newsagent_v2.wordpress.draft_store import WordPressDraftStore
from newsagent_v2.publication.master_index import MasterIndexStore


class CanonicalV5Integration:
    """V5 integration with GenerationWorker wired for controlled E2E.

    Minimal glue - reuses existing components, adds generation support.
    """

    def __init__(
        self,
        config: TelegramConfig,
        client: TelegramTestClient,
        event_store: EventStore | None = None,
        source_registry: SourceRegistry | None = None,
        persistent_store: PersistentV5Store | None = None,
        environ: dict[str, str] | None = None,
        wordpress_lifecycle: WordPressDraftLifecycle | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.event_store = event_store or EventStore(root=Path("./data/events"))
        self.source_registry = source_registry or SourceRegistry()
        self.environ = environ or dict(os.environ)
        if wordpress_lifecycle is None:
            from newsagent_v2.wordpress.config import load_wordpress_config, WordPressConfigError
            from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
            try:
                wordpress_lifecycle = WordPressDraftLifecycle(
                    config=load_wordpress_config(self.environ),
                    transport=build_live_wordpress_transport(),
                    store=WordPressDraftStore(), master_index=MasterIndexStore(),
                )
            except WordPressConfigError:
                wordpress_lifecycle = None

        # Persistent state
        self.persistent_store = persistent_store or PersistentV5Store()

        # Provider preflight
        self.preflight = ProviderPreflight(environ=self.environ)

        # Generation worker
        self.generation_worker = GenerationWorker(
            client=client,
            config=config,
            persistent_store=self.persistent_store,
            environ=self.environ,
            wordpress_lifecycle=wordpress_lifecycle,
        )

        # Telegram state
        self.telegram_store = V5TelegramStore()
        self.callback_handler = V5CallbackHandler(
            event_store=self.event_store,
            telegram_store=self.telegram_store,
            generation_worker=self.generation_worker,
            preflight=self.preflight,
            environ=self.environ,
        )

        # Review callback handler for rate, feedback, approve, revise, etc.
        self.review_store = PersistentReviewStore()
        self.version_store = VersionStore()
        self.revision_controller = RevisionController(
            version_store=self.version_store,
            environ=self.environ,
        )
        self.review_callback_handler = V5ReviewCallbackHandler(
            review_store=self.review_store,
            revision_controller=self.revision_controller,
            version_store=self.version_store,
            persistent_store=self.persistent_store,
            wordpress_lifecycle=wordpress_lifecycle,
            master_index=MasterIndexStore(),
            environ=self.environ,
        )

        # Discovery pipeline
        self._discovery_pipeline: Any | None = None
        self._poll_offset: int | None = None

    def run_discovery(self) -> list[Any]:
        """Run V5 discovery and send Top 5."""
        self._discovery_pipeline = create_v5_discovery_pipeline(
            event_store=self.event_store,
            source_registry=self.source_registry,
            telegram_store=self.telegram_store,
        )
        events = self._discovery_pipeline.run_discovery()
        self.send_top_events(count=5, offset=0)
        return events

    def send_top_events(self, count: int = 5, offset: int = 0) -> list[dict[str, Any]]:
        """Send events to Telegram."""
        if self._discovery_pipeline is None:
            return [{"ok": False, "reason": "no_discovery_run"}]

        events = self._discovery_pipeline.get_top_events(count=count, offset=offset)
        total = len(self.telegram_store.get_ranked_events() or [])
        results = []

        if not events:
            results.append(self.client.send_message(
                chat_id=self.config.test_chat_id,
                text="No stories passed the evidence gate after source expansion.",
                parse_mode="HTML",
            ))
            return results

        for i, event in enumerate(events):
            rank = offset + i + 1
            card = render_card_from_event(event, rank, total)
            result = self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=card["text"],
                parse_mode=card["parse_mode"],
                reply_markup=card["reply_markup"],
            )
            results.append(result)

        if offset + count < total:
            next_result = self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=f"Showing {min(offset + count, total)}/{total} events",
                parse_mode="HTML",
                reply_markup=seepage_keyboard(offset=offset + count),
            )
            results.append(next_result)

        return results

    def handle_callback(self, data: str, update: dict[str, Any] | None = None) -> dict[str, Any]:
        """Handle V5 callback.

        Routes to:
        1. V5ReviewCallbackHandler for review actions (rate, feedback, approve, etc.)
        2. V5CallbackHandler for discovery actions (run, follow, ignore, see_next)
        """
        # Try review handler first - it returns None for non-review callbacks
        review_result = self._try_review_callback(data, update)
        if review_result is not None:
            return review_result

        # Fall through to discovery callback handler
        result = self.callback_handler.handle(data)

        # Send acknowledgment (discovery callbacks)
        if result.get("ok") and result.get("message"):
            self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=result["message"],
                parse_mode="HTML",
            )

        # Handle SEE NEXT
        if result.get("action") == "see_next":
            offset = result.get("offset", 0)
            self.send_top_events(count=5, offset=offset)

        return result

    def _send_revision_result(self, result: dict[str, Any]) -> None:
        """Send compact revision package with VIEW FULL ARTICLE button.

        ZERO provider calls - only loads frozen artifacts.
        """
        from ..v5_generation.telegram_delivery import send_compact_revision_summary
        from ..v5_generation.version_store import VersionStore

        event_id = result.get("event_id", "")
        article_version = result.get("article_version")
        image_version = result.get("image_version")
        article_revised = result.get("article_revised", False)
        image_revised = result.get("image_revised", False)
        canonical_title = result.get("canonical_title", "Unknown")

        # Use existing VersionStore
        version_store = VersionStore()

        # Send COMPACT revision summary (with VIEW FULL button)
        send_compact_revision_summary(
            client=self.client,
            config=self.config,
            version_store=version_store,
            event_id=event_id,
            canonical_title=canonical_title,
            article_version=article_version,
            image_version=image_version,
            article_revised=article_revised,
            image_revised=image_revised,
        )

    def _send_view_full_article(self, result: dict[str, Any]) -> None:
        """Send complete article - idempotent, read-only, ZERO provider calls.

        Loads persisted article from VersionStore and sends to Telegram.
        Respects message limits by splitting if needed.
        Errors are logged but do not crash the runtime.
        """
        from ..v5_generation.telegram_delivery import _send_article_text

        event_id = result.get("event_id", "")
        version = result.get("version", "")
        headline = result.get("headline", "Unknown")
        body = result.get("body", "")

        print(f"[VIEWFULL] Sending full article: event={event_id} version={version}")

        # Send via delivery helper (handles splitting for Telegram limits)
        # Errors are caught and logged to prevent crashing the polling runtime
        try:
            _send_article_text(
                client=self.client,
                chat_id=self.config.test_chat_id,
                headline=headline,
                dek=None,
                body=body,
                version=version,
            )
            print(f"[VIEWFULL] Article sent successfully")
        except Exception as e:
            print(f"[VIEWFULL] ERROR sending article: {e}")
            import traceback
            traceback.print_exc()
            # Notify user of failure but keep runtime alive
            try:
                self.client.send_message(
                    chat_id=self.config.test_chat_id,
                    text=f"âŒ Failed to send full article {version}: {str(e)[:100]}",
                    parse_mode="HTML",
                )
            except Exception:
                pass  # If even error notification fails, silently continue

    def _send_view_full_image(self, result: dict[str, Any]) -> None:
        """Send complete image - idempotent, read-only, ZERO provider calls.

        Loads persisted image from VersionStore and sends to Telegram.
        """
        from pathlib import Path

        image_path = result.get("image_path", "")
        if image_path and Path(image_path).exists():
            self.client.send_photo(
                chat_id=self.config.test_chat_id,
                photo=image_path,
                caption="ðŸ–¼ Image (read-only)",
            )

    def _try_review_callback(self, data: str, update: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Try to handle as review callback. Returns None if not a review callback."""
        if self.callback_handler.parse_callback(data) is not None:
            return None

        # === DIAGNOSTIC: Raw callback received ===
        print(f"[VIEWFULL-DIAG-1] RAW CALLBACK RECEIVED: callback_data='{data}'")

        # Parse to check if it's a review callback
        parsed = self.review_callback_handler.parse_callback(data)

        # === DIAGNOSTIC: Parser result ===
        if parsed is None:
            print(f"[VIEWFULL-DIAG-2] PARSER: REJECTED (parsed=None)")
            return None  # Not a review callback - let discovery handler try
        print(f"[VIEWFULL-DIAG-2] PARSER: ACCEPTED")
        print(f"[VIEWFULL-DIAG-2]   action={parsed.get('action')}")
        print(f"[VIEWFULL-DIAG-2]   event_id={parsed.get('event_id')}")
        print(f"[VIEWFULL-DIAG-2]   version={parsed.get('version')}")
        print(f"[VIEWFULL-DIAG-2]   extra={parsed.get('extra')}")

        # Get chat_id from update for feedback mode
        chat_id = ""
        if update and update.get("callback_query"):
            cq = update["callback_query"]
            if isinstance(cq, dict) and cq.get("message"):
                msg = cq["message"]
                if isinstance(msg, dict) and msg.get("chat"):
                    chat = msg["chat"]
                    if isinstance(chat, dict):
                        chat_id = str(chat.get("id", ""))

        # Handle via review callback handler
        print(f"[VIEWFULL-DIAG-3] HANDLER: About to call review_callback_handler.handle()")
        result = self.review_callback_handler.handle(
            data=data,
            reviewer="user",
            job_id="",
            chat_id=chat_id,
        )

        # === DIAGNOSTIC: Handler result ===
        print(f"[VIEWFULL-DIAG-4] HANDLER RESULT:")
        print(f"[VIEWFULL-DIAG-4]   ok={result.get('ok')}")
        print(f"[VIEWFULL-DIAG-4]   action={result.get('action')}")
        print(f"[VIEWFULL-DIAG-4]   event_id={result.get('event_id')}")
        print(f"[VIEWFULL-DIAG-4]   version={result.get('version')}")
        print(f"[VIEWFULL-DIAG-4]   full_result_keys={list(result.keys())}")

        # Handle special actions
        if result.get("ok"):
            action = result.get("action", "")

            # === DIAGNOSTIC: Router branch selection ===
            print(f"[VIEWFULL-DIAG-5] RUNTIME ROUTER: action='{action}'")

            # Handle revision completion - send compact revision package
            if action == "revise_complete" and result.get("send_revision_package"):
                print(f"[VIEWFULL-DIAG-5]   â†’ branch: _send_revision_result()")
                self._send_revision_result(result)
            elif action == "edit_complete" and result.get("send_review_package"):
                from ..v5_generation.telegram_delivery import send_initial_v5_review_package
                send_initial_v5_review_package(
                    client=self.client, config=self.config, version_store=self.version_store,
                    event_id=result["event_id"], canonical_title=result.get("canonical_title", "Unknown"),
                    article_version=result["article_version"], image_version=result.get("image_version"),
                    generation_result=result,
                )
            # Handle view full article - send complete article (read-only)
            elif action == "view_full_article":
                print(f"[VIEWFULL-DIAG-5]   â†’ branch: _send_view_full_article()")
                self._send_view_full_article(result)
            # Handle view full image - send image (read-only)
            elif action == "view_full_image":
                print(f"[VIEWFULL-DIAG-5]   â†’ branch: _send_view_full_image()")
                self._send_view_full_image(result)
            # Standard message + reply_markup
            elif result.get("reply_markup"):
                print(f"[VIEWFULL-DIAG-5]   â†’ branch: send_message with reply_markup")
                self.client.send_message(
                    chat_id=self.config.test_chat_id,
                    text=result.get("message", ""),
                    parse_mode="HTML",
                    reply_markup=result.get("reply_markup"),
                )
            elif result.get("compact_message"):
                print(f"[VIEWFULL-DIAG-5]   â†’ branch: send_message compact_message")
                # Send compact revision summary
                self.client.send_message(
                    chat_id=self.config.test_chat_id,
                    text=result["compact_message"],
                    parse_mode="HTML",
                )
            elif result.get("message"):
                print(f"[VIEWFULL-DIAG-5]   â†’ branch: send_message simple")
                # Simple acknowledgment
                self.client.send_message(
                    chat_id=self.config.test_chat_id,
                    text=result["message"],
                    parse_mode="HTML",
                )
            else:
                print(f"[VIEWFULL-DIAG-5]   â†’ branch: NO MATCH - no action taken!")
        else:
            # Error message
            error_msg = result.get("message") or result.get("reason") or "Unknown error"
            print(f"[VIEWFULL-DIAG-5]   â†’ branch: ERROR - {error_msg}")
            self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=f"âŒ {error_msg}",
                parse_mode="HTML",
            )

        return result

    def run_once(self) -> dict[str, Any]:
        """Poll once and handle updates."""
        # HTTP client timeout must exceed Telegram long-poll timeout; otherwise
        # empty getUpdates calls fail as read timeouts with no updates forever.
        http_timeout = int(getattr(self.client, "timeout_seconds", 30) or 30)
        long_poll_timeout = max(1, http_timeout - 5)

        try:
            response = poll_once(
                self.client,
                offset=self._poll_offset,
                timeout=long_poll_timeout,
            )
        except Exception as exc:
            print(f"[POLL] getUpdates raised: {exc!r}")
            return {"handled": 0, "updates": 0, "poll_error": str(exc)}

        if not isinstance(response, dict):
            print(f"[POLL] unexpected response type: {type(response).__name__}")
            return {"handled": 0, "updates": 0, "poll_error": "bad_response_type"}

        if not response.get("ok", True):
            print(
                "[POLL] getUpdates failed: "
                f"status={response.get('status_code')} "
                f"error={response.get('error') or response.get('telegram_description')}"
            )
            return {
                "handled": 0,
                "updates": 0,
                "poll_error": response.get("error") or "getUpdates_failed",
            }

        payload = response.get("payload")
        if not isinstance(payload, dict):
            print("[POLL] getUpdates missing payload")
            return {"handled": 0, "updates": 0, "poll_error": "missing_payload"}

        updates = payload.get("result", [])
        if not isinstance(updates, list):
            print("[POLL] getUpdates result is not a list")
            return {"handled": 0, "updates": 0, "poll_error": "bad_result"}

        if updates:
            print(
                f"[POLL] received {len(updates)} update(s) "
                f"offset_before={self._poll_offset}"
            )

        handled = 0
        allowed_chat = str(self.config.test_chat_id).strip()
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._poll_offset = update_id + 1

            # Callback
            callback = update.get("callback_query")
            if isinstance(callback, dict):
                data = str(callback.get("data") or "")
                query_id = str(callback.get("id") or "")
                print(f"[VIEWFULL-DIAG-0] CALLBACK UPDATE: query_id={query_id[:20]}... data='{data}'")
                try:
                    self.handle_callback(data, update)
                except Exception as exc:
                    print(f"[ROUTE] callback handler error: {exc!r}")
                if query_id:
                    print(f"[VIEWFULL-DIAG-8] ANSWERING CALLBACK: query_id={query_id[:20]}...")
                    try:
                        answer_result = self.client.answer_callback_query(callback_query_id=query_id, text="Done")
                        print(f"[VIEWFULL-DIAG-8] CALLBACK ANSWERED: result={answer_result}")
                    except Exception as e:
                        print(f"[VIEWFULL-DIAG-8] CALLBACK ANSWER FAILED: {e}")
                handled += 1
                continue

            # Message (commands or text)
            message = update.get("message") or {}
            if isinstance(message, dict):
                text = message.get("text", "")
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", "")).strip()

                if chat_id != allowed_chat:
                    print(
                        f"[ROUTE] skip update_id={update_id}: "
                        f"chat_id={chat_id!r} != allowed={allowed_chat!r}"
                    )
                    continue

                # === FEEDBACK CAPTURE: Check if awaiting feedback BEFORE command handling ===
                if text and not str(text).startswith("/"):
                    feedback_result = self._try_capture_feedback(chat_id, text)
                    if feedback_result:
                        # Feedback was captured - send confirmation
                        if feedback_result.get("ok") and feedback_result.get("message"):
                            self.client.send_message(
                                chat_id=self.config.test_chat_id,
                                text=feedback_result["message"],
                                parse_mode="HTML",
                            )
                        handled += 1
                        continue  # Don't process as normal message

                # === COMMAND HANDLING ===
                # Accept /make and /make@BotName (group clients often append @bot).
                if is_make_command(text if isinstance(text, str) else None):
                    token = str(text).strip().split()[0]
                    print(f"[ROUTE] /make received token={token!r} chat_id={chat_id}")
                    try:
                        self.client.send_message(
                            chat_id=self.config.test_chat_id,
                            text=ACK_MAKE_TEXT,
                        )
                        print("[ROUTE] /make ACK sent; starting discovery")
                        self.run_discovery()
                        print("[ROUTE] /make discovery finished")
                    except Exception as exc:
                        print(f"[ROUTE] /make handler error: {exc!r}")
                        try:
                            self.client.send_message(
                                chat_id=self.config.test_chat_id,
                                text=f"⚠️ /make failed: {exc}",
                            )
                        except Exception as send_exc:
                            print(f"[ROUTE] /make error notify failed: {send_exc!r}")
                    handled += 1

        return {"handled": handled, "updates": len(updates)}

    def _try_capture_feedback(self, chat_id: str, text: str) -> dict[str, Any] | None:
        """Try to capture feedback text. Returns result if captured, None otherwise."""
        if not chat_id or not text:
            return None

        # Check with review callback handler
        result = self.review_callback_handler.check_and_capture_feedback_text(
            chat_id=chat_id,
            text=text,
            reviewer="user",
        )

        return result


def run_canonical_v5(
    environ: dict[str, str] | None = None,
    max_iterations: int | None = None,
) -> None:
    """Run canonical V5 bot with all protections.

    Reuses:
    - build_runtime for singleton, config, transport
    - CanonicalV5Integration for V5 + generation
    """
    REPO_ROOT = Path(__file__).resolve().parents[3]

    # Load persistent project configuration from .env
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    env = dict(environ or os.environ)

    # Check for existing instance BEFORE acquiring lock
    from newsagent_v2.telegram.singleton import is_another_instance_running
    is_running, pid = is_another_instance_running()
    if is_running:
        print(f"STOP: Another V5 bot instance is running (PID {pid})")
        print("Refusing to start second instance.")
        sys.exit(1)

    # Acquire singleton lock (from runtime)
    try:
        acquire_singleton_lock()
    except RuntimeError as e:
        print(f"Singleton lock failed: {e}")
        sys.exit(1)

    try:
        # --- FAIL-CLOSED: V5 requires Kimi for article generation ---
        from newsagent_v2.article.writer.v4.provider import (
            ENV_PROVIDER,
            ENV_ALLOW_KIMI,
        )
        from newsagent_v2.article.writer.bedrock_mantle import KEY_ENV as KIMI_KEY_ENV

        provider = env.get(ENV_PROVIDER, "").strip().lower()
        allow_kimi = env.get(ENV_ALLOW_KIMI, "").strip().lower() == "true"
        kimi_key = env.get(KIMI_KEY_ENV, "").strip()

        if provider != "kimi":
            print(f"CONFIG ERROR: V5 requires NEWSAGENT_V2_V4_WRITER_PROVIDER=kimi")
            print(f"Current value: '{provider}' (unset or wrong)")
            print(f"Set in {env_path} or environment and restart.")
            sys.exit(1)

        if not allow_kimi:
            print(f"CONFIG ERROR: V5 requires NEWSAGENT_V2_V4_ALLOW_KIMI=true")
            print(f"Set in {env_path} or environment and restart.")
            sys.exit(1)

        if not kimi_key:
            print(f"CONFIG ERROR: V5 requires {KIMI_KEY_ENV}")
            print(f"Set in {env_path} or environment and restart.")
            sys.exit(1)

        print("V5 writer configuration validated: Kimi is ready")
        # -------------------------------------------------------------

        # Load config
        config = load_telegram_config(env)

        # Create transport
        transport = create_live_transport(config)

        # Create client
        client = TelegramTestClient(
            config=config,
            transport=transport,
            live_send_enabled=True,
        )

        # Create integration with generation support
        integration = CanonicalV5Integration(
            config=config,
            client=client,
            environ=env,
        )

        print("Canonical V5 Telegram bot started")
        print(f"Listening on chat: {config.test_chat_id}")
        print("Send /make to run discovery")
        print("Ctrl+C to stop")

        iterations = 0

        while True:
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1

            result = integration.run_once()

            if result.get("handled", 0) > 0:
                print(f"Handled {result['handled']} updates")

    except KeyboardInterrupt:
        print("\nBot stopped by user")
    finally:
        release_singleton_lock()


if __name__ == "__main__":
    raise SystemExit(run_canonical_v5())
