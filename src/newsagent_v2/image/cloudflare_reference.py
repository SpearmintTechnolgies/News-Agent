"""
Local derived Cloudflare Klein reference images.

The master/source file is never mutated. Resize is deterministic LANCZOS
and must fit strictly under 512x512 while preserving aspect ratio.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.image.artifacts import REPO_ROOT, utc_now
from newsagent_v2.image.providers.cloudflare import MAX_REFERENCE_EDGE
from newsagent_v2.image.validate import ImageValidationError, file_sha256, refuse_overwrite, validate_reference_image

EVENT_027_MASTER_NAME = "event-027-source.jpg"
EVENT_027_MASTER_SHA256 = "7579c84c061f0f8ba197b69baeeabc6a696642fbe7cfe75164c9e647bf09a3f7"
DEFAULT_MASTER = REPO_ROOT / "input" / "references" / EVENT_027_MASTER_NAME
DEFAULT_DERIVED = REPO_ROOT / "input" / "references" / "derived" / "event-027-cloudflare-reference.jpg"
JPEG_QUALITY = 95
JPEG_SUBSAMPLING = 0


class CloudflareReferenceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fit_under_max_edge(width: int, height: int, max_edge: int = MAX_REFERENCE_EDGE) -> tuple[int, int]:
    if width < 1 or height < 1:
        raise CloudflareReferenceError("invalid_size", "source dimensions must be positive")
    if width <= max_edge and height <= max_edge:
        return width, height
    scale = min(max_edge / width, max_edge / height)
    out_w = max(1, int(width * scale))
    out_h = max(1, int(height * scale))
    while out_w > max_edge or out_h > max_edge:
        if out_w >= out_h:
            out_w -= 1
            out_h = max(1, int(round(out_w * height / width)))
        else:
            out_h -= 1
            out_w = max(1, int(round(out_h * width / height)))
    return out_w, out_h


def derive_cloudflare_reference(
    master_path: Path,
    dest_path: Path,
    *,
    expected_master_sha256: str | None = None,
) -> dict[str, Any]:
    if dest_path.resolve() == master_path.resolve():
        raise CloudflareReferenceError("dest_is_master", "refusing to overwrite the master reference")
    if not master_path.is_file():
        raise CloudflareReferenceError("master_missing", f"master reference not found: {master_path}")

    before = file_sha256(master_path)
    if expected_master_sha256 and before != expected_master_sha256:
        raise CloudflareReferenceError(
            "master_hash_mismatch",
            "master reference SHA-256 does not match the expected source digest",
        )

    inspected = validate_reference_image(master_path)
    src_w = int(inspected["width"])
    src_h = int(inspected["height"])
    out_w, out_h = fit_under_max_edge(src_w, src_h)
    if out_w > MAX_REFERENCE_EDGE or out_h > MAX_REFERENCE_EDGE:
        raise CloudflareReferenceError("still_too_large", f"derived size {out_w}x{out_h} is not under 512")

    refuse_overwrite(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(master_path) as src:
        rgb = src.convert("RGB")
        if (src_w, src_h) != (out_w, out_h):
            rgb = rgb.resize((out_w, out_h), Image.Resampling.LANCZOS)
        rgb.save(
            dest_path,
            format="JPEG",
            quality=JPEG_QUALITY,
            subsampling=JPEG_SUBSAMPLING,
            optimize=False,
        )

    after = file_sha256(master_path)
    if after != before:
        raise CloudflareReferenceError("master_mutated", "derive must not mutate the master reference")

    derived = validate_reference_image(dest_path)
    if derived["width"] > MAX_REFERENCE_EDGE or derived["height"] > MAX_REFERENCE_EDGE:
        raise CloudflareReferenceError("still_too_large", "derived file exceeds Cloudflare 512 cap")

    meta = {
        "schema_version": "cloudflare-reference-derive-v1",
        "master_path": str(master_path),
        "master_sha256": after,
        "master_width": src_w,
        "master_height": src_h,
        "master_bytes": inspected["size_bytes"],
        "derived_path": str(dest_path),
        "derived_sha256": derived["sha256"],
        "derived_width": derived["width"],
        "derived_height": derived["height"],
        "derived_bytes": derived["size_bytes"],
        "derived_format": derived["format"],
        "aspect_preserved": True,
        "ai_edits": False,
        "branding_removed": False,
        "max_edge": MAX_REFERENCE_EDGE,
        "usage": "cloudflare_klein_input_image_0",
        "must_not_be_final_image": True,
        "timestamp_utc": utc_now().strftime("%Y%m%dT%H%M%SZ"),
    }
    write_json_utf8(dest_path.with_suffix(".provenance.json"), meta)
    if file_sha256(master_path) != before:
        raise CloudflareReferenceError("master_mutated", "provenance write mutated the master reference")
    return meta
