"""Telegram onboarding UI for access control (super admins only).

    /admin      the control panel: users, groups, requests, add
    /myid       anyone: shows their Telegram ID (needed to be onboarded)
    /adduser <id> [name]    /removeuser <id>     /allowgroup (inside a group, or with a group id)    /removegroup

Strangers who message the bot get their ID and a "Request access" button; the super admin gets a card with
Approve / Deny / Block. Adding the bot to a group sends the admin the same kind of card (or approves it at once if
the admin added it). Callback data: ac:<action>[:<id>] (Telegram allows 64 bytes).
"""

from __future__ import annotations

import html
import re
import time
from typing import Any

from newsagent_v2.telegram import access

PREFIX = "ac:"
ADMIN_COMMANDS = frozenset({"/admin", "/adduser", "/removeuser", "/allowgroup", "/removegroup", "/limit"})
ACCESS_COMMANDS = ADMIN_COMMANDS | {"/myid"}

Rows = list[list[tuple[str, str]]]

# One unbroken run of token-ish characters. A pasted access token (or any key) looks like this and nothing else does.
_SECRET_SHAPE = re.compile(r"[A-Za-z0-9_\-\.|:/+=~]{20,}")


def looks_like_secret(text: str) -> bool:
    """A long message with no spaces: treat it as a pasted token. Never echoed, never logged."""
    t = (text or "").strip()
    return bool(_SECRET_SHAPE.fullmatch(t)) and not t.startswith("/")


def _kb(rows: Rows | None) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in (rows or [])]}


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def name_of(u: dict) -> str:
    full = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
    return full or (f"@{u['username']}" if u.get("username") else str(u.get("id", "?")))


def _ago(ts: float) -> str:
    return time.strftime("%d %b", time.localtime(ts)) if ts else "?"


def _send(client, chat_id: Any, text: str, rows: Rows | None = None, markup: dict | None = None) -> None:
    try:
        client.send_message(chat_id=str(chat_id), text=text[:4000], reply_markup=markup if markup is not None else (_kb(rows) if rows else None))
    except Exception:  # noqa: BLE001  (the person may never have opened the bot)
        pass


def _edit(client, chat_id: Any, mid: int, text: str, rows: Rows | None = None) -> None:
    if mid:
        client.edit_message_text(chat_id=str(chat_id), message_id=mid, text=text[:4000], reply_markup=_kb(rows or []))
    else:
        _send(client, chat_id, text, rows)


def _answer(client, cq_id: str, text: str = "") -> None:
    try:
        client.answer_callback_query(callback_query_id=cq_id, text=text or None)
    except Exception:  # noqa: BLE001
        pass


def _group_info(client, chat_id: int) -> dict:
    try:
        resp = client._post("getChat", json_body={"chat_id": chat_id})
        return ((resp.get("payload") or {}).get("result") or {}) if resp.get("ok") else {}
    except Exception:  # noqa: BLE001
        return {}


def command_list(admin: bool) -> list[dict]:
    """What Telegram shows in the blue 'Menu' button next to the text box."""
    base = [("start", "Websites and story menu"), ("author", "Choose the byline"), ("voice", "Choose the narration voice"),
            ("myid", "Show your Telegram ID"), ("help", "All commands")]
    extra = [("admin", "Access control panel"), ("autopilot", "Idle auto-publish settings"), ("adduser", "Approve a user"),
             ("removeuser", "Remove a user"), ("allowgroup", "Approve this group"), ("removegroup", "Remove this group"), ("limit", "Daily story limit")]
    return [{"command": c, "description": d} for c, d in (base + (extra if admin else []))]


def register_commands(client) -> None:
    """Public list for everyone; the full list in each super admin's own chat."""
    try:
        client._post("setMyCommands", json_body={"commands": command_list(False)})
        for admin_id in access.admin_ids():
            client._post("setMyCommands", json_body={"commands": command_list(True), "scope": {"type": "chat", "chat_id": int(admin_id)}})
    except Exception:  # noqa: BLE001
        pass


