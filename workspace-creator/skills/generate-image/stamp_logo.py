#!/usr/bin/env python3
"""Stamp the project logo onto a feature JPEG (Pillow, no ImageMagick).

Default placement matches Coinnetwork cards: top-left watermark.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PIL import Image


def stamp_logo(
    image_path: Path,
    logo_path: Path,
    *,
    gravity: str = "northwest",
    margin: int = 36,
    max_width_frac: float = 0.16,
) -> None:
    base = Image.open(image_path).convert("RGB")
    logo = Image.open(logo_path).convert("RGBA")

    max_w = max(80, int(base.width * max_width_frac))
    if logo.width > max_w:
        ratio = max_w / logo.width
        logo = logo.resize(
            (max_w, max(1, int(logo.height * ratio))),
            Image.Resampling.LANCZOS,
        )

    g = (gravity or "northwest").lower()
    if g in ("northwest", "nw", "top-left", "topleft"):
        xy = (margin, margin)
    elif g in ("northeast", "ne", "top-right", "topright"):
        xy = (base.width - logo.width - margin, margin)
    elif g in ("southwest", "sw", "bottom-left", "bottomleft"):
        xy = (margin, base.height - logo.height - margin)
    else:
        xy = (base.width - logo.width - margin, base.height - logo.height - margin)

    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.paste(logo, xy, logo)
    out = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
    tmp = str(image_path) + ".stamp.tmp.jpg"
    out.save(tmp, format="JPEG", quality=92, optimize=True)
    os.replace(tmp, image_path)


def main() -> int:
    p = argparse.ArgumentParser(description="Stamp brand logo onto feature.jpg")
    p.add_argument("--image", required=True)
    p.add_argument("--logo", required=True)
    p.add_argument("--gravity", default="northwest")
    p.add_argument("--margin", type=int, default=36)
    args = p.parse_args()
    image = Path(args.image)
    logo = Path(args.logo)
    if not image.is_file():
        print(f"STAMP_FAIL: image missing {image}", file=sys.stderr)
        return 1
    if not logo.is_file():
        print(f"STAMP_FAIL: logo missing {logo}", file=sys.stderr)
        return 1
    try:
        stamp_logo(image, logo, gravity=args.gravity, margin=args.margin)
    except Exception as e:
        print(f"STAMP_FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"STAMP_OK: {image}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
