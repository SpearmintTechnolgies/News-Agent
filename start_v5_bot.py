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
import contextvars
import html
import os
import re
import signal
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

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
from newsagent_v2.telegram.operators import actor_from_update, actor_id, set_actor_id, broadcast_message, operator_ids, set_origin_chat
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
    "• /autopilot — idle auto-publish: hours quiet + articles per site\n"
    "• RUN STORY writes only the card you tap\n"
    "• /author — choose the byline used for the next articles\n"
    "• /voice — choose the narration voice for article audio (Amazon Polly)\n"
    "• Each draft arrives as a review card: REVISE, EDIT, CHANGE AUTHOR, REJECT, 🔊 LISTEN or APPROVE & PUBLISH\n\n"
    "Anything else goes to Hermes. In a group, tag the bot first. A direct message does not need a tag."
)
_BOT_COMMANDS = frozenset({"/start", "/make", "/author", "/voice", "/help", "/autopilot"})
ADMIN_HELP = (
    "\n\n🛠 Admin (super admin only):\n"
    "• /admin — access control panel: users, groups, requests, blocked, daily limit; 🔐 Menu on a user or group switches on Publish, Autopilot, Manage websites or Report for them\n"
    "• /adduser <id> [name] · /removeuser <id>\n"
    "• /allowgroup (inside a group) · /removegroup\n"
    "• /limit <n> — stories per approved user per day (0 = unlimited)\n"
    "• /myid — show a Telegram ID"
)


def help_for(actor: str) -> str:
    from newsagent_v2.telegram import access
    return HELP_TEXT + (ADMIN_HELP if access.is_admin(actor) else "")


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

# Review actions that hit WordPress / the writer and must not block the poller.
_HEAVY_REVIEW_ACTIONS = frozenset({
    "approve", "publish", "revise", "edit", "unpublish", "set_author", "site_author",
})


def _ack_callback(client: TelegramTestClient, callback_query_id: str, text: str = "Okay") -> None:
    """Clear the Telegram button spinner immediately."""
    try:
        client.answer_callback_query(
            callback_query_id=callback_query_id,
            text=(text or "Okay")[:200],
        )
    except Exception as exc:
        log_event(f"[CALLBACK ERROR] Failed to answer: {exc}")


def _run_in_background(name: str, fn: Callable[[], Any]) -> None:
    """Keep getUpdates free while long scans / publishes run.

    Copies the caller's contextvars (origin chat) into the worker thread so
    cards and progress stay in the group that clicked the button.
    """
    ctx = contextvars.copy_context()

    def _wrap() -> None:
        try:
            ctx.run(fn)
        except Exception:
            log_event(f"[BG ERROR] {name}")
            traceback.print_exc()

    threading.Thread(target=_wrap, daemon=True, name=name[:40]).start()


def select_postable(events, screen, *, limit: int = 10, keep: int = 10, progress=None):
    """Research stories in rank order and keep only those a full article can be written from.

    ``limit`` is how many candidates to try; ``keep`` is how many passers to collect.
    Use limit > keep so already-seen or thin stories do not leave the batch short.
    """
    postable = []
    dropped = 0
    checked = 0
    candidates = list(events)[: max(int(limit), int(keep))]
    for event in candidates:
        checked += 1
        if progress is not None:
            progress(checked, len(candidates), event)
        passed, _reason = screen(event)
        if passed:
            postable.append(event)
            if len(postable) >= keep:
                break
        else:
            dropped += 1
    return postable, dropped, checked


def fill_until_quota(
    discovery,
    *,
    selection: str,
    keep: int,
    screen,
    unlimited_age: bool,
    on_status=None,
    on_screen=None,
    max_rounds: int | None = None,
):
    """Keep searching and screening until ``keep`` writeable cards are found.

    Round 0 uses full discovery (RSS + search). Later rounds pull fresh Bing /
    Google query sets via ``supplement_from_search`` until the quota is met
    or ``MAX_SEARCH_ROUNDS`` is exhausted.

    ``on_status(message)`` gets round-level updates.
    ``on_screen(index, event)`` gets each source-check (index is 1-based overall).
    """
    from newsagent_v2.control.offered_stories import OfferedStories
    from newsagent_v2.control.story_picker import (
        MAX_SEARCH_ROUNDS,
        order_stories,
        story_matches,
    )

    rounds = MAX_SEARCH_ROUNDS if max_rounds is None else max(1, int(max_rounds))
    site_key = os.environ.get("NEWSAGENT_ACTIVE_SITE_ID", "")
    offered = OfferedStories.load()
    kept = []
    seen_ids: set[str] = set()
    dropped = 0
    checked = 0
    queue: list = []

    for round_idx in range(rounds):
        if len(kept) >= keep:
            break
        if round_idx == 0:
            if on_status is not None:
                on_status(f"Scanning feeds + live search for {keep} cards...")
            pool = max(keep * 4, 40)
            events = discovery.run_discovery(
                unlimited_age=unlimited_age,
                selection=selection,
                offer_batch=pool,
            )
        else:
            if on_status is not None:
                on_status(
                    f"Have {len(kept)}/{keep} — search round {round_idx + 1}/{rounds}..."
                )
            events = discovery.supplement_from_search(
                selection,
                round=round_idx,
                unlimited_age=unlimited_age,
                exclude_ids=seen_ids,
            )
        fresh = order_stories(
            [
                event
                for event in events
                if getattr(event, "event_id", None)
                and event.event_id not in seen_ids
                and story_matches(event, selection)
                and not offered.already(event, site_id=site_key)
            ],
            selection,
        )
        for event in fresh:
            seen_ids.add(event.event_id)
        queue.extend(fresh)
        if not queue:
            continue

        need = keep - len(kept)

        def _progress(index: int, _total: int, event, _base=checked) -> None:
            if on_screen is not None:
                on_screen(_base + index, event)

        batch, batch_dropped, batch_checked = select_postable(
            queue,
            screen,
            limit=len(queue),
            keep=need,
            progress=_progress,
        )
        checked += batch_checked
        dropped += batch_dropped
        queue = queue[batch_checked:]
        for event in batch:
            kept.append(event)
            if len(kept) >= keep:
                break

    return kept, dropped, checked, len(seen_ids)


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