def with_admin_row(markup: dict, chat_id: Any) -> dict:
    """Add the access-control button to a menu shown to a super admin."""
    if access.is_admin(chat_id) and str(chat_id).isdigit():
        markup = {"inline_keyboard": [*markup.get("inline_keyboard", []), [{"text": "🛠 ACCESS CONTROL", "callback_data": "ac:m"}]]}
    return markup


# ------------------------------------------------------------------- screens
def main_menu() -> tuple[str, Rows]:
    s = access.snapshot()
    n_req = len(s["pending_users"]) + len(s["pending_groups"])
    text = (f"🛠 <b>ACCESS CONTROL</b>\n\n👥 Approved users: {len(s['users'])}\n💬 Approved groups: {len(s['groups'])}\n"
            f"🙋 Waiting for approval: {n_req}\n🚫 Blocked: {len(s['blocked'])}\n"
            f"⏱ Daily limit per user: {access.daily_limit() or 'none'} stories\n\n"
            "You (the super admin) can always use the bot and are the only one who can change this.")
    return text, [[("👥 Users", "ac:u"), ("💬 Groups", "ac:g")],
                  [(f"🙋 Requests ({n_req})", "ac:p")],
                  [("➕ Add user", "ac:au"), ("➕ Add group", "ac:ag")],
                  [("⏱ Daily limit", "ac:l"), (f"🚫 Blocked ({len(s['blocked'])})", "ac:b")],
                  [("✖ Close", "ac:x")]]


def _extras(rec: dict) -> str:
    on = [access.FEATURES[f].split(" ", 1)[0] for f in access.FEATURES if f in (rec.get("features") or [])]
    return f" · 🔐 {' '.join(on)}" if on else ""


FEATURE_CODES = {"p": "publish", "a": "autopilot", "s": "sites", "r": "report"}
FEATURE_HELP = {"publish": "approve and publish {whose} to WordPress, and unpublish",
                "autopilot": "change Autopilot (idle auto-publish) settings",
                "sites": "add, edit and remove websites, logos and logins",
                "report": "see today's stories and the health check"}


def features_screen(kind: str, target: str) -> tuple[str, Rows]:
    """The extra menu parts for one approved user (kind "u") or group (kind "g"), each a switch."""
    group = kind == "g"
    back = [("⬅ Back", "ac:g" if group else "ac:u")]
    rec = access.snapshot()["groups" if group else "users"].get(str(target))
    if rec is None:
        return "Not approved any more.", [back]
    name = rec.get("title") if group else (rec.get("name") or (f"@{rec['username']}" if rec.get("username") else ""))
    on = set(rec.get("features") or [])
    whose = "stories started in the group" if group else "their own stories"
    lines = [f"🔐 <b>MENU FOR {_esc(name or target)}</b>", "",
             ("Everyone in this group" if group else "They") + " always get the regular menu: choose a website, scan stories, run "
             "and review their stories (revise, edit, reject, listen), author and voice. Tap to switch on more.", ""]
    rows = []
    for code, f in FEATURE_CODES.items():
        lines.append(f"{'✅' if f in on else '⬜'} {access.FEATURES[f]}: {FEATURE_HELP[f].format(whose=whose)}")
        rows.append([(f"{'✅' if f in on else '⬜'} {access.FEATURES[f]}", f"ac:t{kind}:{target}:{code}")])
    sel = rec.get("sites")
    lines += ["", "🌐 Websites: " + ("all websites" if sel is None else f"{len(sel)} selected")]
    rows.append([("🌐 Choose websites", f"ac:wl:{kind}:{target}")])
    if group:
        lines += ["", "⚠️ Everyone in the group gets these."]
    return "\n".join(lines), rows + [back]


