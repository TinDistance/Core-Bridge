"""摇杆语义解析 -> 命令打包（含 V1.3 坦克串口命令）。

V1.3（坦克小车串口控制协议定义文档 V1.3）：
    左摇杆 -> 履带差速（TRACK_L/R，-100~100）
    右摇杆 -> 炮塔持续旋转（TURRET_YAW_ROT / TURRET_PITCH_ROT，0/1/2）
    A -> 开火 FIRE（按住=1，STM32 上升沿单次触发）
    B -> 后臂顺时针(1)，Y -> 后臂逆时针(2)，同时按/都不按=停
    十字键右 -> 急停：按住 2 秒锁存，锁存后全零+EMERGENCY_STOP=1；
              十字键左或 Start 解除
"""

from __future__ import annotations

from typing import TypedDict

DEADZONE = 4
_RANGE = 32767.0
_CENTER = 32768.0

TURRET_THRESHOLD = 30
ESTOP_HOLD_S = 2.0

BTN13_A = 0x01
BTN13_B = 0x02
BTN13_Y = 0x10
SYS_START = 0x08

_HAT_RIGHT = (2, 3, 4)
_HAT_LEFT = (6, 7, 8)


class MoveCmd(TypedDict):
    name: str
    speed: int
    turn: int


class TurretCmd(TypedDict):
    name: str
    yaw: int
    pitch: int


class TankCmd(TypedDict):
    track_l: int
    track_r: int
    yaw_rot: int
    pitch_rot: int
    rear_rot: int
    fire: int
    estop: int


def _axis(v16: int) -> int:
    """u16 摇杆值(0x8000 中位) -> -100~100 百分制，含死区。"""
    v = int(round((v16 - _CENTER) / _RANGE * 100.0))
    v = max(-100, min(100, v))
    if -DEADZONE < v < DEADZONE:
        return 0
    return v


def build_commands(lx: int, ly: int, rx: int, ry: int) -> list[dict]:
    """解析四轴 -> [MOVE, TURRET] 命令列表（兼容旧链路，面板/HTTP 用）。"""
    cmds: list[dict] = [
        {"name": "MOVE", "speed": _axis(ly), "turn": _axis(lx)},
        {"name": "TURRET", "yaw": _axis(rx), "pitch": _axis(ry)},
    ]
    return cmds


def neutral_commands() -> list[dict]:
    return build_commands(0x8000, 0x8000, 0x8000, 0x8000)


def neutral_tank(estop: int = 0) -> TankCmd:
    return TankCmd(track_l=0, track_r=0, yaw_rot=0, pitch_rot=0,
                   rear_rot=0, fire=0, estop=estop)


def _clamp100(v: int) -> int:
    return max(-100, min(100, int(v)))


def mix_tracks(speed: int, turn: int) -> tuple[int, int]:
    """差速混合：左=速度+转向，右=速度-转向（右打杆=右转=左快右慢）。"""
    return _clamp100(speed + turn), _clamp100(speed - turn)


def rot_from_axis(pct: int, threshold: int = TURRET_THRESHOLD) -> int:
    """百分制轴 -> 持续旋转命令：正=1，负=2，否则停。"""
    if pct > threshold:
        return 1
    if pct < -threshold:
        return 2
    return 0


def rear_from_btn13(b13: int) -> int:
    """B=顺时针(1)，Y=逆时针(2)，同时按/都不按=停。"""
    cw = bool(b13 & BTN13_B)
    ccw = bool(b13 & BTN13_Y)
    if cw and not ccw:
        return 1
    if ccw and not cw:
        return 2
    return 0


def fire_from_btn13(b13: int) -> int:
    return 1 if b13 & BTN13_A else 0


def hat_hold_right(hat: int) -> bool:
    return hat in _HAT_RIGHT


def hat_hold_left(hat: int) -> bool:
    return hat in _HAT_LEFT


def release_pressed(hat: int, sys: int) -> bool:
    return hat_hold_left(hat) or bool(sys & SYS_START)


class EstopLatch:
    """急存锁存：十字键右按住 ESTOP_HOLD_S 秒后锁存，左键/Start 解除。"""

    def __init__(self, hold_s: float = ESTOP_HOLD_S) -> None:
        self.hold_s = float(hold_s)
        self.latched = False
        self._hold_start: float | None = None

    def update(self, held: bool, release: bool, now: float) -> bool:
        if release:
            self.latched = False
            self._hold_start = None
            return self.latched
        if self.latched:
            return True
        if held:
            if self._hold_start is None:
                self._hold_start = now
            if now - self._hold_start >= self.hold_s:
                self.latched = True
                self._hold_start = None
        else:
            self._hold_start = None
        return self.latched

    def progress(self, now: float) -> float:
        if self.latched:
            return 1.0
        if self._hold_start is None:
            return 0.0
        return max(0.0, min(1.0, (now - self._hold_start) / self.hold_s))


class TankCommandBuilder:
    """u16 四轴 + hat/btn/sys -> V1.3 TankCmd（含急停锁存状态）。"""

    def __init__(self, hold_s: float = ESTOP_HOLD_S) -> None:
        self.estop = EstopLatch(hold_s)

    def update(self, lx: int, ly: int, rx: int, ry: int,
               hat: int, btn13: int, sys: int, now: float) -> TankCmd:
        latched = self.estop.update(hat_hold_right(hat),
                                    release_pressed(hat, sys), now)
        if latched:
            return TankCmd(track_l=0, track_r=0, yaw_rot=0, pitch_rot=0,
                           rear_rot=0, fire=0, estop=1)
        speed, turn = _axis(ly), _axis(lx)
        track_l, track_r = mix_tracks(speed, turn)
        return TankCmd(
            track_l=track_l,
            track_r=track_r,
            yaw_rot=rot_from_axis(_axis(rx)),
            pitch_rot=rot_from_axis(_axis(ry)),
            rear_rot=rear_from_btn13(btn13),
            fire=fire_from_btn13(btn13),
            estop=0,
        )
