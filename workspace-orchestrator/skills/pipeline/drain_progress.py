#!/usr/bin/env python3
"""drain_progress.py — One Telegram message that shows which drain step is live.

Scripts own this board. Nexus must not narrate the pipeline in chat (that
historically ended the turn and stalled the story). Call notify() from
handle_card_feedback / _fire_drain_once / continue_feed_drain.

Edits one HTML message in place. Typical full run is still 35–45 min — this
only makes the wait visible, and says so when a step dies.
"""
from __future__ import annotations

import json
import os
import sys
from html import escape as html_escape

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db as db  # noqa: E402

STEPS: list[tuple[str, str]] = [
    ("queued", "Queued"),
    ("sieve", "Sieve — classifying the headline"),
    ("research", "Scout — researching"),
    ("writer", "Quill — writing the article"),
    ("image", "Pixel — making the image"),
    ("publish", "WordPress — saving the draft"),
    ("finalize", "Telegram — sending the story card"),
    ("done", "Draft card sent"),
]
STEP_INDEX = {key: i for i, (key, _) in enumerate(STEPS)}
PROGRESS_PREFIX = "tg_progress:"
ATTEMPTS_PREFIX = "drain_attempts:"


def _progress_key(job_id: int) -> str:
    return f"{PROGRESS_PREFIX}{int(job_id)}"


def _attempts_key(job_id: int) -> str:
    return f"{ATTEMPTS_PREFIX}{int(job_id)}"


def _load_state(project: str, job_id: int) -> dict:
    raw = db.get_state(project, _progress_key(job_id))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(project: str, job_id: int, data: dict) -> None:
    db.set_state(project, _progress_key(job_id), json.dumps(data, ensure_ascii=False))


def bump_attempts(project: str, job_id: int) -> int:
    """Count a retry. Stampede dispatches within 90s share one attempt."""
    import time

    at_key = f"drain_attempt_at:{int(job_id)}"
    raw_n = db.get_state(project, _attempts_key(job_id)) or "0"
    try:
        n = int(raw_n)
    except (TypeError, ValueError):
        n = 0
    last = db.get_state(project, at_key) or ""
    now = time.time()
    try:
        if last and (now - float(last)) < 90 and n > 0:
            return n
    except (TypeError, ValueError):
        pass
    n += 1
    db.set_state(project, _attempts_key(job_id), str(n))
    db.set_state(project, at_key, str(now))
    return n


def clear_attempts(project: str, job_id: int) -> None:
    db.set_state(project, _attempts_key(job_id), "0")


def render(
    headline: str,
    project: str,
    step: str,
    *,
    failed: bool = False,
    note: str = "",
) -> str:
    current = STEP_INDEX.get(step, 0)
    lines = [
        f"📰 <b>{html_escape((headline or 'Story')[:240])}</b>",
        f"<i>{html_escape(project or '')}</i>",
        "",
    ]
    visible = STEPS if step == "done" and not failed else STEPS[:-1]
    for i, (key, label) in enumerate(visible):
        if failed and key == step:
            mark = "❌"
        elif key == "done" or i < current:
            mark = "✅"
        elif i == current:
            mark = "▶️"
        else:
            mark = "⏳"
        lines.append(f"{mark} {html_escape(label)}")
    lines.append("")
    if failed:
        lines.append("<b>Stopped</b> — do not tap Run again.")
        if note:
            lines.append(html_escape(note[:500]))
        else:
            lines.append("The worker will retry automatically in about a minute.")
    elif step == "done":
        lines.append("Draft card is in this chat (photo + cost).")
    else:
        lines.append(
            "<i>This message updates in place. A full story is usually 35–45 min.</i>"
        )
        if note:
            lines.append(html_escape(note[:500]))
    return "\n".join(lines)


def _token_and_chat(project: str, chat_id: str | None) -> tuple[str, str]:
    from build_and_send_card import (
        TELEGRAM_CONFIG,
        load_bot_token,
        load_json,
        resolve_chat_id,
    )

    token = load_bot_token()
    tg_cfg = load_json(TELEGRAM_CONFIG)
    fallback = str(tg_cfg.get("group_id") or "").strip()
    chat = str(chat_id or "").strip() or resolve_chat_id(project, fallback=fallback)
    return token, chat


def notify(
    job_id: int,
    step: str,
    *,
    failed: bool = False,
    note: str = "",
    headline: str | None = None,
    project: str | None = None,
    chat_id: str | None = None,
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send or edit the step board. Best-effort; never raises to the drain."""
    try:
        db.init_db()
        job = db.get_feed_job(int(job_id))
        project = (project or (job.project if job else "") or "coinnetwork").strip()
        headline = (
            headline
            or (job.headline if job else "")
            or "Story"
        )
        db.set_feed_job_step(int(job_id), "failed" if failed else step)
        # Never stamp the drain lease from Telegram. A retry board used to
        # re-arm a 45-min lease while the job was only queued — Sieve looked
        # live and the dispatcher would not start a worker.

        state = _load_state(project, int(job_id))
        token, chat = _token_and_chat(project, chat_id or state.get("chat_id"))
        if not token or not chat:
            print("PROGRESS_WARN: missing telegram token or chat_id", file=sys.stderr)
            return None

        text = render(headline, project, step, failed=failed, note=note)
        message_id = state.get("message_id")
        from build_and_send_card import edit_message_text
        from telegram_api import telegram_request

        def _send() -> int | None:
            payload = {
                "chat_id": chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
            if reply_to_message_id is not None:
                payload["reply_to_message_id"] = str(int(reply_to_message_id))
            result = telegram_request(token, "sendMessage", payload)
            msg = result.get("result") or {}
            mid = msg.get("message_id")
            return int(mid) if mid else None

        if message_id:
            try:
                edit_message_text(token, chat, int(message_id), text)
            except RuntimeError as exc:
                if "not modified" not in str(exc).lower():
                    message_id = _send()
        else:
            message_id = _send()

        _save_state(
            project,
            int(job_id),
            {
                "chat_id": chat,
                "message_id": message_id,
                "headline": headline,
                "step": step,
                "failed": failed,
            },
        )
        print(
            f"PROGRESS: job={job_id} step={step} failed={failed} message_id={message_id}",
            flush=True,
        )
        return int(message_id) if message_id else None
    except Exception as exc:
        print(f"PROGRESS_WARN: job={job_id} {exc}", file=sys.stderr)
        return None
