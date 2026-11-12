"""白平衡查找表、色盘底图、以及图传上的调色盘。"""
from __future__ import annotations

import tkinter as tk

from PIL import Image

from desktop.modules.color_panel import ColorGradePanel
from desktop.streaming.color_grade import (
    ColorGrade,
    apply_grade,
    axis_from_unit,
    channel_gains,
    contrast_of,
    exposure_of,
    load_state,
    map_channel,
    prepare,
    save_state,
    wb_field_image,
)
from desktop.streaming.h264_viewer import H264Viewer


def test_neutral_grade_does_not_copy():
    image = Image.new("RGB", (2, 2), (10, 20, 30))
    assert apply_grade(image, prepare(ColorGrade())) is image
    assert not prepare(ColorGrade()).active


def test_warm_lifts_red_and_drops_blue():
    image = Image.new("RGB", (1, 1), (128, 128, 128))
    red, green, blue = apply_grade(
        image, prepare(ColorGrade(temperature=100))).getpixel((0, 0))
    assert red > 128
    assert blue < 128
    assert green == 128
    assert image.getpixel((0, 0)) == (128, 128, 128)


def test_cool_is_the_opposite_of_warm():
    image = Image.new("RGB", (1, 1), (128, 128, 128))
    red, _green, blue = apply_grade(
        image, prepare(ColorGrade(temperature=-100))).getpixel((0, 0))
    assert red < 128
    assert blue > 128


def test_magenta_tint_drops_green():
    image = Image.new("RGB", (1, 1), (128, 128, 128))
    _red, green, _blue = apply_grade(
        image, prepare(ColorGrade(tint=100))).getpixel((0, 0))
    assert green < 128


def test_brightness_and_contrast_move_the_right_way():
    bright = apply_grade(
        Image.new("RGB", (1, 1), (100, 100, 100)),
        prepare(ColorGrade(brightness=100)),
    ).getpixel((0, 0))
    assert bright[0] > 100

    dark = apply_grade(
        Image.new("RGB", (1, 1), (100, 100, 100)),
        prepare(ColorGrade(brightness=-100)),
    ).getpixel((0, 0))
    assert dark[0] < 100

    image = Image.new("RGB", (1, 2))
    image.putpixel((0, 0), (200, 200, 200))
    image.putpixel((0, 1), (40, 40, 40))
    graded = apply_grade(image, prepare(ColorGrade(contrast=100)))
    assert graded.getpixel((0, 0))[0] > 200
    assert graded.getpixel((0, 1))[0] < 40


def test_saturation_floor_is_gray():
    graded = apply_grade(
        Image.new("RGB", (1, 1), (200, 40, 40)),
        prepare(ColorGrade(saturation=-100)),
    ).getpixel((0, 0))
    assert abs(graded[0] - graded[1]) <= 2
    assert abs(graded[1] - graded[2]) <= 2


def test_lut_matches_channel_formula():
    grade = ColorGrade(temperature=40, tint=-15, brightness=10, contrast=-20)
    source = 90
    graded = apply_grade(
        Image.new("RGB", (1, 1), (source, source, source)),
        prepare(grade),
    ).getpixel((0, 0))
    exposure = exposure_of(grade.brightness)
    contrast = contrast_of(grade.contrast)
    expect = tuple(
        map_channel(source, gain, exposure, contrast)
        for gain in channel_gains(grade)
    )
    assert graded == expect


def test_axes_and_clamp():
    assert axis_from_unit(0) == -100
    assert axis_from_unit(0.5) == 0
    assert axis_from_unit(1) == 100
    assert axis_from_unit(-2) == -100
    assert axis_from_unit(3) == 100
    assert ColorGrade(temperature=500, tint=-999).clamped() == ColorGrade(
        temperature=100, tint=-100)


def test_wb_field_matches_the_axes():
    image = wb_field_image(21, 21, base=160)
    assert image.getpixel((10, 10)) == (160, 160, 160)
    left = image.getpixel((0, 10))
    right = image.getpixel((20, 10))
    assert right[0] > left[0]
    assert right[2] < left[2]
    top = image.getpixel((10, 0))
    bottom = image.getpixel((10, 20))
    assert top[1] < bottom[1]


def test_state_roundtrip(tmp_path):
    path = tmp_path / "grade.json"
    missing, open_panel = load_state(path)
    assert missing == ColorGrade()
    assert open_panel is True
    save_state(ColorGrade(temperature=12, tint=-3, saturation=4), False, path)
    loaded, open_panel = load_state(path)
    assert loaded == ColorGrade(temperature=12, tint=-3, saturation=4)
    assert open_panel is False
    path.write_text("{", encoding="utf-8")
    assert load_state(path) == (ColorGrade(), True)


def test_viewer_clamps_grade():
    viewer = H264Viewer("127.0.0.1", 9)
    assert viewer.color_grade == ColorGrade()
    viewer.set_color_grade(ColorGrade(temperature=150, tint=-5))
    assert viewer.color_grade == ColorGrade(temperature=100, tint=-5)


def test_panel_drives_grade_and_persists(tmp_path):
    path = tmp_path / "grade.json"
    seen: list[ColorGrade] = []
    root = tk.Tk()
    root.withdraw()
    try:
        panel = ColorGradePanel(root, on_change=seen.append, store_path=path)
        root.update_idletasks()
        panel._scales["brightness"].set(25)
        assert seen[-1].brightness == 25
        panel._on_pad(type("E", (), {"x": 159, "y": 52})())
        assert seen[-1].temperature == 100
        panel._reset_all()
        assert seen[-1] == ColorGrade()
        panel._scales["contrast"].set(-10)
        panel.destroy()

        again: list[ColorGrade] = []
        restored = ColorGradePanel(root, on_change=again.append, store_path=path)
        assert again[0].contrast == -10
        assert restored.grade().contrast == -10
        restored.destroy()
    finally:
        root.destroy()
