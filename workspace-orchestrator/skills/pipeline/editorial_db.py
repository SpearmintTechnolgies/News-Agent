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
  wp_status TEXT DEFAULT 'draft',
  wp_author_id INTEGER,
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

CREATE TABLE IF NOT EXISTS picked_stories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pick_run_id TEXT NOT NULL,
  pipeline_run_id TEXT,
  pick_index INTEGER NOT NULL,
  category TEXT NOT NULL,
  category_score REAL,
  alt_categories TEXT,
  story_id TEXT,
  primary_headline TEXT NOT NULL,
  primary_url TEXT,
  primary_asset TEXT,
  pub_date TEXT,
  source_name TEXT,
  raw_payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  failed_reason TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_picked_status ON picked_stories(status, pick_run_id);
CREATE INDEX IF NOT EXISTS idx_picked_category ON picked_stories(category, created_at);
CREATE INDEX IF NOT EXISTS idx_picked_pipeline ON picked_stories(pipeline_run_id);

-- Approve-title-first: persistent per-project headline pool filled by the 24x7
-- scanner. STRICTLY project-scoped: every read MUST filter by project (the
-- UNIQUE key is (project, url) so the same URL can exist independently for two
-- projects and the two lists can never bleed into each other).
CREATE TABLE IF NOT EXISTS headline_pool (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  url TEXT NOT NULL,
  headline TEXT NOT NULL,
  source TEXT,
  pub_date TEXT,
  summary TEXT,
  corroborating_json TEXT,
  scanned_at TEXT DEFAULT (datetime('now')),
  status TEXT NOT NULL DEFAULT 'fresh',
  UNIQUE(project, url)
);
CREATE INDEX IF NOT EXISTS idx_pool_project_status ON headline_pool(project, status, pub_date);

-- Daily feed card (the tap-to-select menu posted to the group). One row per
-- card sent; selected_json holds the toggled candidate indexes.
CREATE TABLE IF NOT EXISTS feed_cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feed_id TEXT NOT NULL UNIQUE,
  project TEXT NOT NULL,
  telegram_group TEXT NOT NULL,
  telegram_message_id INTEGER,
  candidates_json TEXT NOT NULL,
  selected_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feed_cards_msg ON feed_cards(telegram_group, telegram_message_id);

-- Serial execution queue for single-click feed-card taps (one pipeline at a time).
CREATE TABLE IF NOT EXISTS feed_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  feed_id TEXT NOT NULL,
  candidate_index INTEGER NOT NULL,
  headline TEXT NOT NULL,
  selection_file TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  started_at TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feed_jobs_status ON feed_jobs(status, id);

-- Small key/value state store (e.g. group-level last_contact_at for the 48h
-- idle clock). project '_global' is used for group-wide values.
CREATE TABLE IF NOT EXISTS pipeline_state (
  project TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  updated_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (project, key)
);

