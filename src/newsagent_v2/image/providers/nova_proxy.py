"""AWS Nova Canvas image provider through the OpenAI-compatible proxy.

Used only as the fallback when the Vertex image path is blocked (daily Vertex
budget exhausted, or Vertex answered 429 / RESOURCE_EXHAUSTED / quota).

- Endpoint: ``{NEWSAGENT_V2_V4_KIMI_BASE_URL}/images/generations`` (same proxy and
  same key as the Kimi writer: ``NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY``).
- Model: ``NEWSAGENT_V2_NOVA_MODEL`` (default ``nova-canvas``), served by Bedrock
  ``amazon.nova-canvas-v1:0``.
- Text-to-image only. Reference images are never sent; the Python compositor still
  applies the site logo afterwards.
- One HTTP attempt per provider instance, no retries. Never logs or persists the key.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping

import requests
from PIL import Image

from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.image.provider import (
    ImageProvider,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
)
from newsagent_v2.image.validate import file_sha256, refuse_overwrite

PROVIDER_NAME = "nova"
NOVA_BEDROCK_MODEL_ID = "amazon.nova-canvas-v1:0"
DEFAULT_PROXY_MODEL = "nova-canvas"
ACCEPTED_MODELS = frozenset({"nova-canvas", NOVA_BEDROCK_MODEL_ID})

ENV_FALLBACK_ENABLED = "NEWSAGENT_V2_NOVA_FALLBACK_ENABLED"
ENV_MODEL = "NEWSAGENT_V2_NOVA_MODEL"
# Reuse the proxy + key the Kimi writer already uses (see newsagent_v2.write.kimi).
ENV_BASE_URL = "NEWSAGENT_V2_V4_KIMI_BASE_URL"
ENV_API_KEY = "NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY"

PROMPT_CHAR_LIMIT = 1024
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_TIMEOUT_SECONDS = 120
HARD_MAX_GENERATION_CALLS = 1

# Bedrock Nova Canvas list price per image (USD). Flat per-image pricing.
NOVA_PRICE_USD = {
    "standard": {"up_to_1024": 0.04, "up_to_2048": 0.06},
    "premium": {"up_to_1024": 0.06, "up_to_2048": 0.08},
}
NOVA_PRICING_SOURCE = "AWS Bedrock Nova Canvas on-demand list price per image"


class NovaConfigError(ValueError):
    """Nova fallback is not configured. Message never contains secrets."""


@dataclass(frozen=True)
class NovaProxyConfig:
    base_url: str
    api_key: str
    model: str = DEFAULT_PROXY_MODEL
    quality: str = "standard"

    def secrets(self) -> tuple[str, ...]:
        return (self.api_key,) if self.api_key else ()

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/images/generations"


def nova_fallback_enabled(environ: Mapping[str, str] | None) -> bool:
    """Default ON. Only an explicit false/0/no/off disables the fallback."""
    raw = str((environ or {}).get(ENV_FALLBACK_ENABLED, "") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def load_nova_proxy_config(environ: Mapping[str, str] | None) -> NovaProxyConfig:
    env = environ or {}
    base_url = str(env.get(ENV_BASE_URL) or "").strip().strip('"').strip("'")
    api_key = str(env.get(ENV_API_KEY) or "").strip().strip('"').strip("'")
    model = str(env.get(ENV_MODEL) or DEFAULT_PROXY_MODEL).strip() or DEFAULT_PROXY_MODEL
    missing = [name for name, value in ((ENV_BASE_URL, base_url), (ENV_API_KEY, api_key)) if not value]
    if missing:
        raise NovaConfigError("nova_config_incomplete: missing " + ", ".join(missing))
    if not base_url.lower().startswith("https://"):
        raise NovaConfigError(f"nova_config_invalid: {ENV_BASE_URL} must be an https URL")
    if model not in ACCEPTED_MODELS:
        raise NovaConfigError(
            f"nova_config_invalid: {ENV_MODEL} must be one of {sorted(ACCEPTED_MODELS)}"
        )
    return NovaProxyConfig(base_url=base_url, api_key=api_key, model=model)


def nova_price_usd(width: int | None, height: int | None, *, quality: str = "standard") -> float:
    """List price for one image. Either side above 1024 uses the 2048 tier (conservative)."""
    tiers = NOVA_PRICE_USD.get(quality, NOVA_PRICE_USD["standard"])
    w = int(width or 0)
    h = int(height or 0)
    if w and h and w <= 1024 and h <= 1024:
        return tiers["up_to_1024"]
    return tiers["up_to_2048"]


def fit_nova_prompt(prompt: str, limit: int = PROMPT_CHAR_LIMIT) -> str:
    """Collapse whitespace and keep the prompt within Nova's character limit.

    Cuts at the last sentence end that fits, then at a word boundary.
    """
    text = " ".join(str(prompt or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("; "))
    if cut >= int(limit * 0.5):
        return head[: cut + 1].strip()
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).rstrip(" ,;:-")


PROMPT_TIER_STORY = "story_scene"
PROMPT_TIER_GENERIC = "category_generic"
PROMPT_TIERS = (PROMPT_TIER_STORY, PROMPT_TIER_GENERIC)

# Story themes, detected from headline / dek / category. Only the fixed scene text on the
# right ever reaches Nova: no headline words, so no real people, companies, brands,
# tickers, logos or quoted text. Order is priority; at most two themes are combined.
_THEMES: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (name, re.compile(pattern, re.I), scene)
    for name, pattern, scene in (
        (
            "security",
            r"\b(hack\w*|exploit\w*|breach\w*|stole\w*|steal\w*|drain\w*|attack\w*|heist|phish\w*|"
            r"scam\w*|fraud\w*|launder\w*|vulnerab\w*|rug ?pull\w*|theft|compromis\w*)\b",
            "a glowing digital padlock floating above a dark network of connected nodes, cybersecurity concept",
        ),
        (
            "regulation",
            r"\b(sec|cftc|regulat\w*|law|laws|legislat\w*|bill|bills|senate|congress\w*|court\w*|judge\w*|"
            r"lawsuit\w*|ruling\w*|polic(y|ies)|complian\w*|mica|ban|bans|banned|tax\w*|government\w*|"
            r"treasury|lawmaker\w*|regulator\w*|approv\w*)\b",
            "a classical stone government building with tall columns at dusk, warm light, calm civic atmosphere",
        ),
        (
            "etf",
            r"\b(etfs?|etps?|funds?|asset managers?|issuers?|inflows?|outflows?|filings?|spot product)\b",
            "a modern trading desk with several large monitors showing abstract glowing line charts, "
            "institutional investment atmosphere",
        ),
        (
            "privacy",
            r"\b(privacy|private|zcash|zec|monero|xmr|zero[- ]knowledge|zk\w*|shielded|anonym\w*)\b",
            "streams of softly glowing encrypted data flowing through translucent shields, digital privacy concept",
        ),
        (
            "mining",
            r"\b(min(ing|er|ers)|hash ?rate|halving)\b",
            "long rows of computer mining rigs with small blinking lights inside a cool blue data center",
        ),
        (
            "payments",
            r"\b(stablecoins?|payments?|remittances?|cbdcs?|digital dollar)\b",
            "a sleek contactless payment terminal beside a smartphone on a clean desk, digital payments concept",
        ),
        (
            "meme",
            r"\b(memes?|memecoins?|doge\w*|shiba\w*|pepe|floki|bonk)\b",
            "a friendly shiba inu dog sitting calmly beside a small stack of shiny golden coins, soft studio light",
        ),
        (
            "network",
            r"\b(defi|decentrali\w*|lending|liquidity|staking|yield|dex|protocols?|layer[- ]?(2|two)|"
            r"blockchains?|upgrades?|mainnet|testnet|smart contracts?)\b",
            "an abstract three-dimensional web of glowing connected blocks and nodes, decentralized network concept",
        ),
        (
            "ai",
            r"\b(ai|artificial intelligence|machine learning|agents?)\b",
            "an abstract neural network made of soft light inside a modern server room",
        ),
        (
            "conference",
            r"\b(conferences?|summits?|forums?|expo|keynotes?|token2049|consensus|devcon)\b",
            "a modern conference stage with large blank screens and softly lit audience silhouettes",
        ),
        (
            "markets",
            r"\b(prices?|rall\w*|surg\w*|jump\w*|climb\w*|gain\w*|drop\w*|fall\w*|slump\w*|crash\w*|"
            r"plung\w*|record|highs?|lows?|markets?|trad(e|es|ing|ers?)|volatil\w*|liquidat\w*|bull\w*|bear\w*)\b",
            "large blank trading screens with abstract glowing candlestick charts in a dim modern trading room",
        ),
        (
            "crypto",
            r"\b(bitcoin|btc|ethereum|eth|crypto\w*|tokens?|coins?|altcoins?|xrp|solana|wallets?|exchanges?)\b",
            "a single polished golden cryptocurrency coin with an abstract circuit pattern on a dark reflective surface",
        ),
    )
)

_GENERIC_SCENES = {
    "security": (
        "Abstract digital art of a glowing geometric padlock shape made of blue light on a dark gradient "
        "background, soft floating particles, clean minimal modern style, wide 16:9 landscape."
    ),
    "regulation": (
        "Abstract digital art of balanced geometric shapes and soft light beams on a dark blue gradient "
        "background, calm and orderly, clean minimal modern style, wide 16:9 landscape."
    ),
    "default": (
        "Abstract visualization of digital market data: flowing glowing blue and teal lines, soft light "
        "particles and gentle bokeh on a dark gradient background, clean minimal modern style, wide 16:9 landscape."
    ),
}

_STORY_STYLE = (
    "Single clear focal point, clean uncluttered composition, strong depth of field, cinematic soft lighting, "
    "serious and calm mood, realistic textures, muted blue and teal palette with warm accents. "
    "Open empty space in the top-right corner. Imagery only, plain unmarked surfaces."
)

# Safety net for any free text: names, brands, tickers and quotes are removed.
_BRAND_TERMS = (
    "binance", "coinbase", "okx", "kraken", "bybit", "bitget", "kucoin", "gemini", "robinhood", "blackrock",
    "fidelity", "grayscale", "vanguard", "invesco", "franklin templeton", "ark invest", "bitwise", "vaneck",
    "nyse", "nasdaq", "cme", "jpmorgan", "goldman sachs", "morgan stanley", "citi", "hsbc", "visa", "mastercard",
    "paypal", "tether", "circle", "ripple", "microstrategy", "tesla", "metamask", "uniswap", "opensea",
    "federal reserve", "the fed", "white house", "token2049",
)
_QUOTED_RE = re.compile(r"[\"\u201c\u201d][^\"\u201c\u201d]{0,200}[\"\u201c\u201d]|'[^']{2,80}'|\u2018[^\u2019]{0,200}\u2019")
_TICKER_RE = re.compile(r"\$[A-Za-z]{2,10}\b|\b[A-Z]{2,6}\b")
_BRAND_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in _BRAND_TERMS) + r")\b", re.I)
# Two or more consecutive Capitalised words that are not sentence-initial (likely a proper name).
_PROPER_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")


def sanitize_nova_text(text: str) -> str:
    """Remove quoted text, tickers, known brand/organisation names and multi-word proper names."""
    out = _QUOTED_RE.sub(" ", str(text or ""))
    out = _BRAND_RE.sub(" ", out)
    out = _TICKER_RE.sub(" ", out)
    out = _PROPER_RE.sub(" ", out)
    out = re.sub(r"\s+([,.;:])", r"\1", " ".join(out.split()))
    return out.strip()


def detect_story_themes(*texts: str, limit: int = 2) -> list[str]:
    blob = " ".join(str(t or "") for t in texts)
    found = [name for name, pattern, _scene in _THEMES if pattern.search(blob)]
    return found[:limit]


def _theme_scene(name: str) -> str:
    for theme, _pattern, scene in _THEMES:
        if theme == name:
            return scene
    return ""


def _story_texts(story_brief: Mapping[str, Any] | None, category: str | None, extra: str = "") -> list[str]:
    brief = story_brief or {}
    return [
        str(brief.get("source_headline") or brief.get("primary_subject") or ""),
        str(brief.get("event") or ""),
        str(brief.get("source_dek_truncated") or ""),
        str(category or ""),
        extra,
    ]


def build_nova_prompt(
    story_brief: Mapping[str, Any] | None,
    fallback_prompt: str = "",
    *,
    category: str | None = None,
    tier: str = PROMPT_TIER_STORY,
) -> str:
    """Nova Canvas prompt (<= 1024 chars) built only from fixed, brand-free scene text.

    The story's headline, dek and category are used to *choose* a generic editorial scene
    (ETF, regulation, privacy, security, markets, ...). None of their words are copied in,
    because Nova's input filter blocks real people, companies, brands, logos and some
    finance/crime words. The Coin Network logo is added later by the compositor.

    tier=story_scene: topic scene for this story.
    tier=category_generic: very safe abstract image for the story's broad category
    (used once after a story_scene safety block).
    """
    themes = detect_story_themes(*_story_texts(story_brief, category, fallback_prompt))
    if tier == PROMPT_TIER_GENERIC:
        key = "security" if "security" in themes else "regulation" if "regulation" in themes else "default"
        return fit_nova_prompt(sanitize_nova_text(_GENERIC_SCENES[key]))
    if not themes:
        themes = ["crypto"]
    parts = [
        "Premium editorial news photograph for a financial news website, wide 16:9 landscape.",
        f"Scene: {_theme_scene(themes[0])}.",
    ]
    if len(themes) > 1:
        parts.append(f"Secondary element: {_theme_scene(themes[1])}.")
    parts.append(_STORY_STYLE)
    return fit_nova_prompt(sanitize_nova_text(" ".join(parts)))


class NovaHttpResponse:
    def __init__(self, status_code: int, content: bytes, headers: Any = None, text: str | None = None) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self._text = text

    @property
    def text(self) -> str:
        if self._text is not None:
            return self._text
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


def default_transport(url: str, *, headers: dict[str, str], json_body: dict[str, Any], timeout: int) -> NovaHttpResponse:
    response = requests.post(url, headers=headers, json=json_body, timeout=timeout, allow_redirects=False)
    return NovaHttpResponse(
        status_code=response.status_code,
        content=response.content or b"",
        headers=response.headers,
        text=response.text,
    )


def _header(headers: Any, name: str) -> str | None:
    try:
        value = headers.get(name) or headers.get(name.lower())
    except Exception:
        return None
    return str(value) if value else None


def _error_message(payload: Any, text: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err.get("code") or "")
        if isinstance(err, str):
            return err
        if payload.get("message"):
            return str(payload.get("message"))
        if payload.get("detail"):
            return str(payload.get("detail"))
    return " ".join(str(text or "").split())[:300]


# Nova / Bedrock content-filter wording, e.g.
#   input:  "This request has been blocked by our content filters. Please adjust your text prompt..."
#   output: "All of the generated images have been blocked by our content filters..."
_SAFETY_RE = re.compile(r"content filters?|responsible ai|blocked by (our|the) (content|safety)", re.I)
_OUTPUT_FILTER_RE = re.compile(r"generated images? (has|have) been blocked|all of the generated", re.I)


def nova_filter_stage(message: str) -> str | None:
    """'input' or 'output' for a content-filter message, else None."""
    if not _SAFETY_RE.search(str(message or "")):
        return None
    return "output" if _OUTPUT_FILTER_RE.search(str(message or "")) else "input"


def classify_nova_http_error(status: int | None, message: str) -> str:
    blob = str(message or "").lower()
    if status in (401, 403):
        return "nova_auth_error"
    if status == 429 or "throttl" in blob or "too many requests" in blob:
        return "nova_rate_limited"
    if status == 404:
        return "nova_model_not_found"
    # Only Bedrock's content-filter wording is a safety block; other 4xx are request errors.
    if status is not None and 400 <= status < 500 and nova_filter_stage(message):
        return "nova_safety_block"
    if status is not None and 400 <= status < 500:
        return "nova_bad_request"
    return "nova_http_error"


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    return ".png"


class NovaProxyImageProvider(ImageProvider):
    """Nova Canvas via proxy ``POST /v1/images/generations`` (OpenAI images API shape)."""

    reference_images_supported = False

    def __init__(
        self,
        config: NovaProxyConfig,
        *,
        transport: Callable[..., NovaHttpResponse] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self.model = config.model
        self._transport = transport or default_transport
        self.timeout_seconds = timeout_seconds
        self.generation_calls = 0

    def secrets(self) -> tuple[str, ...]:
        return self._config.secrets()

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider_name=PROVIDER_NAME,
            model_name=NOVA_BEDROCK_MODEL_ID,
            model_version=None,
            backend_type="remote_hosted",
            deployment_notes=(
                f"AWS Bedrock Nova Canvas via OpenAI-compatible proxy /images/generations "
                f"(proxy model id {self.model}). Text-to-image only; logo applied by Python compositor."
            ),
        )

    def _size(self, request: ProviderRequest) -> tuple[int, int]:
        width = int(request.width or DEFAULT_WIDTH)
        height = int(request.height or DEFAULT_HEIGHT)
        return width, height

    def wire_body(self, request: ProviderRequest) -> dict[str, Any]:
        width, height = self._size(request)
        return {
            "model": self.model,
            "prompt": fit_nova_prompt(request.prompt),
            "size": f"{width}x{height}",
            "n": 1,
            "response_format": "b64_json",
        }

    def recorded_request(self, request: ProviderRequest) -> dict[str, Any]:
        body = self.wire_body(request)
        payload = {
            "provider_name": PROVIDER_NAME,
            "model": NOVA_BEDROCK_MODEL_ID,
            "proxy_model": self.model,
            "endpoint_path": "/images/generations",
            "base_url_env": ENV_BASE_URL,
            "api_key_env": ENV_API_KEY,
            "method": "POST",
            "auth": "Bearer proxy key",
            "authorization_included": False,
            "allow_redirects": False,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": 0,
            "hard_max_generation_calls": HARD_MAX_GENERATION_CALLS,
            "body": body,
            "prompt_chars": len(body["prompt"]),
            "prompt_char_limit": PROMPT_CHAR_LIMIT,
            "negative_prompt_sent": False,
            "reference_images_supported": False,
            "reference_image_dropped": bool(request.reference_image_path),
        }
        return redact_secrets(payload, self.secrets())

    def generate(self, request: ProviderRequest, *, dest_path: str, cold_start: bool) -> ProviderResult:
        identity = self.identity()
        if self.generation_calls >= HARD_MAX_GENERATION_CALLS:
            return self._failure(identity, cold_start, 0, None, None, "hard_max_generation_calls_exceeded")
        self.generation_calls += 1

        body = self.wire_body(request)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }
        started = perf_counter()
        try:
            response = self._transport(
                self._config.endpoint, headers=headers, json_body=body, timeout=self.timeout_seconds
            )
        except requests.RequestException as exc:
            elapsed = int((perf_counter() - started) * 1000)
            return self._failure(identity, cold_start, elapsed, None, None, f"transport_error: {exc.__class__.__name__}")
        elapsed = int((perf_counter() - started) * 1000)
        status = response.status_code
        request_id = _header(response.headers, "x-request-id") or _header(response.headers, "x-amzn-requestid")
        content_type = _header(response.headers, "content-type")

        if status is not None and 300 <= status < 400:
            return self._failure(identity, cold_start, elapsed, status, request_id, "unsafe_redirect")

        try:
            payload = response.json()
        except Exception:
            payload = None

        if status != 200 or not isinstance(payload, dict) or isinstance(payload.get("error"), (dict, str)):
            message = _error_message(payload, response.text if payload is None else "")
            reason = classify_nova_http_error(status, message) if status != 200 else "nova_malformed_response"
            diagnostic = {
                "http_status": status,
                "response_content_type": content_type,
                "safe_error_message": str(redact_secrets(message[:320], self.secrets())),
                "content_filter_stage": nova_filter_stage(message),
                "prompt_sent": body["prompt"],
            }
            return self._failure(identity, cold_start, elapsed, status, request_id, reason, diagnostic=diagnostic)

        data = payload.get("data")
        first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        b64 = first.get("b64_json")
        if not isinstance(b64, str) or not b64.strip():
            return self._failure(
                identity, cold_start, elapsed, status, request_id, "nova_missing_image",
                diagnostic={"http_status": status, "top_level_keys": sorted(str(k) for k in payload.keys())},
            )
        try:
            image_bytes = base64.b64decode(b64, validate=False)
            with Image.open(BytesIO(image_bytes)) as image:
                image.load()
                width, height = image.size
            if width <= 0 or height <= 0:
                raise ValueError("zero_dimensions")
        except Exception:
            return self._failure(identity, cold_start, elapsed, status, request_id, "invalid_image_bytes")

        dest = Path(dest_path)
        suffix = _image_suffix(image_bytes)
        if dest.suffix.lower() != suffix:
            dest = dest.with_suffix(suffix)
        refuse_overwrite(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(image_bytes)

        req_w, req_h = self._size(request)
        cost = nova_price_usd(width, height, quality=self._config.quality)
        usage: dict[str, Any] = {
            "images": 1,
            "quality": self._config.quality,
            "returned_width": width,
            "returned_height": height,
            "list_price_usd": cost,
            "pricing_source": NOVA_PRICING_SOURCE,
            "prompt_chars": len(body["prompt"]),
        }
        if isinstance(payload.get("usage"), dict):
            usage["proxy_usage"] = payload["usage"]
        return ProviderResult(
            provider_name=identity.provider_name,
            success=True,
            model_name=identity.model_name,
            backend_type=identity.backend_type,
            total_latency_ms=elapsed,
            first_image_latency_ms=elapsed if cold_start else None,
            cold_start=cold_start,
            warm_generation=not cold_start,
            width=width,
            height=height,
            retry_count=0,
            provider_reported_cost=cost,
            raw_image_path=str(dest),
            sha256=file_sha256(dest),
            http_status=status,
            request_latency_ms=elapsed,
            response_format=content_type or "application/json",
            size_bytes=len(image_bytes),
            provider_reported_usage=redact_secrets(usage, self.secrets()),
            requested_width=req_w,
            requested_height=req_h,
            provider_reported_cost_usd=cost,
            estimated_list_price_usd=cost,
            estimated_list_price_is_estimate=False,
            estimated_list_price_note=(
                f"{NOVA_PRICING_SOURCE}: ${cost:.2f} ({self._config.quality}); flat per image, "
                "so list price equals billed price."
            ),
            requested_aspect_ratio="16:9",
            provider_request_id=request_id,
            total_pipeline_latency_ms=elapsed,
            deployment_notes=identity.deployment_notes,
        )

    def _failure(
        self,
        identity: ProviderIdentity,
        cold_start: bool,
        elapsed: int,
        http_status: int | None,
        request_id: str | None,
        reason: str,
        *,
        diagnostic: dict[str, Any] | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            provider_name=identity.provider_name,
            success=False,
            model_name=identity.model_name,
            backend_type=identity.backend_type,
            total_latency_ms=elapsed,
            cold_start=cold_start,
            warm_generation=not cold_start,
            retry_count=0,
            failure_reason=reason,
            http_status=http_status,
            request_latency_ms=elapsed,
            provider_reported_usage=(
                {"_safe_response_diagnostic": redact_secrets(diagnostic, self.secrets())} if diagnostic else None
            ),
            provider_request_id=request_id,
            deployment_notes=identity.deployment_notes,
        )
