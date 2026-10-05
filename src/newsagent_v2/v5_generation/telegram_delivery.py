"""V5 Telegram Delivery - sends frozen artifacts to Telegram.

Handles:
- Initial generation completion (article + image + controls)
- Revision completion (article-only, image-only, or both)
- Artifact splitting for Telegram limits
- Version-aware controls

ZERO generation calls - only loads frozen artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

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


def _load_review_telemetry(
    event_id: str,
    generation_result: dict[str, Any] | None,
    environ: dict[str, str] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load durable provider receipts used by the review card."""
    from newsagent_v2.v5_generation.depth_cost_helpers import accumulate_vertex_receipts

    result = generation_result or {}
    text = dict(result.get("kimi_usage") or result.get("text_usage") or {})
    image = dict(result.get("image_usage") or {})
    stories_root = Path((environ or {}).get("V5_STORIES_ROOT") or Path(__file__).resolve().parents[3] / "output" / "v5_stories")
    try:
        from newsagent_v2.providers.kimi_budget import KimiBudgetStore

        budget = KimiBudgetStore(root=stories_root).load_story_budget(event_id)
        if budget is not None:
            text.update({
                "requests": budget.request_count,
                "request_count": budget.request_count,
                "prompt_tokens": budget.prompt_tokens,
                "completion_tokens": budget.completion_tokens,
                "total_tokens": budget.total_tokens,
                "status": budget.status,
            })
    except (OSError, TypeError, ValueError):
        pass

    make_runs_root = Path((environ or {}).get("V5_MAKE_RUNS_ROOT") or Path(__file__).resolve().parents[3] / "output" / "make_runs")
    accumulated = accumulate_vertex_receipts(event_id, make_runs_root, environ)
    if accumulated.get("requests"):
        merged = dict(image)
        merged.update({k: v for k, v in accumulated.items() if v is not None})
        image = merged
    elif not image:
        image = accumulated
    return text, image


def _configured_vertex_cost_usd(image: dict[str, Any], environ: dict[str, str]) -> float | None:
    from newsagent_v2.v5_generation.depth_cost_helpers import compute_vertex_receipt_cost_usd

    cost, _missing = compute_vertex_receipt_cost_usd(
        {
            "provider_name": image.get("provider"),
            "model_name": image.get("model"),
            "provider_reported_usage": image.get("provider_reported_usage"),
            "provider_reported_cost_usd": image.get("accumulated_cost_usd") or image.get("provider_reported_cost_usd"),
        },
        environ,
    )
    return cost


def _configured_text_cost_usd(usage: dict[str, Any], environ: dict[str, str]) -> float | None:
    from newsagent_v2.v5_generation.depth_cost_helpers import compute_kimi_cost_usd

    cost, _missing = compute_kimi_cost_usd(usage, environ)
    return cost


def _wp_admin_editor_url(base_url: str | None, post_id: Any) -> str | None:
    """Build an authenticated admin editor target for a persisted post ID."""
    if not base_url or not isinstance(post_id, int) or isinstance(post_id, bool):
        return None
    base = str(base_url).strip().rstrip("/") + "/"
    return urljoin(base, f"wp-admin/post.php?post={post_id}&action=edit")


