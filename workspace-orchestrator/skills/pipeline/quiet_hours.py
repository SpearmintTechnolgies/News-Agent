#!/usr/bin/env python3
"""quiet_hours.py — Shared IST quiet-window gate for pool_scheduler and plugins.

Env (same as pool_scheduler):
  FEED_QUIET_START_HOUR (default 0)
  FEED_QUIET_END_HOUR   (default 6)
  FEED_QUIET_TIMEZONE   (default Asia/Kolkata)

CLI:
  --check                 exit 0 if inside quiet hours, 1 otherwise; prints true/false
  --label                 print quiet window label (e.g. 00:00-06:00 IST)
  --send-snooze --chat-id ID [--reply-to-message-id ID]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

QUIET_START_HOUR = int(os.environ.get("FEED_QUIET_START_HOUR", "0"))
QUIET_END_HOUR = int(os.environ.get("FEED_QUIET_END_HOUR", "6"))
QUIET_TZ_NAME = os.environ.get("FEED_QUIET_TIMEZONE", "Asia/Kolkata")


def _resolve_tz() -> ZoneInfo:
    try:
        return ZoneInfo(QUIET_TZ_NAME)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def resolve_tz() -> ZoneInfo:
    """Public alias for scheduler/adjust_feed_fire_time."""
    return _resolve_tz()


def now_quiet_tz() -> datetime:
    return datetime.now(tz=_resolve_tz())


def in_quiet_hours(dt: datetime | None = None) -> bool:
    """True during the configured quiet window (default 00:00–05:59 IST)."""
    dt = dt or now_quiet_tz()
    tz = _resolve_tz()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    h = dt.hour
    start, end = QUIET_START_HOUR, QUIET_END_HOUR
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


def quiet_end_from(dt: datetime) -> datetime:
    """Next quiet-window end at or after dt."""
    tz = _resolve_tz()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    end = QUIET_END_HOUR % 24
    candidate = dt.replace(hour=end, minute=0, second=0, microsecond=0)
    if dt >= candidate:
        candidate += timedelta(days=1)
    return candidate


def quiet_hours_label() -> str:
    return f"{QUIET_START_HOUR:02d}:00-{QUIET_END_HOUR:02d}:00 IST"


def quiet_end_label() -> str:
    return f"{QUIET_END_HOUR:02d}:00 IST"


def snooze_message() -> str:
    return (
        f"News Agent is snoozed until {quiet_end_label()} "
        f"(quiet hours {quiet_hours_label()}). "
        "Automatic and manual runs resume after quiet hours."
    )


def send_snooze_reply(chat_id: str, reply_to_message_id: str | None = None) -> int:
    """Send a Telegram snooze notice. Returns 0 on success."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from build_and_send_card import load_bot_token  # noqa: E402
    from telegram_api import telegram_request  # noqa: E402

    token = load_bot_token()
    if not token:
        print("SNOOZE_REPLY_FAILED: no bot token", file=sys.stderr)
        return 1
    payload: dict[str, str] = {
        "chat_id": str(chat_id),
        "text": snooze_message(),
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = str(reply_to_message_id)
    telegram_request(token, "sendMessage", data=payload)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Shared quiet-hours gate")
    p.add_argument("--check", action="store_true", help="Print true/false; exit 0 if quiet")
    p.add_argument("--label", action="store_true", help="Print quiet hours label")
    p.add_argument("--send-snooze", action="store_true", help="Send snooze Telegram reply")
    p.add_argument("--chat-id", default="")
    p.add_argument("--reply-to-message-id", default="")
    args = p.parse_args()

    if args.label:
        print(quiet_hours_label())
        return 0

    if args.check:
        quiet = in_quiet_hours()
        print("true" if quiet else "false")
        return 0 if quiet else 1

    if args.send_snooze:
        if not args.chat_id.strip():
            print("SNOOZE_REPLY_FAILED: --chat-id required", file=sys.stderr)
            return 1
        rid = args.reply_to_message_id.strip() or None
        return send_snooze_reply(args.chat_id.strip(), rid)

    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
