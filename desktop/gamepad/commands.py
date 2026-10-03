"""摇杆语义解析 -> 命令打包。"""

from __future__ import annotations

from typing import TypedDict

DEADZONE = 4
_RANGE = 32767.0
_CENTER = 32768.0


class MoveCmd(TypedDict):
    name: str
    speed: int
    turn: int


class TurretCmd(TypedDict):
    name: str
    yaw: int
    pitch: int


def _axis(v16: int) -> int:
    """u16 摇杆值(0x8000 中位) -> -100~100 百分制，含死区。"""
    v = int(round((v16 - _CENTER) / _RANGE * 100.0))
    v = max(-100, min(100, v))
    if -DEADZONE < v < DEADZONE:
        return 0
    return v


def build_commands(lx: int, ly: int, rx: int, ry: int) -> list[dict]:
    """解析四轴 -> [MOVE, TURRET] 命令列表。"""
    cmds: list[dict] = [
        {"name": "MOVE", "speed": _axis(ly), "turn": _axis(lx)},
        {"name": "TURRET", "yaw": _axis(rx), "pitch": _axis(ry)},
    ]
    return cmds


def neutral_commands() -> list[dict]:
    return build_commands(0x8000, 0x8000, 0x8000, 0x8000)
