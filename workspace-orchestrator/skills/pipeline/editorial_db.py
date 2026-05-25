#!/usr/bin/env python3
"""editorial_db.py — SQLite store for published news cards and editorial feedback."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/data/editorial.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  alert_id TEXT NOT NULL,
  story_id TEXT,
  headline TEXT,
  wp_url TEXT,
  wp_post_id TEXT,
  telegram_group TEXT NOT NULL,
  telegram_message_id INTEGER NOT NULL,
  card_sent_at TEXT,
  run_dir TEXT,
  wp_status TEXT DEFAULT 'publish',
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_articles_tg_msg ON articles(telegram_group, telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_articles_alert ON articles(alert_id);

CREATE TABLE IF NOT EXISTS feedback_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL REFERENCES articles(id),
  event_type TEXT NOT NULL,
  score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 10),
  user_id TEXT,
  user_username TEXT,
  source TEXT NOT NULL,
  raw_payload TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_article ON feedback_events(article_id, event_type);

CREATE TABLE IF NOT EXISTS article_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL REFERENCES articles(id),
  version_type TEXT NOT NULL,
  content_md TEXT NOT NULL,
  user_id TEXT,
  user_username TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_versions_article ON article_versions(article_id, version_type);

CREATE TABLE IF NOT EXISTS edit_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL REFERENCES articles(id),
  user_id TEXT NOT NULL,
  state TEXT NOT NULL,
  prompt_message_id INTEGER,
  suggested_version_id INTEGER,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(article_id, user_id)
);

CREATE TABLE IF NOT EXISTS editorial_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL REFERENCES articles(id),
  action_type TEXT NOT NULL,
  user_id TEXT,
  source TEXT NOT NULL,
  raw_payload TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
"""


@dataclass
class Article:
    id: int
    run_id: str
    alert_id: str
    story_id: str | None
    headline: str | None
    wp_url: str | None
    wp_post_id: str | None
    telegram_group: str
    telegram_message_id: int
    card_sent_at: str | None
    run_dir: str | None = None
    wp_status: str = "publish"


@dataclass
class EditSession:
    id: int
    article_id: int
    user_id: str
    state: str
    prompt_message_id: int | None
    suggested_version_id: int | None


@dataclass
class ArticleVersion:
    id: int
    article_id: int
    version_type: str
    content_md: str
    user_id: str | None
    user_username: str | None


def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "articles", "run_dir"):
        conn.execute("ALTER TABLE articles ADD COLUMN run_dir TEXT")
    if not _column_exists(conn, "articles", "wp_status"):
        conn.execute("ALTER TABLE articles ADD COLUMN wp_status TEXT DEFAULT 'publish'")
    conn.execute("UPDATE articles SET wp_status = 'publish' WHERE wp_status IS NULL")


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()


def _row_to_article(row: sqlite3.Row) -> Article:
    keys = row.keys()
    return Article(
        id=row["id"],
        run_id=row["run_id"],
        alert_id=row["alert_id"],
        story_id=row["story_id"],
        headline=row["headline"],
        wp_url=row["wp_url"],
        wp_post_id=row["wp_post_id"],
        telegram_group=row["telegram_group"],
        telegram_message_id=row["telegram_message_id"],
        card_sent_at=row["card_sent_at"],
        run_dir=row["run_dir"] if "run_dir" in keys else None,
        wp_status=(row["wp_status"] if "wp_status" in keys else None) or "publish",
    )


def _row_to_edit_session(row: sqlite3.Row) -> EditSession:
    return EditSession(
        id=row["id"],
        article_id=row["article_id"],
        user_id=row["user_id"],
        state=row["state"],
        prompt_message_id=row["prompt_message_id"],
        suggested_version_id=row["suggested_version_id"],
    )


