from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from newsagent_v2.image.cloudflare_reference import (
    EVENT_027_MASTER_SHA256,
    DEFAULT_MASTER,
    derive_cloudflare_reference,
    fit_under_max_edge,
)
from newsagent_v2.image.providers.cloudflare import MAX_REFERENCE_EDGE
from newsagent_v2.image.validate import file_sha256


class CloudflareReferenceDeriveTests(unittest.TestCase):
    def test_fit_strictly_under_512(self) -> None:
        width, height = fit_under_max_edge(1450, 966)
        self.assertLessEqual(width, MAX_REFERENCE_EDGE)
        self.assertLessEqual(height, MAX_REFERENCE_EDGE)
        self.assertLess(width, 512)
        self.assertLess(height, 512)
        self.assertAlmostEqual(width / height, 1450 / 966, places=2)

    def test_derived_reference_under_512_and_master_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "event-027-source.jpg"
            dest = Path(tmp) / "derived" / "event-027-cloudflare-reference.jpg"
            Image.new("RGB", (1450, 966), (40, 50, 60)).save(master, format="JPEG", quality=95)
            digest = file_sha256(master)
            meta = derive_cloudflare_reference(master, dest)
            self.assertEqual(file_sha256(master), digest)
            self.assertTrue(dest.is_file())
            self.assertLess(meta["derived_width"], 512)
            self.assertLess(meta["derived_height"], 512)
            self.assertLessEqual(meta["derived_width"], MAX_REFERENCE_EDGE)
            self.assertLessEqual(meta["derived_height"], MAX_REFERENCE_EDGE)
            self.assertTrue(meta["aspect_preserved"])
            self.assertFalse(meta["ai_edits"])
            self.assertEqual(meta["master_sha256"], digest)
            with Image.open(dest) as image:
                self.assertEqual(image.size, (meta["derived_width"], meta["derived_height"]))

    def test_event_027_master_bytes_stay_identical(self) -> None:
        if not DEFAULT_MASTER.is_file():
            self.skipTest("event-027 master reference is not present")
        digest = file_sha256(DEFAULT_MASTER)
        self.assertEqual(digest, EVENT_027_MASTER_SHA256)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "event-027-cloudflare-reference.jpg"
            derive_cloudflare_reference(
                DEFAULT_MASTER,
                dest,
                expected_master_sha256=EVENT_027_MASTER_SHA256,
            )
            self.assertEqual(file_sha256(DEFAULT_MASTER), EVENT_027_MASTER_SHA256)
            with Image.open(dest) as image:
                width, height = image.size
            self.assertLess(width, 512)
            self.assertLess(height, 512)


if __name__ == "__main__":
    unittest.main()


