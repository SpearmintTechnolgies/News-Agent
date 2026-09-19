"""Persist one image run without mixing raw artwork and compositor output."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.image.contract import IMAGE_RUN_SCHEMA_VERSION
from newsagent_v2.image.validate import refuse_overwrite

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNS_ROOT = REPO_ROOT / "output" / "image_runs"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id(now: datetime | None = None) -> str:
    stamp = (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def run_dir(run_id: str, *, root: Path | None = None) -> Path:
    return (root or DEFAULT_RUNS_ROOT) / run_id


def raw_dir(base: Path) -> Path:
    return base / "raw"


def final_dir(base: Path) -> Path:
    return base / "final"


def prepare_run_dir(base: Path) -> Path:
    raw_dir(base).mkdir(parents=True, exist_ok=True)
    final_dir(base).mkdir(parents=True, exist_ok=True)
    return base


def raw_destination(base: Path, filename: str = "artwork.png") -> Path:
    dest = raw_dir(base) / filename
    refuse_overwrite(dest)
    return dest


def persist_image_run(
    base: Path,
    *,
    brief: dict[str, Any],
    provider_request: dict[str, Any],
    provider_result: dict[str, Any],
    telemetry: dict[str, Any],
    scorecard: dict[str, Any],
    composition: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    run_id: str | None = None,
    secrets: list[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    prepare_run_dir(base)
    payload = {
        "brief": brief,
        "provider_request": provider_request,
        "provider_result": provider_result,
        "telemetry": telemetry,
        "scorecard": scorecard,
    }
    if validation is not None:
        payload["validation"] = validation
    paths = {name: base / f"{name}.json" for name in payload}
    for name, path in paths.items():
        write_json_utf8(path, redact_secrets(payload[name], secrets))
    if composition is not None:
        paths["composition"] = base / "composition.json"
        write_json_utf8(paths["composition"], redact_secrets(composition, secrets))
    manifest = {
        "schema_version": IMAGE_RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "raw_dir": str(raw_dir(base)),
        "final_dir": str(final_dir(base)),
        "raw_and_final_separated": True,
    }
    paths["manifest"] = base / "manifest.json"
    write_json_utf8(paths["manifest"], manifest)
    return {name: str(path) for name, path in paths.items()}
