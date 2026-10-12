"""News-agent images via the local gx10 OpenAI-compatible proxy."""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping

from PIL import Image

from newsagent_v2.image.provider import ProviderResult
from newsagent_v2.image.validate import file_sha256

PROXY_MODEL = "gemini-3.1-flash-image"
DEFAULT_PROXY_URL = "http://127.0.0.1:8092"
_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")


def image_via_proxy(environ: Mapping[str, str] | None) -> bool:
    raw = str((environ or {}).get("NEWSAGENT_IMAGE_VIA_PROXY", "true")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _proxy_settings(environ: Mapping[str, str]) -> tuple[str, str]:
    url = str(environ.get("NEWSAGENT_IMAGE_PROXY_URL") or DEFAULT_PROXY_URL).rstrip("/")
    key = str(environ.get("NEWSAGENT_IMAGE_PROXY_KEY") or "").strip()
    return url, key


def redact_proxy_text(text: str, secret: str) -> str:
    cleaned = str(text or "").replace("\r", " ").replace("\n", " ")
    if secret:
        cleaned = cleaned.replace(secret, "[redacted]")
    cleaned = _BEARER_RE.sub("Bearer [redacted]", cleaned)
    return cleaned[:500]


def _crop_16_9(path: Path) -> None:
    """Published cards are 16:9. Crop a square proxy image before the size check."""
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        if height <= 0:
            return
        if abs((width / height) - (16 / 9)) <= 0.03:
            return
        if width / height > 16 / 9:
            new_width = int(height * 16 / 9)
            left = max(0, (width - new_width) // 2)
            cropped = image.crop((left, 0, left + new_width, height))
        else:
            new_height = int(width * 9 / 16)
            top = max(0, (height - new_height) // 2)
            cropped = image.crop((0, top, width, top + new_height))
        cropped.save(path, format="PNG")


class Gx10ImageProxy:
    """One image call. A non-200 is retried once after 3 seconds."""

    provider_name = "image_proxy"

    def __init__(self, environ: Mapping[str, str]) -> None:
        self.environ = environ
        self.generation_calls = 0
        self.model_name = PROXY_MODEL

    def recorded_request(self, request) -> dict:
        return {
            "provider": "image_proxy",
            "model": self.model_name,
            "prompt_chars": len(getattr(request, "prompt", "") or ""),
            "response_format": "b64_json",
        }

    def _post_once(self, url: str, key: str, payload: bytes) -> tuple[int, str]:
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            return int(exc.code), body

    def generate(self, request, dest_path: str, cold_start: bool = True) -> ProviderResult:
        started = time.perf_counter()
        self.generation_calls = 1
        url, key = _proxy_settings(self.environ)
        endpoint = f"{url}/v1/images/generations"
        prompt = str(getattr(request, "prompt", "") or "")

        def fail(status: int | None, reason: str) -> ProviderResult:
            elapsed = int((time.perf_counter() - started) * 1000)
            return ProviderResult(
                provider_name="image_proxy",
                success=False,
                model_name=self.model_name,
                failure_reason=redact_proxy_text(reason, key),
                http_status=status,
                total_latency_ms=elapsed,
                cold_start=cold_start,
            )

        if not key:
            return fail(None, "NEWSAGENT_IMAGE_PROXY_KEY is empty")
        body = json.dumps(
            {
                "model": PROXY_MODEL,
                "prompt": prompt,
                "n": 1,
                "size": "1K",
                "response_format": "b64_json",
            }
        ).encode("utf-8")
        status, raw = 0, ""
        for attempt in (1, 2):
            try:
                status, raw = self._post_once(endpoint, key, body)
            except Exception as exc:
                status, raw = 0, f"{type(exc).__name__}"
            if status == 200:
                break
            safe = redact_proxy_text(raw, key)
            print(
                f"image_proxy status={status} attempt={attempt} error={safe}",
                flush=True,
            )
            if attempt == 1:
                time.sleep(3)
        if status != 200:
            return fail(status, raw or "proxy request failed")
        try:
            payload = json.loads(raw)
            encoded = payload["data"][0]["b64_json"]
            image_bytes = base64.b64decode(encoded)
        except Exception:
            return fail(status, "proxy 200 response had no b64_json image")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(image_bytes)
        try:
            _crop_16_9(dest)
        except Exception:
            return fail(status, "proxy image could not be decoded")
        elapsed = int((time.perf_counter() - started) * 1000)
        with Image.open(dest) as image:
            width, height = image.size
        return ProviderResult(
            provider_name="image_proxy",
            success=True,
            model_name=self.model_name,
            raw_image_path=str(dest),
            sha256=file_sha256(dest),
            http_status=200,
            width=width,
            height=height,
            total_latency_ms=elapsed,
            cold_start=cold_start,
            response_format="b64_json",
            requested_aspect_ratio="16:9",
            requested_resolution_tier="1K",
        )
