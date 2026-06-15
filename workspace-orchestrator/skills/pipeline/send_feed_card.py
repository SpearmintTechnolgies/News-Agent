#!/usr/bin/env python3
"""send_feed_card.py — Post the daily "tap to select" headline feed card.

Pure Python, NO LLM. Runs via pool_scheduler (daily 10:00) or
on demand. Reads the per-project `headline_pool`, posts a numbered headline
list to the news-agent GROUP with inline selection buttons, and records a
`feed_cards` row. A team member taps number tiles to toggle selection, then
"Publish selected" to start the pipeline on the chosen titles.

Callback prefixes (handled by handle_card_feedback.py):
  oc_sel:{feed_id}:{idx}   toggle candidate idx
  oc_go:{feed_id}          publish the selected candidates
  oc_feed_refresh:{feed_id} rebuild from the latest pool (edit in place)

Usage:
  python3 send_feed_card.py --project memecoinist
  python3 send_feed_card.py --all [--limit 10]

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
    edit_message_reply_markup,
    edit_message_text,
    load_bot_token,
    load_json,
    telegram_request,
)

FEED_CAPTION_MAX = 3900  # sendMessage text limit is 4096; leave headroom
MAX_CANDIDATES = 10


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


def build_feed_caption(project_name: str, card_prefix: str, candidates: list[dict]) -> str:
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
        lines.append(f"<b>{c['index']}.</b> {head}{src}")
    lines.append("")
    lines.append("Tap a number to toggle. Selected show a check. Then tap <b>Publish selected</b>.")
    text = "\n".join(lines)
    if len(text) > FEED_CAPTION_MAX:
        text = text[: FEED_CAPTION_MAX - 1] + "…"
    return text


def build_feed_keyboard(feed_id: str, candidates: list[dict], selected: list[int]) -> dict:
    sel = set(int(i) for i in selected)
    rows: list[list[dict[str, str]]] = []
    tiles = []
    for c in candidates:
        idx = int(c["index"])
        label = f"\u2713 {idx}" if idx in sel else str(idx)
        tiles.append({"text": label, "callback_data": f"oc_sel:{feed_id}:{idx}"})
    # rows of 5
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


def send_one(project: str, *, token: str, chat_id: str, limit: int) -> str:
    candidates = candidates_from_pool(project, limit)
    if not candidates:
        return f"FEED_SKIP: project={project} reason=empty_pool"
    feed_id = make_feed_id(project)
    name, prefix = _project_meta(project)
    caption = build_feed_caption(name, prefix, candidates)
    keyboard = build_feed_keyboard(feed_id, candidates, [])

    # Record the card first (message_id filled after send) so a crash mid-send
    # never orphans selection state.
    db.insert_feed_card(feed_id, project, chat_id, candidates)

    result = telegram_request(
        token,
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(keyboard),
        },
    )
    message_id = (result.get("result") or {}).get("message_id")
    if message_id is not None:
        db.set_feed_card_message_id(feed_id, int(message_id))
        db.mark_pool(project, [c["url"] for c in candidates], "shown")
    return f"FEED_CARD_SENT: project={project} feed_id={feed_id} message_id={message_id}"


def refresh_card(feed_id: str, *, token: str, chat_id: str, limit: int) -> str:
    """Rebuild a feed card in place from the latest pool (Refresh button)."""
    card = db.get_feed_card(feed_id)
    if not card:
        return f"FEED_REFRESH_FAIL: feed_id={feed_id} reason=not_found"
    project = card.project
    candidates = candidates_from_pool(project, limit)
    name, prefix = _project_meta(project)
    caption = build_feed_caption(name, prefix, candidates)
    keyboard = build_feed_keyboard(feed_id, candidates, [])
    # Reset selection + refresh candidate list
    db.insert_feed_card(feed_id, project, chat_id, candidates, telegram_message_id=card.telegram_message_id)
    db.set_feed_card_selection(feed_id, [])
    if card.telegram_message_id:
        edit_message_text(
            token, chat_id, int(card.telegram_message_id), caption,
            reply_markup=json.dumps(keyboard),
        )
        if candidates:
            db.mark_pool(project, [c["url"] for c in candidates], "shown")
    return f"FEED_CARD_REFRESHED: feed_id={feed_id} candidates={len(candidates)}"


def main() -> int:
    p = argparse.ArgumentParser(description="Send the daily headline feed card")
    p.add_argument("--project", help="Single project slug")
    p.add_argument("--all", action="store_true", help="Send a card for every project")
    p.add_argument("--limit", type=int, default=MAX_CANDIDATES, help="Headlines per card (default 10)")
    p.add_argument("--refresh-feed-id", help="Refresh an existing feed card in place")
    args = p.parse_args()

    try:
        token = load_bot_token()
        tg_cfg = load_json(TELEGRAM_CONFIG)
        chat_id = str(tg_cfg.get("group_id") or "").strip()
        if not token or not chat_id:
            print("FEED_ERROR: missing telegram token or group_id", file=sys.stderr)
            print("NO_REPLY")
            return 0
    except Exception as e:
        print(f"FEED_ERROR: {e}", file=sys.stderr)
        print("NO_REPLY")
        return 0

    if args.refresh_feed_id:
        print(refresh_card(args.refresh_feed_id, token=token, chat_id=chat_id, limit=args.limit), file=sys.stderr)
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
            print(send_one(slug, token=token, chat_id=chat_id, limit=args.limit), file=sys.stderr)
        except Exception as e:
            print(f"FEED_ERROR: project={slug} detail={e}", file=sys.stderr)

    print("NO_REPLY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
