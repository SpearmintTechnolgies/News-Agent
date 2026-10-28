"""Who may use the bot: super admins, approved users, approved groups.

State lives in ``<state root>/access.json`` (runtime state, never committed). Super admins are the numeric ids in
``NEWSAGENT_TELEGRAM_ADMIN_IDS``; the first one receives access requests. They can never be removed and are the only
ones who can onboard or remove people and groups.

    allows(chat_id, actor_id, admin=False) -> bool
    * super admin   -> always
    * private chat  -> the person must be an approved user
    * group chat    -> the GROUP must be approved (then any member of it may use the bot)
    * admin=True    -> super admin only (site setup, autopilot, onboarding)

This layer is additive: ``TelegramConfig.allows_actor`` (the env allowlists) is still checked first by the caller.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_DAILY_LIMIT = 5      # stories per approved user per day (super admins are unlimited, 0 = no limit)
_LOCK = threading.RLock()
_NOTICES: dict[str, float] = {}
_STATE_PATH: Path | None = None
_ADMIN_IDS: tuple[str, ...] = ()


def configure(state_root: Path | str | None, admin_ids: tuple[str, ...] | list[str]) -> None:
    """Bind the state file and the super admins. Called once when the runtime is built."""
    global _STATE_PATH, _ADMIN_IDS
    _STATE_PATH = Path(state_root) / "access.json" if state_root else None
    _ADMIN_IDS = tuple(str(int(a)) for a in admin_ids if str(a).strip().isdigit())


def enabled() -> bool:
    return bool(_ADMIN_IDS)


def admin_ids() -> tuple[str, ...]:
    return _ADMIN_IDS


def primary_admin() -> str:
    return _ADMIN_IDS[0] if _ADMIN_IDS else ""


def _empty() -> dict[str, Any]:
    return {"users": {}, "groups": {}, "blocked": [], "pending_users": {}, "pending_groups": {}, "awaiting": {},
            "jobs": {}, "settings": {"daily_limit": DEFAULT_DAILY_LIMIT}}


def _load() -> dict[str, Any]:
    base = _empty()
    if _STATE_PATH is None:
        return base
    try:
        data = json.loads(_STATE_PATH.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    base.update({k: v for k, v in data.items() if k in base})
    base["settings"] = {**_empty()["settings"], **(data.get("settings") or {})}
    return base


def _update(fn: Callable[[dict[str, Any]], Any]) -> Any:
    """Read-modify-write under a lock; the file is replaced atomically."""
    with _LOCK:
        data = _load()
        out = fn(data)
        if _STATE_PATH is not None:
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), "utf-8")
            tmp.replace(_STATE_PATH)
        return out


def snapshot() -> dict[str, Any]:
    return _load()


# ------------------------------------------------------------------ who is who
def is_admin(uid: Any) -> bool:
    return bool(uid) and str(uid) in _ADMIN_IDS


def is_user(uid: Any) -> bool:
    return bool(uid) and str(uid) in _load()["users"]


def is_group(chat_id: Any) -> bool:
    return bool(chat_id) and str(chat_id) in _load()["groups"]


def is_blocked(uid: Any) -> bool:
    return bool(uid) and int(uid) in set(_load()["blocked"])


def allows(chat_id: Any, actor_id: Any, *, admin: bool = False) -> bool:
    if not enabled() or not actor_id or not str(actor_id).isdigit():
        return False
    if is_admin(actor_id):
        return True
    if admin or is_blocked(actor_id):
        return False
    if str(chat_id or "").startswith("-"):
        return is_group(chat_id)
    return str(chat_id or "") == str(actor_id) and is_user(actor_id)


# ------------------------------------------------------------------- onboarding
def add_user(uid: Any, by: Any, name: str = "", username: str = "") -> bool:
    """True when newly added. Also clears any pending request and any block."""
    uid = int(uid)

    def fn(d: dict) -> bool:
        new = str(uid) not in d["users"]
        entry = d["users"].setdefault(str(uid), {"added_at": time.time()})
        entry.update({"by": int(by), "name": name or entry.get("name", ""), "username": username or entry.get("username", "")})
        d["pending_users"].pop(str(uid), None)
        d["blocked"] = [b for b in d["blocked"] if b != uid]
        return new

    return _update(fn)


def remove_user(uid: Any) -> bool:
    if is_admin(uid):
        return False                     # a super admin can never be removed
    return _update(lambda d: d["users"].pop(str(int(uid)), None) is not None)


def add_group(chat_id: Any, by: Any, title: str = "") -> bool:
    chat_id = int(chat_id)

    def fn(d: dict) -> bool:
        new = str(chat_id) not in d["groups"]
        old = d["groups"].get(str(chat_id), {"added_at": time.time()})
        d["groups"][str(chat_id)] = {**old, "by": int(by), "title": title or old.get("title", "")}
        d["pending_groups"].pop(str(chat_id), None)
        return new

    return _update(fn)


def remove_group(chat_id: Any) -> bool:
    return _update(lambda d: d["groups"].pop(str(int(chat_id)), None) is not None)


def block(uid: Any) -> None:
    uid = int(uid)
    if is_admin(uid):
        return

    def fn(d: dict) -> None:
        d["pending_users"].pop(str(uid), None)
        if uid not in d["blocked"]:
            d["blocked"].append(uid)

    _update(fn)


def request_user(uid: Any, name: str = "", username: str = "") -> bool:
    """Record an access request. True when it is new (so the admin is only notified once)."""
    uid = int(uid)

    def fn(d: dict) -> bool:
        if str(uid) in d["users"] or uid in d["blocked"]:
            return False
        new = str(uid) not in d["pending_users"]
        d["pending_users"][str(uid)] = {"name": name, "username": username, "at": time.time()}
        return new

    return _update(fn)


def request_group(chat_id: Any, title: str = "", by: Any = None) -> bool:
    chat_id = int(chat_id)

    def fn(d: dict) -> bool:
        if str(chat_id) in d["groups"]:
            return False
        new = str(chat_id) not in d["pending_groups"]
        d["pending_groups"][str(chat_id)] = {"title": title, "by": by, "at": time.time()}
        return new

    return _update(fn)


def deny_group(chat_id: Any) -> None:
    _update(lambda d: d["pending_groups"].pop(str(int(chat_id)), None))


def deny_user(uid: Any) -> None:
    _update(lambda d: d["pending_users"].pop(str(int(uid)), None))


# --------------------------------------------------- admin's "waiting for input"
def set_awaiting(uid: Any, mode: str) -> None:
    _update(lambda d: d["awaiting"].__setitem__(str(uid), {"mode": mode, "at": time.time()}))


def get_awaiting(uid: Any, max_age_s: float = 600) -> str:
    a = _load()["awaiting"].get(str(uid))
    return a["mode"] if a and time.time() - a["at"] < max_age_s else ""


def clear_awaiting(uid: Any) -> None:
    _update(lambda d: d["awaiting"].pop(str(uid), None))


def should_notify(key: str, every_s: float = 600) -> bool:
    """Throttle replies to strangers so they cannot make the bot spam."""
    now = time.time()
    if now - _NOTICES.get(key, 0) < every_s:
        return False
    _NOTICES[key] = now
    return True


def unblock(uid: Any) -> bool:
    uid = int(uid)

    def fn(d: dict) -> bool:
        had = uid in d["blocked"]
        d["blocked"] = [b for b in d["blocked"] if b != uid]
        return had

    return _update(fn)


# ------------------------------------------------------------- daily story limit
def daily_limit() -> int:
    return int(_load()["settings"].get("daily_limit", DEFAULT_DAILY_LIMIT))


def set_daily_limit(n: int) -> None:
    _update(lambda d: d["settings"].__setitem__("daily_limit", max(0, int(n))))


def record_story(uid: Any, event_id: str, chat_id: Any = "") -> None:
    """Remember who started a story (per day, per person) so the limit and /admin can count them."""
    if not uid or not str(uid).isdigit():
        return

    def fn(d: dict) -> None:
        d["jobs"][str(event_id)] = {"user": int(uid), "chat": str(chat_id or ""), "at": time.time()}
        for k in sorted(d["jobs"], key=lambda k: d["jobs"][k]["at"])[:-400]:   # keep the file small
            d["jobs"].pop(k)

    _update(fn)


def stories_today(uid: Any) -> int:
    day = time.strftime("%Y-%m-%d")
    return sum(1 for j in _load()["jobs"].values()
               if int(j.get("user") or 0) == int(uid) and time.strftime("%Y-%m-%d", time.localtime(j["at"])) == day)


def check_quota(uid: Any) -> tuple[bool, str]:
    """Approved users get `daily_limit` stories a day (0 = unlimited). Super admins and unmanaged actors are never limited."""
    if not enabled() or not uid or not str(uid).isdigit() or is_admin(uid):
        return True, ""
    lim = daily_limit()
    used = stories_today(uid)
    if lim and used >= lim:
        return False, f"Daily limit reached ({used}/{lim} stories today). It resets at midnight; ask the admin for more."
    return True, ""


# ------------------------------------------------------------ extra parts the admin switches on
# Beyond the regular menu (choose a site, scan stories, run + review your own stories, author, voice). A user's switches
# apply in their private chat; a group's switches apply to everyone in the group. Removing them drops the switches.
FEATURES = {"publish": "📤 Publish", "autopilot": "🤖 Autopilot", "sites": "🌐 Manage websites", "report": "📈 Report + health"}


def features(uid: Any, chat_id: Any = None, chat_type: str = "private") -> set[str]:
    """The extra parts this chat's menu shows: the group's switches in a group, the user's in a private chat, everything for a super admin."""
    if not uid or (is_blocked(uid) and not is_admin(uid)):
        return set()
    data = _load()
    if chat_type in ("group", "supergroup"):
        rec = data["groups"].get(str(chat_id)) or {}
    elif is_admin(uid):
        return set(FEATURES)
    else:
        rec = data["users"].get(str(uid)) or {}
    return set(rec.get("features") or []) & set(FEATURES)


