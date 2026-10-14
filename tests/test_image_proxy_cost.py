"""Proxy image receipts must carry list-price USD so review cards show IMAGE cost."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from PIL import Image

from newsagent_v2.image.image_proxy import (
    DEFAULT_USD_PER_1K_IMAGE,
    Gx10ImageProxy,
    IMAGE_OUTPUT_TOKENS_1K,
    proxy_image_list_price_usd,
)
from newsagent_v2.v5_generation.depth_cost_helpers import (
    accumulate_vertex_receipts,
    build_review_cost_text,
)


def _png_b64(*, width: int = 1024, height: int = 1024) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(20, 40, 60)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_proxy_list_price_default_and_override():
    assert proxy_image_list_price_usd({}) == DEFAULT_USD_PER_1K_IMAGE
    assert proxy_image_list_price_usd({"NEWSAGENT_IMAGE_PROXY_USD_PER_IMAGE": "0.099"}) == 0.099


def test_proxy_success_attaches_billed_usd(tmp_path, monkeypatch):
    png = _png_b64()
    body = json.dumps({"data": [{"b64_json": png}]})

    def fake_post(self, url, key, payload):
        return 200, body

    monkeypatch.setattr(Gx10ImageProxy, "_post_once", fake_post)
    dest = tmp_path / "out.png"
    result = Gx10ImageProxy(
        {"NEWSAGENT_IMAGE_PROXY_KEY": "test-key"}
    ).generate(type("R", (), {"prompt": "hero"})(), dest_path=str(dest))
    assert result.success is True
    assert result.provider_reported_cost_usd == DEFAULT_USD_PER_1K_IMAGE
    assert result.provider_reported_usage["image_tokens"] == IMAGE_OUTPUT_TOKENS_1K
    assert dest.is_file()


def test_accumulate_includes_image_proxy_receipt(tmp_path):
    event_id = "evt-proxy-cost"
    run_dir = tmp_path / f"{event_id}-image_proxy"
    run_dir.mkdir()
    (run_dir / "provider_result.json").write_text(
        json.dumps(
            {
                "provider_name": "image_proxy",
                "model_name": "gemini-3.1-flash-image",
                "success": True,
                "provider_reported_cost_usd": DEFAULT_USD_PER_1K_IMAGE,
                "provider_reported_usage": {
                    "promptTokenCount": 0,
                    "candidatesTokenCount": IMAGE_OUTPUT_TOKENS_1K,
                    "candidatesTokensDetails": [
                        {"modality": "IMAGE", "tokenCount": IMAGE_OUTPUT_TOKENS_1K}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    image = accumulate_vertex_receipts(event_id, tmp_path, {})
    assert image["requests"] == 1
    assert image["provider"] == "image_proxy"
    assert image["provider_reported_cost_usd"] == DEFAULT_USD_PER_1K_IMAGE
    text = build_review_cost_text(
        text_usage={"attempts": []},
        image_usage=image,
        environ={},
        escape_html=lambda value: value,
    )
    assert f"IMAGE — 1 call — ${DEFAULT_USD_PER_1K_IMAGE:.4f} USD" in text
    assert "TOTAL COST — $" in text
