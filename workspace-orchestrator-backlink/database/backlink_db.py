#!/usr/bin/env python3
"""backlink_db.py — SQLite store for backlink workflow state."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/data/backlink_agent.db")
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
APPROVAL_TTL_HOURS = 48


@dataclass
class DraftRow:
    id: int
    workflow_id: str
    version: int
    draft_text: str
    tone: str | None
    confidence: float | None
    approved: int


@dataclass
class ApprovalRow:
    id: int
    workflow_id: str
    draft_id: int | None
    telegram_chat_id: str | None
    telegram_message_id: int | None
    decision: str | None
    callback_data: str | None
    user_id: str | None


@dataclass
class EditSessionRow:
    workflow_id: str
    state: str
    edit_prompt: str | None
    user_id: str | None


@dataclass
class WorkflowRow:
    id: int
    workflow_id: str
    campaign_id: int | None
    state: str
    current_agent: str | None
    last_error: str | None
    retry_count: int
    project_id: int | None = None
    opportunity_id: int | None = None
    pending_approval_at: str | None = None
    approval_expires_at: str | None = None
    next_verify_at: str | None = None


@dataclass
class BacklinkRow:
    id: int
    workflow_id: str
    opportunity_id: int | None
    published_url: str | None
    status: str | None
    verified: int
    verified_at: str | None
    screenshot_path: str | None


@dataclass
class OpportunityRow:
    id: int
    campaign_id: int | None
    url: str
    normalized_url: str
    url_hash: str
    domain: str | None
    title: str | None
    snippet: str | None
    status: str
    project_id: int | None = None
    placement_type: str | None = None
    final_score: float | None = None
    context_json: str | None = None


@dataclass
class NicheRow:
    id: int
    name: str
    slug: str
    status: str
    search_queries_json: str | None = None
    config_json: str | None = None


@dataclass
class ProjectRow:
    id: int
    niche_id: int
    name: str
    target_domain: str
    target_url: str | None
    brand_name: str | None
    status: str
    config_json: str | None = None


@dataclass
class AuditRow:
    id: int
    workflow_id: str
    opportunity_id: int | None
    audit_json: str
    placement_type: str | None
    image_required: int
    audit_score: float | None
    pass_: int


@dataclass
class ContentAssetRow:
    id: int
    workflow_id: str
    version: int
    content_text: str
    target_link: str | None
    image_url: str | None
    image_local_path: str | None
    image_prompt: str | None
    content_type: str | None
    confidence: float | None
    approved: int


@dataclass
class FeedbackEventRow:
    id: int
    workflow_id: str
    action: str
    user_id: str | None
    edit_prompt: str | None
    content_version_before: int | None
    content_version_after: int | None


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/") or ""
    return f"{parsed.scheme}://{parsed.netloc.lower()}{path}"


def url_hash(url: str) -> str:
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def domain_from_url(url: str) -> str | None:
    parsed = urlparse(url.strip())
    return parsed.netloc.lower() or None


def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    workflow_cols = {
        "opportunity_id": "INTEGER REFERENCES opportunities(id)",
        "pending_approval_at": "TEXT",
        "approval_expires_at": "TEXT",
        "next_verify_at": "TEXT",
    }
    for column, typedef in workflow_cols.items():
        if not _column_exists(conn, "workflows", column):
            conn.execute(f"ALTER TABLE workflows ADD COLUMN {column} {typedef}")

    opportunity_cols = {
        "campaign_id": "INTEGER REFERENCES campaigns(id)",
        "normalized_url": "TEXT",
        "url_hash": "TEXT",
    }
    for column, typedef in opportunity_cols.items():
        if not _column_exists(conn, "opportunities", column):
            conn.execute(f"ALTER TABLE opportunities ADD COLUMN {column} {typedef}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edit_sessions (
          workflow_id TEXT PRIMARY KEY REFERENCES workflows(workflow_id),
          state TEXT NOT NULL,
          edit_prompt TEXT,
          user_id TEXT,
          updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Add Phase 1 tables and nullable project_id columns."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS niches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          slug TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'active',
          search_queries_json TEXT,
          config_json TEXT,
          created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS projects (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          niche_id INTEGER NOT NULL REFERENCES niches(id),
          name TEXT NOT NULL,
          target_domain TEXT NOT NULL,
          target_url TEXT,
          brand_name TEXT,
          status TEXT NOT NULL DEFAULT 'active',
          config_json TEXT,
          created_at TEXT DEFAULT (datetime('now')),
          UNIQUE(niche_id, target_domain)
        );
        CREATE TABLE IF NOT EXISTS audits (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
          opportunity_id INTEGER REFERENCES opportunities(id),
          audit_json TEXT NOT NULL,
          placement_type TEXT,
          image_required INTEGER NOT NULL DEFAULT 0,
          audit_score REAL,
          pass INTEGER NOT NULL DEFAULT 0,
          created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS content_assets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
          version INTEGER NOT NULL DEFAULT 1,
          content_text TEXT NOT NULL,
          target_link TEXT,
          image_url TEXT,
          image_local_path TEXT,
          image_prompt TEXT,
          content_type TEXT,
          confidence REAL,
          approved INTEGER NOT NULL DEFAULT 0,
          created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS feedback_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
          action TEXT NOT NULL,
          user_id TEXT,
          edit_prompt TEXT,
          content_version_before INTEGER,
          content_version_after INTEGER,
          raw_payload_json TEXT,
          created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )

    for table, column, typedef in (
        ("opportunities", "project_id", "INTEGER REFERENCES projects(id)"),
        ("workflows", "project_id", "INTEGER REFERENCES projects(id)"),
    ):
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")

    # Relax campaign_id NOT NULL on opportunities for migrated DBs (SQLite cannot ALTER)
    # New installs use nullable campaign_id in schema.sql.


def _seed_default_niche_project(conn: sqlite3.Connection) -> None:
    """Ensure default crypto/cryptography.com project exists from campaigns."""
    niche = conn.execute(
        "SELECT id FROM niches WHERE slug = ?",
        ("crypto",),
    ).fetchone()
    if not niche:
        conn.execute(
            """
            INSERT INTO niches (name, slug, status)
            VALUES ('Crypto', 'crypto', 'active')
            """
        )
        niche_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    else:
        niche_id = int(niche["id"])

    campaign = conn.execute(
        "SELECT id, name, target_domain FROM campaigns ORDER BY id LIMIT 1"
    ).fetchone()
    if not campaign:
        return

    project = conn.execute(
        "SELECT id FROM projects WHERE niche_id = ? AND target_domain = ?",
        (niche_id, campaign["target_domain"]),
    ).fetchone()
    if not project:
        conn.execute(
            """
            INSERT INTO projects (niche_id, name, target_domain, target_url, brand_name, status)
            VALUES (?, ?, ?, ?, ?, 'active')
            """,
            (
                niche_id,
                campaign["name"],
                campaign["target_domain"],
                f"https://{campaign['target_domain']}",
                campaign["name"],
            ),
        )
        project_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    else:
        project_id = int(project["id"])

    conn.execute(
        "UPDATE opportunities SET project_id = ? WHERE project_id IS NULL AND campaign_id = ?",
        (project_id, campaign["id"]),
    )
    conn.execute(
        "UPDATE workflows SET project_id = ? WHERE project_id IS NULL AND campaign_id = ?",
        (project_id, campaign["id"]),
    )


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    with _connect(db_path) as conn:
        conn.executescript(schema)
        _migrate_v2(conn)
        _migrate_v3(conn)
        _seed_default_niche_project(conn)
        conn.commit()


def ensure_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create or migrate an existing SQLite db without re-running full schema.sql."""
    if not os.path.exists(db_path):
        init_db(db_path)
        return
    with _connect(db_path) as conn:
        _migrate_v2(conn)
        _migrate_v3(conn)
        _seed_default_niche_project(conn)
        conn.commit()