def run_idle_autopilot(runtime: V5BotRuntime, settings) -> dict[str, Any]:
    """For each ready website: pick high-trend stories, write, approve, and publish.

    Used when nobody has pressed RUN STORY or published for ``settings.idle_hours``.
    """
    from newsagent_v2.control.idle_autopilot import record_autopilot, record_publish, record_run
    from newsagent_v2.control.offered_stories import OfferedStories
    from newsagent_v2.control.site_flow import (
        best_category_for_event,
        bind_event_category,
        fetch_categories,
        remember_publication,
        store_categories,
    )
    from newsagent_v2.control.sites import is_ready, list_sites
    from newsagent_v2.control.story_picker import HIGH_TREND, skips_age_cap
    from newsagent_v2.story6 import screen_story
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
    from newsagent_v2.wordpress.authors import author_for, list_site_authors, remember_default
    from newsagent_v2.control.sites import wordpress_config

    client = runtime.client
    config = runtime.config
    discovery = runtime.discovery
    worker = runtime.generation_worker
    review = runtime.review_handler
    if client is None or config is None or discovery is None or worker is None or review is None:
        return {"ok": False, "reason": "runtime_incomplete"}
    if runtime._controlled_e2e:
        return {"ok": False, "reason": "controlled_e2e"}

    count = int(settings.article_count)
    sites = [s for s in list_sites(environ=os.environ) if is_ready(s)]
    broadcast_message(
        client,
        config,
        text=(
            f"🤖 Idle autopilot: no RUN/publish for {settings.idle_hours:g}h. "
            f"Writing and publishing up to {count} high-trend articles on {len(sites)} site(s)."
        ),
        parse_mode="HTML",
    )
    published_total = 0
    for site in sites:
        try:
            _use_site(runtime, site, index_async=False)
            transport = build_live_wordpress_transport()
            authors = list_site_authors(wordpress_config(site), transport)
            author = author_for("") or (authors[0] if authors else None)
            if author is None:
                log_event(f"[IDLE-AUTOPILOT] skip {site.name}: no authors")
                continue
            remember_default(author)
            categories = fetch_categories(site, transport)
            store_categories(site.id, categories)
            ready_count = {"n": 0}

            def _screen_and_bind(event, _site=site, _author=author, _cats=categories):
                passed, reason = screen_story(event)
                if not passed:
                    return False, reason
                category = best_category_for_event(event, _cats)
                if category is None:
                    return False, "no_matching_category"
                remember_publication(_site.id, int(category["id"]), str(category["name"]), _author)
                bind_event_category(
                    event.event_id,
                    site_id=_site.id,
                    category_id=int(category["id"]),
                    category_name=str(category["name"]),
                )
                event._high_trend_category = category  # type: ignore[attr-defined]
                ready_count["n"] += 1
                return True, "ok"

            postable, _dropped, _checked, _cands = fill_until_quota(
                discovery,
                selection=HIGH_TREND,
                keep=count,
                screen=_screen_and_bind,
                unlimited_age=skips_age_cap(HIGH_TREND),
                max_rounds=3,
            )
            OfferedStories.load().remember(postable, site_id=site.id)
            discovery.replace_ranked(postable)
            site_published = 0
            for event in postable[:count]:
                record_run(site.id)
                job = worker._new_job(event.event_id, discovery_run_id="idle-autopilot")
                result = worker.run_generation(event, job)
                if not result.get("ok"):
                    log_event(f"[IDLE-AUTOPILOT] write failed {event.event_id}: {result.get('error')}")
                    continue
                version = str(result.get("article_version") or "v1")
                pub = review.handle_approve_and_publish(
                    event.event_id,
                    reviewer="idle-autopilot",
                    job_id=str(result.get("job_id") or job.job_id),
                    version=version,
                )
                if pub.get("ok") and pub.get("action") == "publish":
                    record_publish(site.id)
                    site_published += 1
                    published_total += 1
                    log_event(f"[IDLE-AUTOPILOT] published {event.event_id} on {site.name}")
                else:
                    log_event(f"[IDLE-AUTOPILOT] publish failed {event.event_id}: {pub.get('message')}")
            broadcast_message(
                client,
                config,
                text=f"🤖 {site.name}: published {site_published}/{count} idle-autopilot articles.",
                parse_mode="HTML",
            )
        except Exception as exc:
            log_event(f"[IDLE-AUTOPILOT] site={site.name} error={type(exc).__name__}: {exc}")
            traceback.print_exc()
    record_autopilot()
    return {"ok": True, "published": published_total, "sites": len(sites)}


def tick_idle_autopilot(runtime: V5BotRuntime) -> dict[str, Any]:
    """Poll-loop hook: start idle autopilot when the desk has been quiet long enough."""
    from newsagent_v2.control.idle_autopilot import maybe_tick

    return maybe_tick(
        environ=dict(os.environ),
        run_fn=lambda settings: run_idle_autopilot(runtime, settings),
    )

def send_site_menu(client: TelegramTestClient, config, *, chat_id: str | None = None) -> None:
    """Ask which website to write for. /start sends this."""
    from newsagent_v2.control.sites import MENU_TEXT, active_id, list_sites, menu_keyboard

    sites = list_sites(environ=os.environ)
    text = MENU_TEXT
    markup = menu_keyboard(sites, active=active_id())
    from newsagent_v2.telegram import home_menu
    target = chat_id or operator_ids(config)[0]
    uid = actor_id() or (target if str(target).isdigit() else "")
    head = home_menu.header(uid, target)
    client.send_message(chat_id=target, text=(head + chr(10) * 2 if head else "") + text,
                        reply_markup=home_menu.decorate(markup, uid, target))


def _runtime_allows(runtime, chat_id, actor: str) -> bool:
    """Chats in the env allowlist keep full access exactly as before; the approval layer adds approved users and groups."""
    from newsagent_v2.telegram import access
    return runtime.config.allows(chat_id) or access.allows(chat_id, actor)


def _feature_needed(update: dict) -> str:
    """The extra switch (see access.FEATURES) the admin must have turned on for approved users/groups to do this."""
    data = str(safe_get_callback_data(update) or "")
    if data.startswith("idle:") or safe_get_message_text(update).split(" ", 1)[0].split("@", 1)[0] == "/autopilot":
        return "autopilot"
    if data.startswith(("site:add", "site:finish:", "site:settings:", "site:logo:", "site:user:", "site:password:", "site:remove:")):
        return "sites"
    if data.split(":", 1)[0] in {"approve", "publish", "unpublish"}:
        return "publish"
    return ""


def _authorized(runtime, update: dict) -> bool:
    from newsagent_v2.telegram import access
    chat_id, actor = safe_get_chat_id(update), actor_from_update(update)
    if runtime.config.allows(chat_id):
        return True            # the env allowlist keeps working exactly as before
    if not access.allows(chat_id, actor):
        return False
    feature = _feature_needed(update)
    return not feature or access.can(feature, actor, chat_id, access.chat_type_of(chat_id))


def _site_ok(update: dict) -> bool:
    """The admin picks which websites a person / group may use; every button that points at a website is checked."""
    from newsagent_v2.telegram import access
    actor, chat_id = actor_from_update(update), safe_get_chat_id(update)
    if access.allowed_sites(actor, chat_id, access.chat_type_of(chat_id)) is None:
        return True
    parts = str(safe_get_callback_data(update) or "").split(":")
    site_id = ""
    if parts[0] == "site" and len(parts) >= 2 and parts[1] != "add":
        site_id = parts[-1] if parts[1] in {"finish", "settings", "logo", "user", "password", "remove"} else parts[1]
    elif parts[0] in {"c", "a", "high", "fill"} and len(parts) > 1:
        site_id = parts[1]
    elif parts[0] == "fetch":
        from newsagent_v2.control.site_flow import pending_category
        site_id = str((pending_category(chat_id or "") or {}).get("site_id") or "")
    elif len(parts) > 1 and parts[0] in {"run", "gen", "cancel", "flw", "ign", "approve", "publish", "unpublish", "revise", "edit", "reject",
                                         "author", "set_author", "view_full", "listen"}:
        from newsagent_v2.control.site_flow import category_for_event
        site_id = str((category_for_event(parts[1]) or {}).get("site_id") or "")
    return not site_id or access.site_allowed(actor, chat_id, site_id)


def _story_owner_ok(update: dict) -> bool:
    """People manage the stories they started (a group's members, the group's); the super admin manages all."""
    from newsagent_v2.telegram import access
    parts = str(safe_get_callback_data(update) or "").split(":")
    if parts[0] in {"gen", "cancel", "approve", "publish", "unpublish", "revise", "edit", "reject", "author", "set_author", "qa_revise", "qa_discard"} and len(parts) > 1:
        return access.can_manage_story(actor_from_update(update), safe_get_chat_id(update), parts[1])
    return True


