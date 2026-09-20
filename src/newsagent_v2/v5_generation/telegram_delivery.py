"""V5 Telegram Delivery - sends frozen artifacts to Telegram.

Handles:
- Initial generation completion (article + image + controls)
- Revision completion (article-only, image-only, or both)
- Artifact splitting for Telegram limits
- Version-aware controls

ZERO generation calls - only loads frozen artifacts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.full_article_delivery import build_full_article_parts
from newsagent_v2.telegram.contract import TELEGRAM_MESSAGE_LIMIT

from .version_store import VersionStore


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return (text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _build_article_message(
    headline: str,
    dek: str | None,
    body: str,
    version: str,
) -> str:
    """Build article message."""
    header = f"📰 ARTICLE {version}\n\n{_escape_html(headline)}"
    if dek:
        header += f"\n\n{_escape_html(dek)}"
    if body:
        header += f"\n\n{_escape_html(body)}"
    return header


def _send_article_text(
    client: TelegramTestClient,
    chat_id: str,
    headline: str,
    dek: str | None,
    body: str,
    version: str,
) -> list[dict[str, Any]]:
    """Send article text, splitting if needed.
    
    Returns list of send results.
    """
    full_text = _build_article_message(headline, dek, body, version)
    results = []
    
    if len(full_text) <= TELEGRAM_MESSAGE_LIMIT:
        # Single message
        result = client.send_message(
            chat_id=chat_id,
            text=full_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        results.append(result)
    else:
        # Split by paragraphs
        parts = build_full_article_parts(
            rank=1,
            total=1,
            headline=headline,
            body=body,
            article_type=None,
            words=None,
        )
        
        # Send header + first part
        first_header = f"📰 ARTICLE {version}\n\n{_escape_html(headline)}"
        if dek:
            first_header += f"\n\n{_escape_html(dek)}"
        
        first_full = first_header + "\n\n" + _escape_html(parts[0])
        if len(first_full) > TELEGRAM_MESSAGE_LIMIT:
            first_full = first_full[:TELEGRAM_MESSAGE_LIMIT - 100] + "..."
        
        result = client.send_message(
            chat_id=chat_id,
            text=first_full,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        results.append(result)
        
        # Send continuation parts
        for i, part in enumerate(parts[1:], start=2):
            cont_header = f"📰 ARTICLE {version} (continued {i}/{len(parts)})\n\n"
            cont_text = cont_header + _escape_html(part)
            if len(cont_text) > TELEGRAM_MESSAGE_LIMIT:
                cont_text = cont_text[:TELEGRAM_MESSAGE_LIMIT - 100] + "..."
            
            result = client.send_message(
                chat_id=chat_id,
                text=cont_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            results.append(result)
    
    return results


def _send_image(
    client: TelegramTestClient,
    chat_id: str,
    image_path: Path,
    headline: str,
    version: str,
) -> dict[str, Any]:
    """Send image with caption."""
    caption = f"🖼️ IMAGE {version}\n{_escape_html(headline[:100])}"
    
    return client.send_photo(
        chat_id=chat_id,
        photo_name=image_path.name,
        photo_bytes=image_path.read_bytes(),
        caption=caption,
        parse_mode="HTML",
    )


def _build_review_keyboard(
    event_id: str,
    article_version: str,
    image_version: str,
) -> dict[str, Any]:
    """Build review keyboard with version-aware callbacks."""
    return {
        "inline_keyboard": [
            [
                {"text": "RATE ARTICLE", "callback_data": f"rate_article:{event_id}:{article_version}"},
                {"text": "RATE IMAGE", "callback_data": f"rate_image:{event_id}:{image_version}"},
            ],
            [
                {"text": "ARTICLE FEEDBACK", "callback_data": f"feedback_article:{event_id}:{article_version}"},
                {"text": "IMAGE FEEDBACK", "callback_data": f"feedback_image:{event_id}:{image_version}"},
            ],
            [
                {"text": "REVISE", "callback_data": f"revise:{event_id}:{article_version}:{image_version}"},
                {"text": "PUBLISH", "callback_data": f"publish:{event_id}:{article_version}:{image_version}"},
            ],
            [
                {"text": "REJECT", "callback_data": f"reject:{event_id}:{article_version}"},
            ],
        ]
    }


def send_initial_review_package(
    client: TelegramTestClient,
    config: TelegramConfig,
    version_store: VersionStore,
    event_id: str,
    canonical_title: str,
    article_version: str,
    image_version: str,
) -> dict[str, Any]:
    """Send initial review package after first generation.
    
    Flow:
    1. Send article (split if needed)
    2. Send image
    3. Send review controls
    
    Returns:
        Delivery result with all message IDs.
    """
    results = {
        "ok": True,
        "event_id": event_id,
        "article_version": article_version,
        "image_version": image_version,
        "article_sent": False,
        "image_sent": False,
        "controls_sent": False,
        "message_ids": [],
        "kimi_calls": 0,
        "vertex_calls": 0,
    }
    
    # Load article
    article_record = version_store.get_article(event_id, article_version)
    if not article_record:
        return {
            **results,
            "ok": False,
            "error": f"Article {article_version} not found",
        }
    
    article_data = article_record.get("article", {})
    headline = article_data.get("headline", canonical_title)
    dek = article_data.get("dek")
    body = article_data.get("article_body", "")
    
    # Send article
    article_results = _send_article_text(
        client=client,
        chat_id=config.test_chat_id,
        headline=headline,
        dek=dek,
        body=body,
        version=article_version,
    )
    
    for r in article_results:
        if r.get("ok") and r.get("message_id"):
            results["message_ids"].append(r["message_id"])
    
    results["article_sent"] = all(r.get("ok") for r in article_results)
    if not results["article_sent"]:
        results["ok"] = False
        results["error"] = "Article send failed"
        return results
    
    # Send image
    image_path = version_store.get_image_path(event_id, image_version)
    if image_path and image_path.is_file():
        image_result = _send_image(
            client=client,
            chat_id=config.test_chat_id,
            image_path=image_path,
            headline=headline,
            version=image_version,
        )
        
        if image_result.get("ok") and image_result.get("message_id"):
            results["message_ids"].append(image_result["message_id"])
            results["image_sent"] = True
    else:
        # Image not found - still send controls but mark as failed
        results["image_sent"] = False
        results["image_error"] = "Image not found"
    
    # Send review controls
    keyboard = _build_review_keyboard(event_id, article_version, image_version)
    controls_result = client.send_message(
        chat_id=config.test_chat_id,
        text=f"✅ <b>Story Ready for Review</b>\n\nArticle: {article_version}\nImage: {image_version}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    
    if controls_result.get("ok") and controls_result.get("message_id"):
        results["message_ids"].append(controls_result["message_id"])
        results["controls_sent"] = True
    else:
        results["ok"] = False
        results["error"] = "Controls send failed"
    
    return results


def send_revision_package(
    client: TelegramTestClient,
    config: TelegramConfig,
    version_store: VersionStore,
    event_id: str,
    canonical_title: str,
    article_version: str | None,
    image_version: str | None,
    article_revised: bool,
    image_revised: bool,
) -> dict[str, Any]:
    """Send revision package after revision.
    
    Handles three modes:
    - Article-only: sends new article, reuses existing image
    - Image-only: reuses existing article, sends new image
    - Both: sends both new
    
    Flow:
    1. Send revised article (if article_revised)
    2. Send existing article context (if image-only revision for context)
    3. Send revised image (if image_revised)
    4. Send existing image (if article-only revision, for visual context)
    5. Send review controls
    """
    # Determine which versions to display
    current_article = article_version if article_revised else None
    current_image = image_version if image_revised else None
    
    # For context, we might need to load previous versions
    # But the revision controller sets the current version, so we use that
    display_article_version = article_version or "V1"
    display_image_version = image_version or "V1"
    
    results = {
        "ok": True,
        "event_id": event_id,
        "article_revised": article_revised,
        "image_revised": image_revised,
        "article_version": article_version,
        "image_version": image_version,
        "message_ids": [],
        "kimi_calls": 0,
        "vertex_calls": 0,
    }
    
    # Determine headline
    article_record = version_store.get_article(event_id, display_article_version)
    headline = canonical_title
    dek = None
    body = ""
    
    if article_record:
        article_data = article_record.get("article", {})
        headline = article_data.get("headline", canonical_title)
        dek = article_data.get("dek")
        body = article_data.get("article_body", "")
    
    # Send article
    if current_article:
        # New article version
        article_results = _send_article_text(
            client=client,
            chat_id=config.test_chat_id,
            headline=f"[REVISED] {headline}",
            dek=dek,
            body=body,
            version=current_article,
        )
    else:
        # Existing article - send brief context
        article_results = _send_article_text(
            client=client,
            chat_id=config.test_chat_id,
            headline=headline,
            dek=dek,
            body="...(existing article - see previous messages for full text)...",
            version=display_article_version,
        )
    
    for r in article_results:
        if r.get("ok") and r.get("message_id"):
            results["message_ids"].append(r["message_id"])
    
    results["article_sent"] = all(r.get("ok") for r in article_results)
    
    # Send image
    if current_image:
        image_path = version_store.get_image_path(event_id, current_image)
        if image_path and image_path.is_file():
            image_result = _send_image(
                client=client,
                chat_id=config.test_chat_id,
                image_path=image_path,
                headline=headline,
                version=current_image,
            )
            
            if image_result.get("ok") and image_result.get("message_id"):
                results["message_ids"].append(image_result["message_id"])
                results["image_sent"] = True
    else:
        # Existing image - resend for context
        image_path = version_store.get_image_path(event_id, display_image_version)
        if image_path and image_path.is_file():
            image_result = _send_image(
                client=client,
                chat_id=config.test_chat_id,
                image_path=image_path,
                headline=headline,
                version=f"{display_image_version} (existing)",
            )
            
            if image_result.get("ok") and image_result.get("message_id"):
                results["message_ids"].append(image_result["message_id"])
                results["image_sent"] = True
    
    # Send review controls
    keyboard = _build_review_keyboard(event_id, display_article_version, display_image_version)
    controls_result = client.send_message(
        chat_id=config.test_chat_id,
        text=f"🔄 <b>Revision Complete</b>\n\nArticle: {display_article_version}{' (revised)' if article_revised else ''}\nImage: {display_image_version}{' (revised)' if image_revised else ''}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    
    if controls_result.get("ok") and controls_result.get("message_id"):
        results["message_ids"].append(controls_result["message_id"])
        results["controls_sent"] = True
    
    return results


def send_compact_revision_summary(
    client: TelegramTestClient,
    config: TelegramConfig,
    version_store: VersionStore,
    event_id: str,
    canonical_title: str,
    article_version: str | None,
    image_version: str | None,
    article_revised: bool,
    image_revised: bool,
) -> dict[str, Any]:
    """Send compact revision summary with VIEW FULL ARTICLE button.
    
    Target UX:
    ✍️ Article V2 — REVISED
    <headline>
    <short preview 250-400 chars>
    [📖 VIEW FULL ARTICLE]
    
    🖼 Image V1 — REUSED
    
    Then existing review controls.
    
    ZERO provider calls - only loads frozen artifacts.
    """
    # Determine display versions
    current_article = article_version or "V1"
    current_image = image_version or "V1"
    
    article_record = version_store.get_article(event_id, current_article)
    if not article_record:
        return {"ok": False, "error": "article_not_found"}
    
    article = article_record.get("article", {})
    headline = article.get("headline", canonical_title) or "Unknown"
    body = article.get("article_body", "")
    
    # Build compact preview (250-400 chars)
    preview_text = body[:400].strip()
    if len(preview_text) > 380:
        # Find last space before limit
        last_space = preview_text.rfind(" ", 0, 380)
        if last_space > 0:
            preview_text = preview_text[:last_space] + "..."
        else:
            preview_text = preview_text[:380] + "..."
    elif len(preview_text) == len(body) and len(body) > 0:
        # Short article, use full
        preview_text = body
    elif body:
        preview_text = body[:400].strip() + "..."
    
    # Build status labels
    article_status = "REVISED" if article_revised else "REUSED"
    image_status = "NEW" if image_revised else "REUSED"
    
    # Build compact message
    message_parts = [
        f"✍️ Article {current_article} — {article_status}",
        "",
        f"<b>{_escape_html(headline)}</b>",
        "",
        _escape_html(preview_text) if preview_text else "(no preview available)",
    ]
    
    # Add VIEW FULL ARTICLE callback
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "📖 VIEW FULL ARTICLE",
                    "callback_data": f"view_full:{event_id}:{current_article}:article",
                }
            ]
        ]
    }
    
    # Send compact article summary
    result = client.send_message(
        chat_id=config.test_chat_id,
        text="\n".join(message_parts),
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    
    article_message_id = result.get("message_id") if result.get("ok") else None
    
    # Send image status
    image_path = version_store.get_image_path(event_id, current_image)
    image_message = f"🖼 Image {current_image} — {image_status}"
    
    if image_path and image_path.exists():
        # Send actual persisted image (regardless of revision status - zero cost)
        image_result = _send_image(
            client=client,
            chat_id=config.test_chat_id,
            image_path=image_path,
            headline=headline,
            version=current_image,
        )
        image_message_id = image_result.get("message_id") if image_result.get("ok") else None
        
        if not image_message_id:
            print(f"[WARNING] Failed to send image: {image_path}")
    else:
        # Image path not found - print explicit error, NEVER regenerate
        print(f"[ERROR] Image not found: {image_path}")
        image_result = client.send_message(
            chat_id=config.test_chat_id,
            text=image_message + "\n[ERROR: Image file not found]",
            parse_mode="HTML",
        )
        image_message_id = image_result.get("message_id") if image_result.get("ok") else None
    
    # Send review controls targeting CURRENT versions
    review_keyboard = _build_review_keyboard(event_id, current_article, current_image)
    controls_result = client.send_message(
        chat_id=config.test_chat_id,
        text="🔍 Review current versions:",
        parse_mode="HTML",
        reply_markup=review_keyboard,
    )
    controls_message_id = controls_result.get("message_id") if controls_result.get("ok") else None
    
    return {
        "ok": True,
        "event_id": event_id,
        "article_version": current_article,
        "image_version": current_image,
        "article_revised": article_revised,
        "image_revised": image_revised,
        "message_ids": [mid for mid in [article_message_id, image_message_id, controls_message_id] if mid],
        "kimi_calls": 0,
        "vertex_calls": 0,
    }
