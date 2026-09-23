"""No-network regression: WP posts payload must not include rank_math_* or null canonical."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from newsagent_v2.wordpress.config import WordPressConfig
from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle
from newsagent_v2.wordpress.draft_store import WordPressDraftStore
from newsagent_v2.wordpress.seo_metadata import (
    SEOMetadata,
    validate_wordpress_seo_meta,
)


class CapturingTransport:
    """Capture REST payloads; never touches the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._post_id = 9000

    def __call__(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, url, dict(kwargs)))
        if method == "POST" and "/wp/v2/posts" in url:
            self._post_id += 1
            body = kwargs.get("json") or {}
            return {
                "ok": True,
                "payload": {
                    "id": self._post_id,
                    "link": f"https://example.com/?p={self._post_id}",
                    "modified": "2026-09-22T12:00:00",
                    "slug": body.get("slug") or f"post-{self._post_id}",
                    "status": "draft",
                },
            }
        if method == "PUT" and "/wp/v2/posts/" in url:
            body = kwargs.get("json") or {}
            return {
                "ok": True,
                "payload": {
                    "id": 9001,
                    "link": "https://example.com/?p=9001",
                    "modified": "2026-09-22T12:01:00",
                    "slug": body.get("slug") or "updated",
                    "status": body.get("status") or "draft",
                },
            }
        if method == "POST" and "/wp/v2/media" in url:
            return {"ok": True, "payload": {"id": 8001, "source_url": "https://example.com/m.png"}}
        if method == "POST" and "rankmath/v1/updateMeta" in url:
            body = kwargs.get("json") or {}
            return {"ok": True, "payload": {"success": True, "objectID": body.get("objectID")}}
        # Taxonomy / unrelated: soft-fail offline
        return {"ok": False, "error": "offline-mock"}


class TestWpPayloadNoRankMath(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.store = WordPressDraftStore(root=Path(self.temp_dir))
        self.config = WordPressConfig(
            base_url="https://example.com",
            username="testuser",
            app_password="testpass12345",
        )
        self.transport = CapturingTransport()
        self.lifecycle = WordPressDraftLifecycle(
            config=self.config,
            transport=self.transport,
            store=self.store,
        )
        self.article = {
            "headline": "US Spot Bitcoin ETFs Log Nearly $1 Billion Daily Inflow",
            "slug": "us-spot-bitcoin-etfs-nearly-1-billion",
            "article_body": " ".join(["Word"] * 450),
            "dek": "Funds saw nearly $1 billion in net inflows on Monday.",
            "meta_description": "US spot bitcoin ETFs saw nearly $1 billion in net inflows on Monday.",
            "seo_title": "US Spot Bitcoin ETFs Nearly $1 Billion Daily Inflow",
            "canonical_url": None,  # must coerce to ""
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _assert_safe_meta(self, meta: dict[str, Any]) -> None:
        self.assertIsInstance(meta, dict)
        rank_keys = [k for k in meta if str(k).startswith("rank_math_")]
        self.assertEqual(rank_keys, [], f"forbidden rank_math keys in payload: {rank_keys}")
        self.assertIn("newsagent_seo_title", meta)
        self.assertIn("newsagent_focus_keyphrase", meta)
        self.assertIn("newsagent_canonical_url", meta)
        self.assertIsNotNone(meta["newsagent_canonical_url"])
        self.assertEqual(meta["newsagent_canonical_url"], "")
        # JSON null must never be present for canonical
        import json
        wire = json.loads(json.dumps(meta))
        self.assertIsNot(wire.get("newsagent_canonical_url"), None)
        self.assertFalse(any(str(k).startswith("rank_math_") for k in wire))

    def test_create_payload_has_no_rank_math_and_no_null_canonical(self) -> None:
        result = self.lifecycle.create_or_update_draft(
            event_id="evt-payload-regression",
            article=self.article,
            article_version="v1",
            format_html=False,
        )
        self.assertTrue(result.ok, msg=result.error)
        post_calls = [
            (m, u, kw) for m, u, kw in self.transport.calls
            if m == "POST" and "/wp/v2/posts" in u
        ]
        self.assertEqual(len(post_calls), 1)
        body = post_calls[0][2].get("json") or {}
        self._assert_safe_meta(body.get("meta") or {})

    def test_update_payload_has_no_rank_math_and_no_null_canonical(self) -> None:
        # Seed existing draft record so create_or_update takes PUT path
        first = self.lifecycle.create_or_update_draft(
            event_id="evt-payload-update",
            article=self.article,
            article_version="v1",
            format_html=False,
        )
        self.assertTrue(first.ok, msg=first.error)
        self.transport.calls.clear()
        # Ensure mock has the post id for PUT
        self.transport.calls  # noqa: B018 — clarity
        # Mock PUT handler does not require prior posts dict; CapturingTransport returns ok
        second = self.lifecycle.create_or_update_draft(
            event_id="evt-payload-update",
            article=self.article,
            article_version="v1",
            format_html=False,
        )
        self.assertTrue(second.ok, msg=second.error)
        put_calls = [
            (m, u, kw) for m, u, kw in self.transport.calls
            if m == "PUT" and "/wp/v2/posts/" in u
        ]
        self.assertEqual(len(put_calls), 1)
        body = put_calls[0][2].get("json") or {}
        self._assert_safe_meta(body.get("meta") or {})

    def test_validator_requires_newsagent_not_rank_math(self) -> None:
        seo = SEOMetadata(
            title="T",
            seo_title="SEO Title Here For Test",
            meta_description="A meta description long enough for validation purposes here.",
            focus_keyphrase="bitcoin etf",
            slug="bitcoin-etf-test",
            canonical_url=None,
            open_graph_title="OG",
            open_graph_description="OG desc",
            twitter_title="TW",
            twitter_description="TW desc",
        )
        good = {
            "newsagent_seo_title": seo.seo_title,
            "newsagent_focus_keyphrase": seo.focus_keyphrase,
            "newsagent_canonical_url": "",
        }
        self.assertEqual(validate_wordpress_seo_meta(good, seo), [])
        with_rank = dict(good)
        with_rank["rank_math_title"] = seo.seo_title
        issues = validate_wordpress_seo_meta(with_rank, seo)
        self.assertTrue(any("forbidden_rank_math" in i for i in issues))
        with_null = dict(good)
        with_null["newsagent_canonical_url"] = None
        issues2 = validate_wordpress_seo_meta(with_null, seo)
        self.assertIn("newsagent_canonical_url_null", issues2)


if __name__ == "__main__":
    unittest.main()
