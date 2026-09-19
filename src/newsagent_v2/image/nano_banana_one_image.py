"""
V4 Nano Banana one-image controlled test on a frozen Top-5 article.

Article generation is frozen. This module never calls Kimi, never regenerates
articles, never sends Telegram, and never publishes WordPress.

MAX NANO BANANA GENERATION CALLS = 1.
"""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PIL import Image

from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.image.artifacts import DEFAULT_RUNS_ROOT, new_run_id, prepare_run_dir, raw_dir, final_dir
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

REPO_ROOT = Path(__file__).resolve().parents[3]
LOGO_PATH = REPO_ROOT / "brand" / "coinnetwork_logo.png"
EXPECTED_LOGO_SHA256 = "09c8106c0458e5225530ac6736a56944c81fbde6d7b81ed5b6ce69586e516818"
DEFAULT_APPROVAL_STORY = (
    REPO_ROOT
    / "output"
    / "approval"
    / "v4-top5-articles-20260917T082903Z"
    / "stories"
    / "event-013.json"
)
DEFAULT_EVIDENCE = (
    REPO_ROOT
    / "output"
    / "make_runs"
    / "v4-top5-articles-20260917T082903Z"
    / "attempts"
    / "event-013"
    / "evidence_packet.json"
)
TEST_RUNS_ROOT = REPO_ROOT / "output" / "capability_tests" / "nano_banana_one_image"


def _load_environ() -> dict[str, str]:
    load_dotenv(REPO_ROOT / ".env")
    environ = {key: str(value) for key, value in os.environ.items()}
    if os.name != "nt":
        return environ
    try:
        import winreg
    except ImportError:
        return environ
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            wanted = (
                "NEWSAGENT_V2_VERTEX_PROJECT",
                "NEWSAGENT_V2_VERTEX_LOCATION",
                "NEWSAGENT_V2_VERTEX_MODEL",
                "NEWSAGENT_V2_NANO_BANANA_MODEL",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GOOGLE_CLOUD_PROJECT",
                "GOOGLE_CLOUD_LOCATION",
            )
            for name in wanted:
                if str(environ.get(name, "")).strip():
                    continue
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if value:
                    environ[name] = str(value)
    except OSError:
        return environ
    return environ


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_article_hash(article: dict[str, Any]) -> str:
    body = str(article.get("article_body") or "")
    return _sha256_text(body)


def _resize_to_card(src: Path, dest: Path) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as image:
        image.load()
        rgb = image.convert("RGB")
        if rgb.size != (CARD_WIDTH, CARD_HEIGHT):
            rgb = rgb.resize((CARD_WIDTH, CARD_HEIGHT), Image.Resampling.LANCZOS)
        rgb.save(dest, format="PNG")
    return {
        "source": str(src),
        "dest": str(dest),
        "width": CARD_WIDTH,
        "height": CARD_HEIGHT,
        "resized": True,
    }


def _fail_report(**fields: Any) -> dict[str, Any]:
    base = {
        "status": "FAIL",
        "FINAL": "FAIL",
        "telegram_sent": False,
        "wordpress_published": False,
        "kimo_calls": 0,
        "Kimi_calls": 0,
        "image_calls": 0,
    }
    base.update(fields)
    return base


