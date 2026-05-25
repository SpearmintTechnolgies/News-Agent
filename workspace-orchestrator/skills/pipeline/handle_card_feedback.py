#!/usr/bin/env python3
"""
handle_card_feedback.py — Telegram editorial feedback: RATE, IMAGE, DRAFT, EDIT, PUBLISH.

Always exits 0 (fail-open). Sends user-facing Telegram replies directly.
Stdout is for orchestrator logging only — do not echo back to the group.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from editorial_db import (
    Article,
    DEFAULT_DB_PATH,
    clear_edit_session,
    get_edit_session_by_prompt,
    get_latest_version,
    init_db,
    lookup_by_alert_id,
    lookup_by_message_id,
    lookup_by_run_id,
    record_editorial_action,
    record_rating,
    save_article_version,
    set_wp_status,
    upsert_edit_session,
)

OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
WP_ACTIONS = os.path.expanduser(
    "~/.openclaw/workspace-wp-publisher/skills/wordpress/wp_post_actions.sh"
)

_RE_ARTICLE_RATE = re.compile(r"^oc_r:(.+):(\d{1,2})$")
_RE_IMAGE_RATE = re.compile(r"^oc_ri:(.+):(\d{1,2})$")
_RE_IMAGE_MENU = re.compile(r"^oc_ri_menu:(.+)$")
_RE_DRAFT = re.compile(r"^oc_draft:(.+)$")
_RE_DRAFT_YES = re.compile(r"^oc_draft_yes:(.+)$")
_RE_DRAFT_NO = re.compile(r"^oc_draft_no:(.+)$")
_RE_PUBLISH = re.compile(r"^oc_publish:(.+)$")
_RE_PUBLISH_YES = re.compile(r"^oc_publish_yes:(.+)$")
_RE_PUBLISH_NO = re.compile(r"^oc_publish_no:(.+)$")
_RE_EDIT = re.compile(r"^oc_edit:(.+)$")
_RE_EDIT_APPLY = re.compile(r"^oc_edit_apply:(.+)$")
_RE_EDIT_CANCEL = re.compile(r"^oc_edit_cancel:(.+)$")
_RE_TEXT_RATE = re.compile(r"^(?:RATE|rate)\s+(\d{1,2})\b")
_RE_TEXT_IMAGE = re.compile(r"^(?:IMAGE|image)\s+(\d{1,2})\b")
_RE_TEXT_DRAFT = re.compile(r"^(?:DRAFT|draft|UNPUBLISH|unpublish)\s*$")
_RE_TEXT_PUBLISH = re.compile(r"^(?:PUBLISH|publish|REPUBLISH|republish)\s*$")
_RE_TEXT_EDIT = re.compile(r"^(?:EDIT|edit)\s*$")
_RE_ALERT = re.compile(r"\bta-([A-Za-z0-9_-]+)\b")

def normalize_md(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


def edit_diff_stats(baseline: str, edited: str) -> tuple[int, int]:
    base_lines = normalize_md(baseline).splitlines()
    edit_lines = normalize_md(edited).splitlines()
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base_lines, edit_lines).get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            added += j2 - j1
            removed += i2 - i1
    return added, removed


def format_diff_summary(baseline: str, edited: str) -> str:
    added, removed = edit_diff_stats(baseline, edited)
    added = min(added, 99)
    removed = min(removed, 99)
    return f"+{added} / −{removed} lines"



def load_bot_token() -> str:
    try:
        with open(OPENCLAW_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("channels") or {}).get("telegram", {}).get("botToken", "")
    except (OSError, json.JSONDecodeError):
        return ""


def telegram_request(
    token: str, method: str, data: dict | None = None, files: dict | None = None
) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = "----OpenClawBoundary"
        body_parts: list[bytes] = []
        for name, (filename, content, mime) in files.items():
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            body_parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
            body_parts.append(content)
            body_parts.append(b"\r\n")
        if data:
            for key, val in data.items():
                body_parts.append(f"--{boundary}\r\n".encode())
                body_parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
                body_parts.append(str(val).encode("utf-8"))
                body_parts.append(b"\r\n")
        body_parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(body_parts)
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
    else:
        encoded = urllib.parse.urlencode(data or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    result = json.loads(raw)
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "unknown Telegram error"))
    return result


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    reply_to_message_id: int | None = None,
    reply_markup: dict | None = None,
) -> int | None:
    payload: dict[str, str] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = str(reply_to_message_id)
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    result = telegram_request(token, "sendMessage", payload)
    msg = result.get("result") or {}
    return msg.get("message_id")


def send_document(
    token: str,
    chat_id: str,
    file_path: str,
    *,
    caption: str = "",
    reply_to_message_id: int | None = None,
) -> int | None:
    with open(file_path, "rb") as f:
        content = f.read()
    filename = os.path.basename(file_path)
    data: dict[str, str] = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    if reply_to_message_id is not None:
        data["reply_to_message_id"] = str(reply_to_message_id)
    result = telegram_request(
        token,
        "sendDocument",
        data=data,
        files={"document": (filename, content, "text/markdown")},
    )
    msg = result.get("result") or {}
    return msg.get("message_id")


def download_telegram_file(token: str, file_id: str) -> bytes:
    result = telegram_request(token, "getFile", {"file_id": file_id})
    file_path = (result.get("result") or {}).get("file_path")
    if not file_path:
        raise RuntimeError("getFile returned no path")
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def build_image_score_keyboard(run_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": str(n), "callback_data": f"oc_ri:{run_id}:{n}"} for n in range(1, 6)],
            [{"text": str(n), "callback_data": f"oc_ri:{run_id}:{n}"} for n in range(6, 11)],
        ]
    }


def build_draft_confirm_keyboard(run_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Yes, unpublish", "callback_data": f"oc_draft_yes:{run_id}"},
                {"text": "Cancel", "callback_data": f"oc_draft_no:{run_id}"},
            ]
        ]
    }


def build_publish_confirm_keyboard(run_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Yes, publish", "callback_data": f"oc_publish_yes:{run_id}"},
                {"text": "Cancel", "callback_data": f"oc_publish_no:{run_id}"},
            ]
        ]
    }


def build_edit_confirm_keyboard(run_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Apply to WordPress", "callback_data": f"oc_edit_apply:{run_id}"},
                {"text": "Cancel", "callback_data": f"oc_edit_cancel:{run_id}"},
            ]
        ]
    }


def build_publish_prompt_keyboard(run_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Publish", "callback_data": f"oc_publish:{run_id}"}],
        ]
    }


def parse_score(value: str) -> int | None:
    try:
        score = int(value)
    except ValueError:
        return None
    return score if 1 <= score <= 10 else None


def resolve_article(
    *,
    chat_id: str | None,
    reply_to_message_id: str | None,
    run_id: str | None,
    alert_id: str | None,
    db_path: str,
) -> Article | None:
    if run_id:
        article = lookup_by_run_id(run_id, db_path)
        if article:
            return article
    if alert_id:
        article = lookup_by_alert_id(alert_id, db_path)
        if article:
            return article
    if chat_id and reply_to_message_id:
        try:
            return lookup_by_message_id(chat_id, int(reply_to_message_id), db_path)
        except ValueError:
            return None
    return None


def extract_alert_from_text(text: str) -> str | None:
    match = _RE_ALERT.search(text)
    return match.group(1) if match else None


def parse_input(payload: str | None, message_text: str | None) -> dict | None:
    if payload:
        p = payload.strip()
        for pattern, action in (
            (_RE_IMAGE_MENU, "image_menu"),
            (_RE_DRAFT_YES, "draft_yes"),
            (_RE_DRAFT_NO, "draft_no"),
            (_RE_DRAFT, "draft"),
            (_RE_PUBLISH_YES, "publish_yes"),
            (_RE_PUBLISH_NO, "publish_no"),
            (_RE_PUBLISH, "publish"),
            (_RE_EDIT_APPLY, "edit_apply"),
            (_RE_EDIT_CANCEL, "edit_cancel"),
            (_RE_EDIT, "edit"),
        ):
            m = pattern.match(p)
            if m:
                return {"action": action, "run_id": m.group(1).strip()}
        art = _RE_ARTICLE_RATE.match(p)
        if art:
            score = parse_score(art.group(2))
            if score is None:
                return None
            return {"action": "rate_article", "run_id": art.group(1).strip(), "score": score}
        img = _RE_IMAGE_RATE.match(p)
        if img:
            score = parse_score(img.group(2))
            if score is None:
                return None
            return {"action": "rate_image", "run_id": img.group(1).strip(), "score": score}

    if message_text:
        text = message_text.strip()
        if _RE_TEXT_DRAFT.match(text):
            return {"action": "draft", "run_id": None, "alert_id": extract_alert_from_text(text)}
        if _RE_TEXT_PUBLISH.match(text):
            return {"action": "publish", "run_id": None, "alert_id": extract_alert_from_text(text)}
        if _RE_TEXT_EDIT.match(text):
            return {"action": "edit", "run_id": None, "alert_id": extract_alert_from_text(text)}
        img_match = _RE_TEXT_IMAGE.match(text)
        if img_match:
            score = parse_score(img_match.group(1))
            if score is None:
                return None
            return {"action": "rate_image", "run_id": None, "score": score, "alert_id": extract_alert_from_text(text)}
        rate_match = _RE_TEXT_RATE.match(text)
        if rate_match:
            score = parse_score(rate_match.group(1))
            if score is None:
                return None
            return {"action": "rate_article", "run_id": None, "score": score, "alert_id": extract_alert_from_text(text)}
    return None


def resolve_article_markdown(article: Article, db_path: str) -> str | None:
    snap = get_latest_version(article.id, "published_snapshot", db_path)
    if snap and snap.content_md.strip():
        return snap.content_md
    if article.run_dir:
        path = os.path.join(article.run_dir, "article", "final.md")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    fallback = f"/tmp/crypto-run-{article.run_id}/article/final.md"
    if os.path.isfile(fallback):
        with open(fallback, encoding="utf-8") as f:
            return f.read()
    return None


def run_wp_action(args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["bash", WP_ACTIONS, *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
        line = (result.stdout or result.stderr or "").strip().split("\n")[-1]
        if result.returncode == 0 and line.startswith("WP_") and "_OK" in line:
            return True, line
        return False, line or f"exit {result.returncode}"
    except Exception as e:
        return False, str(e)


def handle_rate(
    *,
    article: Article,
    event_type: str,
    score: int,
    user_id: str | None,
    username: str | None,
    source: str,
    raw: str | None,
    db_path: str,
    token: str,
    chat_id: str,
    reply_id: int | None,
) -> str:
    record_rating(
        article.id,
        event_type,
        score,
        user_id=user_id,
        user_username=username,
        source=source,
        raw_payload=raw,
        db_path=db_path,
    )
    label = "article" if event_type == "rate_article" else "image"
    if token and chat_id:
        send_message(
            token,
            chat_id,
            f"Rated {label} <b>{score}/10</b> for ALERT: <code>{article.alert_id}</code>",
            reply_to_message_id=reply_id,
        )
    kind = "article" if event_type == "rate_article" else "image"
    return f"RATE_RECORDED: {article.alert_id} {kind}={score}"


def handle_draft_request(
    article: Article, token: str, chat_id: str, reply_id: int | None, source: str, raw: str | None, db_path: str
) -> str:
    if article.wp_status == "draft":
        if token and chat_id:
            send_message(token, chat_id, f"<code>{article.alert_id}</code> is already a draft.", reply_to_message_id=reply_id)
        return f"DRAFT_ALREADY: {article.alert_id}"
    if token and chat_id:
        send_message(
            token,
            chat_id,
            f"Unpublish <code>{article.alert_id}</code> on WordPress?",
            reply_to_message_id=reply_id,
            reply_markup=build_draft_confirm_keyboard(article.run_id),
        )
    return f"DRAFT_CONFIRM_SENT: {article.alert_id}"


def handle_draft_yes(
    article: Article, user_id: str | None, source: str, raw: str | None, db_path: str, token: str, chat_id: str, reply_id: int | None
) -> str:
    if not article.wp_post_id:
        if token and chat_id:
            send_message(token, chat_id, "No WordPress post ID on file.", reply_to_message_id=reply_id)
        return "DRAFT_FAILED: no wp_post_id"
    ok, msg = run_wp_action(["--post-id", article.wp_post_id, "--set-status", "draft"])
    if not ok:
        if token and chat_id:
            send_message(token, chat_id, f"Unpublish failed: {msg}", reply_to_message_id=reply_id)
        return f"DRAFT_FAILED: {msg}"
    set_wp_status(article.id, "draft", db_path)
    record_editorial_action(article.id, "draft", user_id=user_id, source=source, raw_payload=raw, db_path=db_path)
    if token and chat_id:
        send_message(
            token,
            chat_id,
            f"Unpublished <code>{article.alert_id}</code> — now a WordPress draft.",
            reply_to_message_id=reply_id,
        )
    return f"DRAFT_OK: {article.alert_id}"


def handle_publish_request(
    article: Article, token: str, chat_id: str, reply_id: int | None, source: str, raw: str | None, db_path: str
) -> str:
    if article.wp_status == "publish":
        if token and chat_id:
            send_message(
                token,
                chat_id,
                f"<code>{article.alert_id}</code> is already published.",
                reply_to_message_id=reply_id,
            )
        return f"PUBLISH_ALREADY: {article.alert_id}"
    if token and chat_id:
        send_message(
            token,
            chat_id,
            f"Publish <code>{article.alert_id}</code> on WordPress?",
            reply_to_message_id=reply_id,
            reply_markup=build_publish_confirm_keyboard(article.run_id),
        )
    return f"PUBLISH_CONFIRM_SENT: {article.alert_id}"


def handle_publish_yes(
    article: Article, user_id: str | None, source: str, raw: str | None, db_path: str, token: str, chat_id: str, reply_id: int | None
) -> str:
    if not article.wp_post_id:
        if token and chat_id:
            send_message(token, chat_id, "No WordPress post ID on file.", reply_to_message_id=reply_id)
        return "PUBLISH_FAILED: no wp_post_id"
    ok, msg = run_wp_action(["--post-id", article.wp_post_id, "--set-status", "publish"])
    if not ok:
        if token and chat_id:
            send_message(token, chat_id, f"Publish failed: {msg}", reply_to_message_id=reply_id)
        return f"PUBLISH_FAILED: {msg}"
    set_wp_status(article.id, "publish", db_path)
    record_editorial_action(article.id, "publish", user_id=user_id, source=source, raw_payload=raw, db_path=db_path)
    url = ""
    if "url=" in msg:
        url = msg.split("url=", 1)[-1].strip()
    if token and chat_id:
        body = f"Published <code>{article.alert_id}</code> — now live on WordPress."
        if url:
            body += f"\n\n{url}"
        send_message(token, chat_id, body, reply_to_message_id=reply_id)
    return f"PUBLISH_OK: {article.alert_id}"


def handle_edit_start(
    article: Article, user_id: str, username: str | None, db_path: str, token: str, chat_id: str, reply_id: int | None
) -> str:
    content = resolve_article_markdown(article, db_path)
    if not content:
        if token and chat_id:
            send_message(token, chat_id, f"No article file found for <code>{article.alert_id}</code>.", reply_to_message_id=reply_id)
        return "ARTICLE_FILE_NOT_FOUND"
    if not token or not chat_id:
        return "EDIT_PROMPT_SENT: no telegram token"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(content)
        tmp_path = f.name
    try:
        caption = (
            f"Edit <code>{article.alert_id}</code>\n\n"
            "Download → edit → <b>save</b> → reply to <b>this message</b> with the corrected <b>.md</b> file.\n"
            "(Pasted text is not accepted — articles exceed Telegram's limit.)"
        )
        prompt_id = send_document(token, chat_id, tmp_path, caption=caption, reply_to_message_id=reply_id)
    finally:
        os.unlink(tmp_path)
    if not prompt_id:
        return "EDIT_FAILED: sendDocument failed"
    upsert_edit_session(
        article.id,
        user_id,
        "awaiting_paste",
        prompt_message_id=prompt_id,
        db_path=db_path,
    )
    return f"EDIT_PROMPT_SENT: {article.alert_id}"


def handle_edit_document(
    session,
    article: Article,
    content: bytes,
    doc_name: str,
    user_id: str,
    username: str | None,
    db_path: str,
    token: str,
    chat_id: str,
    reply_id: int | None,
) -> str:
    if session.state != "awaiting_paste":
        return f"EDIT_INVALID: session state {session.state}"
    name = (doc_name or "").lower()
    if not name.endswith(".md"):
        if token and chat_id:
            send_message(token, chat_id, "Please upload a <b>.md</b> file.", reply_to_message_id=reply_id)
        return "EDIT_USE_MARKDOWN"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        if token and chat_id:
            send_message(token, chat_id, "File must be UTF-8 markdown.", reply_to_message_id=reply_id)
        return "EDIT_USE_MARKDOWN"
    if len(text.strip()) < 100:
        if token and chat_id:
            send_message(token, chat_id, "File too short — upload the full corrected article.", reply_to_message_id=reply_id)
        return "EDIT_USE_MARKDOWN"
    baseline = resolve_article_markdown(article, db_path) or ""
    if normalize_md(text) == normalize_md(baseline):
        if token and chat_id:
            send_message(
                token,
                chat_id,
                "No changes detected. Your file matches the current article.\n\n"
                "Edit the downloaded file, <b>save it</b>, then upload again.",
                reply_to_message_id=reply_id,
            )
        return "EDIT_UNCHANGED"
    diff_summary = format_diff_summary(baseline, text)
    version_id = save_article_version(
        article.id,
        "user_suggested",
        text,
        user_id=user_id,
        user_username=username,
        db_path=db_path,
    )
    upsert_edit_session(
        article.id,
        user_id,
        "awaiting_confirm",
        prompt_message_id=session.prompt_message_id,
        suggested_version_id=version_id,
        db_path=db_path,
    )
    if token and chat_id:
        send_message(
            token,
            chat_id,
            f"Edit saved for <code>{article.alert_id}</code> — <b>{diff_summary}</b> changed.\n\nApply to WordPress?",
            reply_to_message_id=reply_id,
            reply_markup=build_edit_confirm_keyboard(article.run_id),
        )
    return f"EDIT_SAVED: {article.alert_id} awaiting_confirm {diff_summary}"


def handle_edit_apply(
    article: Article, user_id: str, source: str, raw: str | None, db_path: str, token: str, chat_id: str, reply_id: int | None
) -> str:
    suggested = get_latest_version(article.id, "user_suggested", db_path)
    if not suggested:
        if token and chat_id:
            send_message(token, chat_id, "No pending edit found.", reply_to_message_id=reply_id)
        return "EDIT_FAILED: no suggested version"
    if not article.wp_post_id:
        return "EDIT_FAILED: no wp_post_id"
    baseline = resolve_article_markdown(article, db_path) or ""
    if normalize_md(suggested.content_md) == normalize_md(baseline):
        if token and chat_id:
            send_message(
                token,
                chat_id,
                "No changes to apply — upload a corrected file first.",
                reply_to_message_id=reply_id,
            )
        return "EDIT_UNCHANGED"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(suggested.content_md)
        tmp_path = f.name
    try:
        ok, msg = run_wp_action(
            ["--post-id", article.wp_post_id, "--markdown", tmp_path, "--update-content"]
        )
    finally:
        os.unlink(tmp_path)
    if not ok:
        if token and chat_id:
            send_message(token, chat_id, f"Apply failed: {msg}", reply_to_message_id=reply_id)
        return f"EDIT_FAILED: {msg}"
    save_article_version(
        article.id,
        "applied",
        suggested.content_md,
        user_id=user_id,
        db_path=db_path,
    )
    record_editorial_action(article.id, "edit_apply", user_id=user_id, source=source, raw_payload=raw, db_path=db_path)
    clear_edit_session(article.id, user_id or "", db_path)
    if token and chat_id:
        if article.wp_status == "draft":
            send_message(
                token,
                chat_id,
                f"Edit applied to WordPress for <code>{article.alert_id}</code>.\n\n"
                "Post is still a draft — publish now?",
                reply_to_message_id=reply_id,
                reply_markup=build_publish_prompt_keyboard(article.run_id),
            )
        else:
            send_message(
                token,
                chat_id,
                f"Edit applied to WordPress for <code>{article.alert_id}</code>.",
                reply_to_message_id=reply_id,
            )
    return f"EDIT_APPLIED: {article.alert_id}"


def handle_edit_cancel(
    article: Article, user_id: str, source: str, raw: str | None, db_path: str, token: str, chat_id: str, reply_id: int | None
) -> str:
    clear_edit_session(article.id, user_id or "", db_path)
    record_editorial_action(article.id, "edit_cancel", user_id=user_id, source=source, raw_payload=raw, db_path=db_path)
    if token and chat_id:
        send_message(token, chat_id, f"Edit cancelled for <code>{article.alert_id}</code>.", reply_to_message_id=reply_id)
    return f"EDIT_CANCELLED: {article.alert_id}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Handle Telegram editorial feedback")
    parser.add_argument("--payload", help="Callback data")
    parser.add_argument("--message-text", help="Plain text command")
    parser.add_argument("--chat-id", help="Telegram chat id")
    parser.add_argument("--user-id", help="Telegram user id")
    parser.add_argument("--username", default="", help="Telegram username")
    parser.add_argument("--reply-to-message-id", help="Replied-to message id")
    parser.add_argument("--document-file-id", help="Telegram document file_id")
    parser.add_argument("--document-name", default="", help="Uploaded document filename")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--no-reply", action="store_true")
    args = parser.parse_args()

    init_db(args.db_path)
    chat_id = str(args.chat_id or "").strip()
    token = "" if args.no_reply else load_bot_token()
    reply_id: int | None = None
    if args.reply_to_message_id:
        try:
            reply_id = int(args.reply_to_message_id)
        except ValueError:
            reply_id = None
    source = "callback" if args.payload else "text"
    raw = args.payload or args.message_text

    # Document upload (edit paste)
    if args.document_file_id and reply_id is not None:
        pair = get_edit_session_by_prompt(reply_id, args.db_path)
        if not pair:
            print("EDIT_INVALID: no session for this reply")
            return 0
        session, article = pair
        if not token:
            print("EDIT_FAILED: no token")
            return 0
        try:
            content = download_telegram_file(token, args.document_file_id)
        except Exception as e:
            print(f"EDIT_FAILED: download {e}")
            return 0
        print(
            handle_edit_document(
                session,
                article,
                content,
                args.document_name,
                args.user_id or "",
                args.username or None,
                args.db_path,
                token,
                chat_id,
                reply_id,
            )
        )
        return 0

    # Text reply during awaiting_paste
    if args.message_text and reply_id is not None and not args.payload:
        pair = get_edit_session_by_prompt(reply_id, args.db_path)
        if pair and pair[0].state == "awaiting_paste":
            if token and chat_id:
                send_message(
                    token,
                    chat_id,
                    "Upload a corrected <b>.md</b> file as a reply to the article document.",
                    reply_to_message_id=reply_id,
                )
            print("EDIT_USE_DOCUMENT")
            return 0

    if not args.payload and not args.message_text:
        print("INVALID: no payload or message-text", file=sys.stderr)
        return 0

    parsed = parse_input(args.payload, args.message_text)
    if parsed is None:
        print("INVALID: unrecognized command", file=sys.stderr)
        return 0

    action = parsed["action"]
    run_id = parsed.get("run_id")
    alert_id = parsed.get("alert_id")
    score = parsed.get("score")

    article = resolve_article(
        chat_id=chat_id or None,
        reply_to_message_id=args.reply_to_message_id,
        run_id=run_id,
        alert_id=alert_id,
        db_path=args.db_path,
    )

    if action == "image_menu":
        if not run_id:
            print("RATE_INVALID: missing run_id", file=sys.stderr)
            return 0
        article = lookup_by_run_id(run_id, args.db_path)
        if not article:
            print("ARTICLE_NOT_FOUND")
            return 0
        if token and chat_id:
            send_message(
                token,
                chat_id,
                f"Rate the <b>image</b> for <code>{article.alert_id}</code>:",
                reply_to_message_id=reply_id or article.telegram_message_id,
                reply_markup=build_image_score_keyboard(article.run_id),
            )
        print(f"RATE_MENU_SENT: {article.alert_id}")
        return 0

    if action in ("rate_article", "rate_image"):
        if not article:
            print("ARTICLE_NOT_FOUND")
            if token and chat_id:
                send_message(token, chat_id, "No article found. Reply directly to the news card.", reply_to_message_id=reply_id)
            return 0
        print(
            handle_rate(
                article=article,
                event_type=action,
                score=score,
                user_id=args.user_id,
                username=args.username or None,
                source=source,
                raw=raw,
                db_path=args.db_path,
                token=token,
                chat_id=chat_id,
                reply_id=reply_id,
            )
        )
        return 0

    if not article:
        print("ARTICLE_NOT_FOUND")
        if token and chat_id:
            send_message(token, chat_id, "No article found for this message.", reply_to_message_id=reply_id)
        return 0

    if action == "draft":
        print(handle_draft_request(article, token, chat_id, reply_id, source, raw, args.db_path))
    elif action == "draft_yes":
        print(handle_draft_yes(article, args.user_id, source, raw, args.db_path, token, chat_id, reply_id))
    elif action == "draft_no":
        if token and chat_id:
            send_message(token, chat_id, "Unpublish cancelled.", reply_to_message_id=reply_id)
        print("DRAFT_CANCELLED")
    elif action == "publish":
        print(handle_publish_request(article, token, chat_id, reply_id, source, raw, args.db_path))
    elif action == "publish_yes":
        print(handle_publish_yes(article, args.user_id, source, raw, args.db_path, token, chat_id, reply_id))
    elif action == "publish_no":
        if token and chat_id:
            send_message(token, chat_id, "Publish cancelled.", reply_to_message_id=reply_id)
        print("PUBLISH_CANCELLED")
    elif action == "edit":
        if not args.user_id:
            print("EDIT_FAILED: user-id required")
            return 0
        print(handle_edit_start(article, args.user_id, args.username or None, args.db_path, token, chat_id, reply_id))
    elif action == "edit_apply":
        print(handle_edit_apply(article, args.user_id, source, raw, args.db_path, token, chat_id, reply_id))
    elif action == "edit_cancel":
        print(handle_edit_cancel(article, args.user_id or "", source, raw, args.db_path, token, chat_id, reply_id))

    return 0


if __name__ == "__main__":
    sys.exit(main())
