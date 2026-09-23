"""Mocked Rank Math updateMeta + focus keyphrase + media alt tests. No live WP."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from newsagent_v2.wordpress.config import WordPressConfig
from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle
from newsagent_v2.wordpress.draft_store import WordPressDraftStore
from newsagent_v2.wordpress.rankmath import (
    apply_rankmath_seo,
    build_rankmath_update_payload,
    is_quality_focus_keyphrase,
    select_focus_keyphrase,
)
from newsagent_v2.wordpress.seo_metadata import (
    SEOMetadata,
    build_seo_for_article,
)


class RankMathAwareTransport:
    """Capture posts + Rank Math + media calls; never touches the network."""

    def __init__(self, *, fail_rankmath: bool = False, fail_media_alt: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._post_id = 9000
        self.fail_rankmath = fail_rankmath
        self.fail_media_alt = fail_media_alt
        self.media_alt: dict[int, str] = {}

    def __call__(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, url, dict(kwargs)))
        if "rankmath/v1/updateMeta" in url:
            if self.fail_rankmath:
                return {
                    "ok": False,
                    "error": "HTTP 500: Rank Math boom",
                    "status_code": 500,
                    "error_code": "http_error",
                }
            body = kwargs.get("json") or {}
            return {"ok": True, "payload": {"success": True, "objectID": body.get("objectID")}}
        if method == "POST" and "/wp/v2/posts" in url and "/media" not in url:
            self._post_id += 1
            body = kwargs.get("json") or {}
            return {
                "ok": True,
                "payload": {
                    "id": self._post_id,
                    "link": f"https://example.com/?p={self._post_id}",
                    "modified": "2026-09-23T12:00:00",
                    "slug": body.get("slug") or f"post-{self._post_id}",
                    "status": "draft",
                    "featured_media": body.get("featured_media") or 0,
                },
            }
        if method == "PUT" and "/wp/v2/posts/" in url:
            body = kwargs.get("json") or {}
            return {
                "ok": True,
                "payload": {
                    "id": 9001,
                    "link": "https://example.com/?p=9001",
                    "modified": "2026-09-23T12:01:00",
                    "slug": body.get("slug") or "updated",
                    "status": body.get("status") or "draft",
                    "featured_media": body.get("featured_media") or 0,
                },
            }
        if method == "POST" and "/wp/v2/media" in url and url.rstrip("/").split("/")[-1].isdigit():
            media_id = int(url.rstrip("/").split("/")[-1])
            if self.fail_media_alt:
                return {"ok": False, "error": "HTTP 403: media alt denied", "status_code": 403}
            alt = (kwargs.get("json") or {}).get("alt_text") or ""
            self.media_alt[media_id] = alt
            return {"ok": True, "payload": {"id": media_id, "alt_text": alt}}
        if method == "POST" and "/wp/v2/media" in url:
            return {"ok": True, "payload": {"id": 8001, "source_url": "https://example.com/m.png"}}
        return {"ok": False, "error": "offline-mock"}


class TestFocusKeyphraseSelection(unittest.TestCase):
    def test_rejects_garbage_single_tokens(self) -> None:
        self.assertFalse(is_quality_focus_keyphrase("spot"))
        self.assertFalse(is_quality_focus_keyphrase("log"))
        self.assertFalse(is_quality_focus_keyphrase("a"))
        self.assertFalse(is_quality_focus_keyphrase(""))

    def test_accepts_multiword_and_strong_singles(self) -> None:
        self.assertTrue(is_quality_focus_keyphrase("spot bitcoin etfs"))
        self.assertTrue(is_quality_focus_keyphrase("bitcoin etf"))
        self.assertTrue(is_quality_focus_keyphrase("ethereum"))

    def test_empty_keywords_derives_meaningful_phrase(self) -> None:
        phrase = select_focus_keyphrase(
            keywords=[],
            headline="US Spot Bitcoin ETFs Log Nearly $1 Billion Daily Inflow, Largest Since October",
            topic="markets",
            entities=["BlackRock", "IBIT"],
            dek="US spot bitcoin ETFs saw $998.9 million in net inflows Monday.",
        )
        self.assertTrue(is_quality_focus_keyphrase(phrase), phrase)
        self.assertNotEqual(phrase.lower(), "spot")
        self.assertGreaterEqual(len(phrase.split()), 2)
        self.assertIn("bitcoin", phrase.lower())

    def test_prefers_quality_article_keywords(self) -> None:
        phrase = select_focus_keyphrase(
            keywords=["spot", "bitcoin etf inflows"],
            headline="Something else entirely here today",
        )
        self.assertEqual(phrase.lower(), "bitcoin etf inflows")

    def test_repair_does_not_collapse_to_spot(self) -> None:
        article = {
            "headline": "US Spot Bitcoin ETFs Log Nearly $1 Billion Daily Inflow, Largest Since October",
            "slug": "us-spot-bitcoin-etfs-log-nearly-1-billion-daily-in",
            "article_body": "US spot bitcoin exchange-traded funds attracted inflows. " * 40,
            "dek": "Funds saw nearly $1 billion in net inflows on Monday.",
            "meta_description": "US spot bitcoin ETFs saw nearly $1 billion in net inflows on Monday.",
            "seo_title": "US Spot Bitcoin ETFs Nearly $1 Billion Daily Inflow Largest",
            "keywords": [],
        }
        seo, _validation = build_seo_for_article(article, "https://example.com", repair=True)
        self.assertNotEqual(seo.focus_keyphrase.lower(), "spot")
        self.assertTrue(is_quality_focus_keyphrase(seo.focus_keyphrase), seo.focus_keyphrase)


class TestRankMathPayload(unittest.TestCase):
    def test_payload_shape_and_social_mirrors(self) -> None:
        seo = SEOMetadata(
            title="Headline Title Here For Test Article",
            seo_title="SEO Title For Bitcoin ETF Inflows",
            meta_description="Meta description about bitcoin ETF inflows that is long enough.",
            focus_keyphrase="bitcoin etf inflows",
            slug="bitcoin-etf-inflows",
            canonical_url=None,
            open_graph_title="OG Title Bitcoin ETF",
            open_graph_description="OG desc bitcoin ETF inflows.",
            twitter_title="TW Title Bitcoin ETF",
            twitter_description="TW desc bitcoin ETF inflows.",
        )
        payload = build_rankmath_update_payload(post_id=14334, seo=seo, permalink_slug="bitcoin-etf-inflows")
        self.assertEqual(payload["objectID"], 14334)
        self.assertEqual(payload["objectType"], "post")
        meta = payload["meta"]
        self.assertEqual(meta["rank_math_title"], seo.seo_title)
        self.assertEqual(meta["rank_math_description"], seo.meta_description)
        self.assertEqual(meta["rank_math_facebook_title"], seo.open_graph_title)
        self.assertEqual(meta["rank_math_twitter_title"], seo.twitter_title)
        self.assertEqual(meta["rank_math_facebook_description"], seo.open_graph_description)
        self.assertEqual(meta["rank_math_twitter_description"], seo.twitter_description)
        self.assertEqual(meta["rank_math_focus_keyword"], "bitcoin etf inflows")
        self.assertEqual(meta["permalink"], "bitcoin-etf-inflows")
        # No full URL invented
        self.assertFalse(str(meta["permalink"]).startswith("http"))

    def test_permalink_uses_slug_not_invented_url(self) -> None:
        seo = SEOMetadata(
            title="T",
            seo_title="SEO Title Enough Length Here",
            meta_description="A meta description long enough for validation purposes here.",
            focus_keyphrase="bitcoin etf",
            slug="my-slug",
            canonical_url=None,
            open_graph_title=None,
            open_graph_description=None,
            twitter_title=None,
            twitter_description=None,
        )
        payload = build_rankmath_update_payload(post_id=1, seo=seo)
        self.assertEqual(payload["meta"]["permalink"], "my-slug")
        # Social falls back to SEO title/description
        self.assertEqual(payload["meta"]["rank_math_facebook_title"], seo.seo_title)
        self.assertEqual(payload["meta"]["rank_math_twitter_description"], seo.meta_description)


class TestRankMathLifecycleIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.store = WordPressDraftStore(root=Path(self.temp_dir))
        self.config = WordPressConfig(
            base_url="https://example.com",
            username="testuser",
            app_password="testpass12345",
        )
        self.article = {
            "headline": "US Spot Bitcoin ETFs Log Nearly $1 Billion Daily Inflow",
            "slug": "us-spot-bitcoin-etfs-nearly-1-billion",
            "article_body": " ".join(["Word"] * 450) + " US spot bitcoin ETFs inflows.",
            "dek": "Funds saw nearly $1 billion in net inflows on Monday.",
            "meta_description": "US spot bitcoin ETFs saw nearly $1 billion in net inflows on Monday.",
            "seo_title": "US Spot Bitcoin ETFs Nearly $1 Billion Daily Inflow",
            "keywords": [],
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_calls_update_meta_without_rank_math_on_posts(self) -> None:
        transport = RankMathAwareTransport()
        lifecycle = WordPressDraftLifecycle(
            config=self.config, transport=transport, store=self.store
        )
        result = lifecycle.create_or_update_draft(
            event_id="evt-rm-create",
            article=self.article,
            article_version="v1",
            format_html=False,
        )
        self.assertTrue(result.ok, msg=result.error)
        self.assertTrue(result.rank_math_applied)
        self.assertEqual(result.status, "draft")

        post_calls = [c for c in transport.calls if c[0] == "POST" and "/wp/v2/posts" in c[1] and "rankmath" not in c[1]]
        self.assertEqual(len(post_calls), 1)
        posts_meta = (post_calls[0][2].get("json") or {}).get("meta") or {}
        self.assertFalse(any(str(k).startswith("rank_math_") for k in posts_meta))

        rm_calls = [c for c in transport.calls if "rankmath/v1/updateMeta" in c[1]]
        self.assertEqual(len(rm_calls), 1)
        self.assertEqual(rm_calls[0][0], "POST")
        body = rm_calls[0][2].get("json") or {}
        self.assertEqual(body["objectType"], "post")
        self.assertIn("rank_math_title", body["meta"])
        self.assertIn("rank_math_focus_keyword", body["meta"])
        self.assertIn("permalink", body["meta"])
        self.assertEqual(body["meta"]["rank_math_title"], result.seo.seo_title)
        self.assertEqual(body["meta"]["rank_math_description"], result.seo.meta_description)
        self.assertNotEqual(body["meta"]["rank_math_focus_keyword"].lower(), "spot")

    def test_media_alt_text_set_from_focus_keyphrase(self) -> None:
        transport = RankMathAwareTransport()
        lifecycle = WordPressDraftLifecycle(
            config=self.config, transport=transport, store=self.store
        )
        # Seed store with existing media so update path uses featured_media_id
        # Create with image upload mock via writing a tiny file
        img = Path(self.temp_dir) / "hero.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        result = lifecycle.create_or_update_draft(
            event_id="evt-rm-alt",
            article=self.article,
            article_version="v1",
            image_path=str(img),
            format_html=False,
        )
        self.assertTrue(result.ok, msg=result.error)
        alt_calls = [
            c for c in transport.calls
            if c[0] == "POST" and "/wp/v2/media/" in c[1] and (c[2].get("json") or {}).get("alt_text")
        ]
        self.assertEqual(len(alt_calls), 1)
        alt = alt_calls[0][2]["json"]["alt_text"]
        self.assertEqual(alt, result.seo.focus_keyphrase)
        self.assertTrue(result.media_alt_applied)

    def test_rankmath_failure_surfaces_without_publishing(self) -> None:
        transport = RankMathAwareTransport(fail_rankmath=True)
        lifecycle = WordPressDraftLifecycle(
            config=self.config, transport=transport, store=self.store
        )
        result = lifecycle.create_or_update_draft(
            event_id="evt-rm-fail",
            article=self.article,
            article_version="v1",
            format_html=False,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "rank_math_update_failed")
        self.assertIn("Rank Math", result.error or "")
        self.assertEqual(result.status, "draft")
        self.assertIsNotNone(result.wp_post_id)
        self.assertFalse(result.rank_math_applied)
        # Draft record persisted; status never flipped to publish
        record = self.store.load("evt-rm-fail")
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "draft")
        # No publish PUT
        publish_puts = [
            c for c in transport.calls
            if c[0] == "PUT" and (c[2].get("json") or {}).get("status") == "publish"
        ]
        self.assertEqual(publish_puts, [])

    def test_apply_rankmath_seo_helper_endpoint(self) -> None:
        transport = RankMathAwareTransport()
        seo = SEOMetadata(
            title="Headline",
            seo_title="SEO Title Bitcoin ETF Inflows Today",
            meta_description="Meta description about bitcoin ETF inflows long enough here.",
            focus_keyphrase="bitcoin etf inflows",
            slug="bitcoin-etf-inflows",
            canonical_url=None,
            open_graph_title=None,
            open_graph_description=None,
            twitter_title=None,
            twitter_description=None,
        )
        out = apply_rankmath_seo(
            config=self.config,
            transport=transport,
            post_id=42,
            seo=seo,
            featured_media_id=7,
        )
        self.assertTrue(out["ok"])
        self.assertTrue(out["rank_math_applied"])
        urls = [c[1] for c in transport.calls]
        self.assertTrue(any("rankmath/v1/updateMeta" in u for u in urls))
        self.assertTrue(any(u.endswith("/wp/v2/media/7") for u in urls))


if __name__ == "__main__":
    unittest.main()