def _handle_access_update(runtime, update: dict, bot_info: dict) -> bool:
    """Onboarding traffic: group add/remove, ac: buttons, /myid and admin commands, strangers. True = fully handled."""
    from newsagent_v2.telegram import access, access_menu

    client = runtime.client
    if update.get("my_chat_member"):
        access_menu.handle_my_chat_member(client, update["my_chat_member"])
        return True
    cq = update.get("callback_query")
    if cq:
        if str(cq.get("data") or "").startswith(access_menu.PREFIX):
            access_menu.handle_callback(client, cq)
            return True
        if str(cq.get("data") or "").startswith("hm:"):
            from newsagent_v2.telegram import home_menu
            return home_menu.handle_callback(client, cq, help_for(actor_from_update(update)))
        if not _authorized(runtime, update):
            _ack_callback(client, str(cq.get("id") or ""), "🔒 Not approved, or the admin has not switched this on for you")
            return True
        blocked_here = os.environ.get("NEWSAGENT_TEST_BLOCK_PUBLISH", "").lower() in {"1", "true", "yes", "on"}
        if blocked_here and str(cq.get("data") or "").split(":", 1)[0] in {"approve", "publish"}:
            # test bot only (the live server never sets this): a copy of the live sites must not publish for real
            _ack_callback(client, str(cq.get("id") or ""), "Publishing is blocked on the test bot")
            return True
        if not _story_owner_ok(update):
            _ack_callback(client, str(cq.get("id") or ""), "This story belongs to someone else")
            return True
        if not _site_ok(update):
            _ack_callback(client, str(cq.get("id") or ""), "The admin has not given you this website")
            return True
        return False
    msg = update.get("message")
    if not msg:
        return False
    sender = msg.get("from") or {}
    uid = sender.get("id")
    if sender.get("is_bot") or msg.get("sender_chat"):
        return False
    cmd, args = access_menu.command_of(update, str(bot_info.get("username") or ""))
    if cmd == "/myid":
        access_menu.handle_command(client, cmd, args, msg)
        return True
    if access.is_admin(uid):
        if cmd in access_menu.ADMIN_COMMANDS:
            access_menu.handle_command(client, cmd, args, msg)
            return True
        return access_menu.handle_admin_input(client, msg)
    if _authorized(runtime, update):
        if cmd in access_menu.ADMIN_COMMANDS:
            client.send_message(chat_id=str(msg["chat"]["id"]), text="🔒 That command is for the admin only.")
            return True
        return False
    if access.allows(str((msg.get("chat") or {}).get("id") or ""), uid):
        # approved, but this part is not switched on for them
        client.send_message(chat_id=str(msg["chat"]["id"]), text="🔒 The admin has not switched this on for you.")
        return True
    chat_type = (msg.get("chat") or {}).get("type") or "private"
    if chat_type == "private" or cmd:
        access_menu.deny_unauthorized(client, msg)
    return True

def send_idle_settings_menu(client: TelegramTestClient, *, chat_id: str) -> None:
    """Show editable idle-autopilot hours and article count."""
    from newsagent_v2.control.idle_autopilot import load_settings, settings_keyboard, settings_menu_text

    settings = load_settings(dict(os.environ))
    client.send_message(
        chat_id=chat_id,
        text=settings_menu_text(settings),
        parse_mode="HTML",
        reply_markup=settings_keyboard(settings),
    )