-- Deterministic /onboard flow state (project-onboarder plugin + onboard_project.py
-- engine). One row per (chat_id, user_id) — mirrors edit_sessions above. Never
-- touches any other table; safe to drop independently.
CREATE TABLE IF NOT EXISTS onboard_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  step TEXT NOT NULL,
  answers_json TEXT NOT NULL DEFAULT '{}',
  prompt_message_id INTEGER,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  UNIQUE(chat_id, user_id)
);
"""

GLOBAL_PROJECT = "_global"


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
    wp_status: str = "draft"
    wp_author_id: int | None = None
    category: str | None = None
    project: str = "coinography"
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_total: int | None = None
    tokens_by_model: str | None = None
    cost_usd: float | None = None
    duration_seconds: int | None = None


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


@dataclass
class PickedStory:
    id: int
    pick_run_id: str
    pipeline_run_id: str | None
    pick_index: int
    category: str
    category_score: float | None
    alt_categories: str | None
    story_id: str | None
    primary_headline: str
    primary_url: str | None
    primary_asset: str | None
    pub_date: str | None
    source_name: str | None
    raw_payload: str
    status: str
    failed_reason: str | None
    project: str = "coinography"
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_total: int | None = None
    tokens_by_model: str | None = None
    cost_usd: float | None = None
    duration_seconds: int | None = None


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
        conn.execute("ALTER TABLE articles ADD COLUMN wp_status TEXT DEFAULT 'draft'")
    if not _column_exists(conn, "articles", "wp_author_id"):
        conn.execute("ALTER TABLE articles ADD COLUMN wp_author_id INTEGER")
    if not _column_exists(conn, "articles", "category"):
        conn.execute("ALTER TABLE articles ADD COLUMN category TEXT")
    # Multi-project support: every article + pick is tied to a publishing project.
    # Existing rows are coinography (the only project until now), so backfill
    # via the column DEFAULT and an explicit UPDATE for any pre-existing rows.
    if not _column_exists(conn, "articles", "project"):
        conn.execute("ALTER TABLE articles ADD COLUMN project TEXT NOT NULL DEFAULT 'coinography'")
        conn.execute("UPDATE articles SET project = 'coinography' WHERE project IS NULL OR project = ''")
    if not _column_exists(conn, "picked_stories", "project"):
        conn.execute("ALTER TABLE picked_stories ADD COLUMN project TEXT NOT NULL DEFAULT 'coinography'")
        conn.execute("UPDATE picked_stories SET project = 'coinography' WHERE project IS NULL OR project = ''")
    # WordPress category support: each pick/article carries 1 primary + up to 2
    # secondary WP categories. Stored as JSON arrays of slugs and resolved IDs.
    # The legacy single `category` column keeps the PRIMARY slug for back-compat
    # with recent_published_categories() diversity logic.
    if not _column_exists(conn, "picked_stories", "wp_category_slugs"):
        conn.execute("ALTER TABLE picked_stories ADD COLUMN wp_category_slugs TEXT")
    if not _column_exists(conn, "picked_stories", "wp_category_ids"):
        conn.execute("ALTER TABLE picked_stories ADD COLUMN wp_category_ids TEXT")
    if not _column_exists(conn, "articles", "wp_category_slugs"):
        conn.execute("ALTER TABLE articles ADD COLUMN wp_category_slugs TEXT")
    if not _column_exists(conn, "articles", "wp_category_ids"):
        conn.execute("ALTER TABLE articles ADD COLUMN wp_category_ids TEXT")
    if not _column_exists(conn, "articles", "tokens_in"):
        conn.execute("ALTER TABLE articles ADD COLUMN tokens_in INTEGER")
    if not _column_exists(conn, "articles", "tokens_out"):
        conn.execute("ALTER TABLE articles ADD COLUMN tokens_out INTEGER")
    if not _column_exists(conn, "articles", "tokens_total"):
        conn.execute("ALTER TABLE articles ADD COLUMN tokens_total INTEGER")
    if not _column_exists(conn, "picked_stories", "tokens_in"):
        conn.execute("ALTER TABLE picked_stories ADD COLUMN tokens_in INTEGER")
    if not _column_exists(conn, "picked_stories", "tokens_out"):
        conn.execute("ALTER TABLE picked_stories ADD COLUMN tokens_out INTEGER")
    if not _column_exists(conn, "picked_stories", "tokens_total"):
        conn.execute("ALTER TABLE picked_stories ADD COLUMN tokens_total INTEGER")
    if not _column_exists(conn, "articles", "tokens_by_model"):
        conn.execute("ALTER TABLE articles ADD COLUMN tokens_by_model TEXT")
    if not _column_exists(conn, "articles", "cost_usd"):
        conn.execute("ALTER TABLE articles ADD COLUMN cost_usd REAL")
    if not _column_exists(conn, "picked_stories", "tokens_by_model"):
        conn.execute("ALTER TABLE picked_stories ADD COLUMN tokens_by_model TEXT")
    if not _column_exists(conn, "picked_stories", "cost_usd"):
        conn.execute("ALTER TABLE picked_stories ADD COLUMN cost_usd REAL")
    if not _column_exists(conn, "articles", "duration_seconds"):
        conn.execute("ALTER TABLE articles ADD COLUMN duration_seconds INTEGER")
    if not _column_exists(conn, "picked_stories", "duration_seconds"):
        conn.execute("ALTER TABLE picked_stories ADD COLUMN duration_seconds INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_project ON articles(project, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_picked_project ON picked_stories(project, pick_run_id)")
    conn.execute("UPDATE articles SET wp_status = 'draft' WHERE wp_status IS NULL")
    if not _column_exists(conn, "feed_jobs", "current_step"):
        conn.execute("ALTER TABLE feed_jobs ADD COLUMN current_step TEXT")
    if not _column_exists(conn, "feed_jobs", "step_updated_at"):
        conn.execute("ALTER TABLE feed_jobs ADD COLUMN step_updated_at TEXT")


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()


def _row_to_article(row: sqlite3.Row) -> Article:
    keys = row.keys()
    wp_url = row["wp_url"]
    wp_post_id = row["wp_post_id"]
    if not wp_post_id and wp_url:
        import re as _re
        _m = _re.search(r"[?&]p=(\d+)", wp_url)
        if _m:
            wp_post_id = _m.group(1)
    return Article(
        id=row["id"],
        run_id=row["run_id"],
        alert_id=row["alert_id"],
        story_id=row["story_id"],
        headline=row["headline"],
        wp_url=wp_url,
        wp_post_id=wp_post_id,
        telegram_group=row["telegram_group"],
        telegram_message_id=row["telegram_message_id"],
        card_sent_at=row["card_sent_at"],
        run_dir=row["run_dir"] if "run_dir" in keys else None,
        wp_status=(row["wp_status"] if "wp_status" in keys else None) or "draft",
        wp_author_id=(
            int(row["wp_author_id"])
            if "wp_author_id" in keys and row["wp_author_id"] is not None
            else None
        ),
        category=row["category"] if "category" in keys else None,
        project=(row["project"] if "project" in keys else None) or "coinography",
        tokens_in=int(row["tokens_in"]) if "tokens_in" in keys and row["tokens_in"] is not None else None,
        tokens_out=int(row["tokens_out"]) if "tokens_out" in keys and row["tokens_out"] is not None else None,
        tokens_total=int(row["tokens_total"]) if "tokens_total" in keys and row["tokens_total"] is not None else None,
        tokens_by_model=row["tokens_by_model"] if "tokens_by_model" in keys else None,
        cost_usd=float(row["cost_usd"]) if "cost_usd" in keys and row["cost_usd"] is not None else None,
        duration_seconds=int(row["duration_seconds"])
        if "duration_seconds" in keys and row["duration_seconds"] is not None
        else None,
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

    project = str(card.get("project") or "coinography").strip() or "coinography"
    import json as _json

    by_model = card.get("by_model")
    tokens_by_model = (
        _json.dumps(by_model, ensure_ascii=False) if isinstance(by_model, dict) and by_model else None
    )
    cost_usd = float(card["cost_usd"]) if card.get("cost_usd") is not None else None
    duration_seconds = (
        int(card["duration_seconds"]) if card.get("duration_seconds") is not None else None
    )

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO articles (
              run_id, alert_id, story_id, headline, category, wp_url, wp_post_id,
              telegram_group, telegram_message_id, card_sent_at, run_dir, wp_status, project,
              tokens_in, tokens_out, tokens_total, tokens_by_model, cost_usd, duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
              alert_id = excluded.alert_id,
              story_id = excluded.story_id,
              headline = excluded.headline,
              category = COALESCE(excluded.category, articles.category),
              wp_url = excluded.wp_url,
              wp_post_id = excluded.wp_post_id,
              telegram_group = excluded.telegram_group,
              telegram_message_id = excluded.telegram_message_id,
              card_sent_at = excluded.card_sent_at,
              run_dir = excluded.run_dir,
              wp_status = COALESCE(excluded.wp_status, articles.wp_status),
              project = excluded.project,
              tokens_in = COALESCE(excluded.tokens_in, articles.tokens_in),
              tokens_out = COALESCE(excluded.tokens_out, articles.tokens_out),
              tokens_total = COALESCE(excluded.tokens_total, articles.tokens_total),
              tokens_by_model = COALESCE(excluded.tokens_by_model, articles.tokens_by_model),
              cost_usd = COALESCE(excluded.cost_usd, articles.cost_usd),
              duration_seconds = COALESCE(excluded.duration_seconds, articles.duration_seconds)
            """,
            (
                run_id,
                str(card.get("alert_id") or f"ta-{run_id}"),
                str(card.get("story_id") or "") or None,
                str(card.get("headline") or "") or None,
                str(card.get("category") or "") or None,
                str(card.get("wp_url") or "") or None,
                str(card.get("wp_post_id") or "") or None,
                group,
                int(message_id),
                str(card.get("card_sent_at") or "") or None,
                str(card.get("run_dir") or "") or None,
                str(card.get("wp_status") or "draft"),
                project,
                int(card["tokens_in"]) if card.get("tokens_in") is not None else None,
                int(card["tokens_out"]) if card.get("tokens_out") is not None else None,
                int(card["tokens_total"]) if card.get("tokens_total") is not None else None,
                tokens_by_model,
                cost_usd,
                duration_seconds,
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


def set_wp_author(article_id: int, author_id: int, db_path: str = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE articles SET wp_author_id = ? WHERE id = ?",
            (int(author_id), article_id),
        )
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


# ---------------------------------------------------------------------------
# picked_stories — multi-story batch pipeline support
# ---------------------------------------------------------------------------

PICK_TERMINAL_STATUSES = {"published", "drafted", "failed", "cancelled"}
PICK_VALID_STATUSES = {
    "pending",
    "researching",
    "writing",
    "drafted",
    "published",
    "failed",
    "cancelled",
}


def _row_to_pick(row: sqlite3.Row) -> PickedStory:
    keys = row.keys()
    return PickedStory(
        id=row["id"],
        pick_run_id=row["pick_run_id"],
        pipeline_run_id=row["pipeline_run_id"],
        pick_index=int(row["pick_index"]),
        category=row["category"],
        category_score=(
            float(row["category_score"]) if row["category_score"] is not None else None
        ),
        alt_categories=row["alt_categories"],
        story_id=row["story_id"],
        primary_headline=row["primary_headline"],
        primary_url=row["primary_url"],
        primary_asset=row["primary_asset"],
        pub_date=row["pub_date"],
        source_name=row["source_name"],
        raw_payload=row["raw_payload"],
        status=row["status"],
        failed_reason=row["failed_reason"],
        project=(row["project"] if "project" in keys else None) or "coinography",
        tokens_in=int(row["tokens_in"]) if "tokens_in" in keys and row["tokens_in"] is not None else None,
        tokens_out=int(row["tokens_out"]) if "tokens_out" in keys and row["tokens_out"] is not None else None,
        tokens_total=int(row["tokens_total"]) if "tokens_total" in keys and row["tokens_total"] is not None else None,
        tokens_by_model=row["tokens_by_model"] if "tokens_by_model" in keys else None,
        cost_usd=float(row["cost_usd"]) if "cost_usd" in keys and row["cost_usd"] is not None else None,
        duration_seconds=int(row["duration_seconds"])
        if "duration_seconds" in keys and row["duration_seconds"] is not None
        else None,
    )


def insert_picked_stories(
    picks: list[dict[str, Any]],
    pick_run_id: str,
    *,
    pipeline_run_id: str | None = None,
    project: str = "coinography",
    db_path: str = DEFAULT_DB_PATH,
) -> list[int]:
    """Bulk insert picks from picks.json. Returns inserted row ids in order."""
    init_db(db_path)
    if not picks:
        return []
    import json as _json

    project = (project or "coinography").strip() or "coinography"
    inserted: list[int] = []
    with _connect(db_path) as conn:
        for pick in picks:
            alt = pick.get("alt_categories")
            if isinstance(alt, (list, dict)):
                alt = _json.dumps(alt)
            elif alt is not None:
                alt = str(alt)
            wp_slugs = pick.get("wp_category_slugs")
            wp_slugs = _json.dumps(wp_slugs) if isinstance(wp_slugs, list) else None
            wp_ids = pick.get("wp_category_ids")
            wp_ids = _json.dumps(wp_ids) if isinstance(wp_ids, list) else None
            cur = conn.execute(
                """
                INSERT INTO picked_stories (
                  pick_run_id, pipeline_run_id, pick_index, category, category_score,
                  alt_categories, story_id, primary_headline, primary_url,
                  primary_asset, pub_date, source_name, raw_payload, status, project,
                  wp_category_slugs, wp_category_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    pick_run_id,
                    pipeline_run_id,
                    int(pick.get("pick_index") or 0),
                    str(pick.get("category") or "").strip(),
                    (
                        float(pick.get("category_score"))
                        if pick.get("category_score") is not None
                        else None
                    ),
                    alt,
                    str(pick.get("story_id") or "") or None,
                    str(pick.get("headline") or pick.get("primary_headline") or ""),
                    str(pick.get("url") or pick.get("primary_url") or "") or None,
                    str(pick.get("primary_asset") or "") or None,
                    str(pick.get("pub_date") or "") or None,
                    str(pick.get("source") or pick.get("source_name") or "") or None,
                    _json.dumps(pick, ensure_ascii=False),
                    project,
                    wp_slugs,
                    wp_ids,
                ),
            )
            inserted.append(int(cur.lastrowid))
        conn.commit()
    return inserted


def update_pick_status(
    pick_id: int,
    status: str,
    *,
    failed_reason: str | None = None,
    pipeline_run_id: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    tokens_total: int | None = None,
    tokens_by_model: str | None = None,
    cost_usd: float | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    if status not in PICK_VALID_STATUSES:
        raise ValueError(f"invalid pick status: {status}")
    init_db(db_path)
    with _connect(db_path) as conn:
        if pipeline_run_id is not None:
            conn.execute(
                """
                UPDATE picked_stories
                SET status = ?, failed_reason = ?, pipeline_run_id = ?,
                    tokens_in = COALESCE(?, tokens_in),
                    tokens_out = COALESCE(?, tokens_out),
                    tokens_total = COALESCE(?, tokens_total),
                    tokens_by_model = COALESCE(?, tokens_by_model),
                    cost_usd = COALESCE(?, cost_usd),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    status,
                    failed_reason,
                    pipeline_run_id,
                    tokens_in,
                    tokens_out,
                    tokens_total,
                    tokens_by_model,
                    cost_usd,
                    pick_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE picked_stories
                SET status = ?, failed_reason = ?,
                    tokens_in = COALESCE(?, tokens_in),
                    tokens_out = COALESCE(?, tokens_out),
                    tokens_total = COALESCE(?, tokens_total),
                    tokens_by_model = COALESCE(?, tokens_by_model),
                    cost_usd = COALESCE(?, cost_usd),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    status,
                    failed_reason,
                    tokens_in,
                    tokens_out,
                    tokens_total,
                    tokens_by_model,
                    cost_usd,
                    pick_id,
                ),
            )
        conn.commit()


