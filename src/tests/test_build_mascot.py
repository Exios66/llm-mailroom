"""Pixel geometry and asset export contracts for the Fumi builder."""

import re
import xml.etree.ElementTree as ET

import pytest
from PIL import Image, ImageColor

from scripts import build_mascot as mascot


@pytest.mark.parametrize("point", [(-2, -2), (97, 117), (0, 0), (95, 115)])
def test_layer_keeps_pixels_inside_drawing_margin(point):
    layer = mascot.Layer()
    layer.set(*point, "#123456")
    assert layer.px == {point: "#123456"}


@pytest.mark.parametrize("point", [(-3, 0), (98, 0), (0, -3), (0, 118)])
def test_layer_discards_pixels_outside_drawing_margin(point):
    layer = mascot.Layer()
    layer.set(*point, "#123456")
    assert not layer.px


def test_rect_is_inclusive_and_merge_overwrites_without_aliasing():
    layer, overlay = mascot.Layer(), mascot.Layer()
    layer.rect(2, 3, 3, 4, "#112233")
    overlay.set(3, 4, "#ffffff")
    layer.merge(overlay)
    overlay.set(3, 4, "#000000")
    assert layer.px == {
        (2, 3): "#112233", (3, 3): "#112233",
        (2, 4): "#112233", (3, 4): "#ffffff",
    }


def test_symmetry_reflects_about_character_axis():
    layer = mascot.Layer()
    layer.sym([(20, 30), (47, 31)], "#123456")
    assert set(layer.px) == {(20, 30), (75, 30), (47, 31), (48, 31)}


@pytest.mark.parametrize("mirror", [False, True])
def test_sprite_preserves_transparent_holes_and_mirrors_each_row(mirror):
    layer = mascot.Layer()
    layer.set(11, 20, "#aabbcc")
    layer.sprite(10, 20, ["R.B", "B"], {"R": "#ff0000", "B": "#0000ff"}, mirror)
    assert layer.px == {
        (10, 20): "#0000ff" if mirror else "#ff0000",
        (11, 20): "#aabbcc",
        (12, 20): "#ff0000" if mirror else "#0000ff",
        (10, 21): "#0000ff",
    }


def test_outline_adds_only_one_orthogonal_border_and_preserves_fill():
    layer = mascot.Layer()
    layer.rect(10, 10, 11, 10, "#ffffff")
    layer.outline("#000000")
    assert layer.px == {
        (10, 10): "#ffffff", (11, 10): "#ffffff",
        (9, 10): "#000000", (12, 10): "#000000",
        (10, 9): "#000000", (11, 9): "#000000",
        (10, 11): "#000000", (11, 11): "#000000",
    }


def test_ellipse_uses_pixel_centers_and_optional_mask():
    layer = mascot.Layer()
    layer.ellipse(2, 2, 1, 1, "#ffffff", where=lambda x, y: x < 2)
    assert set(layer.px) == {(1, 1), (1, 2)}


def test_body_recolors_uniform_at_shirt_skirt_boundary(tmp_path, monkeypatch):
    base = Image.new("RGBA", (62, 107))
    for x, color in enumerate(mascot.UNIFORM_SRC):
        for y in (75, 76, 79, 80):
            base.putpixel((x, y), ImageColor.getcolor(color, "RGBA"))
    base.putpixel((0, 10), (1, 2, 3, 0))
    base.putpixel((1, 10), (4, 5, 6, 255))
    source = tmp_path / "base.png"
    base.save(source)
    monkeypatch.setattr(mascot, "BASE", source)

    body = mascot.draw_body()

    for x in range(3):
        assert body.px[(mascot.SX + x, mascot.SY + 75)] == mascot.SHIRT[x]
        assert body.px[(mascot.SX + x, mascot.SY + 76)] == mascot.SKIRT[x]
        assert body.px[(mascot.SX + x, mascot.SY + 79)] == "#ffffff"
        assert body.px[(mascot.SX + x, mascot.SY + 80)] == mascot.RED
    assert (mascot.SX, mascot.SY + 10) not in body.px
    assert body.px[(mascot.SX + 1, mascot.SY + 10)] == "#040506"


def test_missing_base_sprite_fails_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(mascot, "BASE", tmp_path / "missing.png")
    with pytest.raises(FileNotFoundError):
        mascot.draw_body()


def test_blink_overlay_covers_both_characters_without_repainting_body():
    assert not mascot.draw_eyes(False).px
    blink = mascot.draw_eyes(True)
    for x in (15, 34):
        assert blink.px[(mascot.SX + x, mascot.SY + 37)] == mascot.SKIN
        assert blink.px[(mascot.SX + x, mascot.SY + 41)] == mascot.LASH
    assert blink.px[(mascot.SX + mascot.OWL_X + 3, mascot.SY + mascot.OWL_Y + 5)] == mascot.OWL["f"]
    assert blink.px[(mascot.SX + mascot.OWL_X + 3, mascot.SY + mascot.OWL_Y + 6)] == mascot.OWL["E"]
    assert (57, 64) not in blink.px  # satchel strap stays untouched


def test_animation_windows_cover_exact_ticks_in_one_loop():
    states = [mascot.state(t) for t in range(32)]
    assert [t for t, s in enumerate(states) if s["blink"]] == [20, 21]
    assert [t for t, s in enumerate(states) if s["bubble"]] == list(range(6, 16))
    assert [s["bob"] for s in states] == ([0] * 4 + [1] * 4) * 4
    assert all(sum(s["sparks"]) == 1 for s in states)
    assert [states[t]["sparks"].index(True) for t in (0, 4, 8, 12, 16)] == [0, 3, 2, 1, 0]


