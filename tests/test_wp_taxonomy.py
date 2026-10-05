"""WordPress taxonomy tests - ZERO live API calls."""

from __future__ import annotations

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
from newsagent_v2.wordpress.taxonomy import (
    TaxonomyResult,
    WordPressTaxonomyResolver,
)


class MockTransport:
    """Enhanced mock WordPress REST transport with taxonomy."""

    def __init__(self) -> None:
        self.posts: dict[str, dict[str, Any]] = {}
        self.categories: dict[str, dict[str, Any]] = {
            "1": {"id": 1, "name": "Crypto"},
            "2": {"id": 2, "name": "Bitcoin"},
            "3": {"id": 3, "name": "News"},
        }
        self.tags: dict[str, dict[str, Any]] = {
            "10": {"id": 10, "name": "Blockchain"},
            "11": {"id": 11, "name": "DeFi"},
        }
        self._post_counter = 100
        self._cat_counter = 10
        self._tag_counter = 50

    def __call__(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if "/categories" in url:
            return self._handle_categories(method, url, kwargs)
        if "/tags" in url:
            return self._handle_tags(method, url, kwargs)
        if "/posts" in url:
            return self._handle_posts(method, url, kwargs)
        if "/media" in url:
            return {"ok": True, "payload": {"id": 999}}
        if "rankmath/v1/updateMeta" in url:
            return {"ok": True, "payload": {"slug": True}}
        return {"ok": False, "error": "Unknown endpoint"}

    def _handle_categories(self, method: str, url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if method == "GET":
            if "?search=" in url:
                # Search categories
                search_term = url.split("?search=")[1].split("&")[0].lower()
                results = [c for c in self.categories.values() if search_term in c["name"].lower()]
                return {"ok": True, "payload": results}
            # List all
            return {"ok": True, "payload": list(self.categories.values())}
        if method == "POST":
            # Create category
            name = kwargs.get("json", {}).get("name", "")
            self._cat_counter += 1
            new_cat = {"id": self._cat_counter, "name": name}
            self.categories[str(self._cat_counter)] = new_cat
            return {"ok": True, "payload": new_cat}
        return {"ok": False, "error": "Bad method"}

    def _handle_tags(self, method: str, url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if method == "GET":
            if "?search=" in url:
                search_term = url.split("?search=")[1].split("&")[0].lower()
                results = [t for t in self.tags.values() if search_term in t["name"].lower()]
                return {"ok": True, "payload": results}
            return {"ok": True, "payload": list(self.tags.values())}
        if method == "POST":
            name = kwargs.get("json", {}).get("name", "")
            self._tag_counter += 1
            new_tag = {"id": self._tag_counter, "name": name}
            self.tags[str(self._tag_counter)] = new_tag
            return {"ok": True, "payload": new_tag}
        return {"ok": False, "error": "Bad method"}

    def _handle_posts(self, method: str, url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        parts = url.split("/posts/")
        post_id_str = parts[1] if len(parts) > 1 else None

        if method == "POST":
            json_body = kwargs.get("json", {})
            self._post_counter += 1
            post_data = {
                "id": self._post_counter,
                "title": {"rendered": json_body.get("title", "")},
                "content": {"rendered": json_body.get("content", "")},
                "status": json_body.get("status", "draft"),
                "link": f"https://example.com/{json_body.get('slug', f'post-{self._post_counter}')}",
                "modified": "2024-01-15T10:00:00",
                "slug": json_body.get("slug", f"post-{self._post_counter}"),
                "categories": json_body.get("categories", []),
                "tags": json_body.get("tags", []),
            }
            self.posts[str(self._post_counter)] = post_data
            return {"ok": True, "payload": post_data}

        if method == "PUT" and post_id_str:
            if post_id_str not in self.posts:
                return {"ok": False, "error": "Post not found", "status_code": 404}

            json_body = kwargs.get("json", {})
            post = self.posts[post_id_str]

            if "status" in json_body:
                post["status"] = json_body["status"]
            if "categories" in json_body:
                post["categories"] = json_body["categories"]
            if "tags" in json_body:
                post["tags"] = json_body["tags"]

            post["modified"] = "2024-01-15T11:00:00"
            return {"ok": True, "payload": post}

        if method == "GET" and post_id_str:
            if post_id_str in self.posts:
                return {"ok": True, "payload": self.posts[post_id_str]}
            return {"ok": False, "error": "Post not found", "status_code": 404}

        return {"ok": False, "error": "Bad method"}


class TestWordPressTaxonomyResolver(unittest.TestCase):
    """Test taxonomy resolution."""

    def setUp(self) -> None:
        self.config = WordPressConfig(
            base_url="https://example.com",
            username="test",
            app_password="testpass",
        )
        self.transport = MockTransport()
        self.resolver = WordPressTaxonomyResolver(self.config, self.transport)

    def test_resolve_existing_category(self) -> None:
        """Test resolving existing category by name."""
        result = self.resolver.resolve(
            categories=["Crypto"],
            tags=[],
            create_missing=False,
        )
        self.assertEqual(result.category_ids, [1])
        self.assertEqual(result.created_categories, [])

    def test_resolve_existing_tag(self) -> None:
        """Test resolving existing tag by name."""
        result = self.resolver.resolve(
            categories=[],
            tags=["Blockchain"],
            create_missing=False,
        )
        self.assertEqual(result.tag_ids, [10])
        self.assertEqual(result.created_tags, [])

    def test_create_missing_category(self) -> None:
        """Test creating missing category."""
        result = self.resolver.resolve(
            categories=["NewCategory"],
            tags=[],
            create_missing=True,
        )
        self.assertEqual(len(result.category_ids), 1)
        self.assertEqual(result.created_categories, ["NewCategory"])
        self.assertTrue(all(isinstance(cid, int) for cid in result.category_ids))

    def test_create_missing_tag(self) -> None:
        """Test creating missing tag."""
        result = self.resolver.resolve(
            categories=[],
            tags=["NewTrend"],
            create_missing=True,
        )
        self.assertEqual(len(result.tag_ids), 1)
        self.assertEqual(result.created_tags, ["NewTrend"])

    def test_no_create_when_false(self) -> None:
        """Test that missing terms are skipped when create_missing=False."""
        result = self.resolver.resolve(
            categories=["MissingCat"],
            tags=["MissingTag"],
            create_missing=False,
        )
        self.assertEqual(result.category_ids, [])
        self.assertEqual(result.tag_ids, [])
        self.assertEqual(result.created_categories, [])
        self.assertEqual(result.created_tags, [])

    def test_multiple_categories_tags(self) -> None:
        """Test resolving multiple categories and tags."""
        result = self.resolver.resolve(
            categories=["Crypto", "Bitcoin"],
            tags=["Blockchain", "DeFi", "NewTag"],
            create_missing=True,
        )
        # Should find existing and create missing
        self.assertEqual(len(result.category_ids), 2)  # Crypto, Bitcoin
        self.assertEqual(len(result.tag_ids), 3)  # Blockchain, DeFi, NewTag (created)
        self.assertEqual(result.created_tags, ["NewTag"])


class TestWordPressDraftWithTaxonomy(unittest.TestCase):
    """Test draft lifecycle with taxonomy integration."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.store = WordPressDraftStore(root=Path(self.temp_dir))
        self.config = WordPressConfig(
            base_url="https://example.com",
            username="test",
            app_password="testpass",
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
            "article_body": "Test content.",
            "dek": "Test dek.",
            "meta_description": "Test meta.",
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_draft_with_categories_tags(self) -> None:
        """Test creating draft with categories and tags."""
        result = self.lifecycle.create_or_update_draft(
            event_id="evt-tax-001",
            article=self.article,
            article_version="v1",
            image_path=None,
            image_version=None,
            categories=["Crypto", "Bitcoin"],
            tags=["Blockchain"],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.category_ids, [1, 2])  # Crypto=1, Bitcoin=2
        self.assertEqual(result.tag_ids, [10])  # Blockchain=10

    def test_create_draft_creates_missing_taxonomy(self) -> None:
        """Test that missing categories/tags are created."""
        result = self.lifecycle.create_or_update_draft(
            event_id="evt-tax-002",
            article=self.article,
            article_version="v1",
            categories=["Crypto", "NewCategory"],  # NewCategory doesn't exist
            tags=["NewTag"],  # NewTag doesn't exist
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.category_ids), 2)
        self.assertEqual(len(result.tag_ids), 1)
        self.assertIn("NewCategory", result.taxonomy_created)
        self.assertIn("NewTag", result.taxonomy_created)

    def test_update_draft_changes_categories(self) -> None:
        """Test updating draft with different categories/tags."""
        # First create
        result1 = self.lifecycle.create_or_update_draft(
            event_id="evt-tax-003",
            article=self.article,
            article_version="v1",
            categories=["Crypto"],
            tags=["Blockchain"],
        )
        self.assertTrue(result1.ok)
        self.assertEqual(result1.category_ids, [1])

        # Update with different taxonomy
        result2 = self.lifecycle.create_or_update_draft(
            event_id="evt-tax-003",
            article=self.article,
            article_version="v2",
            categories=["Bitcoin", "News"],
            tags=["DeFi", "NewTag"],
        )
        self.assertTrue(result2.ok)
        self.assertTrue(result2.updated)
        self.assertEqual(result2.category_ids, [2, 3])  # Bitcoin=2, News=3
        self.assertEqual(len(result2.tag_ids), 2)

    def test_categories_tags_in_wp_body(self) -> None:
        """Verify categories/tags are sent in POST body."""
        self.lifecycle.create_or_update_draft(
            event_id="evt-tax-004",
            article=self.article,
            article_version="v1",
            categories=["Crypto", "Bitcoin"],
            tags=["Blockchain"],
        )
        # Check mock transport received them
        created_post = self.transport.posts.get("101")  # First created post
        self.assertIsNotNone(created_post)
        self.assertEqual(created_post.get("categories"), [1, 2])
        self.assertEqual(created_post.get("tags"), [10])

    def test_empty_categories_tags(self) -> None:
        """Test draft without categories/tags works."""
        result = self.lifecycle.create_or_update_draft(
            event_id="evt-tax-005",
            article=self.article,
            article_version="v1",
            categories=None,
            tags=None,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.category_ids, [])
        self.assertEqual(result.tag_ids, [])


class TestTaxonomyEdgeCases(unittest.TestCase):
    """Test edge cases."""

    def setUp(self) -> None:
        self.config = WordPressConfig(
            base_url="https://example.com",
            username="test",
            app_password="testpass",
        )
        self.transport = MockTransport()
        self.resolver = WordPressTaxonomyResolver(self.config, self.transport)

    def test_empty_strings_filtered(self) -> None:
        """Empty category/tag names are filtered out."""
        result = self.resolver.resolve(
            categories=["", "  ", "Crypto"],
            tags=["", "Blockchain"],
            create_missing=False,
        )
        self.assertEqual(result.category_ids, [1])
        self.assertEqual(result.tag_ids, [10])

    def test_case_insensitive_match(self) -> None:
        """Category/tag matching is case-insensitive."""
        result = self.resolver.resolve(
            categories=["crypto", "CRYPTO", "Crypto"],  # All should match same
            tags=["blockchain", "BLOCKCHAIN"],
            create_missing=False,
        )
        self.assertEqual(result.category_ids, [1])  # Only one unique
        self.assertEqual(result.tag_ids, [10])


if __name__ == "__main__":
    unittest.main(verbosity=2)
