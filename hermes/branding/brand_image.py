from __future__ import annotations

from pathlib import Path
from io import BytesIO
from urllib.request import Request, urlopen
from urllib.parse import urlparse
import hashlib
import time

from PIL import Image


HOME = Path.home() / ".hermes"
LOGO = HOME / "branding" / "logo.png"
CACHE = HOME / "image_cache"

CACHE.mkdir(parents=True, exist_ok=True)


def _load_image(ref):
    if not ref:
        return None

    ref = str(ref)

    if ref.startswith(("http://", "https://")):
        req = Request(
            ref,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
        )

        with urlopen(req, timeout=30) as response:
            return Image.open(
                BytesIO(response.read())
            ).convert("RGBA")

    return Image.open(
        Path(ref)
    ).convert("RGBA")


def _prepare_logo(target_width: int):
    logo = Image.open(LOGO).convert("RGBA")

    # Remove only transparent padding from the ORIGINAL logo.
    bbox = logo.getbbox()

    if bbox:
        logo = logo.crop(bbox)

    target_width = max(
        90,
        int(target_width * 0.22)
    )

    scale = target_width / logo.width

    target_height = max(
        1,
        int(logo.height * scale)
    )

    return logo.resize(
        (target_width, target_height),
        Image.Resampling.LANCZOS,
    )


def apply_logo(image_ref):
    """
    Add the exact MemeCoinist logo directly onto the image.

    IMPORTANT:
    - No dark/gray background
    - No rounded rectangle
    - No shadow
    - No AI editing
    - Original RGBA transparency preserved
    """

    if not image_ref:
        return image_ref

    if not LOGO.exists():
        return image_ref

    image = _load_image(image_ref)

    if image is None:
        return image_ref

    width, height = image.size

    logo = _prepare_logo(width)

    # Small top-left safe margin.
    margin = max(
        12,
        int(width * 0.035)
    )

    # DIRECT alpha compositing.
    # Absolutely no backing layer.
    image.alpha_composite(
        logo,
        (
            margin,
            margin,
        ),
    )

    # Deterministic-ish cache filename.
    source_id = hashlib.sha256(
        str(image_ref).encode("utf-8")
    ).hexdigest()[:16]

    output = (
        CACHE
        / f"memecoinist_{source_id}_{int(time.time())}.jpg"
    )

    # Keep generated image compact for Telegram.
    image.convert("RGB").save(
        output,
        "JPEG",
        quality=92,
        optimize=True,
    )

    return str(output)


def brand_response_image(image_ref):
    return apply_logo(image_ref)
