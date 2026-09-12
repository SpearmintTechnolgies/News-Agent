#!/usr/bin/env python3
"""
onboard_project.py — Deterministic, LLM-free engine that adds a complete
news-agent project end to end (no phases): identity, WordPress verification,
category/author curation, Telegram wiring, and gateway rebind.

Two front-ends share this single engine:
  - Terminal wizard:        onboard_project.py wizard
  - Telegram /onboard flow: project-onboarder plugin calls `start` / `step`

Always fail-open on the Telegram side (never raises to the caller uncaught);
prints OK:/ERROR: lines for logging. Never wakes an LLM.

Subcommands:
    wizard                          Interactive terminal run through all steps
    start   --chat-id --user-id     Begin (or resume) a Telegram onboarding session
    step    --chat-id --user-id --input <text or callback>
                                     Advance one step in a Telegram session
    status  --chat-id [--user-id]   Show current step + answers for a session
    cancel  --chat-id --user-id     Abort and clear a session (+ orphaned .pass file)
    finalize --answers <file.json> [--apply-bind] [--dry-run-bind]
                                     Non-interactive full write from collected answers
    verify  --slug <slug> [--pipeline]
                                     Run post-setup verification checks
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db as db  # noqa: E402
import project_config as pc  # noqa: E402
from wp_rest_client import verify_auth, fetch_categories, fetch_users, WpAuthError, WpRequestError  # noqa: E402
from telegram_api import telegram_request  # noqa: E402

OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
PROJECTS_DIR = os.path.expanduser("~/.openclaw/projects")
CREDS_DIR = os.path.expanduser("~/.openclaw/credentials/wp")
ASSETS_DIR = os.path.expanduser("~/.openclaw/assets")
TEMPLATE_PATH = os.path.join(PROJECTS_DIR, "_template.json")
PRESETS_DIR = os.path.join(PROJECTS_DIR, "presets")
BIND_SCRIPT = os.path.join(HERE, "bind_telegram_group.py")
SYNC_SCRIPT = os.path.join(HERE, "sync_openclaw_from_projects.py")
VALIDATE_SCRIPT = os.path.join(HERE, "validate_project_config.py")

DRIVE_SHARED_HINT = (
    "\n\n<i>Google Drive folder and account are shared for all projects on this machine. "
    "Reply <b>skip</b> or <b>-</b> to use the same setup as your existing sites.</i>"
)

SUPERGROUP_RE = re.compile(r"^-100\d+$")
SLUG_RE = re.compile(r"^[a-z0-9-]{2,32}$")
CALLBACK_CODE_RE = re.compile(r"^[a-z0-9]{2,6}$")

PRESET_CHOICES = ["general-crypto", "memecoin"]
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_LOGO_BYTES = 2 * 1024 * 1024
LOGO_MEDIA_INPUT = "__MEDIA__"

STEP_ORDER = [
    "group_id",
    "slug",
    "callback_code",
    "name",
    "description",
    "wp_url",
    "wp_user",
    "wp_password",
    "picker_slugs",
    "fallback_category",
    "authors",
    "group_name",
    "card_prefix",
    "logo_watermark",
    "research_preset",
    "drive_doc_prefix",
    "drive_parent_id",
    "drive_account",
    "preview",
]

INTRO_TEXT = (
    "\U0001F4CB <b>New project onboarding</b>\n\n"
    "Before we start, do this in Telegram:\n"
    "1. Create a new Telegram group for the site\n"
    "2. Convert it to a <b>supergroup</b> (add 15+ members, or Group Settings → set a public link, "
    "or send a media file — any of these auto-upgrades it)\n"
    "3. Add <b>this bot</b> to the group as <b>admin</b>\n"
    "4. Forward any message from that group to <b>@userinfobot</b> — it replies with the group's "
    "id, formatted like <code>-100XXXXXXXXXX</code>\n\n"
    "Reply to this message with that <code>-100…</code> group id to continue. "
    "For every free-text answer in this flow, use Telegram's <b>Reply</b> on the bot's message "
    "(required in project groups). Send /cancel anytime to abort."
)


class OnboardError(Exception):
    """User-facing validation error — re-prompt the same step."""


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _load_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        tmp = f.name
    os.replace(tmp, path)


def load_bot_token(account_id: str = "news") -> str:
    openclaw = _load_json(OPENCLAW_JSON, {})
    telegram = (openclaw.get("channels") or {}).get("telegram") or {}
    account = (telegram.get("accounts") or {}).get(account_id) or {}
    return str(account.get("botToken") or "").strip()


def send_telegram_message(
    chat_id: str, text: str, *, keyboard: dict | None = None
) -> int | None:
    token = load_bot_token()
    if not token:
        print("ERROR: no Telegram bot token configured for account 'news'")
        return None
    data: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    try:
        result = telegram_request(token, "sendMessage", data=data)
    except RuntimeError as e:
        print(f"ERROR: sendMessage failed: {e}")
        return None
    return (result.get("result") or {}).get("message_id")


def delete_telegram_message(chat_id: str, message_id: int) -> None:
    token = load_bot_token()
    if not token or not message_id:
        return
    try:
        telegram_request(token, "deleteMessage", data={"chat_id": chat_id, "message_id": message_id})
    except RuntimeError:
        pass  # best-effort — not fatal if Telegram won't let us delete (e.g. >48h old)


def confirm_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "\u2705 Confirm & create project", "callback_data": "ob_confirm:yes"},
                {"text": "\u274C Cancel", "callback_data": "ob_confirm:no"},
            ]
        ]
    }


def preset_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "General Crypto", "callback_data": "ob_preset:general-crypto"}],
            [{"text": "Memecoin", "callback_data": "ob_preset:memecoin"}],
        ]
    }


def load_preset(name: str) -> dict:
    path = os.path.join(PRESETS_DIR, f"{name}.json")
    data = _load_json(path)
    if not data:
        raise OnboardError(f"unknown preset '{name}'")
    return data


def existing_slugs() -> dict[str, dict]:
    out = {}
    for slug in pc.list_available_projects():
        try:
            out[slug] = dict(pc.load_project_config(slug=slug))
        except (FileNotFoundError, ValueError):
            continue
    return out


def parse_index_selection(raw: str, max_index: int) -> list[int]:
    """Parse '1,3,5' or '1-3,5' style selections into 0-based indices."""
    out: set[int] = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        m = re.match(r"^(\d+)-(\d+)$", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            for n in range(lo, hi + 1):
                out.add(n)
        elif part.isdigit():
            out.add(int(part))
        else:
            raise OnboardError(f"'{part}' is not a number or range")
    if not out:
        raise OnboardError("no selection provided")
    bad = [n for n in out if n < 1 or n > max_index]
    if bad:
        raise OnboardError(f"selection out of range (1-{max_index}): {bad}")
    return sorted(n - 1 for n in out)


def validate_png_file(path: str) -> None:
    if not os.path.isfile(path):
        raise OnboardError(f"Logo file not found: {path}")
    size = os.path.getsize(path)
    if size > MAX_LOGO_BYTES:
        raise OnboardError(
            f"Logo file too large ({size} bytes, max {MAX_LOGO_BYTES}). "
            "Use a PNG under 2 MB."
        )
    with open(path, "rb") as f:
        header = f.read(8)
    if header != PNG_MAGIC:
        raise OnboardError(
            "Logo must be a PNG file (transparent background recommended). "
            "On Telegram, send it as File (paperclip → File), not a compressed photo."
        )


def save_project_logo(slug: str, source_path: str) -> str:
    validate_png_file(source_path)
    os.makedirs(ASSETS_DIR, exist_ok=True)
    dest = os.path.join(ASSETS_DIR, f"logo-{slug}.png")
    with tempfile.NamedTemporaryFile("wb", dir=ASSETS_DIR, delete=False, suffix=".tmp") as f:
        with open(source_path, "rb") as src:
            shutil.copyfileobj(src, f)
        tmp = f.name
    os.replace(tmp, dest)
    return f"assets/logo-{slug}.png"


def _cleanup_orphan_logo(answers: dict) -> None:
    slug = answers.get("slug")
    if not slug or not answers.get("logo_saved"):
        return
    project_path = os.path.join(PROJECTS_DIR, f"{slug}.json")
    logo_path = os.path.join(ASSETS_DIR, f"logo-{slug}.png")
    if os.path.exists(logo_path) and not os.path.exists(project_path):
        try:
            os.remove(logo_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# per-step: validate input -> mutate answers dict -> return next prompt info
# Each handler raises OnboardError to re-prompt the SAME step with a message.
# ---------------------------------------------------------------------------

def h_group_id(answers: dict, raw: str) -> None:
    gid = raw.strip()
    if not SUPERGROUP_RE.match(gid):
        raise OnboardError(
            "That doesn't look like a supergroup id (expected -100XXXXXXXXXX). "
            "Forward a group message to @userinfobot and paste the id it gives you."
        )
    existing = pc.resolve_project_for_chat(gid)
    if existing:
        raise OnboardError(f"Group {gid} is already bound to project '{existing}'. Use a different group.")
    answers["group_id"] = gid


def h_slug(answers: dict, raw: str) -> None:
    slug = raw.strip().lower()
    if not SLUG_RE.match(slug):
        raise OnboardError("Slug must be lowercase letters/digits/hyphens, 2-32 chars (e.g. 'coinnetwork').")
    if slug in existing_slugs():
        raise OnboardError(f"Slug '{slug}' already exists. Choose a different one.")
    answers["slug"] = slug


def h_callback_code(answers: dict, raw: str) -> None:
    code = raw.strip().lower()
    if not CALLBACK_CODE_RE.match(code):
        raise OnboardError("Callback code must be 2-6 lowercase letters/digits (e.g. 'cnw').")
    dup = [s for s, c in existing_slugs().items() if c.get("callback_code") == code]
    if dup:
        raise OnboardError(f"Callback code '{code}' already used by: {dup}. Choose a different one.")
    answers["callback_code"] = code


def h_name(answers: dict, raw: str) -> None:
    name = raw.strip()
    if not name or len(name) > 60:
        raise OnboardError("Name must be 1-60 characters.")
    answers["name"] = name


def h_description(answers: dict, raw: str) -> None:
    desc = raw.strip()
    if not desc:
        raise OnboardError("Description cannot be empty.")
    answers["description"] = desc


def h_wp_url(answers: dict, raw: str) -> None:
    url = raw.strip().rstrip("/")
    if not re.match(r"^https?://[^\s]+$", url):
        raise OnboardError("Enter a valid URL, e.g. https://example.com")
    answers["wp_url"] = url


def h_wp_user(answers: dict, raw: str) -> None:
    user = raw.strip()
    if not user:
        raise OnboardError("WordPress username/email cannot be empty.")
    answers["wp_user"] = user


def h_wp_password(answers: dict, raw: str) -> None:
    password = raw.strip()
    if not password:
        raise OnboardError("WordPress application password cannot be empty.")
    url, user = answers["wp_url"], answers["wp_user"]
    try:
        me = verify_auth(url, user, password)
    except WpAuthError as e:
        raise OnboardError(f"WordPress auth failed: {e}. Re-enter the application password (not your login password).")
    except WpRequestError as e:
        raise OnboardError(f"Could not reach WordPress: {e}. Re-check the URL/user and try again.")

    slug = answers["slug"]
    os.makedirs(CREDS_DIR, exist_ok=True)
    pass_path = os.path.join(CREDS_DIR, f"{slug}.pass")
    with tempfile.NamedTemporaryFile("w", dir=CREDS_DIR, delete=False, suffix=".tmp") as f:
        f.write(password)
        tmp = f.name
    os.chmod(tmp, 0o600)
    os.replace(tmp, pass_path)
    answers["wp_password_written"] = True
    answers["wp_verified_as"] = me.get("name", user)

    try:
        categories = fetch_categories(url, user, password)
        users = fetch_users(url, user, password)
    except (WpAuthError, WpRequestError) as e:
        raise OnboardError(f"Auth OK but fetching categories/users failed: {e}. Try again.")
    if not categories:
        raise OnboardError("WordPress returned zero categories — cannot continue.")
    if not users:
        raise OnboardError("WordPress returned zero users — cannot continue (need at least one author).")
    answers["_categories"] = categories
    answers["_users"] = users


def h_picker_slugs(answers: dict, raw: str) -> None:
    categories = answers["_categories"]
    idxs = parse_index_selection(raw, len(categories))
    slugs = [categories[i]["slug"] for i in idxs]
    answers["picker_category_slugs"] = slugs


def h_fallback_category(answers: dict, raw: str) -> None:
    categories = answers["_categories"]
    idxs = parse_index_selection(raw, len(categories))
    if len(idxs) != 1:
        raise OnboardError("Pick exactly one number for the fallback category.")
    answers["fallback_category_id"] = categories[idxs[0]]["id"]


def h_authors(answers: dict, raw: str) -> None:
    users = answers["_users"]
    idxs = parse_index_selection(raw, len(users))
    answers["authors"] = [{"id": users[i]["id"], "label": users[i]["name"], "name": users[i]["name"]} for i in idxs]


def h_group_name(answers: dict, raw: str) -> None:
    name = raw.strip()
    if not name:
        raise OnboardError("Group name label cannot be empty.")
    answers["group_name"] = name


def h_card_prefix(answers: dict, raw: str) -> None:
    prefix = raw.strip()
    if not prefix or prefix == "-":
        prefix = f"[{answers.get('name', answers['slug'])}]"
    elif not prefix.startswith("["):
        prefix = f"[{prefix}]"
    answers["card_prefix"] = prefix


def h_logo_watermark(answers: dict, raw: str, *, media_path: str | None = None) -> None:
    slug = answers.get("slug")
    if not slug:
        raise OnboardError("Internal error: slug missing before logo step.")

    if raw.strip() == LOGO_MEDIA_INPUT:
        if not media_path:
            raise OnboardError(
                "Could not find your uploaded image. Reply to the bot's message with a PNG "
                "sent as File (paperclip → File)."
            )
        source = os.path.expanduser(media_path)
    else:
        source = os.path.expanduser(raw.strip())
        if not source:
            raise OnboardError("Provide the path to your PNG logo file.")

    rel_path = save_project_logo(slug, source)
    answers["logo_path"] = rel_path
    answers["logo_saved"] = True


def h_research_preset(answers: dict, raw: str) -> None:
    choice = raw.strip().lower().replace("ob_preset:", "")
    if choice not in PRESET_CHOICES:
        raise OnboardError(f"Choose one of: {', '.join(PRESET_CHOICES)}")
    answers["research_preset"] = choice


def h_drive_doc_prefix(answers: dict, raw: str) -> None:
    val = raw.strip()
    if not val or val == "-":
        val = f"{answers.get('name', answers['slug'])} News"
    answers["drive_doc_prefix"] = val


def h_drive_parent_id(answers: dict, raw: str) -> None:
    val = raw.strip()
    answers["drive_parent_id"] = "" if val.lower() in ("skip", "none", "-") else val


def h_drive_account(answers: dict, raw: str) -> None:
    val = raw.strip()
    answers["drive_account"] = "" if val.lower() in ("skip", "none", "-") else val


def h_preview(answers: dict, raw: str) -> None:
    choice = raw.strip().lower().replace("ob_confirm:", "")
    if choice not in ("yes", "no"):
        raise OnboardError("Reply with the Confirm or Cancel button.")
    if choice == "no":
        raise OnboardError("__CANCELLED__")
    answers["confirmed"] = True


STEP_HANDLERS: dict[str, Callable[[dict, str], None]] = {
    "group_id": h_group_id,
    "slug": h_slug,
    "callback_code": h_callback_code,
    "name": h_name,
    "description": h_description,
    "wp_url": h_wp_url,
    "wp_user": h_wp_user,
    "wp_password": h_wp_password,
    "picker_slugs": h_picker_slugs,
    "fallback_category": h_fallback_category,
    "authors": h_authors,
    "group_name": h_group_name,
    "card_prefix": h_card_prefix,
    "logo_watermark": h_logo_watermark,
    "research_preset": h_research_preset,
    "drive_doc_prefix": h_drive_doc_prefix,
    "drive_parent_id": h_drive_parent_id,
    "drive_account": h_drive_account,
    "preview": h_preview,
}


# ---------------------------------------------------------------------------
# per-step prompt builders (text [+ keyboard]) — used for both Telegram and CLI
# ---------------------------------------------------------------------------

def prompt_for(step: str, answers: dict) -> tuple[str, dict | None]:
    if step == "group_id":
        return INTRO_TEXT, None
    if step == "slug":
        return "What's the project <b>slug</b>? (lowercase, e.g. 'coinnetwork')", None
    if step == "callback_code":
        return "What's a short <b>callback code</b> (2-6 chars, e.g. 'cnw')?", None
    if step == "name":
        return "What's the project's <b>display name</b>? (e.g. 'Coinnetwork')", None
    if step == "description":
        return "One-line <b>description</b> of the niche?", None
    if step == "wp_url":
        return "WordPress site <b>URL</b>? (e.g. https://example.com)", None
    if step == "wp_user":
        return "WordPress <b>username/email</b> for the app password?", None
    if step == "wp_password":
        return (
            "WordPress <b>application password</b> (not your login password). "
            "This message will be deleted immediately after we verify it.",
            None,
        )
    if step == "picker_slugs":
        cats = answers["_categories"]
        lines = [f"{i+1}. {c['slug']} — {c['name']} ({c['count']})" for i, c in enumerate(cats)]
        text = (
            "Verified as <b>" + answers.get("wp_verified_as", "?") + "</b>. Fetched "
            f"{len(cats)} live categories:\n\n" + "\n".join(lines) +
            "\n\nReply with the numbers the <b>Picker</b> may use as PRIMARY category "
            "(comma/range, e.g. '2,5,9-12')."
        )
        return text, None
    if step == "fallback_category":
        cats = answers["_categories"]
        lines = [f"{i+1}. {c['slug']} — {c['name']} ({c['count']})" for i, c in enumerate(cats)]
        text = "Which ONE is the <b>fallback</b> category (used when no pick applies)?\n\n" + "\n".join(lines)
        return text, None
    if step == "authors":
        users = answers["_users"]
        lines = [f"{i+1}. {u['name']} ({u['slug']}) — roles={','.join(u['roles']) or '?'}" for i, u in enumerate(users)]
        text = "Reply with the numbers for the publish-<b>author</b> picker (comma/range):\n\n" + "\n".join(lines)
        return text, None
    if step == "group_name":
        return "A short label for this Telegram group (for logs), e.g. 'coinnetwork_news-agent'?", None
    if step == "card_prefix":
        return "Card prefix shown on every Telegram card, e.g. '[Coinnetwork]'? (or send '-' for a default)", None
    if step == "logo_watermark":
        return (
            "Upload your project <b>watermark logo</b> (transparent PNG, max 2 MB). "
            "It is stamped on every feature image before publish.\n\n"
            "<b>Telegram:</b> reply to this message and send the PNG as <b>File</b> "
            "(paperclip → File). Photo uploads are rejected unless they are PNG.\n\n"
            "<b>Terminal:</b> paste the local path to your .png file."
        ), None
    if step == "research_preset":
        return "Pick a niche preset for RSS feeds / voice / image style:", preset_keyboard()
    if step == "drive_doc_prefix":
        return (
            "Google Drive doc prefix for uploaded .docx files "
            f"(e.g. '{answers.get('name', 'Site')} News').\n"
            "Reply <b>-</b> to use the default name-based prefix.",
            None,
        )
    if step == "drive_parent_id":
        return (
            "Google Drive parent folder ID?" + DRIVE_SHARED_HINT,
            None,
        )
    if step == "drive_account":
        return (
            "Google Drive account email for uploads?" + DRIVE_SHARED_HINT,
            None,
        )
    if step == "preview":
        return build_preview_text(answers), confirm_keyboard()
    raise OnboardError(f"unknown step '{step}'")


def build_preview_text(answers: dict) -> str:
    picker_slugs = answers.get("picker_category_slugs") or []
    authors = answers.get("authors") or []
    lines = [
        "\U0001F4CB <b>Review before creating the project</b>",
        f"Slug: <code>{answers['slug']}</code> | Callback: <code>{answers['callback_code']}</code>",
        f"Name: {answers['name']}",
        f"Description: {answers['description']}",
        f"WordPress: {answers['wp_url']} (user: {answers['wp_user']})",
        f"Fallback category id: {answers.get('fallback_category_id')}",
        f"Picker slugs ({len(picker_slugs)}): {', '.join(picker_slugs) or '(none)'}",
        f"Authors ({len(authors)}): {', '.join(a['name'] for a in authors) or '(none)'}",
        f"Telegram group: {answers['group_id']} ({answers.get('group_name', '')}) prefix {answers.get('card_prefix')}",
        f"Logo: {answers.get('logo_path', '(missing)')}"
        + (" (file OK)" if answers.get("logo_path") and os.path.isfile(
            os.path.join(os.path.expanduser("~/.openclaw"), answers["logo_path"])
        ) else " (file missing!)"),
        f"Research preset: {answers.get('research_preset')}",
        f"Drive: {answers.get('drive_doc_prefix')} / parent={answers.get('drive_parent_id') or '(none)'} / acct={answers.get('drive_account') or '(none)'}",
        "",
        "Confirm to write the project, sync Telegram wiring from projects/*.json, and restart the gateway.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# finalize: build project dict, write artifacts, bind, verify
# ---------------------------------------------------------------------------

def shared_drive_defaults() -> dict[str, str]:
    """Inherit shared Drive folder + account from the first existing project."""
    for slug in pc.list_available_projects():
        try:
            cfg = pc.load_project_config(slug=slug)
        except (FileNotFoundError, ValueError):
            continue
        pub = cfg.get("publisher") or {}
        parent = str(pub.get("drive_parent_id") or "").strip()
        account = str(pub.get("drive_account") or "").strip()
        if parent or account:
            return {"drive_parent_id": parent, "drive_account": account}
    gog = os.environ.get("GOG_ACCOUNT", "").strip()
    return {"drive_parent_id": "", "drive_account": gog}


def build_project_dict(answers: dict) -> dict:
    template = _load_json(TEMPLATE_PATH, {})
    preset = load_preset(answers.get("research_preset", "general-crypto"))
    slug = answers["slug"]

    cfg = json.loads(json.dumps(template))  # deep copy
    cfg["slug"] = slug
    cfg["callback_code"] = answers["callback_code"]
    cfg["name"] = answers["name"]
    cfg["description"] = answers["description"]

    cfg["wordpress"]["url"] = answers["wp_url"]
    cfg["wordpress"]["user"] = answers["wp_user"]
    cfg["wordpress"]["app_password_ref"] = f"credentials/wp/{slug}.pass"
    cfg["wordpress"]["fallback_category_id"] = answers["fallback_category_id"]
    cfg["wordpress"]["picker_category_slugs"] = answers.get("picker_category_slugs") or []
    cfg["wordpress"]["categories"] = answers["_categories"]

    cfg["telegram"]["account"] = "news"
    cfg["telegram"]["group_id"] = answers["group_id"]
    cfg["telegram"]["group_name"] = answers.get("group_name", slug)
    cfg["telegram"]["card_prefix"] = answers.get("card_prefix") or f"[{answers['name']}]"

    for key in ("research", "writer", "creator", "picker"):
        if key in preset:
            cfg[key] = {**cfg.get(key, {}), **preset[key]}

    logo_rel = answers.get("logo_path") or f"assets/logo-{slug}.png"
    cfg.setdefault("creator", {})
    cfg["creator"]["logo_path"] = logo_rel

    cfg["publisher"]["drive_doc_prefix"] = answers.get("drive_doc_prefix") or f"{answers['name']} News"
    shared = shared_drive_defaults()
    parent = str(answers.get("drive_parent_id") or "").strip()
    account = str(answers.get("drive_account") or "").strip()
    cfg["publisher"]["drive_parent_id"] = parent or shared.get("drive_parent_id") or ""
    cfg["publisher"]["drive_account"] = account or shared.get("drive_account") or ""

    cfg["authors"] = answers.get("authors") or []
    cfg["state"]["recent_topics_file"] = f"workspace-orchestrator/state/recent_topics-{slug}.json"

    return cfg


def write_project_artifacts(cfg: dict) -> str:
    slug = cfg["slug"]
    project_path = os.path.join(PROJECTS_DIR, f"{slug}.json")
    if os.path.exists(project_path):
        raise OnboardError(f"projects/{slug}.json already exists — refusing to overwrite")
    atomic_write_json(project_path, cfg)

    topics_path = os.path.expanduser(
        f"~/.openclaw/workspace-orchestrator/state/recent_topics-{slug}.json"
    )
    if not os.path.exists(topics_path):
        atomic_write_json(topics_path, [])
    return project_path


def run_validate(slug: str) -> tuple[bool, str]:
    result = subprocess.run(
        [
            sys.executable,
            VALIDATE_SCRIPT,
            "--slug",
            slug,
            "--scanner-ready",
            "--openclaw-sync",
            "--live",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0, (result.stdout + result.stderr)


def run_sync_all(*, apply: bool) -> tuple[bool, str]:
    args = [sys.executable, SYNC_SCRIPT]
    if apply:
        args.append("--apply")
    else:
        args.append("--dry-run")
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    return result.returncode == 0, (result.stdout + result.stderr)


def run_bind(slug: str, group_id: str, name: str, *, apply: bool) -> tuple[bool, str]:
    """Legacy single-project bind — prefer run_sync_all."""
    args = [sys.executable, BIND_SCRIPT, "--slug", slug, "--group-id", group_id, "--name", name]
    args.append("--apply" if apply else "--dry-run")
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    return result.returncode == 0, (result.stdout + result.stderr)


def finalize(answers: dict, *, apply_bind: bool) -> tuple[bool, str]:
    try:
        cfg = build_project_dict(answers)
        project_path = write_project_artifacts(cfg)
    except OnboardError as e:
        return False, f"ERROR: {e}"
    except OSError as e:
        return False, f"ERROR: failed writing project files: {e}"

    ok, out = run_validate(cfg["slug"])
    if not ok:
        return False, f"ERROR: project written to {project_path} but validation failed:\n{out}"

    ok, sync_out = run_sync_all(apply=apply_bind)
    summary = (
        f"OK: project '{cfg['slug']}' written to {project_path}\n"
        f"{sync_out}"
    )
    if not ok:
        summary += "\nWARN: sync step reported problems — review openclaw.json manually."
        return False, summary
    return True, summary


# ---------------------------------------------------------------------------
# Telegram entry points
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    chat_id, user_id = args.chat_id, args.user_id
    existing = db.get_onboard_session(chat_id, user_id)
    if existing and existing.step != "preview":
        # resume, don't restart
        answers = json.loads(existing.answers_json or "{}")
        text, keyboard = prompt_for(existing.step, answers)
        msg_id = send_telegram_message(chat_id, "(resuming) " + text, keyboard=keyboard)
        db.upsert_onboard_session(chat_id, user_id, existing.step, answers_json=existing.answers_json, prompt_message_id=msg_id)
        print(f"OK: resumed session for {chat_id}/{user_id} at step {existing.step}")
        return 0

    first_step = STEP_ORDER[0]
    text, keyboard = prompt_for(first_step, {})
    msg_id = send_telegram_message(chat_id, text, keyboard=keyboard)
    db.upsert_onboard_session(chat_id, user_id, first_step, answers_json="{}", prompt_message_id=msg_id)
    print(f"OK: started onboarding session for {chat_id}/{user_id}")
    return 0


def _strip_serializable(answers: dict) -> dict:
    """Categories/users lists are fine to persist (non-secret); everything
    else in answers is already non-secret (password never stored in dict)."""
    return answers


def cmd_step(args: argparse.Namespace) -> int:
    chat_id, user_id, raw_input = args.chat_id, args.user_id, args.input
    session = db.get_onboard_session(chat_id, user_id)
    if not session:
        send_telegram_message(chat_id, "No active onboarding session. Send /onboard to start.")
        print("ERROR: no active session")
        return 0

    answers = json.loads(session.answers_json or "{}")
    step = session.step
    handler = STEP_HANDLERS.get(step)
    if not handler:
        print(f"ERROR: no handler for step {step}")
        return 0

    try:
        if step == "logo_watermark":
            h_logo_watermark(answers, raw_input, media_path=args.media_path)
        else:
            handler(answers, raw_input)
    except OnboardError as e:
        if str(e) == "__CANCELLED__":
            db.clear_onboard_session(chat_id, user_id)
            _cleanup_orphan_password(answers)
            _cleanup_orphan_logo(answers)
            send_telegram_message(chat_id, "Onboarding cancelled.")
            print("OK: cancelled by user at preview")
            return 0
        send_telegram_message(chat_id, f"\u26A0\uFE0F {e}")
        print(f"OK: validation error at step {step}: {e}")
        return 0

    # Delete the password message immediately once accepted (security hygiene)
    if step == "wp_password" and args.reply_to_message_id:
        try:
            delete_telegram_message(chat_id, int(args.reply_to_message_id))
        except (TypeError, ValueError):
            pass

    idx = STEP_ORDER.index(step)
    if step == "preview":
        # confirmed — finalize now
        ok, summary = finalize(answers, apply_bind=True)
        send_telegram_message(chat_id, summary if len(summary) < 3800 else summary[:3800] + "\n…(truncated)")
        db.clear_onboard_session(chat_id, user_id)
        print(summary)
        return 0

    next_step = STEP_ORDER[idx + 1]
    text, keyboard = prompt_for(next_step, answers)
    msg_id = send_telegram_message(chat_id, text, keyboard=keyboard)
    db.upsert_onboard_session(
        chat_id, user_id, next_step,
        answers_json=json.dumps(_strip_serializable(answers)),
        prompt_message_id=msg_id,
    )
    print(f"OK: advanced {chat_id}/{user_id} to step {next_step}")
    return 0


def _cleanup_orphan_password(answers: dict) -> None:
    slug = answers.get("slug")
    if not slug:
        return
    path = os.path.join(CREDS_DIR, f"{slug}.pass")
    project_path = os.path.join(PROJECTS_DIR, f"{slug}.json")
    if os.path.exists(path) and not os.path.exists(project_path):
        try:
            os.remove(path)
        except OSError:
            pass
    _cleanup_orphan_logo(answers)


def cmd_status(args: argparse.Namespace) -> int:
    session = db.get_onboard_session(args.chat_id, args.user_id or "")
    if not session and args.user_id is None:
        session = db.get_any_onboard_session_for_chat(args.chat_id)
    if not session:
        print("OK: no active session")
        return 0
    print(f"OK: step={session.step} answers={session.answers_json}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    session = db.get_onboard_session(args.chat_id, args.user_id)
    if session:
        answers = json.loads(session.answers_json or "{}")
        _cleanup_orphan_password(answers)
    db.clear_onboard_session(args.chat_id, args.user_id)
    send_telegram_message(args.chat_id, "Onboarding cancelled.")
    print("OK: session cancelled")
    return 0


# ---------------------------------------------------------------------------
# CLI wizard (terminal, interactive)
# ---------------------------------------------------------------------------

def cli_prompt_plain(step: str, answers: dict) -> str:
    text, _ = prompt_for(step, answers)
    plain = re.sub(r"<[^>]+>", "", text)
    return plain


def cmd_wizard(_args: argparse.Namespace) -> int:
    print("=== News Project Onboarding Wizard ===\n")
    answers: dict = {}
    for step in STEP_ORDER:
        while True:
            print("\n" + cli_prompt_plain(step, answers))
            if step == "wp_password":
                import getpass
                raw = getpass.getpass("> ")
            elif step == "research_preset":
                raw = input(f"> (choices: {', '.join(PRESET_CHOICES)}) ").strip()
            elif step == "preview":
                raw = input("> (yes/no) ").strip().lower()
                raw = "ob_confirm:yes" if raw in ("y", "yes") else "ob_confirm:no"
            else:
                raw = input("> ").strip()
            try:
                if step == "logo_watermark":
                    h_logo_watermark(answers, raw, media_path=None)
                else:
                    STEP_HANDLERS[step](answers, raw)
                break
            except OnboardError as e:
                if str(e) == "__CANCELLED__":
                    print("Cancelled.")
                    _cleanup_orphan_password(answers)
                    _cleanup_orphan_logo(answers)
                    return 1
                print(f"Error: {e}")

    print("\nWriting project + patching openclaw.json (dry-run bind by default)...")
    apply_confirm = input("Apply openclaw.json patch + restart gateway now? (yes/no) > ").strip().lower()
    ok, summary = finalize(answers, apply_bind=apply_confirm in ("y", "yes"))
    print("\n" + summary)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# verify (Phase-7 style checks)
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    slug = args.slug
    print(f"=== Verifying project '{slug}' ===\n")
    ok_all = True

    ok, out = run_validate(slug)
    print(out)
    ok_all = ok_all and ok

    print(f"\n$ project_config.py --list  (expect '{slug}' present)")
    r = subprocess.run([sys.executable, os.path.join(HERE, "project_config.py"), "--list"], capture_output=True, text=True)
    print(r.stdout)
    if slug not in r.stdout.split():
        print(f"ERROR: '{slug}' not in project list")
        ok_all = False

    try:
        cfg = pc.load_project_config(slug=slug)
        group_id = cfg.get_path("telegram.group_id")
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "project_config.py"), "--chat-id", str(group_id)],
            capture_output=True, text=True,
        )
        print(f"$ project_config.py --chat-id {group_id}  ->  {r.stdout.strip()}")
        if r.stdout.strip() != slug:
            print(f"ERROR: --chat-id resolved to '{r.stdout.strip()}', expected '{slug}'")
            ok_all = False
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: could not load project config: {e}")
        ok_all = False

    r = subprocess.run(["pgrep", "-af", "pool_scheduler"], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"\nOK: pool_scheduler is running:\n{r.stdout.strip()}")
    else:
        print("\nWARN: pool_scheduler does not appear to be running — run ensure_scheduler.sh")

    if args.pipeline:
        print(f"\n$ update_headline_pool.py --project {slug}")
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "update_headline_pool.py"), "--project", slug],
            capture_output=True, text=True, timeout=120,
        )
        print(r.stdout[-2000:] + r.stderr[-1000:])

    print(f"\n{'VERIFY_OK' if ok_all else 'VERIFY_FAILED'}: {slug}")
    return 0 if ok_all else 1


def cmd_finalize(args: argparse.Namespace) -> int:
    answers = _load_json(args.answers)
    if not answers:
        print(f"ERROR: could not read answers file {args.answers}")
        return 1
    ok, summary = finalize(answers, apply_bind=args.apply_bind)
    print(summary)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("wizard")

    p_start = sub.add_parser("start")
    p_start.add_argument("--chat-id", required=True)
    p_start.add_argument("--user-id", required=True)

    p_step = sub.add_parser("step")
    p_step.add_argument("--chat-id", required=True)
    p_step.add_argument("--user-id", required=True)
    p_step.add_argument("--input", required=True)
    p_step.add_argument("--media-path", default=None, help="Local inbound media path for logo_watermark step")
    p_step.add_argument("--reply-to-message-id", default=None)

    p_status = sub.add_parser("status")
    p_status.add_argument("--chat-id", required=True)
    p_status.add_argument("--user-id", default=None)

    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("--chat-id", required=True)
    p_cancel.add_argument("--user-id", required=True)

    p_finalize = sub.add_parser("finalize")
    p_finalize.add_argument("--answers", required=True)
    p_finalize.add_argument("--apply-bind", action="store_true")

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--slug", required=True)
    p_verify.add_argument("--pipeline", action="store_true")

    args = parser.parse_args()

    if args.cmd == "wizard":
        return cmd_wizard(args)
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "step":
        return cmd_step(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "cancel":
        return cmd_cancel(args)
    if args.cmd == "finalize":
        return cmd_finalize(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
