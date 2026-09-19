"""
Deterministic CoinNetwork card compositor.

The model never renders the headline or logo. Python draws overlay type
on a 16:9 master. Missing production logos are not fabricated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from newsagent_v2.image.contract import (
    CARD_HEIGHT,
    CARD_WIDTH,
    COMPOSITOR_VERSION,
    IMAGE_COMPOSITION_SCHEMA_VERSION,
)
from newsagent_v2.image.logo_derive import ensure_compositor_logo
from newsagent_v2.image.validate import file_sha256, refuse_overwrite

MARGIN = 48
LOGO_MAX_HEIGHT = 56
LOGO_MAX_WIDTH = 240
LOGO_ONLY_MAX_HEIGHT = 40
LOGO_ONLY_MAX_WIDTH = 168
GRADIENT_HEIGHT = 300
HEADLINE_SIZE = 42
HEADLINE_MIN_SIZE = 26
HEADLINE_MAX_LINES = 3
CATEGORY_SIZE = 18
LINE_GAP = 10
CATEGORY_GAP = 14

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")

WINDOWS_FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\segoeuib.ttf"),
    Path(r"C:\Windows\Fonts\calibrib.ttf"),
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
    Path(r"C:\Windows\Fonts\calibri.ttf"),
    Path(r"C:\Windows\Fonts\arial.ttf"),
)

APPROVED_LOGO_RELATIVE_PATHS = (
    Path("brand") / "coinnetwork_logo.png",
    Path("brand") / "derived" / "coinnetwork_logo_white_transparent.png",
    Path("brand") / "coinnetwork-logo.png",
    Path("brand") / "logo.png",
    Path("assets") / "brand" / "coinnetwork_logo.png",
    Path("assets") / "coinnetwork_logo.png",
    Path("assets") / "logo.png",
    Path("src") / "newsagent_v2" / "image" / "fixtures" / "coinnetwork_logo.png",
)


class CompositorError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CompositionSpec:
    headline: str
    category_label: str | None = None
    logo_path: Path | None = None
    test_mode: bool = False
    width: int = CARD_WIDTH
    height: int = CARD_HEIGHT
    output_name: str = "card.png"
    logo_only: bool = False


def sanitize_overlay_text(text: str) -> str:
    if not isinstance(text, str):
        raise CompositorError("invalid_text", "overlay text must be a string")
    cleaned = CONTROL_RE.sub("", text)
    cleaned = BIDI_RE.sub("", cleaned)
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return " ".join(cleaned.split())


def format_category_label(label: str) -> str:
    return sanitize_overlay_text(label.replace("_", " ")).upper()


def discover_approved_logo(repo_root: Path) -> Path | None:
    for relative in APPROVED_LOGO_RELATIVE_PATHS:
        candidate = repo_root / relative
        if candidate.is_file():
            return candidate
    return None


def wrap_text(
    text: str,
    *,
    max_width: int,
    max_lines: int,
    measure: Callable[[str], int],
) -> list[str]:
    words = text.split()
    if not words:
        raise CompositorError("empty_headline", "headline is empty after sanitization")
    lines: list[str] = []
    current = ""
    for word in words:
        if measure(word) > max_width:
            raise CompositorError(
                "unbreakable_word",
                f"word exceeds overlay width: {word[:48]!r}",
            )
        candidate = word if not current else f"{current} {word}"
        if measure(candidate) <= max_width:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) >= max_lines:
            raise CompositorError(
                "headline_overflow",
                "headline does not fit the overlay without clipping",
            )
    if current:
        if len(lines) >= max_lines:
            raise CompositorError(
                "headline_overflow",
                "headline does not fit the overlay without clipping",
            )
        lines.append(current)
    return lines


def _load_font(size: int) -> ImageFont.ImageFont:
    for path in WINDOWS_FONT_CANDIDATES:
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _measure_with(font: ImageFont.ImageFont) -> Callable[[str], int]:
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def measure(text: str) -> int:
        bbox = probe.textbbox((0, 0), text, font=font, anchor="lt")
        return bbox[2] - bbox[0]

    return measure


def _fit_headline(text: str, max_width: int) -> tuple[list[str], ImageFont.ImageFont, int]:
    for size in range(HEADLINE_SIZE, HEADLINE_MIN_SIZE - 1, -2):
        font = _load_font(size)
        try:
            lines = wrap_text(
                text,
                max_width=max_width,
                max_lines=HEADLINE_MAX_LINES,
                measure=_measure_with(font),
            )
            return lines, font, size
        except CompositorError as exc:
            if exc.code not in {"headline_overflow", "unbreakable_word"}:
                raise
    raise CompositorError(
        "headline_overflow",
        "headline cannot be laid out at the minimum type size without clipping",
    )


def _draw_gradient(base: Image.Image) -> None:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    top = base.height - GRADIENT_HEIGHT
    for y in range(GRADIENT_HEIGHT):
        t = y / max(GRADIENT_HEIGHT - 1, 1)
        alpha = int(230 * (t ** 1.15))
        draw.line([(0, top + y), (base.width, top + y)], fill=(8, 10, 16, alpha))
    composed = Image.alpha_composite(base.convert("RGBA"), overlay)
    base.paste(composed.convert("RGB"))


def _inside_safe(
    box: tuple[int, int, int, int],
    width: int,
    height: int,
    *,
    slack: int = 0,
) -> bool:
    x0, y0, x1, y1 = box
    return (
        x0 >= MARGIN - slack
        and y0 >= MARGIN - slack
        and x1 <= width - MARGIN + slack
        and y1 <= height - MARGIN + slack
        and x0 < x1
        and y0 < y1
    )


def _subtle_logo_shadow(logo: Image.Image) -> Image.Image:
    alpha = logo.getchannel("A")
    if alpha.getextrema()[0] >= 250:
        return logo
    shadow = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda value: int(value * 0.28)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=1.2))
    pad = 3
    layered = Image.new("RGBA", (logo.width + pad, logo.height + pad), (0, 0, 0, 0))
    layered.paste(shadow, (2, 2), shadow)
    layered.paste(logo, (0, 0), logo)
    return layered


def _place_logo(
    canvas: Image.Image,
    logo_path: Path,
    *,
    max_width: int = LOGO_MAX_WIDTH,
    max_height: int = LOGO_MAX_HEIGHT,
) -> dict[str, Any]:
    placed_hash = file_sha256(logo_path)
    with Image.open(logo_path) as src:
        logo = src.convert("RGBA")
        ratio = min(max_width / logo.width, max_height / logo.height, 1.0)
        size = (max(1, int(logo.width * ratio)), max(1, int(logo.height * ratio)))
        logo = logo.resize(size, Image.Resampling.LANCZOS)
        placed = _subtle_logo_shadow(logo)
    x = canvas.width - MARGIN - placed.width
    y = MARGIN
    work = canvas.convert("RGBA")
    work.paste(placed, (x, y), placed)
    canvas.paste(work.convert("RGB"))
    box = (x, y, x + placed.width, y + placed.height)
    if not _inside_safe(box, canvas.width, canvas.height):
        raise CompositorError("logo_bounds", "logo placement is outside the safe area")
    after_hash = file_sha256(logo_path)
    if after_hash != placed_hash:
        raise CompositorError("logo_mutated", "compositor must not mutate the logo file being placed")
    return {
        "present": True,
        "path": str(logo_path),
        "sha256": placed_hash,
        "width": placed.width,
        "height": placed.height,
        "x": x,
        "y": y,
        "box": {"x0": x, "y0": y, "x1": x + placed.width, "y1": y + placed.height},
        "inside_safe_bounds": True,
        "master_unchanged": True,
        "fabricated": False,
        "blue_rectangle_restored": False,
    }


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> tuple[int, int, int, int]:
    x, y = xy
    draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0), anchor="lt")
    draw.text((x, y), text, font=font, fill=fill, anchor="lt")
    bbox = draw.textbbox((x, y), text, font=font, anchor="lt")
    return (bbox[0], bbox[1], bbox[2] + 2, bbox[3] + 2)


def compose_card(
    raw_path: Path,
    dest_path: Path,
    spec: CompositionSpec,
) -> dict[str, Any]:
    if spec.logo_only:
        headline = ""
        category = None
    else:
        headline = sanitize_overlay_text(spec.headline)
        if not headline:
            raise CompositorError("empty_headline", "headline is empty after sanitization")
        category = None
        if spec.category_label:
            category = format_category_label(spec.category_label) or None

    if spec.logo_path is None:
        if not spec.test_mode:
            raise CompositorError(
                "logo_required",
                "production composition requires an approved CoinNetwork logo asset; "
                "none is present in NewsAgent-V2. Pass logo_path or use test_mode.",
            )
        logo_meta = {
            "present": False,
            "path": None,
            "sha256": None,
            "required_asset": True,
            "fabricated": False,
            "test_mode": True,
            "inside_safe_bounds": None,
            "master_unchanged": None,
        }
    else:
        if not spec.logo_path.exists():
            raise CompositorError("logo_missing", f"logo file not found: {spec.logo_path}")
        logo_meta = None

    refuse_overwrite(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    master_logo_hash = None
    if spec.logo_path is not None:
        master_logo_hash = file_sha256(spec.logo_path)

    with Image.open(raw_path) as raw:
        raw = raw.convert("RGB")
        canvas = raw.resize((spec.width, spec.height), Image.Resampling.LANCZOS)

    raw_hash = file_sha256(raw_path)
    if not spec.logo_only:
        _draw_gradient(canvas)
    draw = ImageDraw.Draw(canvas)

    if spec.logo_path is not None:
        place_path, derive_meta = ensure_compositor_logo(spec.logo_path)
        if derive_meta is not None and derive_meta["master_sha256"] != master_logo_hash:
            raise CompositorError("logo_mutated", "logo derive mutated the approved master")
        max_w = LOGO_ONLY_MAX_WIDTH if spec.logo_only else LOGO_MAX_WIDTH
        max_h = LOGO_ONLY_MAX_HEIGHT if spec.logo_only else LOGO_MAX_HEIGHT
        logo_meta = _place_logo(canvas, place_path, max_width=max_w, max_height=max_h)
        logo_meta["required_asset"] = False
        logo_meta["fabricated"] = False
        logo_meta["test_mode"] = spec.test_mode
        if derive_meta is not None:
            logo_meta["master_path"] = derive_meta["master_path"]
            logo_meta["master_sha256"] = derive_meta["master_sha256"]
            logo_meta["derived_path"] = derive_meta["derived_path"]
            logo_meta["derived_sha256"] = derive_meta["derived_sha256"]
            logo_meta["has_alpha"] = True
            logo_meta["proportions_preserved"] = derive_meta["proportions_preserved"]
            logo_meta["master_unchanged"] = True
            if file_sha256(spec.logo_path) != master_logo_hash:
                raise CompositorError("logo_mutated", "compositor must not mutate the approved logo master")
        else:
            logo_meta["master_unchanged"] = file_sha256(spec.logo_path) == master_logo_hash

    lines: list[str] = []
    font_size = 0
    headline_boxes: list[tuple[int, int, int, int]] = []
    category_box = None
    if not spec.logo_only:
        text_width = spec.width - (MARGIN * 2) - 2
        lines, font, font_size = _fit_headline(headline, text_width)
        line_heights = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font, anchor="lt")
            line_heights.append(bbox[3] - bbox[1])
        block_height = sum(line_heights) + LINE_GAP * (len(lines) - 1)
        y = spec.height - MARGIN - block_height - 2
        if category:
            cat_font = _load_font(CATEGORY_SIZE)
            cat_bbox = draw.textbbox((0, 0), category, font=cat_font, anchor="lt")
            cat_h = cat_bbox[3] - cat_bbox[1]
            y = spec.height - MARGIN - block_height - 2
            cat_y = y - CATEGORY_GAP - cat_h
            if cat_y < spec.height - GRADIENT_HEIGHT + 24:
                raise CompositorError("headline_overflow", "category collides with the artwork safe area")
            category_box = _draw_text(
                draw,
                (MARGIN, cat_y),
                category,
                cat_font,
                (212, 175, 106),
            )
            if not _inside_safe(category_box, spec.width, spec.height, slack=12):
                raise CompositorError("category_bounds", "category label is outside the safe area")
        elif y < spec.height - GRADIENT_HEIGHT + 24:
            raise CompositorError("headline_overflow", "headline collides with the artwork safe area")

        if y < spec.height - GRADIENT_HEIGHT + 24:
            raise CompositorError("headline_overflow", "headline collides with the artwork safe area")

        cursor = y
        for line, line_height in zip(lines, line_heights):
            box = _draw_text(draw, (MARGIN, cursor), line, font, (250, 250, 252))
            headline_boxes.append(box)
            if not _inside_safe(box, spec.width, spec.height, slack=12):
                raise CompositorError("headline_bounds", "headline is outside the safe area")
            cursor += line_height + LINE_GAP

    if file_sha256(raw_path) != raw_hash:
        raise CompositorError("raw_mutated", "compositor must not mutate the raw artwork file")

    canvas.save(dest_path, format="PNG")
    if file_sha256(raw_path) != raw_hash:
        raise CompositorError("raw_mutated", "compositor must not mutate the raw artwork file")

    with Image.open(dest_path) as final:
        final.load()
        final_width, final_height = final.size
        final_format = (final.format or "PNG").upper()

    if final_width != spec.width or final_height != spec.height:
        raise CompositorError(
            "final_dimensions",
            f"final image is {final_width}x{final_height}, expected {spec.width}x{spec.height}",
        )

    compositor_validation = {
        "passed": True,
        "output_exactly_1280x720": final_width == 1280 and final_height == 720,
        "valid_image_file": final_format == "PNG",
        "approved_logo_loaded": bool(logo_meta and logo_meta.get("present")),
        "logo_inside_safe_bounds": None if logo_meta is None else logo_meta.get("inside_safe_bounds"),
        "headline_inside_safe_bounds": True if spec.logo_only else True,
        "no_text_clipping": True,
        "no_text_overflow": True,
        "line_count": len(lines),
        "line_count_ok": True if spec.logo_only else 1 <= len(lines) <= HEADLINE_MAX_LINES,
        "contrast_strategy": "none" if spec.logo_only else "bottom_gradient_plus_text_shadow",
        "logo_only": spec.logo_only,
        "headline_drawn": not spec.logo_only,
        "category_drawn": bool(category),
        "raw_immutable": True,
        "visual_ai_artifact_review": "human_required",
        "warnings": [],
    }
    if spec.test_mode and not compositor_validation["approved_logo_loaded"]:
        compositor_validation["warnings"].append(
            "test_mode composition has no production logo"
        )
        compositor_validation["passed"] = True
    elif not compositor_validation["approved_logo_loaded"]:
        compositor_validation["passed"] = False
        compositor_validation["warnings"].append("approved logo was not loaded")

    if not compositor_validation["line_count_ok"]:
        compositor_validation["passed"] = False

    return {
        "schema_version": IMAGE_COMPOSITION_SCHEMA_VERSION,
        "compositor_version": COMPOSITOR_VERSION,
        "raw_path": str(raw_path),
        "final_path": str(dest_path),
        "raw_sha256": raw_hash,
        "final_sha256": file_sha256(dest_path),
        "final_bytes": dest_path.stat().st_size,
        "width": spec.width,
        "height": spec.height,
        "headline": headline,
        "headline_lines": lines,
        "headline_font_size": font_size,
        "headline_boxes": [
            {"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]} for b in headline_boxes
        ],
        "category_label": category,
        "category_box": None
        if category_box is None
        else {
            "x0": category_box[0],
            "y0": category_box[1],
            "x1": category_box[2],
            "y1": category_box[3],
        },
        "logo": logo_meta,
        "raw_preserved": True,
        "test_mode": spec.test_mode,
        "compositor_validation": compositor_validation,
    }
