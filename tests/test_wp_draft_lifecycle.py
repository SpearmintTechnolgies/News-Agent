"""WordPress draft lifecycle tests - ZERO live API calls.

Mock tests only; test draft must remain a draft.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from newsagent_v2.wordpress.config import WordPressConfig
from newsagent_v2.wordpress.draft_lifecycle import (
    DraftResult,
    WordPressDraftLifecycle,
)
from newsagent_v2.wordpress.draft_store import WordPressDraftStore


class MockTransport:
    """Mock WordPress REST transport."""

    def __init__(self) -> None:
        self.posts: dict[str, dict[str, Any]] = {}
        self.media: dict[str, dict[str, Any]] = {}
        self._post_counter = 100
        self._media_counter = 200
        self._call_log: list[tuple[str, str, dict[str, Any]]] = []

    def _next_post_id(self) -> int:
        self._post_counter += 1
        return self._post_counter

    def _next_media_id(self) -> int:
        self._media_counter += 1
        return self._media_counter

    def __call__(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Simulate WordPress REST calls."""
        self._call_log.append((method, url, kwargs))
        # Parse endpoint from URL
        if "/wp/v2/posts" in url:
            return self._handle_posts(method, url, kwargs)
        if "/wp/v2/media" in url:
            return self._handle_media(method, url, kwargs)
        if "rankmath/v1/updateMeta" in url:
            if method == "POST":
                body = kwargs.get("json") or {}
                return {"ok": True, "payload": {"success": True, "objectID": body.get("objectID")}}
            return {"ok": False, "error": "Method not allowed", "status_code": 405}
        return {"ok": False, "error": "Unknown endpoint"}

    def _handle_posts(self, method: str, url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Handle post endpoints."""
        # Extract post ID from URL if present
        parts = url.split("/posts/")
        post_id_str = parts[1] if len(parts) > 1 else None

        try:
            if method == "POST":
                # Create new post
                json_body = kwargs.get("json", {})
                post_id = self._next_post_id()
                post_data = {
                    "id": post_id,
                    "title": {"rendered": json_body.get("title", "")},
                    "content": {"rendered": json_body.get("content", "")},
                    "status": json_body.get("status", "draft"),
                    "link": f"https://example.com/{json_body.get('slug', f'post-{post_id}')}",
                    "modified": "2024-01-15T10:00:00",
                    "slug": json_body.get("slug", f"post-{post_id}"),
                }
                if json_body.get("featured_media"):
                    post_data["featured_media"] = json_body["featured_media"]
                self.posts[str(post_id)] = post_data
                return {"ok": True, "payload": post_data}

            if method == "PUT" and post_id_str:
                # Update existing post
                post_id = int(post_id_str)
                json_body = kwargs.get("json", {})
                if post_id_str not in self.posts:
                    return {"ok": False, "error": "Post not found", "status_code": 404}

                post = self.posts[post_id_str]
                if "status" in json_body:
                    post["status"] = json_body["status"]
                post["modified"] = "2024-01-15T11:00:00"
                return {"ok": True, "payload": post}

            if method == "GET" and post_id_str:
                # Retrieve post
                if post_id_str not in self.posts:
                    return {"ok": False, "error": "Post not found", "status_code": 404}
                return {"ok": True, "payload": self.posts[post_id_str]}

            return {"ok": False, "error": "Unsupported method", "status_code": 405}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _handle_media(self, method: str, url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Handle media upload and alt_text updates."""
        # POST /media/{id} with JSON = alt_text update
        parts = url.rstrip("/").split("/media/")
        if method == "POST" and len(parts) > 1 and parts[1].isdigit():
            media_id = parts[1]
            body = kwargs.get("json") or {}
            if media_id in self.media and "alt_text" in body:
                self.media[media_id]["alt_text"] = body["alt_text"]
            return {"ok": True, "payload": {"id": int(media_id), "alt_text": body.get("alt_text", "")}}
        if method == "POST":
            media_id = self._next_media_id()
            media_data = {
                "id": media_id,
                "source_url": f"https://example.com/wp-content/uploads/media-{media_id}.png",
                "title": {"rendered": "Featured Image"},
            }
            self.media[str(media_id)] = media_data
            return {"ok": True, "payload": media_data}
        return {"ok": False, "error": "Unsupported method"}


class TestWordPressDraftLifecycle(unittest.TestCase):
    """Test WordPress draft lifecycle with mocks."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.store = WordPressDraftStore(root=Path(self.temp_dir))
        self.config = WordPressConfig(
            base_url="https://example.com",
            username="testuser",
            app_password="testpass12345",
        )
        self.transport = MockTransport()
        self.lifecycle = WordPressDraftLifecycle(
            config=self.config,
            transport=self.transport,
            store=self.store,
        )
        self.article = {
            "headline": "Test Headline",
            "slug": "test-headline",
            "article_body": "Test article content.",
            "dek": "Test dek text.",
            "meta_description": "Test meta description.",
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_create_draft(self) -> None:
        """Test creating new WordPress draft."""
        result = self.lifecycle.create_or_update_draft(
            event_id="evt-test-001",
            article=self.article,
            article_version="v1",
            image_path=None,
            image_version=None,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.created)
        self.assertFalse(result.updated)
        self.assertEqual(result.event_id, "evt-test-001")
        self.assertIsNotNone(result.wp_post_id)
        self.assertIsNotNone(result.wp_url)
        self.assertEqual(result.status, "draft")

        # Check persistence
        record = self.store.load("evt-test-001")
        self.assertIsNotNone(record)
        self.assertEqual(record.wp_post_id, result.wp_post_id)
        self.assertEqual(record.article_version, "v1")
        self.assertEqual(record.status, "draft")

    def test_02_update_existing_draft(self) -> None:
        """Test updating existing draft."""
        # First create
        result1 = self.lifecycle.create_or_update_draft(
            event_id="evt-test-002",
            article=self.article,
            article_version="v1",
        )
        self.assertTrue(result1.ok)
        original_id = result1.wp_post_id

        # Update with new content
        updated_article = {**self.article, "headline": "Updated Headline"}
        result2 = self.lifecycle.create_or_update_draft(
            event_id="evt-test-002",
            article=updated_article,
            article_version="v2",
        )
        self.assertTrue(result2.ok)
        self.assertTrue(result2.updated)
        self.assertFalse(result2.created)
        self.assertEqual(result2.wp_post_id, original_id)  # Same post

        # Check persistence updated
        record = self.store.load("evt-test-002")
        self.assertEqual(record.article_version, "v2")

    def test_03_publish_draft(self) -> None:
        """Test publishing a draft."""
        # Create first
        result = self.lifecycle.create_or_update_draft(
            event_id="evt-test-003",
            article=self.article,
            article_version="v1",
        )
        self.assertTrue(result.ok)

        # Publish
        publish_result = self.lifecycle.publish_draft("evt-test-003")
        self.assertTrue(publish_result.ok)
        self.assertEqual(publish_result.status, "publish")

        # Check persistence
        record = self.store.load("evt-test-003")
        self.assertEqual(record.status, "publish")

    def test_03b_publish_draft_is_idempotent(self) -> None:
        """Publishing an already-published draft does not reissue the WordPress publish call."""
        result = self.lifecycle.create_or_update_draft(
            event_id="evt-test-003b",
            article=self.article,
            article_version="v1",
        )
        self.assertTrue(result.ok)

        first = self.lifecycle.publish_draft("evt-test-003b")
        self.assertTrue(first.ok)
        self.assertEqual(first.status, "publish")

        second = self.lifecycle.publish_draft("evt-test-003b")
        self.assertTrue(second.ok)
        self.assertEqual(second.status, "publish")
        self.assertEqual(second.wp_post_id, first.wp_post_id)
        self.assertFalse(second.updated)

        put_calls = sum(
            1
            for method, url, *_ in getattr(self.transport, "_call_log", [])
            if method == "PUT" and "/posts/" in url
        )
        self.assertEqual(put_calls, 1)

    def test_04_unpublish_draft(self) -> None:
        """Test unpublishing back to draft."""
        # Create and publish
        self.lifecycle.create_or_update_draft(
            event_id="evt-test-004",
            article=self.article,
            article_version="v1",
        )
        self.lifecycle.publish_draft("evt-test-004")

        # Unpublish
        result = self.lifecycle.unpublish_draft("evt-test-004")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "draft")

        # Check persistence
        record = self.store.load("evt-test-004")
        self.assertEqual(record.status, "draft")

    def test_05_retrieve_wp_state(self) -> None:
        """Test retrieving WordPress state."""
        # Create
        result = self.lifecycle.create_or_update_draft(
            event_id="evt-test-005",
            article=self.article,
            article_version="v1",
        )
        self.assertTrue(result.ok)

        # Retrieve
        state = self.lifecycle.retrieve_wp_state("evt-test-005")
        self.assertTrue(state["ok"])
        self.assertEqual(state["event_id"], "evt-test-005")
        self.assertEqual(state["status"], "draft")
        self.assertEqual(state["wp_post_id"], result.wp_post_id)

    def test_06_draft_not_found_errors(self) -> None:
        """Test error handling for missing drafts."""
        # Publish non-existent
        result = self.lifecycle.publish_draft("evt-nonexistent")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "draft_not_found")

        # Unpublish non-existent
        result = self.lifecycle.unpublish_draft("evt-nonexistent")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "draft_not_found")

        # Retrieve non-existent
        state = self.lifecycle.retrieve_wp_state("evt-nonexistent")
        self.assertFalse(state["ok"])
        self.assertEqual(state["error"], "draft_not_found")

    def test_07_draft_remains_draft(self) -> None:
        """Test that drafts stay drafts unless explicitly published."""
        result = self.lifecycle.create_or_update_draft(
            event_id="evt-test-007",
            article=self.article,
            article_version="v1",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "draft")

        # Double-check persisted
        record = self.store.load("evt-test-007")
        self.assertEqual(record.status, "draft")

        # Mock transport shows draft
        state = self.lifecycle.retrieve_wp_state("evt-test-007")
        self.assertEqual(state["status"], "draft")

    def test_08_featured_image_upload(self) -> None:
        """Test creating draft with featured image."""
        # Create temp image file
        temp_img = Path(self.temp_dir) / "test_image.png"
        temp_img.write_bytes(b"fake png data")

        result = self.lifecycle.create_or_update_draft(
            event_id="evt-test-008",
            article=self.article,
            article_version="v1",
            image_path=str(temp_img),
            image_version="v1",
        )
        self.assertTrue(result.ok)

        # Check record has media
        record = self.store.load("evt-test-008")
        self.assertIsNotNone(record.featured_media_id)
        self.assertEqual(record.image_version, "v1")

    def test_09_different_events_separate_drafts(self) -> None:
        """Test different event_ids get separate drafts."""
        for i in range(3):
            result = self.lifecycle.create_or_update_draft(
                event_id=f"evt-separate-{i}",
                article=self.article,
                article_version="v1",
            )
            self.assertTrue(result.ok)

        # Verify separate records
        ids = [r.wp_post_id for r in self.store.list_all()]
        self.assertEqual(len(set(ids)), 3)

    def test_10_persistence_survives_reload(self) -> None:
        """Test draft persists across store reload."""
        # Create with first store
        self.lifecycle.create_or_update_draft(
            event_id="evt-persist-010",
            article=self.article,
            article_version="v1",
        )

        # New store instance
        store2 = WordPressDraftStore(root=Path(self.temp_dir))
        record = store2.load("evt-persist-010")
        self.assertIsNotNone(record)
        self.assertEqual(record.event_id, "evt-persist-010")


class TestWordPressDraftStore(unittest.TestCase):
    """Test WordPress draft store directly."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.store = WordPressDraftStore(root=Path(self.temp_dir))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_load(self) -> None:
        """Test saving and loading a record."""
        from datetime import datetime, timezone
        from newsagent_v2.wordpress.draft_store import WordPressDraftRecord

        now = datetime.now(timezone.utc).isoformat()
        record = WordPressDraftRecord(
            event_id="evt-001",
            wp_post_id=123,
            article_version="v1",
            image_version=None,
            status="draft",
            wp_url="https://example.com/test",
            created_at=now,
            updated_at=now,
            wp_modified=None,
        )

        path = self.store.save(record)
        self.assertTrue(path.exists())

        loaded = self.store.load("evt-001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.event_id, "evt-001")
        self.assertEqual(loaded.wp_post_id, 123)

    def test_exists_and_delete(self) -> None:
        """Test exists and delete methods."""
        self.assertFalse(self.store.exists("evt-delete"))

        from datetime import datetime, timezone
        from newsagent_v2.wordpress.draft_store import WordPressDraftRecord

        now = datetime.now(timezone.utc).isoformat()
        record = WordPressDraftRecord(
            event_id="evt-delete",
            wp_post_id=456,
            article_version="v1",
            image_version=None,
            status="draft",
            wp_url="https://example.com/test",
            created_at=now,
            updated_at=now,
            wp_modified=None,
        )
        self.store.save(record)

        self.assertTrue(self.store.exists("evt-delete"))
        self.assertTrue(self.store.delete("evt-delete"))
        self.assertFalse(self.store.exists("evt-delete"))
        self.assertFalse(self.store.delete("evt-delete"))  # Already deleted


if __name__ == "__main__":
    unittest.main(verbosity=2)
