"""One-shot event-027 Cloudflare Klein live request. Not imported by the default CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from newsagent_v2.image.artifacts import REPO_ROOT
from newsagent_v2.image.benchmark.fixture import load_canonical_brief
from newsagent_v2.image.benchmark.runner import run_provider_benchmark
from newsagent_v2.image.cloudflare_reference import (
    DEFAULT_DERIVED,
    DEFAULT_MASTER,
    EVENT_027_MASTER_SHA256,
    derive_cloudflare_reference,
)
from newsagent_v2.image.compositor import CompositionSpec
from newsagent_v2.image.provider import ProviderRequest
from newsagent_v2.image.providers.cloudflare import (
    DEFAULT_MODEL,
    CloudflareImageProvider,
    load_cloudflare_config,
)
from newsagent_v2.image.validate import file_sha256, validate_reference_image

PROMPT = (
    "Generate a completely original premium financial-news editorial artwork. "
    "Use input image 0 only to understand the underlying Bitcoin/ETF subject and visual context. "
    "DO NOT reproduce the source composition, Cointelegraph branding, source watermark, "
    "source typography, or source illustration layout. "
    "Story: Bitcoin ETF outflows accelerate as investors pull $449M in three days. "
    "Visual goal: institutional capital moving out of Bitcoin ETF exposure; "
    "Bitcoin as clear principal subject; subtle sense of capital withdrawal / "
    "institutional risk-off positioning; serious premium financial newsroom aesthetic; "
    "restrained, realistic, sophisticated. "
    "Prefer a strong single focal concept, realistic institutional/financial visual language, "
    "controlled dark editorial lighting, clean negative space, and a premium "
    "Bloomberg/Reuters-style feature-image sensibility. "
    "AVOID generic trader staring at monitors, crypto-bro aesthetic, neon cyberpunk, "
    "excessive glowing lines, floating random Bitcoin coins, malformed Bitcoin symbols, "
    "fake documents, fake text, fake UI, fake dashboards, pseudotext, logos, watermarks, "
    "headline text, source branding, and YouTube-thumbnail styling. "
    "ABSOLUTELY NO READABLE TEXT."
)

DERIVED_LOGO = REPO_ROOT / "brand" / "derived" / "coinnetwork_logo_white_transparent.png"


def prepare_derived_reference() -> dict:
    master_sha = file_sha256(DEFAULT_MASTER)
    if master_sha != EVENT_027_MASTER_SHA256:
        raise SystemExit(f"master SHA mismatch: {master_sha}")
    if DEFAULT_DERIVED.is_file():
        inspected = validate_reference_image(DEFAULT_DERIVED)
        if inspected["width"] >= 512 or inspected["height"] >= 512:
            raise SystemExit("existing derived reference is not under 512x512")
        if file_sha256(DEFAULT_MASTER) != EVENT_027_MASTER_SHA256:
            raise SystemExit("master mutated while inspecting derived reference")
        return {
            "created": False,
            "derived_path": str(DEFAULT_DERIVED),
            "derived_width": inspected["width"],
            "derived_height": inspected["height"],
            "derived_bytes": inspected["size_bytes"],
            "derived_sha256": inspected["sha256"],
            "master_sha256": master_sha,
        }
    meta = derive_cloudflare_reference(
        DEFAULT_MASTER,
        DEFAULT_DERIVED,
        expected_master_sha256=EVENT_027_MASTER_SHA256,
    )
    if file_sha256(DEFAULT_MASTER) != EVENT_027_MASTER_SHA256:
        raise SystemExit("master mutated during derive")
    return {"created": True, **meta}


def main() -> int:
    from newsagent_v2.image.__main__ import _load_environ, _public_result

    derived = prepare_derived_reference()
    environ = _load_environ()
    config = load_cloudflare_config(environ)
    provider = CloudflareImageProvider(
        config,
        model=DEFAULT_MODEL,
        timeout_seconds=180,
        max_retries=0,
    )
    brief = load_canonical_brief()
    request = ProviderRequest(
        prompt=PROMPT,
        width=1280,
        height=720,
        reference_image_path=str(DEFAULT_DERIVED),
        reference_image_role="story_reference",
        reference_required=True,
    )
    logo_present = DERIVED_LOGO.is_file()
    spec = None
    if logo_present:
        spec = CompositionSpec(
            headline="",
            category_label=None,
            logo_path=DERIVED_LOGO,
            test_mode=False,
            logo_only=True,
        )
    runs = run_provider_benchmark(
        provider,
        brief,
        generations=1,
        compose=bool(spec),
        composition_spec=spec,
        request=request,
    )
    summary = _public_result(runs[0])
    telemetry = runs[0].get("telemetry") or {}
    composition = runs[0].get("composition")
    payload = {
        "derived_reference": derived,
        "master_sha256_after": file_sha256(DEFAULT_MASTER),
        "master_unchanged": file_sha256(DEFAULT_MASTER) == EVENT_027_MASTER_SHA256,
        "run": summary,
        "reference_telemetry": {
            "reference_used": telemetry.get("reference_used"),
            "reference_role": telemetry.get("reference_role"),
            "reference_sha256": telemetry.get("reference_sha256"),
            "reference_width": telemetry.get("reference_width"),
            "reference_height": telemetry.get("reference_height"),
            "provider_reference_supported": telemetry.get("provider_reference_supported"),
            "reference_required": telemetry.get("reference_required"),
        },
        "usage": summary.get("provider_reported_usage"),
        "cost": summary.get("provider_reported_cost"),
        "neurons": (runs[0].get("result") or {}).get("provider_reported_neurons"),
        "branding_asset_present": logo_present,
        "final_path": None if composition is None else composition.get("final_path"),
        "gemini_calls": 0,
        "live_requests": 1,
    }
    print(json.dumps(payload, indent=2))
    if not summary.get("success"):
        return 1
    if not logo_present:
        print("BRANDING ASSET MISSING: derived white transparent CoinNetwork logo not found", file=sys.stderr)
        return 0
    return 0 if summary.get("validation_passed") else 1


if __name__ == "__main__":
    sys.exit(main())
