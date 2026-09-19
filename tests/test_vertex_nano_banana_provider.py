from __future__ import annotations

import base64
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from newsagent_v2.image.brief import build_visual_brief
from newsagent_v2.image.compositor import CompositionSpec, compose_card
from newsagent_v2.image.contract import CARD_HEIGHT, CARD_WIDTH
from newsagent_v2.image.provider import canonical_provider_request
from newsagent_v2.image.providers.vertex_nano_banana import (
    HARD_MAX_GENERATION_CALLS,
    VertexConfigError,
    VertexHttpResponse,
    VertexNanoBananaConfig,
    VertexNanoBananaImageProvider,
    classify_vertex_http_error,
    load_vertex_nano_banana_config,
    parse_vertex_response_payload,
    resolve_vertex_config_status,
    vertex_api_host,
    vertex_generate_content_url,
)
from newsagent_v2.image.story_image_brief import build_story_image_brief
from newsagent_v2.image.validate import validate_raw_artwork

REPO = Path(__file__).resolve().parents[1]
LOGO = REPO / "brand" / "coinnetwork_logo.png"


def _png_bytes(width: int = 1280, height: int = 720) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (18, 28, 44)).save(buf, format="PNG")
    return buf.getvalue()


def _png_b64(**kwargs: int) -> str:
    return base64.b64encode(_png_bytes(**kwargs)).decode("ascii")


def _success_payload(*, snake_case: bool = False) -> dict:
    inline_key = "inline_data" if snake_case else "inlineData"
    mime_key = "mime_type" if snake_case else "mimeType"
    return {
        "responseId": "vertex-test-1",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "here is an image"},
                        {
                            inline_key: {
                                mime_key: "image/png",
                                "data": _png_b64(),
                            }
                        },
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 40,
            "candidatesTokenCount": 1120,
            "totalTokenCount": 1160,
        },
    }


def _provider() -> tuple[VertexNanoBananaImageProvider, ScriptedTransport]:
    transport = ScriptedTransport([])
    config = VertexNanoBananaConfig(
        project="demo-project",
        location="global",
        model="gemini-3.1-flash-image",
        credentials_path_set=False,
        auth_mode="adc",
    )
    provider = VertexNanoBananaImageProvider(
        config,
        transport=transport,
        access_token_provider=lambda: "test-token-not-real",
    )
    return provider, transport