def websites_screen(kind: str, target: str) -> tuple[str, Rows]:
    """Which websites one approved user (kind "u") or group (kind "g") may use and manage."""
    from newsagent_v2.control.sites import list_sites

    group = kind == "g"
    rec = access.snapshot()["groups" if group else "users"].get(str(target))
    back = [("⬅ Back", f"ac:f{kind}:{target}")]
    if rec is None:
        return "Not approved any more.", [back]
    sites = list_sites()
    sel = rec.get("sites")
    on = {s.id for s in sites} if sel is None else {str(x) for x in sel}
    name = rec.get("title") if group else (rec.get("name") or target)
    NL = chr(10)
    lines = [f"🌐 <b>WEBSITES FOR {_esc(name or target)}</b>", "",
             "They can open, scan and write for the ticked websites only" + (" (everyone in the group)." if group else "."),
             "Untick a website to hide it from their menu and block its buttons.", ""]
    rows = []
    for site in sites:
        lines.append(f"{chr(9989) if site.id in on else chr(11036)} {_esc(site.name)}")
        rows.append([(f"{chr(9989) if site.id in on else chr(11036)} {site.name}"[:40], f"ac:ws:{kind}:{target}:{site.id}")])
    rows.append([("♻️ All websites", f"ac:wa:{kind}:{target}")])
    return NL.join(lines), rows + [back]


def users_screen() -> tuple[str, Rows]:
    users = access.snapshot()["users"]
    if not users:
        return "👥 No approved users yet.\n\nTap ➕ Add user, or share the bot and approve their requests.", [[("➕ Add user", "ac:au"), ("⬅ Back", "ac:m")]]
    lines, rows = ["👥 <b>APPROVED USERS</b>", ""], []
    for uid, u in sorted(users.items(), key=lambda kv: kv[1].get("added_at", 0)):
        label = u.get("name") or (f"@{u['username']}" if u.get("username") else uid)
        lines.append(f"• {_esc(label)} · {uid} · {access.stories_today(uid)} story(ies) today · added {_ago(u.get('added_at', 0))}")
        rows.append([("🔐 Menu", f"ac:fu:{uid}"), (f"❌ Remove {label[:24]}", f"ac:ru:{uid}")])
        lines[-1] += _extras(u)
    return "\n".join(lines), rows[:30] + [[("⬅ Back", "ac:m")]]


def groups_screen() -> tuple[str, Rows]:
    groups = access.snapshot()["groups"]
    if not groups:
        return ("💬 No approved groups yet.\n\nAdd the bot to a group (it is approved at once when you add it), or send "
                "/allowgroup inside the group, or approve the request the bot sends you."), [[("➕ Add group", "ac:ag"), ("⬅ Back", "ac:m")]]
    lines, rows = ["💬 <b>APPROVED GROUPS</b>", ""], []
    for cid, g in groups.items():
        title = g.get("title") or cid
        lines.append(f"• {_esc(title)} · {cid} · added {_ago(g.get('added_at', 0))}")
        rows.append([("🔐 Menu", f"ac:fg:{cid}"), (f"❌ Remove {title[:24]}", f"ac:rg:{cid}")])
        lines[-1] += _extras(g)
    return "\n".join(lines), rows[:30] + [[("⬅ Back", "ac:m")]]


def pending_screen() -> tuple[str, Rows]:
    s = access.snapshot()
    if not (s["pending_users"] or s["pending_groups"]):
        return "🙋 No one is waiting for approval.", [[("⬅ Back", "ac:m")]]
    lines, rows = ["🙋 <b>WAITING FOR APPROVAL</b>", ""], []
    for uid, p in s["pending_users"].items():
        who = p.get("name") or uid
        lines.append(f"• person: {_esc(who)}" + (f" (@{_esc(p['username'])})" if p.get("username") else "") + f" · {uid}")
        rows.append([(f"✅ {who[:18]}", f"ac:ok:{uid}"), ("❌ Deny", f"ac:no:{uid}"), ("🚫 Block", f"ac:bl:{uid}")])
    for cid, p in s["pending_groups"].items():
        title = p.get("title") or cid
        lines.append(f"• group: {_esc(title)} · {cid}")
        rows.append([(f"✅ {title[:18]}", f"ac:gok:{cid}"), ("❌ Ignore", f"ac:gno:{cid}")])
    return "\n".join(lines), rows[:30] + [[("⬅ Back", "ac:m")]]


LIMITS = (3, 5, 10, 20, 0)   # 0 = unlimited


def limit_screen() -> tuple[str, Rows]:
    cur = access.daily_limit()
    text = (f"⏱ <b>DAILY LIMIT</b>\n\nHow many stories can each approved user start per day? Now: {cur if cur else 'unlimited'}.\n"
            "It protects your API spend. Super admins are never limited.")
    return text, [[(("• " if n == cur else "") + (str(n) if n else "No limit"), f"ac:lim:{n}") for n in LIMITS], [("⬅ Back", "ac:m")]]


