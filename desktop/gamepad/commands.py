"""摇杆语义解析 -> 命令打包。

命令定义（随 POST /command 的 cmds 字段下发）：
- MOVE    {speed, turn}   左摇杆：前+/后-（ly 上为正），左-/右+（lx）
- TURRET  {yaw, pitch}    右摇杆：左-/右+（rx），下-/上+（ry）

数值：int 百分制 -100~100，中心 ±DEADZONE 归零，端点饱和。
"""

from __future__ import annotations

from typing import TypedDict

DEADZONE = 4  # 百分制，约 3% 死区，抗摇杆回中漂移
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
