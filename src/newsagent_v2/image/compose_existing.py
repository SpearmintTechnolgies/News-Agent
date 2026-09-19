"""Compose a branded card from an existing raw artwork run. No image APIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.image.artifacts import (
    DEFAULT_RUNS_ROOT,
    new_run_id,
    persist_image_run,
    prepare_run_dir,
    run_dir,
    utc_now,
    final_dir,
)
from newsagent_v2.image.compositor import (
    CompositionSpec,
    CompositorError,
    compose_card,
    discover_approved_logo,
)
from newsagent_v2.image.contract import COMPOSITOR_VERSION
from newsagent_v2.image.validate import file_sha256, validate_raw_artwork

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def compose_existing_run(
    source_run_id: str,
    *,
    headline: str,
    category_label: str,
    event_id: str,
    logo_path: Path,
    expected_raw_sha256: str | None = None,
    persist_root: Path | None = None,
    source_root: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = persist_root or DEFAULT_RUNS_ROOT
    source_base = (source_root or DEFAULT_RUNS_ROOT) / source_run_id
    if not source_base.is_dir():
        raise CompositorError("source_run_missing", f"source run not found: {source_base}")
    raw_path = source_base / "raw" / "artwork.jpg"
    if not raw_path.is_file():
        png = source_base / "raw" / "artwork.png"
        raw_path = png if png.is_file() else raw_path
    if not raw_path.is_file():
        raise CompositorError("source_raw_missing", f"source raw artwork not found under {source_base}")

    source_hash_before = file_sha256(raw_path)
    if expected_raw_sha256 and source_hash_before != expected_raw_sha256:
        raise CompositorError(
            "source_hash_mismatch",
            "source raw SHA-256 does not match the approved run record",
        )

    validate_raw_artwork(raw_path, expected_width=1280, expected_height=720)

    source_brief = {}
    brief_path = source_base / "brief.json"
    if brief_path.is_file():
        source_brief = load_json(brief_path)
    source_telemetry = {}
    telemetry_path = source_base / "telemetry.json"
    if telemetry_path.is_file():
        source_telemetry = load_json(telemetry_path)

    stamp = utc_now()
    run_id = new_run_id(stamp)
    base = prepare_run_dir(run_dir(run_id, root=root))
    dest = final_dir(base) / "card.png"

    spec = CompositionSpec(
        headline=headline,
        category_label=category_label,
        logo_path=logo_path,
        test_mode=False,
        output_name="card.png",
    )
    composition = compose_card(raw_path, dest, spec)
    source_hash_after = file_sha256(raw_path)
    if source_hash_after != source_hash_before:
        raise CompositorError("raw_mutated", "compositor must not mutate the raw artwork file")

    validation = composition["compositor_validation"]
    source_event = source_brief.get("event_id") or source_telemetry.get("event_id")
    validation["event_id_ok"] = True if source_event is None else source_event == event_id
    if not validation["event_id_ok"]:
        validation["warnings"] = list(validation.get("warnings") or [])
        validation["warnings"].append("event_id mismatch versus source brief")
        validation["passed"] = False
    validation["headline_ok"] = composition["headline"] == " ".join(headline.split())
    validation["category_ok"] = True
    validation["source_run_id"] = source_run_id
    validation["source_raw_sha256"] = source_hash_after
    validation["final_sha256"] = composition["final_sha256"]
    validation["provider_provenance"] = {
        "provider_name": source_telemetry.get("provider_name"),
        "model_name": source_telemetry.get("model_name"),
        "source_run_id": source_run_id,
    }

    composition["event_id"] = event_id
    composition["source_run_id"] = source_run_id
    composition["source_raw_sha256"] = source_hash_after
    composition["timestamp_utc"] = stamp.strftime("%Y%m%dT%H%M%SZ")
    composition["compositor_version"] = COMPOSITOR_VERSION
    composition["image_generation_requests"] = 0
    composition["compositor_validation"] = validation

    persist_image_run(
        base,
        brief=source_brief or {"event_id": event_id, "editorial_subject": headline},
        provider_request={
            "skipped": True,
            "reason": "compose_existing_run_no_generation",
            "image_generation_requests": 0,
        },
        provider_result={
            "success": False,
            "skipped": True,
            "reason": "no_image_generation",
            "provider_name": source_telemetry.get("provider_name"),
            "model_name": source_telemetry.get("model_name"),
        },
        telemetry={
            "schema_version": "image-telemetry-v1",
            "run_id": run_id,
            "event_id": event_id,
            "kind": "compositor_only",
            "source_run_id": source_run_id,
            "image_generation_requests": 0,
            "quality_score": None,
            "actual_cost_inr": None,
            "timestamp_utc": composition["timestamp_utc"],
            "provider_name": source_telemetry.get("provider_name"),
            "model_name": source_telemetry.get("model_name"),
        },
        scorecard={
            "run_id": run_id,
            "event_id": event_id,
            "auto_scored": False,
            "kind": "compositor_only",
            "source_run_id": source_run_id,
        },
        composition=composition,
        validation=validation,
        run_id=run_id,
    )
    provenance = {
        "kind": "compositor_only",
        "source_run_id": source_run_id,
        "source_raw_path": str(raw_path),
        "source_raw_sha256": source_hash_after,
        "source_raw_bytes": raw_path.stat().st_size,
        "source_raw_unchanged": True,
        "logo_path": str(logo_path),
        "logo_sha256": composition["logo"].get("sha256") if composition.get("logo") else None,
        "master_logo_path": str((repo_root or REPO_ROOT) / "brand" / "coinnetwork_logo.png"),
        "master_logo_sha256": composition["logo"].get("master_sha256")
        if composition.get("logo")
        else None,
        "headline": composition["headline"],
        "category_label": composition["category_label"],
        "event_id": event_id,
        "compositor_version": COMPOSITOR_VERSION,
        "timestamp_utc": composition["timestamp_utc"],
        "final_path": composition["final_path"],
        "final_width": composition["width"],
        "final_height": composition["height"],
        "final_bytes": composition["final_bytes"],
        "final_sha256": composition["final_sha256"],
        "image_generation_requests": 0,
        "repo_root": str(repo_root or REPO_ROOT),
    }
    write_json_utf8(base / "source_provenance.json", provenance)
    return {
        "run_id": run_id,
        "run_dir": str(base),
        "composition": composition,
        "provenance": provenance,
        "source_hash_before": source_hash_before,
        "source_hash_after": source_hash_after,
        "logo_path": str(logo_path),
    }


def resolve_logo_path(explicit: Path | None, repo_root: Path | None = None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise CompositorError("logo_missing", f"logo file not found: {explicit}")
        return explicit
    found = discover_approved_logo(repo_root or REPO_ROOT)
    if found is None:
        raise CompositorError(
            "logo_required",
            "approved CoinNetwork logo was not found inside NewsAgent-V2",
        )
    return found
