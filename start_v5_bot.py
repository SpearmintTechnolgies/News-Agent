"""V5 Telegram Bot - Canonical Entry Point.

Usage:
    python start_v5_bot.py

Environment required:
    NEWSAGENT_V2_TELEGRAM_BOT_TOKEN
    NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID

    Optional:
    NEWSAGENT_V5_CONTROLLED_E2E=true  # Disables automatic generation

Single-instance guarded. Ctrl+C for clean shutdown.
Acknowledgement-first /make handling.
Callback routing for RUN/FOLLOW/IGNORE/SEE NEXT.
Generation support via GenerationWorker (V6 story pipeline).
"""

from __future__ import annotations

import atexit
import html
import os
import re
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")

# Load project .env so the bot can be started without exporting vars in the shell.
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
except ImportError:
    pass

from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.telegram.singleton import (
    acquire_singleton_lock,
    release_singleton_lock,
    SingletonError,
)
from newsagent_v2.telegram.config import CHAT_ENV, load_telegram_config
from newsagent_v2.telegram.operators import broadcast_message, operator_ids, set_origin_chat
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.contract import SEND_TYPE_GET_UPDATES
from newsagent_v2.telegram.live_transport import create_live_transport
from newsagent_v2.telegram.state import V5BotState
from newsagent_v2.telegram.acknowledgement import MakeAcknowledgement
from newsagent_v2.telegram.v5_callbacks import (
    V5CallbackHandler,
    V5TelegramStore,
    CONTROLLED_E2E_ENV,
)
from newsagent_v2.telegram.v5_cards import seepage_keyboard
from newsagent_v2.telegram.v5_callbacks import RUN_PREFIX, FOLLOW_PREFIX, IGNORE_PREFIX
from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline, create_v5_discovery_pipeline

# V5 Generation imports
from newsagent_v2.v5_generation.persistent_store import PersistentV5Store, DiscoveryRun
from newsagent_v2.v5_generation.provider_preflight import ProviderPreflight
from newsagent_v2.v5_generation.generation_worker import GenerationWorker
from newsagent_v2.v5_generation.version_store import VersionStore
from newsagent_v2.v5_generation.persistent_review import PersistentReviewStore
from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
OFFSET_FILE = LOG_DIR / "v5_last_offset.txt"
V5_STATE_DIR = Path("./data/v5_state")


def log_event(event: str) -> None:
    ts = datetime.now().isoformat()
    line = f"[{ts}] {event}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Windows cp1252 consoles choke on arrows/emoji in log lines.
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


def load_persisted_offset() -> int | None:
    try:
        if OFFSET_FILE.exists():
            return int(OFFSET_FILE.read_text().strip())
    except (ValueError, OSError):
        pass
    return None


def persist_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(str(offset))
    except OSError:
        pass


# ============ SAFE EXTRACTORS ============

def safe_get_chat_id(update: dict) -> str | None:
    """Safely extract chat_id from update (message or callback_query)."""
    try:
        # Try message first
        msg = update.get("message") or {}
        if msg:
            chat = msg.get("chat") or {}
            return str(chat.get("id", ""))
        
        # Try callback_query
        cq = update.get("callback_query") or {}
        if cq:
            msg = cq.get("message") or {}
            chat = msg.get("chat") or {}
            return str(chat.get("id", ""))
    except Exception:
        pass
    return None


def safe_get_message_text(update: dict) -> str:
    """Safely extract text from message update."""
    try:
        msg = update.get("message") or {}
        return (msg.get("text", "") or "").strip()
    except Exception:
        return ""


_CHAT_SYSTEM_PROMPT = (
    "You are the News Agent Telegram assistant for CoinNetwork.\n"
    "Be brief, friendly, and practical (2–6 short sentences).\n"
    "You help operate this bot: /start lists the websites; pick one, then RUN STORY → "
    "GENERATE NOW → APPROVE → PUBLISH.\n"
    "Answer normal chat questions normally.\n"
    "Do not invent live news facts or claim a story was generated unless told.\n"
    "Plain text only — no markdown fences, no HTML tags."
)

HELP_TEXT = (
    "👋 News Agent is online.\n\n"
    "Commands:\n"
    "• /start — your websites, then TRENDING, 6 HOURS, or one category\n"
    "• RUN STORY writes only the card you tap\n"
    "• /author — choose the byline used for the next articles\n"
    "• Each draft arrives as a review card: REVISE, EDIT, CHANGE AUTHOR, REJECT or APPROVE & PUBLISH\n\n"
    "Anything else goes to Hermes. In a group, tag the bot first. A direct message does not need a tag."
)
_BOT_COMMANDS = frozenset({"/start", "/make", "/author", "/help"})


def is_group_chat(update: dict, chat_id: str = "") -> bool:
    """Telegram groups and supergroups. A private chat is a positive id."""
    message = update.get("message") or {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    if str(chat.get("type") or "") in {"group", "supergroup"}:
        return True
    return str(chat_id or "").startswith("-")


def mentions_bot(update: dict, bot_username: str) -> bool:
    """True when this message tags @bot_username."""
    username = str(bot_username or "").lstrip("@").lower()
    if not username:
        return False
    message = update.get("message") or {}
    text = str(message.get("text") or "")
    if f"@{username}" in text.lower():
        return True
    for entity in message.get("entities") or []:
        if not isinstance(entity, dict) or entity.get("type") != "mention":
            continue
        try:
            offset = int(entity.get("offset") or 0)
            length = int(entity.get("length") or 0)
        except (TypeError, ValueError):
            continue
        token = text[offset:offset + length].lstrip("@").lower()
        if token == username:
            return True
    return False


def strip_bot_mention(text: str, bot_username: str) -> str:
    """Drop the bot tag so Hermes sees the question."""
    username = str(bot_username or "").lstrip("@")
    cleaned = text
    if username:
        cleaned = re.sub(rf"@{re.escape(username)}\b", " ", text, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


def hermes_chat_reply(user_text: str, environ: dict[str, str] | None = None) -> str:
    """Ask Hermes/Kimi to interpret a free-form Telegram message."""
    import requests

    env = environ if environ is not None else os.environ
    key = str(env.get("NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY") or "").strip()
    if not key:
        return "Writer chat is not configured (missing Hermes API key). Send /start or /help."

    base = str(
        env.get("NEWSAGENT_V2_V4_KIMI_BASE_URL")
        or "https://gemini.warriorfinance.online/v1"
    ).rstrip("/")
    model = str(env.get("NEWSAGENT_V2_V4_WRITER_MODEL") or "moonshotai.kimi-k3").strip()
    url = f"{base}/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_text[:2000]},
        ],
        "max_tokens": 350,
        "temperature": 0.4,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, json=body, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"Hermes HTTP {response.status_code}")
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError("Hermes returned no choices")
    message = choices[0].get("message") or {}
    content = str(message.get("content") or "").strip()
    if not content:
        raise RuntimeError("Hermes returned empty content")
    # Strip common model fences; Telegram uses HTML parse mode.
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("text"):
            content = content[4:].lstrip()
    return content[:3500]


def safe_get_callback_data(update: dict) -> str | None:
    """Safely extract callback_data from callback_query update."""
    try:
        cq = update.get("callback_query") or {}
        return cq.get("data")
    except Exception:
        return None


def safe_get_callback_query_id(update: dict) -> str | None:
    """Safely extract callback_query.id for acknowledgement."""
    try:
        cq = update.get("callback_query") or {}
        return cq.get("id")
    except Exception:
        return None


def safe_get_update_id(update: dict) -> int | None:
    """Safely extract update_id from update."""
    try:
        uid = update.get("update_id")
        return int(uid) if isinstance(uid, (int, str)) else None
    except Exception:
        return None


def is_message_update(update: dict) -> bool:
    """Check if this is a message update."""
    return bool(update.get("message"))


def is_callback_update(update: dict) -> bool:
    """Check if this is a callback_query update."""
    return bool(update.get("callback_query"))


# ============ STARTUP ============

