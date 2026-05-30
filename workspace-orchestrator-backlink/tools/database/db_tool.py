#!/usr/bin/env python3
"""db_tool.py — database wrappers for backlink tools."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_DB_DIR = _ROOT / "database"
if str(_DB_DIR) not in sys.path:
    sys.path.insert(0, str(_DB_DIR))

import backlink_db  # noqa: E402

DEFAULT_DB_PATH = backlink_db.DEFAULT_DB_PATH


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    backlink_db.init_db(db_path)


def get_workflow(workflow_id: str, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    row = backlink_db.get_workflow(workflow_id, db_path=db_path)
    if row is None:
        return None
    return {
        "workflow_id": row.workflow_id,
        "state": row.state,
        "current_agent": row.current_agent,
        "last_error": row.last_error,
        "retry_count": row.retry_count,
    }


def is_blacklisted(domain: str | None, url: str | None, db_path: str = DEFAULT_DB_PATH) -> bool:
    return backlink_db.is_blacklisted(domain, url, db_path=db_path)


def get_search_cache(cache_key: str, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]] | None:
    return backlink_db.get_search_cache(cache_key, db_path=db_path)


def save_search_cache(
    cache_key: str,
    query: str,
    provider: str,
    results: list[dict[str, Any]],
    *,
    ttl_seconds: int = 900,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    backlink_db.save_search_cache(
        cache_key,
        query,
        provider,
        results,
        ttl_seconds=ttl_seconds,
        db_path=db_path,
    )


def purge_expired_search_cache(db_path: str = DEFAULT_DB_PATH) -> int:
    return backlink_db.purge_expired_search_cache(db_path=db_path)