class ScriptedTransport:
    def __init__(self, responses: list[VertexHttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, *, headers, json_body, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": {
                    k: ("[REDACTED]" if k.lower() == "authorization" else v) for k, v in headers.items()
                },
                "json_body": json_body,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("no scripted responses left")
        return self.responses.pop(0)


def _request():
    brief = build_visual_brief(
        {
            "event_id": "event-013",
            "editorial_subject": "Bitcoin ETF outflows and Senate setback",
            "visual_concept": "institutional markets and legislation",
            "provider_visual_prompt": "artwork only editorial news image",
        }
    )
    return canonical_provider_request(brief)


class VertexConfigTests(unittest.TestCase):
    def test_missing_fields_reported_without_ready(self) -> None:
        status = resolve_vertex_config_status({})
        self.assertFalse(status["ready"])
        self.assertIn("NEWSAGENT_V2_VERTEX_PROJECT", status["missing_fields"])

    def test_load_raises_when_incomplete(self) -> None:
        with self.assertRaises(VertexConfigError):
            load_vertex_nano_banana_config({})


class VertexEndpointTests(unittest.TestCase):
    def test_global_host_is_unprefixed(self) -> None:
        self.assertEqual(vertex_api_host("global"), "aiplatform.googleapis.com")
        url = vertex_generate_content_url(
            project="p",
            location="global",
            model="gemini-3.1-flash-image",
        )
        self.assertEqual(
            url,
            "https://aiplatform.googleapis.com/v1/projects/p/locations/global/"
            "publishers/google/models/gemini-3.1-flash-image:generateContent",
        )
        self.assertNotIn("global-aiplatform", url)

    def test_regional_host_is_prefixed(self) -> None:
        url = vertex_generate_content_url(project="p", location="us-central1", model="m")
        self.assertEqual(
            url,
            "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/"
            "publishers/google/models/m:generateContent",
        )


class VertexParseTaxonomyTests(unittest.TestCase):
    def test_html_404_is_non_json_http_error_not_malformed_json(self) -> None:
        """Reproduces the real failed-run class: HTTP 404 + HTML body."""
        html = b"<!DOCTYPE html><html><body>Error 404 (Not Found)!!1</body></html>"
        response = VertexHttpResponse(
            404,
            html,
            headers={"Content-Type": "text/html; charset=UTF-8"},
            text=html.decode("utf-8"),
        )
        payload, code, diagnostic = parse_vertex_response_payload(response)
        self.assertIsNone(payload)
        self.assertEqual(code, "vertex_non_json_response")
        self.assertEqual(diagnostic["provider_response_class"], "NON_JSON")
        self.assertEqual(diagnostic["http_status"], 404)

        provider, transport = _provider()
        transport.responses = [response]
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(_request(), dest_path=str(Path(tmp) / "a.png"), cold_start=True)
        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "vertex_http_error")
        self.assertEqual(result.http_status, 404)
        self.assertNotEqual(result.failure_reason, "malformed_json")
        self.assertNotEqual(result.failure_reason, "vertex_malformed_json")

    def test_truncated_json_is_malformed_json(self) -> None:
        response = VertexHttpResponse(200, b'{"candidates":[', headers={"Content-Type": "application/json"})
        _payload, code, diagnostic = parse_vertex_response_payload(response)
        self.assertEqual(code, "vertex_malformed_json")
        self.assertEqual(diagnostic["provider_response_class"], "NON_JSON")

    def test_empty_response(self) -> None:
        response = VertexHttpResponse(200, b"", headers={"Content-Type": "application/json"}, text="")
        _payload, code, diagnostic = parse_vertex_response_payload(response)
        self.assertEqual(code, "vertex_non_json_response")
        self.assertEqual(diagnostic["provider_response_class"], "EMPTY")

    def test_json_permission_error(self) -> None:
        body = {
            "error": {
                "code": 403,
                "message": "Permission denied on resource",
                "status": "PERMISSION_DENIED",
            }
        }
        response = VertexHttpResponse(
            403,
            json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        provider, transport = _provider()
        transport.responses = [response]
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(_request(), dest_path=str(Path(tmp) / "a.png"), cold_start=True)
        self.assertEqual(result.failure_reason, "vertex_permission_denied")

    def test_json_model_not_found(self) -> None:
        body = {"error": {"code": 404, "message": "Publisher Model not found", "status": "NOT_FOUND"}}
        self.assertEqual(classify_vertex_http_error(404, body), "vertex_model_not_found")

    def test_safety_block(self) -> None:
        body = {
            "candidates": [{"finishReason": "SAFETY", "content": {"parts": [{"text": "no"}]}}],
        }
        provider, transport = _provider()
        transport.responses = [
            VertexHttpResponse(200, json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(_request(), dest_path=str(Path(tmp) / "a.png"), cold_start=True)
        self.assertEqual(result.failure_reason, "vertex_safety_block")

    def test_text_only_response(self) -> None:
        body = {
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": "I cannot draw that"}]}}
            ]
        }
        provider, transport = _provider()
        transport.responses = [
            VertexHttpResponse(200, json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(_request(), dest_path=str(Path(tmp) / "a.png"), cold_start=True)
        self.assertEqual(result.failure_reason, "vertex_text_only_response")

    def test_missing_image_empty_candidates(self) -> None:
        body = {"candidates": []}
        provider, transport = _provider()
        transport.responses = [
            VertexHttpResponse(200, json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(_request(), dest_path=str(Path(tmp) / "a.png"), cold_start=True)
        self.assertEqual(result.failure_reason, "vertex_missing_image")


class VertexSuccessFixtureTests(unittest.TestCase):
    def test_generate_success_camel_case_inline(self) -> None:
        provider, transport = _provider()
        transport.responses = [
            VertexHttpResponse(
                200,
                json.dumps(_success_payload()).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artwork.png"
            result = provider.generate(_request(), dest_path=str(dest), cold_start=True)
            self.assertTrue(result.success)
            self.assertEqual(provider.generation_calls, 1)
            self.assertTrue(Path(result.raw_image_path).is_file())
            self.assertIn("aiplatform.googleapis.com/v1/projects/demo-project/locations/global/", transport.calls[0]["url"])
            self.assertNotIn("global-aiplatform", transport.calls[0]["url"])

            validation = validate_raw_artwork(Path(result.raw_image_path))
            self.assertTrue(validation["passed"])
            self.assertEqual(validation["width"], 1280)
            self.assertEqual(validation["height"], 720)

            if LOGO.is_file():
                branded = Path(tmp) / "branded.png"
                composition = compose_card(
                    Path(result.raw_image_path),
                    branded,
                    CompositionSpec(
                        headline="",
                        logo_path=LOGO,
                        logo_only=True,
                        width=CARD_WIDTH,
                        height=CARD_HEIGHT,
                        output_name="branded.png",
                    ),
                )
                self.assertTrue((composition.get("compositor_validation") or {}).get("passed"))

            second = provider.generate(_request(), dest_path=str(Path(tmp) / "b.png"), cold_start=False)
            self.assertFalse(second.success)
            self.assertEqual(second.failure_reason, "hard_max_generation_calls_exceeded")
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(HARD_MAX_GENERATION_CALLS, 1)

    def test_generate_success_snake_case_inline(self) -> None:
        provider, transport = _provider()
        transport.responses = [
            VertexHttpResponse(
                200,
                json.dumps(_success_payload(snake_case=True)).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(_request(), dest_path=str(Path(tmp) / "artwork.png"), cold_start=True)
        self.assertTrue(result.success)


class StoryBriefTests(unittest.TestCase):
    def test_brief_fact_bounded_and_forbids_logo_text(self) -> None:
        article = {
            "event_id": "event-013",
            "headline": "Bitcoin ETFs Shed $450M as Senate Blocks Crypto Legislation",
            "dek": "Spot Bitcoin funds record outflows after Clarity Act fails.",
            "category": "markets",
            "entities": [],
        }
        bundle = build_story_image_brief(
            article=article, evidence_packet={"story_topic": article["headline"]}
        )
        brief = bundle["story_image_brief"]
        self.assertIn("Bitcoin ETFs", brief["primary_subject"])
        self.assertIn("CoinNetwork logo", brief["forbidden_elements"])
        self.assertIn("government seals", brief["forbidden_elements"])


if __name__ == "__main__":
    unittest.main()