def run_nano_banana_one_image(
    *,
    environ: dict[str, str] | None = None,
    approval_story_path: Path | None = None,
    evidence_path: Path | None = None,
    persist_root: Path | None = None,
    allow_live: bool = True,
) -> dict[str, Any]:
    """
    Controlled one-image Vertex Nano Banana test on frozen event-013.

    If Vertex configuration is incomplete, STOP without making a provider call.
    """
    env = environ if environ is not None else _load_environ()
    story_path = approval_story_path or DEFAULT_APPROVAL_STORY
    evidence_file = evidence_path or DEFAULT_EVIDENCE
    root = persist_root or TEST_RUNS_ROOT
    root.mkdir(parents=True, exist_ok=True)

    if not story_path.is_file():
        report = _fail_report(
            failure_stage="load_frozen_article",
            failure_code="missing_approval_story",
            failure_detail=f"frozen approval story not found: {story_path}",
            event="event-013",
            headline=None,
            article_hash_before=None,
            article_hash_after=None,
            article_preserved=False,
            provider="vertex",
            model=None,
            region=None,
        )
        write_json_utf8(root / "report.json", report)
        return report

    story = json.loads(story_path.read_text(encoding="utf-8"))
    article = story.get("article") if isinstance(story.get("article"), dict) else {}
    event_id = str(story.get("event_id") or article.get("event_id") or "event-013")
    headline = str(article.get("headline") or story.get("headline") or "")
    article_before = deepcopy(article)
    article_hash_before = str(story.get("canonical_body_hash") or _canonical_article_hash(article))
    # Prefer recomputed hash for immutability proof.
    recomputed_before = _canonical_article_hash(article)
    if story.get("canonical_body_hash") and story["canonical_body_hash"] != recomputed_before:
        report = _fail_report(
            failure_stage="load_frozen_article",
            failure_code="canonical_hash_mismatch_on_load",
            failure_detail="approval canonical_body_hash does not match recomputed body hash",
            event=event_id,
            headline=headline,
            article_hash_before=recomputed_before,
            article_hash_after=None,
            article_preserved=False,
            provider="vertex",
            stored_canonical_body_hash=story.get("canonical_body_hash"),
        )
        write_json_utf8(root / "report.json", report)
        return report
    article_hash_before = recomputed_before

    packet = None
    if evidence_file.is_file():
        packet = json.loads(evidence_file.read_text(encoding="utf-8"))

    brief_bundle = build_story_image_brief(
        article=article,
        evidence_packet=packet if isinstance(packet, dict) else None,
        event_id=event_id,
    )
    story_brief = brief_bundle["story_image_brief"]
    visual = brief_bundle["visual_brief"]

    run_id = new_run_id()
    run_base = prepare_run_dir(root / run_id)
    write_json_utf8(run_base / "story_image_brief.json", story_brief)
    write_json_utf8(run_base / "visual_brief.json", visual.as_dict())
    write_json_utf8(
        run_base / "article_immutability_before.json",
        {
            "event_id": event_id,
            "article_hash_before": article_hash_before,
            "approval_story_path": str(story_path),
            "canonical_body_words": story.get("canonical_body_words"),
        },
    )

    config_status = resolve_vertex_config_status(env)
    write_json_utf8(run_base / "vertex_config_status.json", config_status)

    if not config_status.get("ready"):
        article_hash_after = _canonical_article_hash(article_before)
        report = _fail_report(
            failure_stage="configuration",
            failure_code="vertex_config_incomplete",
            failure_detail=(
                "Required NON-SECRET Vertex fields missing or auth unavailable. "
                "No image provider call was made."
            ),
            missing_non_secret_fields=config_status.get("missing_fields"),
            event=event_id,
            headline=headline,
            article_hash_before=article_hash_before,
            article_hash_after=article_hash_after,
            article_preserved=article_hash_before == article_hash_after,
            provider="vertex",
            model=config_status.get("model"),
            region=config_status.get("region"),
            project_configured=config_status.get("project_configured"),
            brief_subject=story_brief.get("primary_subject"),
            brief_event=story_brief.get("event"),
            image_calls=0,
            raw_validation=None,
            logo_hash_verified=None,
            branding=None,
            generation_time_seconds=None,
            provider_cost_available=False,
            provider_cost=None,
            cost_verified=False,
            raw_image_path=None,
            branded_image_path=None,
            telemetry_path=str(run_base / "image_telemetry.json"),
            note=(
                "NEWSAGENT_V2_GEMINI_API_KEY may be present for the separate google_gemini "
                "Nano Banana path; this controlled test requires Vertex (provider=vertex) "
                "and does not fall back to the Generative Language API."
            ),
        )
        write_json_utf8(
            run_base / "image_telemetry.json",
            {
                "schema_version": "image-telemetry-v1",
                "run_id": run_id,
                "event_id": event_id,
                "canonical_article_hash": article_hash_before,
                "provider": "vertex",
                "image_provider_calls": 0,
                "Kimi_calls": 0,
                "success": False,
                "failure_stage": "configuration",
                "config_status": config_status,
            },
        )
        write_json_utf8(run_base / "report.json", report)
        write_json_utf8(root / "report.json", report)
        return report

    if not allow_live:
        report = _fail_report(
            failure_stage="authorization",
            failure_code="live_not_authorized",
            failure_detail="allow_live=false; refusing Vertex network call",
            event=event_id,
            headline=headline,
            article_hash_before=article_hash_before,
            article_hash_after=article_hash_before,
            article_preserved=True,
            provider="vertex",
            model=config_status.get("model"),
            region=config_status.get("region"),
        )
        write_json_utf8(root / "report.json", report)
        return report

    logo_hash = file_sha256(LOGO_PATH) if LOGO_PATH.is_file() else None
    logo_ok = logo_hash == EXPECTED_LOGO_SHA256
    if not logo_ok:
        report = _fail_report(
            failure_stage="logo_verification",
            failure_code="logo_hash_mismatch",
            failure_detail="approved CoinNetwork logo hash mismatch; STOP without generation",
            event=event_id,
            headline=headline,
            article_hash_before=article_hash_before,
            article_hash_after=article_hash_before,
            article_preserved=True,
            provider="vertex",
            model=config_status.get("model"),
            region=config_status.get("region"),
            logo_hash_verified=False,
            expected_logo_sha256=EXPECTED_LOGO_SHA256,
            observed_logo_sha256=logo_hash,
        )
        write_json_utf8(root / "report.json", report)
        return report

    try:
        config = load_vertex_nano_banana_config(env)
    except VertexConfigError as exc:
        report = _fail_report(
            failure_stage="configuration",
            failure_code="vertex_config_error",
            failure_detail=str(exc),
            event=event_id,
            headline=headline,
            article_hash_before=article_hash_before,
            article_hash_after=article_hash_before,
            article_preserved=True,
            provider="vertex",
            model=config_status.get("model"),
            region=config_status.get("region"),
        )
        write_json_utf8(root / "report.json", report)
        return report

    provider = VertexNanoBananaImageProvider(config)
    request = canonical_provider_request(visual)
    recorded = provider.recorded_request(request)
    write_json_utf8(run_base / "provider_request_metadata.json", recorded)

    raw_dest = raw_dir(run_base) / "artwork.png"
    started = datetime.now(timezone.utc)
    result = provider.generate(request, dest_path=str(raw_dest), cold_start=True)
    ended = datetime.now(timezone.utc)
    generation_seconds = round((ended - started).total_seconds(), 3)

    # Enforce one-call cap observability
    assert provider.generation_calls <= HARD_MAX_GENERATION_CALLS

    write_json_utf8(run_base / "provider_result.json", redact_secrets(result.as_dict()))
    usage = result.provider_reported_usage if isinstance(result.provider_reported_usage, dict) else {}
    diagnostic = usage.get("_safe_response_diagnostic")
    if isinstance(diagnostic, dict):
        write_json_utf8(run_base / "provider_response_diagnostic.json", redact_secrets(diagnostic))

    if not result.success or not result.raw_image_path:
        article_hash_after = _canonical_article_hash(article_before)
        report = _fail_report(
            failure_stage="provider_generation",
            failure_code=str(result.failure_reason or "generation_failed"),
            failure_detail=str(result.failure_reason or "no raw image"),
            event=event_id,
            headline=headline,
            article_hash_before=article_hash_before,
            article_hash_after=article_hash_after,
            article_preserved=article_hash_before == article_hash_after,
            provider="vertex",
            model=config.model,
            region=config.location,
            project_configured=True,
            image_calls=provider.generation_calls,
            brief_subject=story_brief.get("primary_subject"),
            brief_event=story_brief.get("event"),
            generation_time_seconds=generation_seconds,
            provider_cost_available=result.provider_reported_cost is not None,
            provider_cost=result.provider_reported_cost,
            cost_verified=False,
            logo_hash_verified=True,
            raw_image_path=result.raw_image_path,
            branded_image_path=None,
            telemetry_path=str(run_base / "image_telemetry.json"),
            http_status=result.http_status,
        )
        write_json_utf8(
            run_base / "image_telemetry.json",
            {
                "run_id": run_id,
                "event_id": event_id,
                "canonical_article_hash": article_hash_before,
                "provider": "vertex",
                "model": config.model,
                "region": config.location,
                "image_provider_calls": provider.generation_calls,
                "Kimi_calls": 0,
                "generation_time_seconds": generation_seconds,
                "success": False,
                "failure_reason": result.failure_reason,
                "provider_reported_cost": result.provider_reported_cost,
                "provider_reported_cost_available": result.provider_reported_cost is not None,
                "cost_verified": False,
            },
        )
        write_json_utf8(run_base / "report.json", report)
        write_json_utf8(root / "report.json", report)
        return report

    raw_path = Path(result.raw_image_path)
    try:
        validation = validate_raw_artwork(raw_path)
    except Exception as exc:
        article_hash_after = _canonical_article_hash(article_before)
        report = _fail_report(
            failure_stage="raw_validation",
            failure_code=getattr(exc, "code", "validation_error"),
            failure_detail=str(getattr(exc, "message", exc)),
            event=event_id,
            headline=headline,
            article_hash_before=article_hash_before,
            article_hash_after=article_hash_after,
            article_preserved=article_hash_before == article_hash_after,
            provider="vertex",
            model=config.model,
            region=config.location,
            image_calls=provider.generation_calls,
            generation_time_seconds=generation_seconds,
            logo_hash_verified=True,
            raw_image_path=str(raw_path),
            raw_validation="FAIL",
        )
        write_json_utf8(run_base / "image_validation.json", {"passed": False, "error": str(exc)})
        write_json_utf8(root / "report.json", report)
        return report

    write_json_utf8(run_base / "image_validation.json", validation)

    # Deterministic resize to card size before branding.
    card_raw = raw_dir(run_base) / "artwork_1280x720.png"
    resize_meta = _resize_to_card(raw_path, card_raw)
    write_json_utf8(run_base / "resize_meta.json", resize_meta)

    branded_path = final_dir(run_base) / "branded.png"
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
    write_json_utf8(run_base / "composition.json", composition)
    branding_passed = bool((composition.get("compositor_validation") or {}).get("passed"))

    article_hash_after = _canonical_article_hash(article_before)
    preserved = article_hash_before == article_hash_after
    write_json_utf8(
        run_base / "article_immutability_after.json",
        {
            "article_hash_before": article_hash_before,
            "article_hash_after": article_hash_after,
            "article_preserved": preserved,
            "Kimi_calls": 0,
        },
    )

    final_dims = None
    if branded_path.is_file():
        with Image.open(branded_path) as img:
            final_dims = f"{img.size[0]}x{img.size[1]}"

    telemetry = {
        "schema_version": "image-telemetry-v1",
        "run_id": run_id,
        "event_id": event_id,
        "canonical_article_hash": article_hash_before,
        "provider": "vertex",
        "model": config.model,
        "region": config.location,
        "image_provider_calls": provider.generation_calls,
        "Kimi_calls": 0,
        "generation_time_seconds": generation_seconds,
        "provider_reported_cost": result.provider_reported_cost,
        "provider_reported_cost_available": result.provider_reported_cost is not None,
        "cost_verified": False,
        "raw_validation_passed": bool(validation.get("passed")),
        "logo_hash_verified": True,
        "branding_passed": branding_passed,
        "telegram_sent": False,
        "wordpress_published": False,
    }
    write_json_utf8(run_base / "image_telemetry.json", telemetry)

    passed = (
        preserved
        and provider.generation_calls == 1
        and bool(validation.get("passed"))
        and logo_ok
        and branding_passed
        and branded_path.is_file()
    )

    report = {
        "status": "PASS" if passed else "FAIL",
        "FINAL": "PASS" if passed else "FAIL",
        "event": event_id,
        "headline": headline,
        "article_hash_before": article_hash_before,
        "article_hash_after": article_hash_after,
        "article_preserved": preserved,
        "provider": "vertex",
        "model": config.model,
        "region": config.location,
        "project_configured": True,
        "image_calls": provider.generation_calls,
        "kimo_calls": 0,
        "Kimi_calls": 0,
        "brief_subject": story_brief.get("primary_subject"),
        "brief_event": story_brief.get("event"),
        "raw_image_dimensions": f"{validation.get('width')}x{validation.get('height')}",
        "final_image_dimensions": final_dims,
        "aspect_ratio": validation.get("aspect_ratio"),
        "raw_validation": "PASS" if validation.get("passed") else "FAIL",
        "logo_hash_verified": True,
        "branding": "PASS" if branding_passed else "FAIL",
        "generation_time_seconds": generation_seconds,
        "provider_cost_available": result.provider_reported_cost is not None,
        "provider_cost": result.provider_reported_cost,
        "cost_verified": False,
        "raw_image_path": str(raw_path),
        "branded_image_path": str(branded_path) if branded_path.is_file() else None,
        "telemetry_path": str(run_base / "image_telemetry.json"),
        "telegram_sent": False,
        "wordpress_published": False,
        "run_id": run_id,
        "run_dir": str(run_base),
    }
    if not passed:
        report["failure_stage"] = (
            "article_immutability"
            if not preserved
            else "branding"
            if not branding_passed
            else "unknown"
        )
        report["failure_code"] = "success_criteria_not_met"
        report["failure_detail"] = "one or more success criteria failed after generation"
    write_json_utf8(run_base / "report.json", report)
    write_json_utf8(root / "report.json", report)
    return report


def main() -> None:
    report = run_nano_banana_one_image(allow_live=True)
    # Public summary only — no secrets.
    public = {
        k: report.get(k)
        for k in (
            "FINAL",
            "event",
            "headline",
            "article_preserved",
            "provider",
            "model",
            "region",
            "image_calls",
            "Kimi_calls",
            "failure_stage",
            "failure_code",
            "missing_non_secret_fields",
            "raw_image_path",
            "branded_image_path",
            "telemetry_path",
        )
        if k in report or report.get(k) is not None
    }
    print(json.dumps(public, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
