from __future__ import annotations

import base64
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from newsagent_v2.image.benchmark.fixture import load_canonical_brief
from newsagent_v2.image.benchmark.runner import run_provider_benchmark
from newsagent_v2.image.provider import ProviderRequest, canonical_provider_request
from newsagent_v2.image.validate import file_sha256
from newsagent_v2.image.providers.gemini import (
    DEFAULT_MODEL,
    KEY_ENV,
    REQUESTED_ASPECT_RATIO,
    REQUESTED_RESOLUTION_TIER,
    GeminiConfigError,
    GeminiHttpResponse,
    GeminiImageProvider,
    generate_content_url,
    load_gemini_config,
)

TOKEN = "TEST_GEMINI_KEY"


def _png_bytes(width: int = 1376, height: int = 768) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (22, 30, 48)).save(buf, format="PNG")
    return buf.getvalue()


def _png_b64(**kwargs: int) -> str:
    return base64.b64encode(_png_bytes(**kwargs)).decode("ascii")


def _json_response(status: int, payload: dict, headers=None) -> GeminiHttpResponse:
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    return GeminiHttpResponse(status, body, hdrs)


def _success_payload(width: int = 1376, height: int = 768) -> dict:
    return {
        "responseId": "resp-test-1",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "here is an image"},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": _png_b64(width=width, height=height),
                            }
                        },
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 80,
            "candidatesTokenCount": 1120,
            "totalTokenCount": 1200,
            "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}],
        },
    }


