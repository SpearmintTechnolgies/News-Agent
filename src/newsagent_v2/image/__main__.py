"""Opt-in Cloudflare live image generation. Default: no network."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from newsagent_v2.image.benchmark.fixture import load_canonical_brief
from newsagent_v2.image.benchmark.runner import run_provider_benchmark
from newsagent_v2.image.provider import canonical_provider_request
from newsagent_v2.image.providers.cloudflare import (
    ACCOUNT_ENV,
    DEFAULT_MODEL,
    TOKEN_ENV,
    CloudflareConfigError,
    CloudflareImageProvider,
    load_cloudflare_config,
    mask_token,
)
from newsagent_v2.image.providers.gemini import (
    DEFAULT_MODEL as GEMINI_MODEL,
    KEY_ENV as GEMINI_KEY_ENV,
    GeminiConfigError,
    GeminiImageProvider,
    load_gemini_config,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


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
            for name in (ACCOUNT_ENV, TOKEN_ENV, GEMINI_KEY_ENV):
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


def _public_result(run: dict) -> dict:
    result = run.get("result") or {}
    telemetry = run.get("telemetry") or {}
    validation = run.get("validation") or {}
    return {
        "run_id": run.get("run_id"),
        "run_dir": run.get("run_dir"),
        "provider": result.get("provider_name"),
        "model": result.get("model_name"),
        "success": result.get("success"),
        "http_status": result.get("http_status"),
        "retry_count": result.get("retry_count"),
        "request_latency_ms": result.get("request_latency_ms"),
        "generation_time_ms": result.get("generation_time_ms"),
        "total_latency_ms": result.get("total_latency_ms"),
        "requested_width": result.get("requested_width"),
        "requested_height": result.get("requested_height"),
        "width": result.get("width"),
        "height": result.get("height"),
        "raw_image_path": result.get("raw_image_path"),
        "size_bytes": result.get("size_bytes"),
        "sha256": result.get("sha256"),
        "cloudflare_request_id": result.get("cloudflare_request_id") or result.get("provider_request_id"),
        "provider_request_id": result.get("provider_request_id"),
        "requested_aspect_ratio": result.get("requested_aspect_ratio"),
        "requested_resolution_tier": result.get("requested_resolution_tier"),
        "provider_reported_usage": result.get("provider_reported_usage"),
        "provider_reported_tokens": result.get("provider_reported_tokens"),
        "provider_reported_image_usage": result.get("provider_reported_image_usage"),
        "provider_reported_cost": result.get("provider_reported_cost"),
        "provider_reported_cost_usd": result.get("provider_reported_cost_usd"),
        "estimated_list_price_usd": result.get("estimated_list_price_usd"),
        "estimated_list_price_inr": result.get("estimated_list_price_inr"),
        "estimated_list_price_is_estimate": result.get("estimated_list_price_is_estimate"),
        "actual_cost_inr": result.get("actual_cost_inr"),
        "validation_passed": validation.get("passed"),
        "failure_reason": result.get("failure_reason"),
        "token_fingerprint": mask_token(None),
        "composed": run.get("composition") is not None,
        "quality_score": telemetry.get("quality_score"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--live-once",
        action="store_true",
        help="Explicit opt-in for EXACTLY ONE Cloudflare Workers AI image request.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--provider",
        choices=("cloudflare", "gemini"),
        default="cloudflare",
        help="Live image backend. Default remains cloudflare.",
    )
    parser.add_argument(
        "--compose-from-run",
        default=None,
        help="Compose a branded card from an existing raw run. Makes ZERO image API calls.",
    )
    parser.add_argument("--logo", default=None, help="Approved CoinNetwork logo path inside V2.")
    args = parser.parse_args(argv)

    if args.compose_from_run:
        if args.live_once:
            print("compose-from-run cannot be combined with --live-once", file=sys.stderr)
            return 2
        from newsagent_v2.image.compose_existing import compose_existing_run, resolve_logo_path
        from newsagent_v2.image.compositor import CompositorError, sanitize_overlay_text

        source_id = str(args.compose_from_run)
        source_brief_path = REPO_ROOT / "output" / "image_runs" / source_id / "brief.json"
        source_val_path = REPO_ROOT / "output" / "image_runs" / source_id / "validation.json"
        if not source_brief_path.is_file():
            print(f"source brief missing: {source_brief_path}", file=sys.stderr)
            return 2
        brief = json.loads(source_brief_path.read_text(encoding="utf-8"))
        expected_sha = None
        if source_val_path.is_file():
            expected_sha = json.loads(source_val_path.read_text(encoding="utf-8")).get("sha256")
        try:
            logo = resolve_logo_path(Path(args.logo) if args.logo else None, REPO_ROOT)
            result = compose_existing_run(
                source_id,
                headline=sanitize_overlay_text(str(brief.get("editorial_subject") or "")),
                category_label=str(brief.get("story_category") or ""),
                event_id=str(brief.get("event_id") or ""),
                logo_path=logo,
                expected_raw_sha256=expected_sha,
            )
        except CompositorError as exc:
            print(json.dumps({"ok": False, "code": exc.code, "message": exc.message}, indent=2))
            return 2
        print(json.dumps({
            "ok": True,
            "run_id": result["run_id"],
            "run_dir": result["run_dir"],
            "source_run_id": source_id,
            "source_raw_unchanged": result["source_hash_before"] == result["source_hash_after"],
            "headline": result["composition"]["headline"],
            "category": result["composition"]["category_label"],
            "logo_path": result["logo_path"],
            "final_path": result["composition"]["final_path"],
            "width": result["composition"]["width"],
            "height": result["composition"]["height"],
            "final_bytes": result["composition"]["final_bytes"],
            "final_sha256": result["composition"]["final_sha256"],
            "compositor_validation": result["composition"]["compositor_validation"],
            "image_generation_requests": 0,
        }, indent=2))
        return 0 if result["composition"]["compositor_validation"].get("passed") else 1

    if not args.live_once:
        print(
            "NewsAgent V2 live image generation requires --live-once.\n"
            "Default: NO NETWORK CALL.\n"
            f"Cloudflare env: {ACCOUNT_ENV}, {TOKEN_ENV}\n"
            f"Gemini env: {GEMINI_KEY_ENV}"
        )
        return 0

    environ = _load_environ()
    brief = load_canonical_brief()
    request = canonical_provider_request(brief)

    if args.provider == "gemini":
        if str(environ.get(GEMINI_KEY_ENV, "")).strip() == "":
            print("GEMINI API KEY MISSING", file=sys.stderr)
            return 2
        try:
            config = load_gemini_config(environ)
        except GeminiConfigError:
            print("GEMINI API KEY MISSING", file=sys.stderr)
            return 2
        provider = GeminiImageProvider(config, model=GEMINI_MODEL, timeout_seconds=60)
    else:
        try:
            config = load_cloudflare_config(environ)
        except CloudflareConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        provider = CloudflareImageProvider(config, model=args.model, timeout_seconds=180)

    runs = run_provider_benchmark(
        provider,
        brief,
        generations=1,
        compose=False,
        request=request,
    )
    summary = _public_result(runs[0])
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("success") and summary.get("validation_passed") else 1


if __name__ == "__main__":
    sys.exit(main())
