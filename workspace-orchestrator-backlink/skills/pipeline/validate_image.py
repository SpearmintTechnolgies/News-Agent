#!/usr/bin/env python3
"""validate_image.py — check generated image meets minimum quality bar."""
from __future__ import annotations

import os
from dataclasses import dataclass

MIN_BYTES = 51_200


def write_test_jpeg(path: str) -> str:
    """Write a minimal valid JPEG-sized file for unit tests (BACKLINK_SKIP_IMAGE_GEN)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # JPEG SOI + padding to satisfy min size check
    body = b"\xff\xd8\xff\xe0" + b"\x00" * (MIN_BYTES - 4)
    with open(path, "wb") as f:
        f.write(body)
    return path


@dataclass
class ImageValidation:
    valid: bool
    path: str
    size_bytes: int
    reason: str | None = None


def validate_image(path: str, *, min_bytes: int = MIN_BYTES) -> ImageValidation:
    if not path or not os.path.isfile(path):
        return ImageValidation(False, path, 0, "file missing")

    size = os.path.getsize(path)
    if size < min_bytes:
        return ImageValidation(False, path, size, f"too small ({size} < {min_bytes})")

    with open(path, "rb") as f:
        header = f.read(3)
    if header[:2] != b"\xff\xd8":
        return ImageValidation(False, path, size, "not JPEG")

    return ImageValidation(True, path, size)
