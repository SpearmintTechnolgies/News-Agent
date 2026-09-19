from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from newsagent_v2.image.hero import (
    MIN_HERO_HEIGHT,
    MIN_HERO_WIDTH,
    HeroImageError,
    acquire_hero_image,
    extract_declared_hero,
)
from newsagent_v2.image.validate import file_sha256


def _jpeg_bytes(width: int, height: int, color: tuple[int, int, int] = (30, 40, 50)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


OG_HTML = """
<html><head>
<meta property="og:image" content="/hero/story.jpg">
<meta name="twitter:image" content="https://cdn.example/twitter.jpg">
</head><body><img src="/ads/banner.jpg"><img src="/avatars/writer.png"></body></html>
"""

TWITTER_HTML = """
<html><head>
<meta name="twitter:image" content="https://cdn.example/tw-hero.webp">
</head><body><img src="https://cdn.example/random.jpg"></body></html>
"""

JSONLD_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"NewsArticle","image":"https://cdn.example/ld-hero.jpg"}
</script>
</head><body><img src="https://cdn.example/unrelated.jpg"></body></html>
"""

EMPTY_HTML = """
<html><head><title>Story</title></head>
<body><img src="https://cdn.example/photo.jpg"><img src="/logo.png"></body></html>
"""


class ExtractorTests(unittest.TestCase):
    def test_og_image_wins_and_ignores_arbitrary_img(self) -> None:
        found = extract_declared_hero(OG_HTML, "https://example.com/markets/story")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["discovery_method"], "og:image")
        self.assertEqual(found["url"], "https://example.com/hero/story.jpg")

    def test_twitter_image_fallback(self) -> None:
        found = extract_declared_hero(TWITTER_HTML, "https://example.com/a")
        self.assertEqual(found["discovery_method"], "twitter:image")
        self.assertEqual(found["url"], "https://cdn.example/tw-hero.webp")

    def test_jsonld_image_fallback(self) -> None:
        found = extract_declared_hero(JSONLD_HTML, "https://example.com/a")
        self.assertEqual(found["discovery_method"], "jsonld_article_image")
        self.assertEqual(found["url"], "https://cdn.example/ld-hero.jpg")

    def test_missing_metadata_has_no_img_fallback(self) -> None:
        self.assertIsNone(extract_declared_hero(EMPTY_HTML, "https://example.com/a"))


class AcquireTests(unittest.TestCase):
    def test_acquire_writes_provenance_and_keeps_bytes(self) -> None:
        jpeg = _jpeg_bytes(640, 360)

        def fetch(url: str):
            if url.endswith("/story"):
                return 200, "text/html; charset=utf-8", OG_HTML.encode("utf-8"), url
            if url.endswith("/hero/story.jpg"):
                return 200, "image/jpeg", jpeg, url
            raise AssertionError(f"unexpected url {url}")

        with tempfile.TemporaryDirectory() as tmp:
            stem = Path(tmp) / "event-027-source"
            result = acquire_hero_image(
                "https://example.com/story",
                stem,
                fetch=fetch,
                event_id="event-027",
            )
            dest = Path(result["local_path"])
            self.assertEqual(dest.read_bytes(), jpeg)
            self.assertEqual(file_sha256(dest), result["sha256"])
            self.assertEqual(result["width"], 640)
            self.assertEqual(result["height"], 360)
            self.assertEqual(result["discovery_method"], "og:image")
            self.assertTrue(result["must_not_be_final_image"])
            self.assertFalse(result["arbitrary_img_fallback"])
            self.assertEqual(result["image_generation_requests"], 0)
            self.assertEqual(result["role"], "story_reference")
            prov = Path(result["provenance_path"])
            saved = json.loads(prov.read_text(encoding="utf-8"))
            self.assertEqual(saved["sha256"], result["sha256"])
            self.assertEqual(dest.read_bytes(), jpeg)

    def test_non_image_response_rejected(self) -> None:
        def fetch(url: str):
            if "story" in url and "hero" not in url:
                return 200, "text/html", OG_HTML.encode("utf-8"), url
            return 200, "text/html", b"<html>not an image</html>", url

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HeroImageError) as ctx:
                acquire_hero_image("https://example.com/story", Path(tmp) / "x", fetch=fetch)
            self.assertEqual(ctx.exception.code, "not_image")

    def test_tiny_image_rejected(self) -> None:
        tiny = _jpeg_bytes(64, 64)

        def fetch(url: str):
            if url.endswith("/story"):
                return 200, "text/html", OG_HTML.encode("utf-8"), url
            return 200, "image/jpeg", tiny, url

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HeroImageError) as ctx:
                acquire_hero_image("https://example.com/story", Path(tmp) / "x", fetch=fetch)
            self.assertEqual(ctx.exception.code, "tiny_image")
            self.assertGreaterEqual(MIN_HERO_WIDTH, 320)
            self.assertGreaterEqual(MIN_HERO_HEIGHT, 180)

    def test_missing_metadata_acquire(self) -> None:
        def fetch(url: str):
            return 200, "text/html", EMPTY_HTML.encode("utf-8"), url

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HeroImageError) as ctx:
                acquire_hero_image("https://example.com/story", Path(tmp) / "x", fetch=fetch)
            self.assertEqual(ctx.exception.code, "missing_metadata")
            self.assertFalse(any(Path(tmp).iterdir()))


if __name__ == "__main__":
    unittest.main()