def list_picks_by_run(
    pick_run_id: str, db_path: str = DEFAULT_DB_PATH
) -> list[PickedStory]:
    """All picks for a given pick_run_id, ordered by pick_index."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM picked_stories WHERE pick_run_id = ? ORDER BY pick_index ASC",
            (pick_run_id,),
        ).fetchall()
    return [_row_to_pick(r) for r in rows]


def pending_picks_for_run(
    pick_run_id: str, db_path: str = DEFAULT_DB_PATH
) -> list[PickedStory]:
    """Non-terminal picks for a pick_run_id, ordered by pick_index."""
    init_db(db_path)
    placeholders = ",".join("?" * len(PICK_TERMINAL_STATUSES))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM picked_stories
            WHERE pick_run_id = ? AND status NOT IN ({placeholders})
            ORDER BY pick_index ASC
            """,
            (pick_run_id, *PICK_TERMINAL_STATUSES),
        ).fetchall()
    return [_row_to_pick(r) for r in rows]


def get_pick(pick_id: int, db_path: str = DEFAULT_DB_PATH) -> PickedStory | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM picked_stories WHERE id = ?", (pick_id,)
        ).fetchone()
    return _row_to_pick(row) if row else None


def get_pick_by_index(
    pick_run_id: str, pick_index: int, db_path: str = DEFAULT_DB_PATH
) -> PickedStory | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM picked_stories WHERE pick_run_id = ? AND pick_index = ?",
            (pick_run_id, int(pick_index)),
        ).fetchone()
    return _row_to_pick(row) if row else None