def get_or_create_campaign(
    name: str,
    target_domain: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM campaigns WHERE name = ? AND target_domain = ?",
            (name, target_domain),
        ).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            """
            INSERT INTO campaigns (name, target_domain)
            VALUES (?, ?)
            """,
            (name, target_domain),
        )
        conn.commit()
        return int(cur.lastrowid)


def find_opportunity_by_url_hash(
    campaign_id: int,
    url_hash_value: str,
    db_path: str = DEFAULT_DB_PATH,
) -> OpportunityRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM opportunities
            WHERE campaign_id = ? AND url_hash = ?
            """,
            (campaign_id, url_hash_value),
        ).fetchone()
    return _row_to_opportunity(row) if row else None


def _find_opportunity_by_project_hash(
    project_id: int,
    url_hash_value: str,
    db_path: str = DEFAULT_DB_PATH,
) -> OpportunityRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM opportunities
            WHERE project_id = ? AND url_hash = ?
            """,
            (project_id, url_hash_value),
        ).fetchone()
    return _row_to_opportunity(row) if row else None


def create_opportunity(
    campaign_id: int | None,
    url: str,
    *,
    project_id: int | None = None,
    workflow_id: str | None = None,
    title: str | None = None,
    snippet: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> OpportunityRow:
    normalized = normalize_url(url)
    hash_value = url_hash(url)
    scope_id = project_id or campaign_id
    if scope_id is None:
        raise ValueError("create_opportunity requires campaign_id or project_id")

    if project_id is not None:
        existing = _find_opportunity_by_project_hash(project_id, hash_value, db_path=db_path)
    else:
        existing = find_opportunity_by_url_hash(campaign_id, hash_value, db_path=db_path)  # type: ignore[arg-type]
    if existing:
        raise ValueError(f"Opportunity already exists for url_hash={hash_value}")

    domain = domain_from_url(url)
    with _connect(db_path) as conn:
        if _column_exists(conn, "opportunities", "workflow_id"):
            cur = conn.execute(
                """
                INSERT INTO opportunities (
                  workflow_id, campaign_id, project_id, url, normalized_url, url_hash,
                  domain, title, snippet
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id or "",
                    campaign_id,
                    project_id,
                    url.strip(),
                    normalized,
                    hash_value,
                    domain,
                    title,
                    snippet,
                ),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO opportunities (
                  campaign_id, project_id, url, normalized_url, url_hash, domain, title, snippet
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (campaign_id, project_id, url.strip(), normalized, hash_value, domain, title, snippet),
            )
        row = conn.execute(
            "SELECT * FROM opportunities WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_opportunity(row)


def create_workflow(
    workflow_id: str,
    *,
    campaign_id: int | None = None,
    project_id: int | None = None,
    opportunity_id: int | None = None,
    state: str = "NEW",
    db_path: str = DEFAULT_DB_PATH,
) -> WorkflowRow:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO workflows (workflow_id, campaign_id, project_id, opportunity_id, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (workflow_id, campaign_id, project_id, opportunity_id, state),
        )
        row = conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        conn.commit()
    assert row is not None
    insert_log(workflow_id, "create", f"Workflow created in state {state}", db_path=db_path)
    return _row_to_workflow(row)


def create_opportunity_and_workflow(
    campaign_id: int,
    url: str,
    workflow_id: str,
    *,
    title: str | None = None,
    snippet: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[OpportunityRow, WorkflowRow]:
    """Create opportunity then workflow. Raises if duplicate url_hash for campaign."""
    with _connect(db_path) as conn:
        legacy_fk = _column_exists(conn, "opportunities", "workflow_id")

    if legacy_fk:
        wf = create_workflow(
            workflow_id,
            campaign_id=campaign_id,
            db_path=db_path,
        )
        opp = create_opportunity(
            campaign_id,
            url,
            workflow_id=workflow_id,
            title=title,
            snippet=snippet,
            db_path=db_path,
        )
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE workflows SET opportunity_id = ? WHERE workflow_id = ?",
                (opp.id, workflow_id),
            )
            conn.commit()
        wf = get_workflow(workflow_id, db_path=db_path)
        assert wf is not None
        return opp, wf

    opp = create_opportunity(
        campaign_id,
        url,
        workflow_id=workflow_id,
        title=title,
        snippet=snippet,
        db_path=db_path,
    )
    wf = create_workflow(
        workflow_id,
        campaign_id=campaign_id,
        opportunity_id=opp.id,
        db_path=db_path,
    )
    return opp, wf


def get_opportunity(opportunity_id: int, db_path: str = DEFAULT_DB_PATH) -> OpportunityRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM opportunities WHERE id = ?",
            (opportunity_id,),
        ).fetchone()
    return _row_to_opportunity(row) if row else None


def set_pending_approval(
    workflow_id: str,
    *,
    ttl_hours: int = APPROVAL_TTL_HOURS,
    db_path: str = DEFAULT_DB_PATH,
) -> WorkflowRow:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE workflows
            SET pending_approval_at = datetime('now'),
                approval_expires_at = datetime('now', ?),
                updated_at = datetime('now')
            WHERE workflow_id = ?
            """,
            (f"+{ttl_hours} hours", workflow_id),
        )
        row = conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        conn.commit()
    if row is None:
        raise KeyError(f"Workflow not found: {workflow_id}")
    return _row_to_workflow(row)


def expire_stale_approvals(db_path: str = DEFAULT_DB_PATH) -> list[str]:
    """Move PENDING_APPROVAL workflows past expiry to APPROVAL_EXPIRED, then ARCHIVED."""
    expired_ids: list[str] = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT workflow_id FROM workflows
            WHERE state = 'PENDING_APPROVAL'
              AND approval_expires_at IS NOT NULL
              AND approval_expires_at <= datetime('now')
            """
        ).fetchall()
        for row in rows:
            wid = str(row["workflow_id"])
            conn.execute(
                """
                UPDATE workflows
                SET state = 'APPROVAL_EXPIRED', updated_at = datetime('now')
                WHERE workflow_id = ?
                """,
                (wid,),
            )
            conn.execute(
                """
                UPDATE workflows
                SET state = 'ARCHIVED', updated_at = datetime('now')
                WHERE workflow_id = ?
                """,
                (wid,),
            )
            expired_ids.append(wid)
        conn.commit()
    for wid in expired_ids:
        insert_log(wid, "expire_approval", "Approval expired after 48h", db_path=db_path)
    return expired_ids


def set_next_verify_at(
    workflow_id: str,
    *,
    hours: int = 24,
    db_path: str = DEFAULT_DB_PATH,
) -> WorkflowRow:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE workflows
            SET next_verify_at = datetime('now', ?),
                updated_at = datetime('now')
            WHERE workflow_id = ?
            """,
            (f"+{hours} hours", workflow_id),
        )
        row = conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        conn.commit()
    if row is None:
        raise KeyError(f"Workflow not found: {workflow_id}")
    return _row_to_workflow(row)