def _handle_idle_settings_callback(
    *,
    client: TelegramTestClient,
    config,
    runtime,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """Telegram buttons for idle hours / article count / on-off."""
    from newsagent_v2.control.idle_autopilot import (
        IDLE_BACK_CALLBACK,
        IDLE_MENU_CALLBACK,
        load_settings,
        set_article_count,
        set_enabled,
        set_idle_hours,
        settings_keyboard,
        settings_menu_text,
    )

    chat_id = safe_get_chat_id(update) or ""
    parts = str(callback_data).split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    if callback_data == IDLE_BACK_CALLBACK or action == "back":
        _ack_callback(client, callback_query_id, "Websites")
        send_site_menu(client, config, chat_id=chat_id)
        return {"ok": True, "action": "idle_back"}

    if callback_data == IDLE_MENU_CALLBACK or action == "menu":
        _ack_callback(client, callback_query_id, "Autopilot")
        send_idle_settings_menu(client, chat_id=chat_id)
        return {"ok": True, "action": "idle_menu"}

    settings = load_settings(dict(os.environ))
    try:
        if action == "on":
            settings = set_enabled(True)
            _ack_callback(client, callback_query_id, "Autopilot ON")
        elif action == "off":
            settings = set_enabled(False)
            _ack_callback(client, callback_query_id, "Autopilot OFF")
        elif action == "hours" and len(parts) > 2:
            settings = set_idle_hours(float(parts[2]))
            _ack_callback(client, callback_query_id, f"{settings.idle_hours:g}h")
        elif action == "count" and len(parts) > 2:
            settings = set_article_count(int(parts[2]))
            _ack_callback(client, callback_query_id, f"{settings.article_count} articles")
        else:
            _ack_callback(client, callback_query_id, "Unknown")
            return {"ok": False, "error": "unknown_idle_action"}
    except (TypeError, ValueError) as exc:
        _ack_callback(client, callback_query_id, "Invalid value")
        return {"ok": False, "error": str(exc)}

    # Refresh the same card so the ✓ marks move.
    message = (update.get("callback_query") or {}).get("message") or {}
    message_id = message.get("message_id")
    if message_id and chat_id:
        try:
            client.edit_message_text(
                chat_id=str(chat_id),
                message_id=int(message_id),
                text=settings_menu_text(settings),
                parse_mode="HTML",
                reply_markup=settings_keyboard(settings),
            )
        except Exception as exc:
            log_event(f"[IDLE] menu refresh failed {type(exc).__name__}")
            send_idle_settings_menu(client, chat_id=chat_id)
    else:
        send_idle_settings_menu(client, chat_id=chat_id)
    log_event(
        f"[IDLE] settings enabled={settings.enabled} hours={settings.idle_hours:g} "
        f"count={settings.article_count}"
    )
    return {
        "ok": True,
        "action": "idle_settings",
        "settings": settings.to_dict(),
    }


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


def _use_site(runtime, site, *, index_async: bool = True) -> None:
    """Point drafts, authors, and the admin link at the chosen website.

    Sitemap indexing is optional background work so site-switch buttons stay snappy.
    """
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
    elif lifecycle is not None:
        lifecycle.config = wp_config
    if handler is not None and lifecycle is not None:
        handler.wordpress_lifecycle = lifecycle
    log_event(
        f"[SITE] active={site.name} base={site.base_url} "
        f"lifecycle={getattr(getattr(lifecycle, 'config', None), 'base_url', None)}"
    )
    if index_async:
        _run_in_background(f"sitemap-{site.id}", lambda: _index_site(site))
        return 0
    return _index_site(site)


def _refresh_site_menu_message(client, update: dict, site) -> None:
    """Move the ✓ on the /start keyboard so the switch is visible."""
    from newsagent_v2.control.sites import MENU_TEXT, list_sites, menu_keyboard

    message = (update.get("callback_query") or {}).get("message") or {}
    message_id = message.get("message_id")
    chat_id = safe_get_chat_id(update)
    if not message_id or not chat_id:
        return
    text = f"{MENU_TEXT}\n\nActive: {site.name}"
    try:
        client.edit_message_text(
            chat_id=str(chat_id),
            message_id=int(message_id),
            text=text,
            parse_mode="HTML",
            reply_markup=menu_keyboard(list_sites(environ=os.environ), active=site.id),
        )
    except Exception as exc:
        log_event(f"[SITE] menu refresh failed {type(exc).__name__}")


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


def execute_high_trend(
    client: TelegramTestClient,
    config,
    state: V5BotState,
    discovery: V5DiscoveryPipeline,
    persistent_store: PersistentV5Store,
    runtime: V5BotRuntime,
    update_id: int,
    update: dict,
    site,
    author,
) -> dict[str, any]:
    """Fetch the top high-traffic trend stories and file each under the closest category."""
    from newsagent_v2.control.offered_stories import OfferedStories
    from newsagent_v2.control.site_flow import (
        best_category_for_event,
        bind_event_category,
        fetch_categories,
        remember_publication,
        store_categories,
    )
    from newsagent_v2.control.story_picker import (
        HIGH_TREND,
        HIGH_TREND_TOTAL,
        skips_age_cap,
        traffic_seo_score,
    )
    from newsagent_v2.story6 import screen_story
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport

    start_time = time.perf_counter()
    make_run_id = state.start_make_run(update_id)
    runtime.current_discovery_run_id = make_run_id
    log_event(f"[HIGH] accepted update_id={update_id} make_run_id={make_run_id} site={site.name}")

    ack_targets = operator_ids(config) or (str(config.test_chat_id),)
    ack = MakeAcknowledgement(
        client=client,
        chat_id=ack_targets[0],
        update_id=update_id,
        make_run_id=make_run_id,
        chat_ids=ack_targets,
    )
    ack_result = ack.send_initial()
    if not ack_result.get("ok"):
        return {"ok": False, "error": "acknowledgement_failed", "make_run_id": make_run_id}

    try:
        ack.update_progress(f"Scanning high-traffic trends for {site.name}...")
        categories = fetch_categories(site, build_live_wordpress_transport())
        store_categories(site.id, categories)
        site_key = os.environ.get("NEWSAGENT_ACTIVE_SITE_ID", "") or site.id
        offered = OfferedStories.load()
        ready_count = {"n": 0}

        def _status(message: str) -> None:
            ack.update_progress(message)

        def _screen(index: int, event) -> None:
            title = str(getattr(event, "canonical_title", "") or "")[:80]
            ack.update_progress(
                f"Checking #{index} · {ready_count['n']}/{HIGH_TREND_TOTAL} cards ready: {title}"
            )

        def _screen_and_bind(event):
            passed, reason = screen_story(event)
            if not passed:
                return False, reason
            category = best_category_for_event(event, categories)
            if category is None:
                return False, "no_matching_category"
            remember_publication(site.id, int(category["id"]), str(category["name"]), author)
            bind_event_category(
                event.event_id,
                site_id=site.id,
                category_id=int(category["id"]),
                category_name=str(category["name"]),
            )
            event._high_trend_category = category  # type: ignore[attr-defined]
            ready_count["n"] += 1
            return True, "ok"

        postable, dropped, checked, candidates = fill_until_quota(
            discovery,
            selection=HIGH_TREND,
            keep=HIGH_TREND_TOTAL,
            screen=_screen_and_bind,
            unlimited_age=skips_age_cap(HIGH_TREND),
            on_status=_status,
            on_screen=_screen,
        )
        kept = []
        filed: list[str] = []
        for event in postable:
            category = getattr(event, "_high_trend_category", None)
            if category is None:
                category = best_category_for_event(event, categories)
            if category is None:
                continue
            offered.remember([event], site_id=site_key)
            kept.append(event)
            filed.append(f"{category['name']} ({traffic_seo_score(event):.1f})")

        discovery.replace_ranked(kept)
        from datetime import datetime, timezone

        discovery_run = DiscoveryRun(
            run_id=make_run_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            event_ids=[e.event_id for e in kept],
            total_events=len(kept),
            chat_id=safe_get_chat_id(update) or config.test_chat_id,
        )
        persistent_store.save_discovery_run(discovery_run)

        if kept:
            note = (
                f"HIGH TREND on {site.name} · byline {author.name}. "
                f"Showing {len(kept)} high-traffic stories"
            )
            if dropped:
                note += f" ({dropped} too thin to write)."
            else:
                note += "."
            note += f" Filed under: {', '.join(filed[:8])}"
            if len(filed) > 8:
                note += f", +{len(filed) - 8} more"
            note += "."
        else:
            note = (
                f"HIGH TREND on {site.name}: checked {checked} candidates, "
                "but none had enough sources to write right now."
            )
        ack.mark_complete(len(kept), note)

        results = discovery.send_to_telegram(
            client=client,
            config=config,
            count=len(kept),
            offset=0,
        ) if kept else []
        cards_sent = len([r for r in results if r.get("ok")])
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log_event(
            f"[HIGH] done cards={cards_sent} checked={checked} dropped={dropped} "
            f"candidates={candidates} duration_ms={duration_ms}"
        )
        return {
            "ok": True,
            "action": "high_trend",
            "make_run_id": make_run_id,
            "cards_sent": cards_sent,
            "filed": filed,
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        log_event(f"[HIGH ERROR] {type(exc).__name__}: {exc}")
        try:
            ack.mark_complete(0, f"High trend failed: {type(exc).__name__}")
        except Exception:
            pass
        return {"ok": False, "error": str(exc)[:200], "make_run_id": make_run_id}


def execute_fill_gaps(
    client: TelegramTestClient,
    config,
    state: V5BotState,
    discovery: V5DiscoveryPipeline,
    persistent_store: PersistentV5Store,
    runtime: V5BotRuntime,
    update_id: int,
    update: dict,
    site,
    author,
) -> dict[str, any]:
    """Scan the thinnest WordPress categories and offer one story for each (max 15)."""
    from newsagent_v2.control.offered_stories import OfferedStories
    from newsagent_v2.control.site_flow import (
        FILL_GAPS_TOTAL,
        bind_event_category,
        fetch_categories,
        lacking_categories,
        remember_publication,
        store_categories,
    )
    from newsagent_v2.control.story_picker import skips_age_cap
    from newsagent_v2.story6 import screen_story
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport

    start_time = time.perf_counter()
    make_run_id = state.start_make_run(update_id)
    runtime.current_discovery_run_id = make_run_id
    log_event(f"[FILL] accepted update_id={update_id} make_run_id={make_run_id} site={site.name}")

    ack_targets = operator_ids(config) or (str(config.test_chat_id),)
    ack = MakeAcknowledgement(
        client=client,
        chat_id=ack_targets[0],
        update_id=update_id,
        make_run_id=make_run_id,
        chat_ids=ack_targets,
    )
    ack_result = ack.send_initial()
    if not ack_result.get("ok"):
        return {"ok": False, "error": "acknowledgement_failed", "make_run_id": make_run_id}

    try:
        ack.update_progress(f"Scanning {site.name} categories for gaps...")
        categories = fetch_categories(site, build_live_wordpress_transport())
        store_categories(site.id, categories)
        thin = lacking_categories(categories, total=FILL_GAPS_TOTAL)
        if not thin:
            ack.mark_complete(0, f"{site.name}: no categories to fill.")
            return {"ok": False, "error": "no_categories", "make_run_id": make_run_id}

        gap_lines = ", ".join(f"{row['name']} ({row['count']})" for row in thin[:8])
        if len(thin) > 8:
            gap_lines += f", +{len(thin) - 8} more"
        ack.update_progress(f"{len(thin)} thin categories: {gap_lines}")
        log_event(f"[FILL] thin={len(thin)} categories={[row['name'] for row in thin]}")

        postable = []
        filled_names: list[str] = []
        skipped: list[str] = []
        offered = OfferedStories.load()
        site_key = os.environ.get("NEWSAGENT_ACTIVE_SITE_ID", "") or site.id

        for index, row in enumerate(thin, start=1):
            name = str(row["name"])
            category_id = int(row["id"])
            ack.update_progress(f"Gap {index}/{len(thin)}: finding a story for {name}...")
            remember_publication(site.id, category_id, name, author)
            try:
                found, dropped, checked, candidates = fill_until_quota(
                    discovery,
                    selection=name,
                    keep=1,
                    screen=screen_story,
                    unlimited_age=skips_age_cap(name),
                    on_status=lambda msg, _n=name, _i=index: ack.update_progress(
                        f"Gap {_i}/{len(thin)} {_n}: {msg}"
                    ),
                    max_rounds=4,
                )
            except Exception as exc:  # noqa: BLE001 — continue other categories
                log_event(f"[FILL] discovery failed category={name} {type(exc).__name__}")
                skipped.append(name)
                continue
            log_event(
                f"[FILL] category={name} checked={checked} postable={len(found)} "
                f"dropped={dropped} candidates={candidates}"
            )
            if not found:
                skipped.append(name)
                continue
            event = found[0]
            bind_event_category(
                event.event_id,
                site_id=site.id,
                category_id=category_id,
                category_name=name,
            )
            offered.remember([event], site_id=site_key)
            postable.append(event)
            filled_names.append(name)
            if len(postable) >= FILL_GAPS_TOTAL:
                break

        discovery.replace_ranked(postable)
        from datetime import datetime, timezone

        discovery_run = DiscoveryRun(
            run_id=make_run_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            event_ids=[e.event_id for e in postable],
            total_events=len(postable),
            chat_id=safe_get_chat_id(update) or config.test_chat_id,
        )
        persistent_store.save_discovery_run(discovery_run)

        if postable:
            note = (
                f"FILL GAPS on {site.name} · byline {author.name}. "
                f"Showing {len(postable)} {'story' if len(postable) == 1 else 'stories'} "
                f"across thin categories: {', '.join(filled_names)}."
            )
            if skipped:
                note += f" No writeable story for: {', '.join(skipped)}."
        else:
            note = (
                f"FILL GAPS on {site.name}: scanned {len(thin)} thin categories, "
                "but none had a writeable story right now."
            )
        ack.mark_complete(len(postable), note)

        results = discovery.send_to_telegram(
            client=client,
            config=config,
            count=len(postable),
            offset=0,
        ) if postable else []
        cards_sent = len([r for r in results if r.get("ok")])
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log_event(
            f"[FILL] done cards={cards_sent} filled={len(filled_names)} "
            f"skipped={len(skipped)} duration_ms={duration_ms}"
        )
        return {
            "ok": True,
            "action": "fill_gaps",
            "make_run_id": make_run_id,
            "cards_sent": cards_sent,
            "filled": filled_names,
            "skipped": skipped,
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        log_event(f"[FILL ERROR] {type(exc).__name__}: {exc}")
        try:
            ack.mark_complete(0, f"Fill gaps failed: {type(exc).__name__}")
        except Exception:
            pass
        return {"ok": False, "error": str(exc)[:200], "make_run_id": make_run_id}


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


def _reply_voice_menu(runtime: V5BotRuntime, chat_id: str) -> None:
    """Open the /voice picker: language step first (English first, then A-Z)."""
    from newsagent_v2.telegram.voice_picker import LANG_STEP_TEXT, PICKER_TITLE, language_keyboard
    from newsagent_v2.tts.polly import PollyError, list_voices

    if runtime.client is None:
        return
    try:
        voices = list_voices()
    except PollyError as exc:
        runtime.client.send_message(chat_id=chat_id, text=f"Polly is not available right now: {exc}")
        return
    runtime.client.send_message(
        chat_id=chat_id,
        text=f"{PICKER_TITLE}\n\n{LANG_STEP_TEXT}",
        reply_markup=language_keyboard(voices),
    )


def _handle_voice_callback(
    *,
    client: TelegramTestClient,
    config,
    runtime: V5BotRuntime,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """Voice picker steps: language page, voice page, save the choice, preview."""
    from newsagent_v2.telegram.voice_picker import (
        PICKER_TITLE,
        PREVIEW_TEXT,
        language_keyboard,
        parse_voice_callback,
        safe_page,
        set_voice,
        voice_for,
        voice_keyboard,
    )
    from newsagent_v2.tts.polly import PollyError, find_voice, list_voices, synthesize

    chat_id = safe_get_chat_id(update) or ""
    parsed = parse_voice_callback(callback_data)
    if parsed is None:
        return {"ok": False, "error": "unknown_voice_callback"}
    try:
        voices = list_voices()
    except PollyError as exc:
        log_event(f"[VOICE] polly unavailable: {exc}")
        try:
            client.answer_callback_query(
                callback_query_id=callback_query_id,
                text="Polly is not available right now.",
            )
        except Exception:
            pass
        return {"ok": False, "error": "polly_unavailable"}

    message = (update.get("callback_query") or {}).get("message") or {}
    message_id = message.get("message_id")
    action = parsed["action"]

    if action == "langs":
        markup = language_keyboard(voices, page=safe_page(parsed.get("page", "0")))
        if message_id:
            client.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=f"{PICKER_TITLE}\n\nPick a language.",
                reply_markup=markup,
            )
        else:
            client.send_message(chat_id=chat_id, text="Pick a language.", reply_markup=markup)
        try:
            client.answer_callback_query(callback_query_id=callback_query_id)
        except Exception:
            pass
        return {"ok": True, "action": "voice_langs"}

    if action == "lang":
        language_code = parsed.get("language_code", "")
        markup = voice_keyboard(
            voices,
            language_code,
            page=safe_page(parsed.get("page", "0")),
            selected_id=voice_for(chat_id),
        )
        if message_id:
            client.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text="Pick a voice. ▶️ plays a one-sentence sample; the name row selects it.",
                reply_markup=markup,
            )
        else:
            client.send_message(chat_id=chat_id, text="Pick a voice.", reply_markup=markup)
        try:
            client.answer_callback_query(callback_query_id=callback_query_id)
        except Exception:
            pass
        return {"ok": True, "action": "voice_lang", "language_code": language_code}

    voice = find_voice(voices, parsed.get("voice_id", ""))
    if voice is None:
        try:
            client.answer_callback_query(
                callback_query_id=callback_query_id,
                text="That voice is no longer available.",
            )
        except Exception:
            pass
        return {"ok": False, "error": "unknown_voice"}

    if action == "set":
        set_voice(chat_id, voice.id)
        log_event(f"[VOICE] chat={chat_id} voice={voice.id}")
        try:
            client.answer_callback_query(
                callback_query_id=callback_query_id,
                text=f"Voice for this chat: {voice.id} · {voice.best_engine}",
            )
        except Exception:
            pass
        if message_id:
            client.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=(
                    f"✅ This chat now reads articles in <b>{html.escape(voice.id)}</b> "
                    f"({html.escape(voice.language_name)}, {voice.best_engine}).\n"
                    "Send /voice to change it."
                ),
                parse_mode="HTML",
            )
        return {"ok": True, "action": "voice_set", "voice_id": voice.id}

    # action == "play": one-sentence sample, never saved.
    try:
        client.answer_callback_query(
            callback_query_id=callback_query_id,
            text=f"Playing a {voice.id} sample…",
        )
    except Exception:
        pass
    try:
        audio = synthesize(PREVIEW_TEXT, voice.id, engine=voice.best_engine)
    except PollyError as exc:
        log_event(f"[VOICE] preview failed: {exc}")
        client.send_message(chat_id=chat_id, text=f"Preview failed: {exc}")
        return {"ok": False, "error": "preview_failed"}
    sent = client.send_audio(
        chat_id=chat_id,
        audio_name=f"{voice.id.lower()}-preview.mp3",
        audio_bytes=audio,
        caption=f"▶️ {html.escape(voice.label())}",
        title=f"{voice.id} sample",
    )
    return {"ok": bool(sent.get("ok")), "action": "voice_preview", "voice_id": voice.id}


def _handle_listen_callback(
    *,
    client: TelegramTestClient,
    config,
    runtime: V5BotRuntime,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """🔊 LISTEN on a review card: read the article aloud in this chat's voice."""
    from newsagent_v2.telegram.voice_picker import voice_for
    from newsagent_v2.tts.polly import PollyError, clean_for_speech, synthesize

    chat_id = safe_get_chat_id(update) or str(config.test_chat_id)
    parts = str(callback_data).split(":")
    event_id = parts[1] if len(parts) > 1 else ""
    version = parts[2] if len(parts) > 2 else ""

    store = runtime.version_store
    if store is None or not event_id:
        try:
            client.answer_callback_query(callback_query_id=callback_query_id, text="Article not found.")
        except Exception:
            pass
        return {"ok": False, "error": "no_version_store"}
    if not version:
        version = store.get_current_version(event_id, "article") or ""
    record = store.get_article(event_id, version) if version else None
    article = (record or {}).get("article") or {}
    headline = str(article.get("headline") or "").strip()
    body = clean_for_speech(str(article.get("article_body") or ""))
    if not body:
        try:
            client.answer_callback_query(callback_query_id=callback_query_id, text="No article text to read.")
        except Exception:
            pass
        return {"ok": False, "error": "no_article_text"}

    voice = voice_for(chat_id)
    try:
        client.answer_callback_query(
            callback_query_id=callback_query_id,
            text=f"Preparing audio ({voice})…",
        )
    except Exception:
        pass

    narration = f"{headline}. {body}" if headline else body
    try:
        audio = synthesize(narration, voice)
    except PollyError as exc:
        log_event(f"[LISTEN] synthesize failed event={event_id}: {exc}")
        client.send_message(chat_id=chat_id, text=f"Audio is not available right now: {exc}")
        return {"ok": False, "error": "polly_failed"}

    caption = f"🔊 {html.escape(headline[:120])}\nVoice: {html.escape(voice)}"
    sent = client.send_audio(
        chat_id=chat_id,
        audio_name=f"{event_id}-{version or 'current'}.mp3",
        audio_bytes=audio,
        caption=caption[:1024],
        title=(headline or "News Agent article")[:128],
    )
    log_event(f"[LISTEN] event={event_id} version={version} voice={voice} ok={bool(sent.get('ok'))}")
    return {"ok": bool(sent.get("ok")), "action": "listen", "event_id": event_id, "voice": voice}


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

def _finish_review_result(
    client: TelegramTestClient,
    config,
    update: dict,
    actor_chat: str,
    result: dict[str, any],
) -> None:
    """Send follow-up Telegram UI after a review action finishes."""
    action = result.get("action", "unknown")
    message_text = result.get("message", "Done")
    if result.get("edit_card"):
        edited = _edit_author_on_card(client, update, result)
        if not edited:
            broadcast_message(client, config, text=message_text, parse_mode="HTML")
    elif result.get("reply_markup"):
        client.send_message(
            chat_id=actor_chat,
            text=message_text,
            parse_mode="HTML",
            reply_markup=result.get("reply_markup"),
        )
    elif action in [
        "rate_article", "rate_image", "feedback_captured",
        "approve", "revise", "publish", "feedback_cancelled",
        "set_author", "site_author",
    ] or (not result.get("ok") and result.get("message")):
        broadcast_message(client, config, text=message_text, parse_mode="HTML")


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
    """Handle review callbacks: rate, feedback, revise, approve, publish.

    Heavy actions (approve/publish/revise) are acknowledged immediately and run
    in a background thread so other Telegram buttons keep responding.
    """
    start_time = time.perf_counter()
    log_event(f"[REVIEW CALLBACK] data={callback_data}")

    try:
        cq = update.get("callback_query") or {}
        from_user = cq.get("from", {})
        reviewer = from_user.get("username") or from_user.get("first_name", "user")
        actor_chat = safe_get_chat_id(update) or str(config.test_chat_id)
        parsed = runtime.review_handler.parse_callback(callback_data) if runtime.review_handler else None
        action_hint = (parsed or {}).get("action", "")

        if action_hint in _HEAVY_REVIEW_ACTIONS:
            _ack_callback(client, callback_query_id, "Working…")
            log_event("[REVIEW CALLBACK] acknowledged (async)")

            def _heavy() -> None:
                t0 = time.perf_counter()
                try:
                    result = runtime.review_handler.handle(
                        data=callback_data,
                        reviewer=reviewer,
                        chat_id=actor_chat,
                    )
                    log_event(f"[REVIEW CALLBACK] action={result.get('action', 'unknown')}")
                    _finish_review_result(client, config, update, actor_chat, result)
                except Exception as exc:
                    log_event(f"[REVIEW CALLBACK ERROR] {type(exc).__name__}: {exc}")
                    traceback.print_exc()
                    broadcast_message(
                        client, config,
                        text=f"❌ Review action failed: {html.escape(str(exc)[:200])}",
                        parse_mode="HTML",
                    )
                finally:
                    log_event(
                        f"[REVIEW CALLBACK] completed duration_ms="
                        f"{int((time.perf_counter() - t0) * 1000)}"
                    )

            _run_in_background(f"review-{action_hint}", _heavy)
            return {"ok": True, "action": action_hint, "async": True}

        result = runtime.review_handler.handle(
            data=callback_data,
            reviewer=reviewer,
            chat_id=actor_chat,
        )
        action = result.get("action", "unknown")
        log_event(f"[REVIEW CALLBACK] action={action}")
        _ack_callback(client, callback_query_id, result.get("message", "Done"))
        log_event("[REVIEW CALLBACK] acknowledged")
        _finish_review_result(client, config, update, actor_chat, result)
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        log_event(f"[REVIEW CALLBACK] completed duration_ms={duration_ms}")
        return result

    except Exception as e:
        log_event(f"[REVIEW CALLBACK ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        _ack_callback(client, callback_query_id, f"Error: {str(e)[:100]}")
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
    if callback_data == ADD_CALLBACK:
        try:
            client.answer_callback_query(callback_query_id=callback_query_id, text="Add website")
        except Exception as exc:
            log_event(f"[CALLBACK ERROR] Failed to answer site: {exc}")
        prompt = begin_add(chat_id)
        client.send_message(chat_id=chat_id, text=prompt)
        log_event("[SITE] add started")
        return {"ok": True, "action": "add_site"}
    if str(callback_data).startswith(FINISH_PREFIX):
        site_id = str(callback_data)[len(FINISH_PREFIX):]
        site = find_site(site_id, environ=os.environ)
        if site is None:
            try:
                client.answer_callback_query(callback_query_id=callback_query_id, text="Unknown site")
            except Exception:
                pass
            client.send_message(chat_id=chat_id, text="That website is no longer saved. Send /start to see the list.")
            return {"ok": False, "error": "unknown_site"}
        if is_ready(site):
            try:
                client.answer_callback_query(callback_query_id=callback_query_id, text=f"Switched to {site.name}"[:200])
            except Exception as exc:
                log_event(f"[CALLBACK ERROR] Failed to answer site: {exc}")
            _use_site(runtime, site)
            _refresh_site_menu_message(client, update, site)
            _send_site_categories(client, chat_id, site)
            return {"ok": True, "action": "select_site", "site": site.name}
        try:
            client.answer_callback_query(callback_query_id=callback_query_id, text="Finish setup")
        except Exception:
            pass
        prompt = begin_logo(chat_id, site)
        client.send_message(chat_id=chat_id, text=prompt)
        log_event(f"[SITE] logo requested site={site.name}")
        return {"ok": True, "action": "finish_setup"}
    managed = _manage_site_callback(client, runtime, chat_id, str(callback_data))
    if managed is not None:
        try:
            client.answer_callback_query(callback_query_id=callback_query_id, text="Okay")
        except Exception:
            pass
        return managed
    site_id = callback_data.split(":", 1)[1]
    site = find_site(site_id, environ=os.environ)
    if site is None:
        try:
            client.answer_callback_query(callback_query_id=callback_query_id, text="Unknown site")
        except Exception:
            pass
        client.send_message(chat_id=chat_id, text="That website is no longer saved. Send /start to see the list.")
        return {"ok": False, "error": "unknown_site"}
    try:
        client.answer_callback_query(callback_query_id=callback_query_id, text=f"Switched to {site.name}"[:200])
    except Exception as exc:
        log_event(f"[CALLBACK ERROR] Failed to answer site: {exc}")
    _use_site(runtime, site)
    _refresh_site_menu_message(client, update, site)
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


def _handle_high_trend_callback(
    *,
    client: TelegramTestClient,
    runtime,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """Top high-traffic trend stories. Author pick is the only step before the batch."""
    from newsagent_v2.control.site_flow import (
        author_keyboard,
        high_trend_author_menu_text,
        remember_high_trend,
    )
    from newsagent_v2.control.sites import find_site, wordpress_config
    from newsagent_v2.control.story_picker import HIGH_TREND_TOTAL
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
    from newsagent_v2.wordpress.authors import list_site_authors

    chat_id = safe_get_chat_id(update) or ""
    site_id = str(callback_data).split(":", 1)[1]
    site = find_site(site_id, environ=os.environ)
    try:
        client.answer_callback_query(callback_query_id=callback_query_id, text="High trend")
    except Exception as exc:
        log_event(f"[CALLBACK ERROR] Failed to answer high trend: {exc}")
    if site is None:
        client.send_message(chat_id=chat_id, text="That website is no longer saved. Send /start.")
        return {"ok": False, "error": "unknown_site"}
    _use_site(runtime, site)
    remember_high_trend(chat_id, site.id)
    authors = list_site_authors(wordpress_config(site), build_live_wordpress_transport())
    log_event(f"[SITE] high_trend authors site={site.name} count={len(authors)}")
    if not authors:
        client.send_message(chat_id=chat_id, text=f"{site.name} did not return any authors.")
        return {"ok": False, "error": "no_authors"}
    client.send_message(
        chat_id=chat_id,
        text=high_trend_author_menu_text(site.name, len(authors)),
        reply_markup=author_keyboard(site.id, authors),
    )
    return {"ok": True, "action": "high_trend_author", "site": site.name, "total": HIGH_TREND_TOTAL}


def _handle_fill_gaps_callback(
    *,
    client: TelegramTestClient,
    runtime,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """Scan thin categories next. Author pick is the only step before the batch."""
    from newsagent_v2.control.site_flow import (
        FILL_GAPS_TOTAL,
        author_keyboard,
        fill_gaps_author_menu_text,
        remember_fill_gaps,
    )
    from newsagent_v2.control.sites import find_site, wordpress_config
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
    from newsagent_v2.wordpress.authors import list_site_authors

    chat_id = safe_get_chat_id(update) or ""
    site_id = str(callback_data).split(":", 1)[1]
    site = find_site(site_id, environ=os.environ)
    try:
        client.answer_callback_query(callback_query_id=callback_query_id, text="Fill gaps")
    except Exception as exc:
        log_event(f"[CALLBACK ERROR] Failed to answer fill gaps: {exc}")
    if site is None:
        client.send_message(chat_id=chat_id, text="That website is no longer saved. Send /start.")
        return {"ok": False, "error": "unknown_site"}
    _use_site(runtime, site)
    remember_fill_gaps(chat_id, site.id)
    authors = list_site_authors(wordpress_config(site), build_live_wordpress_transport())
    log_event(f"[SITE] fill_gaps authors site={site.name} count={len(authors)}")
    if not authors:
        client.send_message(chat_id=chat_id, text=f"{site.name} did not return any authors.")
        return {"ok": False, "error": "no_authors"}
    client.send_message(
        chat_id=chat_id,
        text=fill_gaps_author_menu_text(site.name, len(authors)),
        reply_markup=author_keyboard(site.id, authors),
    )
    return {"ok": True, "action": "fill_gaps_author", "site": site.name, "total": FILL_GAPS_TOTAL}


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


def _qa_repair_feedback(version_store, event_id: str, article_version: str) -> str:
    """Turn saved QA blocking issues into a short repair brief for the writer."""
    record = version_store.get_article(event_id, article_version) or {}
    qa = record.get("qa_result") or {}
    lines: list[str] = []
    for row in qa.get("issues") or []:
        if not isinstance(row, dict) or str(row.get("severity") or "") != "block":
            continue
        loc = str(row.get("location") or "?")
        msg = str(row.get("message") or "").strip()
        if msg:
            lines.append(f"[{loc}] {msg}")
        if len(lines) >= 8:
            break
    if not lines:
        return (
            "QA blocked this draft. Fix only inventing figures, names, or quotes. "
            "Remove unsourced numbers; do not rewrite the whole article."
        )
    return (
        "QA repair: fix only these blocking problems in the named locations. "
        "Prefer deleting or rephrasing the bad figure/name/quote. Do not invent new facts.\n- "
        + "\n- ".join(lines)
    )


def _handle_qa_revise_offer_callback(
    *,
    client: TelegramTestClient,
    runtime,
    update: dict,
    callback_query_id: str,
    callback_data: str,
) -> dict[str, any]:
    """YES revises the QA-blocked draft with a fresh repair budget; NO discards."""
    from newsagent_v2.v5_generation.run_story_adapter import Revision

    chat_id = safe_get_chat_id(update) or ""
    parts = str(callback_data).split(":")
    action = parts[0] if parts else ""
    event_id = parts[1] if len(parts) > 1 else ""
    article_version = parts[2] if len(parts) > 2 else "v1"
    if not event_id:
        _ack_callback(client, callback_query_id, "Unknown story")
        return {"ok": False, "error": "no_event"}

    if action == "qa_discard":
        _ack_callback(client, callback_query_id, "Discarded")
        client.send_message(
            chat_id=chat_id,
            text=f"OK — left {event_id} without a review card. Run the story again later if you want a new draft.",
        )
        return {"ok": True, "action": "qa_discard", "event_id": event_id}

    worker = getattr(runtime, "generation_worker", None)
    store = getattr(runtime, "version_store", None) or (
        worker._version_store() if worker is not None else None
    )
    if worker is None or store is None:
        _ack_callback(client, callback_query_id, "Unavailable")
        client.send_message(chat_id=chat_id, text="Revising is unavailable right now.")
        return {"ok": False, "error": "no_worker"}

    record = store.get_article(event_id, article_version) or {}
    if not record.get("article"):
        _ack_callback(client, callback_query_id, "No draft")
        client.send_message(chat_id=chat_id, text="No saved draft to revise. Tap RUN STORY again.")
        return {"ok": False, "error": "no_draft"}

    feedback = _qa_repair_feedback(store, event_id, article_version)
    try:
        repair = int(str(os.environ.get("NEWSAGENT_QA_REPAIR_CALLS") or "2").strip() or "2")
    except ValueError:
        repair = 2
    _ack_callback(client, callback_query_id, "Revising…")
    started = worker.start_revision(
        event_id,
        Revision(
            article_feedback=feedback,
            image_feedback="",
            article_version=article_version,
            image_version=store.get_current_version(event_id, "image"),
            qa_repair=True,
        ),
        headline=str((record.get("article") or {}).get("headline") or ""),
    )
    if not started.get("ok"):
        client.send_message(
            chat_id=chat_id,
            text=started.get("message") or "Could not start the QA repair.",
        )
        return {"ok": False, "error": started.get("reason") or "start_failed"}
    client.send_message(
        chat_id=chat_id,
        text=(
            f"✍️ Revising the blocked lines on {event_id} "
            f"(up to {max(1, min(repair, 6))} writer calls). "
            "A review card follows if QA clears; otherwise you will be asked again."
        ),
    )
    return {"ok": True, "action": "qa_revise", "event_id": event_id, "job_id": started.get("job_id")}


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
    from newsagent_v2.control.site_flow import (
        pending_category,
        pending_is_fill_gaps,
        pending_is_high_trend,
        remember_publication,
    )
    from newsagent_v2.control.sites import find_site, wordpress_config
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport
    from newsagent_v2.wordpress.authors import find_author, list_site_authors, remember_default

    chat_id = safe_get_chat_id(update) or ""
    _prefix, site_id, user_text = (callback_data.split(":") + ["", ""])[:3]
    site = find_site(site_id, environ=os.environ)
    chosen = pending_category(chat_id)
    if site is None or chosen is None or chosen.get("site_id") != site_id:
        _ack_callback(client, callback_query_id, "Pick a category first")
        client.send_message(chat_id=chat_id, text="Pick the website and a category first. Send /start.")
        return {"ok": False, "error": "no_category"}
    # Ack before WordPress author lookup so the spinner clears even if WP is slow.
    _ack_callback(client, callback_query_id, "Okay")
    authors = list_site_authors(wordpress_config(site), build_live_wordpress_transport())
    author = find_author(authors, user_text)
    if author is None:
        client.send_message(chat_id=chat_id, text="That author is no longer on the site. Pick again.")
        return {"ok": False, "error": "unknown_author"}
    _use_site(runtime, site)
    if pending_is_high_trend(chat_id):
        remember_default(author)
        log_event(f"[SITE] high_trend site={site.name} author={author.name}")
        _run_in_background(
            f"high-{site.id}",
            lambda: execute_high_trend(
                client=client,
                config=config,
                state=state,
                discovery=discovery,
                persistent_store=runtime.persistent_store,
                runtime=runtime,
                update_id=update_id,
                update=update,
                site=site,
                author=author,
            ),
        )
        return {"ok": True, "action": "high_trend", "async": True, "site": site.name}
    if pending_is_fill_gaps(chat_id):
        remember_default(author)
        log_event(f"[SITE] fill_gaps site={site.name} author={author.name}")
        _run_in_background(
            f"fill-{site.id}",
            lambda: execute_fill_gaps(
                client=client,
                config=config,
                state=state,
                discovery=discovery,
                persistent_store=runtime.persistent_store,
                runtime=runtime,
                update_id=update_id,
                update=update,
                site=site,
                author=author,
            ),
        )
        return {"ok": True, "action": "fill_gaps", "async": True, "site": site.name}
    remember_publication(site.id, int(chosen["category_id"]), str(chosen["category_name"]), author)
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
    _ack_callback(client, callback_query_id, f"Fetching {count}")
    log_event(f"[SITE] fetch count={count} category={chosen.get('category_name')}")
    selection = str(chosen["category_name"])
    _run_in_background(
        f"make-{selection[:20]}",
        lambda: execute_make_with_acknowledgement(
            client=client,
            config=config,
            state=state,
            discovery=discovery,
            persistent_store=runtime.persistent_store,
            runtime=runtime,
            update_id=update_id,
            update=update,
            selection=selection,
            offer_batch=count,
        ),
    )
    return {"ok": True, "action": "make", "async": True, "count": count}


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

    if str(callback_data).split(":", 1)[0] in {"run", "gen"}:
        from newsagent_v2.telegram import access
        quota_ok, quota_why = access.check_quota(actor_id())
        if not quota_ok:
            _ack_callback(client, callback_query_id, "Daily limit reached")
            client.send_message(chat_id=safe_get_chat_id(update), text=html.escape(f"⏱ {quota_why}"))
            return {"ok": False, "reason": "daily_limit"}

    if str(callback_data).startswith("idle:"):
        return _handle_idle_settings_callback(
            client=client,
            config=config,
            runtime=runtime,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
        )

    if str(callback_data).startswith("c:"):
        return _handle_category_callback(
            client=client,
            runtime=runtime,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
        )

    if str(callback_data).startswith("fill:"):
        return _handle_fill_gaps_callback(
            client=client,
            runtime=runtime,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
        )

    if str(callback_data).startswith("high:"):
        return _handle_high_trend_callback(
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

    if str(callback_data).startswith("qa_revise:") or str(callback_data).startswith("qa_discard:"):
        return _handle_qa_revise_offer_callback(
            client=client,
            runtime=runtime,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
        )

    if str(callback_data).startswith("pick:"):
        choice = str(callback_data).split(":", 1)[1]
        from newsagent_v2.control.story_picker import CHOICES, HIGH_TREND, HIGH_TREND_TOTAL, choice_label

        known = {key for key, _label in CHOICES}
        _ack_callback(client, callback_query_id, f"Scanning {choice_label(choice)}")
        if choice not in known:
            return {"ok": False, "error": "unknown_pick"}
        offer = HIGH_TREND_TOTAL if choice == HIGH_TREND else 10
        _run_in_background(
            f"pick-{choice[:20]}",
            lambda: execute_make_with_acknowledgement(
                client=client,
                config=config,
                state=state,
                discovery=discovery,
                persistent_store=runtime.persistent_store,
                runtime=runtime,
                update_id=update_id,
                update=update,
                selection=choice,
                offer_batch=offer,
            ),
        )
        return {"ok": True, "action": "pick", "async": True, "selection": choice}
    
    if str(callback_data).startswith("voice:"):
        return _handle_voice_callback(
            client=client,
            config=config,
            runtime=runtime,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
        )

    if str(callback_data).startswith("listen:"):
        return _handle_listen_callback(
            client=client,
            config=config,
            runtime=runtime,
            update=update,
            callback_query_id=callback_query_id,
            callback_data=str(callback_data),
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
    from newsagent_v2.telegram import access as _access
    _access.configure(V5_STATE_DIR, [x.strip() for x in os.environ.get("NEWSAGENT_TELEGRAM_ADMIN_IDS", "").replace(";", ",").split(",") if x.strip()])
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
    from newsagent_v2.telegram.access_menu import register_commands
    register_commands(runtime.client)
    
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
            body: dict[str, any] = {"timeout": 10, "limit": 10,
                                    "allowed_updates": ["message", "callback_query", "my_chat_member"]}
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
                try:
                    idle = tick_idle_autopilot(runtime)
                    if idle.get("started"):
                        log_event(f"[IDLE-AUTOPILOT] started settings={idle.get('settings')}")
                except Exception as exc:
                    log_event(f"[IDLE-AUTOPILOT] tick failed: {exc}")
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
                    set_actor_id(actor_from_update(update))
                    if _handle_access_update(runtime, update, bot_info):
                        runtime.state.mark_update_processed(update_id)
                        continue
                    if not _authorized(runtime, update):
                        runtime.state.mark_update_processed(update_id)
                        continue
                    if is_callback_update(update):
                        # HANDLE CALLBACK
                        chat_id = safe_get_chat_id(update)
                        if not _runtime_allows(runtime, chat_id, actor_id()):
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
                        if not _runtime_allows(runtime, chat_id, actor_id()):
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
                            # Log the command only; ordinary messages may contain pasted credentials.
                            log_event(f"[MESSAGE] cmd={preview_cmd.strip('!.?,:;…')!r}" if preview_cmd.startswith('/') else "[MESSAGE] received (text withheld)")

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
                        elif cmd_key == "/autopilot":
                            send_idle_settings_menu(runtime.client, chat_id=str(chat_id or ""))
                            log_event("[MESSAGE] idle_settings_menu")
                        elif cmd_key == "/author":
                            _reply_author_menu(runtime, chat_id)
                            log_event("[MESSAGE] author_menu")
                        elif cmd_key == "/voice":
                            _reply_voice_menu(runtime, chat_id)
                            log_event("[MESSAGE] voice_menu")
                        elif cmd_key == "/help":
                            _reply(help_for(actor_id()))
                            log_event("[MESSAGE] help_reply")
                        elif is_group_chat(update, str(chat_id or "")) and not mentions_bot(update, bot_username):
                            log_event("[MESSAGE] group ignored (bot not tagged)")
                        else:
                            question = raw_text
                            if is_group_chat(update, str(chat_id or "")):
                                question = strip_bot_mention(raw_text, bot_username)
                            from newsagent_v2.telegram.access_menu import looks_like_secret
                            if looks_like_secret(question):
                                # a pasted token / key: do not send it to the chat model, take it out of the chat, never log it
                                if not is_group_chat(update, str(chat_id or "")):
                                    try:
                                        runtime.client._post("deleteMessage", json_body={
                                            "chat_id": chat_id, "message_id": int((update.get("message") or {}).get("message_id") or 0)})
                                    except Exception:
                                        pass
                                _reply("That is not a command, so I ignored it and did not keep it. If it was a token or a key, treat it as leaked and create a new one.")
                                log_event("[MESSAGE] secret-shaped text dropped")
                            elif not question:
                                _reply(help_for(actor_id()))
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