def recent_published_categories(
    hours: int = 24,
    project: str = "coinography",
    db_path: str = DEFAULT_DB_PATH,
) -> list[str]:
    """Distinct categories of articles drafted/published within the window
    for a given project.

    Sourced from `articles` (canonical record of cards sent), filtered by
    `created_at >= now - hours` and `project`. Used by picker to bias away
    from recent categories *within the same site*. Different projects can
    legitimately repeat categories independently.
    """
    init_db(db_path)
    project = (project or "coinography").strip() or "coinography"
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT category FROM articles
            WHERE category IS NOT NULL AND category != ''
              AND project = ?
              AND created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            """,
            (project, f"-{int(hours)} hours"),
        ).fetchall()
    return [r["category"] for r in rows if r["category"]]


# ---------------------------------------------------------------------------
# headline_pool — 24x7 scanner candidate list (STRICTLY project-scoped)
# ---------------------------------------------------------------------------

POOL_VALID_STATUSES = {"fresh", "shown", "selected", "consumed"}


@dataclass
class PoolCandidate:
    id: int
    project: str
    url: str
    headline: str
    source: str | None
    pub_date: str | None
    summary: str | None
    corroborating_json: str | None
    scanned_at: str | None
    status: str


def _require_project(project: str | None) -> str:
    """Hard guard: pool/feed/state reads must always name a project.

    There is intentionally no default here so a caller can never accidentally
    query across projects and mix the two lists.
    """
    p = (project or "").strip()
    if not p:
        raise ValueError("project is required (no cross-project queries allowed)")
    return p


def _row_to_pool(row: sqlite3.Row) -> PoolCandidate:
    return PoolCandidate(
        id=row["id"],
        project=row["project"],
        url=row["url"],
        headline=row["headline"],
        source=row["source"],
        pub_date=row["pub_date"],
        summary=row["summary"],
        corroborating_json=row["corroborating_json"],
        scanned_at=row["scanned_at"],
        status=row["status"],
    )


def upsert_pool_candidates(
    project: str,
    candidates: list[dict[str, Any]],
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Insert/refresh scanner candidates for ONE project. Returns rows added.

    Existing (project, url) rows are left untouched (so a 'shown'/'selected'
    status is preserved); only genuinely new URLs are inserted as 'fresh'.
    """
    project = _require_project(project)
    if not candidates:
        return 0
    import json as _json

    added = 0
    init_db(db_path)
    with _connect(db_path) as conn:
        for cand in candidates:
            url = str(cand.get("url") or "").strip()
            headline = str(cand.get("headline") or "").strip()
            if not url or not headline:
                continue
            corro = cand.get("corroborating_sources")
            corro_json = _json.dumps(corro, ensure_ascii=False) if isinstance(corro, list) else None
            cur = conn.execute(
                """
                INSERT INTO headline_pool (
                  project, url, headline, source, pub_date, summary,
                  corroborating_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'fresh')
                ON CONFLICT(project, url) DO NOTHING
                """,
                (
                    project,
                    url,
                    headline,
                    str(cand.get("source") or "") or None,
                    str(cand.get("pub_date") or "") or None,
                    str(cand.get("summary") or "") or None,
                    corro_json,
                ),
            )
            if cur.rowcount:
                added += 1
        conn.commit()
    return added


