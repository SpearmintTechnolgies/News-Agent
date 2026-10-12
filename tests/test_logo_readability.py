"""A faint white wordmark must stay visible on a bright photo."""

from pathlib import Path

from PIL import Image, ImageDraw

from newsagent_v2.image.compositor import CompositionSpec, compose_card


def test_semitransparent_white_logo_stays_visible_on_a_bright_photo(tmp_path: Path):
    logo = tmp_path / "logo.png"
    mark = Image.new("RGBA", (240, 72), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)
    draw.ellipse((8, 12, 56, 60), fill=(40, 170, 230, 160))
    draw.rectangle((70, 24, 220, 48), fill=(255, 255, 255, 140))
    mark.save(logo)

    photo = tmp_path / "photo.png"
    Image.new("RGB", (1280, 720), (230, 220, 180)).save(photo)
    branded = tmp_path / "branded.png"
    composition = compose_card(
        photo,
        branded,
        CompositionSpec(
            headline="",
            logo_path=logo,
            logo_only=True,
            width=1280,
            height=720,
            output_name="branded.png",
        ),
    )
    box = composition["logo"]["box"]
    image = Image.open(branded).convert("RGB")
    pixels = image.load()
    white = 0
    for y in range(box["y0"], box["y1"]):
        for x in range(box["x0"], box["x1"]):
            r, g, b = pixels[x, y]
            if r > 230 and g > 230 and b > 230:
                white += 1
    assert white > 80
    assert composition["logo"]["height"] > 50
