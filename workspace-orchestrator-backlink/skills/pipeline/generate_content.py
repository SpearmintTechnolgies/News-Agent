#!/usr/bin/env python3
"""generate_content.py — Phase 1 content agent with mandatory image generation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))
if str(_ROOT / "skills" / "pipeline") not in sys.path:
    sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from generate_draft import (  # noqa: E402
    DraftContext,
    DraftResult,
    build_draft_context,
    generate_draft_text,
    load_campaign_config,
)
from validate_image import validate_image, write_test_jpeg  # noqa: E402

GENERATE_SCRIPT = _ROOT / "skills" / "generate-image" / "generate.sh"
MEDIA_ROOT = Path(os.path.expanduser("~/.openclaw/data/backlink_media"))
IMAGE_GEN_ATTEMPTS = 3  # initial try + 2 retries
IMAGE_FAIL_NOTE = "Feature image failed after 3 attempts"
IMAGE_FAIL_URL_PREFIX = "failed:"


@dataclass
class ContentResult:
    content_text: str
    target_link: str
    tone: str
    confidence: float
    placement_type: str
    topic: str
    version: int
    image_prompt: str
    image_local_path: str | None = None
    image_error: str | None = None
    content_type: str = "backlink_content"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_project_config(
    row: backlink_db.WorkflowRow,
    db_path: str,
) -> dict[str, Any]:
    if row.project_id:
        project = backlink_db.get_project(row.project_id, db_path=db_path)
        if project:
            cfg: dict[str, Any] = {}
            if project.config_json:
                try:
                    cfg = json.loads(project.config_json)
                except json.JSONDecodeError:
                    cfg = {}
            cfg.setdefault("target_url", project.target_url or f"https://{project.target_domain}")
            cfg.setdefault("brand_name", project.brand_name or project.name)
            cfg.setdefault("name", project.name)
            return cfg
    return load_campaign_config()


def _media_dir(workflow_id: str) -> Path:
    path = MEDIA_ROOT / workflow_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _default_image_path(workflow_id: str) -> str:
    return str(_media_dir(workflow_id) / "feature.jpg")


def _build_image_prompt(context: DraftContext, topic: str) -> str:
    host = urlparse(context.host_url).netloc or context.host_domain
    return (
        f"Editorial photograph illustrating {topic}, professional tech blog featured image, "
        f"context of {host}, modern cybersecurity and cryptography theme, "
        f"clean composition, no text, photorealistic"
    )


def _should_regenerate_image(edit_prompt: str | None) -> bool:
    if not edit_prompt:
        return False
    lower = edit_prompt.lower()
    return any(word in lower for word in ("image", "photo", "picture", "visual", "thumbnail"))


def _skip_image_gen() -> bool:
    return os.environ.get("BACKLINK_SKIP_IMAGE_GEN", "").lower() in {"1", "true", "yes"}


def _generate_feature_image(
    workflow_id: str,
    prompt: str,
    project_config: dict[str, Any],
) -> str | None:
    if not GENERATE_SCRIPT.is_file():
        return None

    media_dir = _media_dir(workflow_id)
    output_path = media_dir / "feature.jpg"
    result_file = media_dir / "image-result.txt"

    env = os.environ.copy()
    env["OUTPUT_PATH"] = str(output_path)
    env["RESULT_FILE"] = str(result_file)
    env["STAMP_LOGO"] = str(project_config.get("stamp_logo", 1))
    if project_config.get("logo_path"):
        env["LOGO_PATH"] = str(project_config["logo_path"])
    if project_config.get("image_model"):
        env["IMAGE_MODEL"] = str(project_config["image_model"])
    if project_config.get("image_model_fallback"):
        env["IMAGE_MODEL_FALLBACK"] = str(project_config["image_model_fallback"])

    try:
        proc = subprocess.run(
            ["bash", str(GENERATE_SCRIPT), prompt],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if proc.returncode != 0:
        return None

    if result_file.is_file():
        path = result_file.read_text(encoding="utf-8").strip()
        if path and os.path.isfile(path):
            validation = validate_image(path)
            if validation.valid:
                return path

    if output_path.is_file():
        validation = validate_image(str(output_path))
        if validation.valid:
            return str(output_path)
    return None


def _resolve_feature_image(
    workflow_id: str,
    context: DraftContext,
    topic: str,
    project_config: dict[str, Any],
    *,
    previous_path: str | None = None,
    previous_prompt: str | None = None,
    edit_prompt: str | None = None,
) -> tuple[str | None, str, str | None]:
    """Try image generation up to 3 times; return (path, prompt, error)."""
    if (
        previous_path
        and edit_prompt
        and not _should_regenerate_image(edit_prompt)
        and validate_image(previous_path).valid
    ):
        return previous_path, previous_prompt or _build_image_prompt(context, topic), None

    image_prompt = _build_image_prompt(context, topic)

    if _skip_image_gen():
        path = write_test_jpeg(_default_image_path(workflow_id))
        return path, image_prompt, None

    last_error = IMAGE_FAIL_NOTE
    for attempt in range(1, IMAGE_GEN_ATTEMPTS + 1):
        image_local_path = _generate_feature_image(workflow_id, image_prompt, project_config)
        if image_local_path:
            return image_local_path, image_prompt, None
        last_error = f"{IMAGE_FAIL_NOTE} (try {attempt}/{IMAGE_GEN_ATTEMPTS})"
        if attempt < IMAGE_GEN_ATTEMPTS:
            time.sleep(2)

    return None, image_prompt, last_error


def generate_content(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> tuple[ContentResult, backlink_db.ContentAssetRow]:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    project_config = _load_project_config(row, db_path)

    context = build_draft_context(workflow_id, db_path=db_path)
    if project_config.get("target_url"):
        context.target_url = str(project_config["target_url"]).rstrip("/")
    if project_config.get("brand_name"):
        context.brand_name = str(project_config["brand_name"])

    session = backlink_db.get_edit_session(workflow_id, db_path=db_path)
    previous_content = backlink_db.get_latest_content_asset(workflow_id, db_path=db_path)
    previous_draft = backlink_db.get_latest_draft(workflow_id, db_path=db_path)
    edit_prompt = session.edit_prompt if session else None

    prior_text = None
    if edit_prompt:
        if previous_content:
            prior_text = previous_content.content_text
        elif previous_draft:
            prior_text = previous_draft.draft_text

    generated: DraftResult = generate_draft_text(
        context,
        edit_prompt=edit_prompt,
        previous_draft=prior_text,
    )

    prev_path = previous_content.image_local_path if previous_content else None
    prev_prompt = previous_content.image_prompt if previous_content else None
    image_local_path, image_prompt, image_error = _resolve_feature_image(
        workflow_id,
        context,
        generated.topic,
        project_config,
        previous_path=prev_path,
        previous_prompt=prev_prompt,
        edit_prompt=edit_prompt,
    )

    image_url = f"{IMAGE_FAIL_URL_PREFIX}{image_error}" if image_error else None

    saved = backlink_db.save_content_asset(
        workflow_id,
        generated.draft_text,
        target_link=context.target_url,
        image_local_path=image_local_path,
        image_url=image_url,
        image_prompt=image_prompt,
        content_type=generated.placement_type,
        confidence=generated.confidence,
        db_path=db_path,
    )

    backlink_db.save_draft(
        workflow_id,
        generated.draft_text,
        tone=generated.tone,
        confidence=generated.confidence,
        db_path=db_path,
    )

    result = ContentResult(
        content_text=generated.draft_text,
        target_link=context.target_url,
        tone=generated.tone,
        confidence=generated.confidence,
        placement_type=generated.placement_type,
        topic=generated.topic,
        version=saved.version,
        image_local_path=image_local_path,
        image_prompt=image_prompt,
        image_error=image_error,
    )

    log_message = (
        f"Content v{saved.version} ({generated.placement_type}) + image"
        if not image_error
        else f"Content v{saved.version} ({generated.placement_type}); image failed, continuing"
    )
    backlink_db.insert_log(
        workflow_id,
        "content",
        log_message,
        level="warning" if image_error else "info",
        detail={
            **result.to_dict(),
            "image_required": True,
            "image_generated": bool(image_local_path),
            "image_attempts": IMAGE_GEN_ATTEMPTS if image_error else 1,
            "edit_applied": bool(edit_prompt),
        },
        db_path=db_path,
    )
    return result, saved


def content_workflow(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> workflow_manager.AgentResult:
    try:
        result, saved = generate_content(workflow_id, db_path=db_path)
    except ValueError as exc:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="content",
            data={},
            error=str(exc),
        )

    preview = result.content_text
    if len(preview) > 500:
        preview = preview[:497].rstrip() + "..."

    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="content",
        data={
            "content_preview": preview,
            "content_version": saved.version,
            "tone": result.tone,
            "confidence": result.confidence,
            "placement_type": result.placement_type,
            "topic": result.topic,
            "image_local_path": result.image_local_path,
            "image_error": result.image_error,
        },
    )


def main() -> int:
    import argparse

    from worker_cli import run_content  # noqa: E402

    parser = argparse.ArgumentParser(description="Run content generation for one workflow")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)
    args = parser.parse_args()
    return run_content(args.workflow_id, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
