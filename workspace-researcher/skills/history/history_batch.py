#!/usr/bin/env python3
"""
history_batch.py — Batched URL history checks for headline scan.

Usage:
  python3 history_batch.py check-batch --project coinography url1 url2 ...
  python3 history_batch.py check-batch --project coinography --stdin

Prints one URL per line that EXISTS in the 7-day history for the project.
Exit 0 always (batch is informational).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DEFAULT_DB = os.environ.get(
    "ARTICLE_HISTORY_DB", os.path.expanduser("~/.openclaw/article_history.db")
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
          url TEXT NOT NULL,
          project TEXT NOT NULL DEFAULT 'coinography',
          timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (url, project)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_project_ts ON history(project, timestamp)"
    )
    # Migrate legacy single-column PK table if present
    cols = {
        row[1]: row for row in conn.execute("PRAGMA table_info(history)").fetchall()
    }
    if "project" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN project TEXT NOT NULL DEFAULT 'coinography'")
        conn.execute("UPDATE history SET project='coinography' WHERE project IS NULL OR project=''")
    # If old schema had url-only PK, rebuild table
    pk_cols = [row[1] for row in conn.execute("PRAGMA table_info(history)").fetchall() if row[5]]
    if pk_cols == ["url"]:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history_new (
              url TEXT NOT NULL,
              project TEXT NOT NULL DEFAULT 'coinography',
              timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (url, project)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO history_new (url, project, timestamp)
            SELECT url, COALESCE(NULLIF(project, ''), 'coinography'), timestamp FROM history
            """
        )
        conn.execute("DROP TABLE history")
        conn.execute("ALTER TABLE history_new RENAME TO history")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_project_ts ON history(project, timestamp)"
        )


def purge_old(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM history WHERE timestamp <= datetime('now', '-7 days')")


def existing_urls(
    urls: list[str],
    project: str,
    db_path: str = DEFAULT_DB,
) -> set[str]:
    """Return subset of urls that already exist for project within retention window."""
    project = (project or "coinography").strip() or "coinography"
    clean = [u.strip() for u in urls if u and u.strip()]
    if not clean:
        return set()

    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        purge_old(conn)
        conn.commit()
        placeholders = ",".join("?" for _ in clean)
        rows = conn.execute(
            f"SELECT url FROM history WHERE project = ? AND url IN ({placeholders})",
            [project, *clean],
        ).fetchall()
    return {r[0] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_batch = sub.add_parser("check-batch")
    p_batch.add_argument("--project", default=os.environ.get("PROJECT_SLUG", "coinography"))
    p_batch.add_argument("--db-path", default=DEFAULT_DB)
    p_batch.add_argument("--stdin", action="store_true")
    p_batch.add_argument("urls", nargs="*")

    args = parser.parse_args()

    if args.stdin:
        urls = [line.strip() for line in sys.stdin if line.strip()]
    else:
        urls = args.urls

    exists = existing_urls(urls, args.project, args.db_path)
    for u in urls:
        if u in exists:
            print(u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
