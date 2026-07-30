#!/usr/bin/env python3
"""
build_and_send_card.py — Build news-card.json and send to Telegram group.

Usage:
    python3 build_and_send_card.py --manifest /path/to/manifest.json

Always exits 0 (fail-open). Prints CARD_SENT or CARD_FAILED.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import tempfile
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from editorial_db import init_db, insert_article, save_article_version  # noqa: E402
import project_config as pc  # noqa: E402
from telegram_api import telegram_request  # noqa: E402

OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
TELEGRAM_CONFIG = os.path.expanduser(
    "~/.openclaw/workspace-orchestrator/config/telegram_card_config.json"
)
TELEGRAM_CAPTION_MAX = 1024


def load_json(path: str, default: dict | None = None) -> dict:
    if not path or not os.path.isfile(path):
        return default or {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else (default or {})
    except (OSError, json.JSONDecodeError):
        return default or {}


def load_bot_token() -> str:
    tg_cfg = load_json(TELEGRAM_CONFIG)
    token = str(tg_cfg.get("bot_token") or "").strip()
    if token:
        return token

    openclaw = load_json(OPENCLAW_JSON)
    telegram = (openclaw.get("channels") or {}).get("telegram") or {}
    account_id = str(tg_cfg.get("telegram_account") or "news").strip()
    account = (telegram.get("accounts") or {}).get(account_id) or {}
    token = str(account.get("botToken") or "").strip()
    if token:
        return token
    return str(telegram.get("botToken") or "").strip()


def resolve_chat_id(project: str | None, fallback: str = "") -> str:
    """Per-publication Telegram group id.

    Reads ``telegram.group_id`` from the project config so each publication
    routes to its own group. Falls back to the given default (the global
    ``telegram_card_config.json`` group) when the project has no value, so a
    missing/typo'd field can never silently drop a send.
    """
    if project:
        try:
            cfg = pc.load_project_config(slug=project)
            gid = str(cfg.get_path("telegram.group_id", "") or "").strip()
            if gid:
                return gid
        except (FileNotFoundError, ValueError):
            pass
    return str(fallback or "").strip()


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def extract_drive_url(data: dict) -> str:
    if not data:
        return ""
    for key in ("webViewLink", "web_view_link", "url", "link"):
        val = data.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    # gog may nest under file or files[0]
    for container in (data.get("file"), data.get("File")):
        if isinstance(container, dict):
            link = container.get("webViewLink") or container.get("webViewLink".lower())
            if isinstance(link, str) and link.startswith("http"):
                return link
    files = data.get("files")
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, dict):
            link = first.get("webViewLink")
            if isinstance(link, str) and link.startswith("http"):
                return link
    return ""


def first_key_fact(facts: Any) -> str:
    if isinstance(facts, list) and facts:
        return str(facts[0]).strip()
    if isinstance(facts, str):
        return facts.strip()
    return ""


def truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def format_run_tokens(total: int | None) -> str:
    n = int(total or 0)
    if n <= 0:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def short_model_label(name: str) -> str:
    label = str(name or "").strip()
    if not label:
        return "Unknown"
    if " (Bedrock)" in label:
        label = label.replace(" (Bedrock)", "")
    for suffix in (" 675B", " 80B", " 30B", " 120B"):
        if label.endswith(suffix):
            label = label[: -len(suffix)]
    return label.strip() or "Unknown"


def format_cost_usd(cost_usd: float | None) -> str:
    value = float(cost_usd or 0)
    if value <= 0:
        return ""
    if value >= 1:
        return f"~${value:.2f}"
    if value >= 0.01:
        return f"~${value:.2f}"
    return f"~${value:.3f}"


def format_duration(seconds: int | None) -> str:
    s = int(seconds or 0)
    if s <= 0:
        return ""
    if s >= 3600:
        hours = s // 3600
        minutes = (s % 3600) // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


def agent_display_name(agent: str) -> str:
    names = {
        "orchestrator": "Orchestrator",
        "researcher": "Researcher",
        "writer": "Writer",
        "creator": "Creator",
        "picker": "Picker",
        "chart-generator": "Chart Generator",
    }
    return names.get(agent, str(agent).replace("-", " ").title())


def build_run_cost_lines(card: dict, remaining_budget: int) -> list[str]:
    tokens_total = int(card.get("tokens_total") or 0)
    if tokens_total <= 0 or remaining_budget <= 0:
        return []

    token_label = format_run_tokens(tokens_total)
    header_parts = [f"<b>Run cost:</b> {token_label} tok"]
    if card.get("pricing_available") and card.get("cost_usd") is not None:
        cost_suffix = format_cost_usd(float(card.get("cost_usd") or 0))
        if cost_suffix:
            header_parts.append(cost_suffix)
    duration = format_duration(card.get("duration_seconds"))
    if duration:
        header_parts.append(duration)

    lines = [" · ".join(header_parts)]
    by_agent = card.get("by_agent") if isinstance(card.get("by_agent"), dict) else {}
    by_model = card.get("by_model") if isinstance(card.get("by_model"), dict) else {}

    detail_items: list[tuple[int, str, bool]] = []
    for agent, usage in by_agent.items():
        if not isinstance(usage, dict):
            continue
        total = int(usage.get("tokens_total") or 0)
        if total <= 0:
            continue
        primary = usage.get("primary_model")
        if not primary and isinstance(usage.get("models"), list) and usage["models"]:
            primary = usage["models"][0]
        model_meta = by_model.get(str(primary or "")) or {}
        model_name = short_model_label(str(model_meta.get("name") or primary or "Unknown"))
        token_str = format_run_tokens(total)
        line = f"- {agent_display_name(str(agent))} ({model_name}): {token_str} tok"
        if card.get("pricing_available") and usage.get("cost_usd") is not None:
            agent_cost = format_cost_usd(float(usage.get("cost_usd") or 0))
            if agent_cost:
                line += f" · {agent_cost}"
        agent_duration = format_duration(usage.get("duration_seconds"))
        if agent_duration:
            line += f" · {agent_duration}"
        priced = bool(model_meta.get("priced", True)) if primary else False
        detail_items.append((total, line, priced))

    detail_items.sort(key=lambda item: item[0], reverse=True)
    has_unpriced = False
    for _total, detail_line, priced in detail_items:
        star_line = detail_line if priced else f"{detail_line} *"
        if not priced:
            has_unpriced = True
        candidate = "\n".join(lines + [star_line])
        if len(candidate) <= remaining_budget:
            lines.append(star_line)
        else:
            break

    if has_unpriced and len(lines) > 1:
        note = "<i>* est.</i>"
        if len("\n".join(lines + [note])) <= remaining_budget:
            lines.append(note)

    image_cost = card.get("image_cost") if isinstance(card.get("image_cost"), dict) else {}
    image_cost_usd = card.get("image_cost_usd")
    if image_cost_usd is None and image_cost:
        image_cost_usd = image_cost.get("cost_usd")
    if image_cost_usd is not None and float(image_cost_usd or 0) > 0:
        model_name = short_model_label(
            str(image_cost.get("model_name") or image_cost.get("model") or "Image")
        )
        image_line = f"- Image ({model_name}): {format_cost_usd(float(image_cost_usd))}"
        candidate = "\n".join(lines + [image_line])
        if len(candidate) <= remaining_budget:
            lines.append(image_line)

    return lines


def build_per_model_breakdown(by_model: dict, max_len: int = 220) -> str:
    if not isinstance(by_model, dict) or not by_model:
        return ""
    items = sorted(
        by_model.items(),
        key=lambda kv: int((kv[1] or {}).get("tokens_total") or 0),
        reverse=True,
    )
    parts: list[str] = []
    for _model_id, usage in items:
        if not isinstance(usage, dict):
            continue
        total = int(usage.get("tokens_total") or 0)
        if total <= 0:
            continue
        label = short_model_label(str(usage.get("name") or _model_id))
        token_label = format_run_tokens(total)
        if not token_label:
            continue
        candidate = " | ".join(parts + [f"{label} {token_label}"])
        if len(candidate) > max_len and parts:
            break
        parts.append(f"{label} {token_label}")
    return " | ".join(parts)


def load_tokens_for_run(run_dir: str) -> dict[str, Any]:
    tokens_path = os.path.join(run_dir, "publish", "tokens.json")
    data = load_json(tokens_path)
    if not data:
        return {
            "tokens_in": None,
            "tokens_out": None,
            "tokens_total": None,
            "by_agent": {},
            "by_model": {},
            "cost_usd": None,
            "image_cost": {},
            "image_cost_usd": None,
            "duration_seconds": None,
            "pricing_available": False,
        }
    by_model = data.get("by_model") if isinstance(data.get("by_model"), dict) else {}
    by_agent = data.get("by_agent") if isinstance(data.get("by_agent"), dict) else {}
    return {
        "tokens_in": int(data["tokens_in"]) if data.get("tokens_in") is not None else None,
        "tokens_out": int(data["tokens_out"]) if data.get("tokens_out") is not None else None,
        "tokens_total": int(data["tokens_total"]) if data.get("tokens_total") is not None else None,
        "by_agent": by_agent,
        "by_model": by_model,
        "cost_usd": float(data["cost_usd"]) if data.get("cost_usd") is not None else None,
        "image_cost": data.get("image_cost") if isinstance(data.get("image_cost"), dict) else {},
        "image_cost_usd": float(data["image_cost_usd"])
        if data.get("image_cost_usd") is not None
        else None,
        "duration_seconds": int(data["duration_seconds"])
        if data.get("duration_seconds") is not None
        else None,
        "pricing_available": bool(data.get("pricing_available")),
    }


def resolve_attached_category_fields(
    wp: dict,
    research: dict,
    wp_categories: list[dict] | None,
) -> tuple[list[str], str | None, str | None]:
    """Prefer categories actually attached at publish time (wordpress.json)."""
    attached_names = wp.get("wp_category_names")
    attached_slugs = wp.get("wp_category_slugs")
    if isinstance(attached_names, list) and attached_names:
        slugs = (
            [str(s) for s in attached_slugs]
            if isinstance(attached_slugs, list)
            else []
        )
        labels = [str(n) for n in attached_names if str(n).strip()]
        return slugs, (labels[0] if labels else None), ", ".join(labels)
    return resolve_category_fields(research, wp_categories)


def resolve_category_fields(
    research: dict,
    wp_categories: list[dict] | None,
) -> tuple[list[str], str | None, str | None]:
    """Map publish slugs from validated.json to display names for the card."""
    raw_slugs = research.get("wp_category_slugs")
    if isinstance(raw_slugs, list):
        slugs = [str(s).strip() for s in raw_slugs if str(s).strip()]
    else:
        primary = str(research.get("category") or "").strip()
        slugs = [primary] if primary else []

    by_slug = {
        str(c.get("slug")): str(c.get("name") or c.get("slug"))
        for c in (wp_categories or [])
        if isinstance(c, dict) and c.get("slug")
    }
    labels = [by_slug.get(s, s) for s in slugs]
    category_display = ", ".join(labels) if labels else None
    primary_name = labels[0] if labels else None
    return slugs, primary_name, category_display


def build_inline_keyboard(card: dict) -> dict:
    rows: list[list[dict[str, str]]] = []
    if card.get("wp_url"):
        rows.append([{"text": "Read Article", "url": card["wp_url"]}])
    if card.get("drive_url"):
        rows.append([{"text": "Google Doc", "url": card["drive_url"]}])

    run_id = str(card.get("run_id") or "").strip()
    if run_id:
        rows.append(
            [{"text": str(n), "callback_data": f"oc_r:{run_id}:{n}"} for n in range(1, 6)]
        )
        rows.append(
            [{"text": str(n), "callback_data": f"oc_r:{run_id}:{n}"} for n in range(6, 11)]
        )
        rows.append([{"text": "Rate Image", "callback_data": f"oc_ri_menu:{run_id}"}])
        rows.append(
            [
                {"text": "Unpublish", "callback_data": f"oc_draft:{run_id}"},
                {"text": "Publish", "callback_data": f"oc_publish:{run_id}"},
                {"text": "Edit", "callback_data": f"oc_edit:{run_id}"},
            ]
        )

    return {"inline_keyboard": rows} if rows else {}


def _is_action_row(row: list[dict]) -> bool:
    for btn in row:
        cb = str(btn.get("callback_data") or "")
        if cb.startswith(("oc_draft:", "oc_publish:", "oc_edit:")):
            return True
    return False


def build_author_picker_card_keyboard(card: dict, authors: list[dict]) -> dict:
    """Full card keyboard with the action row replaced by author buttons + Back."""
    run_id = str(card.get("run_id") or "").strip()
    base = build_inline_keyboard(card)
    rows = list(base.get("inline_keyboard") or [])
    if rows and _is_action_row(rows[-1]):
        rows = rows[:-1]

    author_buttons: list[dict] = []
    for author in authors:
        author_id = int(author["id"])
        author_buttons.append(
            {
                "text": str(author.get("label") or author.get("name") or author_id),
                "callback_data": f"oc_pub_a:{run_id}:{author_id}",
            }
        )
    for i in range(0, len(author_buttons), 2):
        rows.append(author_buttons[i : i + 2])
    if run_id:
        rows.append([{"text": "Back", "callback_data": f"oc_pub_n:{run_id}"}])
    return {"inline_keyboard": rows}


def build_published_card_keyboard(card: dict, live_url: str | None = None) -> dict:
    """Card keyboard after live publish — swap Publish for inert Published; keep Unpublish + Edit."""
    card2 = dict(card)
    if live_url:
        card2["wp_url"] = live_url
    card2["wp_status"] = "publish"
    run_id = str(card2.get("run_id") or "").strip()
    base = build_inline_keyboard(card2)
    rows = list(base.get("inline_keyboard") or [])
    if rows and run_id and _is_action_row(rows[-1]):
        rows[-1] = [
            {"text": "Published", "callback_data": f"oc_noop:{run_id}"}
            if str(b.get("callback_data") or "").startswith("oc_publish:")
            else b
            for b in rows[-1]
        ]
    return {"inline_keyboard": rows}


def build_caption(card: dict) -> str:
    run_id = card.get("run_id", "")
    headline = html_escape(card.get("headline") or "Untitled")
    topic = html_escape(card.get("topic_theme") or "")
    why = html_escape(truncate(card.get("why_now") or "", 200))
    asset = html_escape(card.get("primary_asset") or "")
    keyword = html_escape(card.get("primary_keyword") or "")
    sources = card.get("sources_count", 0)
    card_sent = card.get("card_sent_at", "")[:10]
    wp_status = str(card.get("wp_status") or "draft")
    status_label = "Live" if wp_status == "publish" else "Draft"
    project_name = html_escape(card.get("project_name") or card.get("project") or "")
    project_card_prefix = html_escape(card.get("project_card_prefix") or "")
    # Use card_prefix if the project supplied one (e.g. "[MemeCoinist]"),
    # otherwise fall back to a derived tag like "[Coinography]". For the
    # coinography project today this yields the prefix configured in
    # projects/coinography.json: "[Coinography]".
    project_tag = project_card_prefix or (
        f"[{project_name}]" if project_name and project_name.lower() != "coinography" else ""
    )

    lines = []
    if project_tag:
        lines.append(f"<b>{project_tag}</b>")
    lines.extend([
        f"<b>ALERT: ta-{html_escape(run_id)}</b>",
        f"<b>{headline}</b>",
        "",
    ])
    if topic or why:
        detail = topic
        if why:
            detail = f"{detail}. {why}" if detail else why
        lines.append(truncate(detail, 300))
        lines.append("")

    category_display = html_escape(
        str(card.get("category_display") or card.get("category") or "").strip()
    )
    if category_display:
        lines.append(f"<b>Category:</b> {category_display}")
        lines.append("")

    meta = []
    if asset:
        meta.append(f"<b>Asset:</b> {asset}")
    if keyword:
        meta.append(f"<b>Tags:</b> #{keyword}")
    if meta:
        lines.append(" | ".join(meta))
    lines.append(
        f"<b>Sources:</b> {sources} | <b>WP:</b> {status_label} | <b>Card sent:</b> {card_sent}"
    )

    footer = (
        "\n\nReply: <code>RATE 1-10</code> | <code>IMAGE 1-10</code> | "
        "<code>DRAFT</code> | <code>PUBLISH</code> | <code>EDIT</code> (.md file)"
    )
    remaining_budget = TELEGRAM_CAPTION_MAX - len("\n".join(lines)) - len(footer)
    cost_lines = build_run_cost_lines(card, remaining_budget)
    lines.extend(cost_lines)

    lines.append("")
    lines.append(
        "Reply: <code>RATE 1-10</code> | <code>IMAGE 1-10</code> | "
        "<code>DRAFT</code> | <code>PUBLISH</code> | <code>EDIT</code> (.md file)"
    )

    caption = "\n".join(lines)
    if len(caption) > TELEGRAM_CAPTION_MAX:
        caption = caption[: TELEGRAM_CAPTION_MAX - 1] + "…"
    return caption


def send_telegram_card(
    token: str,
    chat_id: str,
    caption: str,
    image_path: str | None,
    reply_markup: str | None = None,
) -> int | None:
    base_data: dict[str, str] = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
    }
    if reply_markup:
        base_data["reply_markup"] = reply_markup

    if image_path and os.path.isfile(image_path):
        size = os.path.getsize(image_path)
        if size > 10000:
            mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            with open(image_path, "rb") as f:
                content = f.read()
            payload = {**base_data, "caption": caption}
            result = telegram_request(
                token,
                "sendPhoto",
                data=payload,
                files={"photo": (os.path.basename(image_path), content, mime)},
            )
            msg = result.get("result") or {}
            return msg.get("message_id")

    payload = {
        **base_data,
        "text": caption,
        "disable_web_page_preview": "false",
    }
    result = telegram_request(token, "sendMessage", data=payload)
    msg = result.get("result") or {}
    return msg.get("message_id")


def edit_message_reply_markup(
    token: str, chat_id: str, message_id: int, reply_markup: str | None
) -> dict:
    """Update only the inline keyboard of an existing message (e.g. selection
    checkmarks on the feed card). Pass reply_markup=None to clear it."""
    data: dict[str, str] = {"chat_id": str(chat_id), "message_id": str(int(message_id))}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return telegram_request(token, "editMessageReplyMarkup", data=data)


def edit_message_text(
    token: str,
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: str | None = None,
) -> dict:
    """Replace the text + (optionally) keyboard of an existing message in place
    (used by the feed card on Refresh)."""
    data: dict[str, str] = {
        "chat_id": str(chat_id),
        "message_id": str(int(message_id)),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        data["reply_markup"] = reply_markup
    return telegram_request(token, "editMessageText", data=data)


def build_card_from_manifest(manifest_path: str) -> tuple[dict, str, str | None]:
    manifest = load_json(manifest_path)
    if not manifest:
        raise ValueError(f"manifest missing or invalid: {manifest_path}")

    run_id = manifest.get("run_id", "")
    run_dir = manifest.get("run_dir", "")
    project_slug = str(manifest.get("project") or "coinography")
    project_name = project_slug.title()
    project_card_prefix = ""
    wp_categories: list[dict] = []
    try:
        cfg = pc.load_project_config(slug=project_slug)
        project_name = cfg.get("name") or project_slug.title()
        project_card_prefix = str(cfg.get_path("telegram.card_prefix", "") or "")
        raw_cats = cfg.get_path("wordpress.categories", []) or []
        wp_categories = [c for c in raw_cats if isinstance(c, dict)]
    except (FileNotFoundError, ValueError):
        pass
    artifacts = manifest.get("artifacts") or {}

    validated_path = artifacts.get("research_validated") or os.path.join(
        run_dir, "research", "validated.json"
    )
    wp_path = artifacts.get("wordpress") or os.path.join(run_dir, "publish", "wordpress.json")
    drive_path = artifacts.get("google_drive") or os.path.join(
        run_dir, "publish", "google-drive.json"
    )
    image_path = artifacts.get("feature_image") or os.path.join(run_dir, "media", "feature.jpg")
    image_path = os.path.realpath(image_path) if image_path else None

    research = load_json(validated_path)
    wp = load_json(wp_path)
    drive = load_json(drive_path)

    wp_url = (wp.get("draft_url") or wp.get("post_url") or "").strip()
    if not wp_url:
        wp_txt = os.path.join(run_dir, "publish", "wp-url.txt")
        if os.path.isfile(wp_txt):
            wp_url = open(wp_txt, encoding="utf-8").read().strip()

    sources_used = research.get("sources_used") or research.get("source_urls") or []
    sources_count = len(sources_used) if isinstance(sources_used, list) else 0

    wp_category_slugs, category_name, category_display = resolve_attached_category_fields(
        wp, research, wp_categories
    )

    card: dict[str, Any] = {
        "run_id": run_id,
        "run_dir": run_dir,
        "project": project_slug,
        "project_name": project_name,
        "project_card_prefix": project_card_prefix,
        "alert_id": f"ta-{run_id}",
        "story_id": str(research.get("story_id") or "") or None,
        "headline": research.get("primary_headline") or wp.get("article_headline") or "",
        "seo_title": wp.get("seo_title") or "",
        "topic_theme": research.get("topic_theme") or "",
        "why_now": first_key_fact(research.get("combined_key_facts")),
        "primary_asset": research.get("primary_asset") or "",
        "primary_keyword": research.get("primary_keyword") or "",
        "category": category_name,
        "wp_category_slugs": wp_category_slugs or None,
        "category_display": category_display,
        "sources_count": sources_count,
        "wp_url": wp_url,
        "wp_post_id": str(
            wp.get("post_id")
            or (re.search(r"[?&]p=(\d+)", wp_url).group(1) if wp_url and re.search(r"[?&]p=(\d+)", wp_url) else "")
        ),
        "wp_status": str(wp.get("post_status") or "draft"),
        "drive_url": extract_drive_url(drive),
        "image_path": image_path if image_path and os.path.isfile(image_path) else None,
        "card_sent_at": datetime.now(timezone.utc).isoformat(),
        "telegram_group": "",
        "telegram_message_id": None,
        **load_tokens_for_run(run_dir),
    }

    news_card_path = os.path.join(run_dir, "publish", "news-card.json")
    return card, news_card_path, image_path if card.get("image_path") else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and send Telegram news card")
    parser.add_argument("--manifest", required=True, help="Path to pipeline manifest.json")
    args = parser.parse_args()

    manifest_path = os.path.realpath(args.manifest)

    try:
        card, news_card_path, image_path = build_card_from_manifest(manifest_path)

        token = load_bot_token()
        if not token:
            raise ValueError("Telegram botToken not found in openclaw.json")

        tg_cfg = load_json(TELEGRAM_CONFIG)
        fallback_chat_id = str(tg_cfg.get("group_id") or "").strip()
        project_slug = pc.resolve_project_slug(manifest_path=manifest_path)
        chat_id = resolve_chat_id(project_slug, fallback=fallback_chat_id)
        if not chat_id:
            raise ValueError(
                "no telegram group id: set telegram.group_id in the project "
                "config or group_id in telegram_card_config.json"
            )

        card["telegram_group"] = chat_id
        caption = build_caption(card)

        keyboard = build_inline_keyboard(card)
        reply_markup = json.dumps(keyboard) if keyboard else None
        if keyboard:
            card["inline_keyboard"] = keyboard.get("inline_keyboard", [])

        message_id = send_telegram_card(
            token, chat_id, caption, image_path, reply_markup=reply_markup
        )
        card["telegram_message_id"] = message_id

        try:
            init_db()
            article_id = insert_article(card)
            run_dir = str(card.get("run_dir") or "").strip()
            final_md = os.path.join(run_dir, "article", "final.md") if run_dir else ""
            if final_md and os.path.isfile(final_md):
                with open(final_md, encoding="utf-8") as f:
                    snapshot = f.read()
                if snapshot.strip():
                    save_article_version(article_id, "published_snapshot", snapshot)
        except Exception as db_err:
            print(f"DB_INSERT_FAILED: {db_err}", file=sys.stderr)

        atomic_write_json(news_card_path, card)
        print(f"CARD_SENT: {card.get('run_id')} message_id={message_id}")
    except Exception as e:
        print(f"CARD_FAILED: {e}", file=sys.stderr)
        # Best-effort write partial card on failure
        try:
            card, news_card_path, _ = build_card_from_manifest(manifest_path)
            card["telegram_message_id"] = None
            card["send_error"] = str(e)
            atomic_write_json(news_card_path, card)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
