"""Featured-image encoding: JPEG with compression for Telegram / WordPress."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PIL import Image

DEFAULT_JPEG_QUALITY = 85
JPEG_QUALITY_ENV = "NEWSAGENT_IMAGE_JPEG_QUALITY"
# Progressive JPEG shrinks download size for the 1280×720 card without a visible hit.
DEFAULT_PROGRESSIVE = True


def jpeg_quality(environ: dict[str, str] | None = None) -> int:
    """JPEG quality 1–95 (default 85)."""
    env = environ if environ is not None else os.environ
    raw = str(env.get(JPEG_QUALITY_ENV) or DEFAULT_JPEG_QUALITY).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_JPEG_QUALITY
    return max(1, min(95, value))


def save_featured_image(
    image: Image.Image,
    dest: Path,
    *,
    quality: int | None = None,
    progressive: bool = DEFAULT_PROGRESSIVE,
) -> dict[str, Any]:
    """Write the branded card. ``.jpg`` / ``.jpeg`` → compressed JPEG; else PNG."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = dest.suffix.lower()
    rgb = image.convert("RGB")
    if suffix in {".jpg", ".jpeg"}:
        q = DEFAULT_JPEG_QUALITY if quality is None else max(1, min(95, int(quality)))
        rgb.save(
            dest,
            format="JPEG",
            quality=q,
            optimize=True,
            progressive=bool(progressive),
            subsampling="4:2:0",
        )
        return {
            "format": "JPEG",
            "quality": q,
            "progressive": bool(progressive),
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
        }
    rgb.save(dest, format="PNG", optimize=True)
    return {
        "format": "PNG",
        "quality": None,
        "progressive": False,
        "path": str(dest),
        "size_bytes": dest.stat().st_size,
    }


def recompress_path(
    src: Path,
    dest: Path,
    *,
    quality: int | None = None,
) -> dict[str, Any]:
    """Re-encode an existing card file to the destination format."""
    with Image.open(src) as image:
        image.load()
        return save_featured_image(image, dest, quality=quality)
