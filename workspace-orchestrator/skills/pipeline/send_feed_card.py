#!/usr/bin/env python3
"""send_feed_card.py — Post hourly single-click headline feed cards.

Pure Python, NO LLM. Runs via pool_scheduler (hourly) or on demand. Reads the
per-project `headline_pool`, posts one Telegram message per headline (linked
title + source + Run button). One tap queues that story for the pipeline via
the feed_jobs serial queue.

Callback prefixes (handled by handle_card_feedback.py):
  oc_go:{feed_id}:{idx}       run this single headline (queued if busy)
  oc_feed_refresh:{feed_id}   rebuild from the latest pool (legacy, edit in place)

Usage:
  python3 send_feed_card.py --project memecoinist
  python3 send_feed_card.py --all [--limit 5]

Always exits 0. Prints FEED_CARD_SENT lines to stderr and NO_REPLY to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from html import escape as html_escape

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import editorial_db as db  # noqa: E402
import project_config as pc  # noqa: E402
from build_and_send_card import (  # noqa: E402
    TELEGRAM_CONFIG,
    edit_message_text,
    load_bot_token,
    load_json,
    resolve_chat_id,
    telegram_request,
)

FEED_CAPTION_MAX = 3900  # sendMessage text limit is 4096; leave headroom
HEADLINES_PER_PROJECT = 5
MAX_CANDIDATES = HEADLINES_PER_PROJECT


def make_feed_id(project: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"f-{project}-{ts}"


def candidates_from_pool(project: str, limit: int) -> list[dict]:
    rows = db.fresh_pool(project, limit=limit)
    out: list[dict] = []
    for i, c in enumerate(rows, 1):
        out.append(
            {
                "index": i,
                "headline": c.headline,
                "url": c.url,
                "source": c.source or "",
                "pub_date": c.pub_date or "",
                "summary": c.summary or "",
            }
        )
    return out


def build_headline_card_caption(
    project_name: str, card_prefix: str, candidate: dict, *, total: int
) -> str:
    tag_label = card_prefix or project_name
    idx = int(candidate["index"])
    ref = f"{html_escape(tag_label)} {idx}/{total}"
    src = (
        f" <i>({html_escape(candidate['source'])})</i>"
        if candidate.get("source")
        else ""
    )
    head = html_escape(candidate["headline"])
    url = html_escape(candidate.get("url") or "")
    head = f'<a href="{url}">{head}</a>' if url else head
    text = f"<i>{ref}</i>\n\n{head}{src}"
    if len(text) > FEED_CAPTION_MAX:
        text = text[: FEED_CAPTION_MAX - 1] + "…"
    return text


def build_run_card_keyboard(feed_id: str, index: int, state: str = "ready") -> dict:
    labels = {
        "ready": "Run this story",
        "queued": "Queued",
        "running": "Running...",
    }
    label = labels.get(state, "Run this story")
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": f"oc_go:{feed_id}:{index}"}]
        ]
    }


def build_feed_caption(project_name: str, card_prefix: str, candidates: list[dict]) -> str:
    """Legacy combined-card caption (kept for back-compat)."""
    tag = card_prefix or (f"[{project_name}]" if project_name else "")
    lines: list[str] = []
    if tag:
        lines.append(f"<b>{html_escape(tag)}</b>")
    lines.append("<b>Today's headlines — tap numbers to select, then Publish selected</b>")
    lines.append("")
    if not candidates:
        lines.append("<i>No fresh headlines in the pool right now. Try again shortly.</i>")
    for c in candidates:
        src = f" <i>({html_escape(c['source'])})</i>" if c.get("source") else ""
        head = html_escape(c["headline"])
        url = html_escape(c.get("url") or "")
        head = f'<a href="{url}">{head}</a>' if url else head
        lines.append(f"<b>{c['index']}.</b> {head}{src}")
    lines.append("")
    lines.append("Tap a number to toggle. Selected show a check. Then tap <b>Publish selected</b>.")
    text = "\n".join(lines)
    if len(text) > FEED_CAPTION_MAX:
        text = text[: FEED_CAPTION_MAX - 1] + "…"
    return text


def build_feed_keyboard(feed_id: str, candidates: list[dict], selected: list[int]) -> dict:
    """Legacy combined-card keyboard (kept for back-compat)."""
    sel = set(int(i) for i in selected)
    rows: list[list[dict[str, str]]] = []
    tiles = []
    for c in candidates:
        idx = int(c["index"])
        label = f"\u2713 {idx}" if idx in sel else str(idx)
        tiles.append({"text": label, "callback_data": f"oc_sel:{feed_id}:{idx}"})
    for i in range(0, len(tiles), 5):
        rows.append(tiles[i : i + 5])
    action_row = [
        {"text": "\u25b6 Publish selected", "callback_data": f"oc_go:{feed_id}"},
        {"text": "\U0001f504 Refresh", "callback_data": f"oc_feed_refresh:{feed_id}"},
    ]
    rows.append(action_row)
    return {"inline_keyboard": rows}


def _project_meta(project: str) -> tuple[str, str]:
    """Return (project_name, card_prefix)."""
    try:
        cfg = pc.load_project_config(slug=project)
        name = cfg.get("name") or project.title()
        prefix = str(cfg.get_path("telegram.card_prefix", "") or "")
        return name, prefix
    except (FileNotFoundError, ValueError):
        return project.title(), ""


def _send_text_card(
    token: str, chat_id: str, caption: str, keyboard: dict
) -> int | None:
    result = telegram_request(
        token,
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
            "reply_markup": json.dumps(keyboard),
        },
    )
    return (result.get("result") or {}).get("message_id")


def send_one(project: str, *, token: str, chat_id: str, limit: int) -> str:
    limit = min(int(limit), HEADLINES_PER_PROJECT)
    candidates = candidates_from_pool(project, limit)
    if not candidates:
        return f"FEED_SKIP: project={project} reason=empty_pool"
    feed_id = make_feed_id(project)
    name, prefix = _project_meta(project)
    total = len(candidates)

    db.insert_feed_card(feed_id, project, chat_id, candidates)

    for c in candidates:
        caption = build_headline_card_caption(name, prefix, c, total=total)
        keyboard = build_run_card_keyboard(feed_id, int(c["index"]), "ready")
        message_id = _send_text_card(token, chat_id, caption, keyboard)
        if message_id is not None:
            c["message_id"] = int(message_id)

    db.update_feed_card_candidates(feed_id, candidates)
    db.mark_pool(project, [c["url"] for c in candidates], "shown")

    return (
        f"FEED_CARD_SENT: project={project} feed_id={feed_id} "
        f"headline_cards={len(candidates)}"
    )


def refresh_card(feed_id: str, *, token: str, fallback_chat_id: str, limit: int) -> str:
    """Rebuild headline cards in place from the latest pool (legacy Refresh)."""
    card = db.get_feed_card(feed_id)
    if not card:
        return f"FEED_REFRESH_FAIL: feed_id={feed_id} reason=not_found"
    project = card.project
    # Prefer the chat the card was originally posted to; then per-project config.
    chat_id = str(card.telegram_group or "").strip() or resolve_chat_id(
        project, fallback=fallback_chat_id
    )
    limit = min(int(limit), HEADLINES_PER_PROJECT)
    try:
        old_candidates = json.loads(card.candidates_json)
    except (ValueError, TypeError):
        old_candidates = []
    old_by_idx = {
        int(c["index"]): c
        for c in old_candidates
        if "index" in c and c.get("message_id")
    }

    candidates = candidates_from_pool(project, limit)
    name, prefix = _project_meta(project)
    total = len(candidates) or len(old_candidates)

    for c in candidates:
        idx = int(c["index"])
        if idx in old_by_idx:
            old = old_by_idx[idx]
            c["message_id"] = int(old["message_id"])
            if old.get("claimed"):
                c["claimed"] = True

    db.insert_feed_card(feed_id, project, chat_id, candidates)
    db.set_feed_card_selection(feed_id, [])

    for c in candidates:
        msg_id = c.get("message_id")
        state = "queued" if c.get("claimed") else "ready"
        caption = build_headline_card_caption(name, prefix, c, total=total)
        keyboard = build_run_card_keyboard(feed_id, int(c["index"]), state)
        if not msg_id:
            new_id = _send_text_card(token, chat_id, caption, keyboard)
            if new_id is not None:
                c["message_id"] = int(new_id)
            continue
        edit_message_text(
            token,
            chat_id,
            int(msg_id),
            caption,
            reply_markup=json.dumps(keyboard),
        )

    db.update_feed_card_candidates(feed_id, candidates)
    if candidates:
        db.mark_pool(project, [c["url"] for c in candidates], "shown")

    return f"FEED_CARD_REFRESHED: feed_id={feed_id} candidates={len(candidates)}"


def main() -> int:
    p = argparse.ArgumentParser(description="Send hourly headline feed cards")
    p.add_argument("--project", help="Single project slug")
    p.add_argument("--all", action="store_true", help="Send a card for every project")
    p.add_argument(
        "--limit",
        type=int,
        default=MAX_CANDIDATES,
        help=f"Headlines per project (default {MAX_CANDIDATES}, max {HEADLINES_PER_PROJECT})",
    )
    p.add_argument("--refresh-feed-id", help="Refresh an existing feed card in place")
    args = p.parse_args()

    try:
        token = load_bot_token()
        tg_cfg = load_json(TELEGRAM_CONFIG)
        fallback_chat_id = str(tg_cfg.get("group_id") or "").strip()
        if not token:
            print("FEED_ERROR: missing telegram token", file=sys.stderr)
            print("NO_REPLY")
            return 0
    except Exception as e:
        print(f"FEED_ERROR: {e}", file=sys.stderr)
        print("NO_REPLY")
        return 0

    if args.refresh_feed_id:
        print(
            refresh_card(
                args.refresh_feed_id,
                token=token,
                fallback_chat_id=fallback_chat_id,
                limit=args.limit,
            ),
            file=sys.stderr,
        )
        print("NO_REPLY")
        return 0

    if args.all:
        projects = pc.list_available_projects()
    elif args.project:
        projects = [args.project]
    else:
        projects = [pc.resolve_project_slug()]

    for slug in projects:
        try:
            chat_id = resolve_chat_id(slug, fallback=fallback_chat_id)
            if not chat_id:
                print(f"FEED_ERROR: project={slug} reason=no_group_id", file=sys.stderr)
                continue
            print(send_one(slug, token=token, chat_id=chat_id, limit=args.limit), file=sys.stderr)
        except Exception as e:
            print(f"FEED_ERROR: project={slug} detail={e}", file=sys.stderr)

    print("NO_REPLY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
