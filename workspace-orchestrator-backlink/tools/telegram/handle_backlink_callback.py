#!/usr/bin/env python3
"""Handle Telegram callbacks and edit replies for backlink approval cards."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))
sys.path.insert(0, str(_ROOT / "tools" / "telegram"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from send_backlink_card import send_approval_card  # noqa: E402
from states import WorkflowState  # noqa: E402

_RE_CALLBACK = re.compile(r"^bl_(approve|reject|edit|blacklist):(.+)$")
_RE_ALERT = re.compile(r"\bbl-([A-Za-z0-9_-]+)\b")
_RE_TEXT_APPROVE = re.compile(r"^(?:APPROVE|approve)\s*$")
_RE_TEXT_REJECT = re.compile(r"^(?:REJECT|reject)\s*$")
_RE_TEXT_EDIT = re.compile(r"^(?:EDIT|edit)\s*$")
_RE_TEXT_BLACKLIST = re.compile(r"^(?:BLACKLIST|blacklist)\s*$")


def parse_callback(callback_data: str) -> tuple[str, str] | None:
    match = _RE_CALLBACK.match(callback_data.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_alert_id(text: str) -> str | None:
    match = _RE_ALERT.search(text or "")
    return match.group(1) if match else None


def resolve_workflow_id(
    *,
    callback_data: str | None = None,
    text: str | None = None,
    reply_to_message_id: int | None = None,
    chat_id: str | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> str | None:
    if callback_data:
        parsed = parse_callback(callback_data)
        if parsed:
            return parsed[1]

    if text:
        alert_wid = parse_alert_id(text)
        if alert_wid:
            return alert_wid

    if chat_id and reply_to_message_id:
        approval = backlink_db.lookup_approval_by_telegram(
            chat_id,
            reply_to_message_id,
            db_path=db_path,
        )
        if approval:
            return approval.workflow_id

    return None


def _record_decision(
    workflow_id: str,
    decision: str,
    *,
    user_id: str | None,
    callback_data: str | None,
    db_path: str,
    edit_prompt: str | None = None,
    content_version_before: int | None = None,
    content_version_after: int | None = None,
) -> None:
    approval = backlink_db.lookup_pending_approval(workflow_id, db_path=db_path)
    if approval:
        backlink_db.record_approval_decision(
            approval.id,
            decision=decision,
            user_id=user_id,
            callback_data=callback_data,
            db_path=db_path,
        )
    backlink_db.insert_feedback_event(
        workflow_id,
        decision,
        user_id=user_id,
        edit_prompt=edit_prompt,
        content_version_before=content_version_before,
        content_version_after=content_version_after,
        raw_payload={"callback_data": callback_data} if callback_data else None,
        db_path=db_path,
    )


def handle_approve(
    workflow_id: str,
    *,
    user_id: str | None = None,
    callback_data: str | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state != WorkflowState.PENDING_APPROVAL.value:
        raise ValueError(f"Cannot approve workflow in state {row.state}")

    workflow_manager.transition(
        workflow_id,
        WorkflowState.APPROVED.value,
        agent="telegram_approval",
        detail={"decision": "approve", "user_id": user_id},
        db_path=db_path,
    )
    _record_decision(workflow_id, "approve", user_id=user_id, callback_data=callback_data, db_path=db_path)
    draft = backlink_db.get_latest_draft(workflow_id, db_path=db_path)
    if draft:
        backlink_db.mark_draft_approved(draft.id, db_path=db_path)
    content = backlink_db.get_latest_content_asset(workflow_id, db_path=db_path)
    if content:
        backlink_db.mark_content_approved(content.id, db_path=db_path)

    # Phase 1: record learning from approval (no publish required)
    try:
        from update_learning_weights import record_feedback_learning  # noqa: E402

        record_feedback_learning(workflow_id, action="approve", db_path=db_path)
    except (ImportError, ValueError):
        pass

    return {"action": "approve", "workflow_id": workflow_id, "state": WorkflowState.APPROVED.value}


def handle_reject(
    workflow_id: str,
    *,
    user_id: str | None = None,
    callback_data: str | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state != WorkflowState.PENDING_APPROVAL.value:
        raise ValueError(f"Cannot reject workflow in state {row.state}")

    workflow_manager.transition(
        workflow_id,
        WorkflowState.REJECTED.value,
        agent="telegram_approval",
        detail={"decision": "reject", "user_id": user_id},
        db_path=db_path,
    )
    workflow_manager.transition(
        workflow_id,
        WorkflowState.ARCHIVED.value,
        agent="telegram_approval",
        db_path=db_path,
    )
    _record_decision(workflow_id, "reject", user_id=user_id, callback_data=callback_data, db_path=db_path)
    try:
        from update_learning_weights import record_feedback_learning  # noqa: E402

        record_feedback_learning(workflow_id, action="reject", db_path=db_path)
    except (ImportError, ValueError):
        pass
    return {"action": "reject", "workflow_id": workflow_id, "state": WorkflowState.ARCHIVED.value}


def handle_edit_request(
    workflow_id: str,
    *,
    user_id: str | None = None,
    callback_data: str | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state != WorkflowState.PENDING_APPROVAL.value:
        raise ValueError(f"Cannot request edit for workflow in state {row.state}")

    workflow_manager.transition(
        workflow_id,
        WorkflowState.EDIT_REQUESTED.value,
        agent="telegram_approval",
        detail={"decision": "edit", "user_id": user_id},
        db_path=db_path,
    )
    backlink_db.upsert_edit_session(
        workflow_id,
        "awaiting_instructions",
        user_id=user_id,
        db_path=db_path,
    )
    _record_decision(workflow_id, "edit", user_id=user_id, callback_data=callback_data, db_path=db_path)
    return {
        "action": "edit",
        "workflow_id": workflow_id,
        "state": WorkflowState.EDIT_REQUESTED.value,
        "reply": "Reply with your edit instructions for this content.",
    }


def handle_blacklist(
    workflow_id: str,
    *,
    user_id: str | None = None,
    callback_data: str | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state != WorkflowState.PENDING_APPROVAL.value:
        raise ValueError(f"Cannot blacklist workflow in state {row.state}")

    domain = url = None
    if row.opportunity_id:
        opp = backlink_db.get_opportunity(row.opportunity_id, db_path=db_path)
        if opp:
            domain = opp.domain
            url = opp.url

    backlink_db.add_blacklist(
        domain=domain,
        url=url,
        reason=f"Blacklisted from approval card for {workflow_id}",
        db_path=db_path,
    )
    workflow_manager.transition(
        workflow_id,
        WorkflowState.REJECTED.value,
        agent="telegram_approval",
        detail={"decision": "blacklist", "user_id": user_id, "domain": domain},
        db_path=db_path,
    )
    workflow_manager.transition(
        workflow_id,
        WorkflowState.ARCHIVED.value,
        agent="telegram_approval",
        db_path=db_path,
    )
    _record_decision(workflow_id, "blacklist", user_id=user_id, callback_data=callback_data, db_path=db_path)
    return {
        "action": "blacklist",
        "workflow_id": workflow_id,
        "state": WorkflowState.ARCHIVED.value,
        "domain": domain,
    }


def apply_edit_instructions(
    workflow_id: str,
    instructions: str,
    *,
    user_id: str | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    redraft_fn: Any | None = None,
) -> dict[str, Any]:
    """Save edit instructions, redraft, and resend approval card."""
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state != WorkflowState.EDIT_REQUESTED.value:
        raise ValueError(f"Cannot apply edit for workflow in state {row.state}")

    session = backlink_db.get_edit_session(workflow_id, db_path=db_path)
    if session is None or session.state != "awaiting_instructions":
        raise ValueError("No edit session awaiting instructions")

    instructions = instructions.strip()
    if not instructions:
        raise ValueError("Edit instructions cannot be empty")

    backlink_db.upsert_edit_session(
        workflow_id,
        "redrafting",
        edit_prompt=instructions,
        user_id=user_id,
        db_path=db_path,
    )

    if redraft_fn is None:
        import workflow_driver  # noqa: E402

        redraft_fn = workflow_driver.run_step

    prior = backlink_db.get_latest_content_asset(workflow_id, db_path=db_path)
    version_before = prior.version if prior else None

    content_step = redraft_fn(workflow_id, db_path=db_path)
    if content_step.status != "ok" or content_step.action not in {"content", "drafting"}:
        raise RuntimeError(f"Content regeneration failed: {content_step.message}")

    card_step = redraft_fn(workflow_id, db_path=db_path)
    if card_step.status != "ok" or card_step.action != "send_approval_card":
        raise RuntimeError(f"Resend approval card failed: {card_step.message}")

    backlink_db.clear_edit_session(workflow_id, db_path=db_path)
    row = workflow_manager.load(workflow_id, db_path=db_path)
    content = backlink_db.get_latest_content_asset(workflow_id, db_path=db_path)
    draft = backlink_db.get_latest_draft(workflow_id, db_path=db_path)

    backlink_db.insert_feedback_event(
        workflow_id,
        "edit_applied",
        user_id=user_id,
        edit_prompt=instructions,
        content_version_before=version_before,
        content_version_after=content.version if content else None,
        raw_payload={"instructions": instructions},
        db_path=db_path,
    )
    try:
        from update_learning_weights import record_feedback_learning  # noqa: E402

        record_feedback_learning(workflow_id, action="edit", db_path=db_path)
    except (ImportError, ValueError):
        pass

    return {
        "action": "edit_applied",
        "workflow_id": workflow_id,
        "state": row.state,
        "content_version": content.version if content else (draft.version if draft else None),
        "instructions": instructions,
        "content_step": content_step.to_dict(),
        "card_step": card_step.to_dict(),
    }


def handle_callback(
    callback_data: str,
    *,
    user_id: str | None = None,
    chat_id: str | None = None,
    message_id: int | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    backlink_db.expire_stale_approvals(db_path=db_path)
    parsed = parse_callback(callback_data)
    if not parsed:
        raise ValueError(f"Unknown callback: {callback_data!r}")

    action, workflow_id = parsed
    handlers = {
        "approve": handle_approve,
        "reject": handle_reject,
        "edit": handle_edit_request,
        "blacklist": handle_blacklist,
    }
    handler = handlers[action]
    result = handler(
        workflow_id,
        user_id=user_id,
        callback_data=callback_data,
        db_path=db_path,
    )
    result["chat_id"] = chat_id
    result["message_id"] = message_id
    return result


def handle_text(
    text: str,
    *,
    user_id: str | None = None,
    chat_id: str | None = None,
    reply_to_message_id: int | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    backlink_db.expire_stale_approvals(db_path=db_path)
    workflow_id = resolve_workflow_id(
        text=text,
        reply_to_message_id=reply_to_message_id,
        chat_id=chat_id,
        db_path=db_path,
    )
    if not workflow_id:
        raise ValueError("Could not resolve workflow from text/reply")

    stripped = text.strip()
    if _RE_TEXT_APPROVE.match(stripped):
        return handle_approve(workflow_id, user_id=user_id, db_path=db_path)
    if _RE_TEXT_REJECT.match(stripped):
        return handle_reject(workflow_id, user_id=user_id, db_path=db_path)
    if _RE_TEXT_EDIT.match(stripped):
        return handle_edit_request(workflow_id, user_id=user_id, db_path=db_path)
    if _RE_TEXT_BLACKLIST.match(stripped):
        return handle_blacklist(workflow_id, user_id=user_id, db_path=db_path)

    session = backlink_db.get_edit_session(workflow_id, db_path=db_path)
    if session and session.state == "awaiting_instructions":
        return apply_edit_instructions(
            workflow_id,
            stripped,
            user_id=user_id,
            db_path=db_path,
        )

    raise ValueError(f"No handler for text in workflow {workflow_id}")


def dispatch(
    *,
    callback_data: str | None = None,
    text: str | None = None,
    user_id: str | None = None,
    chat_id: str | None = None,
    message_id: int | None = None,
    reply_to_message_id: int | None = None,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if callback_data:
        return handle_callback(
            callback_data,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            db_path=db_path,
        )
    if text:
        return handle_text(
            text,
            user_id=user_id,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            db_path=db_path,
        )
    raise ValueError("Provide callback_data or text")


def main() -> int:
    parser = argparse.ArgumentParser(description="Handle backlink Telegram feedback")
    parser.add_argument("--callback-data")
    parser.add_argument("--text")
    parser.add_argument("--user-id")
    parser.add_argument("--chat-id")
    parser.add_argument("--message-id", type=int)
    parser.add_argument("--reply-to-message-id", type=int)
    parser.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)
    args = parser.parse_args()

    try:
        result = dispatch(
            callback_data=args.callback_data,
            text=args.text,
            user_id=args.user_id,
            chat_id=args.chat_id,
            message_id=args.message_id,
            reply_to_message_id=args.reply_to_message_id,
            db_path=args.db,
        )
        print(json.dumps(result, indent=2))
        return 0
    except (KeyError, ValueError, RuntimeError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