def handle_startup_backlog(
    client: TelegramTestClient,
    state: V5BotState,
    config,
) -> int | None:
    """Handle startup backlog - establish high-water mark without executing."""
    persisted = load_persisted_offset()
    if persisted is not None:
        log_event(f"[STARTUP] Resuming from persisted offset={persisted}")
        return persisted
    
    log_event("[STARTUP] Fresh start - checking for historical updates...")
    
    response = client._post(SEND_TYPE_GET_UPDATES, json_body={"timeout": 5, "limit": 100})
    
    if not response.get("ok"):
        log_event(f"[STARTUP] getUpdates failed, starting fresh")
        return None
    
    payload = response.get("payload", {})
    updates = payload.get("result", [])
    
    if not isinstance(updates, list) or not updates:
        log_event("[STARTUP] No pending updates")
        return None
    
    update_ids = []
    chat_ids_seen = set()
    for u in updates:
        uid = safe_get_update_id(u)
        if uid is not None:
            update_ids.append(uid)
            chat_id = safe_get_chat_id(u)
            if chat_id:
                chat_ids_seen.add(chat_id)
    
    if not update_ids:
        log_event("[STARTUP] No valid update IDs found")
        return None
    
    high_water = max(update_ids)
    next_offset = high_water + 1
    
    log_event(f"[STARTUP] skipped_historical_updates={len(updates)}")
    log_event(f"[STARTUP] high_water_update_id={high_water}")
    log_event(f"[STARTUP] next_offset={next_offset}")
    if chat_ids_seen:
        log_event(f"[STARTUP] chat_ids_seen={chat_ids_seen}")
    
    for uid in update_ids:
        state.mark_update_processed(uid)
    
    return next_offset


# ============ /make HANDLER ============

AUTO_GENERATE_ENV = "NEWSAGENT_V6_AUTO_GENERATE"
AUTO_CANDIDATES_ENV = "NEWSAGENT_V6_AUTO_CANDIDATES"


def select_postable(events, screen, *, limit: int = 10, keep: int = 10, progress=None):
    """Research stories in rank order and keep only those a full article can be written from."""
    postable = []
    dropped = 0
    checked = 0
    for event in list(events)[:limit]:
        checked += 1
        if progress is not None:
            progress(checked, min(len(events), limit), event)
        passed, _reason = screen(event)
        if passed:
            postable.append(event)
            if len(postable) >= keep:
                break
        else:
            dropped += 1
    return postable, dropped, checked


def auto_generate_top(client, config, discovery, runtime) -> dict[str, Any]:
    """Write the top stories in the background until N reach review (0 disables)."""
    target = int(os.environ.get(AUTO_GENERATE_ENV, "0") or 0)
    worker = runtime.generation_worker
    if target <= 0 or worker is None or runtime._controlled_e2e:
        return {"ok": False, "reason": "disabled"}
    candidates = int(os.environ.get(AUTO_CANDIDATES_ENV, "10") or 10)
    events = list(discovery.get_top_events(count=candidates, offset=0) or [])
    if not events:
        return {"ok": False, "reason": "no_events"}
    started = worker.start_batch(events, target_ready=target)
    broadcast_message(
        client,
        config,
        text=(
            f"✍️ Writing {min(target, len(events))} of the stories that already passed the source check."
            if started
            else "✍️ A batch is already being written; new stories will be picked up on the next /make."
        ),
        parse_mode="HTML",
    )
    return {"ok": started, "target": target, "candidates": len(events)}

def send_site_menu(client: TelegramTestClient, config, *, chat_id: str | None = None) -> None:
    """Ask which website to write for. /start sends this."""
    from newsagent_v2.control.sites import MENU_TEXT, active_id, list_sites, menu_keyboard

    sites = list_sites(environ=os.environ)
    text = MENU_TEXT
    markup = menu_keyboard(sites, active=active_id())
    if chat_id:
        client.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        return
    broadcast_message(client, config, text=text, reply_markup=markup)


def send_story_menu(client: TelegramTestClient, config, *, chat_id: str | None = None, site_name: str = "") -> None:
    """Ask which stories to fetch before a scan starts."""
    from newsagent_v2.control.story_picker import MENU_TEXT, menu_keyboard

    text = f"Writing for {site_name}.\n\n{MENU_TEXT}" if site_name else MENU_TEXT
    if chat_id:
        client.send_message(chat_id=chat_id, text=text, reply_markup=menu_keyboard())
        return
    broadcast_message(client, config, text=text, reply_markup=menu_keyboard())


def _incoming_image_id(update: dict) -> str:
    message = update.get("message") or {}
    photos = message.get("photo") or []
    if isinstance(photos, list) and photos:
        last = photos[-1]
        if isinstance(last, dict) and last.get("file_id"):
            return str(last["file_id"])
    document = message.get("document") or {}
    if isinstance(document, dict) and document.get("file_id"):
        mime = str(document.get("mime_type") or "")
        name = str(document.get("file_name") or "").lower()
        if mime.startswith("image/") or name.endswith(".png"):
            return str(document["file_id"])
    return ""


def _download_telegram_file(client: TelegramTestClient, file_id: str) -> bytes:
    import requests

    meta = client._post("getFile", json_body={"file_id": file_id})
    payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    file_path = str(result.get("file_path") or "")
    if not meta.get("ok") or not file_path:
        raise RuntimeError("telegram file missing")
    response = requests.get(
        f"https://api.telegram.org/file/bot{client.config.bot_token}/{file_path}",
        timeout=60,
    )
    response.raise_for_status()
    return response.content


def _accept_site_logo(runtime, chat_id: str, update: dict) -> None:
    from newsagent_v2.control.site_flow import fetch_categories
    from newsagent_v2.control.sites import attach_logo, confirm_text, logo_is_replacement, rankmath_warning_for, wordpress_config
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
    from newsagent_v2.wordpress.authors import list_site_authors

    log_event("[MESSAGE] site logo received")
    file_id = _incoming_image_id(update)
    try:
        raw = _download_telegram_file(runtime.client, file_id)
    except Exception as exc:
        log_event(f"[SITE] logo download failed {type(exc).__name__}")
        runtime.client.send_message(chat_id=chat_id, text="I could not read that photo. Send the logo again.")
        return
    replacing = logo_is_replacement(str(chat_id or ""))
    warning = rankmath_warning_for(str(chat_id or ""))
    site, reason = attach_logo(str(chat_id or ""), raw)
    if site is None:
        runtime.client.send_message(chat_id=chat_id, text=reason)
        return
    if replacing:
        _use_site(runtime, site)
        runtime.client.send_message(chat_id=chat_id, text=f"Logo replaced for {site.name}.")
        send_site_menu(runtime.client, runtime.config, chat_id=str(chat_id or ""))
        log_event(f"[SITE] logo replaced site={site.name}")
        return
    transport = build_live_wordpress_transport()
    categories = fetch_categories(site, transport)
    authors = list_site_authors(wordpress_config(site), transport)
    posts = _use_site(runtime, site)
    runtime.client.send_message(
        chat_id=chat_id,
        text=confirm_text(site, len(categories), len(authors), posts, warning),
    )
    send_site_menu(runtime.client, runtime.config, chat_id=str(chat_id or ""))
    log_event(f"[SITE] added={site.name} categories={len(categories)} authors={len(authors)} posts={posts}")


def _use_site(runtime, site) -> None:
    """Point drafts, authors, and the admin link at the chosen website."""
    from newsagent_v2.control.sites import activate, wordpress_config
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
    from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle

    activate(site.id, environ=os.environ)
    wp_config = wordpress_config(site)
    worker = runtime.generation_worker
    handler = runtime.review_handler
    lifecycle = getattr(worker, "wordpress_lifecycle", None) if worker is not None else None
    if lifecycle is None and worker is not None:
        lifecycle = WordPressDraftLifecycle(wp_config, build_live_wordpress_transport())
        worker.wordpress_lifecycle = lifecycle
        if handler is not None:
            handler.wordpress_lifecycle = lifecycle
    elif lifecycle is not None:
        lifecycle.config = wp_config
    posts = _index_site(site)
    log_event(f"[SITE] active={site.name} posts={posts}")
    return posts


def _index_site(site) -> int:
    """Read this website's sitemap and recent posts. Used for internal links."""
    from newsagent_v2.seo6.sitemap import load_site_index

    try:
        index = load_site_index(site.base_url)
    except Exception as exc:
        log_event(f"[SITE] sitemap failed site={site.name} {type(exc).__name__}")
        return 0
    log_event(f"[SITE] sitemap site={site.name} posts={len(index.posts)}")
    return len(index.posts)


