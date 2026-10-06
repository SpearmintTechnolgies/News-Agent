"""Websites the bot can publish to.

/start lists these as buttons, plus one button to add another. Credentials live
in data/v5_state, which is not committed. The site already in the environment
is the first entry.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from newsagent_v2.wordpress.authors import DEFAULT_PATH as AUTHOR_PATH
from newsagent_v2.wordpress.config import (
    BASE_ENV,
    PASSWORD_ENV,
    USER_ENV,
    WordPressConfig,
)

DEFAULT_PATH = Path("data/v5_state/sites.json")
ADD_CALLBACK = "site:add"
MENU_TEXT = "Which website should I write for?\n\nTap a site, or add another."
URL_PROMPT = "Send the website address.\nExample: https://example.com\n\n/start cancels."
USERNAME_PROMPT = (
    "Send the WordPress username or the account email for the agent on this website.\n"
    "Username example: editor\n"
    "Email example: name@example.com\n\n"
    "/start cancels."
)
ASK_USERNAME = (
    "Send the WordPress username or the account email for this password.\n"
    "Example: editor or name@example.com\n\n"
    "/start cancels."
)
_ACCOUNT_ALONE = "Send the WordPress username or the account email on its own (no spaces)."
APP_PASSWORD_HELP = (
    "In WordPress: Users → Profile → Application Passwords, "
    "type a name for the password, click 'Add New Application Password', and send the password it shows."
)
PASSWORD_PROMPT = (
    "Send the application password WordPress showed you.\n\n"
    f"{APP_PASSWORD_HELP}\n\n"
    "I will not repeat it back.\n\n"
    "/start cancels."
)
LOGO_PROMPT = (
    "Send the site logo as a photo or PNG.\n"
    "It is placed on every article image for this website.\n\n"
    "/start cancels."
)
WIZARD_PATH = Path("data/v5_state/site_onboarding.json")
LOGO_DIR = Path("data/v5_state/logos")
COIN_NETWORK_LOGO = Path("brand/coinnetwork_logo.png")
FINISH_PREFIX = "site:finish:"
_TWO_FACTOR_MARKERS = (
    "two_factor",
    "two-factor",
    "2fa",
    "otp",
    "one-time",
    "one time",
    "verification code",
    "wfls",
    "itsec",
)

Transport = Callable[..., Any]
_GROUPED_PASSWORD = re.compile(r"(?:[A-Za-z0-9]{4}[ \t]+){5}[A-Za-z0-9]{4}")


def _load_wizard() -> dict[str, dict[str, str]]:
    if not WIZARD_PATH.is_file():
        return {}
    try:
        payload = json.loads(WIZARD_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _flush_wizard() -> None:
    try:
        WIZARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        WIZARD_PATH.write_text(json.dumps(_pending, indent=2), encoding="utf-8")
    except OSError:
        return


_pending: dict[str, dict[str, str]] = _load_wizard()


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    base_url: str
    username: str
    app_password: str
    logo_path: str = ""

    def public(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "base_url": self.base_url}


def site_id_for(base_url: str) -> str:
    return hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:8]


COIN_NETWORK_SITE_ID = site_id_for("https://coinnetwork.info")


def normalize_base_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text or any(char.isspace() for char in text):
        raise ValueError("Send one website address, like https://example.com")
    if "://" not in text:
        text = "https://" + text
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Send one website address, like https://example.com")
    path = parsed.path.rstrip("/")
    for suffix in ("/wp-admin", "/wp-login.php"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return f"{scheme}://{parsed.netloc}{path}"


def _label(base_url: str, name: str = "") -> str:
    title = " ".join(str(name or "").split())
    if title:
        return title[:40]
    host = urlsplit(base_url).hostname or base_url
    return host[:40]


def _read(path: Path | None) -> dict[str, Any]:
    store = path or DEFAULT_PATH
    if not store.is_file():
        return {"active": "", "sites": []}
    try:
        payload = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active": "", "sites": []}
    if not isinstance(payload, dict):
        return {"active": "", "sites": []}
    sites = payload.get("sites")
    if not isinstance(sites, list):
        payload["sites"] = []
    return payload


def _write(payload: dict[str, Any], path: Path | None) -> None:
    store = path or DEFAULT_PATH
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _site_from(row: Any) -> Site | None:
    if not isinstance(row, dict):
        return None
    try:
        base_url = normalize_base_url(str(row.get("base_url") or ""))
    except ValueError:
        return None
    username = str(row.get("username") or "").strip()
    password = str(row.get("app_password") or "").strip()
    if not username or not password:
        return None
    site_id = str(row.get("id") or site_id_for(base_url))
    return Site(
        id=site_id,
        name=_label(base_url, str(row.get("name") or "")),
        base_url=base_url,
        username=username,
        app_password=password,
        logo_path=str(row.get("logo_path") or ""),
    )


def list_sites(path: Path | None = None, environ: dict[str, str] | None = None) -> list[Site]:
    """Saved sites. The environment site is added once when the list is empty."""
    store = path or DEFAULT_PATH
    payload = _read(store)
    sites = [site for row in payload.get("sites") or [] if (site := _site_from(row))]
    if sites:
        return sites
    env = environ or {}
    base = str(env.get(BASE_ENV) or "").strip()
    username = str(env.get(USER_ENV) or "").strip()
    password = str(env.get(PASSWORD_ENV) or "").strip()
    if not base or not username or not password:
        return []
    try:
        base_url = normalize_base_url(base)
    except ValueError:
        return []
    seeded = Site(
        id=site_id_for(base_url),
        name=_label(base_url),
        base_url=base_url,
        username=username,
        app_password=password,
    )
    _write({"active": seeded.id, "sites": [_row(seeded)]}, store)
    _keep_existing_bylines(seeded.id, store)
    return [seeded]


def active_id(path: Path | None = None) -> str:
    payload = _read(path or DEFAULT_PATH)
    return str(payload.get("active") or "")


def _host(base_url: str) -> str:
    return (urlsplit(base_url).hostname or "").lower()


def is_ready(site: Site) -> bool:
    """A site stays off the normal list until its logo is saved. Coin Network already has one."""
    if site.logo_path and Path(site.logo_path).is_file():
        return True
    return _host(site.base_url) == "coinnetwork.info"


def logo_file(site: Site) -> Path | None:
    if site.logo_path and Path(site.logo_path).is_file():
        return Path(site.logo_path)
    if _host(site.base_url) == "coinnetwork.info":
        return COIN_NETWORK_LOGO
    return None


def logo_for_environ(environ: dict[str, str] | None = None) -> Path | None:
    env = environ or {}
    explicit = str(env.get("NEWSAGENT_ACTIVE_LOGO_PATH") or "").strip()
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    site_id = str(env.get("NEWSAGENT_ACTIVE_SITE_ID") or "").strip()
    if not site_id:
        return None
    site = find_site(site_id, environ=env)
    if site is None:
        return None
    return logo_file(site)


def menu_keyboard(sites: list[Site], *, active: str = "") -> dict[str, Any]:
    rows = []
    for site in sites:
        if is_ready(site):
            mark = "✓ " if site.id == active else ""
            rows.append([{
                "text": f"{mark}{site.name}"[:40],
                "callback_data": f"site:{site.id}",
            }])
        else:
            rows.append([{
                "text": f"FINISH SETUP {site.name}"[:40],
                "callback_data": f"{FINISH_PREFIX}{site.id}",
            }])
    rows.append([{"text": "ADD WEBSITE", "callback_data": ADD_CALLBACK}])
    return {"inline_keyboard": rows}


def find_site(site_id: str, path: Path | None = None, environ: dict[str, str] | None = None) -> Site | None:
    for site in list_sites(path, environ):
        if site.id == site_id:
            return site
    return None


def save_site(site: Site, path: Path | None = None) -> list[Site]:
    """Add a site, or replace the one with the same address."""
    store = path or DEFAULT_PATH
    payload = _read(store)
    rows = [row for row in payload.get("sites") or [] if _site_from(row) and _site_from(row).base_url != site.base_url]
    rows.append(_row(site))
    payload["sites"] = rows
    if not payload.get("active"):
        payload["active"] = site.id
    _write(payload, store)
    return [parsed for row in rows if (parsed := _site_from(row))]


def settings_keyboard(site_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "Replace logo", "callback_data": f"site:logo:{site_id}"}],
        [{"text": "Replace username or email", "callback_data": f"site:user:{site_id}"}],
        [{"text": "Replace password", "callback_data": f"site:password:{site_id}"}],
        [{"text": "Remove website", "callback_data": f"site:remove:{site_id}"}],
        [{"text": "Back", "callback_data": f"site:{site_id}"}],
    ]}


def remove_confirm_keyboard(site_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "Remove website", "callback_data": f"site:remove:yes:{site_id}"}],
        [{"text": "Back", "callback_data": f"site:settings:{site_id}"}],
    ]}


def remove_site(site_id: str, path: Path | None = None) -> Site | None:
    """Drop the saved website. Posts already on that site are left alone."""
    store = path or DEFAULT_PATH
    payload = _read(store)
    removed: Site | None = None
    kept: list[Any] = []
    for row in payload.get("sites") or []:
        site = _site_from(row)
        if site and site.id == site_id:
            removed = site
            continue
        kept.append(row)
    if removed is None:
        return None
    payload["sites"] = kept
    if payload.get("active") == site_id:
        nxt = next((site for row in kept if (site := _site_from(row))), None)
        payload["active"] = nxt.id if nxt else ""
    _write(payload, store)
    for candidate in {Path(removed.logo_path), LOGO_DIR / f"{site_id}.png"}:
        if str(candidate) and candidate.is_file():
            candidate.unlink()
    author_file = AUTHOR_PATH.with_name(f"author-{site_id}.json")
    if author_file.is_file():
        author_file.unlink()
    from newsagent_v2.control.site_flow import forget_site
    from newsagent_v2.seo6.sitemap import cache_path_for

    cache = cache_path_for(removed.base_url)
    if cache.is_file():
        cache.unlink()
    forget_site(site_id)
    return removed


def activate(site_id: str, path: Path | None = None, environ: dict[str, str] | None = None) -> Site | None:
    site = find_site(site_id, path, environ)
    if site is None:
        return None
    store = path or DEFAULT_PATH
    payload = _read(store)
    payload["active"] = site.id
    _write(payload, store)
    _keep_existing_bylines(site.id, store)
    if environ is not None:
        environ[BASE_ENV] = site.base_url
        environ[USER_ENV] = site.username
        environ[PASSWORD_ENV] = site.app_password
        environ["NEWSAGENT_ACTIVE_SITE_ID"] = site.id
        logo = logo_file(site)
        environ["NEWSAGENT_ACTIVE_LOGO_PATH"] = str(logo) if logo else ""
    from newsagent_v2.wordpress import authors

    authors.active_site_id = site.id
    return site


def wordpress_config(site: Site) -> WordPressConfig:
    return WordPressConfig(site.base_url, site.username, site.app_password)


def _row(site: Site) -> dict[str, str]:
    return {
        "id": site.id,
        "name": site.name,
        "base_url": site.base_url,
        "username": site.username,
        "app_password": site.app_password,
        "logo_path": site.logo_path,
    }


def _keep_existing_bylines(site_id: str, store: Path) -> None:
    """The original byline file belongs to the first site."""
    if store != DEFAULT_PATH:
        return
    target = AUTHOR_PATH.with_name(f"author-{site_id}.json")
    if AUTHOR_PATH.is_file() and not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(AUTHOR_PATH, target)


def resume_password(chat_id: str, base_url: str, username: str) -> None:
    _pending[str(chat_id)] = {"step": "password", "base_url": base_url, "username": username}
    _flush_wizard()


def resume_username(chat_id: str, base_url: str, app_password: str) -> None:
    _pending[str(chat_id)] = {"step": "username", "base_url": base_url, "app_password": app_password}
    _flush_wizard()


def onboarding_hides_text(chat_id: str) -> bool:
    pending = _pending.get(str(chat_id))
    return bool(pending and pending.get("step") in {"password", "logo"})


def begin_add(chat_id: str) -> str:
    _pending[str(chat_id)] = {"step": "url"}
    _flush_wizard()
    return URL_PROMPT


def cancel_add(chat_id: str) -> None:
    _pending.pop(str(chat_id), None)
    _flush_wizard()


def take_reply(chat_id: str, text: str) -> dict[str, Any] | None:
    """Consume one onboarding reply. None when this chat is not adding a site."""
    key = str(chat_id)
    pending = _pending.get(key)
    if pending is None:
        return None
    raw = str(text or "").strip()
    command = raw.split()[0].split("@", 1)[0].lower() if raw else ""
    if command in {"/start", "/cancel"}:
        _pending.pop(key, None)
        _flush_wizard()
        return {"cancel": True, "text": MENU_TEXT}
    step = pending.get("step")
    if step == "logo":
        return {"cancel": False, "text": LOGO_PROMPT, "awaiting_logo": True}
    if step == "url":
        try:
            pending["base_url"] = normalize_base_url(raw)
        except ValueError as exc:
            return {"cancel": False, "text": str(exc)}
        pending["step"] = "username"
        _flush_wizard()
        return {"cancel": False, "text": USERNAME_PROMPT}
    if step == "username":
        grouped = _GROUPED_PASSWORD.search(raw)
        cleaned = clean_app_password(raw)
        # A long email is an account, not an application password.
        if grouped or ("@" not in raw and len(cleaned) >= 16):
            _pending.pop(key, None)
            _flush_wizard()
            return {
                "cancel": False,
                "ready": True,
                "base_url": pending.get("base_url", ""),
                "username": "",
                "app_password": cleaned,
                "replacing": pending.get("replacing") == "1",
                "site_id": pending.get("site_id", ""),
            }
        username = raw.strip()
        if not username or any(char.isspace() for char in username):
            return {"cancel": False, "text": _ACCOUNT_ALONE}
        pending["username"] = username
        pending["step"] = "password"
        _flush_wizard()
        return {"cancel": False, "text": f"{_account_label(username)}: {username}\n\n{PASSWORD_PROMPT}"}
    if step == "replace_username":
        username = raw.strip()
        if not username or any(char.isspace() for char in username):
            return {"cancel": False, "text": _ACCOUNT_ALONE}
        _pending.pop(key, None)
        _flush_wizard()
        return {
            "cancel": False,
            "ready": True,
            "base_url": pending.get("base_url", ""),
            "username": username,
            "app_password": pending.get("app_password", ""),
            "replacing": True,
            "site_id": pending.get("site_id", ""),
        }
    password = clean_app_password(raw)
    if len(password) < 8:
        return {"cancel": False, "text": "That application password looks too short. Send it again."}
    _pending.pop(key, None)
    _flush_wizard()
    return {
        "cancel": False,
        "ready": True,
        "base_url": pending.get("base_url", ""),
        "username": pending.get("username", ""),
        "app_password": password,
        "replacing": pending.get("replacing") == "1",
        "site_id": pending.get("site_id", ""),
    }


def hold_password(
    chat_id: str,
    base_url: str,
    username: str = "",
    *,
    replacing: bool = False,
    site_id: str = "",
) -> None:
    row = {"step": "password", "base_url": base_url, "username": username}
    if replacing:
        row["replacing"] = "1"
        row["site_id"] = site_id
    _pending[str(chat_id)] = row
    _flush_wizard()


def begin_replace_username(chat_id: str, site: Site) -> str:
    _pending[str(chat_id)] = {
        "step": "replace_username",
        "base_url": site.base_url,
        "app_password": site.app_password,
        "site_id": site.id,
        "replacing": "1",
    }
    _flush_wizard()
    return (
        f"Current account for {site.name} is: {site.username}\n\n"
        "Send the new WordPress username or the account email.\n"
        "Example: editor or name@example.com\n\n"
        "/start cancels."
    )


def begin_replace_password(chat_id: str, site: Site) -> str:
    _pending[str(chat_id)] = {
        "step": "password",
        "base_url": site.base_url,
        "username": "",
        "site_id": site.id,
        "replacing": "1",
    }
    _flush_wizard()
    return PASSWORD_PROMPT


def begin_logo(chat_id: str, site: Site, *, replacing: bool = False, rankmath_warning: str = "") -> str:
    """The site is not listed until this photo is saved."""
    row = {
        "step": "logo",
        "base_url": site.base_url,
        "username": site.username,
        "app_password": site.app_password,
        "name": site.name,
        "site_id": site.id,
    }
    if replacing:
        row["replacing"] = "1"
    if rankmath_warning:
        row["rankmath_warning"] = rankmath_warning
    _pending[str(chat_id)] = row
    _flush_wizard()
    return "Send a new logo as a photo or PNG.\n\n/start cancels." if replacing else LOGO_PROMPT


def logo_step(chat_id: str) -> bool:
    pending = _pending.get(str(chat_id))
    return bool(pending and pending.get("step") == "logo")


def logo_is_replacement(chat_id: str) -> bool:
    pending = _pending.get(str(chat_id)) or {}
    return pending.get("replacing") == "1"


def rankmath_warning_for(chat_id: str) -> str:
    pending = _pending.get(str(chat_id)) or {}
    return str(pending.get("rankmath_warning") or "")


def adopt_saved_logo(site: Site, path: Path | None = None) -> Site:
    current = find_site(site.id, path)
    if current is None or not current.logo_path or not Path(current.logo_path).is_file():
        return site
    return Site(
        id=site.id,
        name=site.name,
        base_url=site.base_url,
        username=site.username,
        app_password=site.app_password,
        logo_path=current.logo_path,
    )


def attach_logo(
    chat_id: str,
    image_bytes: bytes,
    path: Path | None = None,
    directory: Path | None = None,
) -> tuple[Site | None, str]:
    """Save the Telegram photo and list the site."""
    pending = _pending.get(str(chat_id))
    if not pending or pending.get("step") != "logo":
        return None, "Send /start and tap FINISH SETUP."
    import io

    from PIL import Image

    base_url = str(pending.get("base_url") or "")
    site = Site(
        id=str(pending.get("site_id") or site_id_for(base_url)),
        name=str(pending.get("name") or _label(base_url)),
        base_url=base_url,
        username=str(pending.get("username") or ""),
        app_password=str(pending.get("app_password") or ""),
    )
    dest = (directory or LOGO_DIR) / f"{site.id}.png"
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            dest.parent.mkdir(parents=True, exist_ok=True)
            image.save(dest, format="PNG")
    except Exception:
        return None, "That file is not an image. Send the logo as a photo or PNG."
    saved = Site(
        id=site.id,
        name=site.name,
        base_url=site.base_url,
        username=site.username,
        app_password=site.app_password,
        logo_path=str(dest),
    )
    save_site(saved, path)
    _pending.pop(str(chat_id), None)
    _flush_wizard()
    return saved, ""


def confirm_text(site: Site, categories: int, authors: int, posts: int | None = None, rankmath_warning: str = "") -> str:
    indexed = f"Sitemap: {posts} posts.\n" if posts is not None else ""
    seo = f"{rankmath_warning}\n" if rankmath_warning else ""
    return (
        f"Added {site.name}.\n"
        f"Account: {site.username}.\n"
        f"Categories: {categories}. Authors: {authors}.\n"
        f"{indexed}"
        f"{seo}"
        "Logo saved."
    )


def publishing_rights(
    base_url: str,
    username: str,
    app_password: str,
    transport: Transport,
) -> tuple[list[str], str]:
    """Capabilities required to draft, and a warning when Rank Math is absent."""
    try:
        base = normalize_base_url(base_url)
    except ValueError:
        return ["edit_posts", "upload_files"], ""
    me = transport(
        "GET",
        base.rstrip("/") + "/wp-json/wp/v2/users/me?context=edit",
        auth=(username, app_password),
    )
    payload = me.get("payload") if isinstance(me.get("payload"), dict) else {}
    raw_caps = payload.get("capabilities") if me.get("ok") else None
    if isinstance(raw_caps, dict):
        granted = {str(name) for name, allowed in raw_caps.items() if allowed}
    elif isinstance(raw_caps, list):
        granted = {str(name) for name in raw_caps}
    else:
        granted = set()
    missing = [name for name in ("edit_posts", "upload_files") if name not in granted]
    home = transport("GET", base.rstrip("/") + "/wp-json/")
    namespaces = (home.get("payload") or {}).get("namespaces") if isinstance(home.get("payload"), dict) else []
    warning = ""
    if not isinstance(namespaces, list) or "rankmath/v1" not in namespaces:
        warning = "SEO fields will not be written because Rank Math is not on this site."
    return missing, warning


def rights_message(missing: list[str]) -> str:
    labels = {"edit_posts": "edit posts", "upload_files": "upload files"}
    named = [labels.get(item, item) for item in missing]
    return (
        "This account cannot " + " or ".join(named) + ".\n"
        "Create the application password for a user who can edit posts and upload files, then send that password."
    )


def clean_app_password(raw: str) -> str:
    """WordPress shows the password in groups of four. A pasted name on the same line is left out."""
    text = str(raw or "").replace("\u00a0", " ")
    grouped = _GROUPED_PASSWORD.search(text)
    if grouped:
        return "".join(char for char in grouped.group(0) if char.isalnum())
    return "".join(char for char in text if char.isalnum())


def _auth_refused(response: dict[str, Any]) -> str:
    """A login password fails when 2FA is on. An application password is the credential that works."""
    payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
    code = str(payload.get("code") or "").lower()
    message = str(payload.get("message") or "").lower()
    blob = f"{code} {message}"
    if "application_password" in code and "disabled" in code:
        return (
            "This site has turned application passwords off.\n\n"
            "In the security or 2FA plugin, allow Application Passwords. "
            "Then create one under Users → Profile → Application Passwords and send it here."
        )
    if any(marker in blob for marker in _TWO_FACTOR_MARKERS):
        return f"This site uses 2FA, so that login was refused.\n\n{APP_PASSWORD_HELP}"
    return f"WordPress did not accept that username and password.\n\n{APP_PASSWORD_HELP}"


def _account_label(login: str) -> str:
    return "Email" if "@" in login else "Username"


def _login_refused_for_good(message: str) -> bool:
    return message.startswith("This site uses 2FA") or message.startswith(
        "This site has turned application passwords off"
    )


def open_site(
    base_url: str,
    username: str,
    app_password: str,
    transport: Transport,
    *,
    discover: bool = True,
) -> tuple[Site | None, str]:
    """Log in with a WordPress username or an account email.

    Application passwords accept the WordPress login. When that login is an
    email, the address itself works. Otherwise the mailbox name is tried, and
    while adding a site the public user list is searched for the account that
    owns the password.
    """
    login = username.strip()
    site, message = probe_site(base_url, login, app_password, transport)
    if site is not None or "@" not in login or _login_refused_for_good(message):
        return site, message
    if message.startswith("That address did not"):
        return None, message
    local = login.split("@", 1)[0].strip()
    if local and local.casefold() != login.casefold():
        site, _local_message = probe_site(base_url, local, app_password, transport)
        if site is not None:
            return site, f"Added {site.name}."
    if not discover:
        return None, message
    discovered, discover_message = match_site(base_url, app_password, transport)
    if discovered is not None:
        return discovered, discover_message
    if discover_message == ASK_USERNAME:
        return None, message
    return None, discover_message


def probe_site(
    base_url: str,
    username: str,
    app_password: str,
    transport: Transport,
) -> tuple[Site | None, str]:
    """Confirm the address is WordPress and the application password is accepted."""
    try:
        base = normalize_base_url(base_url)
    except ValueError as exc:
        return None, str(exc)
    home = transport("GET", base.rstrip("/") + "/wp-json/")
    if not home.get("ok") or not isinstance(home.get("payload"), dict):
        return None, "That address did not answer as a WordPress site."
    me = transport(
        "GET",
        base.rstrip("/") + "/wp-json/wp/v2/users/me?context=edit",
        auth=(username, app_password),
    )
    if not me.get("ok"):
        return None, _auth_refused(me)
    name = _label(base, str((home.get("payload") or {}).get("name") or ""))
    site = Site(
        id=site_id_for(base),
        name=name,
        base_url=base,
        username=username.strip(),
        app_password=app_password,
    )
    return site, f"Added {site.name}."


def _login_candidates(users: list[Any]) -> list[str]:
    """Logins worth trying. Email-style display names are the usual application-password owner."""
    emails: list[str] = []
    others: list[str] = []
    for row in users:
        if not isinstance(row, dict):
            continue
        name = " ".join(str(row.get("name") or "").split())
        slug = " ".join(str(row.get("slug") or "").split())
        if "@" in name:
            emails.append(name)
        if slug:
            others.append(slug)
            compact = slug.replace("-", "")
            if compact != slug:
                others.append(compact)
        if name and "@" not in name:
            others.append(name)
    found: list[str] = []
    seen: set[str] = set()
    for login in emails + others:
        key = login.casefold()
        if not login or key in seen:
            continue
        seen.add(key)
        found.append(login)
        if len(found) >= 40:
            break
    return found


def match_site(
    base_url: str,
    app_password: str,
    transport: Transport,
) -> tuple[Site | None, str]:
    """Find which account owns this application password. The password name is not a login."""
    try:
        base = normalize_base_url(base_url)
    except ValueError as exc:
        return None, str(exc)
    home = transport("GET", base.rstrip("/") + "/wp-json/")
    if not home.get("ok") or not isinstance(home.get("payload"), dict):
        return None, "That address did not answer as a WordPress site."
    listed = transport("GET", base.rstrip("/") + "/wp-json/wp/v2/users?per_page=100")
    users = listed.get("payload") if listed.get("ok") and isinstance(listed.get("payload"), list) else []
    for login in _login_candidates(users):
        me = transport(
            "GET",
            base.rstrip("/") + "/wp-json/wp/v2/users/me?context=edit",
            auth=(login, app_password),
        )
        if me.get("ok"):
            name = _label(base, str((home.get("payload") or {}).get("name") or ""))
            site = Site(
                id=site_id_for(base),
                name=name,
                base_url=base,
                username=login,
                app_password=app_password,
            )
            return site, f"Added {site.name}."
    return None, ASK_USERNAME