def fresh_pool(
    project: str,
    *,
    limit: int = 10,
    max_age_hours: int | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> list[PoolCandidate]:
    """Top fresh candidates for ONE project, newest first. status='fresh' only."""
    project = _require_project(project)
    init_db(db_path)
    clauses = ["project = ?", "status = 'fresh'"]
    params: list[Any] = [project]
    if max_age_hours is not None:
        clauses.append("(scanned_at >= datetime('now', ?))")
        params.append(f"-{int(max_age_hours)} hours")
    where = " AND ".join(clauses)
    params.append(int(limit))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM headline_pool
            WHERE {where}
            ORDER BY (pub_date IS NULL), pub_date DESC, scanned_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_pool(r) for r in rows]


def available_for_backfill(
    project: str,
    *,
    exclude_urls: list[str] | None = None,
    limit: int = 50,
    db_path: str = DEFAULT_DB_PATH,
) -> list[PoolCandidate]:
    """Candidates usable to replace a failed story: status fresh OR shown
    (i.e. seen on a card but not yet selected/consumed), newest first,
    excluding the given URLs. STRICTLY one project.
    """
    project = _require_project(project)
    init_db(db_path)
    exclude = set(exclude_urls or [])
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM headline_pool
            WHERE project = ? AND status IN ('fresh', 'shown')
            ORDER BY (pub_date IS NULL), pub_date DESC, scanned_at DESC
            LIMIT ?
            """,
            (project, int(limit) + len(exclude)),
        ).fetchall()
    out = [_row_to_pool(r) for r in rows if r["url"] not in exclude]
    return out[:limit]


def pool_by_urls(
    project: str, urls: list[str], *, db_path: str = DEFAULT_DB_PATH
) -> list[PoolCandidate]:
    """Fetch specific pool rows by URL for ONE project (selection resolution)."""
    project = _require_project(project)
    urls = [u for u in (urls or []) if u]
    if not urls:
        return []
    init_db(db_path)
    placeholders = ",".join("?" * len(urls))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM headline_pool WHERE project = ? AND url IN ({placeholders})",
            (project, *urls),
        ).fetchall()
    by_url = {r["url"]: _row_to_pool(r) for r in rows}
    # Preserve caller order
    return [by_url[u] for u in urls if u in by_url]


def mark_pool(
    project: str,
    urls: list[str],
    status: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Set status for the given URLs within ONE project. Returns rows changed."""
    project = _require_project(project)
    if status not in POOL_VALID_STATUSES:
        raise ValueError(f"invalid pool status: {status}")
    urls = [u for u in (urls or []) if u]
    if not urls:
        return 0
    init_db(db_path)
    placeholders = ",".join("?" * len(urls))
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE headline_pool SET status = ? WHERE project = ? AND url IN ({placeholders})",
            (status, project, *urls),
        )
        conn.commit()
        return cur.rowcount


def prune_pool(
    project: str,
    *,
    older_than_hours: int = 168,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Delete stale rows for ONE project (default 7 days). Returns rows deleted."""
    project = _require_project(project)
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM headline_pool WHERE project = ? AND scanned_at < datetime('now', ?)",
            (project, f"-{int(older_than_hours)} hours"),
        )
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# feed_cards — the daily tap-to-select menu
# ---------------------------------------------------------------------------


@dataclass
class FeedCard:
    id: int
    feed_id: str
    project: str
    telegram_group: str
    telegram_message_id: int | None
    candidates_json: str
    selected_json: str
    status: str


def _row_to_feed_card(row: sqlite3.Row) -> FeedCard:
    return FeedCard(
        id=row["id"],
        feed_id=row["feed_id"],
        project=row["project"],
        telegram_group=row["telegram_group"],
        telegram_message_id=row["telegram_message_id"],
        candidates_json=row["candidates_json"],
        selected_json=row["selected_json"],
        status=row["status"],
    )


def insert_feed_card(
    feed_id: str,
    project: str,
    telegram_group: str,
    candidates: list[dict[str, Any]],
    *,
    telegram_message_id: int | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    project = _require_project(project)
    import json as _json

    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO feed_cards (
              feed_id, project, telegram_group, telegram_message_id,
              candidates_json, selected_json, status
            ) VALUES (?, ?, ?, ?, ?, '[]', 'open')
            ON CONFLICT(feed_id) DO UPDATE SET
              project = excluded.project,
              telegram_group = excluded.telegram_group,
              telegram_message_id = excluded.telegram_message_id,
              candidates_json = excluded.candidates_json,
              updated_at = datetime('now')
            """,
            (
                feed_id,
                project,
                str(telegram_group),
                int(telegram_message_id) if telegram_message_id is not None else None,
                _json.dumps(candidates, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM feed_cards WHERE feed_id = ?", (feed_id,)).fetchone()
        return int(row["id"]) if row else 0


def set_feed_card_message_id(
    feed_id: str, telegram_message_id: int, *, db_path: str = DEFAULT_DB_PATH
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE feed_cards SET telegram_message_id = ?, updated_at = datetime('now') WHERE feed_id = ?",
            (int(telegram_message_id), feed_id),
        )
        conn.commit()


def update_feed_card_candidates(
    feed_id: str, candidates: list[dict[str, Any]], *, db_path: str = DEFAULT_DB_PATH
) -> None:
    import json as _json

    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE feed_cards SET candidates_json = ?, updated_at = datetime('now') WHERE feed_id = ?",
            (_json.dumps(candidates, ensure_ascii=False), feed_id),
        )
        conn.commit()


def get_feed_card(feed_id: str, *, db_path: str = DEFAULT_DB_PATH) -> FeedCard | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM feed_cards WHERE feed_id = ?", (feed_id,)).fetchone()
    return _row_to_feed_card(row) if row else None


def get_feed_card_by_message(
    telegram_group: str, telegram_message_id: int, *, db_path: str = DEFAULT_DB_PATH
) -> FeedCard | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM feed_cards WHERE telegram_group = ? AND telegram_message_id = ? ORDER BY id DESC LIMIT 1",
            (str(telegram_group), int(telegram_message_id)),
        ).fetchone()
    return _row_to_feed_card(row) if row else None


def set_feed_card_selection(
    feed_id: str, selected: list[int], *, db_path: str = DEFAULT_DB_PATH
) -> None:
    import json as _json

    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE feed_cards SET selected_json = ?, updated_at = datetime('now') WHERE feed_id = ?",
            (_json.dumps(sorted(set(int(i) for i in selected))), feed_id),
        )
        conn.commit()