def execute_make_with_acknowledgement(
    client: TelegramTestClient,
    config,
    state: V5BotState,
    discovery: V5DiscoveryPipeline,
    persistent_store: PersistentV5Store,
    runtime: V5BotRuntime,
    update_id: int,
    update: dict,
    selection: str = "trend",
    offer_batch: int = 10,
) -> dict[str, any]:
    """Execute /make with immediate acknowledgement and progress updates."""
    start_time = time.perf_counter()
    
    make_run_id = state.start_make_run(update_id)
    runtime.current_discovery_run_id = make_run_id
    log_event(f"[MAKE] accepted update_id={update_id} make_run_id={make_run_id}")
    
    ack_targets = operator_ids(config) or (str(config.test_chat_id),)
    ack = MakeAcknowledgement(
        client=client,
        chat_id=ack_targets[0],
        update_id=update_id,
        make_run_id=make_run_id,
        chat_ids=ack_targets,
    )
    
    log_event("[MAKE] acknowledgement_sending...")
    ack_result = ack.send_initial()
    if not ack_result.get("ok"):
        log_event(f"[MAKE ERROR] Failed to send acknowledgement: {ack_result}")
        return {"ok": False, "error": "acknowledgement_failed", "make_run_id": make_run_id}
    
    log_event(f"[MAKE] acknowledgement_sent message_id={ack_result.get('message_id')}")
    
    try:
        ack.update_progress("Starting news scan...")
        log_event("[MAKE] [DISCOVERY] started")
        
        ack.update_progress("Collecting sources...")
        log_event("[MAKE] [DISCOVERY] collector_started")
        
        from newsagent_v2.control.story_picker import (
            choice_label,
            order_stories,
            skips_age_cap,
            story_matches,
        )

        events = discovery.run_discovery(
            unlimited_age=skips_age_cap(selection),
            selection=selection,
            offer_batch=offer_batch,
        )
        page = order_stories(
            [event for event in events if story_matches(event, selection)],
            selection,
        )
        match = getattr(discovery, "_discovery_match", {})
        raw_count = len(page)
        log_event(
            f"[MAKE] [DISCOVERY] collector_finished raw={raw_count} "
            f"selection={selection} clustered={match.get('clustered', 0)} "
            f"matched={match.get('matched', 0)}"
        )
        ack.update_progress(f"{choice_label(selection)}: {raw_count} matching stories.")
        
        ack.update_progress(f"Found {raw_count} events. Checking which ones have enough sources to write...")
        log_event(f"[MAKE] [DISCOVERY] ranking_finished events={raw_count}")

        from newsagent_v2.control.offered_stories import OfferedStories
        from newsagent_v2.story6 import screen_story

        def _screen_progress(index: int, total: int, event) -> None:
            title = str(getattr(event, "canonical_title", "") or "")[:80]
            ack.update_progress(f"Checking sources {index}/{total}: {title}")
            log_event(f"[MAKE] [SCREEN] {index}/{total} event_id={getattr(event, 'event_id', '')}")

        postable, dropped, checked = select_postable(
            page,
            screen_story,
            limit=offer_batch,
            keep=offer_batch,
            progress=_screen_progress,
        )
        discovery.replace_ranked(postable)
        screened = list(page)[:checked]
        if screened:
            OfferedStories.load().remember(screened, site_id=os.environ.get("NEWSAGENT_ACTIVE_SITE_ID", ""))
            log_event(f"[MAKE] [OFFERED] remembered={len(screened)}")
        log_event(f"[MAKE] [SCREEN] checked={checked} postable={len(postable)} dropped={dropped}")
        
        # PERSIST discovery run for callbacks/pagination
        all_event_ids = [e.event_id for e in postable]
        from datetime import datetime, timezone
        discovery_run = DiscoveryRun(
            run_id=make_run_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            event_ids=all_event_ids,
            total_events=raw_count,
            chat_id=safe_get_chat_id(update) or config.test_chat_id,
        )
        persistent_store.save_discovery_run(discovery_run)
        log_event(f"[MAKE] persisted_discovery_run run_id={make_run_id} events={raw_count}")

        label = choice_label(selection)
        matched = int(match.get("matched") or 0)
        if not page and matched == 0:
            note = f"{label}: no stories in the feeds matched that choice."
        elif postable:
            note = f"{label}. Showing {len(postable)} {'story' if len(postable) == 1 else 'stories'} with enough sources to write."
            if dropped:
                note += f" {dropped} {'was' if dropped == 1 else 'were'} too thin to write and left out."
        elif not page:
            note = (
                f"{label}: found {matched} matching stories, "
                "but none had enough sources to write."
            )
        else:
            note = (
                f"Checked {checked} stories. None had enough verified sources to write, "
                "so no cards were sent."
            )
        if page:
            note += " The next scan skips this batch and continues with the following stories."
        ack.mark_complete(raw_count, note)
        
        top_count = len(postable)
        top_events = discovery.get_top_events(count=top_count, offset=0)
        
        event_ids = [e.event_id for e in top_events]
        if len(event_ids) != len(set(event_ids)):
            raise AssertionError(f"Duplicate event IDs: {event_ids}")
        
        log_event(f"[MAKE] [TELEGRAM] sending {len(top_events)} cards")
        
        for i, event in enumerate(top_events, 1):
            log_event(f"[MAKE] [TELEGRAM] sending_card rank={i} event_id={event.event_id}")
        
        results = discovery.send_to_telegram(
            client=client,
            config=config,
            count=top_count,
            offset=0,
        )
        
        cards_sent = len([r for r in results if r.get("ok")])
        log_event(f"[MAKE] [TELEGRAM] cards_sent={cards_sent}")

        auto = auto_generate_top(client, config, discovery, runtime)
        log_event(f"[MAKE] [AUTO] {auto}")

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log_event(f"[MAKE] completed duration_ms={duration_ms}")
        
        return {
            "ok": True,
            "cards_sent": cards_sent,
            "event_count": raw_count,
            "make_run_id": make_run_id,
            "auto_generate": auto,
        }
    
    except Exception as e:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log_event(f"[MAKE] [ERROR] {type(e).__name__}: {e}")
        log_event(f"[MAKE] FAILED stage=discovery duration_ms={duration_ms}")
        traceback.print_exc()
        
        try:
            ack.mark_failed(str(e))
        except Exception:
            pass
        
        return {"ok": False, "error": str(e), "make_run_id": make_run_id}


def _reply_author_menu(runtime: V5BotRuntime, chat_id: str) -> None:
    """List the site's WordPress authors and save the one the editor taps."""
    from newsagent_v2.wordpress.authors import author_keyboard, label_for, list_site_authors

    if runtime.client is None:
        return
    lifecycle = runtime.review_handler.wordpress_lifecycle if runtime.review_handler else None
    if lifecycle is None:
        runtime.client.send_message(chat_id=chat_id, text="WordPress is not connected, so authors cannot be listed.")
        return
    authors = list_site_authors(lifecycle.config, lifecycle.transport)
    if not authors:
        runtime.client.send_message(chat_id=chat_id, text="WordPress did not return any authors.")
        return
    runtime.client.send_message(
        chat_id=chat_id,
        text=f"Author for the next articles: {html.escape(label_for())}\nTap a name to change it.",
        parse_mode="HTML",
        reply_markup=author_keyboard(authors, callback_prefix="site_author"),
    )


def _edit_author_on_card(client: TelegramTestClient, update: dict, result: dict) -> bool:
    """Rewrite the review card in place: flow line, author, and author buttons."""
    from newsagent_v2.v5_generation.telegram_delivery import paint_author_flow, review_keyboard

    message = (update.get("callback_query") or {}).get("message") or {}
    message_id = message.get("message_id")
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not message_id or not chat_id:
        return False
    open_url = ""
    for row in (message.get("reply_markup") or {}).get("inline_keyboard") or []:
        for button in row:
            if button.get("text") == "OPEN DRAFT" and button.get("url"):
                open_url = str(button["url"])
    text = paint_author_flow(
        str(message.get("text") or ""),
        str(result.get("author_name") or ""),
        picking=bool(result.get("picking")),
    )
    markup = review_keyboard(
        str(result.get("event_id") or ""),
        str(result.get("article_version") or "v1"),
        str(result.get("image_version") or "v1"),
        open_draft_url=open_url or None,
        authors=result.get("authors") or [],
        selected_id=result.get("selected_id"),
    )
    edited = client.edit_message_text(
        chat_id=chat_id,
        message_id=int(message_id),
        text=text,
        parse_mode="HTML",
        reply_markup=markup,
    )
    return bool(edited.get("ok"))


