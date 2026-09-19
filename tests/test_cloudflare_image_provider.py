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
from newsagent_v2.image.providers.cloudflare import (
    ACCOUNT_ENV,
    DEFAULT_MODEL,
    REFERENCE_FIELD,
    TOKEN_ENV,
    CloudflareConfigError,
    CloudflareHttpResponse,
    CloudflareImageProvider,
    load_cloudflare_config,
)
from newsagent_v2.image.reference import apply_reference_policy
from newsagent_v2.image.validate import file_sha256

TOKEN = "cf_test_token_value_not_real_abcdef123456"
ACCOUNT = "acct_test_account_id_value_xyz"


def _png_bytes(width: int = 1280, height: int = 720) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (18, 26, 38)).save(buf, format="PNG")
    return buf.getvalue()


def _png_b64(**kwargs: int) -> str:
    return base64.b64encode(_png_bytes(**kwargs)).decode("ascii")


def _json_response(status: int, payload: dict, headers=None) -> CloudflareHttpResponse:
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    return CloudflareHttpResponse(status, body, hdrs)


def _config():
    return load_cloudflare_config({ACCOUNT_ENV: ACCOUNT, TOKEN_ENV: TOKEN})


class ScriptedTransport:
    def __init__(self, responses: list[CloudflareHttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, *, headers, data, timeout, files=None):
        self.calls.append(
            {"url": url, "headers": headers, "data": data, "timeout": timeout, "files": files}
        )
        if not self.responses:
            raise AssertionError("unexpected extra HTTP call")
        return self.responses.pop(0)


class SleepRecorder:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class ConfigTests(unittest.TestCase):
    def test_missing_credentials(self) -> None:
        with self.assertRaises(CloudflareConfigError):
            load_cloudflare_config({})
        with self.assertRaises(CloudflareConfigError):
            load_cloudflare_config({ACCOUNT_ENV: ACCOUNT})
        with self.assertRaises(CloudflareConfigError):
            load_cloudflare_config({TOKEN_ENV: TOKEN})
        with self.assertRaises(CloudflareConfigError):
            load_cloudflare_config(None)

    def test_model_configurable(self) -> None:
        other = "@cf/black-forest-labs/flux-2-klein-9b"
        provider = CloudflareImageProvider(_config(), model=other, transport=ScriptedTransport([]))
        self.assertEqual(provider.identity().model_name, other)
        self.assertNotEqual(other, DEFAULT_MODEL)
        self.assertEqual(CloudflareImageProvider(_config(), transport=ScriptedTransport([])).model, DEFAULT_MODEL)


class CloudflareProviderTests(unittest.TestCase):
    def _provider(self, responses, sleep=None):
        transport = ScriptedTransport(responses)
        sleeper = sleep or SleepRecorder()
        provider = CloudflareImageProvider(_config(), transport=transport, sleep=sleeper)
        return provider, transport, sleeper

    def test_successful_image_response(self) -> None:
        image = _png_b64()
        provider, transport, _ = self._provider(
            [
                _json_response(
                    200,
                    {"success": True, "result": {"image": image}},
                    {"cf-ray": "ray-test-1"},
                )
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artwork.png"
            result = provider.generate(
                ProviderRequest(prompt="artwork only", width=1280, height=720),
                dest_path=str(dest),
                cold_start=True,
            )
            self.assertTrue(result.success)
            self.assertEqual(result.http_status, 200)
            self.assertEqual(result.retry_count, 0)
            self.assertEqual(result.width, 1280)
            self.assertEqual(result.height, 720)
            self.assertEqual(result.requested_width, 1280)
            self.assertEqual(result.requested_height, 720)
            self.assertEqual(result.cloudflare_request_id, "ray-test-1")
            self.assertIsNone(result.provider_reported_usage)
            self.assertIsNone(result.provider_reported_neurons)
            self.assertIsNone(result.provider_reported_cost)
            self.assertIsNone(result.actual_cost_inr)
            self.assertTrue(Path(result.raw_image_path).exists())
            self.assertEqual(len(transport.calls), 1)
            self.assertIn(DEFAULT_MODEL, transport.calls[0]["url"])
            self.assertEqual(transport.calls[0]["data"]["width"], "1280")
            self.assertEqual(transport.calls[0]["data"]["height"], "720")
            self.assertNotIn("negative_prompt", transport.calls[0]["data"])
            self.assertIsNone(transport.calls[0]["files"])
            self.assertTrue(provider.reference_images_supported)

    def test_http_400_401_403_not_retried(self) -> None:
        for status, payload in (
            (400, {"success": False, "errors": [{"message": "invalid request"}]}),
            (401, {"success": False, "errors": [{"message": f"auth failed {TOKEN}"}]}),
            (403, {"success": False, "errors": [{"message": "forbidden"}]}),
        ):
            provider, transport, sleeper = self._provider([_json_response(status, payload)])
            with tempfile.TemporaryDirectory() as tmp:
                result = provider.generate(
                    ProviderRequest(prompt="x", width=1280, height=720),
                    dest_path=str(Path(tmp) / "artwork.png"),
                    cold_start=True,
                )
            self.assertFalse(result.success)
            self.assertEqual(result.http_status, status)
            self.assertEqual(result.retry_count, 0)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(sleeper.waits, [])
            self.assertNotIn(TOKEN, result.failure_reason or "")

    def test_429_retries_once(self) -> None:
        sleeper = SleepRecorder()
        provider, transport, _ = self._provider(
            [
                _json_response(429, {"success": False, "errors": [{"message": "rate"}]}, {"Retry-After": "2"}),
                _json_response(200, {"success": True, "result": {"image": _png_b64()}}),
            ],
            sleep=sleeper,
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x", width=1280, height=720),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertTrue(result.success)
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeper.waits, [2.0])

    def test_5xx_retries_once(self) -> None:
        provider, transport, sleeper = self._provider(
            [
                _json_response(503, {"success": False, "errors": [{"message": "busy"}]}),
                _json_response(200, {"success": True, "result": {"image": _png_b64()}}),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x", width=1280, height=720),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertTrue(result.success)
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeper.waits, [1.0])

    def test_retry_exhaustion(self) -> None:
        provider, transport, _ = self._provider(
            [
                _json_response(429, {"success": False, "errors": [{"message": "rate"}]}),
                _json_response(429, {"success": False, "errors": [{"message": "rate"}]}),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x", width=1280, height=720),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(result.http_status, 429)

    def test_malformed_json(self) -> None:
        provider, _, _ = self._provider(
            [CloudflareHttpResponse(200, b"{not-json", {"Content-Type": "application/json"})]
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x", width=1280, height=720),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "malformed_json")

    def test_missing_image(self) -> None:
        provider, _, _ = self._provider([_json_response(200, {"success": True, "result": {}})])
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x", width=1280, height=720),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "missing_image")

    def test_invalid_image_bytes(self) -> None:
        junk = base64.b64encode(b"not-an-image" * 20).decode("ascii")
        provider, _, _ = self._provider([_json_response(200, {"result": {"image": junk}})])
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "artwork.png"
            result = provider.generate(
                ProviderRequest(prompt="x", width=1280, height=720),
                dest_path=str(dest),
                cold_start=True,
            )
            self.assertFalse(result.success)
            self.assertEqual(result.failure_reason, "invalid_image_bytes")
            self.assertFalse(dest.exists())

    def test_token_never_in_artifacts(self) -> None:
        brief = load_canonical_brief()
        provider, _, _ = self._provider(
            [_json_response(200, {"success": True, "result": {"image": _png_b64()}})]
        )
        with tempfile.TemporaryDirectory() as tmp:
            runs = run_provider_benchmark(provider, brief, persist_root=Path(tmp), compose=False)
            root = Path(runs[0]["run_dir"])
            blob = ""
            for path in root.rglob("*"):
                if path.is_file():
                    blob += path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn(TOKEN, blob)
            self.assertNotIn(ACCOUNT, blob)
            request = json.loads((root / "provider_request.json").read_text(encoding="utf-8"))
            self.assertNotIn("Authorization", json.dumps(request))
            self.assertIn("{account_id}", request["endpoint"])
            self.assertEqual(request["form"]["prompt"], canonical_provider_request(brief).prompt)
            self.assertIsNone(runs[0]["scorecard"]["subjective"]["overall_quality"])
            self.assertTrue(request["reference_images_supported"])
            self.assertFalse(request["reference_image_attached"])

    def test_required_reference_sends_input_image_0(self) -> None:
        provider, transport, _ = self._provider(
            [_json_response(200, {"success": True, "result": {"image": _png_b64()}})]
        )
        self.assertTrue(provider.reference_images_supported)
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "story.jpg"
            Image.new("RGB", (400, 225), (30, 40, 50)).save(ref, format="JPEG")
            digest = file_sha256(ref)
            dest = Path(tmp) / "artwork.png"
            request = ProviderRequest(
                prompt="artwork only",
                width=1280,
                height=720,
                reference_image_path=str(ref),
                reference_image_role="story_reference",
                reference_required=True,
            )
            info = apply_reference_policy(provider, request)
            self.assertTrue(info["reference_used"])
            self.assertTrue(info["provider_reference_supported"])
            result = provider.generate(request, dest_path=str(dest), cold_start=True)
            self.assertTrue(result.success)
            self.assertEqual(len(transport.calls), 1)
            files = transport.calls[0]["files"]
            self.assertIsNotNone(files)
            self.assertIn(REFERENCE_FIELD, files)
            filename, payload, mime = files[REFERENCE_FIELD]
            self.assertEqual(filename, "story.jpg")
            self.assertEqual(payload, ref.read_bytes())
            self.assertEqual(mime, "image/jpeg")
            self.assertNotIn(REFERENCE_FIELD, transport.calls[0]["data"])
            self.assertEqual(transport.calls[0]["data"]["width"], "1280")
            self.assertEqual(transport.calls[0]["data"]["height"], "720")
            self.assertIn("Do not copy the exact composition", transport.calls[0]["data"]["prompt"])
            recorded = provider.recorded_request(request)
            self.assertTrue(recorded["reference_images_supported"])
            self.assertTrue(recorded["reference_image_attached"])
            self.assertEqual(recorded["reference_form_field"], REFERENCE_FIELD)
            self.assertEqual(recorded["reference_image"]["sha256"], digest)
            self.assertFalse(recorded["reference_image"]["bytes_included_in_artifact"])
            self.assertEqual(file_sha256(ref), digest)

    def test_oversized_reference_fails_before_network(self) -> None:
        provider, transport, _ = self._provider([])
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "big.jpg"
            Image.new("RGB", (600, 400), (10, 10, 10)).save(ref, format="JPEG")
            result = provider.generate(
                ProviderRequest(
                    prompt="x",
                    width=1280,
                    height=720,
                    reference_image_path=str(ref),
                    reference_required=True,
                ),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
            self.assertFalse(result.success)
            self.assertEqual(result.failure_reason, "reference_too_large")
            self.assertEqual(transport.calls, [])

    def test_no_retries_when_configured(self) -> None:
        transport = ScriptedTransport([_json_response(429, {"success": False, "errors": [{"message": "rate"}]})])
        provider = CloudflareImageProvider(_config(), transport=transport, sleep=SleepRecorder(), max_retries=0)
        with tempfile.TemporaryDirectory() as tmp:
            result = provider.generate(
                ProviderRequest(prompt="x", width=1280, height=720),
                dest_path=str(Path(tmp) / "artwork.png"),
                cold_start=True,
            )
        self.assertFalse(result.success)
        self.assertEqual(result.retry_count, 0)
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()