class ScriptedTransport:
    def __init__(self, responses: list[GeminiHttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, *, headers, json_body, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "json_body": json_body, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError("unexpected extra HTTP call")
        return self.responses.pop(0)


def _config():
    return load_gemini_config({KEY_ENV: TOKEN})


class GeminiConfigTests(unittest.TestCase):
    def test_missing_api_key_raises_before_request(self) -> None:
        with self.assertRaises(GeminiConfigError):
            load_gemini_config({})
        with self.assertRaises(GeminiConfigError):
            load_gemini_config(None)
        with self.assertRaises(GeminiConfigError):
            load_gemini_config({KEY_ENV: "   "})

    def test_exact_default_model(self) -> None:
        self.assertEqual(DEFAULT_MODEL, "gemini-3.1-flash-lite-image")
        provider = GeminiImageProvider(_config(), transport=ScriptedTransport([]))
        self.assertEqual(provider.identity().model_name, DEFAULT_MODEL)
        self.assertEqual(generate_content_url(DEFAULT_MODEL).endswith(f"/{DEFAULT_MODEL}:generateContent"), True)


class GeminiProviderTests(unittest.TestCase):
    def test_request_construction(self) -> None:
        transport = ScriptedTransport([_json_response(200, _success_payload())])
        provider = GeminiImageProvider(_config(), transport=transport)
        brief = load_canonical_brief()
        request = canonical_provider_request(brief)
        recorded = provider.recorded_request(request)
        self.assertEqual(recorded["model"], DEFAULT_MODEL)
        self.assertFalse(recorded["api_key_in_url"])
        self.assertNotIn(TOKEN, json.dumps(recorded))
        body = recorded["body"]
        self.assertEqual(body["generationConfig"]["imageConfig"]["aspectRatio"], REQUESTED_ASPECT_RATIO)
        self.assertEqual(body["generationConfig"]["imageConfig"]["imageSize"], REQUESTED_RESOLUTION_TIER)
        self.assertEqual(body["contents"][0]["parts"][0]["text"], request.prompt)
        self.assertNotIn("$449M", request.prompt)
        with tempfile.TemporaryDirectory() as tmp:
            provider.generate(request, dest_path=str(Path(tmp) / "artwork.png"), cold_start=True)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn(DEFAULT_MODEL, transport.calls[0]["url"])
        self.assertNotIn("key=", transport.calls[0]["url"])
        self.assertEqual(transport.calls[0]["headers"]["x-goog-api-key"], TOKEN)

    def test_successful_image_extraction_and_dimensions(self) -> None:
        transport = ScriptedTransport([_json_response(200, _success_payload(1376, 768))])
        provider = GeminiImageProvider(_config(), transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="artwork only"),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertTrue(result.success)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.retry_count, 0)
        self.assertEqual(result.width, 1376)
        self.assertEqual(result.height, 768)
        self.assertEqual(result.requested_aspect_ratio, "16:9")
        self.assertEqual(result.requested_resolution_tier, "1K")
        self.assertIsNone(result.requested_width)
        self.assertEqual(result.provider_request_id, "resp-test-1")
        self.assertIsNone(result.provider_reported_cost)
        self.assertIsNone(result.actual_cost_inr)
        self.assertIsNotNone(result.estimated_list_price_usd)
        self.assertTrue(result.estimated_list_price_is_estimate)
        self.assertIn("Not billed cost", result.estimated_list_price_note or "")
        self.assertIsNone(result.generation_time_ms)
        self.assertIsNone(result.provider_reported_latency)

    def test_no_image_response(self) -> None:
        payload = {
            "candidates": [{"content": {"parts": [{"text": "no picture"}]}, "finishReason": "STOP"}]
        }
        provider = GeminiImageProvider(
            _config(), transport=ScriptedTransport([_json_response(200, payload)])
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x"),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "missing_image")
        self.assertEqual(result.retry_count, 0)

    def test_malformed_response(self) -> None:
        provider = GeminiImageProvider(
            _config(),
            transport=ScriptedTransport(
                [GeminiHttpResponse(200, b"{not-json", {"Content-Type": "application/json"})]
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x"),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "malformed_json")

    def test_http_error_not_retried(self) -> None:
        payload = {"error": {"message": f"permission denied {TOKEN}", "status": "PERMISSION_DENIED"}}
        transport = ScriptedTransport([_json_response(403, payload)])
        provider = GeminiImageProvider(_config(), transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x"),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.http_status, 403)
        self.assertEqual(result.retry_count, 0)
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn(TOKEN, result.failure_reason or "")

    def test_timeout_no_retry(self) -> None:
        import requests as req

        class Raising:
            def __call__(self, *a, **k):
                raise req.Timeout("timed out")

        provider = GeminiImageProvider(_config(), transport=Raising())
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x"),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertFalse(result.success)
        self.assertIn("Timeout", result.failure_reason or "")
        self.assertEqual(result.retry_count, 0)

    def test_secret_never_in_artifacts(self) -> None:
        brief = load_canonical_brief()
        provider = GeminiImageProvider(
            _config(), transport=ScriptedTransport([_json_response(200, _success_payload())])
        )
        with tempfile.TemporaryDirectory() as tmp:
            runs = run_provider_benchmark(provider, brief, persist_root=Path(tmp), compose=False)
            root = Path(runs[0]["run_dir"])
            blob = ""
            for path in root.rglob("*"):
                if path.is_file() and path.suffix.lower() in {".json", ".txt"}:
                    blob += path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn(TOKEN, blob)
            request = json.loads((root / "provider_request.json").read_text(encoding="utf-8"))
            self.assertNotIn(TOKEN, json.dumps(request))
            self.assertFalse(request.get("api_key_in_url"))
            telemetry = json.loads((root / "telemetry.json").read_text(encoding="utf-8"))
            self.assertIsNone(telemetry["actual_cost_inr"])
            self.assertNotEqual(telemetry.get("estimated_list_price_usd"), telemetry["actual_cost_inr"])
            self.assertEqual(runs[0]["result"]["width"], 1376)
            self.assertEqual(runs[0]["result"]["height"], 768)

    def test_reference_image_in_wire_body_and_prompt(self) -> None:
        transport = ScriptedTransport([_json_response(200, _success_payload())])
        provider = GeminiImageProvider(_config(), transport=transport)
        self.assertTrue(provider.reference_images_supported)
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "source.jpg"
            Image.new("RGB", (640, 360), (40, 50, 60)).save(ref, format="JPEG")
            digest = file_sha256(ref)
            request = ProviderRequest(
                prompt="Create premium cinematic editorial artwork.",
                reference_image_path=str(ref),
                reference_image_role="story_reference",
                reference_required=True,
            )
            body = provider.wire_body(request)
            parts = body["contents"][0]["parts"]
            self.assertEqual(len(parts), 2)
            self.assertIn("inlineData", parts[0])
            self.assertEqual(parts[0]["inlineData"]["mimeType"], "image/jpeg")
            self.assertTrue(parts[0]["inlineData"]["data"])
            text = parts[1]["text"]
            self.assertIn("Create premium cinematic editorial artwork.", text)
            for needle in (
                "factual/visual context only",
                "new original editorial image",
                "do not copy the exact composition",
                "do not reproduce logos",
                "do not render readable text",
                "do not render a headline",
                "do not render a watermark",
                "fake ui text",
                "pseudotext",
                "source branding",
            ):
                self.assertIn(needle, text.lower())
            recorded = provider.recorded_request(request)
            self.assertTrue(recorded["reference_images_supported"])
            self.assertTrue(recorded["reference_image_attached"])
            inline = recorded["body"]["contents"][0]["parts"][0]["inlineData"]
            self.assertIn("[omitted", inline["data"])
            self.assertEqual(inline["sha256"], digest)
            dest = Path(tmp) / "out.png"
            result = provider.generate(
                request,
                dest_path=str(dest),
                cold_start=True,
            )
            self.assertTrue(result.success)
            self.assertEqual(len(transport.calls), 1)
            sent = transport.calls[0]["json_body"]["contents"][0]["parts"]
            self.assertIn("inlineData", sent[0])
            self.assertEqual(file_sha256(ref), digest)


if __name__ == "__main__":
    unittest.main()


