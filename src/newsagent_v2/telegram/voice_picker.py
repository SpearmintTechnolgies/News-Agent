"""Per-chat Amazon Polly voice selection: the /voice picker.

/voice opens an inline keyboard. Step one picks the language (English first,
then the others alphabetically), step two picks the voice, labelled
"Name · Gender · best engine". Both steps paginate at ~8 buttons. A ▶️ button
per voice plays a one-sentence sample. The choice is saved per chat (a group
and a DM are separate chats) in data/v5_state/tts_voice.json.

Callback data (all well under Telegram's 64-byte limit):
  voice:langs:<page>           language list page
  voice:lang:<code>:<page>     voice list for one language, paged
  voice:set:<voice_id>         save the voice for this chat
  voice:play:<voice_id>        one-sentence preview in this chat
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from newsagent_v2.tts.polly import Voice, default_voice_id

STORE_PATH = Path("data/v5_state/tts_voice.json")
CALLBACK_PREFIX = "voice"
PAGE_SIZE = 8
PREVIEW_TEXT = "Hi! This is how I sound reading the news."

PICKER_TITLE = "🔊 Narration voice"
LANG_STEP_TEXT = "Pick a language. English is first; the rest are alphabetical."


# ============ PER-CHAT STORE ============

def _read(path: Path | None = None) -> dict[str, Any]:
    store = path or STORE_PATH
    if not store.exists():
        return {}
    try:
        payload = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write(payload: dict[str, Any], path: Path | None = None) -> None:
    store = path or STORE_PATH
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def set_voice(chat_id: str, voice_id: str, path: Path | None = None) -> None:
    """Save the chosen voice for this chat (group and DM separately)."""
    chat = str(chat_id or "").strip()
    voice = str(voice_id or "").strip()
    if not chat or not voice:
        return
    payload = _read(path)
    chats = payload.get("chats")
    if not isinstance(chats, dict):
        chats = {}
    chats[chat] = voice
    payload["chats"] = chats
    _write(payload, path)


def voice_for(chat_id: str, path: Path | None = None, environ: dict[str, str] | None = None) -> str:
    """This chat's voice, else the env default, else Ruth."""
    chats = _read(path).get("chats")
    if isinstance(chats, dict):
        chosen = str(chats.get(str(chat_id or "").strip()) or "").strip()
        if chosen:
            return chosen
    return default_voice_id(environ)


# ============ PICKER KEYBOARDS ============

def language_groups(voices: list[Voice]) -> list[tuple[str, str, int]]:
    """(language_code, language_name, voice_count): en-US first, other English
    variants next, then everything else alphabetically by language name."""
    grouped: dict[str, tuple[str, int]] = {}
    for voice in voices:
        name, count = grouped.get(voice.language_code, (voice.language_name or voice.language_code, 0))
        grouped[voice.language_code] = (name, count + 1)

    def _rank(code: str) -> int:
        lowered = code.lower()
        if lowered == "en-us":
            return 0
        if lowered.startswith("en-"):
            return 1
        return 2

    rows = sorted(
        ((code, name, count) for code, (name, count) in grouped.items()),
        key=lambda row: (_rank(row[0]), row[1].lower()),
    )
    return rows


def _page_rows(rows: list[Any], page: int) -> tuple[list[Any], int, int]:
    """Slice rows into pages. Returns (page_rows, page, page_count)."""
    page_count = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    start = page * PAGE_SIZE
    return rows[start:start + PAGE_SIZE], page, page_count


def _nav_row(prev_data: str | None, next_data: str | None, back_data: str | None = None) -> list[dict[str, str]]:
    row: list[dict[str, str]] = []
    if back_data:
        row.append({"text": "« Languages", "callback_data": back_data})
    if prev_data:
        row.append({"text": "‹ Prev", "callback_data": prev_data})
    if next_data:
        row.append({"text": "Next ›", "callback_data": next_data})
    return row


def language_keyboard(voices: list[Voice], page: int = 0) -> dict[str, Any]:
    """Step one: one button per language, English first, paginated."""
    groups, page, page_count = _page_rows(language_groups(voices), page)
    keyboard: list[list[dict[str, str]]] = [
        [{"text": f"{name} · {count}", "callback_data": f"{CALLBACK_PREFIX}:lang:{code}:0"}]
        for code, name, count in groups
    ]
    prev_data = f"{CALLBACK_PREFIX}:langs:{page - 1}" if page > 0 else None
    next_data = f"{CALLBACK_PREFIX}:langs:{page + 1}" if page < page_count - 1 else None
    nav = _nav_row(prev_data, next_data)
    if nav:
        keyboard.append(nav)
    return {"inline_keyboard": keyboard}


def voices_for_language(voices: list[Voice], language_code: str) -> list[Voice]:
    wanted = str(language_code or "").strip().lower()
    return sorted(
        (voice for voice in voices if voice.language_code.lower() == wanted),
        key=lambda voice: voice.id.lower(),
    )


def voice_keyboard(voices: list[Voice], language_code: str, page: int = 0, *, selected_id: str = "") -> dict[str, Any]:
    """Step two: 'Name · Gender · best engine' per voice, plus a ▶️ preview."""
    matching, page, page_count = _page_rows(voices_for_language(voices, language_code), page)
    keyboard: list[list[dict[str, str]]] = []
    for voice in matching:
        mark = "✓ " if selected_id and voice.id.lower() == selected_id.lower() else ""
        keyboard.append([
            {"text": f"{mark}{voice.label()}", "callback_data": f"{CALLBACK_PREFIX}:set:{voice.id}"},
            {"text": "▶️", "callback_data": f"{CALLBACK_PREFIX}:play:{voice.id}"},
        ])
    prev_data = f"{CALLBACK_PREFIX}:lang:{language_code}:{page - 1}" if page > 0 else None
    next_data = f"{CALLBACK_PREFIX}:lang:{language_code}:{page + 1}" if page < page_count - 1 else None
    nav = _nav_row(prev_data, next_data, back_data=f"{CALLBACK_PREFIX}:langs:0")
    keyboard.append(nav)
    return {"inline_keyboard": keyboard}


# ============ CALLBACK PARSING ============

def parse_voice_callback(data: str) -> dict[str, str] | None:
    """Parse a voice: callback; None when it is not one."""
    parts = str(data or "").split(":")
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX:
        return None
    action = parts[1]
    if action == "langs" and len(parts) == 3:
        return {"action": "langs", "page": parts[2]}
    if action == "lang" and len(parts) == 4:
        return {"action": "lang", "language_code": parts[2], "page": parts[3]}
    if action in {"set", "play"} and len(parts) == 3 and parts[2]:
        return {"action": action, "voice_id": parts[2]}
    return None


def safe_page(raw: str) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0
