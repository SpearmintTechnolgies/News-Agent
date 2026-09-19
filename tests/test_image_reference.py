from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from newsagent_v2.image.benchmark.fixture import load_canonical_brief
from newsagent_v2.image.benchmark.runner import run_provider_benchmark
from newsagent_v2.image.provider import ImageProviderError, ProviderRequest, canonical_provider_request
from newsagent_v2.image.providers.cloudflare import CloudflareImageProvider
from newsagent_v2.image.providers.gemini import GeminiImageProvider
from newsagent_v2.image.providers.synthetic import SyntheticImageProvider
from newsagent_v2.image.reference import apply_reference_policy, with_reference_prompt
from newsagent_v2.image.validate import ImageValidationError, file_sha256, write_png_rgb


def _write_jpeg(path: Path, width: int = 320, height: int = 180) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (12, 18, 24)).save(path, format="JPEG")
    return path


class ReferenceImageContractTests(unittest.TestCase):
    def test_no_reference_request_still_works(self) -> None:
        brief = load_canonical_brief()
        request = canonical_provider_request(brief)
        self.assertIsNone(request.reference_image_path)
        self.assertFalse(request.reference_required)
        provider = SyntheticImageProvider()
        info = apply_reference_policy(provider, request)
        self.assertFalse(info["reference_used"])
        self.assertFalse(info["reference_required"])
        self.assertFalse(info["provider_reference_supported"])
        with tempfile.TemporaryDirectory() as tmp:
            runs = run_provider_benchmark(
                provider,
                brief,
                persist_root=Path(tmp),
                compose=False,
                request=request,
            )
            self.assertTrue(runs[0]["result"]["success"])
            telemetry = runs[0]["telemetry"]
            self.assertFalse(telemetry["reference_used"])
            self.assertIsNone(telemetry["reference_sha256"])

    def test_valid_reference_accepted_and_recorded(self) -> None:
        provider = SyntheticImageProvider()
        with tempfile.TemporaryDirectory() as tmp:
            ref = _write_jpeg(Path(tmp) / "story.jpg", 400, 225)
            digest = file_sha256(ref)
            request = ProviderRequest(
                prompt="artwork only",
                width=1280,
                height=720,
                reference_image_path=str(ref),
                reference_image_role="story_reference",
                reference_required=False,
            )
            info = apply_reference_policy(provider, request)
            self.assertEqual(info["reference_sha256"], digest)
            self.assertEqual(info["reference_width"], 400)
            self.assertEqual(info["reference_height"], 225)
            self.assertEqual(info["reference_role"], "story_reference")
            self.assertFalse(info["reference_used"])
            self.assertFalse(info["provider_reference_supported"])
            self.assertEqual(file_sha256(ref), digest)

    def test_corrupt_reference_rejected(self) -> None:
        provider = SyntheticImageProvider()
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "bad.jpg"
            ref.write_bytes(b"not-an-image" * 20)
            request = ProviderRequest(
                prompt="x",
                reference_image_path=str(ref),
                reference_required=False,
            )
            with self.assertRaises(ImageValidationError) as ctx:
                apply_reference_policy(provider, request)
            self.assertEqual(ctx.exception.code, "undecodable")

    def test_missing_required_reference_rejected(self) -> None:
        provider = SyntheticImageProvider()
        request = ProviderRequest(prompt="x", reference_required=True)
        with self.assertRaises(ImageProviderError) as ctx:
            apply_reference_policy(provider, request)
        self.assertEqual(ctx.exception.code, "missing_reference")

    def test_unsupported_provider_required_reference_fails_before_network(self) -> None:
        provider = SyntheticImageProvider()
        with tempfile.TemporaryDirectory() as tmp:
            ref = write_png_rgb(Path(tmp) / "ref.png", 64, 64, (1, 2, 3))
            request = ProviderRequest(
                prompt="x",
                width=1280,
                height=720,
                reference_image_path=str(ref),
                reference_required=True,
            )
            with self.assertRaises(ImageProviderError) as ctx:
                apply_reference_policy(provider, request)
            self.assertEqual(ctx.exception.code, "reference_unsupported")
            brief = load_canonical_brief()
            with self.assertRaises(ImageProviderError):
                run_provider_benchmark(
                    provider,
                    brief,
                    persist_root=Path(tmp) / "runs",
                    compose=False,
                    request=request,
                )

    def test_source_image_remains_unchanged(self) -> None:
        provider = SyntheticImageProvider()
        with tempfile.TemporaryDirectory() as tmp:
            ref = _write_jpeg(Path(tmp) / "story.jpg")
            digest = file_sha256(ref)
            request = ProviderRequest(
                prompt="x",
                reference_image_path=str(ref),
                reference_image_role="style_reference",
            )
            apply_reference_policy(provider, request)
            self.assertEqual(file_sha256(ref), digest)

    def test_capability_metadata(self) -> None:
        self.assertTrue(GeminiImageProvider.reference_images_supported)
        self.assertTrue(CloudflareImageProvider.reference_images_supported)
        self.assertFalse(SyntheticImageProvider.reference_images_supported)
        text = with_reference_prompt("base prompt", role="story_reference")
        self.assertIn("base prompt", text)
        self.assertIn("Do not render a headline.", text)


if __name__ == "__main__":
    unittest.main()


