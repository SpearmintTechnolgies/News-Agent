"""Vertex Nano Banana image_fn for V4 production make path.

Same return contract as build_flux_make_image_fn.
Cost guards: kill switch, per-candidate/batch/day ceilings, frozen-image reuse.
Provider retries = 0. Never calls Kimi. Never regenerates articles.

Ordering (mandatory):
  1) validate Vertex configuration + credential path
  2) construct/validate provider readiness
  3) THEN reserve Vertex budget
  4) THEN provider.generate

Config failures must leave budget reservations at 0.

Nova fallback (NEWSAGENT_V2_NOVA_FALLBACK_ENABLED, default on):
  - daily Vertex budget exhausted -> skip Vertex, one Nova Canvas call
  - Vertex rejected for rate limit / quota (429, RESOURCE_EXHAUSTED) -> one Nova call
  Safety blocks, prompt errors and config/auth failures never fall back.
  Nova calls do not touch the Vertex budget ledger counters.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.image.compositor import CompositionSpec, compose_card
from newsagent_v2.image.contract import CARD_HEIGHT, CARD_WIDTH
from newsagent_v2.image.provider import canonical_provider_request
from newsagent_v2.image.providers.nova_proxy import (
    NOVA_BEDROCK_MODEL_ID,
    NovaConfigError,
    NovaProxyImageProvider,
    build_nova_prompt,
    load_nova_proxy_config,
    nova_fallback_enabled,
)
from newsagent_v2.image.providers.vertex_nano_banana import (
    HARD_MAX_GENERATION_CALLS,
    VertexConfigError,
    VertexNanoBananaImageProvider,
    load_vertex_nano_banana_config,
    resolve_vertex_config_status,
)
from newsagent_v2.image.story_image_brief import build_story_image_brief
from newsagent_v2.image.validate import file_sha256, validate_raw_artwork
from newsagent_v2.image.vertex_budget import (
    CODE_DAILY_EXHAUSTED,
    CODE_VERTEX_DISABLED,
    VertexBudgetLedger,
    guarded_vertex_generate,
    vertex_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
LOGO_PATH = REPO_ROOT / "brand" / "coinnetwork_logo.png"
EXPECTED_LOGO_SHA256 = "09c8106c0458e5225530ac6736a56944c81fbde6d7b81ed5b6ce69586e516818"
MAKE_RUNS_ROOT = REPO_ROOT / "output" / "make_runs"

logger = logging.getLogger(__name__)

FALLBACK_DAILY_BUDGET = "daily_budget_exhausted"
FALLBACK_VERTEX_RATE_LIMITED = "vertex_rate_limited"
_QUOTA_RE = re.compile(r"resource[_ ]?exhausted|quota|rate[ _-]?limit|too many requests", re.I)


def is_vertex_quota_failure(result: dict[str, Any]) -> bool:
    """True only for Vertex rate-limit / quota rejections (HTTP 429 or RESOURCE_EXHAUSTED/quota).

    The provider's classifier also maps some non-429 messages to vertex_rate_limited,
    so a plain 4xx needs explicit quota wording before we fall back.
    Safety blocks and prompt errors never qualify.
    """
    if str(result.get("image_failure_code") or "") != "vertex_rate_limited":
        return False
    if result.get("http_status") == 429:
        return True
    diag = result.get("safe_diagnostic") if isinstance(result.get("safe_diagnostic"), dict) else {}
    blob = f"{diag.get('safe_error_status') or ''} {diag.get('safe_error_message') or ''}"
    return bool(_QUOTA_RE.search(blob))


def resolve_article_logo(environ: dict[str, str] | None = None) -> Path:
    """The website being written for supplies the logo. Coin Network keeps its master file."""
    import os

    from newsagent_v2.control.sites import logo_for_environ

    source = os.environ if environ is None else environ
    chosen = logo_for_environ(source)
    if chosen is not None and chosen.is_file():
        return chosen
    return LOGO_PATH


def _logo_is_locked(logo: Path) -> bool:
    """Only the Coin Network master is refused when its hash changes."""
    try:
        if logo.resolve() == Path(LOGO_PATH).resolve():
            return True
    except OSError:
        pass
    return logo.name == "coinnetwork_logo.png"


def logo_rejection(logo: Path) -> str | None:
    if not logo.is_file():
        return "logo_hash_mismatch"
    if _logo_is_locked(logo) and file_sha256(logo) != EXPECTED_LOGO_SHA256:
        return "logo_hash_mismatch"
    return None


def _resize_to_card(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as image:
        image.load()
        rgb = image.convert("RGB")
        if rgb.size != (CARD_WIDTH, CARD_HEIGHT):
            rgb = rgb.resize((CARD_WIDTH, CARD_HEIGHT), Image.Resampling.LANCZOS)
        rgb.save(dest, format="PNG")


def _config_incomplete(
    *,
    event_id: str,
    missing_fields: list[str] | None = None,
    reason: str = "vertex_config_incomplete",
    failure_code: str | None = None,
) -> dict[str, Any]:
    """Fail closed BEFORE budget reservation. image_request_count must stay 0."""
    code = failure_code or reason
    if missing_fields and "vertex_auth_not_ready" in missing_fields:
        code = "vertex_auth_not_ready"
    return {
        "success": False,
        "event_id": event_id,
        "reason": reason,
        "image_failure_code": code,
        "image_request_count": 0,
        "budget_reserved": False,
        "safe_diagnostic": {"missing_fields": list(missing_fields or [])},
    }


def _prepare_vertex_provider(
    environ: dict[str, str],
    *,
    provider_factory: Callable[..., Any] | None,
) -> tuple[Any, Any] | dict[str, Any]:
    """
    Validate config + local auth + construct provider WITHOUT reserving budget
    or calling generateContent.

    Returns (config, provider) on success, or a failure dict (caller must not reserve).
    """
    if provider_factory is not None:
        class _Cfg:
            model = "fake-vertex"
            location = "global"
            project = "fake"

        config = _Cfg()
        return config, provider_factory(config)

    # Sync process env, then require google.auth.default() to resolve locally.
    from newsagent_v2.image.vertex_local_env import sync_vertex_process_environ

    sync_vertex_process_environ(environ)
    status = resolve_vertex_config_status(environ)
    if not status.get("ready"):
        missing = list(status.get("missing_fields") or [])
        auth_markers = {
            "vertex_auth_not_ready",
            "application_default_credentials",
            "process_env:GOOGLE_APPLICATION_CREDENTIALS",
            "credential_file_unreadable",
            "resolved_project_mismatch",
        }
        auth_fail = any(m in missing for m in auth_markers)
        return _config_incomplete(
            event_id="",
            missing_fields=missing,
            reason="vertex_auth_not_ready" if auth_fail else "vertex_config_incomplete",
            failure_code="vertex_auth_not_ready" if auth_fail else "vertex_config_incomplete",
        )

    creds_path = str(environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if creds_path and not Path(creds_path).is_file():
        return _config_incomplete(
            event_id="",
            missing_fields=["GOOGLE_APPLICATION_CREDENTIALS"],
            reason="vertex_credential_path_missing",
            failure_code="vertex_auth_not_ready",
        )

    try:
        config = load_vertex_nano_banana_config(environ)
    except VertexConfigError as exc:
        return {
            "success": False,
            "event_id": "",
            "reason": str(exc),
            "image_failure_code": "vertex_config_error",
            "image_request_count": 0,
            "budget_reserved": False,
        }

    # Construct provider object as readiness proof (no network generate).
    provider = VertexNanoBananaImageProvider(config)
    return config, provider


def build_vertex_make_image_fn(
    environ: dict[str, str],
    *,
    batch_id: str | None = None,
    budget_root: Path | None = None,
    provider_factory: Callable[..., Any] | None = None,
    nova_provider_factory: Callable[..., Any] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """
    Production image_fn: config-ready → guards/reserve → StoryImageBrief → Vertex once → brand.

    provider_factory: optional injectable factory for offline tests
      (config) -> provider with .generate(...) and .generation_calls / .recorded_request
    nova_provider_factory: optional injectable factory for the Nova fallback in tests
      (NovaProxyConfig) -> provider
    """

    ledger = VertexBudgetLedger(
        root=budget_root,
        environ=environ,
        batch_id=batch_id or "make-batch",
    )

    def image_fn(job: dict[str, Any]) -> dict[str, Any]:
        event_id = str(job.get("event_id") or "")
        article = job.get("article") if isinstance(job.get("article"), dict) else {}
        packet = job.get("evidence_packet") if isinstance(job.get("evidence_packet"), dict) else None
        article_hash = str(job.get("canonical_body_hash") or job.get("article_hash") or "")

        # Kill switch before any config/logo work or budget reservation.
        if not vertex_enabled(environ):
            return {
                "success": False,
                "event_id": event_id,
                "reason": "NEWSAGENT_V2_VERTEX_ENABLED is false",
                "image_failure_code": CODE_VERTEX_DISABLED,
                "image_request_count": 0,
                "budget_reserved": False,
            }

        # ------------------------------------------------------------------
        # 1–3: config + credential path + provider readiness BEFORE reserve
        # ------------------------------------------------------------------
        prepared = _prepare_vertex_provider(environ, provider_factory=provider_factory)
        if isinstance(prepared, dict):
            prepared["event_id"] = event_id
            return prepared
        config, provider = prepared

        logo = resolve_article_logo(environ)
        if logo_rejection(logo):
            return {
                "success": False,
                "event_id": event_id,
                "reason": "logo hash mismatch",
                "image_failure_code": "logo_hash_mismatch",
                "image_request_count": 0,
                "budget_reserved": False,
            }

        model = getattr(config, "model", "fake-vertex")
        region = getattr(config, "location", "global")
        nova_on = nova_fallback_enabled(environ)

        def _run_provider(
            gen_provider: Any,
            *,
            label: str,
            model: str,
            region: str | None,
            fallback_reason: str | None = None,
        ) -> dict[str, Any]:
            # One provider attempt: brief -> generate -> validate -> brand -> freeze record.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bundle_dir = MAKE_RUNS_ROOT / f"{event_id}-{stamp}-{label}"
            raw_dir = bundle_dir / "raw"
            final_dir = bundle_dir / "final"
            raw_dir.mkdir(parents=True, exist_ok=True)
            final_dir.mkdir(parents=True, exist_ok=True)

            brief_bundle = build_story_image_brief(
                article=article,
                evidence_packet=packet,
                event_id=event_id,
            )
            story_brief = brief_bundle["story_image_brief"]
            visual = brief_bundle["visual_brief"]
            write_json_utf8(bundle_dir / "story_image_brief.json", story_brief)
            write_json_utf8(bundle_dir / "visual_brief.json", visual.as_dict())
            link = {
                "event_id": event_id,
                "canonical_article_hash": article_hash,
                "image_provider": label,
                "model": model,
                "region": region,
                "logo_path": str(logo),
            }
            if fallback_reason:
                link["fallback_from"] = "vertex"
                link["fallback_reason"] = fallback_reason
            write_json_utf8(bundle_dir / "article_link.json", link)

            request = canonical_provider_request(visual)
            if label == "nova":
                # Nova Canvas: text-to-image only, prompt <= 1024 chars, positive phrasing.
                request.prompt = build_nova_prompt(story_brief, request.prompt)
            if hasattr(gen_provider, "recorded_request"):
                write_json_utf8(
                    bundle_dir / "provider_request_metadata.json",
                    gen_provider.recorded_request(request),
                )

            raw_dest = raw_dir / "artwork.png"
            result = gen_provider.generate(request, dest_path=str(raw_dest), cold_start=True)
            if (
                label == "vertex"
                and not result.success
                and result.http_status == 429
                and result.provider_reported_cost_usd is None
            ):
                # A 429 is rejected before generation; Vertex does not bill it.
                result.provider_reported_cost_usd = 0.0
                result.estimated_list_price_note = "HTTP 429 rejected request; not billed"
            write_json_utf8(bundle_dir / "provider_result.json", redact_secrets(result.as_dict()))
            usage = result.provider_reported_usage if isinstance(result.provider_reported_usage, dict) else {}
            diagnostic = usage.get("_safe_response_diagnostic")
            if isinstance(diagnostic, dict):
                write_json_utf8(
                    bundle_dir / "provider_response_diagnostic.json",
                    redact_secrets(diagnostic),
                )

            calls = int(getattr(gen_provider, "generation_calls", 1) or 1)
            assert calls <= HARD_MAX_GENERATION_CALLS
            extra_out: dict[str, Any] = {}
            if fallback_reason:
                extra_out = {"fallback_from": "vertex", "fallback_reason": fallback_reason}

            if not result.success or not result.raw_image_path:
                return {
                    "success": False,
                    "event_id": event_id,
                    "reason": str(result.failure_reason or "generation_failed"),
                    "image_failure_code": str(result.failure_reason or "generation_failed"),
                    "image_request_count": calls,
                    "http_status": result.http_status,
                    "latency_ms": result.total_latency_ms,
                    "safe_diagnostic": diagnostic,
                    "bundle_dir": str(bundle_dir),
                    "provider": label,
                    "model": model,
                    "region": region,
                    **extra_out,
                }

            raw_path = Path(result.raw_image_path)
            try:
                validation = validate_raw_artwork(raw_path)
            except Exception as exc:  # noqa: BLE001
                return {
                    "success": False,
                    "event_id": event_id,
                    "reason": str(getattr(exc, "message", exc)),
                    "image_failure_code": str(getattr(exc, "code", "raw_validation_failed")),
                    "image_request_count": calls,
                    "http_status": result.http_status,
                    "latency_ms": result.total_latency_ms,
                    "bundle_dir": str(bundle_dir),
                }
            write_json_utf8(bundle_dir / "image_validation.json", validation)
            if not validation.get("passed"):
                return {
                    "success": False,
                    "event_id": event_id,
                    "reason": "raw_validation_failed",
                    "image_failure_code": "raw_validation_failed",
                    "image_request_count": calls,
                    "bundle_dir": str(bundle_dir),
                }

            card_raw = raw_dir / "artwork_1280x720.png"
            _resize_to_card(raw_path, card_raw)
            branded_path = final_dir / "branded.png"
            composition = compose_card(
                card_raw,
                branded_path,
                CompositionSpec(
                    headline="",
                    category_label=None,
                    logo_path=logo,
                    test_mode=False,
                    logo_only=True,
                    output_name="branded.png",
                ),
            )
            write_json_utf8(bundle_dir / "composition.json", composition)
            if not (composition.get("compositor_validation") or {}).get("passed"):
                return {
                    "success": False,
                    "event_id": event_id,
                    "reason": "compositor failed",
                    "image_failure_code": "branding_failed",
                    "image_request_count": calls,
                    "bundle_dir": str(bundle_dir),
                }

            raw_hash = file_sha256(raw_path)
            branded_hash = file_sha256(branded_path)
            write_json_utf8(
                bundle_dir / "image_freeze.json",
                {
                    "event_id": event_id,
                    "canonical_article_hash": article_hash,
                    "raw_image_path": str(raw_path),
                    "raw_image_hash": raw_hash,
                    "branded_image_path": str(branded_path),
                    "branded_image_hash": branded_hash,
                    "logo_hash_verified": True,
                    "provider": label,
                    "model": model,
                    "region": region,
                    "image_request_count": calls,
                    "frozen": True,
                    **extra_out,
                },
            )
            return {
                "success": True,
                "event_id": event_id,
                "final_path": str(branded_path),
                "raw_image_path": str(raw_path),
                "raw_image_hash": raw_hash,
                "branded_image_hash": branded_hash,
                "image_request_count": calls,
                "http_status": result.http_status,
                "latency_ms": result.total_latency_ms,
                "provider": label,
                "model": model,
                "region": region,
                "logo_hash_verified": True,
                "bundle_dir": str(bundle_dir),
                "story_image_brief_path": str(bundle_dir / "story_image_brief.json"),
                "provider_reported_cost": result.provider_reported_cost,
                "provider_reported_cost_usd": result.provider_reported_cost_usd,
                "provider_reported_usage": result.provider_reported_usage,
                **extra_out,
            }

        def _nova_fallback(reason: str) -> dict[str, Any]:
            """Exactly one Nova attempt. Never reserves or counts Vertex budget."""
            logger.warning("image_fallback vertex->nova reason=%s event=%s", reason, event_id)
            try:
                nova_config = load_nova_proxy_config(environ)
            except NovaConfigError as exc:
                logger.warning("image_fallback nova not configured event=%s: %s", event_id, exc)
                return {
                    "success": False,
                    "event_id": event_id,
                    "reason": str(exc),
                    "image_failure_code": "nova_config_incomplete",
                    "image_request_count": 0,
                    "provider": "nova",
                    "fallback_from": "vertex",
                    "fallback_reason": reason,
                }
            nova = (
                nova_provider_factory(nova_config)
                if nova_provider_factory is not None
                else NovaProxyImageProvider(nova_config)
            )
            out = _run_provider(
                nova,
                label="nova",
                model=NOVA_BEDROCK_MODEL_ID,
                region=None,
                fallback_reason=reason,
            )
            logger.warning(
                "image_fallback nova result=%s event=%s latency_ms=%s code=%s",
                "ok" if out.get("success") else "failed",
                event_id,
                out.get("latency_ms"),
                out.get("image_failure_code"),
            )
            return out

        def _combined_failure(vertex_code: str, nova_out: dict[str, Any]) -> dict[str, Any]:
            nova_code = str(nova_out.get("image_failure_code") or nova_out.get("reason") or "nova_failed")
            out = dict(nova_out)
            out["vertex_failure_code"] = vertex_code
            out["nova_failure_code"] = nova_code
            out["image_failure_code"] = f"{vertex_code}; nova fallback failed: {nova_code}"
            out["reason"] = out["image_failure_code"]
            return out

        def _do_generate() -> dict[str, Any]:
            # Runs ONLY after budget reservation inside guarded_vertex_generate.
            out = _run_provider(provider, label="vertex", model=model, region=region)
            if out.get("success") or not nova_on or not is_vertex_quota_failure(out):
                return out
            vertex_calls = int(out.get("image_request_count") or 1)
            nova_out = _nova_fallback(FALLBACK_VERTEX_RATE_LIMITED)
            nova_out["vertex_request_count"] = vertex_calls
            nova_out["nova_request_count"] = int(nova_out.get("image_request_count") or 0)
            nova_out["image_request_count"] = vertex_calls + nova_out["nova_request_count"]
            if not nova_out.get("success"):
                return _combined_failure(str(out.get("image_failure_code")), nova_out)
            return nova_out

        # ------------------------------------------------------------------
        # 4–5: reserve budget, then provider.generate (inside generate_fn)
        # ------------------------------------------------------------------
        guarded = guarded_vertex_generate(
            ledger=ledger,
            event_id=event_id,
            article_hash=article_hash,
            generate_fn=_do_generate,
        )
        if (
            guarded.get("success")
            or not nova_on
            or guarded.get("image_failure_code") != CODE_DAILY_EXHAUSTED
        ):
            return guarded

        # Daily Vertex cap reached: skip Vertex entirely, one Nova call, no budget reservation.
        nova_out = _nova_fallback(FALLBACK_DAILY_BUDGET)
        nova_out.setdefault("event_id", event_id)
        nova_out["canonical_article_hash"] = article_hash
        nova_out["safe_telemetry"] = guarded.get("safe_telemetry")
        nova_out["nova_request_count"] = int(nova_out.get("image_request_count") or 0)
        if not nova_out.get("success"):
            return _combined_failure(CODE_DAILY_EXHAUSTED, nova_out)
        branded = Path(str(nova_out.get("final_path") or ""))
        if branded.is_file():
            ledger.register_frozen_image(
                event_id=event_id,
                article_hash=article_hash,
                branded_image_path=str(branded),
                branded_image_hash=str(nova_out.get("branded_image_hash") or file_sha256(branded)),
                raw_image_path=nova_out.get("raw_image_path"),
                raw_image_hash=nova_out.get("raw_image_hash"),
                extra={"provider": "nova", "model": nova_out.get("model")},
            )
        return nova_out

    image_fn.vertex_budget_ledger = ledger  # type: ignore[attr-defined]
    return image_fn