# ============ REVIEW CALLBACK HANDLER ============

def _handle_review_callback(
    client: TelegramTestClient,
    config,
    state: V5BotState,
    runtime: V5BotRuntime,
    update_id: int,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """Handle review callbacks: rate, feedback, revise, approve, publish."""
    start_time = time.perf_counter()
    
    log_event(f"[REVIEW CALLBACK] data={callback_data}")
    
    try:
        # Get reviewer from callback
        cq = update.get("callback_query") or {}
        from_user = cq.get("from", {})
        reviewer = from_user.get("username") or from_user.get("first_name", "user")
        
        # Handle through review callback handler
        actor_chat = safe_get_chat_id(update) or str(config.test_chat_id)
        result = runtime.review_handler.handle(
            data=callback_data,
            reviewer=reviewer,
            chat_id=actor_chat,
        )
        
        action = result.get("action", "unknown")
        log_event(f"[REVIEW CALLBACK] action={action}")
        
        # Acknowledge callback
        message_text = result.get("message", "Done")
        try:
            client.answer_callback_query(
                callback_query_id=callback_query_id,
                text=message_text[:200],
            )
            log_event("[REVIEW CALLBACK] acknowledged")
        except Exception as e:
            log_event(f"[REVIEW CALLBACK ERROR] Failed to answer: {e}")
        
        # Keep the author choice on the same card so the flow stays visible.
        if result.get("edit_card"):
            edited = _edit_author_on_card(client, update, result)
            if not edited:
                broadcast_message(client, config, text=message_text, parse_mode="HTML")
        elif result.get("reply_markup"):
            # Prompt only the person who pressed the button; their next message is the reply.
            client.send_message(
                chat_id=actor_chat,
                text=message_text,
                parse_mode="HTML",
                reply_markup=result.get("reply_markup"),
            )
        elif action in ["rate_article", "rate_image", "feedback_captured",
                       "approve", "revise", "publish", "feedback_cancelled",
                       "set_author", "site_author"] or (
            not result.get("ok") and result.get("message")
        ):
            broadcast_message(client, config, text=message_text, parse_mode="HTML")
        
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log_event(f"[REVIEW CALLBACK] completed duration_ms={duration_ms}")
        
        return result
        
    except Exception as e:
        log_event(f"[REVIEW CALLBACK ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        
        try:
            client.answer_callback_query(
                callback_query_id=callback_query_id,
                text=f"Error: {str(e)[:100]}",
            )
        except Exception:
            pass
        
        return {"ok": False, "error": str(e), "callback_data": callback_data}


def _manage_site_callback(client, runtime, chat_id: str, callback_data: str) -> dict[str, any] | None:
    """Logo, password, and removal for one saved website."""
    from newsagent_v2.control.sites import (
        begin_logo,
        begin_replace_password,
        begin_replace_username,
        find_site,
        remove_confirm_keyboard,
        remove_site,
        settings_keyboard,
    )

    prefixes = (
        ("site:remove:yes:", "remove_yes"),
        ("site:remove:", "remove"),
        ("site:settings:", "settings"),
        ("site:logo:", "logo"),
        ("site:user:", "username"),
        ("site:password:", "password"),
    )
    action = ""
    site_id = ""
    for prefix, name in prefixes:
        if callback_data.startswith(prefix):
            action = name
            site_id = callback_data[len(prefix):]
            break
    if not action:
        return None
    if action == "remove_yes":
        removed = remove_site(site_id)
        if removed is None:
            client.send_message(chat_id=chat_id, text="That website is no longer saved. Send /start to see the list.")
            return {"ok": False, "error": "unknown_site"}
        if os.environ.get("NEWSAGENT_ACTIVE_SITE_ID") == removed.id:
            from newsagent_v2.control.sites import list_sites

            remaining = list_sites()
            if remaining:
                _use_site(runtime, remaining[0])
        send_site_menu(client, runtime.config, chat_id=str(chat_id or ""))
        log_event(f"[SITE] removed={removed.name}")
        return {"ok": True, "action": "remove_site", "site": removed.name}
    site = find_site(site_id, environ=os.environ)
    if site is None:
        client.send_message(chat_id=chat_id, text="That website is no longer saved. Send /start to see the list.")
        return {"ok": False, "error": "unknown_site"}
    if action == "settings":
        client.send_message(
            chat_id=chat_id,
            text=f"{site.name}\nAccount: {site.username}\n\nReplace the logo, username or email, application password, or remove this website.",
            reply_markup=settings_keyboard(site.id),
        )
        return {"ok": True, "action": "site_settings", "site": site.name}
    if action == "logo":
        client.send_message(chat_id=chat_id, text=begin_logo(chat_id, site, replacing=True))
        log_event(f"[SITE] logo replace site={site.name}")
        return {"ok": True, "action": "replace_logo", "site": site.name}
    if action == "username":
        client.send_message(chat_id=chat_id, text=begin_replace_username(chat_id, site))
        log_event(f"[SITE] username replace site={site.name}")
        return {"ok": True, "action": "replace_username", "site": site.name}
    if action == "password":
        client.send_message(chat_id=chat_id, text=begin_replace_password(chat_id, site))
        log_event(f"[SITE] password replace site={site.name}")
        return {"ok": True, "action": "replace_password", "site": site.name}
    client.send_message(
        chat_id=chat_id,
        text=f"Remove {site.name}? Articles already on the site stay there.",
        reply_markup=remove_confirm_keyboard(site.id),
    )
    return {"ok": True, "action": "confirm_remove", "site": site.name}


def _handle_site_callback(
    *,
    client: TelegramTestClient,
    config,
    runtime,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """Choose a website, or start adding one."""
    from newsagent_v2.control.sites import ADD_CALLBACK, FINISH_PREFIX, begin_add, begin_logo, find_site, is_ready

    chat_id = safe_get_chat_id(update) or ""
    try:
        client.answer_callback_query(callback_query_id=callback_query_id, text="Okay")
    except Exception as exc:
        log_event(f"[CALLBACK ERROR] Failed to answer site: {exc}")
    if callback_data == ADD_CALLBACK:
        prompt = begin_add(chat_id)
        client.send_message(chat_id=chat_id, text=prompt)
        log_event("[SITE] add started")
        return {"ok": True, "action": "add_site"}
    if str(callback_data).startswith(FINISH_PREFIX):
        site_id = str(callback_data)[len(FINISH_PREFIX):]
        site = find_site(site_id, environ=os.environ)
        if site is None:
            client.send_message(chat_id=chat_id, text="That website is no longer saved. Send /start to see the list.")
            return {"ok": False, "error": "unknown_site"}
        if is_ready(site):
            _use_site(runtime, site)
            _send_site_categories(client, chat_id, site)
            return {"ok": True, "action": "select_site", "site": site.name}
        prompt = begin_logo(chat_id, site)
        client.send_message(chat_id=chat_id, text=prompt)
        log_event(f"[SITE] logo requested site={site.name}")
        return {"ok": True, "action": "finish_setup"}
    managed = _manage_site_callback(client, runtime, chat_id, str(callback_data))
    if managed is not None:
        return managed
    site_id = callback_data.split(":", 1)[1]
    site = find_site(site_id, environ=os.environ)
    if site is None:
        client.send_message(chat_id=chat_id, text="That website is no longer saved. Send /start to see the list.")
        return {"ok": False, "error": "unknown_site"}
    _use_site(runtime, site)
    _send_site_categories(client, chat_id, site)
    return {"ok": True, "action": "select_site", "site": site.name}


def _send_site_categories(client, chat_id: str, site) -> None:
    """The categories that exist on this website, not a shared list."""
    from newsagent_v2.control.site_flow import (
        category_keyboard,
        category_menu_text,
        fetch_categories,
        store_categories,
    )
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport

    categories = fetch_categories(site, build_live_wordpress_transport())
    store_categories(site.id, categories)
    client.send_message(
        chat_id=chat_id,
        text=category_menu_text(site.name, len(categories)),
        reply_markup=category_keyboard(site.id, categories),
    )
    log_event(f"[SITE] categories site={site.name} count={len(categories)}")


def _handle_category_callback(
    *,
    client: TelegramTestClient,
    runtime,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """A category on the chosen website opens that site's authors."""
    from newsagent_v2.control.site_flow import (
        author_keyboard,
        author_menu_text,
        cached_categories,
        category_name_for,
        fetch_categories,
        remember_category,
        store_categories,
    )
    from newsagent_v2.control.sites import find_site, wordpress_config
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
    from newsagent_v2.wordpress.authors import list_site_authors

    chat_id = safe_get_chat_id(update) or ""
    try:
        client.answer_callback_query(callback_query_id=callback_query_id, text="Okay")
    except Exception as exc:
        log_event(f"[CALLBACK ERROR] Failed to answer category: {exc}")
    _prefix, site_id, category_text = (callback_data.split(":") + ["", ""])[:3]
    site = find_site(site_id, environ=os.environ)
    if site is None:
        client.send_message(chat_id=chat_id, text="That website is no longer saved. Send /start.")
        return {"ok": False, "error": "unknown_site"}
    try:
        category_id = int(category_text)
    except ValueError:
        return {"ok": False, "error": "bad_category"}
    categories = cached_categories(site.id) or fetch_categories(site, build_live_wordpress_transport())
    store_categories(site.id, categories)
    name = category_name_for(site.id, category_id, categories)
    if not name:
        client.send_message(chat_id=chat_id, text="That category is no longer on the site. Send /start.")
        return {"ok": False, "error": "unknown_category"}
    _use_site(runtime, site)
    remember_category(chat_id, site.id, category_id, name)
    transport = build_live_wordpress_transport()
    authors = list_site_authors(wordpress_config(site), transport)
    log_event(f"[SITE] authors site={site.name} category={name} count={len(authors)}")
    if not authors:
        client.send_message(chat_id=chat_id, text=f"{site.name} did not return any authors for {name}.")
        return {"ok": False, "error": "no_authors"}
    client.send_message(
        chat_id=chat_id,
        text=author_menu_text(site.name, name, len(authors)),
        reply_markup=author_keyboard(site.id, authors),
    )
    return {"ok": True, "action": "site_category", "category": name}


def _handle_site_author_callback(
    *,
    client: TelegramTestClient,
    config,
    state: V5BotState,
    discovery: V5DiscoveryPipeline,
    runtime,
    update_id: int,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """The author completes this website's flow and starts the scan."""
    from newsagent_v2.control.site_flow import pending_category, remember_publication
    from newsagent_v2.control.sites import find_site, wordpress_config
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
    from newsagent_v2.wordpress.authors import find_author, list_site_authors

    chat_id = safe_get_chat_id(update) or ""
    _prefix, site_id, user_text = (callback_data.split(":") + ["", ""])[:3]
    site = find_site(site_id, environ=os.environ)
    chosen = pending_category(chat_id)
    if site is None or chosen is None or chosen.get("site_id") != site_id:
        try:
            client.answer_callback_query(callback_query_id=callback_query_id, text="Pick a category first")
        except Exception:
            pass
        client.send_message(chat_id=chat_id, text="Pick the website and a category first. Send /start.")
        return {"ok": False, "error": "no_category"}
    authors = list_site_authors(wordpress_config(site), build_live_wordpress_transport())
    author = find_author(authors, user_text)
    if author is None:
        try:
            client.answer_callback_query(callback_query_id=callback_query_id, text="Author not found")
        except Exception:
            pass
        return {"ok": False, "error": "unknown_author"}
    _use_site(runtime, site)
    remember_publication(site.id, int(chosen["category_id"]), str(chosen["category_name"]), author)
    try:
        client.answer_callback_query(
            callback_query_id=callback_query_id,
            text=f"{chosen['category_name']} · {author.name}"[:200],
        )
    except Exception as exc:
        log_event(f"[CALLBACK ERROR] Failed to answer author: {exc}")
    log_event(f"[SITE] flow site={site.name} category={chosen['category_name']} author={author.name}")
    from newsagent_v2.control.site_flow import fetch_count_keyboard, fetch_count_text

    client.send_message(
        chat_id=chat_id,
        text=fetch_count_text(site.name, str(chosen["category_name"]), author.name),
        reply_markup=fetch_count_keyboard(),
    )
    return {"ok": True, "action": "fetch_count", "category": chosen["category_name"], "author": author.name}


def _handle_fetch_count_callback(
    *,
    client: TelegramTestClient,
    config,
    state: V5BotState,
    discovery: V5DiscoveryPipeline,
    runtime,
    update_id: int,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """The byline is already saved. This button chooses how many cards to send."""
    from newsagent_v2.control.site_flow import clamp_fetch_count, pending_category

    chat_id = safe_get_chat_id(update) or ""
    count = clamp_fetch_count(str(callback_data).split(":", 1)[1])
    chosen = pending_category(chat_id)
    if count is None or chosen is None:
        try:
            client.answer_callback_query(callback_query_id=callback_query_id, text="Pick a category and author first")
        except Exception:
            pass
        client.send_message(chat_id=chat_id, text="Pick the website, a category, and an author first. Send /start.")
        return {"ok": False, "error": "no_fetch"}
    try:
        client.answer_callback_query(callback_query_id=callback_query_id, text=f"Fetching {count}")
    except Exception as exc:
        log_event(f"[CALLBACK ERROR] Failed to answer fetch count: {exc}")
    log_event(f"[SITE] fetch count={count} category={chosen.get('category_name')}")
    return execute_make_with_acknowledgement(
        client=client,
        config=config,
        state=state,
        discovery=discovery,
        persistent_store=runtime.persistent_store,
        runtime=runtime,
        update_id=update_id,
        update=update,
        selection=str(chosen["category_name"]),
        offer_batch=count,
    )


# ============ CALLBACK HANDLER ============

def execute_callback(
    client: TelegramTestClient,
    config,
    state: V5BotState,
    discovery: V5DiscoveryPipeline,
    runtime: V5BotRuntime,
    update_id: int,
    update: dict,
) -> dict[str, any]:
    """Execute callback_query with full diagnostics and generation support."""
    start_time = time.perf_counter()
    
    callback_query_id = safe_get_callback_query_id(update)
    callback_data = safe_get_callback_data(update)
    
    log_event(f"[CALLBACK] update_id={update_id}")
    log_event(f"[CALLBACK] query_id={callback_query_id}")
    log_event(f"[CALLBACK] data={callback_data}")
    
    if not callback_query_id:
        log_event("[CALLBACK ERROR] No callback_query.id found")
        return {"ok": False, "error": "no_query_id"}

    if not callback_data:
        log_event("[CALLBACK ERROR] No callback_data found")
        try:
            client.answer_callback_query(
                callback_query_id=callback_query_id,
                text="Error: Empty callback",
            )
            log_event("[CALLBACK] acknowledged (empty data)")
        except Exception as e:
            log_event(f"[CALLBACK ERROR] Failed to answer: {e}")
        return {"ok": False, "error": "no_callback_data"}

    if str(callback_data).startswith("c:"):
        return _handle_category_callback(
            client=client,
            runtime=runtime,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
        )

    if str(callback_data).startswith("fetch:"):
        return _handle_fetch_count_callback(
            client=client,
            config=config,
            state=state,
            discovery=discovery,
            runtime=runtime,
            update_id=update_id,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
        )

    if str(callback_data).startswith("a:"):
        return _handle_site_author_callback(
            client=client,
            config=config,
            state=state,
            discovery=discovery,
            runtime=runtime,
            update_id=update_id,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
        )

    if str(callback_data) == "site:add" or str(callback_data).startswith("site:"):
        return _handle_site_callback(
            client=client,
            config=config,
            runtime=runtime,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
        )

    if str(callback_data).startswith("pick:"):
        choice = str(callback_data).split(":", 1)[1]
        from newsagent_v2.control.story_picker import CHOICES, choice_label

        known = {key for key, _label in CHOICES}
        try:
            client.answer_callback_query(
                callback_query_id=callback_query_id,
                text=f"Scanning {choice_label(choice)}"[:200],
            )
        except Exception as exc:
            log_event(f"[CALLBACK ERROR] Failed to answer pick: {exc}")
        if choice not in known:
            return {"ok": False, "error": "unknown_pick"}
        return execute_make_with_acknowledgement(
            client=client,
            config=config,
            state=state,
            discovery=discovery,
            persistent_store=runtime.persistent_store,
            runtime=runtime,
            update_id=update_id,
            update=update,
            selection=choice,
        )
    
    # ==== REVIEW CALLBACKS (rate, feedback, revise, approve, publish) ====
    # Check if this is a review callback first
    if runtime.review_handler and runtime.review_handler.parse_callback(callback_data):
        return _handle_review_callback(
            client=client,
            config=config,
            state=state,
            runtime=runtime,
            update_id=update_id,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=callback_data,
        )
    
    # ==== DISCOVERY CALLBACKS (RUN/FOLLOW/IGNORE/SEE NEXT) ====
    handler = V5CallbackHandler(
        event_store=discovery.event_store,
        telegram_store=discovery.telegram_store,
        generation_worker=runtime.generation_worker,
        preflight=runtime.preflight,
        environ=os.environ,
    )
    
    try:
        log_event("[CALLBACK] handler_started")
        result = handler.handle(callback_data)
        action = result.get("action", "unknown")
        log_event(f"[CALLBACK] action={action}")
        
        # Acknowledge with message
        message_text = result.get("message", "Done")
        try:
            client.answer_callback_query(
                callback_query_id=callback_query_id,
                text=message_text[:200],  # Truncate for safety
            )
            log_event("[CALLBACK] acknowledged")
        except Exception as e:
            log_event(f"[CALLBACK ERROR] Failed to answer: {e}")
        
        # Handle SEE NEXT 5
        if action == "see_next" and result.get("events"):
            events_to_send = result.get("events", [])
            offset = result.get("offset", 5)
            log_event(f"[CALLBACK] sending {len(events_to_send)} events from offset={offset}")
            
            for i, event in enumerate(events_to_send, offset + 1):
                log_event(f"[CALLBACK] [TELEGRAM] sending_card rank={i} event_id={event.event_id}")
            
            # Re-construct the discovery pipeline approach for sending
            results = discovery.send_to_telegram(
                client=client,
                config=config,
                count=len(events_to_send),
                offset=offset,
            )
            cards_sent = len([r for r in results if r.get("ok")])
            log_event(f"[CALLBACK] [TELEGRAM] cards_sent={cards_sent}")
            
            # Send navigation if more exist
            ranked = []
            if hasattr(discovery, "get_ranked_events"):
                ranked = discovery.get_ranked_events() or []
            elif hasattr(discovery, "_ranked_events"):
                ranked = list(getattr(discovery, "_ranked_events") or [])
            total = len(ranked)
            if offset + len(events_to_send) < total:
                broadcast_message(
                    client,
                    config,
                    text=f"📄 Showing {offset + len(events_to_send)}/{total} events",
                    parse_mode="HTML",
                    reply_markup=discovery.seepage_keyboard(offset=offset + len(events_to_send)),
                )
                log_event("[CALLBACK] [TELEGRAM] navigation_sent")
        
        # Handle RUN STORY
        if action == "run_story":
            # Controlled E2E mode: send confirmation keyboard
            if result.get("controlled_e2e") and result.get("awaiting_confirmation"):
                log_event(f"[CONTROLLED] confirmation_sent event_id={result.get('event_id')}")
                broadcast_message(
                    client,
                    config,
                    text=result.get("message", "Review selection"),
                    parse_mode="HTML",
                    reply_markup=result.get("reply_markup"),
                )
            # Normal mode: send progress if already started
            elif result.get("started"):
                log_event(f"[CALLBACK] [RUN STORY] job_id={result.get('job_id')} state={result.get('job_state')}")
                # Could send follow-up message about generation starting
                if result.get("new"):
                    broadcast_message(
                        client,
                        config,
                        text=f"📝 Story generation started for: {result.get('headline', 'Unknown')}",
                        parse_mode="HTML",
                    )
        
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log_event(f"[CALLBACK] completed duration_ms={duration_ms}")
        
        return result
    
    except Exception as e:
        log_event(f"[CALLBACK ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        
        # Still try to answer
        try:
            client.answer_callback_query(
                callback_query_id=callback_query_id,
                text=f"Error: {str(e)[:100]}",
            )
        except Exception:
            pass
        
        return {"ok": False, "error": str(e), "callback_data": callback_data}


# ============ RUNTIME ============

class V5BotRuntime:
    """Runtime container for all initialized components."""
    def __init__(self) -> None:
        self.state: V5BotState | None = None
        self.config: Any = None
        self.client: TelegramTestClient | None = None
        self.discovery: V5DiscoveryPipeline | None = None
        self.event_store: EventStore | None = None
        self.source_registry: SourceRegistry | None = None
        self.offset: int | None = None
        # V5 Generation components
        self.preflight: ProviderPreflight | None = None
        self.version_store: VersionStore | None = None
        self.approval_store: ApprovalStore | None = None
        self.generation_worker: GenerationWorker | None = None
        self.persistent_store: PersistentV5Store | None = None
        self.review_store: PersistentReviewStore | None = None
        self.review_handler: V5ReviewCallbackHandler | None = None
        self._controlled_e2e: bool = False


def _progress_callback(update: Any) -> None:
    """Handle progress updates from generation worker."""
    log_event(f"[GENERATION] {update.job_id}: {update.state} - {update.message}")


def build_runtime() -> tuple[V5BotRuntime, dict]:
    """Build complete runtime with all dependencies verified."""
    runtime = V5BotRuntime()
    
    token = os.environ.get("NEWSAGENT_V2_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get(CHAT_ENV, "").strip()
    
    if not token or not chat_id:
        raise ValueError("Missing credentials")
    
    try:
        acquire_singleton_lock()
    except SingletonError as e:
        raise SingletonError(str(e))
    
    runtime.config = load_telegram_config(os.environ)
    
    transport = create_live_transport(runtime.config)
    runtime.client = TelegramTestClient(
        config=runtime.config,
        transport=transport,
        live_send_enabled=True,
    )
    
    test_result = runtime.client._post("getMe", json_body={})
    if not test_result.get("ok"):
        status = test_result.get("status_code", "unknown")
        raise RuntimeError(f"Telegram connection failed: HTTP {status}")
    
    bot_info = test_result.get("payload", {}).get("result", {})
    
    # Initialize V5 Generation components
    V5_STATE_DIR.mkdir(parents=True, exist_ok=True)
    runtime.persistent_store = PersistentV5Store(V5_STATE_DIR)
    runtime.review_store = PersistentReviewStore(V5_STATE_DIR)
    
    # Provider preflight check
    runtime.preflight = ProviderPreflight(os.environ)
    writer_ready = runtime.preflight.check_writer()
    image_ready = runtime.preflight.check_image()
    
    # Log preflight status
    if writer_ready.status == "READY" and image_ready.status == "READY":
        log_event("[PREFLIGHT] Writer + Image ready")
    elif writer_ready.status == "READY":
        log_event(f"[PREFLIGHT] Writer ready, Image: {image_ready.status}")
    else:
        log_event(f"[PREFLIGHT] Writer: {writer_ready.status}, Image: {image_ready.status}")
    
    # Version store shared with the worker and review handler.
    # MUST match GenerationWorker default (output/v5_stories). A separate
    # data/v5_versions root made APPROVE/PUBLISH unable to find frozen articles.
    from pathlib import Path as _Path
    from newsagent_v2.v5_generation.version_store import DEFAULT_STORE_ROOT

    version_root_env = str(os.environ.get("V5_VERSION_STORE_ROOT") or "").strip()
    version_root = _Path(version_root_env) if version_root_env else DEFAULT_STORE_ROOT
    runtime.version_store = VersionStore(root=version_root)
    runtime.approval_store = ApprovalStore(root=_Path("./data/v5_approval"))
    log_event(f"[STORE] version_store={runtime.version_store.root}")

    # Wire WordPress draft lifecycle when credentials are present so GENERATE
    # creates a draft and PUBLISH can promote it.
    wordpress_lifecycle = None
    try:
        from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
        from newsagent_v2.wordpress.config import load_wordpress_config
        from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle

        wp_status = runtime.preflight.check_wordpress()
        if wp_status.status == "READY":
            from newsagent_v2.control.sites import activate, active_id, list_sites, wordpress_config

            wordpress_lifecycle = WordPressDraftLifecycle(
                config=load_wordpress_config(os.environ),
                transport=build_live_wordpress_transport(),
            )
            saved = list_sites(environ=os.environ)
            chosen = active_id() or (saved[0].id if saved else "")
            current = next((site for site in saved if site.id == chosen), None)
            if current is not None:
                activate(current.id, environ=os.environ)
                wordpress_lifecycle.config = wordpress_config(current)
                log_event(f"[PREFLIGHT] WordPress draft lifecycle READY site={current.name}")
            else:
                log_event("[PREFLIGHT] WordPress draft lifecycle READY")
        else:
            log_event(f"[PREFLIGHT] WordPress: {wp_status.status} (publish disabled)")
    except Exception as exc:  # noqa: BLE001 — keep bot up if WP wiring fails
        log_event(f"[PREFLIGHT] WordPress lifecycle unavailable: {exc}")
    
    runtime._controlled_e2e = os.environ.get("NEWSAGENT_V5_CONTROLLED_E2E", "").lower() == "true"

    # Run reconciliation for any interrupted jobs
    reconciled = runtime.persistent_store.reconcile_jobs_on_startup()
    if reconciled:
        log_event(f"[RECONCILE] {len(reconciled)} jobs reconciled:")
        for job in reconciled:
            log_event(f"  - {job['job_id']}: {job['old_state']} → {job['new_state']}")

    runtime.event_store = EventStore(root=_Path("./data/events"))
    runtime.generation_worker = GenerationWorker(
        client=runtime.client,
        config=runtime.config,
        persistent_store=runtime.persistent_store,
        environ=os.environ,
        wordpress_lifecycle=wordpress_lifecycle,
        event_store=runtime.event_store,
    )

    # Review handler (persistent_store for feedback/edit mode; worker for REVISE/EDIT)
    runtime.review_handler = V5ReviewCallbackHandler(
        review_store=runtime.review_store,
        version_store=runtime.version_store,
        persistent_store=runtime.persistent_store,
        wordpress_lifecycle=wordpress_lifecycle,
        environ=os.environ,
        generation_worker=runtime.generation_worker,
    )
    runtime.source_registry = SourceRegistry()
    
    runtime.discovery = create_v5_discovery_pipeline(
        event_store=runtime.event_store,
        source_registry=runtime.source_registry,
    )
    
    runtime.state = V5BotState()
    
    return runtime, bot_info


def main() -> int:
    print("=" * 60, flush=True)
    print("V5 Telegram Bot Starting", flush=True)
    print("=" * 60, flush=True)
    print(f"PID: {os.getpid()}", flush=True)
    print(f"PPID: {os.getppid()}", flush=True)
    
    try:
        runtime, bot_info = build_runtime()
    except SingletonError as e:
        print(f"[ERROR] {e}", flush=True)
        return 1
    except ValueError as e:
        print(f"[ERROR] {e}", flush=True)
        return 1
    except RuntimeError as e:
        print(f"[ERROR] {e}", flush=True)
        return 1
    
    atexit.register(release_singleton_lock)
    
    log_event("Token: SET")
    log_event(f"Chat IDs: {', '.join(runtime.config.chat_ids)}")
    log_event("LIVE transport configured")
    log_event(f"[OK] Connected as @{bot_info.get('username')}")
    
    # Log generation mode
    if runtime._controlled_e2e:
        log_event("[!] CONTROLLED E2E MODE: Automatic generation disabled")
    
    runtime.offset = handle_startup_backlog(runtime.client, runtime.state, runtime.config)
    
    def signal_handler(signum, frame):
        log_event("")
        log_event("[SHUTDOWN] Signal received")
        release_singleton_lock()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    log_event("=" * 60)
    log_event("BOT IS RUNNING")
    log_event("=" * 60)
    log_event("")
    log_event("Listening for commands...")
    log_event("  - /start -> websites, then TRENDING, 6 HOURS, or a category")
    try:
        send_site_menu(runtime.client, runtime.config)
        log_event("[MENU] website list sent")
    except Exception as exc:
        log_event(f"[MENU] send failed: {exc}")
    log_event("  - [Buttons] RUN/FOLLOW/IGNORE/SEE NEXT")
    if runtime._controlled_e2e:
        log_event("  - [E2E MODE] RUN STORY does NOT auto-generate")
    else:
        log_event("  - RUN STORY -> Async generation")
    log_event("Press Ctrl+C to stop")
    log_event(f"offset_before={runtime.offset}")
    log_event("")
    
    # MAIN POLLING LOOP
    while True:
        try:
            body: dict[str, any] = {"timeout": 10, "limit": 10}
            if runtime.offset is not None:
                body["offset"] = runtime.offset
            
            log_event(f"[POLL] waiting... timeout=10s")
            response = runtime.client._post(SEND_TYPE_GET_UPDATES, json_body=body)
            
            if not response.get("ok"):
                log_event(f"[POLL ERROR] HTTP {response.get('status_code')}")
                time.sleep(1)
                continue
            
            payload = response.get("payload", {})
            updates = payload.get("result", [])
            
            if not isinstance(updates, list):
                continue
            
            if len(updates) == 0:
                if runtime.offset is not None:
                    log_event(f"[POLL] no updates, offset={runtime.offset}")
                continue
            
            log_event(f"[POLL] updates_received={len(updates)}")
            
            for update in sorted(updates, key=lambda u: u.get("update_id", 0)):
                update_id = safe_get_update_id(update)
                if update_id is None:
                    continue

                log_event(f"[UPDATE] id={update_id}")

                # Idempotency
                if runtime.state.is_update_processed(update_id):
                    log_event(f"[SKIP] update_id={update_id} already processed")
                    runtime.offset = update_id + 1
                    persist_offset(runtime.offset)
                    continue

                # CRITICAL: Advance offset BEFORE processing to prevent poison replay
                # Even if handler crashes, we won't retry this update
                runtime.offset = update_id + 1
                persist_offset(runtime.offset)

                # ROUTE BY UPDATE TYPE - wrapped in try/except to ensure errors don't cause replay
                try:
                    set_origin_chat("")
                    if is_callback_update(update):
                        # HANDLE CALLBACK
                        chat_id = safe_get_chat_id(update)
                        if not runtime.config.allows(chat_id):
                            if chat_id:
                                log_event(f"[SKIP] wrong chat_id={chat_id}")
                            runtime.state.mark_update_processed(update_id)
                            continue
                        set_origin_chat(chat_id)

                        execute_callback(
                            client=runtime.client,
                            config=runtime.config,
                            state=runtime.state,
                            discovery=runtime.discovery,
                            runtime=runtime,
                            update_id=update_id,
                            update=update,
                        )

                    elif is_message_update(update):
                        # HANDLE MESSAGE (/make, etc)
                        chat_id = safe_get_chat_id(update)
                        if not runtime.config.allows(chat_id):
                            if chat_id:
                                log_event(f"[SKIP] wrong chat_id={chat_id}")
                            runtime.state.mark_update_processed(update_id)
                            continue
                        set_origin_chat(chat_id)

                        text = safe_get_message_text(update)
                        from newsagent_v2.control.sites import logo_step, onboarding_hides_text

                        if logo_step(str(chat_id or "")) and _incoming_image_id(update):
                            _accept_site_logo(runtime, chat_id, update)
                            runtime.state.mark_update_processed(update_id)
                            continue

                        if onboarding_hides_text(str(chat_id or "")):
                            log_event("[MESSAGE] site onboarding (text hidden)")
                        else:
                            raw_preview = (text or "").strip()
                            preview_cmd = raw_preview.split()[0].split("@", 1)[0].lower() if raw_preview else ""
                            log_event(f"[MESSAGE] text={raw_preview!r} cmd={preview_cmd.strip('!.?,:;…')!r}")

                        # ==== CHECK FOR FEEDBACK MODE ====
                        # First check if this message is feedback for awaiting story
                        if runtime.review_handler:
                            from_user = update.get("message", {}).get("from", {})
                            reviewer = from_user.get("username") or from_user.get("first_name", "user")

                            feedback_result = runtime.review_handler.check_and_capture_feedback_text(
                                chat_id=chat_id,
                                text=text,
                                reviewer=reviewer,
                            )
                            if feedback_result:
                                log_event(f"[FEEDBACK] captured for event={feedback_result.get('event_id')}")
                                # Send confirmation
                                runtime.client.send_message(
                                    chat_id=chat_id,
                                    text=feedback_result.get("message", "✅ Feedback saved."),
                                    parse_mode="HTML",
                                )
                                runtime.state.mark_update_processed(update_id)
                                continue

                        # ==== COMMAND HANDLING ====
                        raw_text = (text or "").strip()
                        cmd_token = raw_text.split()[0] if raw_text else ""
                        # /make@BotName → /make; hey! → hey
                        cmd = cmd_token.split("@", 1)[0].lower()
                        cmd_key = cmd.strip("!.?,:;…")
                        bot_username = str(bot_info.get("username") or "")
                        cmd_target = cmd_token.split("@", 1)[1].lower() if cmd_token.startswith("/") and "@" in cmd_token else ""
                        if cmd_target and bot_username and cmd_target != bot_username.lower():
                            cmd_key = ""  # /start@SomeOtherBot

                        def _reply(body: str) -> None:
                            send_result = runtime.client.send_message(
                                chat_id=chat_id,
                                text=html.escape(body),
                            )
                            if not send_result.get("ok"):
                                log_event(
                                    f"[MESSAGE] send_failed err={send_result.get('error')!r} "
                                    f"desc={send_result.get('telegram_description')!r}"
                                )

                        from newsagent_v2.control.sites import (
                            ASK_USERNAME,
                            PASSWORD_PROMPT,
                            match_site,
                            open_site,
                            resume_password,
                            resume_username,
                            save_site,
                            take_reply,
                        )

                        outcome = take_reply(str(chat_id or ""), raw_text)
                        if outcome is not None:
                            if outcome.get("ready"):
                                from newsagent_v2.wordpress.adapter import build_live_wordpress_transport

                                transport = build_live_wordpress_transport()
                                if outcome.get("username"):
                                    site, message = open_site(
                                        outcome["base_url"],
                                        outcome["username"],
                                        outcome["app_password"],
                                        transport,
                                        discover=not outcome.get("replacing"),
                                    )
                                else:
                                    site, message = match_site(
                                        outcome["base_url"],
                                        outcome["app_password"],
                                        transport,
                                    )
                                if site is None and message == ASK_USERNAME:
                                    resume_username(str(chat_id or ""), outcome["base_url"], outcome["app_password"])
                                    runtime.client.send_message(chat_id=chat_id, text=message)
                                    log_event("[SITE] add needs username")
                                elif site is None:
                                    if outcome.get("username"):
                                        resume_password(str(chat_id or ""), outcome["base_url"], outcome["username"])
                                    runtime.client.send_message(
                                        chat_id=chat_id,
                                        text=f"{message}\n\n{PASSWORD_PROMPT}" if outcome.get("username") else message,
                                    )
                                    log_event("[SITE] add rejected " + (message.splitlines()[0] if message else ""))
                                else:
                                    from newsagent_v2.control.sites import (
                                        adopt_saved_logo,
                                        begin_logo,
                                        hold_password,
                                        is_ready,
                                        publishing_rights,
                                        rights_message,
                                    )

                                    missing, warning = publishing_rights(
                                        site.base_url,
                                        site.username,
                                        site.app_password,
                                        transport,
                                    )
                                    if missing:
                                        hold_password(
                                            str(chat_id or ""),
                                            outcome["base_url"],
                                            outcome.get("username") or "",
                                            replacing=bool(outcome.get("replacing")),
                                            site_id=outcome.get("site_id") or site.id,
                                        )
                                        runtime.client.send_message(chat_id=chat_id, text=rights_message(missing))
                                        log_event("[SITE] rights missing " + ",".join(missing))
                                    elif outcome.get("replacing"):
                                        site = adopt_saved_logo(site)
                                        save_site(site)
                                        _use_site(runtime, site)
                                        runtime.client.send_message(
                                            chat_id=chat_id,
                                            text=f"Password replaced for {site.name}.",
                                        )
                                        send_site_menu(runtime.client, runtime.config, chat_id=str(chat_id or ""))
                                        log_event(f"[SITE] password replaced site={site.name}")
                                    else:
                                        site = adopt_saved_logo(site)
                                        if is_ready(site):
                                            save_site(site)
                                            _use_site(runtime, site)
                                            runtime.client.send_message(chat_id=chat_id, text=message)
                                            send_site_menu(runtime.client, runtime.config, chat_id=str(chat_id or ""))
                                            log_event(f"[SITE] added={site.name}")
                                        else:
                                            prompt = begin_logo(
                                                str(chat_id or ""),
                                                site,
                                                rankmath_warning=warning,
                                            )
                                            runtime.client.send_message(chat_id=chat_id, text=prompt)
                                            log_event(f"[SITE] logo requested site={site.name}")
                            elif outcome.get("cancel"):
                                send_site_menu(runtime.client, runtime.config, chat_id=str(chat_id or ""))
                                log_event("[SITE] add cancelled")
                            else:
                                runtime.client.send_message(chat_id=chat_id, text=outcome.get("text") or "")
                                log_event("[SITE] add step")
                            runtime.state.mark_update_processed(update_id)
                            continue

                        if cmd_key == "/start":
                            send_site_menu(runtime.client, runtime.config, chat_id=str(chat_id or ""))
                            log_event("[START] websites_sent")
                        elif cmd_key == "/make":
                            _reply("Send /start to choose a website.")
                            log_event("[MAKE] redirected_to_start")
                        elif cmd_key == "/author":
                            _reply_author_menu(runtime, chat_id)
                            log_event("[MESSAGE] author_menu")
                        elif cmd_key == "/help":
                            _reply(HELP_TEXT)
                            log_event("[MESSAGE] help_reply")
                        elif is_group_chat(update, str(chat_id or "")) and not mentions_bot(update, bot_username):
                            log_event("[MESSAGE] group ignored (bot not tagged)")
                        else:
                            question = raw_text
                            if is_group_chat(update, str(chat_id or "")):
                                question = strip_bot_mention(raw_text, bot_username)
                            if not question:
                                _reply(HELP_TEXT)
                                log_event("[MESSAGE] help_reply")
                            else:
                                try:
                                    log_event(f"[MESSAGE] hermes_chat start cmd={cmd_key!r}")
                                    chat_answer = hermes_chat_reply(question, os.environ)
                                    _reply(chat_answer)
                                    log_event(
                                        f"[MESSAGE] hermes_chat ok chars={len(chat_answer)}"
                                    )
                                except Exception as chat_error:
                                    log_event(
                                        f"[MESSAGE] hermes_chat failed: "
                                        f"{type(chat_error).__name__}: {chat_error}"
                                    )
                                    _reply(
                                        "I couldn't reach Hermes just now. "
                                        "Send /start to choose stories, or /help for commands."
                                    )

                    else:
                        log_event(f"[SKIP] unknown update type: {list(update.keys())}")

                    # Mark processed after successful handling
                    runtime.state.mark_update_processed(update_id)
                    log_event(f"[OFFSET] offset_after={runtime.offset}")

                except Exception as handler_error:
                    # CRITICAL: Log the error but don't retry - offset already advanced
                    log_event(f"[HANDLER ERROR] update_id={update_id}: {type(handler_error).__name__}: {handler_error}")
                    traceback.print_exc()
                    # Still mark as processed so we don't retry
                    runtime.state.mark_update_processed(update_id)
                finally:
                    set_origin_chat("")
            
        except KeyboardInterrupt:
            log_event("")
            log_event("[SHUTDOWN] Interrupted")
            break
        except Exception as e:
            log_event(f"[ERROR] {type(e).__name__}: {e}")
            traceback.print_exc()
            time.sleep(1)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
