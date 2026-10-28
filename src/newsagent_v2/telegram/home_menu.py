"""The /start home screen, shaped by who is looking.

    stranger        a lock screen with their ID and "Request access" (see access_menu.deny_unauthorized)
    approved user   the website list + their stories today, my ID, help; plus whatever the admin switched on for them
    super admin     all of the above + the admin panel (badge for waiting requests), today's report, health check

Callback data: hm:<action>  (id, my, r, hc, h)
"""

from __future__ import annotations

import html
import os
import time
from typing import Any

from newsagent_v2.telegram import access

STARTED_AT = time.time()
# Buttons of the website menu that need an extra switch (callback data prefix -> feature)
GATED_BUTTONS = {"idle:": "autopilot", "site:add": "sites"}


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def header(uid: Any, chat_id: Any) -> str:
    """The status lines above the website list."""
    ctype = access.chat_type_of(chat_id)
    snap = access.snapshot()
    lim = access.daily_limit()
    if access.is_admin(uid):
        n_req = len(snap["pending_users"]) + len(snap["pending_groups"])
        return ("👑 <b>Super admin</b>\n"
                f"📅 Stories started today: {sum(access.stories_today(u) for u in snap['users'])} (approved users)\n"
                f"👥 {len(snap['users'])} users · 💬 {len(snap['groups'])} groups" + (f" · 🙋 {n_req} waiting" if n_req else ""))
    if not uid:
        return ""
    who = "group" if ctype != "private" else "user"
    return f"👤 <b>Approved {who}</b>\n📅 Your stories today: {access.stories_today(uid)}" + (f" / {lim}" if lim else "")


def site_of_button(data: str) -> str:
    """The website id a menu button points at (empty for buttons that are not about one website)."""
    parts = data.split(":")
    if parts[0] == "site" and len(parts) == 2 and parts[1] != "add":
        return parts[1]
    if data.startswith(("site:finish:", "site:settings:")) and len(parts) > 2:
        return parts[2]
    return ""


def decorate(markup: dict, uid: Any, chat_id: Any) -> dict:
    """Hide the buttons this chat has no switch for, and add the role's own rows."""
    ctype = access.chat_type_of(chat_id)
    rows = []
    for row in markup.get("inline_keyboard", []):
        keep = [b for b in row if not any(str(b.get("callback_data", "")).startswith(prefix) and not access.can(feat, uid, chat_id, ctype)
                                          for prefix, feat in GATED_BUTTONS.items())
                and access.site_allowed(uid, chat_id, site_of_button(str(b.get("callback_data", ""))))]
        if keep:
            rows.append(keep)
    admin = access.is_admin(uid)
    if admin and ctype == "private":
        snap = access.snapshot()
        n_req = len(snap["pending_users"]) + len(snap["pending_groups"])
        rows.append([_btn(f"🛠 ACCESS CONTROL{f' ({n_req})' if n_req else ''}", "ac:m")])
    if access.can("report", uid, chat_id, ctype):
        rows.append([_btn("📈 Today's report", "hm:r"), _btn("🩺 Health check", "hm:hc")])
    if uid:
        rows.append([_btn("📊 My stories", "hm:my"), _btn("🆔 My ID", "hm:id"), _btn("🆘 Help", "hm:h")])
    return {"inline_keyboard": rows}


def report_text() -> str:
    from newsagent_v2.control.sites import is_ready, list_sites

    snap = access.snapshot()
    lines = [f"📈 <b>TODAY</b> · {time.strftime('%d %b %Y')}", ""]
    total = 0
    for uid, u in snap["users"].items():
        n = access.stories_today(uid)
        total += n
        if n:
            lines.append(f"• {_esc(u.get('name') or u.get('username') or uid)}: {n} story(ies)")
    lines.insert(2, f"Stories started by approved users: {total}")
    lim = access.daily_limit()
    lines += ["", f"⏱ Daily limit per user: {lim if lim else 'none'}",
              f"👥 {len(snap['users'])} users · 💬 {len(snap['groups'])} groups · 🚫 {len(snap['blocked'])} blocked"]
    return "\n".join(lines)


def health_text() -> str:
    from newsagent_v2.control.sites import is_ready, list_sites

    sites = list_sites()
    ready = [s for s in sites if is_ready(s)]
    publish_on = str(os.environ.get("NEWSAGENT_PUBLISH_ENABLED", "")).lower() in {"1", "true", "yes", "on"}
    mins = int((time.time() - STARTED_AT) // 60)
    lines = ["🩺 <b>HEALTH</b>", "",
             f"🟢 Bot running for {mins // 60}h {mins % 60}m",
             f"🌐 Websites ready: {len(ready)}/{len(sites)}" + "".join(f"\n   • {_esc(s.name)}{'' if is_ready(s) else ' (setup unfinished)'}" for s in sites),
             f"📤 Publishing: {'enabled' if publish_on else 'disabled (drafts only)'}"]
    return "\n".join(lines)


def my_stories_text(uid: Any) -> str:
    lim = access.daily_limit()
    if access.is_admin(uid):
        return "📊 <b>MY STORIES</b>\n\nYou are the super admin: no daily limit."
    used = access.stories_today(uid)
    left = f"{max(lim - used, 0)} left today" if lim else "no daily limit"
    return f"📊 <b>MY STORIES</b>\n\nStarted today: {used}" + (f" of {lim}" if lim else "") + f"\n{left}. The count resets at midnight."


def handle_callback(client, cq: dict, help_text: str) -> bool:
    """hm:* buttons. True when handled."""
    data = str(cq.get("data") or "")
    if not data.startswith("hm:"):
        return False
    action = data[3:]
    uid = (cq.get("from") or {}).get("id")
    msg = cq.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    ctype = (msg.get("chat") or {}).get("type") or access.chat_type_of(chat_id)

    def answer(text: str = "") -> None:
        try:
            client.answer_callback_query(callback_query_id=str(cq.get("id") or ""), text=text or None)
        except Exception:  # noqa: BLE001
            pass

    def send(text: str) -> None:
        client.send_message(chat_id=chat_id, text=text[:4000])

    if action == "id":                    # anyone may look up their own ID (it is how strangers get onboarded)
        answer()
        send(f"🆔 Your Telegram ID: <code>{uid}</code>\nThis chat's ID: <code>{chat_id}</code>")
        return True
    if not access.allows(chat_id, uid):
        answer("🔒 Not approved to use this bot")
        return True
    if action in {"r", "hc"} and not access.can("report", uid, chat_id, ctype):
        answer("Ask the admin to switch this on")
        return True
    answer()
    if action == "r":
        send(report_text())
    elif action == "hc":
        send(health_text())
    elif action == "my":
        send(my_stories_text(uid))
    elif action == "h":
        send(html.escape(help_text))
    return True
