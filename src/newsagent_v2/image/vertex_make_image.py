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
  Nova prompt is brand/name-free (story_scene tier). If Nova's content filter blocks it,
  one more call with a very safe generic prompt (category_generic tier). Max 2 Nova calls.
  Vertex safety blocks, prompt errors and config/auth failures never fall back.
  Nova calls do not touch the Vertex budget ledger counters.

Backup Vertex (NEWSAGENT_V2_VERTEX_BACKUP_ENABLED, default off; see vertex_backup.py):
  a second service account/project tried BEFORE Nova on the same two triggers
  (primary daily cap exhausted, or primary 429/RESOURCE_EXHAUSTED/quota). It has its
  own daily cap (NEWSAGENT_V2_MAX_VERTEX_BACKUP_CALLS_PER_DAY, default 25) and never
  counts against the primary ledger. Chain: vertex -> vertex_backup -> nova.
  A backup safety block does not fall through to Nova (same rule as the primary).
  Run records carry image_provider/provider = vertex | vertex_backup | nova and
  vertex_cred = primary | backup on Vertex attempts.
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
    PROMPT_TIER_GENERIC,
    PROMPT_TIER_STORY,
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
from newsagent_v2.image.vertex_backup import (
    BACKUP_LABEL,
    CODE_BACKUP_CONFIG,
    CODE_BACKUP_DAILY_EXHAUSTED,
    VertexBackupConfigError,
    VertexBackupDailyBudget,
    backup_access_token_provider,
    load_vertex_backup_config,
    vertex_backup_enabled,
)
from newsagent_v2.image.validate import file_sha256, validate_raw_artwork
from newsagent_v2.image.image_proxy import Gx10ImageProxy, image_via_proxy
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
    code = str(result.get("image_failure_code") or "")
    if code == "vertex_billing_disabled" or result.get("http_status") == 402:
        return True
    if code != "vertex_rate_limited":
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
    backup_provider_factory: Callable[..., Any] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """
    Production image_fn: config-ready → guards/reserve → StoryImageBrief → Vertex once → brand.

    provider_factory: optional injectable factory for offline tests
      (config) -> provider with .generate(...) and .generation_calls / .recorded_request
    nova_provider_factory: optional injectable factory for the Nova fallback in tests
      (NovaProxyConfig) -> provider
    backup_provider_factory: optional injectable factory for the backup Vertex in tests
      (VertexNanoBananaConfig) -> provider
    """

    ledger = VertexBudgetLedger(
        root=budget_root,
        environ=environ,
        batch_id=batch_id or "make-batch",
    )
    backup_budget = VertexBackupDailyBudget(root=budget_root, environ=environ)

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
        backup_on = vertex_backup_enabled(environ)

        def _run_provider(
            gen_provider: Any,
            *,
            label: str,
            model: str,
            region: str | None,
            fallback_reason: str | None = None,
            prompt_tier: str | None = None,
        ) -> dict[str, Any]:
            # One provider attempt: brief -> generate -> validate -> brand -> freeze record.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            tag = f"{label}-{prompt_tier}" if prompt_tier else label
            bundle_dir = MAKE_RUNS_ROOT / f"{event_id}-{stamp}-{tag}"
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
            if prompt_tier:
                link["nova_prompt_tier"] = prompt_tier
            vertex_cred = {"vertex": "primary", BACKUP_LABEL: "backup"}.get(label)
            if vertex_cred:
                link["vertex_cred"] = vertex_cred
            write_json_utf8(bundle_dir / "article_link.json", link)

            request = canonical_provider_request(visual)
            if label == "nova":
                # Nova Canvas: text-to-image only, <= 1024 chars, no names/brands/tickers/quotes.
                request.prompt = build_nova_prompt(
                    story_brief,
                    "",
                    category=str(article.get("category") or getattr(visual, "story_category", "") or ""),
                    tier=prompt_tier or PROMPT_TIER_STORY,
                )
            if hasattr(gen_provider, "recorded_request"):
                write_json_utf8(
                    bundle_dir / "provider_request_metadata.json",
                    gen_provider.recorded_request(request),
                )

            raw_dest = raw_dir / "artwork.png"
            result = gen_provider.generate(request, dest_path=str(raw_dest), cold_start=True)
            if (
                label in ("vertex", BACKUP_LABEL)
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
            if prompt_tier:
                extra_out["nova_prompt_tier"] = prompt_tier
            if vertex_cred:
                extra_out["vertex_cred"] = vertex_cred

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
                    "provider": label,
                    **extra_out,
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

        def _nova_fallback(reason: str, *, source: str = "vertex") -> dict[str, Any]:
            """Nova attempts never reserve or count Vertex budget.

            Tier 1 (story_scene) once; only on a Nova content-filter block, tier 2
            (category_generic) once. Max 2 Nova calls per image, no other retries.
            """
            logger.warning("image_fallback %s->nova reason=%s event=%s", source, reason, event_id)
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
            attempts: list[dict[str, Any]] = []
            out: dict[str, Any] = {}
            for tier in (PROMPT_TIER_STORY, PROMPT_TIER_GENERIC):
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
                    prompt_tier=tier,
                )
                code = out.get("image_failure_code")
                attempts.append({"tier": tier, "success": bool(out.get("success")), "code": code,
                                 "bundle_dir": out.get("bundle_dir")})
                logger.warning(
                    "image_fallback nova attempt=%s tier=%s result=%s event=%s latency_ms=%s code=%s",
                    len(attempts),
                    tier,
                    "ok" if out.get("success") else "failed",
                    event_id,
                    out.get("latency_ms"),
                    code,
                )
                if out.get("success") or code != "nova_safety_block":
                    break
            out["nova_attempts"] = attempts
            out["image_request_count"] = len(attempts)
            if not out.get("success") and len(attempts) > 1:
                out["image_failure_code"] = (
                    f"{out.get('image_failure_code')} ({', '.join(a['tier'] for a in attempts)})"
                )
            return out

        def _combined_failure(
            vertex_code: str, nova_out: dict[str, Any], backup_code: str | None = None
        ) -> dict[str, Any]:
            nova_code = str(nova_out.get("image_failure_code") or nova_out.get("reason") or "nova_failed")
            nova_out = dict(nova_out)
            nova_out.setdefault("nova_failure_code", nova_code.split(" ", 1)[0])
            out = dict(nova_out)
            out["vertex_failure_code"] = vertex_code
            if backup_code:
                out["vertex_backup_failure_code"] = backup_code
                out["image_failure_code"] = (
                    f"{vertex_code}; vertex backup failed: {backup_code}; nova fallback failed: {nova_code}"
                )
            else:
                out["image_failure_code"] = f"{vertex_code}; nova fallback failed: {nova_code}"
            out["reason"] = out["image_failure_code"]
            return out

        def _vertex_backup(reason: str) -> dict[str, Any] | None:
            """One call on the backup service account. None when the backup is disabled."""
            if not backup_on:
                return None
            base = {"success": False, "event_id": event_id, "image_request_count": 0,
                    "provider": BACKUP_LABEL, "vertex_cred": "backup",
                    "fallback_from": "vertex", "fallback_reason": reason}
            try:
                bconfig, bcred = load_vertex_backup_config(environ)
            except VertexBackupConfigError as exc:
                logger.warning("image_fallback vertex_backup not configured event=%s: %s", event_id, exc)
                return {**base, "reason": str(exc), "image_failure_code": CODE_BACKUP_CONFIG}
            allowed, used = backup_budget.try_reserve(event_id=event_id, article_hash=article_hash, reason=reason)
            if not allowed:
                logger.warning("image_fallback vertex_backup daily cap reached (%s/%s) event=%s",
                               used, backup_budget.limit(), event_id)
                return {**base, "reason": "daily Vertex backup ceiling reached",
                        "image_failure_code": CODE_BACKUP_DAILY_EXHAUSTED}
            logger.warning("image_fallback vertex->vertex_backup reason=%s event=%s backup_calls_today=%s/%s",
                           reason, event_id, used, backup_budget.limit())
            bprovider = (
                backup_provider_factory(bconfig)
                if backup_provider_factory is not None
                else VertexNanoBananaImageProvider(
                    bconfig, access_token_provider=backup_access_token_provider(bcred)
                )
            )
            out = _run_provider(bprovider, label=BACKUP_LABEL, model=bconfig.model,
                                region=bconfig.location, fallback_reason=reason)
            out["vertex_backup_calls_today"] = used
            logger.warning("image_fallback vertex_backup result=%s event=%s latency_ms=%s code=%s",
                           "ok" if out.get("success") else "failed", event_id,
                           out.get("latency_ms"), out.get("image_failure_code"))
            return out

        def _fallback_chain(reason: str, vertex_code: str, vertex_calls: int,
                            primary_out: dict[str, Any] | None) -> dict[str, Any]:
            """Primary is out (cap or quota): backup Vertex, then Nova."""
            backup_out = _vertex_backup(reason)
            backup_calls = int((backup_out or {}).get("image_request_count") or 0)
            backup_code: str | None = None
            if backup_out is not None:
                if backup_out.get("success"):
                    backup_out["vertex_request_count"] = vertex_calls
                    backup_out["vertex_backup_request_count"] = backup_calls
                    backup_out["image_request_count"] = vertex_calls + backup_calls
                    backup_out["fallback_chain"] = ["vertex", BACKUP_LABEL]
                    return backup_out
                backup_code = str(backup_out.get("image_failure_code") or backup_out.get("reason")
                                  or "vertex_backup_failed")
                if backup_code == "vertex_safety_block" or not nova_on:
                    out = dict(backup_out)
                    out["vertex_failure_code"] = vertex_code
                    out["vertex_backup_failure_code"] = backup_code
                    out["vertex_request_count"] = vertex_calls
                    out["vertex_backup_request_count"] = backup_calls
                    out["image_request_count"] = vertex_calls + backup_calls
                    out["image_failure_code"] = f"{vertex_code}; vertex backup failed: {backup_code}"
                    out["reason"] = out["image_failure_code"]
                    return out
            elif not nova_on:
                assert primary_out is not None
                return primary_out

            nova_out = _nova_fallback(reason, source=BACKUP_LABEL if backup_out is not None else "vertex")
            nova_out["nova_request_count"] = int(nova_out.get("image_request_count") or 0)
            if vertex_calls or backup_out is not None:
                nova_out["vertex_request_count"] = vertex_calls
            if backup_out is not None:
                nova_out["vertex_backup_request_count"] = backup_calls
                nova_out["vertex_backup_failure_code"] = backup_code
                nova_out["fallback_chain"] = ["vertex", BACKUP_LABEL, "nova"]
            nova_out["image_request_count"] = vertex_calls + backup_calls + nova_out["nova_request_count"]
            if not nova_out.get("success"):
                return _combined_failure(vertex_code, nova_out, backup_code)
            return nova_out


        def _do_proxy() -> dict[str, Any]:
            proxy = Gx10ImageProxy(environ)
            return _run_provider(
                proxy,
                label="image_proxy",
                model=proxy.model_name,
                region=None,
            )

        def _do_generate() -> dict[str, Any]:
            # Runs ONLY after budget reservation inside guarded_vertex_generate.
            out = _run_provider(provider, label="vertex", model=model, region=region)
            if out.get("success") or not (nova_on or backup_on) or not is_vertex_quota_failure(out):
                return out
            vertex_calls = int(out.get("image_request_count") or 1)
            return _fallback_chain(FALLBACK_VERTEX_RATE_LIMITED, str(out.get("image_failure_code")),
                                   vertex_calls, out)

        # ------------------------------------------------------------------
        # 4–5: reserve budget, then provider.generate (inside generate_fn)
        # ------------------------------------------------------------------
        if image_via_proxy(environ):
            proxy_guarded = guarded_vertex_generate(
                ledger=ledger,
                event_id=event_id,
                article_hash=article_hash,
                generate_fn=_do_proxy,
            )
            proxy_code = str(proxy_guarded.get("image_failure_code") or "")
            if proxy_guarded.get("success") or proxy_code == CODE_DAILY_EXHAUSTED:
                guarded = proxy_guarded
            else:
                logger.warning(
                    "image_proxy failed; using direct Vertex path event=%s code=%s",
                    event_id,
                    proxy_code,
                )
                guarded = guarded_vertex_generate(
                    ledger=ledger,
                    event_id=event_id,
                    article_hash=article_hash,
                    generate_fn=_do_generate,
                )
        else:
            guarded = guarded_vertex_generate(
                ledger=ledger,
                event_id=event_id,
                article_hash=article_hash,
                generate_fn=_do_generate,
            )
        if (
            guarded.get("success")
            or not (nova_on or backup_on)
            or guarded.get("image_failure_code") != CODE_DAILY_EXHAUSTED
        ):
            return guarded

        # Daily primary Vertex cap reached: skip the primary entirely. Backup Vertex (own cap),
        # then Nova (max 2 calls). Neither reserves primary budget.
        chain_out = _fallback_chain(FALLBACK_DAILY_BUDGET, CODE_DAILY_EXHAUSTED, 0, guarded)
        chain_out.setdefault("event_id", event_id)
        chain_out["canonical_article_hash"] = article_hash
        chain_out["safe_telemetry"] = guarded.get("safe_telemetry")
        if not chain_out.get("success"):
            return chain_out
        branded = Path(str(chain_out.get("final_path") or ""))
        if branded.is_file():
            ledger.register_frozen_image(
                event_id=event_id,
                article_hash=article_hash,
                branded_image_path=str(branded),
                branded_image_hash=str(chain_out.get("branded_image_hash") or file_sha256(branded)),
                raw_image_path=chain_out.get("raw_image_path"),
                raw_image_hash=chain_out.get("raw_image_hash"),
                extra={"provider": chain_out.get("provider"), "model": chain_out.get("model")},
            )
        return chain_out

    image_fn.vertex_budget_ledger = ledger  # type: ignore[attr-defined]
    return image_fn