def set_feed_card_status(feed_id: str, status: str, *, db_path: str = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE feed_cards SET status = ?, updated_at = datetime('now') WHERE feed_id = ?",
            (status, feed_id),
        )
        conn.commit()


def claim_feed_card(feed_id: str, *, db_path: str = DEFAULT_DB_PATH) -> bool:
    """Atomically flip a feed card 'open' -> 'consumed'. Returns True only if
    THIS call won (status was 'open'); False if a prior tap already claimed it.
    Single-flight guard against duplicate oc_go taps spawning multiple runs."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE feed_cards SET status = 'consumed', updated_at = datetime('now') "
            "WHERE feed_id = ? AND status = 'open'",
            (feed_id,),
        )
        conn.commit()
        return cur.rowcount == 1


def claim_feed_card_index(
    feed_id: str, candidate_index: int, *, db_path: str = DEFAULT_DB_PATH
) -> bool:
    """Mark a single headline index as claimed on its feed card. Returns False if
    already claimed or index not found. Per-card duplicate guard for oc_go taps."""
    import json as _json

    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT candidates_json FROM feed_cards WHERE feed_id = ?", (feed_id,)
        ).fetchone()
        if not row:
            return False
        try:
            candidates = _json.loads(row["candidates_json"])
        except (ValueError, TypeError):
            return False
        found = False
        for c in candidates:
            if int(c.get("index", -1)) == int(candidate_index):
                if c.get("claimed"):
                    return False
                c["claimed"] = True
                found = True
                break
        if not found:
            return False
        conn.execute(
            "UPDATE feed_cards SET candidates_json = ?, updated_at = datetime('now') WHERE feed_id = ?",
            (_json.dumps(candidates, ensure_ascii=False), feed_id),
        )
        conn.commit()
        return True


# ---------------------------------------------------------------------------
# feed_jobs — serial execution queue for single-click feed taps
# ---------------------------------------------------------------------------


@dataclass
class FeedJob:
    id: int
    project: str
    feed_id: str
    candidate_index: int
    headline: str
    selection_file: str
    status: str
    started_at: str | None


def _row_to_feed_job(row: sqlite3.Row) -> FeedJob:
    return FeedJob(
        id=int(row["id"]),
        project=row["project"],
        feed_id=row["feed_id"],
        candidate_index=int(row["candidate_index"]),
        headline=row["headline"],
        selection_file=row["selection_file"],
        status=row["status"],
        started_at=row["started_at"],
    )


def enqueue_feed_job(
    project: str,
    feed_id: str,
    candidate_index: int,
    headline: str,
    selection_file: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    project = _require_project(project)
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO feed_jobs (
              project, feed_id, candidate_index, headline, selection_file, status
            ) VALUES (?, ?, ?, ?, ?, 'queued')
            """,
            (project, feed_id, int(candidate_index), headline, selection_file),
        )
        conn.commit()
        return int(cur.lastrowid)


def count_queued_jobs(*, project: str | None = None, db_path: str = DEFAULT_DB_PATH) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        if project:
            project = _require_project(project)
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM feed_jobs WHERE status = 'queued' AND project = ?",
                (project,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM feed_jobs WHERE status = 'queued'"
            ).fetchone()
        return int(row["n"]) if row else 0


def projects_with_queued_jobs(*, db_path: str = DEFAULT_DB_PATH) -> list[str]:
    """Distinct projects that have at least one queued feed job."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT project FROM feed_jobs WHERE status = 'queued' ORDER BY project"
        ).fetchall()
    return [r["project"] for r in rows]


def active_feed_job(*, db_path: str = DEFAULT_DB_PATH) -> FeedJob | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM feed_jobs WHERE status = 'running' ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return _row_to_feed_job(row) if row else None


def count_running_jobs(*, db_path: str = DEFAULT_DB_PATH) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM feed_jobs WHERE status = 'running'"
        ).fetchone()
        return int(row["n"]) if row else 0


def running_projects(*, db_path: str = DEFAULT_DB_PATH) -> set[str]:
    """Projects that currently have a running feed job."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT project FROM feed_jobs WHERE status = 'running'"
        ).fetchall()
    return {r["project"] for r in rows}


