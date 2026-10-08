"""
Deterministic CoinNetwork logo knockout.

Removes the solid blue rectangle from the approved master and writes a
separate RGBA derivative. The master file is never overwritten.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from newsagent_v2.image.validate import file_sha256

DERIVED_LOGO_NAME = "coinnetwork_logo_white_transparent.png"
MASTER_LOGO_NAME = "coinnetwork_logo.png"


class LogoDeriveError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def is_coinnetwork_master(path: Path) -> bool:
    return path.name == MASTER_LOGO_NAME and "derived" not in path.parts


def derived_path_for_master(master_path: Path) -> Path:
    return master_path.parent / "derived" / DERIVED_LOGO_NAME


def _sample_key_color(image: Image.Image) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    samples: list[tuple[int, int, int]] = []
    step_x = max(1, width // 32)
    step_y = max(1, height // 32)
    for x in range(0, width, step_x):
        samples.append(rgb.getpixel((x, 0)))
        samples.append(rgb.getpixel((x, height - 1)))
    for y in range(0, height, step_y):
        samples.append(rgb.getpixel((0, y)))
        samples.append(rgb.getpixel((width - 1, y)))
    samples.sort()
    return samples[len(samples) // 2]


def _is_solid_blue_field(
    pixel: tuple[int, int, int],
    key: tuple[int, int, int],
) -> bool:
    r, g, b = pixel
    cheb = max(abs(r - key[0]), abs(g - key[1]), abs(b - key[2]))
    if cheb <= 55:
        return True
    return r < 90 and b > 140 and (b - r) >= 70


def color_to_alpha_rgba(image: Image.Image, key: tuple[int, int, int]) -> Image.Image:
    src = image.convert("RGBA")
    keyed: list[tuple[int, int, int, int]] = []
    for r, g, b, _src_a in src.getdata():
        if _is_solid_blue_field((r, g, b), key):
            keyed.append((255, 255, 255, 0))
            continue
        if min(r, g, b) >= 200:
            keyed.append((r, g, b, 255))
            continue
        luminance = (r + g + b) / 3.0
        alpha = int(round(255 * min(1.0, max(0.0, (luminance - 90.0) / 130.0))))
        keyed.append((255, 255, 255, alpha))
    out = Image.new("RGBA", src.size)
    out.putdata(keyed)
    return out


def derive_white_transparent_logo(master_path: Path, dest_path: Path) -> dict[str, Any]:
    if dest_path.resolve() == master_path.resolve():
        raise LogoDeriveError("dest_is_master", "refusing to write the derived logo over the master")
    if not master_path.is_file():
        raise LogoDeriveError("master_missing", f"master logo not found: {master_path}")

    master_hash_before = file_sha256(master_path)
    with Image.open(master_path) as src:
        src.load()
        width, height = src.size
        key = _sample_key_color(src)
        derived = color_to_alpha_rgba(src, key)

    if derived.size != (width, height):
        raise LogoDeriveError("geometry_changed", "derived logo dimensions must match the master")

    alphas = [px[3] for px in derived.getdata()]
    transparent = sum(1 for a in alphas if a == 0)
    opaque = sum(1 for a in alphas if a >= 250)
    if transparent < 1:
        raise LogoDeriveError("no_alpha", "derived logo has no transparent background")
    if opaque < 1:
        raise LogoDeriveError("mark_removed", "derived logo lost the opaque CoinNetwork mark")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    derived.save(dest_path, format="PNG")

    master_hash_after = file_sha256(master_path)
    if master_hash_after != master_hash_before:
        raise LogoDeriveError("master_mutated", "logo derive must not mutate the approved master")

    return {
        "master_path": str(master_path),
        "derived_path": str(dest_path),
        "master_sha256": master_hash_after,
        "derived_sha256": file_sha256(dest_path),
        "width": width,
        "height": height,
        "proportions_preserved": True,
        "has_alpha": True,
        "transparent_pixels": transparent,
        "opaque_pixels": opaque,
        "key_color": {"r": key[0], "g": key[1], "b": key[2]},
        "fabricated": False,
        "geometry_changed": False,
    }


def _already_transparent(image_path: Path) -> bool:
    """True if the logo already has a transparent background (no solid field to knock out)."""
    try:
        with Image.open(image_path) as img:
            rgba = img.convert("RGBA")
            alphas = rgba.getchannel("A").getdata()
            transparent = sum(1 for a in alphas if a < 10)
            total = rgba.width * rgba.height
            return transparent > total * 0.3
    except Exception:
        return False


def ensure_compositor_logo(logo_path: Path) -> tuple[Path, dict[str, Any] | None]:
    """Knock the solid field out of a logo only if it has one. Already-transparent logos are used as-is."""
    if is_coinnetwork_master(logo_path):
        dest = derived_path_for_master(logo_path)
        meta = derive_white_transparent_logo(logo_path, dest)
        return dest, meta
    if _already_transparent(logo_path):
        return logo_path, None
    dest = logo_path.parent / "derived" / f"{logo_path.stem}_white.png"
    try:
        meta = derive_white_transparent_logo(logo_path, dest)
    except LogoDeriveError:
        return logo_path, None
    return dest, meta
