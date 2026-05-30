#!/usr/bin/env python3
"""verify_backlink.py — confirm target link is visible on published page."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

VERIFY_RETRY_HOURS = 24


@dataclass
class VerifyFetchResult:
    check_url: str
    html_length: int
    dry_run: bool
    fetch_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerifyResult:
    verified: bool
    outcome_state: str
    target_domain: str
    check_url: str
    match_count: int
    dry_run: bool
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _env_truthy(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _target_domain(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_campaign_config()
    domain = str(cfg.get("target_domain") or "cryptography.com").lower()
    return domain.lstrip(".")


def _is_dry_run_url(url: str) -> bool:
    return "backlink_dry_run=" in url


def fetch_verify_page(
    url: str,
    *,
    dry_run: bool | None = None,
) -> tuple[str | None, VerifyFetchResult]:
    use_dry_run = dry_run
    if use_dry_run is None:
        env = _env_truthy("BACKLINK_PUBLISH_DRY_RUN")
        use_dry_run = env if env is not None else _is_dry_run_url(url)

    if use_dry_run:
        domain = _target_domain()
        html = (
            f"<html><body><p>Simulated published page for verification.</p>"
            f'<a href="https://{domain}/">Read more on {domain}</a></body></html>'
        )
        return html, VerifyFetchResult(
            check_url=url,
            html_length=len(html),
            dry_run=True,
        )

    try:
        from tools.page_fetch.page_fetch import page_fetch  # noqa: E402

        fetched = page_fetch(url)
        return fetched.html, VerifyFetchResult(
            check_url=fetched.final_url or url,
            html_length=len(fetched.html),
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        return None, VerifyFetchResult(
            check_url=url,
            html_length=0,
            dry_run=False,
            fetch_error=str(exc),
        )


def link_visible_in_html(html: str, target_domain: str) -> int:
    pattern = re.compile(re.escape(target_domain), re.I)
    return len(pattern.findall(html))


def verify_backlink(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    html: str | None = None,
    check_url: str | None = None,
) -> VerifyResult:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if row.state not in {
        WorkflowState.VERIFYING.value,
        WorkflowState.PUBLISHED.value,
        WorkflowState.PENDING_MODERATION.value,
    }:
        raise ValueError(f"Cannot verify workflow in state {row.state}")

    backlink = backlink_db.get_backlink(workflow_id, db_path=db_path)
    if not backlink or not backlink.published_url:
        raise ValueError(f"No published URL recorded for workflow: {workflow_id}")

    target = _target_domain()
    url = check_url or backlink.published_url
    dry_run = _is_dry_run_url(url)

    if html is None:
        html, fetch_meta = fetch_verify_page(url, dry_run=dry_run)
        if html is None:
            backlink_db.set_next_verify_at(
                workflow_id,
                hours=VERIFY_RETRY_HOURS,
                db_path=db_path,
            )
            return VerifyResult(
                verified=False,
                outcome_state=WorkflowState.VERIFY_PENDING.value,
                target_domain=target,
                check_url=url,
                match_count=0,
                dry_run=dry_run,
                detail={"fetch_error": fetch_meta.fetch_error},
            )
        check_url = fetch_meta.check_url
        dry_run = fetch_meta.dry_run

    matches = link_visible_in_html(html, target)
    verified = matches > 0
    outcome = (
        WorkflowState.VERIFIED.value
        if verified
        else WorkflowState.VERIFY_PENDING.value
    )

    backlink_db.upsert_backlink(
        workflow_id,
        published_url=check_url,
        status="verified" if verified else "verify_pending",
        verified=verified,
        verified_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if verified else None,
        db_path=db_path,
    )
    if not verified:
        backlink_db.set_next_verify_at(
            workflow_id,
            hours=VERIFY_RETRY_HOURS,
            db_path=db_path,
        )

    return VerifyResult(
        verified=verified,
        outcome_state=outcome,
        target_domain=target,
        check_url=check_url or url,
        match_count=matches,
        dry_run=dry_run,
        detail={"verified": verified},
    )


def verify_fetch_workflow(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> tuple[VerifyFetchResult, str | None]:
    backlink = backlink_db.get_backlink(workflow_id, db_path=db_path)
    if not backlink or not backlink.published_url:
        raise ValueError(f"No published URL for workflow: {workflow_id}")

    html, meta = fetch_verify_page(backlink.published_url)
    backlink_db.insert_log(
        workflow_id,
        "verify_fetch",
        "Fetched page for verification",
        detail=meta.to_dict(),
        db_path=db_path,
    )
    return meta, html
