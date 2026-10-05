"""V5 Telegram Delivery - sends the review card for a frozen article + image.

Used for first drafts and for revisions (the card labels the revised version).

ZERO generation calls - only loads frozen artifacts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig

from .version_store import VersionStore


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return (text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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


def _wp_admin_editor_url(base_url: str | None, post_id: Any) -> str | None:
    """Build an authenticated admin editor target for a persisted post ID."""
    if not base_url or not isinstance(post_id, int) or isinstance(post_id, bool):
        return None
    base = str(base_url).strip().rstrip("/") + "/"
    return urljoin(base, f"wp-admin/post.php?post={post_id}&action=edit")


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
    revision_label = f" — revised ({_escape_html(article_version)})" if article_version not in {"v1", "V1"} else ""
    text = (
        f"✅ <b>READY FOR REVIEW</b>{revision_label}\n\n"
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


