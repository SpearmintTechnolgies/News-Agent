#!/usr/bin/env python3
"""Send backlink approval card to Telegram (Phase 1 v2)."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from html import escape as html_escape
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from states import WorkflowState  # noqa: E402

OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
TELEGRAM_CONFIG = _ROOT / "config" / "telegram_backlink_config.json"
TELEGRAM_CAPTION_MAX = 1024
CALLBACK_PREFIX = "bl"
IMAGE_FAIL_URL_PREFIX = "failed:"


def load_json(path: os.PathLike[str] | str, default: dict | None = None) -> dict:
    path = Path(path)
    if not path.is_file():
        return default or {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else (default or {})
    except (OSError, json.JSONDecodeError):
        return default or {}


def load_bot_token() -> str:
    cfg = load_json(TELEGRAM_CONFIG)
    token = str(cfg.get("bot_token") or cfg.get("botToken") or "")
    if token:
        return token
    data = load_json(OPENCLAW_JSON)
    account_id = str(cfg.get("openclaw_account_id") or "backlink")
    accounts = (data.get("channels") or {}).get("telegram", {}).get("accounts") or {}
    account = accounts.get(account_id) or {}
    token = str(account.get("botToken") or "")
    if token:
        return token
    return str((data.get("channels") or {}).get("telegram", {}).get("botToken") or "")


def load_group_id(config_path: Path | None = None) -> str:
    cfg = load_json(config_path or TELEGRAM_CONFIG)
    return str(cfg.get("group_id") or "")


def callback_data(action: str, workflow_id: str) -> str:
    return f"{CALLBACK_PREFIX}_{action}:{workflow_id}"


def build_inline_keyboard(workflow_id: str) -> dict[str, Any]:
    """Phase 1: Approve, Edit, Reject only."""
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": callback_data("approve", workflow_id)},
                {"text": "Edit", "callback_data": callback_data("edit", workflow_id)},
                {"text": "Reject", "callback_data": callback_data("reject", workflow_id)},
            ],
        ]
    }


def truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _project_context(
    row: backlink_db.WorkflowRow,
    db_path: str,
) -> tuple[str | None, str | None, str | None]:
    niche_name = project_name = target_url = None
    if row.project_id:
        project = backlink_db.get_project(row.project_id, db_path=db_path)
        if project:
            project_name = project.name
            target_url = project.target_url or f"https://{project.target_domain}"
            niche = backlink_db.get_niche(project.niche_id, db_path=db_path)
            if niche:
                niche_name = niche.name
    return niche_name, project_name, target_url


def build_card_payload(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    approval_expires_at: str | None = None,
) -> dict[str, Any]:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state not in {
        WorkflowState.CONTENT_READY.value,
        WorkflowState.DRAFTED.value,
        WorkflowState.PENDING_APPROVAL.value,
    }:
        raise ValueError(
            f"Cannot send approval card for {workflow_id} in state {row.state}"
        )

    opp = None
    if row.opportunity_id:
        opp = backlink_db.get_opportunity(row.opportunity_id, db_path=db_path)

    content = backlink_db.get_latest_content_asset(workflow_id, db_path=db_path)
    draft = backlink_db.get_latest_draft(workflow_id, db_path=db_path)
    audit = backlink_db.get_latest_audit(workflow_id, db_path=db_path)

    if content:
        body_text = content.content_text
        content_version = content.version
        content_id = content.id
        image_path = content.image_local_path
        target_link = content.target_link
        image_error = None
        if content.image_url and content.image_url.startswith(IMAGE_FAIL_URL_PREFIX):
            image_error = content.image_url[len(IMAGE_FAIL_URL_PREFIX) :]
            image_path = None
        elif image_path and not os.path.isfile(image_path):
            image_path = None
            image_error = "Feature image file missing"
    else:
        body_text = draft.draft_text if draft else "(no content saved yet)"
        content_version = draft.version if draft else 0
        content_id = draft.id if draft else None
        image_path = None
        target_link = None
        image_error = None

    niche_name, project_name, project_url = _project_context(row, db_path)

    alert_id = f"bl-{workflow_id}"
    domain = opp.domain if opp else "unknown"
    url = opp.url if opp else ""
    title = opp.title if opp and opp.title else domain
    placement = opp.placement_type if opp else None
    score = opp.final_score if opp else None

    caption_lines = [
        f"<b>Backlink approval</b>  <code>{html_escape(alert_id)}</code>",
        f"<b>Workflow:</b> <code>{html_escape(workflow_id)}</code>",
    ]
    if niche_name:
        caption_lines.append(f"<b>Niche:</b> {html_escape(niche_name)}")
    if project_name:
        caption_lines.append(f"<b>Project:</b> {html_escape(project_name)}")
    if project_url:
        caption_lines.append(
            f'<b>Target:</b> <a href="{html_escape(project_url)}">{html_escape(project_url)}</a>'
        )
    caption_lines.append(f"<b>Site:</b> {html_escape(domain)}")
    caption_lines.append(f"<b>Title:</b> {html_escape(title)}")
    if url:
        caption_lines.append(f'<b>URL:</b> <a href="{html_escape(url)}">{html_escape(url)}</a>')
    if placement:
        caption_lines.append(f"<b>Placement:</b> {html_escape(str(placement))}")
    if score is not None:
        caption_lines.append(f"<b>Score:</b> {score:.1f}")

    if audit:
        audit_pass = "pass" if audit.pass_ else "fail"
        caption_lines.append(
            f"<b>Audit:</b> {audit_pass} ({audit.audit_score:.2f}) — feature image required"
        )

    if content_version:
        caption_lines.append(f"<b>Content v{content_version}</b>")
    if target_link:
        caption_lines.append(f"<b>Link:</b> {html_escape(target_link)}")
    if image_path and os.path.isfile(image_path):
        caption_lines.append(f"<b>Image:</b> {html_escape(image_path)}")
    elif image_error:
        caption_lines.append(
            f"<i>Image: failed ({html_escape(truncate(image_error, 80))}) — card sent without photo</i>"
        )

    caption_lines.append("")
    caption_lines.append(f"<pre>{html_escape(truncate(body_text, 450))}</pre>")

    expires = approval_expires_at or row.approval_expires_at or "not set"
    caption_lines.append(f"\n<i>Expires: {html_escape(str(expires))} UTC</i>")

    return {
        "workflow_id": workflow_id,
        "alert_id": alert_id,
        "caption": truncate("\n".join(caption_lines), TELEGRAM_CAPTION_MAX),
        "reply_markup": build_inline_keyboard(workflow_id),
        "content_id": content_id,
        "draft_id": draft.id if draft else None,
        "content_version": content_version,
        "image_path": image_path if image_path and os.path.isfile(image_path) else None,
        "image_error": image_error,
        "domain": domain,
        "url": url,
    }


def telegram_request(
    token: str,
    method: str,
    *,
    data: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = "----BacklinkBoundary"
        body_parts: list[bytes] = []
        for key, value in (data or {}).items():
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        for field, (filename, content, mime) in files.items():
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
            )
            body_parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
            body_parts.append(content)
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
        raise RuntimeError(result.get("description") or f"Telegram {method} failed")
    return result


def send_telegram_card(
    token: str,
    chat_id: str,
    caption: str,
    image_path: str | None,
    reply_markup: dict[str, Any],
) -> dict[str, Any]:
    markup_json = json.dumps(reply_markup)
    base_data: dict[str, str] = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "reply_markup": markup_json,
    }

    if image_path and os.path.isfile(image_path):
        size = os.path.getsize(image_path)
        if size > 10_000:
            mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            with open(image_path, "rb") as f:
                content = f.read()
            payload = {**base_data, "caption": caption}
            result = telegram_request(
                token,
                "sendPhoto",
                data=payload,
                files={"photo": (os.path.basename(image_path), content, mime)},
            )
            msg = result.get("result") or {}
            return {
                "telegram_chat_id": str(msg.get("chat", {}).get("id", chat_id)),
                "telegram_message_id": int(msg.get("message_id") or 0),
                "sent_as": "photo",
            }

    payload = {
        **base_data,
        "text": caption,
        "disable_web_page_preview": "false",
    }
    result = telegram_request(token, "sendMessage", data=payload)
    msg = result.get("result") or {}
    return {
        "telegram_chat_id": str(msg.get("chat", {}).get("id", chat_id)),
        "telegram_message_id": int(msg.get("message_id") or 0),
        "sent_as": "message",
    }


def default_sender(token: str, chat_id: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def _send(payload: dict[str, Any]) -> dict[str, Any]:
        return send_telegram_card(
            token,
            chat_id,
            payload["caption"],
            payload.get("image_path"),
            payload["reply_markup"],
        )

    return _send


def send_approval_card(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    dry_run: bool | None = None,
    chat_id: str | None = None,
    sender: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build and send approval card; set pending approval timestamps."""
    backlink_db.expire_stale_approvals(db_path=db_path)
    cfg = load_json(TELEGRAM_CONFIG)
    if dry_run is None:
        if os.environ.get("BACKLINK_TELEGRAM_DRY_RUN", "").lower() in {"1", "true", "yes"}:
            dry_run = True
        else:
            dry_run = bool(cfg.get("dry_run_default", True))

    row = workflow_manager.load(workflow_id, db_path=db_path)
    if not force and row.state == WorkflowState.PENDING_APPROVAL.value:
        pending = backlink_db.lookup_pending_approval(workflow_id, db_path=db_path)
        if pending and row.approval_expires_at:
            return {
                "success": True,
                "skipped": True,
                "reason": "approval_already_pending",
                "workflow_id": workflow_id,
                "approval_id": pending.id,
                "approval_expires_at": row.approval_expires_at,
            }

    pending_row = backlink_db.set_pending_approval(workflow_id, db_path=db_path)
    payload = build_card_payload(
        workflow_id,
        db_path=db_path,
        approval_expires_at=pending_row.approval_expires_at,
    )

    send_meta: dict[str, Any] = {}
    if dry_run and sender is None:
        send_meta = {"dry_run": True}
    else:
        try:
            token = load_bot_token()
            target_chat = chat_id or load_group_id()
            if not token or not target_chat:
                if sender is None:
                    send_meta = {
                        "dry_run": True,
                        "note": "missing bot token or group_id — card not sent",
                    }
                else:
                    send_meta = sender(payload)
            elif sender is not None:
                send_meta = sender(payload)
            else:
                send_meta = default_sender(token, target_chat)(payload)
        except (RuntimeError, urllib.error.URLError) as exc:
            send_meta = {"dry_run": True, "telegram_error": str(exc)}

    approval = backlink_db.create_approval(
        workflow_id,
        draft_id=payload.get("draft_id"),
        telegram_chat_id=send_meta.get("telegram_chat_id"),
        telegram_message_id=send_meta.get("telegram_message_id"),
        callback_data=json.dumps(payload["reply_markup"]),
        db_path=db_path,
    )

    backlink_db.insert_log(
        workflow_id,
        "telegram_card",
        "Approval card sent" if not send_meta.get("dry_run") else "Approval card built (dry run)",
        detail={
            "approval_id": approval.id,
            "content_version": payload.get("content_version"),
            **send_meta,
        },
        db_path=db_path,
    )

    return {
        "success": True,
        "workflow_id": workflow_id,
        "approval_id": approval.id,
        "alert_id": payload["alert_id"],
        "caption": payload["caption"],
        "reply_markup": payload["reply_markup"],
        **send_meta,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Send backlink approval card")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true", help="Force live Telegram send")
    args = parser.parse_args()

    dry_run = True
    if args.live:
        dry_run = False
    elif args.dry_run:
        dry_run = True

    try:
        result = send_approval_card(
            args.workflow_id,
            db_path=args.db,
            dry_run=dry_run,
        )
        print(json.dumps(result, indent=2))
        return 0
    except (KeyError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