def insert_article(card: dict[str, Any], db_path: str = DEFAULT_DB_PATH) -> int:
    init_db(db_path)
    run_id = str(card.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_id required")
    message_id = card.get("telegram_message_id")
    group = str(card.get("telegram_group") or "").strip()
    if message_id is None or not group:
        raise ValueError("telegram_message_id and telegram_group required")

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO articles (
              run_id, alert_id, story_id, headline, wp_url, wp_post_id,
              telegram_group, telegram_message_id, card_sent_at, run_dir, wp_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
              alert_id = excluded.alert_id,
              story_id = excluded.story_id,
              headline = excluded.headline,
              wp_url = excluded.wp_url,
              wp_post_id = excluded.wp_post_id,
              telegram_group = excluded.telegram_group,
              telegram_message_id = excluded.telegram_message_id,
              card_sent_at = excluded.card_sent_at,
              run_dir = excluded.run_dir,
              wp_status = COALESCE(excluded.wp_status, articles.wp_status)
            """,
            (
                run_id,
                str(card.get("alert_id") or f"ta-{run_id}"),
                str(card.get("story_id") or "") or None,
                str(card.get("headline") or "") or None,
                str(card.get("wp_url") or "") or None,
                str(card.get("wp_post_id") or "") or None,
                group,
                int(message_id),
                str(card.get("card_sent_at") or "") or None,
                str(card.get("run_dir") or "") or None,
                str(card.get("wp_status") or "publish"),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM articles WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            raise RuntimeError(f"insert failed for run_id={run_id}")
        return int(row["id"])


def set_wp_status(article_id: int, status: str, db_path: str = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("UPDATE articles SET wp_status = ? WHERE id = ?", (status, article_id))
        conn.commit()


def lookup_by_message_id(
    telegram_group: str, message_id: int, db_path: str = DEFAULT_DB_PATH
) -> Article | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM articles WHERE telegram_group = ? AND telegram_message_id = ? ORDER BY id DESC LIMIT 1",
            (str(telegram_group), int(message_id)),
        ).fetchone()
    return _row_to_article(row) if row else None


def lookup_by_run_id(run_id: str, db_path: str = DEFAULT_DB_PATH) -> Article | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM articles WHERE run_id = ?", (run_id.strip(),)).fetchone()
    return _row_to_article(row) if row else None


def lookup_by_alert_id(alert_id: str, db_path: str = DEFAULT_DB_PATH) -> Article | None:
    init_db(db_path)
    aid = alert_id.strip()
    if not aid.startswith("ta-"):
        aid = f"ta-{aid}"
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM articles WHERE alert_id = ?", (aid,)).fetchone()
    return _row_to_article(row) if row else None


def lookup_by_id(article_id: int, db_path: str = DEFAULT_DB_PATH) -> Article | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    return _row_to_article(row) if row else None


def record_rating(
    article_id: int,
    event_type: str,
    score: int,
    *,
    user_id: str | None = None,
    user_username: str | None = None,
    source: str = "text",
    raw_payload: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    if event_type not in ("rate_article", "rate_image"):
        raise ValueError(f"invalid event_type: {event_type}")
    if not 1 <= score <= 10:
        raise ValueError(f"score must be 1-10, got {score}")
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO feedback_events (article_id, event_type, score, user_id, user_username, source, raw_payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (article_id, event_type, score, user_id, user_username, source, raw_payload),
        )
        conn.commit()
        return int(cur.lastrowid)


def latest_rating(article_id: int, event_type: str, db_path: str = DEFAULT_DB_PATH) -> int | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT score FROM feedback_events WHERE article_id = ? AND event_type = ? ORDER BY id DESC LIMIT 1",
            (article_id, event_type),
        ).fetchone()
    return int(row["score"]) if row else None


def save_article_version(
    article_id: int,
    version_type: str,
    content_md: str,
    *,
    user_id: str | None = None,
    user_username: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    if version_type not in ("published_snapshot", "user_suggested", "applied"):
        raise ValueError(f"invalid version_type: {version_type}")
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO article_versions (article_id, version_type, content_md, user_id, user_username) VALUES (?, ?, ?, ?, ?)",
            (article_id, version_type, content_md, user_id, user_username),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_latest_version(
    article_id: int, version_type: str, db_path: str = DEFAULT_DB_PATH
) -> ArticleVersion | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM article_versions WHERE article_id = ? AND version_type = ? ORDER BY id DESC LIMIT 1",
            (article_id, version_type),
        ).fetchone()
    if not row:
        return None
    return ArticleVersion(
        id=row["id"],
        article_id=row["article_id"],
        version_type=row["version_type"],
        content_md=row["content_md"],
        user_id=row["user_id"],
        user_username=row["user_username"],
    )


def get_edit_session(article_id: int, user_id: str, db_path: str = DEFAULT_DB_PATH) -> EditSession | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM edit_sessions WHERE article_id = ? AND user_id = ?",
            (article_id, user_id),
        ).fetchone()
    return _row_to_edit_session(row) if row else None


def get_edit_session_by_prompt(
    prompt_message_id: int, db_path: str = DEFAULT_DB_PATH
) -> tuple[EditSession, Article] | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM edit_sessions WHERE prompt_message_id = ? ORDER BY id DESC LIMIT 1",
            (int(prompt_message_id),),
        ).fetchone()
        if not row:
            return None
        session = _row_to_edit_session(row)
        article_row = conn.execute("SELECT * FROM articles WHERE id = ?", (session.article_id,)).fetchone()
    return (session, _row_to_article(article_row)) if article_row else None


def upsert_edit_session(
    article_id: int,
    user_id: str,
    state: str,
    *,
    prompt_message_id: int | None = None,
    suggested_version_id: int | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO edit_sessions (article_id, user_id, state, prompt_message_id, suggested_version_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(article_id, user_id) DO UPDATE SET
              state = excluded.state,
              prompt_message_id = excluded.prompt_message_id,
              suggested_version_id = excluded.suggested_version_id
            """,
            (article_id, user_id, state, prompt_message_id, suggested_version_id),
        )
        conn.commit()


def clear_edit_session(article_id: int, user_id: str, db_path: str = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM edit_sessions WHERE article_id = ? AND user_id = ?", (article_id, user_id))
        conn.commit()


def record_editorial_action(
    article_id: int,
    action_type: str,
    *,
    user_id: str | None = None,
    source: str = "callback",
    raw_payload: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO editorial_actions (article_id, action_type, user_id, source, raw_payload) VALUES (?, ?, ?, ?, ?)",
            (article_id, action_type, user_id, source, raw_payload),
        )
        conn.commit()
        return int(cur.lastrowid)