@pytest.mark.parametrize("tick,bob,blink,bubble", [(0, 0, False, False), (6, 1, False, True), (16, 0, False, False), (20, 1, True, False), (22, 1, False, False)])
def test_composition_moves_body_and_eyes_but_keeps_extras_fixed(monkeypatch, tick, bob, blink, bubble):
    def body():
        layer = mascot.Layer()
        layer.set(40, 40, "#123456")
        return layer

    def eyes(closed):
        layer = mascot.Layer()
        if closed:
            layer.set(40, 40, "#654321")
        return layer

    monkeypatch.setattr(mascot, "draw_body", body)
    monkeypatch.setattr(mascot, "draw_eyes", eyes)
    frame = mascot.compose(tick)
    assert frame.px[(40, 40 + bob)] == ("#654321" if blink else "#123456")
    assert (40, 41 - bob) not in frame.px
    assert ((mascot.BUBBLE_X, mascot.BUBBLE_Y) in frame.px) is bubble
    assert all(point in frame.px for point in mascot.SPARKS)


def test_raster_export_clips_margin_and_scales_without_blurring():
    layer = mascot.Layer()
    layer.pts([(-1, 0), (96, 0), (0, 116)], "#ffffff")
    layer.set(95, 115, "#123456")
    image = mascot.to_image(layer, 3)
    assert image.mode == "RGBA"
    assert image.size == (288, 348)
    assert image.getbbox() == (285, 345, 288, 348)
    assert all(
        image.getpixel((x, y)) == (18, 52, 86, 255)
        for x in range(285, 288) for y in range(345, 348)
    )
    assert image.getpixel((0, 0)) == (0, 0, 0, 0)


def test_svg_runs_preserve_holes_colors_and_row_boundaries():
    layer = mascot.Layer()
    layer.rect(-2, 0, 0, 0, "#ff0000")
    layer.set(2, 0, "#ff0000")
    layer.set(0, 1, "#ff0000")
    layer.set(1, 1, "#0000ff")
    paths = ET.fromstring("<svg>" + mascot.rects(layer) + "</svg>")
    assert len(paths) == 2
    decoded = {}
    for path in paths:
        for x, y, width, back in re.findall(r"M(-?\d+) (-?\d+)h(\d+)v1h-(\d+)z", path.attrib["d"]):
            assert width == back
            for dx in range(int(width)):
                decoded[(int(x) + dx, int(y))] = path.attrib["fill"]
    assert decoded == layer.px
    assert mascot.rects(mascot.Layer()) == ""


def test_svg_is_accessible_self_contained_and_supports_reduced_motion():
    svg = ET.fromstring(mascot.build_svg())
    ns = {"s": "http://www.w3.org/2000/svg"}
    assert svg.attrib["viewBox"] == "0 0 96 116"
    assert svg.attrib["role"] == "img"
    title = svg.find("s:title", ns)
    assert svg.attrib["aria-labelledby"] == title.attrib["id"]
    assert "Fumi" in title.text
    assert not svg.findall(".//s:script", ns)
    assert not svg.findall(".//s:image", ns)
    css = svg.find("s:style", ns).text
    assert "@media (prefers-reduced-motion:reduce)" in css
    assert "animation:none!important" in css
    assert "animation:ec 3.2s" in css
    assert len(svg.findall("s:g", ns)) == 6  # four sparkles, body, bubble


@pytest.fixture(scope="module")
def exported_assets(tmp_path_factory):
    root = tmp_path_factory.mktemp("mascot-export")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mascot, "ROOT", root)
        patch.setattr(mascot, "OUT_DIR", root / "docs" / "assets" / "mascot")
        # BASE deliberately remains the checked-in native sprite.
        mascot.main()
    return root


def test_main_exports_all_assets_and_identical_deployable_copies(exported_assets):
    docs = exported_assets / "docs" / "assets" / "mascot"
    landing = exported_assets / "landing" / "assets" / "mascot"
    assert {p.name for p in docs.iterdir()} == {
        "fumi.svg", "fumi.png", "fumi-icon.png", "fumi.gif", "fumi-sheet.png",
    }
    assert {p.name for p in landing.iterdir()} == {"fumi.svg", "fumi.gif", "fumi-icon.png"}
    for copy in landing.iterdir():
        assert copy.read_bytes() == (docs / copy.name).read_bytes()
    for name, size in [("fumi.png", (576, 696)), ("fumi-icon.png", (256, 256)), ("fumi-sheet.png", (1536, 928))]:
        with Image.open(docs / name) as image:
            assert image.size == size
            assert image.mode == "RGBA"
            assert image.getchannel("A").getextrema() == (0, 255)


def test_gif_preserves_loop_duration_transparency_and_changing_frames(exported_assets):
    with Image.open(exported_assets / "docs/assets/mascot/fumi.gif") as gif:
        assert gif.size == (384, 464)
        assert gif.info["loop"] == 0
        duration, frames = 0, set()
        for frame in range(gif.n_frames):
            gif.seek(frame)
            duration += gif.info["duration"]
            assert gif.disposal_method == 2
            assert gif.convert("RGBA").getpixel((0, 0))[3] == 0
            frames.add(gif.convert("RGBA").tobytes())
        # Pillow may coalesce identical adjacent ticks into one longer frame.
        assert duration == 3200
        assert len(frames) > 4


@pytest.mark.parametrize("tick", [0, 6, 15, 16, 20, 21, 22, 31])
def test_frame_sheet_contains_timeline_frames_in_row_major_order(exported_assets, tick):
    with Image.open(exported_assets / "docs/assets/mascot/fumi-sheet.png") as sheet:
        x, y = tick % 8 * 192, tick // 8 * 232
        actual = sheet.crop((x, y, x + 192, y + 232))
        assert actual.tobytes() == mascot.to_image(mascot.compose(tick), 2).tobytes()