def can(feature: str, uid: Any, chat_id: Any = None, chat_type: str = "private") -> bool:
    return is_admin(uid) or feature in features(uid, chat_id, chat_type)


def set_feature(kind: str, target: Any, feature: str, on: bool) -> bool:
    """Switch a part on or off for an approved user (kind "user") or group ("group"). False when they are not approved."""
    if feature not in FEATURES:
        return False
    key = "users" if kind == "user" else "groups"

    def fn(d: dict) -> bool:
        rec = d[key].get(str(int(target)))
        if rec is None:
            return False
        have = set(rec.get("features") or []) - {feature}
        rec["features"] = sorted(have | ({feature} if on else set()))
        return True

    return _update(fn)


def chat_type_of(chat_id: Any) -> str:
    return "supergroup" if str(chat_id or "").startswith("-") else "private"


# ------------------------------------------------------------------ story ownership
def story_info(event_id: str) -> dict | None:
    return _load()["jobs"].get(str(event_id))


def can_manage_story(uid: Any, chat_id: Any, event_id: str) -> bool:
    """Super admins manage everything. A person manages the stories they started; a group's members manage the stories
    started in that approved group. Stories nobody recorded (older ones, env-allowlisted operators) are not restricted."""
    if is_admin(uid):
        return True
    info = story_info(event_id)
    if not info or not info.get("user"):
        return True
    if uid and str(info["user"]) == str(uid):
        return True
    return bool(info.get("chat")) and str(info["chat"]) == str(chat_id) and is_group(chat_id)


