"""手动白平衡和基础调色。

中性参数是空操作，直播路径仍只做 swscale。
非中性时：色温 / 色调 / 亮度 / 对比合成一张每通道查找表，饱和度另做一次混合。
查找表在滑条变化时生成，不在每帧里现算。

轴向（滑条与色盘一致）：
- 色温正方向偏暖（加红、减蓝），负方向偏冷
- 色调正方向偏品红（减绿），负方向偏绿
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageEnhance

# 滑条打到 ±100 时的通道增益。色温是白平衡主轴，幅度大于色调。
TEMP_GAIN = 0.55
TINT_GAIN = 0.40
# 品红时略抬红蓝，避免色调只把画面压暗。
TINT_RB_COUPLE = 0.25
# 亮度 ±100 → 曝光 ×0.50 .. ×1.50
BRIGHTNESS_SPAN = 0.50
# 对比 ±100 → ×0.40 .. ×1.60，绕 128 展开
CONTRAST_SPAN = 0.60
GAIN_MIN = 0.05
GAIN_MAX = 4.0
AXIS_MIN = -100
AXIS_MAX = 100
_MID = 128.0


def default_store_path() -> Path:
    return Path.home() / ".tindistance" / "color_grade.json"


def _clamp_axis(value: object) -> int:
    try:
        number = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if number < AXIS_MIN:
        return AXIS_MIN
    if number > AXIS_MAX:
        return AXIS_MAX
    return number


def _clamp8(value: float) -> int:
    if value <= 0:
        return 0
    if value >= 255:
        return 255
    return int(value + 0.5)


def _clamp_gain(value: float) -> float:
    if value < GAIN_MIN:
        return GAIN_MIN
    if value > GAIN_MAX:
        return GAIN_MAX
    return value


@dataclass(frozen=True)
class ColorGrade:
    temperature: int = 0
    tint: int = 0
    brightness: int = 0
    contrast: int = 0
    saturation: int = 0

    def clamped(self) -> ColorGrade:
        return ColorGrade(
            temperature=_clamp_axis(self.temperature),
            tint=_clamp_axis(self.tint),
            brightness=_clamp_axis(self.brightness),
            contrast=_clamp_axis(self.contrast),
            saturation=_clamp_axis(self.saturation),
        )

    @property
    def neutral(self) -> bool:
        grade = self.clamped()
        return (grade.temperature == 0 and grade.tint == 0
                and grade.brightness == 0 and grade.contrast == 0
                and grade.saturation == 0)

    @property
    def tone_neutral(self) -> bool:
        """查找表是否可以跳过。饱和度不在这张表里。"""
        grade = self.clamped()
        return (grade.temperature == 0 and grade.tint == 0
                and grade.brightness == 0 and grade.contrast == 0)


class PreparedGrade(NamedTuple):
    lut: list[int] | None
    saturation: float

    @property
    def active(self) -> bool:
        return self.lut is not None or abs(self.saturation - 1.0) >= 1e-6


def axis_from_unit(unit: float) -> int:
    """0 → -100，1 → +100。超出两端的拖动夹回端点。"""
    if unit < 0.0:
        unit = 0.0
    elif unit > 1.0:
        unit = 1.0
    return int(round(AXIS_MIN + (AXIS_MAX - AXIS_MIN) * unit))


def unit_from_axis(value: int) -> float:
    return (_clamp_axis(value) - AXIS_MIN) / (AXIS_MAX - AXIS_MIN)


def channel_gains(grade: ColorGrade) -> tuple[float, float, float]:
    grade = grade.clamped()
    temp = grade.temperature / 100.0
    tint = grade.tint / 100.0
    couple = 1.0 + TINT_GAIN * tint * TINT_RB_COUPLE
    red = (1.0 + TEMP_GAIN * temp) * couple
    green = 1.0 - TINT_GAIN * tint
    blue = (1.0 - TEMP_GAIN * temp) * couple
    return _clamp_gain(red), _clamp_gain(green), _clamp_gain(blue)


def exposure_of(brightness: int) -> float:
    return 1.0 + BRIGHTNESS_SPAN * (_clamp_axis(brightness) / 100.0)


def contrast_of(contrast: int) -> float:
    return 1.0 + CONTRAST_SPAN * (_clamp_axis(contrast) / 100.0)


def map_channel(value: int, gain: float, exposure: float, contrast: float) -> int:
    """先白平衡和曝光（乘），再绕中灰拉对比。"""
    linear = value * gain * exposure
    return _clamp8((linear - _MID) * contrast + _MID)


def _build_lut(grade: ColorGrade) -> list[int]:
    gains = channel_gains(grade)
    exposure = exposure_of(grade.brightness)
    contrast = contrast_of(grade.contrast)
    lut = [0] * 768
    for band, gain in enumerate(gains):
        base = band * 256
        for level in range(256):
            lut[base + level] = map_channel(level, gain, exposure, contrast)
    return lut


def prepare(grade: ColorGrade) -> PreparedGrade:
    grade = grade.clamped()
    lut = None if grade.tone_neutral else _build_lut(grade)
    saturation = 1.0 + grade.saturation / 100.0
    return PreparedGrade(lut, saturation)


def apply_grade(img: Image.Image, prepared: PreparedGrade) -> Image.Image:
    """中性时返回原对象。非中性返回新图，不改调用方手里的那张。"""
    if not prepared.active:
        return img
    if img.mode != "RGB":
        img = img.convert("RGB")
    lut = prepared.lut
    if lut is not None:
        # 768 项按 R/G/B 分段。一次 point 比拆通道再合并快。
        img = img.point(lut)
    if abs(prepared.saturation - 1.0) >= 1e-6:
        img = ImageEnhance.Color(img).enhance(prepared.saturation)
    return img


def wb_field_image(width: int, height: int, base: int = 176) -> Image.Image:
    """色盘底图：中灰经过每个色温/色调点之后的颜色。和正式查找表同一套公式。"""
    if width < 2 or height < 2:
        raise ValueError("wb field needs at least 2x2")
    pixels: list[tuple[int, int, int]] = []
    level = _clamp8(float(base))
    for y in range(height):
        tint = axis_from_unit(1.0 - y / (height - 1))
        for x in range(width):
            grade = ColorGrade(temperature=axis_from_unit(x / (width - 1)), tint=tint)
            red, green, blue = channel_gains(grade)
            pixels.append((
                map_channel(level, red, 1.0, 1.0),
                map_channel(level, green, 1.0, 1.0),
                map_channel(level, blue, 1.0, 1.0),
            ))
    image = Image.new("RGB", (width, height))
    image.putdata(pixels)
    return image


def load_state(path: Path) -> tuple[ColorGrade, bool]:
    """返回 (调色, 面板是否展开)。文件缺失或损坏时用中性、展开。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return ColorGrade(), True
    if not isinstance(data, dict):
        return ColorGrade(), True
    grade = ColorGrade(
        temperature=data.get("temperature", 0),
        tint=data.get("tint", 0),
        brightness=data.get("brightness", 0),
        contrast=data.get("contrast", 0),
        saturation=data.get("saturation", 0),
    ).clamped()
    open_panel = data.get("open", True)
    return grade, bool(open_panel) if isinstance(open_panel, bool) else True


def save_state(grade: ColorGrade, open_panel: bool, path: Path) -> None:
    grade = grade.clamped()
    payload = {
        "temperature": grade.temperature,
        "tint": grade.tint,
        "brightness": grade.brightness,
        "contrast": grade.contrast,
        "saturation": grade.saturation,
        "open": bool(open_panel),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
