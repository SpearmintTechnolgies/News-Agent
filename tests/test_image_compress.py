"""Featured cards ship as compressed JPEG, not huge PNGs."""

from pathlib import Path

from PIL import Image

from newsagent_v2.image.compress import jpeg_quality, recompress_path, save_featured_image
from newsagent_v2.image.compositor import CompositionSpec, compose_card


def test_jpeg_quality_clamps(monkeypatch):
    monkeypatch.setenv("NEWSAGENT_IMAGE_JPEG_QUALITY", "200")
    assert jpeg_quality() == 95
    monkeypatch.setenv("NEWSAGENT_IMAGE_JPEG_QUALITY", "40")
    assert jpeg_quality() == 40


def test_save_featured_jpeg_is_much_smaller_than_png(tmp_path: Path):
    import os

    # True entropy: PNG stores nearly raw pixels; JPEG compresses hard.
    image = Image.frombytes("RGB", (1280, 720), os.urandom(1280 * 720 * 3))
    png = tmp_path / "card.png"
    jpg = tmp_path / "card.jpg"
    save_featured_image(image, png)
    meta = save_featured_image(image, jpg, quality=85)
    assert meta["format"] == "JPEG"
    assert meta["quality"] == 85
    assert jpg.stat().st_size < png.stat().st_size // 2
    assert Image.open(jpg).format == "JPEG"


def test_compose_card_writes_jpeg_when_dest_is_jpg(tmp_path: Path):
    photo = tmp_path / "photo.png"
    Image.new("RGB", (1280, 720), (40, 50, 60)).save(photo)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (120, 40), (255, 255, 255, 200)).save(logo)
    branded = tmp_path / "branded.jpg"
    composition = compose_card(
        photo,
        branded,
        CompositionSpec(
            headline="",
            logo_path=logo,
            logo_only=True,
            width=1280,
            height=720,
            output_name="branded.jpg",
        ),
    )
    assert branded.is_file()
    assert Image.open(branded).format == "JPEG"
    validation = composition["compositor_validation"]
    assert validation["valid_image_file"]
    assert validation["encode_format"] == "JPEG"
    assert validation["jpeg_quality"] == 85


def test_recompress_path(tmp_path: Path):
    src = tmp_path / "in.png"
    Image.new("RGB", (1280, 720), (10, 20, 30)).save(src)
    dest = tmp_path / "out.jpg"
    meta = recompress_path(src, dest, quality=80)
    assert meta["format"] == "JPEG"
    assert dest.is_file()
