"""Nova Canvas fallback for the Vertex image path (offline, mocked HTTP)."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import newsagent_v2.image.vertex_make_image as vmi
from newsagent_v2.image.brief import build_visual_brief
from newsagent_v2.image.provider import canonical_provider_request
from newsagent_v2.image.providers.nova_proxy import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_FALLBACK_ENABLED,
    ENV_MODEL,
    NOVA_BEDROCK_MODEL_ID,
    PROMPT_CHAR_LIMIT,
    PROMPT_TIER_GENERIC,
    PROMPT_TIER_STORY,
    classify_nova_http_error,
    detect_story_themes,
    nova_filter_stage,
    sanitize_nova_text,
    NovaConfigError,
    NovaHttpResponse,
    NovaProxyImageProvider,
    build_nova_prompt,
    fit_nova_prompt,
    load_nova_proxy_config,
    nova_fallback_enabled,
    nova_price_usd,
)
from newsagent_v2.image.providers.vertex_nano_banana import (
    VertexHttpResponse,
    VertexNanoBananaConfig,
    VertexNanoBananaImageProvider,
)
from newsagent_v2.image.vertex_budget import VertexBudgetLedger

REPO = Path(__file__).resolve().parents[1]
LOGO = REPO / "brand" / "coinnetwork_logo.png"
FAKE_KEY = "test-proxy-key-not-real-0123456789"


def _png_bytes(width: int = 1280, height: int = 720) -> bytes:
    image = Image.new("RGB", (width, height), (18, 28, 44))
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 40):
        draw.line([(x, 0), (width - x, height)], fill=(40 + x % 200, 90, 160), width=3)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _json_response(cls, status: int, payload: dict):
    raw = json.dumps(payload).encode("utf-8")
    return cls(status, raw, headers={"Content-Type": "application/json"})


class Scripted:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, *, headers, json_body, timeout):
        self.calls.append({"url": url, "auth_set": bool(headers.get("Authorization")), "json_body": json_body})
        if not self.responses:
            raise AssertionError("unexpected extra HTTP call")
        return self.responses.pop(0)


def _nova_ok():
    return _json_response(NovaHttpResponse, 200, {
        "created": 1,
        "data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}],
    })


INPUT_FILTER_MSG = (
    'Nova image HTTP 400: {"message":"This request has been blocked by our content filters. '
    'Please adjust your text prompt to submit a new request."}'
)


def _nova_blocked():
    return _json_response(NovaHttpResponse, 400, {"error": {"message": INPUT_FILTER_MSG}})


def _vertex_ok():
    return _json_response(VertexHttpResponse, 200, {
        "candidates": [{
            "content": {"parts": [{"inlineData": {"mimeType": "image/png",
                                                  "data": base64.b64encode(_png_bytes()).decode("ascii")}}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 1120, "totalTokenCount": 1160},
    })


def _vertex_429():
    return _json_response(VertexHttpResponse, 429, {"error": {
        "code": 429, "message": "Resource exhausted. Please try again later.", "status": "RESOURCE_EXHAUSTED"}})


def _vertex_safety():
    return _json_response(VertexHttpResponse, 200, {
        "candidates": [{"content": {"parts": []}, "finishReason": "IMAGE_SAFETY"}]})


def _vertex_400_prompt():
    # Contains "generate" (substring "rate"), which the Vertex classifier maps to rate_limited.
    return _json_response(VertexHttpResponse, 400, {"error": {
        "code": 400, "message": "Unable to generate image for this prompt.", "status": "INVALID_ARGUMENT"}})


def _env(**extra: str) -> dict[str, str]:
    env = {
        ENV_BASE_URL: "https://proxy.test/v1",
        ENV_API_KEY: FAKE_KEY,
        "NEWSAGENT_V2_VERTEX_ENABLED": "true",
        "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY": "25",
    }
    env.update(extra)
    return env


ARTICLE = {
    "headline": "Bitcoin ETF Inflows Climb as Institutions Return After Rate Cut",
    "dek": "Spot bitcoin funds took in fresh money for a fifth straight session.",
    "body": "Spot bitcoin exchange-traded funds recorded inflows on Thursday.",
    "category": "Bitcoin News",
}


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vmi, "MAKE_RUNS_ROOT", tmp_path / "make_runs")
    monkeypatch.setattr(vmi, "resolve_article_logo", lambda environ=None: LOGO)

    def build(env: dict[str, str], vertex_responses, nova_responses):
        vertex_tx = Scripted(vertex_responses)
        nova_tx = Scripted(nova_responses)
        vconfig = VertexNanoBananaConfig(
            project="demo-project", location="global", model="gemini-3.1-flash-image",
            credentials_path_set=False, auth_mode="adc",
        )

        def vertex_factory(_config):
            return VertexNanoBananaImageProvider(vconfig, transport=vertex_tx,
                                                 access_token_provider=lambda: "test-token-not-real")

        def nova_factory(config):
            return NovaProxyImageProvider(config, transport=nova_tx)

        fn = vmi.build_vertex_make_image_fn(
            env, batch_id="test-batch", budget_root=tmp_path / "budget",
            provider_factory=vertex_factory, nova_provider_factory=nova_factory,
        )
        return fn, vertex_tx, nova_tx

    return build


def _job(event_id: str = "evt-nova01"):
    return {"event_id": event_id, "article": dict(ARTICLE), "canonical_body_hash": "hash-" + event_id}


def _bundle_files(result: dict) -> dict[str, dict]:
    bundle = Path(result["bundle_dir"])
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in bundle.glob("*.json")}


def _assert_no_key_on_disk(root: Path) -> None:
    for path in root.rglob("*.json"):
        assert FAKE_KEY not in path.read_text(encoding="utf-8"), path


# ----- provider ---------------------------------------------------------------------------


def test_nova_provider_success_records_dims_cost_and_request(tmp_path: Path):
    tx = Scripted([_nova_ok()])
    provider = NovaProxyImageProvider(load_nova_proxy_config(_env()), transport=tx)
    brief = build_visual_brief({"event_id": "e1", "editorial_subject": "Bitcoin ETF inflows", "visual_concept": "markets",
                                "provider_visual_prompt": "x " * 900})
    request = canonical_provider_request(brief)
    result = provider.generate(request, dest_path=str(tmp_path / "a.png"), cold_start=True)
    assert result.success and result.provider_name == "nova"
    assert result.model_name == NOVA_BEDROCK_MODEL_ID
    assert (result.width, result.height) == (1280, 720)
    assert result.provider_reported_cost_usd == pytest.approx(0.06)
    assert Path(result.raw_image_path).is_file()
    call = tx.calls[0]
    assert call["url"] == "https://proxy.test/v1/images/generations"
    body = call["json_body"]
    assert body["model"] == "nova-canvas" and body["n"] == 1 and body["response_format"] == "b64_json"
    assert body["size"] == "1280x720"
    assert len(body["prompt"]) <= PROMPT_CHAR_LIMIT
    assert FAKE_KEY not in json.dumps(provider.recorded_request(request))
    assert FAKE_KEY not in json.dumps(result.as_dict())
    # One attempt per provider instance.
    again = provider.generate(request, dest_path=str(tmp_path / "b.png"), cold_start=False)
    assert not again.success and again.failure_reason == "hard_max_generation_calls_exceeded"
    assert len(tx.calls) == 1


def test_nova_provider_error_is_classified_and_redacted(tmp_path: Path):
    tx = Scripted([_json_response(NovaHttpResponse, 401, {"error": {"message": f"bad key {FAKE_KEY}"}})])
    provider = NovaProxyImageProvider(load_nova_proxy_config(_env()), transport=tx)
    request = canonical_provider_request(build_visual_brief({"event_id": "e", "editorial_subject": "x", "visual_concept": "markets"}))
    result = provider.generate(request, dest_path=str(tmp_path / "a.png"), cold_start=True)
    assert not result.success and result.failure_reason == "nova_auth_error"
    assert FAKE_KEY not in json.dumps(result.as_dict())


def test_prompt_fitting_and_pricing_and_toggle():
    long = "Sentence one is here. " * 100
    fitted = fit_nova_prompt(long)
    assert len(fitted) <= PROMPT_CHAR_LIMIT and fitted.endswith(".")
    prompt = build_nova_prompt({"primary_subject": "A" * 400, "event": "B" * 400,
                                "important_objects": ["c" * 300] * 5})
    assert len(prompt) <= PROMPT_CHAR_LIMIT
    assert nova_price_usd(1024, 576) == pytest.approx(0.04)
    assert nova_price_usd(1280, 720) == pytest.approx(0.06)
    assert nova_price_usd(1024, 1024, quality="premium") == pytest.approx(0.06)
    assert nova_price_usd(2048, 1152, quality="premium") == pytest.approx(0.08)
    assert nova_fallback_enabled({}) is True
    assert nova_fallback_enabled({ENV_FALLBACK_ENABLED: "false"}) is False
    assert nova_fallback_enabled({ENV_FALLBACK_ENABLED: "true"}) is True
    with pytest.raises(NovaConfigError):
        load_nova_proxy_config({ENV_BASE_URL: "https://proxy.test/v1"})
    with pytest.raises(NovaConfigError):
        load_nova_proxy_config(_env(**{ENV_MODEL: "titan-image"}))
    assert load_nova_proxy_config(_env(**{ENV_MODEL: NOVA_BEDROCK_MODEL_ID})).model == NOVA_BEDROCK_MODEL_ID


# ----- fallback wiring ----------------------------------------------------------------------


def test_daily_budget_exhausted_skips_vertex_and_uses_nova(harness, tmp_path: Path):
    env = _env(NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0")
    fn, vertex_tx, nova_tx = harness(env, [], [_nova_ok()])
    result = fn(_job())
    assert result["success"] is True, result
    assert result["provider"] == "nova" and result["fallback_reason"] == "daily_budget_exhausted"
    assert vertex_tx.calls == [] and len(nova_tx.calls) == 1
    assert Path(result["final_path"]).is_file()
    assert result["provider_reported_cost_usd"] == pytest.approx(0.06)
    files = _bundle_files(result)
    assert files["article_link.json"]["image_provider"] == "nova"
    assert files["article_link.json"]["fallback_reason"] == "daily_budget_exhausted"
    assert files["article_link.json"]["nova_prompt_tier"] == PROMPT_TIER_STORY
    assert result["nova_prompt_tier"] == PROMPT_TIER_STORY
    assert files["provider_result.json"]["provider_name"] == "nova"
    assert files["image_freeze.json"]["provider"] == "nova"
    ledger = VertexBudgetLedger(root=tmp_path / "budget", environ=env, batch_id="test-batch")
    assert ledger.daily_calls_used() == 0  # Nova never counts against Vertex
    _assert_no_key_on_disk(tmp_path)
    # Frozen image is reused on the next call: no second Nova image.
    again = fn(_job())
    assert again["success"] and again.get("reused_frozen_image") and len(nova_tx.calls) == 1


def test_vertex_429_falls_back_to_nova_once(harness, tmp_path: Path):
    env = _env()
    fn, vertex_tx, nova_tx = harness(env, [_vertex_429()], [_nova_ok()])
    result = fn(_job("evt-nova429"))
    assert result["success"] is True, result
    assert result["provider"] == "nova" and result["fallback_reason"] == "vertex_rate_limited"
    assert len(vertex_tx.calls) == 1 and len(nova_tx.calls) == 1
    assert result["vertex_request_count"] == 1 and result["nova_request_count"] == 1
    ledger = VertexBudgetLedger(root=tmp_path / "budget", environ=env, batch_id="test-batch")
    assert ledger.daily_calls_used() == 1  # only the real Vertex attempt
    runs = {("nova" if "-nova-" in p.name else p.name.rsplit("-", 1)[-1]): p
            for p in (tmp_path / "make_runs").iterdir()}
    assert sorted(runs) == ["nova", "vertex"]
    vertex_receipt = json.loads((runs["vertex"] / "provider_result.json").read_text(encoding="utf-8"))
    assert vertex_receipt["failure_reason"] == "vertex_rate_limited"
    assert vertex_receipt["http_status"] == 429
    assert vertex_receipt["provider_reported_cost_usd"] == 0.0
    assert _bundle_files(result)["article_link.json"]["image_provider"] == "nova"
    _assert_no_key_on_disk(tmp_path)


def test_vertex_safety_block_does_not_fall_back(harness):
    fn, vertex_tx, nova_tx = harness(_env(), [_vertex_safety()], [])
    result = fn(_job("evt-novasafe"))
    assert result["success"] is False
    assert result["image_failure_code"] == "vertex_safety_block"
    assert len(vertex_tx.calls) == 1 and nova_tx.calls == []


def test_vertex_plain_400_does_not_fall_back(harness):
    fn, vertex_tx, nova_tx = harness(_env(), [_vertex_400_prompt()], [])
    result = fn(_job("evt-nova400"))
    assert result["success"] is False
    assert result["http_status"] == 400
    assert nova_tx.calls == []


def test_vertex_success_never_calls_nova(harness):
    fn, vertex_tx, nova_tx = harness(_env(), [_vertex_ok()], [])
    result = fn(_job("evt-novaok"))
    assert result["success"] is True and result["provider"] == "vertex"
    assert "fallback_reason" not in result and nova_tx.calls == []


def test_fallback_disabled_keeps_old_behaviour(harness):
    env = _env(NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0", **{ENV_FALLBACK_ENABLED: "false"})
    fn, vertex_tx, nova_tx = harness(env, [], [])
    result = fn(_job("evt-novaoff"))
    assert result["success"] is False
    assert result["image_failure_code"] == "VERTEX_DAILY_BUDGET_EXHAUSTED"
    assert vertex_tx.calls == [] and nova_tx.calls == []


def test_nova_failure_reports_both_codes(harness):
    env = _env(NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0")
    bad = _json_response(NovaHttpResponse, 500, {"error": {"message": "upstream exploded"}})
    fn, vertex_tx, nova_tx = harness(env, [], [bad])
    result = fn(_job("evt-novafail"))
    assert result["success"] is False
    assert result["image_failure_code"].startswith("VERTEX_DAILY_BUDGET_EXHAUSTED; nova fallback failed:")
    assert result["nova_failure_code"] == "nova_http_error"
    assert len(nova_tx.calls) == 1


# ----- content-filter handling --------------------------------------------------------------

WINKLEVOSS_BRIEF = {
    "primary_subject": "Winklevoss Twins Propose Low-Fee Spot Zcash ETF at Token2049",
    "event": "Winklevoss Twins Propose Low-Fee Spot Zcash ETF at Token2049",
    "source_headline": "Winklevoss Twins Propose Low-Fee Spot Zcash ETF at Token2049",
    "source_dek_truncated": (
        "Tyler and Cameron Winklevoss presented plans for a spot Zcash ETF at Token2049, "
        "aiming to challenge Grayscale's existing product with lower fees."
    ),
}


def test_nova_prompt_never_copies_names_brands_or_tickers():
    for tier in (PROMPT_TIER_STORY, PROMPT_TIER_GENERIC):
        prompt = build_nova_prompt(WINKLEVOSS_BRIEF, category="ETF News", tier=tier)
        low = prompt.lower()
        for banned in ("winklevoss", "tyler", "cameron", "zcash", "token2049", "grayscale", "etf"):
            assert banned not in low, (tier, banned, prompt)
        assert len(prompt) <= PROMPT_CHAR_LIMIT
    story = build_nova_prompt(WINKLEVOSS_BRIEF, category="ETF News")
    assert "trading desk" in story and "privacy" in story
    assert detect_story_themes("Hackers drain $40M from lender after exploit")[0] == "security"
    assert detect_story_themes("SEC sues exchange over unregistered securities")[0] == "regulation"
    generic_sec = build_nova_prompt({"source_headline": "Hackers drain bridge in exploit"}, tier=PROMPT_TIER_GENERIC)
    assert "padlock" in generic_sec and "hack" not in generic_sec.lower()
    cleaned = sanitize_nova_text('The Winklevoss Twins said "we will win" about $ZEC with BlackRock and NYSE.')
    for banned in ("Winklevoss", "we will win", "ZEC", "BlackRock", "NYSE"):
        assert banned not in cleaned


def test_nova_safety_classification_is_specific():
    assert classify_nova_http_error(400, INPUT_FILTER_MSG) == "nova_safety_block"
    assert nova_filter_stage(INPUT_FILTER_MSG) == "input"
    out_msg = "All of the generated images have been blocked by our content filters."
    assert classify_nova_http_error(400, out_msg) == "nova_safety_block"
    assert nova_filter_stage(out_msg) == "output"
    # Other 4xx are request errors, even with words like "blocked" or "policy".
    assert classify_nova_http_error(400, "ValidationException: size must be a multiple of 16") == "nova_bad_request"
    assert classify_nova_http_error(400, "Request blocked: invalid parameter policy") == "nova_bad_request"
    assert classify_nova_http_error(403, INPUT_FILTER_MSG) == "nova_auth_error"
    assert classify_nova_http_error(500, INPUT_FILTER_MSG) == "nova_http_error"


def test_safety_block_then_generic_prompt_succeeds(harness, tmp_path: Path):
    env = _env(NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0")
    fn, vertex_tx, nova_tx = harness(env, [], [_nova_blocked(), _nova_ok()])
    result = fn(_job("evt-novaretry"))
    assert result["success"] is True, result
    assert result["provider"] == "nova" and result["nova_prompt_tier"] == PROMPT_TIER_GENERIC
    assert result["fallback_reason"] == "daily_budget_exhausted"
    assert [a["tier"] for a in result["nova_attempts"]] == [PROMPT_TIER_STORY, PROMPT_TIER_GENERIC]
    assert result["nova_attempts"][0]["code"] == "nova_safety_block"
    assert result["image_request_count"] == 2 and result["nova_request_count"] == 2
    assert vertex_tx.calls == [] and len(nova_tx.calls) == 2
    first, second = (c["json_body"]["prompt"] for c in nova_tx.calls)
    assert first != second and "Abstract" in second
    files = _bundle_files(result)
    assert files["article_link.json"]["nova_prompt_tier"] == PROMPT_TIER_GENERIC
    assert files["image_freeze.json"]["nova_prompt_tier"] == PROMPT_TIER_GENERIC
    blocked_dir = Path(result["nova_attempts"][0]["bundle_dir"])
    blocked = json.loads((blocked_dir / "provider_result.json").read_text(encoding="utf-8"))
    diag = blocked["provider_reported_usage"]["_safe_response_diagnostic"]
    assert blocked["failure_reason"] == "nova_safety_block"
    assert diag["content_filter_stage"] == "input" and diag["prompt_sent"] == first
    ledger = VertexBudgetLedger(root=tmp_path / "budget", environ=env, batch_id="test-batch")
    assert ledger.daily_calls_used() == 0
    _assert_no_key_on_disk(tmp_path)


def test_both_nova_prompts_blocked_gives_clean_error(harness):
    env = _env(NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0")
    fn, vertex_tx, nova_tx = harness(env, [], [_nova_blocked(), _nova_blocked()])
    result = fn(_job("evt-novablock2"))
    assert result["success"] is False
    assert result["image_failure_code"] == (
        "VERTEX_DAILY_BUDGET_EXHAUSTED; nova fallback failed: nova_safety_block (story_scene, category_generic)"
    )
    assert result["nova_failure_code"] == "nova_safety_block"
    assert len(nova_tx.calls) == 2  # never a third call


def test_vertex_429_then_nova_block_then_generic(harness, tmp_path: Path):
    env = _env()
    fn, vertex_tx, nova_tx = harness(env, [_vertex_429()], [_nova_blocked(), _nova_ok()])
    result = fn(_job("evt-nova429b"))
    assert result["success"] is True, result
    assert result["fallback_reason"] == "vertex_rate_limited"
    assert result["nova_prompt_tier"] == PROMPT_TIER_GENERIC
    assert result["vertex_request_count"] == 1 and result["nova_request_count"] == 2
    assert result["image_request_count"] == 3
    ledger = VertexBudgetLedger(root=tmp_path / "budget", environ=env, batch_id="test-batch")
    assert ledger.daily_calls_used() == 1


def test_non_safety_nova_error_is_not_retried(harness):
    env = _env(NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0")
    bad = _json_response(NovaHttpResponse, 400, {"error": {"message": "ValidationException: bad size"}})
    fn, vertex_tx, nova_tx = harness(env, [], [bad])
    result = fn(_job("evt-novabad"))
    assert result["success"] is False and result["nova_failure_code"] == "nova_bad_request"
    assert len(nova_tx.calls) == 1
