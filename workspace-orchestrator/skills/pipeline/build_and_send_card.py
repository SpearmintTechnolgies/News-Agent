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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from editorial_db import init_db, insert_article, save_article_version  # noqa: E402

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

    lines = [
        f"<b>ALERT: ta-{html_escape(run_id)}</b>",
        f"<b>{headline}</b>",
        "",
    ]
    if topic or why:
        detail = topic
        if why:
            detail = f"{detail}. {why}" if detail else why
        lines.append(truncate(detail, 300))
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

    lines.append("")
    lines.append("Reply: <code>RATE 1-10</code> | <code>IMAGE 1-10</code> | <code>DRAFT</code> | <code>PUBLISH</code> | <code>EDIT</code> (.md file)")

    caption = "\n".join(lines)
    if len(caption) > TELEGRAM_CAPTION_MAX:
        caption = caption[: TELEGRAM_CAPTION_MAX - 1] + "…"
    return caption


def telegram_request(
    token: str, method: str, data: dict | None = None, files: dict | None = None
) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = "----OpenClawBoundary"
        body_parts: list[bytes] = []
        for name, (filename, content, mime) in files.items():
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            body_parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
            body_parts.append(content)
            body_parts.append(b"\r\n")
        if data:
            for key, val in data.items():
                body_parts.append(f"--{boundary}\r\n".encode())
                body_parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
                body_parts.append(str(val).encode("utf-8"))
                body_parts.append(b"\r\n")
        body_parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(body_parts)
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
    else:
        encoded = urllib.parse.urlencode(data or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
        )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    result = json.loads(raw)
    if not result.get("ok"):
        desc = result.get("description", "unknown Telegram error")
        raise RuntimeError(desc)
    return result


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


def build_card_from_manifest(manifest_path: str) -> tuple[dict, str, str | None]:
    manifest = load_json(manifest_path)
    if not manifest:
        raise ValueError(f"manifest missing or invalid: {manifest_path}")

    run_id = manifest.get("run_id", "")
    run_dir = manifest.get("run_dir", "")
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

    card: dict[str, Any] = {
        "run_id": run_id,
        "run_dir": run_dir,
        "alert_id": f"ta-{run_id}",
        "story_id": str(research.get("story_id") or "") or None,
        "headline": research.get("primary_headline") or wp.get("article_headline") or "",
        "seo_title": wp.get("seo_title") or "",
        "topic_theme": research.get("topic_theme") or "",
        "why_now": first_key_fact(research.get("combined_key_facts")),
        "primary_asset": research.get("primary_asset") or "",
        "primary_keyword": research.get("primary_keyword") or "",
        "category": str(research.get("category") or "").strip() or None,
        "sources_count": sources_count,
        "wp_url": wp_url,
        "wp_post_id": str(wp.get("post_id") or ""),
        "wp_status": str(wp.get("post_status") or "draft"),
        "drive_url": extract_drive_url(drive),
        "image_path": image_path if image_path and os.path.isfile(image_path) else None,
        "card_sent_at": datetime.now(timezone.utc).isoformat(),
        "telegram_group": "",
        "telegram_message_id": None,
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
        chat_id = str(tg_cfg.get("group_id") or "").strip()
        if not chat_id:
            raise ValueError("group_id missing in telegram_card_config.json")

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