def claim_next_feed_job(
    *,
    project: str | None = None,
    max_concurrent: int = 1,
    db_path: str = DEFAULT_DB_PATH,
) -> FeedJob | None:
    """Atomically promote the oldest eligible queued job to running.

    When ``project`` is set, only claims from that project's queue (used by the
    self-draining FEED_DRAIN worker). Otherwise uses the global concurrency
    model: at most ``max_concurrent`` jobs run at once AND at most one job per
    project runs at a time.
    """
    max_concurrent = max(1, int(max_concurrent))
    init_db(db_path)
    with _connect(db_path) as conn:
        if project:
            project = _require_project(project)
            candidate = conn.execute(
                """
                SELECT * FROM feed_jobs
                WHERE status = 'queued' AND project = ?
                ORDER BY id ASC LIMIT 1
                """,
                (project,),
            ).fetchone()
            if candidate is None:
                return None
        else:
            running_rows = conn.execute(
                "SELECT project FROM feed_jobs WHERE status = 'running'"
            ).fetchall()
            if len(running_rows) >= max_concurrent:
                return None
            busy_projects = {r["project"] for r in running_rows}
            candidate = None
            for row in conn.execute(
                "SELECT * FROM feed_jobs WHERE status = 'queued' ORDER BY id ASC"
            ).fetchall():
                if row["project"] not in busy_projects:
                    candidate = row
                    break
            if candidate is None:
                return None
        cur = conn.execute(
            """
            UPDATE feed_jobs
            SET status = 'running', started_at = datetime('now'), updated_at = datetime('now')
            WHERE id = ? AND status = 'queued'
            """,
            (int(candidate["id"]),),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        fresh = conn.execute(
            "SELECT * FROM feed_jobs WHERE id = ?", (int(candidate["id"]),)
        ).fetchone()
    job = _row_to_feed_job(fresh) if fresh else None
    if job:
        try:
            from feed_job_card import edit_feed_job_card_state

            edit_feed_job_card_state(job, "running")
        except Exception:
            pass
    return job


def mark_feed_job(
    job_id: int, status: str, *, db_path: str = DEFAULT_DB_PATH
) -> None:
    if status not in ("done", "failed", "queued", "running"):
        raise ValueError(f"invalid feed job status: {status}")
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE feed_jobs SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, int(job_id)),
        )
        conn.commit()


def set_feed_job_step(
    job_id: int, step: str, *, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Record the live drain step so Telegram / DB agree on what is running."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE feed_jobs
            SET current_step = ?, step_updated_at = datetime('now'), updated_at = datetime('now')
            WHERE id = ?
            """,
            (str(step or "").strip() or None, int(job_id)),
        )
        conn.commit()


def reclaim_stale_jobs(
    stale_hours: float, *, db_path: str = DEFAULT_DB_PATH
) -> int:
    """Requeue long-running jobs with no artifacts so a new drainer can start.

    Dead Windows drains used to stay ``running`` + hold the lease, blocking the
    queue. First timeout requeues (retry). Very old rows still fail.
    """
    init_db(db_path)
    seconds = max(60, int(float(stale_hours) * 3600))
    fail_seconds = max(seconds * 3, 3600)
    requeued = 0
    failed = 0
    projects: set[str] = set()
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, project, started_at FROM feed_jobs
            WHERE status = 'running' AND started_at IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            job_id = int(row["id"])
            project = str(row["project"] or "")
            if running_job_has_progress(project, db_path=db_path):
                continue
            age = conn.execute(
                "SELECT CAST((julianday('now') - julianday(?)) * 86400 AS INTEGER)",
                (row["started_at"],),
            ).fetchone()[0]
            age_s = int(age or 0)
            if age_s < seconds:
                continue
            projects.add(project)
            if age_s >= fail_seconds:
                conn.execute(
                    "UPDATE feed_jobs SET status = 'failed', updated_at = datetime('now') WHERE id = ?",
                    (job_id,),
                )
                failed += 1
            else:
                conn.execute(
                    "UPDATE feed_jobs SET status = 'queued', updated_at = datetime('now') WHERE id = ?",
                    (job_id,),
                )
                requeued += 1
        conn.commit()
    for project in projects:
        if project:
            clear_drainer_lease(project, db_path=db_path)
    return requeued + failed


def running_job_has_progress(
    project: str, *, db_path: str = DEFAULT_DB_PATH
) -> bool:
    """True if the latest run dir for this project has real picker/research output."""
    roots = [
        r"C:\tmp",
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp"),
        "/tmp",
    ]
    # picker_input is written by bootstrap in seconds — not proof a worker is alive.
    markers = (
        ("picker", "picks.json"),
        ("research", "raw.json"),
        ("research", "validated.json"),
        ("article", "final.md"),
    )
    seen: set[str] = set()
    dirs: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        prefix = f"{project}-run-"
        for name in names:
            if not name.startswith(prefix):
                continue
            path = os.path.realpath(os.path.join(root, name))
            if path in seen or not os.path.isdir(path):
                continue
            seen.add(path)
            dirs.append(path)
    dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    if not dirs:
        return False
    run_dir = dirs[0]
    for parts in markers:
        fp = os.path.join(run_dir, *parts)
        try:
            if os.path.isfile(fp) and os.path.getsize(fp) > 80:
                return True
        except OSError:
            continue
    return False


def get_feed_job(job_id: int, *, db_path: str = DEFAULT_DB_PATH) -> FeedJob | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM feed_jobs WHERE id = ?", (int(job_id),)).fetchone()
    return _row_to_feed_job(row) if row else None


def get_latest_feed_job_for_card(
    feed_id: str, candidate_index: int, *, db_path: str = DEFAULT_DB_PATH
) -> FeedJob | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM feed_jobs
            WHERE feed_id = ? AND candidate_index = ?
            ORDER BY id DESC LIMIT 1
            """,
            (str(feed_id), int(candidate_index)),
        ).fetchone()
    return _row_to_feed_job(row) if row else None


# ---------------------------------------------------------------------------
# pipeline_state — key/value (group-level idle clock, daily counters)
# ---------------------------------------------------------------------------


