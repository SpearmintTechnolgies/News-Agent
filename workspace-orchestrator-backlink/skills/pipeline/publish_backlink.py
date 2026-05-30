#!/usr/bin/env python3
"""publish_backlink.py — submit approved drafts via Playwright (dry-run capable)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from generate_draft import load_campaign_config  # noqa: E402
from states import WorkflowState  # noqa: E402
from tools.parser.parser import parse  # noqa: E402

PUBLISH_CONFIG = _ROOT / "config" / "publish_config.json"

OUTCOME_PUBLISHED = WorkflowState.PUBLISHED.value
OUTCOME_PENDING = WorkflowState.PENDING_MODERATION.value
OUTCOME_REJECTED = WorkflowState.SUBMISSION_REJECTED.value

MODERATION_PHRASES = (
    "pending review",
    "awaiting moderation",
    "thank you for your submission",
    "we will review",
)

REJECTION_PHRASES = (
    "submission rejected",
    "not accepted",
    "spam detected",
    "error submitting",
)


@dataclass
class PublishResult:
    outcome_state: str
    published_url: str | None
    status: str
    dry_run: bool
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _env_truthy(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def is_dry_run(config: dict[str, Any] | None = None) -> bool:
    env = _env_truthy("BACKLINK_PUBLISH_DRY_RUN")
    if env is not None:
        return env
    cfg = config or _load_publish_config()
    return bool(cfg.get("dry_run_default", True))


def _load_publish_config() -> dict[str, Any]:
    if not PUBLISH_CONFIG.is_file():
        return {}
    try:
        data = json.loads(PUBLISH_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _draft_body_for_submit(draft_text: str) -> str:
    lines = draft_text.splitlines()
    if lines and lines[0].lower().startswith("title:"):
        return "\n".join(lines[1:]).strip()
    return draft_text.strip()


def _infer_outcome(html: str, final_url: str) -> str:
    haystack = f"{html}\n{final_url}".lower()
    if any(p in haystack for p in REJECTION_PHRASES):
        return OUTCOME_REJECTED
    if any(p in haystack for p in MODERATION_PHRASES):
        return OUTCOME_PENDING
    return OUTCOME_PUBLISHED


def _dry_run_publish(
    workflow_id: str,
    *,
    opportunity_url: str,
    placement_type: str,
    target_url: str,
) -> PublishResult:
    published_url = f"{opportunity_url.rstrip('/')}?backlink_dry_run={workflow_id}"
    return PublishResult(
        outcome_state=OUTCOME_PUBLISHED,
        published_url=published_url,
        status="dry_run_published",
        dry_run=True,
        detail={
            "note": "Simulated publish — set BACKLINK_PUBLISH_DRY_RUN=0 and run playwright install for live",
            "placement_type": placement_type,
            "target_url": target_url,
        },
    )


def _live_publish_guest_post(
    url: str,
    draft_body: str,
    *,
    timeout_ms: int = 45_000,
) -> tuple[str, str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            html = page.content()
            parsed = parse(html, base_url=page.url)

            target_form = None
            for form in parsed.forms:
                if any(f.field_type == "textarea" for f in form.fields):
                    target_form = form
                    break
            if target_form is None and parsed.forms:
                target_form = parsed.forms[0]
            if target_form is None:
                raise RuntimeError("No submission form found on page")

            form_el = page.locator("form").first
            textarea = page.locator("textarea").first
            if textarea.count() > 0:
                textarea.fill(draft_body[:8000])
            else:
                text_input = page.locator("input[type='text'], input:not([type])").first
                if text_input.count() > 0:
                    text_input.fill(draft_body[:1000])

            submit = page.locator(
                "form button[type='submit'], form input[type='submit'], form button"
            ).first
            if submit.count() == 0:
                raise RuntimeError("No submit control found on form")
            submit.click()
            page.wait_for_timeout(2000)
            final_html = page.content()
            final_url = page.url
        finally:
            browser.close()

    return final_html, final_url, _infer_outcome(final_html, final_url)


def publish_backlink(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    dry_run: bool | None = None,
) -> PublishResult:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state != WorkflowState.APPROVED.value:
        raise ValueError(f"Cannot publish workflow in state {row.state}")

    if not row.opportunity_id:
        raise ValueError(f"Workflow has no opportunity: {workflow_id}")

    opp = backlink_db.get_opportunity(row.opportunity_id, db_path=db_path)
    if not opp:
        raise ValueError(f"Opportunity not found for workflow: {workflow_id}")

    draft = backlink_db.get_latest_draft(workflow_id, db_path=db_path)
    if not draft:
        raise ValueError(f"No draft saved for workflow: {workflow_id}")

    existing = backlink_db.get_backlink(workflow_id, db_path=db_path)
    if existing and existing.published_url and existing.status not in {None, "publish_failed"}:
        outcome = OUTCOME_PUBLISHED
        if existing.status == "comment_manual_required":
            outcome = OUTCOME_PENDING
        return PublishResult(
            outcome_state=outcome,
            published_url=existing.published_url,
            status=existing.status or "published",
            dry_run=bool("dry_run" in (existing.published_url or "")),
            detail={"idempotent": True, "note": "Publish skipped — backlink already recorded"},
        )

    config = load_campaign_config()
    target_url = str(config.get("target_url") or "https://cryptography.com")
    publish_cfg = _load_publish_config()
    use_dry_run = is_dry_run(publish_cfg) if dry_run is None else dry_run
    placement_type = opp.placement_type or "guest_post"
    draft_body = _draft_body_for_submit(draft.draft_text)

    if use_dry_run:
        result = _dry_run_publish(
            workflow_id,
            opportunity_url=opp.url,
            placement_type=placement_type,
            target_url=target_url,
        )
    elif placement_type == "comment":
        result = PublishResult(
            outcome_state=OUTCOME_PENDING,
            published_url=opp.url,
            status="comment_manual_required",
            dry_run=False,
            detail={
                "note": "Comment placement requires manual publish in V1",
                "placement_type": placement_type,
            },
        )
    else:
        try:
            html, final_url, outcome = _live_publish_guest_post(opp.url, draft_body)
            result = PublishResult(
                outcome_state=outcome,
                published_url=final_url,
                status="live_published",
                dry_run=False,
                detail={"placement_type": placement_type, "html_length": len(html)},
            )
        except Exception as exc:  # noqa: BLE001
            result = PublishResult(
                outcome_state=OUTCOME_REJECTED,
                published_url=None,
                status="publish_failed",
                dry_run=False,
                detail={"error": str(exc), "placement_type": placement_type},
            )

    backlink_db.upsert_backlink(
        workflow_id,
        opportunity_id=opp.id,
        published_url=result.published_url,
        status=result.status,
        verified=False,
        db_path=db_path,
    )
    backlink_db.insert_log(
        workflow_id,
        "publisher",
        f"Publish outcome: {result.outcome_state}",
        detail=result.to_dict(),
        db_path=db_path,
    )
    return result