# ------------------------------------------------------------------ which websites a person / group may use
# A record without a "sites" list may use every website (the default). Once the admin picks websites, only those.
def allowed_sites(uid: Any, chat_id: Any = None, chat_type: str = "private") -> set[str] | None:
    """None = every website; otherwise the set of site ids this chat may use and manage."""
    if is_admin(uid):
        return None
    data = _load()
    rec = (data["groups"].get(str(chat_id)) if chat_type in ("group", "supergroup") else data["users"].get(str(uid))) or {}
    sites = rec.get("sites")
    return None if sites is None else {str(s) for s in sites}


def site_allowed(uid: Any, chat_id: Any, site_id: Any) -> bool:
    allowed = allowed_sites(uid, chat_id, chat_type_of(chat_id))
    return allowed is None or str(site_id) in allowed


def set_sites(kind: str, target: Any, site_ids: list[str] | None) -> bool:
    """Pick the websites for an approved user (kind "user") or group ("group"); None = every website."""
    key = "users" if kind == "user" else "groups"

    def fn(d: dict) -> bool:
        rec = d[key].get(str(int(target)))
        if rec is None:
            return False
        if site_ids is None:
            rec.pop("sites", None)
        else:
            rec["sites"] = sorted({str(s) for s in site_ids})
        return True

    return _update(fn)


def toggle_site(kind: str, target: Any, site_id: str, all_site_ids: list[str]) -> bool:
    """Flip one website for a person / group. The first flip starts from 'every website'."""
    key = "users" if kind == "user" else "groups"
    rec = _load()[key].get(str(int(target)))
    if rec is None:
        return False
    current = set(all_site_ids) if rec.get("sites") is None else {str(s) for s in rec["sites"]}
    current ^= {str(site_id)}
    return set_sites(kind, target, sorted(current))