def set_state(project: str, key: str, value: str, *, db_path: str = DEFAULT_DB_PATH) -> None:
    project = _require_project(project)
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pipeline_state (project, key, value, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(project, key) DO UPDATE SET
              value = excluded.value, updated_at = datetime('now')
            """,
            (project, key, value),
        )
        conn.commit()


def get_state(project: str, key: str, *, db_path: str = DEFAULT_DB_PATH) -> str | None:
    project = _require_project(project)
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM pipeline_state WHERE project = ? AND key = ?",
            (project, key),
        ).fetchone()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# feed drainer lease — one isolated worker per project
# ---------------------------------------------------------------------------

DRAINER_LEASE_KEY = "drainer_lease"
DEFAULT_DRAINER_LEASE_STALE_MIN = 30.0


def set_drainer_lease(
    project: str, run_id: str = "active", *, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Stamp/refresh the per-project feed drainer lease (updated_at = now)."""
    set_state(project, DRAINER_LEASE_KEY, run_id, db_path=db_path)


def drainer_active(
    project: str,
    *,
    stale_minutes: float = DEFAULT_DRAINER_LEASE_STALE_MIN,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """True when a live drain is actually running for this project.

    A lease alone is not enough. Failed Sieve used to re-stamp the lease from
    the Telegram board while the job sat ``queued`` — dispatcher then skipped
    the project for 45 minutes with no worker.
    """
    project = _require_project(project)
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT value,
                   (julianday('now') - julianday(updated_at)) * 24.0 * 60.0 AS age_min
            FROM pipeline_state
            WHERE project = ? AND key = ?
            """,
            (project, DRAINER_LEASE_KEY),
        ).fetchone()
    if not row or not row["value"]:
        return False
    age = row["age_min"]
    if age is None:
        return False
    age_min = float(age)
    if age_min > float(stale_minutes):
        return False
    if project in running_projects(db_path=db_path):
        return True
    # Drain Popen stamps the lease a few seconds before claim_next_feed_job.
    return age_min <= 2.0


def clear_drainer_lease(project: str, *, db_path: str = DEFAULT_DB_PATH) -> None:
    """Remove the drainer lease so a new worker may be kicked."""
    project = _require_project(project)
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM pipeline_state WHERE project = ? AND key = ?",
            (project, DRAINER_LEASE_KEY),
        )
        conn.commit()


def touch_last_contact(*, db_path: str = DEFAULT_DB_PATH) -> None:
    """Stamp the GROUP-level last_contact_at to now (resets the 48h idle clock)."""
    set_state(GLOBAL_PROJECT, "last_contact_at", "", db_path=db_path)
    # set_state stamps updated_at = now; we read updated_at as the timestamp.


def hours_since_last_contact(*, db_path: str = DEFAULT_DB_PATH) -> float | None:
    """Hours since the last group engagement, or None if never recorded."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT (julianday('now') - julianday(updated_at)) * 24.0 AS h
            FROM pipeline_state WHERE project = ? AND key = 'last_contact_at'
            """,
            (GLOBAL_PROJECT,),
        ).fetchone()
    if not row or row["h"] is None:
        return None
    return float(row["h"])


def published_today(project: str, *, db_path: str = DEFAULT_DB_PATH) -> int:
    """Count of stories that completed the pipeline today (local day) for a
    project: picked_stories with status 'published' updated today.
    """
    project = _require_project(project)
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM picked_stories
            WHERE project = ? AND status = 'published'
              AND date(updated_at, 'localtime') = date('now', 'localtime')
            """,
            (project,),
        ).fetchone()
    return int(row["c"]) if row else 0


# ---------------------------------------------------------------------------
# onboard_sessions — deterministic /onboard flow state (project-onboarder
# plugin + onboard_project.py). Mirrors edit_sessions; fully independent
# table, safe to clear/drop without touching any other data.
# ---------------------------------------------------------------------------

@dataclass
class OnboardSession:
    id: int
    chat_id: str
    user_id: str
    step: str
    answers_json: str
    prompt_message_id: int | None


def _row_to_onboard_session(row: sqlite3.Row) -> OnboardSession:
    return OnboardSession(
        id=row["id"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        step=row["step"],
        answers_json=row["answers_json"],
        prompt_message_id=row["prompt_message_id"],
    )


def get_onboard_session(
    chat_id: str, user_id: str, *, db_path: str = DEFAULT_DB_PATH
) -> OnboardSession | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM onboard_sessions WHERE chat_id = ? AND user_id = ?",
            (str(chat_id), str(user_id)),
        ).fetchone()
    return _row_to_onboard_session(row) if row else None


def get_any_onboard_session_for_chat(
    chat_id: str, *, db_path: str = DEFAULT_DB_PATH
) -> OnboardSession | None:
    """Return the most recent onboarding session in a chat, regardless of user.

    Used by the plugin to decide whether a plain (non-reply) text message in
    a launch group should be treated as an onboarding answer.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM onboard_sessions WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (str(chat_id),),
        ).fetchone()
    return _row_to_onboard_session(row) if row else None


def upsert_onboard_session(
    chat_id: str,
    user_id: str,
    step: str,
    *,
    answers_json: str = "{}",
    prompt_message_id: int | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO onboard_sessions (chat_id, user_id, step, answers_json, prompt_message_id, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
              step = excluded.step,
              answers_json = excluded.answers_json,
              prompt_message_id = excluded.prompt_message_id,
              updated_at = datetime('now')
            """,
            (str(chat_id), str(user_id), step, answers_json, prompt_message_id),
        )
        conn.commit()


def clear_onboard_session(chat_id: str, user_id: str, *, db_path: str = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM onboard_sessions WHERE chat_id = ? AND user_id = ?",
            (str(chat_id), str(user_id)),
        )
        conn.commit()