def blocked_screen() -> tuple[str, Rows]:
    blocked = access.snapshot()["blocked"]
    if not blocked:
        return "🚫 Nobody is blocked.", [[("⬅ Back", "ac:m")]]
    text = "🚫 <b>BLOCKED</b>\n\n" + "\n".join(f"• {b}" for b in blocked)
    return text, [[(f"♻️ Unblock {b}", f"ac:ub:{b}")] for b in blocked[:30]] + [[("⬅ Back", "ac:m")]]


# ------------------------------------------------------------------- commands
def command_of(update: dict, bot_username: str = "") -> tuple[str, str]:
    """(command, args) of a message update, '' when it is not a command for this bot."""
    text = str((update.get("message") or {}).get("text") or "").strip()
    if not text.startswith("/"):
        return "", ""
    token = text.split()[0]
    cmd, _, target = token.partition("@")
    if target and bot_username and target.lower() != bot_username.lstrip("@").lower():
        return "", ""
    return cmd.lower(), text[len(token):].strip()


def handle_command(client, cmd: str, args: str, msg: dict) -> None:
    """/myid for anyone; the rest only after the caller has checked the sender is a super admin."""
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id"))
    sender = msg.get("from") or {}
    if cmd == "/myid":
        _send(client, chat_id, f"🆔 Your Telegram ID: <code>{sender.get('id')}</code>\nThis chat's ID: <code>{chat_id}</code>")
        return
    if cmd == "/admin":
        text, rows = main_menu()
        _send(client, chat_id, text, rows)
    elif cmd == "/adduser":
        parts = (args or "").split(maxsplit=1)
        if not parts or not parts[0].isdigit():
            _send(client, chat_id, "Usage: /adduser &lt;telegram id&gt; [name]\nOr open /admin and tap ➕ Add user to pick or forward someone.")
            return
        _add_user(client, int(parts[0]), parts[1] if len(parts) > 1 else "", "", chat_id)
    elif cmd == "/removeuser":
        arg = (args or "").strip()
        if not arg.isdigit():
            _send(client, chat_id, "Usage: /removeuser &lt;telegram id&gt;")
        elif access.remove_user(int(arg)):
            _send(client, chat_id, f"✅ Removed {arg}. They can no longer use the bot.")
            _send(client, arg, "Your access to the news agent was removed.")
        else:
            _send(client, chat_id, "That ID isn't an approved user (a super admin can't be removed).")
    elif cmd == "/allowgroup":
        target = (args or "").strip()
        if chat.get("type") in ("group", "supergroup"):
            access.add_group(chat["id"], sender.get("id"), chat.get("title", ""))
            _send(client, chat_id, f"✅ This group is approved: {_esc(chat.get('title', ''))}. Everyone in it can now use the agent.")
        elif target.lstrip("-").isdigit():
            _add_group(client, int(target), chat_id, sender.get("id"))
        else:
            _send(client, chat_id, "Send /allowgroup inside the group (after adding me to it), or /allowgroup &lt;group id&gt;.")
    elif cmd == "/limit":
        arg = (args or "").strip()
        if arg.isdigit():
            access.set_daily_limit(int(arg))
            _send(client, chat_id, f"✅ Daily limit set to {int(arg) or 'unlimited'} story(ies) per approved user.")
        else:
            text, rows = limit_screen()
            _send(client, chat_id, text, rows)
    elif cmd == "/removegroup":
        target = (args or "").strip()
        gid = int(chat["id"]) if chat.get("type") in ("group", "supergroup") else (int(target) if target.lstrip("-").isdigit() else None)
        if gid is not None and access.remove_group(gid):
            _send(client, chat_id, "✅ Group removed. The bot will ignore it from now on.")
        else:
            _send(client, chat_id, "That group isn't approved.")


