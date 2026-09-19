"""
Deterministic checks on generated image files.

No aesthetic scoring. No NSFW/semantic moderation. Extra validators may
be registered later without pretending they already exist.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from newsagent_v2.image.contract import SUPPORTED_RAW_FORMATS

ASPECT_TOLERANCE = 0.03
MIN_WIDTH = 640
MIN_HEIGHT = 360
MIN_BYTES = 64
MAX_BYTES = 40 * 1024 * 1024
MIN_REFERENCE_WIDTH = 32
MIN_REFERENCE_HEIGHT = 32


def validate_reference_image(path: Path) -> dict[str, Any]:
    """Decode a story/source reference. Does not mutate the file. No aesthetic scoring."""
    if not path.exists() or not path.is_file():
        raise ImageValidationError("missing_file", f"reference image does not exist: {path}")
    size = path.stat().st_size
    if size < MIN_BYTES:
        raise ImageValidationError("empty_or_tiny", "reference image is empty or unreasonably small")
    if size > MAX_BYTES:
        raise ImageValidationError("too_large", "reference image exceeds the reasonable size cap")

    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            fmt = _format_name(path, image)
            mime = Image.MIME.get(image.format or "", "")
    except ImageValidationError:
        raise
    except Exception as exc:
        raise ImageValidationError("undecodable", f"reference image could not be decoded: {exc}") from exc

    if fmt not in SUPPORTED_RAW_FORMATS and path.suffix.lstrip(".").lower() not in SUPPORTED_RAW_FORMATS:
        raise ImageValidationError("unsupported_format", f"unsupported reference image format: {fmt}")
    if width <= 0 or height <= 0:
        raise ImageValidationError("zero_dimensions", "reference dimensions must be positive")
    if width < MIN_REFERENCE_WIDTH or height < MIN_REFERENCE_HEIGHT:
        raise ImageValidationError(
            "below_minimum_dimensions",
            f"reference {width}x{height} is below minimum {MIN_REFERENCE_WIDTH}x{MIN_REFERENCE_HEIGHT}",
        )
    if not mime:
        mime = {
            "jpeg": "image/jpeg",
            "jpg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(fmt, f"image/{fmt}")

    return {
        "path": str(path),
        "width": width,
        "height": height,
        "format": fmt,
        "mime_type": mime,
        "size_bytes": size,
        "sha256": file_sha256(path),
        "passed": True,
    }


class ImageValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ExtraImageValidator(Protocol):
    name: str

    def validate(self, path: Path, context: dict[str, Any]) -> list[dict[str, str]]:
        ...


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refuse_overwrite(path: Path) -> None:
    if path.exists():
        raise ImageValidationError(
            "raw_overwrite",
            f"refusing to overwrite existing raw artifact: {path}",
        )


def _format_name(path: Path, image: Image.Image) -> str:
    fmt = (image.format or path.suffix.lstrip(".")).lower()
    if fmt == "jpg":
        return "jpeg"
    return fmt


def validate_raw_artwork(
    path: Path,
    *,
    expected_aspect: tuple[int, int] | None = (16, 9),
    min_width: int = MIN_WIDTH,
    min_height: int = MIN_HEIGHT,
    expected_width: int | None = None,
    expected_height: int | None = None,
    extra_validators: list[ExtraImageValidator] | tuple[ExtraImageValidator, ...] = (),
) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise ImageValidationError("missing_file", f"image file does not exist: {path}")
    size = path.stat().st_size
    if size < MIN_BYTES:
        raise ImageValidationError("empty_or_tiny", "image file is empty or unreasonably small")
    if size > MAX_BYTES:
        raise ImageValidationError("too_large", "image file exceeds the reasonable size cap")

    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            fmt = _format_name(path, image)
    except ImageValidationError:
        raise
    except Exception as exc:
        raise ImageValidationError("undecodable", f"image could not be decoded: {exc}") from exc

    if fmt not in SUPPORTED_RAW_FORMATS and path.suffix.lstrip(".").lower() not in SUPPORTED_RAW_FORMATS:
        raise ImageValidationError("unsupported_format", f"unsupported image format: {fmt}")
    if width <= 0 or height <= 0:
        raise ImageValidationError("zero_dimensions", "image dimensions must be positive")
    if width < min_width or height < min_height:
        raise ImageValidationError(
            "below_minimum_dimensions",
            f"image {width}x{height} is below minimum {min_width}x{min_height}",
        )
    if expected_width is not None and width != expected_width:
        raise ImageValidationError("width_mismatch", f"expected width {expected_width}, got {width}")
    if expected_height is not None and height != expected_height:
        raise ImageValidationError("height_mismatch", f"expected height {expected_height}, got {height}")
    if expected_aspect is not None:
        exp_w, exp_h = expected_aspect
        expected_ratio = exp_w / exp_h
        actual_ratio = width / height
        if abs(actual_ratio - expected_ratio) > ASPECT_TOLERANCE:
            raise ImageValidationError(
                "aspect_ratio",
                f"aspect {actual_ratio:.4f} outside tolerance of {expected_ratio:.4f}",
            )

    extra_issues: list[dict[str, str]] = []
    context = {"width": width, "height": height, "format": fmt, "size_bytes": size}
    for validator in extra_validators:
        extra_issues.extend(validator.validate(path, context))

    return {
        "path": str(path),
        "width": width,
        "height": height,
        "format": fmt,
        "size_bytes": size,
        "sha256": file_sha256(path),
        "aspect_ratio": round(width / height, 6),
        "extra_validator_issues": extra_issues,
        "passed": True,
        "aesthetic_score": None,
        "nsfw_score": None,
        "semantic_moderation": None,
    }


def write_png_rgb(path: Path, width: int, height: int, color: tuple[int, int, int]) -> Path:
    """Local synthetic PNG writer for tests. Not a generative model."""
    refuse_overwrite(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), color)
    image.save(path, format="PNG")
    return path