def send_initial_review_package(
    client: TelegramTestClient,
    config: TelegramConfig,
    version_store: VersionStore,
    event_id: str,
    canonical_title: str,
    article_version: str,
    image_version: str,
    send_controls: bool = True,
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
    
    if not send_controls:
        return results

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


_QA_FLAG_LIMIT = 6


def _qa_review_text(article: dict[str, Any], seo: dict[str, Any] | None = None) -> str:
    """Length, sources and open QA flags for the editor; empty for pre-V6 articles."""
    if article.get("pipeline") != "v6":
        return ""
    flags = [f for f in article.get("qa_flags") or [] if f.get("severity") in {"fix", "warn"}]
    lines = [
        f"\n<b>Length:</b> {article.get('body_words', 0)} body words "
        f"({article.get('total_words', 0)} with Conclusion + FAQ)",
        f"<b>Sources used:</b> {len(article.get('sources') or [])}",
    ]
    if not flags:
        lines.append("<b>QA:</b> no open flags")
    else:
        lines.append(f"<b>QA flags ({len(flags)}) — your call:</b>")
        for flag in flags[:_QA_FLAG_LIMIT]:
            marker = "⚠️" if flag.get("severity") == "fix" else "•"
            lines.append(
                f"{marker} {_escape_html(str(flag.get('location')))}: {_escape_html(str(flag.get('message'))[:140])}"
            )
        if len(flags) > _QA_FLAG_LIMIT:
            lines.append(f"… and {len(flags) - _QA_FLAG_LIMIT} more")
    lines.extend(_seo_review_lines(seo))
    return "\n".join(lines) + "\n"


_SEO_MISS_LIMIT = 5


def _seo_review_lines(seo: dict[str, Any] | None) -> list[str]:
    if not seo:
        return []
    lines = [
        f"<b>SEO score:</b> {seo.get('score', '?')}/100 · keyword "
        f"\"{_escape_html(str(seo.get('focus_keyword') or ''))}\" · {seo.get('internal_links', 0)} internal links"
    ]
    duplicate = seo.get("possible_duplicate")
    if duplicate:
        lines.append(f"⚠️ Possible duplicate of: {_escape_html(str(duplicate.get('title'))[:100])} — {_escape_html(str(duplicate.get('url')))}")
    misses = sorted(
        (c for c in seo.get("checks") or [] if not c.get("passed")),
        key=lambda c: c.get("points", 0) - c.get("max_points", 0),
    )
    for check in misses[:_SEO_MISS_LIMIT]:
        detail = f" ({_escape_html(str(check['detail'])[:80])})" if check.get("detail") else ""
        lines.append(f"• SEO: {_escape_html(str(check.get('label')))}{detail}")
    return lines


def send_initial_v5_review_package(
    client: TelegramTestClient,
    config: TelegramConfig,
    version_store: VersionStore,
    event_id: str,
    canonical_title: str,
    article_version: str,
    image_version: str | None,
    generation_result: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Deliver canonical V5 review controls with persisted publication state."""
    article_record = version_store.get_article(event_id, article_version)
    if not article_record:
        return {"ok": False, "event_id": event_id, "error": f"Article {article_version} not found"}
    article = article_record.get("article", {})
    result = {"ok": True, "event_id": event_id, "article_version": article_version,
              "image_version": image_version, "article_sent": False, "image_sent": False,
              "controls_sent": False, "message_ids": []}
    # Compact review surface: headline/dek only. The frozen article body is not sent.
    headline = article.get("headline") or canonical_title
    dek = article.get("dek") or ""
    image_path = version_store.get_image_path(event_id, image_version) if image_version else None
    if image_path and image_path.is_file():
        image_result = _send_image(client, config.test_chat_id, image_path, headline, image_version or "v1")
        result["image_sent"] = bool(image_result.get("ok"))
        if image_result.get("message_id"):
            result["message_ids"].append(image_result["message_id"])
    compact = f"<b>Headline:</b> {_escape_html(str(headline))}"
    if dek:
        compact += f"\n<b>Dek:</b> {_escape_html(str(dek))}"
    sent_article = client.send_message(chat_id=config.test_chat_id, text=compact, parse_mode="HTML")
    result["article_sent"] = bool(sent_article.get("ok"))
    if sent_article.get("message_id"):
        result["message_ids"].append(sent_article["message_id"])
    draft = (generation_result or {}).get("wordpress_draft") or {}
    usage, image = _load_review_telemetry(event_id, generation_result, environ)
    admin_url = _wp_admin_editor_url((environ or {}).get("NEWSAGENT_V2_WORDPRESS_BASE_URL"), draft.get("wp_post_id"))
    keyboard = {"inline_keyboard": [
        [{"text": "RATE ARTICLE", "callback_data": f"rate_article:{event_id}:{article_version}"}, {"text": "RATE IMAGE", "callback_data": f"rate_image:{event_id}:{image_version or 'v1'}"}],
        [{"text": "ARTICLE FEEDBACK", "callback_data": f"feedback_article:{event_id}:{article_version}"}, {"text": "IMAGE FEEDBACK", "callback_data": f"feedback_image:{event_id}:{image_version or 'v1'}"}],
        [{"text": "REVISE", "callback_data": f"revise:{event_id}:{article_version}:{image_version or ''}"}, {"text": "EDIT", "callback_data": f"edit:{event_id}:{article_version}"}],
        [{"text": "REJECT", "callback_data": f"reject:{event_id}:{article_version}"}],
        [{"text": "APPROVE & PUBLISH", "callback_data": f"approve:{event_id}:{article_version}"}],
    ]}
    if admin_url:
        keyboard["inline_keyboard"].insert(0, [{"text": "OPEN DRAFT", "url": admin_url}])
    from newsagent_v2.v5_generation.depth_cost_helpers import build_review_cost_text
    if usage.get("provider") is None and (generation_result or {}).get("writer_provider"):
        usage["provider"] = (generation_result or {}).get("writer_provider")
    if usage.get("model") is None and (generation_result or {}).get("writer_model"):
        usage["model"] = (generation_result or {}).get("writer_model")
    cost_block = build_review_cost_text(
        text_usage=usage,
        image_usage=image,
        environ=environ or {},
        escape_html=_escape_html,
    )
    text = (
        "✅ <b>READY FOR REVIEW</b>\n\n"
        f"<b>Headline:</b> {_escape_html(str(headline))}\n"
        f"<b>Dek:</b> {_escape_html(str(dek or 'unavailable'))}\n"
        f"WP Draft: #{_escape_html(str(draft.get('wp_post_id') or 'unavailable'))} — {_escape_html(str(draft.get('wp_url') or 'unavailable'))} ({_escape_html(str(draft.get('status') or 'unavailable'))})\n"
        f"SEO: {_escape_html(str(draft.get('seo_status') or 'unavailable'))}\n"
        f"Categories: {_escape_html(', '.join(draft.get('categories') or []) or 'none')}\n"
        f"Tags: {_escape_html(', '.join(draft.get('tags') or []) or 'none')}\n"
        f"{_qa_review_text(article, draft.get('seo_report'))}"
        f"{cost_block}"
    )
    sent = client.send_message(chat_id=config.test_chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)
    if sent.get("message_id"):
        result["message_ids"].append(sent["message_id"])
    result["controls_sent"] = bool(sent.get("ok"))
    result["ok"] = result["ok"] and result["controls_sent"]
    return result


def _usd(value: Any) -> str:
    return f"${float(value):.4f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "unavailable"


def _usd_sum(*values: str) -> str:
    numeric = [float(value[1:]) for value in values if value.startswith("$")]
    return f"${sum(numeric):.4f}" if len(numeric) == len(values) else "unavailable"


def _usage_summary(usage: Any, *, image: dict[str, Any] | None = None) -> str:
    if not isinstance(usage, dict):
        return "unavailable"
    parts = []
    for key in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount", "image_tokens", "input_tokens", "output_tokens", "total_tokens"):
        if usage.get(key) is not None:
            parts.append(f"{key}={usage[key]}")
    return ", ".join(parts) or "provider-reported usage unavailable"


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
    environ: dict[str, str] | None = None,
    generation_result: dict[str, Any] | None = None,
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
    
    # Send review controls + accumulated cost (same format as initial review)
    keyboard = _build_review_keyboard(event_id, display_article_version, display_image_version)
    article_record = version_store.get_article(event_id, display_article_version) or {}
    meta = dict(article_record.get("metadata") or {})
    billable = dict(meta.get("billable_usage") or {})
    gen = dict(generation_result or {})
    usage, image = _load_review_telemetry(event_id, {
        **gen,
        "text_usage": gen.get("text_usage") or billable.get("text") or meta.get("text_usage") or {},
        "image_usage": gen.get("image_usage") or billable.get("image") or meta.get("image_usage") or {},
        "kimi_usage": gen.get("kimi_usage") or billable.get("text") or {},
    }, environ)
    from newsagent_v2.v5_generation.depth_cost_helpers import build_review_cost_text
    cost_block = build_review_cost_text(
        text_usage=usage,
        image_usage=image,
        environ=environ or {},
        escape_html=_escape_html,
    )
    controls_result = client.send_message(
        chat_id=config.test_chat_id,
        text=(
            f"🔄 <b>Revision Complete</b>\n\n"
            f"Article: {display_article_version}{' (revised)' if article_revised else ''}\n"
            f"Image: {display_image_version}{' (revised)' if image_revised else ''}\n\n"
            f"{cost_block}"
        ),
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