def _add_user(client, uid: int, name: str, username: str, reply_chat: str) -> None:
    new = access.add_user(uid, access.primary_admin(), name, username)
    _send(client, reply_chat, f"✅ {'Added' if new else 'Already approved:'} {_esc(name or uid)} ({uid}). They can now message the bot.")
    if new:
        _send(client, uid, "✅ You've been approved to use the news agent. Send /start to begin.")


def _add_group(client, chat_id: int, reply_chat: str, by: Any) -> None:
    info = _group_info(client, chat_id)
    title = info.get("title", "")
    access.add_group(chat_id, by or access.primary_admin(), title)
    warn = "" if info else "\n(I couldn't look the group up, so make sure I've been added to it.)"
    _send(client, reply_chat, f"✅ Group approved: {_esc(title or chat_id)} ({chat_id}).{warn}")


# ----------------------------------------------- admin's free-form onboarding input
def handle_admin_input(client, msg: dict) -> bool:
    """While an admin is in an 'add user / add group' step, accept an ID, a forwarded message or a picked user."""
    sender = msg.get("from") or {}
    uid = sender.get("id")
    if not access.is_admin(uid) or (msg.get("chat") or {}).get("type") != "private":
        return False
    mode = access.get_awaiting(uid)
    if not mode:
        return False
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if text.lower() in {"cancel", "/cancel"}:
        access.clear_awaiting(uid)
        _send(client, chat_id, "Cancelled.", markup={"remove_keyboard": True})
        return True
    if mode == "user":
        found: list[tuple[int, str, str]] = []
        shared = (msg.get("users_shared") or {}).get("users") or ([msg["user_shared"]] if msg.get("user_shared") else [])
        for u in shared:
            found.append((int(u["user_id"]), " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x), u.get("username", "")))
        origin = msg.get("forward_origin") or {}
        fwd = origin.get("sender_user") or msg.get("forward_from")
        if fwd:
            found.append((int(fwd["id"]), name_of(fwd), fwd.get("username", "")))
        elif origin.get("type") == "hidden_user" or (msg.get("forward_sender_name") and not msg.get("forward_from")):
            _send(client, chat_id, "That person hides their account when forwarded, so I can't read their ID. Ask them to send me /myid and paste the number here.")
            return True
        if text.isdigit():
            found.append((int(text), "", ""))
        if not found:
            return False
        access.clear_awaiting(uid)
        for new_uid, name, username in found:
            _add_user(client, new_uid, name, username, chat_id)
        _send(client, chat_id, "Done.", markup={"remove_keyboard": True})
        return True
    if mode == "group" and text.lstrip("-").isdigit():
        access.clear_awaiting(uid)
        _add_group(client, int(text), chat_id, uid)
        return True
    return False


# ----------------------------------------------------------------- strangers
def deny_unauthorized(client, msg: dict) -> None:
    """Politely tell a stranger how to get in, throttled so nobody can make the bot spam."""
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_id, uid = str(chat.get("id")), sender.get("id")
    if not uid or access.is_blocked(uid):
        return
    if chat.get("type") in ("group", "supergroup"):
        if access.should_notify(f"g:{chat_id}", 3600):
            _send(client, chat_id, "🔒 This group isn't approved to use the news agent yet. I've asked the admin.")
            if access.request_group(chat["id"], chat.get("title", ""), uid):
                _notify_admin_group(client, chat["id"], chat.get("title", ""), name_of(sender))
        return
    if not access.should_notify(f"u:{uid}", 600):
        return
    _send(client, chat_id, f"🔒 This bot is private.\n\nYour Telegram ID: <code>{uid}</code>\nTap below to ask the admin for access.",
          [[("🙋 Request access", "ac:req")]])


def _notify_admin_group(client, chat_id: Any, title: str, by: str) -> None:
    _send(client, access.primary_admin(), f"💬 <b>GROUP REQUEST</b>\n{_esc(title or chat_id)} ({chat_id})\nAdded / used by: {_esc(by)}",
          [[("✅ Allow group", f"ac:gok:{chat_id}"), ("❌ Ignore", f"ac:gno:{chat_id}")]])


def handle_my_chat_member(client, upd: dict) -> None:
    """The bot was added to / removed from a chat."""
    chat = upd.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup"):
        return
    status = (upd.get("new_chat_member") or {}).get("status")
    by = upd.get("from") or {}
    cid = str(chat["id"])
    if status in ("member", "administrator"):
        if access.is_admin(by.get("id")):
            access.add_group(chat["id"], by["id"], chat.get("title", ""))
            _send(client, cid, "✅ Hello! The admin approved this group. Send /start to begin.")
            _send(client, access.primary_admin(), f"✅ Group approved automatically (you added me): {_esc(chat.get('title', ''))}")
        elif not access.is_group(chat["id"]):
            if access.request_group(chat["id"], chat.get("title", ""), by.get("id")):
                _notify_admin_group(client, chat["id"], chat.get("title", ""), name_of(by))
            _send(client, cid, "🔒 I'm private. I've asked the admin to approve this group.")
    elif status in ("left", "kicked"):
        if access.remove_group(chat["id"]):
            _send(client, access.primary_admin(), f"ℹ️ I was removed from {_esc(chat.get('title', cid))}; it is no longer approved.")
        access.deny_group(chat["id"])


# ------------------------------------------------------------------ callbacks
def handle_callback(client, cq: dict) -> str:
    data = str(cq.get("data") or "")
    cq_id = str(cq.get("id") or "")
    sender = cq.get("from") or {}
    msg = cq.get("message") or {}
    chat_id, mid = str((msg.get("chat") or {}).get("id") or ""), int(msg.get("message_id") or 0)
    parts = data.split(":")
    action, arg = parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""

    if action == "req":                         # anyone can ask; only an admin can approve
        if access.is_blocked(sender.get("id")):
            _answer(client, cq_id)
            return "blocked"
        _answer(client, cq_id, "Request sent")
        if access.request_user(sender["id"], name_of(sender), sender.get("username", "")):
            _send(client, access.primary_admin(),
                  f"🙋 <b>ACCESS REQUEST</b>\n{_esc(name_of(sender))}" + (f" (@{_esc(sender['username'])})" if sender.get("username") else "") + f"\nID {sender['id']}",
                  [[("✅ Approve", f"ac:ok:{sender['id']}"), ("❌ Deny", f"ac:no:{sender['id']}"), ("🚫 Block", f"ac:bl:{sender['id']}")]])
        _edit(client, chat_id, mid, "🙋 Request sent to the admin. You'll get a message here when you're approved.")
        return "requested"

    if not access.is_admin(sender.get("id")):
        _answer(client, cq_id, "Admin only")
        return "not admin"

    def show(screen) -> None:
        text, rows = screen()
        _edit(client, chat_id, mid, text, rows)

    if action == "x":
        _answer(client, cq_id, "Closed")
        _edit(client, chat_id, mid, "Closed. Send /admin to open the panel again.")
    elif action in {"m", "u", "g", "p", "l", "b"}:
        _answer(client, cq_id)
        show({"m": main_menu, "u": users_screen, "g": groups_screen, "p": pending_screen, "l": limit_screen, "b": blocked_screen}[action])
    elif action == "lim":
        access.set_daily_limit(int(arg))
        _answer(client, cq_id, "Saved")
        show(limit_screen)
    elif action == "ub":
        _answer(client, cq_id, "Unblocked" if access.unblock(int(arg)) else "Not blocked")
        show(blocked_screen)
    elif action == "au":
        _answer(client, cq_id)
        access.set_awaiting(sender["id"], "user")
        markup = {"keyboard": [[{"text": "👤 Pick a person", "request_users": {"request_id": 1, "user_is_bot": False, "max_quantity": 10}}],
                               [{"text": "Cancel"}]], "resize_keyboard": True, "one_time_keyboard": True}
        _send(client, chat_id, "➕ <b>ADD A USER</b>\n\nDo any ONE of these:\n• tap 👤 Pick a person (below)\n• send their numeric Telegram ID\n"
                               "• forward me any message from them\n\nThey can find their ID by sending /myid to this bot.", markup=markup)
    elif action == "ag":
        _answer(client, cq_id)
        access.set_awaiting(sender["id"], "group")
        _send(client, chat_id, "➕ <b>ADD A GROUP</b>\n\n1. Add this bot to the group.\n2. In the group, send /allowgroup (only you can).\n\n"
                               "Or send me the group's ID here (it starts with -100). If you add the bot yourself, I approve it automatically.")
    elif action in {"fu", "fg"}:
        _answer(client, cq_id)
        text, rows = features_screen(action[1], arg)
        _edit(client, chat_id, mid, text, rows)
    elif action in {"wl", "ws", "wa"}:
        from newsagent_v2.control.sites import list_sites

        kind, target = (parts[2] if len(parts) > 2 else ""), (parts[3] if len(parts) > 3 else "")
        word = "group" if kind == "g" else "user"
        if action == "ws" and len(parts) > 4:
            ok = access.toggle_site(word, int(target), parts[4], [x.id for x in list_sites()])
            _answer(client, cq_id, "Saved" if ok else "Not approved any more")
        elif action == "wa":
            access.set_sites(word, int(target), None)
            _answer(client, cq_id, "All websites")
        else:
            _answer(client, cq_id)
        text, rows = websites_screen(kind, target)
        _edit(client, chat_id, mid, text, rows)
    elif action in {"tu", "tg"}:
        code = parts[3] if len(parts) > 3 else ""
        feature = FEATURE_CODES.get(code, "")
        rec = access.snapshot()["groups" if action == "tg" else "users"].get(arg) or {}
        turn_on = feature not in (rec.get("features") or [])
        ok = bool(feature) and access.set_feature("group" if action == "tg" else "user", int(arg), feature, turn_on)
        _answer(client, cq_id, ("On" if turn_on else "Off") if ok else "Not approved any more")
        text, rows = features_screen(action[1], arg)
        _edit(client, chat_id, mid, text, rows)
    elif action == "ru":
        _answer(client, cq_id)
        _send(client, chat_id, f"Remove user {_esc(arg)}? They will lose access immediately.", [[("✅ Yes, remove", f"ac:ruy:{arg}"), ("↩ Keep", "ac:u")]])
    elif action == "ruy":
        ok = access.remove_user(int(arg))
        _answer(client, cq_id, "Removed" if ok else "Not found")
        if ok:
            _send(client, arg, "Your access to the news agent was removed.")
        show(users_screen)
    elif action == "rg":
        _answer(client, cq_id)
        _send(client, chat_id, f"Remove group {_esc(arg)}? The bot will ignore it.", [[("✅ Yes, remove", f"ac:rgy:{arg}"), ("↩ Keep", "ac:g")]])
    elif action == "rgy":
        _answer(client, cq_id, "Removed" if access.remove_group(int(arg)) else "Not found")
        show(groups_screen)
    elif action == "ok":
        pending = access.snapshot()["pending_users"].get(arg) or {}
        new = access.add_user(int(arg), access.primary_admin(), pending.get("name", ""), pending.get("username", ""))
        _answer(client, cq_id, "Approved")
        if new:
            _send(client, arg, "✅ You've been approved to use the news agent. Send /start to begin.")
        _edit(client, chat_id, mid, f"✅ Approved {_esc(arg)}")
    elif action == "no":
        access.deny_user(int(arg))
        _answer(client, cq_id, "Denied")
        _send(client, arg, "Your access request was not approved.")
        _edit(client, chat_id, mid, f"❌ Denied {_esc(arg)}")
    elif action == "bl":
        access.block(int(arg))
        _answer(client, cq_id, "Blocked")
        _edit(client, chat_id, mid, f"🚫 Blocked {_esc(arg)}. The bot will ignore them.")
    elif action == "gok":
        title = (access.snapshot()["pending_groups"].get(arg) or {}).get("title", "")
        access.add_group(int(arg), access.primary_admin(), title)
        _answer(client, cq_id, "Group approved")
        _send(client, arg, "✅ This group is approved. Send /start to begin.")
        _edit(client, chat_id, mid, f"✅ Group approved: {_esc(title or arg)}")
    elif action == "gno":
        access.deny_group(int(arg))
        _answer(client, cq_id, "Ignored")
        _edit(client, chat_id, mid, f"❌ Ignored group {_esc(arg)}")
    else:
        _answer(client, cq_id, "Unknown")
    return action
