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
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.image.compositor import CompositionSpec, compose_card
from newsagent_v2.image.contract import CARD_HEIGHT, CARD_WIDTH
from newsagent_v2.image.provider import canonical_provider_request
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
    CODE_VERTEX_DISABLED,
    VertexBudgetLedger,
    guarded_vertex_generate,
    vertex_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
LOGO_PATH = REPO_ROOT / "brand" / "coinnetwork_logo.png"
EXPECTED_LOGO_SHA256 = "09c8106c0458e5225530ac6736a56944c81fbde6d7b81ed5b6ce69586e516818"
MAKE_RUNS_ROOT = REPO_ROOT / "output" / "make_runs"


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
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """
    Production image_fn: config-ready → guards/reserve → StoryImageBrief → Vertex once → brand.

    provider_factory: optional injectable factory for offline tests
      (config) -> provider with .generate(...) and .generation_calls / .recorded_request
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

        if not LOGO_PATH.is_file() or file_sha256(LOGO_PATH) != EXPECTED_LOGO_SHA256:
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

        def _do_generate() -> dict[str, Any]:
            # Runs ONLY after budget reservation inside guarded_vertex_generate.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bundle_dir = MAKE_RUNS_ROOT / f"{event_id}-{stamp}-vertex"
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
            write_json_utf8(
                bundle_dir / "article_link.json",
                {
                    "event_id": event_id,
                    "canonical_article_hash": article_hash,
                    "image_provider": "vertex",
                    "model": model,
                    "region": region,
                },
            )

            request = canonical_provider_request(visual)
            if hasattr(provider, "recorded_request"):
                write_json_utf8(
                    bundle_dir / "provider_request_metadata.json",
                    provider.recorded_request(request),
                )

            raw_dest = raw_dir / "artwork.png"
            result = provider.generate(request, dest_path=str(raw_dest), cold_start=True)
            write_json_utf8(bundle_dir / "provider_result.json", redact_secrets(result.as_dict()))
            usage = result.provider_reported_usage if isinstance(result.provider_reported_usage, dict) else {}
            diagnostic = usage.get("_safe_response_diagnostic")
            if isinstance(diagnostic, dict):
                write_json_utf8(
                    bundle_dir / "provider_response_diagnostic.json",
                    redact_secrets(diagnostic),
                )

            calls = int(getattr(provider, "generation_calls", 1) or 1)
            assert calls <= HARD_MAX_GENERATION_CALLS

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
                    "provider": "vertex",
                    "model": model,
                    "region": region,
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
                    logo_path=LOGO_PATH,
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
                    "provider": "vertex",
                    "model": model,
                    "region": region,
                    "image_request_count": calls,
                    "frozen": True,
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
                "provider": "vertex",
                "model": model,
                "region": region,
                "logo_hash_verified": True,
                "bundle_dir": str(bundle_dir),
                "story_image_brief_path": str(bundle_dir / "story_image_brief.json"),
                "provider_reported_cost": result.provider_reported_cost,
                "provider_reported_usage": result.provider_reported_usage,
            }

        # ------------------------------------------------------------------
        # 4–5: reserve budget, then provider.generate (inside generate_fn)
        # ------------------------------------------------------------------
        return guarded_vertex_generate(
            ledger=ledger,
            event_id=event_id,
            article_hash=article_hash,
            generate_fn=_do_generate,
        )

    image_fn.vertex_budget_ledger = ledger  # type: ignore[attr-defined]
    return image_fn