def save_parsed_page(
    url: str,
    *,
    title: str | None = None,
    content_hash: str | None = None,
    signals: dict[str, Any] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    normalized = normalize_url(url)
    hash_value = url_hash(url)
    signals_json = json.dumps(signals, ensure_ascii=False) if signals else None
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO parsed_pages (url, normalized_url, url_hash, title, content_hash, signals_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url_hash) DO UPDATE SET
              url = excluded.url,
              normalized_url = excluded.normalized_url,
              title = excluded.title,
              content_hash = excluded.content_hash,
              signals_json = excluded.signals_json,
              parsed_at = datetime('now')
            """,
            (url.strip(), normalized, hash_value, title, content_hash, signals_json),
        )
        conn.commit()


def get_parsed_page(url: str, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    hash_value = url_hash(url)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM parsed_pages WHERE url_hash = ?",
            (hash_value,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    if result.get("signals_json"):
        result["signals"] = json.loads(result["signals_json"])
    return result


def insert_log(
    workflow_id: str,
    step: str,
    message: str,
    *,
    level: str = "info",
    detail: dict[str, Any] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO logs (workflow_id, step, level, message, detail_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (workflow_id, step, level, message, detail_json),
        )
        conn.commit()


def get_workflow(workflow_id: str, db_path: str = DEFAULT_DB_PATH) -> WorkflowRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
    return _row_to_workflow(row) if row else None


def list_workflows(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT w.workflow_id, w.state, w.opportunity_id, o.url, w.updated_at
            FROM workflows w
            LEFT JOIN opportunities o ON o.id = w.opportunity_id
            ORDER BY w.updated_at DESC, w.workflow_id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def save_draft(
    workflow_id: str,
    draft_text: str,
    *,
    tone: str | None = None,
    confidence: float | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> DraftRow:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS max_version
            FROM drafts WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        version = int(row["max_version"]) + 1 if row else 1
        cur = conn.execute(
            """
            INSERT INTO drafts (workflow_id, version, draft_text, tone, confidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (workflow_id, version, draft_text, tone, confidence),
        )
        draft_row = conn.execute(
            "SELECT * FROM drafts WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        conn.commit()
    assert draft_row is not None
    return _row_to_draft(draft_row)


def get_latest_draft(
    workflow_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> DraftRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM drafts
            WHERE workflow_id = ?
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
            (workflow_id,),
        ).fetchone()
    return _row_to_draft(row) if row else None


def mark_draft_approved(
    draft_id: int,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE drafts SET approved = 1 WHERE id = ?",
            (draft_id,),
        )
        conn.commit()


def update_opportunity_fields(
    opportunity_id: int,
    *,
    title: str | None = None,
    snippet: str | None = None,
    placement_type: str | None = None,
    placement_allowed: bool | None = None,
    relevance_score: float | None = None,
    spam_risk_score: float | None = None,
    final_score: float | None = None,
    status: str | None = None,
    context_json: dict[str, Any] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    fields: list[str] = ["updated_at = datetime('now')"]
    params: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        params.append(title)
    if snippet is not None:
        fields.append("snippet = ?")
        params.append(snippet)
    if placement_type is not None:
        fields.append("placement_type = ?")
        params.append(placement_type)
    if placement_allowed is not None:
        fields.append("placement_allowed = ?")
        params.append(1 if placement_allowed else 0)
    if relevance_score is not None:
        fields.append("relevance_score = ?")
        params.append(relevance_score)
    if spam_risk_score is not None:
        fields.append("spam_risk_score = ?")
        params.append(spam_risk_score)
    if final_score is not None:
        fields.append("final_score = ?")
        params.append(final_score)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if context_json is not None:
        fields.append("context_json = ?")
        params.append(json.dumps(context_json, ensure_ascii=False))
    if len(fields) == 1:
        return
    params.append(opportunity_id)
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE opportunities SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        conn.commit()


def create_approval(
    workflow_id: str,
    *,
    draft_id: int | None = None,
    telegram_chat_id: str | None = None,
    telegram_message_id: int | None = None,
    callback_data: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> ApprovalRow:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO approvals (
              workflow_id, draft_id, telegram_chat_id, telegram_message_id, callback_data
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (workflow_id, draft_id, telegram_chat_id, telegram_message_id, callback_data),
        )
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_approval(row)


def record_approval_decision(
    approval_id: int,
    *,
    decision: str,
    user_id: str | None = None,
    callback_data: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> ApprovalRow:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE approvals
            SET decision = ?, user_id = ?, callback_data = COALESCE(?, callback_data),
                decided_at = datetime('now')
            WHERE id = ?
            """,
            (decision, user_id, callback_data, approval_id),
        )
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        conn.commit()
    if row is None:
        raise KeyError(f"Approval not found: {approval_id}")
    return _row_to_approval(row)


def lookup_approval_by_telegram(
    telegram_chat_id: str,
    telegram_message_id: int,
    db_path: str = DEFAULT_DB_PATH,
) -> ApprovalRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM approvals
            WHERE telegram_chat_id = ? AND telegram_message_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (telegram_chat_id, telegram_message_id),
        ).fetchone()
    return _row_to_approval(row) if row else None


def lookup_pending_approval(
    workflow_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> ApprovalRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM approvals
            WHERE workflow_id = ? AND decision IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (workflow_id,),
        ).fetchone()
    return _row_to_approval(row) if row else None


def add_blacklist(
    *,
    domain: str | None = None,
    url: str | None = None,
    reason: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO blacklist (domain, url, reason)
            VALUES (?, ?, ?)
            """,
            (domain, url, reason),
        )
        conn.commit()


def upsert_edit_session(
    workflow_id: str,
    state: str,
    *,
    edit_prompt: str | None = None,
    user_id: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> EditSessionRow:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO edit_sessions (workflow_id, state, edit_prompt, user_id, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(workflow_id) DO UPDATE SET
              state = excluded.state,
              edit_prompt = COALESCE(excluded.edit_prompt, edit_sessions.edit_prompt),
              user_id = COALESCE(excluded.user_id, edit_sessions.user_id),
              updated_at = datetime('now')
            """,
            (workflow_id, state, edit_prompt, user_id),
        )
        row = conn.execute(
            "SELECT * FROM edit_sessions WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_edit_session(row)


def get_edit_session(
    workflow_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> EditSessionRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM edit_sessions WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
    return _row_to_edit_session(row) if row else None


def clear_edit_session(
    workflow_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM edit_sessions WHERE workflow_id = ?",
            (workflow_id,),
        )
        conn.commit()


def update_workflow_state(
    workflow_id: str,
    state: str,
    *,
    current_agent: str | None = None,
    last_error: str | None = None,
    retry_count: int | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> WorkflowRow:
    fields: list[str] = ["state = ?", "updated_at = datetime('now')"]
    params: list[Any] = [state]

    if current_agent is not None:
        fields.append("current_agent = ?")
        params.append(current_agent)
    if last_error is not None:
        fields.append("last_error = ?")
        params.append(last_error)
    if retry_count is not None:
        fields.append("retry_count = ?")
        params.append(retry_count)

    params.append(workflow_id)
    sql = f"UPDATE workflows SET {', '.join(fields)} WHERE workflow_id = ?"

    with _connect(db_path) as conn:
        cur = conn.execute(sql, params)
        if cur.rowcount == 0:
            raise KeyError(f"Workflow not found: {workflow_id}")
        row = conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        conn.commit()

    if row is None:
        raise KeyError(f"Workflow not found: {workflow_id}")
    return _row_to_workflow(row)


def list_logs(workflow_id: str, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT step, level, message, detail_json, created_at
            FROM logs WHERE workflow_id = ?
            ORDER BY id ASC
            """,
            (workflow_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def is_blacklisted(domain: str | None, url: str | None, db_path: str = DEFAULT_DB_PATH) -> bool:
    with _connect(db_path) as conn:
        if domain:
            hit = conn.execute(
                "SELECT 1 FROM blacklist WHERE domain = ? LIMIT 1",
                (domain,),
            ).fetchone()
            if hit:
                return True
        if url:
            hit = conn.execute(
                "SELECT 1 FROM blacklist WHERE url = ? LIMIT 1",
                (url,),
            ).fetchone()
            if hit:
                return True
    return False


def get_search_cache(
    cache_key: str,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT results_json FROM search_cache
            WHERE cache_key = ? AND expires_at > datetime('now')
            """,
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    return json.loads(row["results_json"])


def save_search_cache(
    cache_key: str,
    query: str,
    provider: str,
    results: list[dict[str, Any]],
    *,
    ttl_seconds: int = 900,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    results_json = json.dumps(results, ensure_ascii=False)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO search_cache (cache_key, query, provider, results_json, expires_at)
            VALUES (?, ?, ?, ?, datetime('now', ?))
            ON CONFLICT(cache_key) DO UPDATE SET
              query = excluded.query,
              provider = excluded.provider,
              results_json = excluded.results_json,
              created_at = datetime('now'),
              expires_at = excluded.expires_at
            """,
            (cache_key, query, provider, results_json, f"+{ttl_seconds} seconds"),
        )
        conn.commit()


def purge_expired_search_cache(db_path: str = DEFAULT_DB_PATH) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM search_cache WHERE expires_at <= datetime('now')"
        )
        conn.commit()
        return cur.rowcount


def get_or_create_niche(
    name: str,
    slug: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> NicheRow:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM niches WHERE slug = ?", (slug,)).fetchone()
        if row:
            return _row_to_niche(row)
        cur = conn.execute(
            "INSERT INTO niches (name, slug) VALUES (?, ?)",
            (name, slug),
        )
        row = conn.execute("SELECT * FROM niches WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_niche(row)


def get_or_create_project(
    niche_id: int,
    name: str,
    target_domain: str,
    *,
    target_url: str | None = None,
    brand_name: str | None = None,
    config_json: dict[str, Any] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> ProjectRow:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE niche_id = ? AND target_domain = ?",
            (niche_id, target_domain),
        ).fetchone()
        if row:
            return _row_to_project(row)
        cfg = json.dumps(config_json, ensure_ascii=False) if config_json else None
        cur = conn.execute(
            """
            INSERT INTO projects (niche_id, name, target_domain, target_url, brand_name, config_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (niche_id, name, target_domain, target_url, brand_name, cfg),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_project(row)


def get_project(project_id: int, db_path: str = DEFAULT_DB_PATH) -> ProjectRow | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _row_to_project(row) if row else None


def get_niche(niche_id: int, db_path: str = DEFAULT_DB_PATH) -> NicheRow | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM niches WHERE id = ?", (niche_id,)).fetchone()
    return _row_to_niche(row) if row else None


def get_project_by_domain(
    target_domain: str,
    *,
    niche_slug: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> ProjectRow | None:
    with _connect(db_path) as conn:
        if niche_slug:
            row = conn.execute(
                """
                SELECT p.* FROM projects p
                JOIN niches n ON n.id = p.niche_id
                WHERE p.target_domain = ? AND n.slug = ?
                LIMIT 1
                """,
                (target_domain, niche_slug),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM projects WHERE target_domain = ? LIMIT 1",
                (target_domain,),
            ).fetchone()
    return _row_to_project(row) if row else None


def save_audit(
    workflow_id: str,
    audit_json: dict[str, Any],
    *,
    opportunity_id: int | None = None,
    placement_type: str | None = None,
    image_required: bool = False,
    audit_score: float | None = None,
    passed: bool = False,
    db_path: str = DEFAULT_DB_PATH,
) -> AuditRow:
    payload = json.dumps(audit_json, ensure_ascii=False)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO audits (
              workflow_id, opportunity_id, audit_json, placement_type,
              image_required, audit_score, pass
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                opportunity_id,
                payload,
                placement_type,
                1 if image_required else 0,
                audit_score,
                1 if passed else 0,
            ),
        )
        row = conn.execute("SELECT * FROM audits WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_audit(row)


def get_latest_audit(
    workflow_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> AuditRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM audits
            WHERE workflow_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (workflow_id,),
        ).fetchone()
    return _row_to_audit(row) if row else None


def save_content_asset(
    workflow_id: str,
    content_text: str,
    *,
    target_link: str | None = None,
    image_url: str | None = None,
    image_local_path: str | None = None,
    image_prompt: str | None = None,
    content_type: str | None = None,
    confidence: float | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> ContentAssetRow:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS max_version
            FROM content_assets WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        version = int(row["max_version"]) + 1 if row else 1
        cur = conn.execute(
            """
            INSERT INTO content_assets (
              workflow_id, version, content_text, target_link, image_url,
              image_local_path, image_prompt, content_type, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                version,
                content_text,
                target_link,
                image_url,
                image_local_path,
                image_prompt,
                content_type,
                confidence,
            ),
        )
        asset_row = conn.execute(
            "SELECT * FROM content_assets WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        conn.commit()
    assert asset_row is not None
    return _row_to_content_asset(asset_row)


def get_latest_content_asset(
    workflow_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> ContentAssetRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM content_assets
            WHERE workflow_id = ?
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
            (workflow_id,),
        ).fetchone()
    return _row_to_content_asset(row) if row else None


def mark_content_approved(
    content_asset_id: int,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE content_assets SET approved = 1 WHERE id = ?",
            (content_asset_id,),
        )
        conn.commit()


def insert_feedback_event(
    workflow_id: str,
    action: str,
    *,
    user_id: str | None = None,
    edit_prompt: str | None = None,
    content_version_before: int | None = None,
    content_version_after: int | None = None,
    raw_payload: dict[str, Any] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> FeedbackEventRow:
    payload = json.dumps(raw_payload, ensure_ascii=False) if raw_payload else None
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO feedback_events (
              workflow_id, action, user_id, edit_prompt,
              content_version_before, content_version_after, raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                action,
                user_id,
                edit_prompt,
                content_version_before,
                content_version_after,
                payload,
            ),
        )
        row = conn.execute(
            "SELECT * FROM feedback_events WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_feedback_event(row)


def list_feedback_events(
    workflow_id: str | None = None,
    *,
    limit: int = 100,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM feedback_events"
    params: list[Any] = []
    if workflow_id:
        query += " WHERE workflow_id = ?"
        params.append(workflow_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, limit))
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def upsert_backlink(
    workflow_id: str,
    *,
    opportunity_id: int | None = None,
    published_url: str | None = None,
    status: str | None = None,
    verified: bool | None = None,
    verified_at: str | None = None,
    screenshot_path: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> BacklinkRow:
    existing = get_backlink(workflow_id, db_path=db_path)
    with _connect(db_path) as conn:
        if existing:
            fields = ["updated_at = datetime('now')"]
            params: list[Any] = []
            if published_url is not None:
                fields.append("published_url = ?")
                params.append(published_url)
            if status is not None:
                fields.append("status = ?")
                params.append(status)
            if verified is not None:
                fields.append("verified = ?")
                params.append(1 if verified else 0)
            if verified_at is not None:
                fields.append("verified_at = ?")
                params.append(verified_at)
            if screenshot_path is not None:
                fields.append("screenshot_path = ?")
                params.append(screenshot_path)
            if opportunity_id is not None:
                fields.append("opportunity_id = ?")
                params.append(opportunity_id)
            params.append(workflow_id)
            conn.execute(
                f"UPDATE backlinks SET {', '.join(fields)} WHERE workflow_id = ?",
                params,
            )
        else:
            conn.execute(
                """
                INSERT INTO backlinks (
                  workflow_id, opportunity_id, published_url, status, verified, verified_at, screenshot_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    opportunity_id,
                    published_url,
                    status,
                    1 if verified else 0 if verified is not None else 0,
                    verified_at,
                    screenshot_path,
                ),
            )
        row = conn.execute(
            "SELECT * FROM backlinks WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_backlink(row)


def get_backlink(workflow_id: str, db_path: str = DEFAULT_DB_PATH) -> BacklinkRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM backlinks WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
    return _row_to_backlink(row) if row else None


def insert_learning(
    *,
    workflow_id: str,
    domain: str | None = None,
    placement_type: str | None = None,
    final_score: float | None = None,
    approved: bool = False,
    published: bool = False,
    verified: bool = False,
    failure_reason: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO learning (
              workflow_id, domain, placement_type, final_score,
              approved, published, verified, failure_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                domain,
                placement_type,
                final_score,
                1 if approved else 0,
                1 if published else 0,
                1 if verified else 0,
                failure_reason,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_learning(
    *,
    domain: str | None = None,
    limit: int = 100,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM learning"
    params: list[Any] = []
    if domain:
        query += " WHERE domain = ?"
        params.append(domain)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, limit))
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_learning_for_workflow(
    workflow_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM learning WHERE workflow_id = ? ORDER BY id DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
    return dict(row) if row else None


def learning_aggregate(db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    with _connect(db_path) as conn:
        totals = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(approved) AS approved,
              SUM(published) AS published,
              SUM(verified) AS verified
            FROM learning
            """
        ).fetchone()
        by_domain = conn.execute(
            """
            SELECT
              domain,
              COUNT(*) AS total,
              SUM(verified) AS verified,
              SUM(published) AS published,
              AVG(final_score) AS avg_score
            FROM learning
            WHERE domain IS NOT NULL AND domain != ''
            GROUP BY domain
            ORDER BY total DESC, domain ASC
            """
        ).fetchall()
        by_placement = conn.execute(
            """
            SELECT
              placement_type,
              COUNT(*) AS total,
              SUM(verified) AS verified
            FROM learning
            WHERE placement_type IS NOT NULL AND placement_type != ''
            GROUP BY placement_type
            ORDER BY total DESC
            """
        ).fetchall()
        by_state = conn.execute(
            """
            SELECT state, COUNT(*) AS count
            FROM workflows
            GROUP BY state
            ORDER BY count DESC
            """
        ).fetchall()
    return {
        "totals": dict(totals) if totals else {},
        "by_domain": [dict(row) for row in by_domain],
        "by_placement": [dict(row) for row in by_placement],
        "workflows_by_state": [dict(row) for row in by_state],
    }


def get_latest_approval(
    workflow_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> ApprovalRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM approvals
            WHERE workflow_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (workflow_id,),
        ).fetchone()
    return _row_to_approval(row) if row else None


def _row_to_backlink(row: sqlite3.Row) -> BacklinkRow:
    return BacklinkRow(
        id=int(row["id"]),
        workflow_id=str(row["workflow_id"]),
        opportunity_id=row["opportunity_id"],
        published_url=row["published_url"],
        status=row["status"],
        verified=int(row["verified"]),
        verified_at=row["verified_at"],
        screenshot_path=row["screenshot_path"],
    )


def _row_to_draft(row: sqlite3.Row) -> DraftRow:
    return DraftRow(
        id=int(row["id"]),
        workflow_id=str(row["workflow_id"]),
        version=int(row["version"]),
        draft_text=str(row["draft_text"]),
        tone=row["tone"],
        confidence=row["confidence"],
        approved=int(row["approved"]),
    )


def _row_to_approval(row: sqlite3.Row) -> ApprovalRow:
    return ApprovalRow(
        id=int(row["id"]),
        workflow_id=str(row["workflow_id"]),
        draft_id=row["draft_id"],
        telegram_chat_id=row["telegram_chat_id"],
        telegram_message_id=row["telegram_message_id"],
        decision=row["decision"],
        callback_data=row["callback_data"],
        user_id=row["user_id"],
    )


def _row_to_edit_session(row: sqlite3.Row) -> EditSessionRow:
    return EditSessionRow(
        workflow_id=str(row["workflow_id"]),
        state=str(row["state"]),
        edit_prompt=row["edit_prompt"],
        user_id=row["user_id"],
    )


def _row_to_workflow(row: sqlite3.Row) -> WorkflowRow:
    keys = row.keys()
    return WorkflowRow(
        id=int(row["id"]),
        workflow_id=str(row["workflow_id"]),
        campaign_id=row["campaign_id"],
        state=str(row["state"]),
        current_agent=row["current_agent"],
        last_error=row["last_error"],
        retry_count=int(row["retry_count"]),
        project_id=row["project_id"] if "project_id" in keys else None,
        opportunity_id=row["opportunity_id"] if "opportunity_id" in keys else None,
        pending_approval_at=row["pending_approval_at"] if "pending_approval_at" in keys else None,
        approval_expires_at=row["approval_expires_at"] if "approval_expires_at" in keys else None,
        next_verify_at=row["next_verify_at"] if "next_verify_at" in keys else None,
    )


def _row_to_opportunity(row: sqlite3.Row) -> OpportunityRow:
    keys = row.keys()
    return OpportunityRow(
        id=int(row["id"]),
        campaign_id=row["campaign_id"],
        url=str(row["url"]),
        normalized_url=str(row["normalized_url"]),
        url_hash=str(row["url_hash"]),
        domain=row["domain"],
        title=row["title"],
        snippet=row["snippet"],
        status=str(row["status"]),
        project_id=row["project_id"] if "project_id" in keys else None,
        placement_type=row["placement_type"] if "placement_type" in keys else None,
        final_score=row["final_score"] if "final_score" in keys else None,
        context_json=row["context_json"] if "context_json" in keys else None,
    )


def _row_to_niche(row: sqlite3.Row) -> NicheRow:
    return NicheRow(
        id=int(row["id"]),
        name=str(row["name"]),
        slug=str(row["slug"]),
        status=str(row["status"]),
        search_queries_json=row["search_queries_json"],
        config_json=row["config_json"],
    )


def _row_to_project(row: sqlite3.Row) -> ProjectRow:
    return ProjectRow(
        id=int(row["id"]),
        niche_id=int(row["niche_id"]),
        name=str(row["name"]),
        target_domain=str(row["target_domain"]),
        target_url=row["target_url"],
        brand_name=row["brand_name"],
        status=str(row["status"]),
        config_json=row["config_json"],
    )


def _row_to_audit(row: sqlite3.Row) -> AuditRow:
    return AuditRow(
        id=int(row["id"]),
        workflow_id=str(row["workflow_id"]),
        opportunity_id=row["opportunity_id"],
        audit_json=str(row["audit_json"]),
        placement_type=row["placement_type"],
        image_required=int(row["image_required"]),
        audit_score=row["audit_score"],
        pass_=int(row["pass"]),
    )


def _row_to_content_asset(row: sqlite3.Row) -> ContentAssetRow:
    return ContentAssetRow(
        id=int(row["id"]),
        workflow_id=str(row["workflow_id"]),
        version=int(row["version"]),
        content_text=str(row["content_text"]),
        target_link=row["target_link"],
        image_url=row["image_url"],
        image_local_path=row["image_local_path"],
        image_prompt=row["image_prompt"],
        content_type=row["content_type"],
        confidence=row["confidence"],
        approved=int(row["approved"]),
    )


def _row_to_feedback_event(row: sqlite3.Row) -> FeedbackEventRow:
    return FeedbackEventRow(
        id=int(row["id"]),
        workflow_id=str(row["workflow_id"]),
        action=str(row["action"]),
        user_id=row["user_id"],
        edit_prompt=row["edit_prompt"],
        content_version_before=row["content_version_before"],
        content_version_after=row["content_version_after"],
    )
