"""XInput 读取 + 16 字节 BLE HID 协议映射。"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

XINPUT_GAMEPAD_DPAD_UP = 0x0001
XINPUT_GAMEPAD_DPAD_DOWN = 0x0002
XINPUT_GAMEPAD_DPAD_LEFT = 0x0004
XINPUT_GAMEPAD_DPAD_RIGHT = 0x0008
XINPUT_GAMEPAD_START = 0x0010
XINPUT_GAMEPAD_BACK = 0x0020
XINPUT_GAMEPAD_LEFT_THUMB = 0x0040
XINPUT_GAMEPAD_RIGHT_THUMB = 0x0080
XINPUT_GAMEPAD_LEFT_SHOULDER = 0x0100
XINPUT_GAMEPAD_RIGHT_SHOULDER = 0x0200
XINPUT_GAMEPAD_GUIDE = 0x0400
XINPUT_GAMEPAD_A = 0x1000
XINPUT_GAMEPAD_B = 0x2000
XINPUT_GAMEPAD_X = 0x4000
XINPUT_GAMEPAD_Y = 0x8000

HAT_NAMES = {
    0: "中位", 1: "上", 2: "右上", 3: "右", 4: "右下",
    5: "下", 6: "左下", 7: "左", 8: "左上",
}

LAMPS_13 = [
    ("A", XINPUT_GAMEPAD_A), ("B", XINPUT_GAMEPAD_B),
    ("X", XINPUT_GAMEPAD_X), ("Y", XINPUT_GAMEPAD_Y),
    ("LB", XINPUT_GAMEPAD_LEFT_SHOULDER), ("RB", XINPUT_GAMEPAD_RIGHT_SHOULDER),
]
LAMPS_14 = [
    ("View", XINPUT_GAMEPAD_BACK), ("Menu", XINPUT_GAMEPAD_START),
    ("Xbox", XINPUT_GAMEPAD_GUIDE),
    ("LS", XINPUT_GAMEPAD_LEFT_THUMB), ("RS", XINPUT_GAMEPAD_RIGHT_THUMB),
]


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


def _load_xinput():
    if sys.platform != "win32":
        return None, False
    for name in ("XInput1_4.dll", "XInput1_3.dll", "XInput9_1_0.dll"):
        try:
            return ctypes.WinDLL(name), True
        except OSError:
            continue
    return None, False


_XINPUT, _DLL_FOUND = _load_xinput()
if _XINPUT is not None:
    try:
        _GET_STATE = _XINPUT[100]
        _GUIDE_OK = True
    except (AttributeError, OSError):
        _GET_STATE = _XINPUT.XInputGetState
        _GUIDE_OK = False
    _GET_STATE.restype = ctypes.c_ulong
    _GET_STATE.argtypes = [ctypes.c_ulong, ctypes.POINTER(XINPUT_STATE)]
else:
    _GET_STATE = None
    _GUIDE_OK = False

GUIDE_SUPPORTED = bool(_GUIDE_OK and _DLL_FOUND)


def available() -> bool:
    """本机能否调 XInput（Windows + DLL 存在）。"""
    return _GET_STATE is not None


def guide_supported() -> bool:
    return GUIDE_SUPPORTED


def find_controller() -> int | None:
    """轮询 0~3 号槽，返回首个在线槽位；无 DLL / 无手柄返回 None。"""
    if _GET_STATE is None:
        return None
    state = XINPUT_STATE()
    for i in range(4):
        try:
            if _GET_STATE(i, ctypes.byref(state)) == 0:
                return i
        except Exception:
            continue
    return None


@dataclass
class PadState:
    connected: bool
    slot: int | None = None
    packet: int = 0
    buttons: int = 0
    lt8: int = 0
    rt8: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    proto: bytes = b"\x00\x80\x00\x80\x00\x80\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00"


def hat_from_buttons(w: int) -> int:
    up = w & XINPUT_GAMEPAD_DPAD_UP
    down = w & XINPUT_GAMEPAD_DPAD_DOWN
    left = w & XINPUT_GAMEPAD_DPAD_LEFT
    right = w & XINPUT_GAMEPAD_DPAD_RIGHT
    if up and right:
        return 2
    if right and down:
        return 4
    if down and left:
        return 6
    if left and up:
        return 8
    if up:
        return 1
    if right:
        return 3
    if down:
        return 5
    if left:
        return 7
    return 0


def xinput_to_protocol(g: XINPUT_GAMEPAD) -> bytes:
    """XInput 状态 -> 项目 16 字节 BLE HID 报文（与 xinput_gui.py 逐字节一致）。

    XInput 摇杆为有符号 SHORT，中位 0；协议为 u16 偏置码，中位 0x8000。
    """
    lx = (int(g.sThumbLX) + 32768) & 0xFFFF
    ly = (int(g.sThumbLY) + 32768) & 0xFFFF
    rx = (int(g.sThumbRX) + 32768) & 0xFFFF
    ry = (int(g.sThumbRY) + 32768) & 0xFFFF
    lt = min(1023, (int(g.bLeftTrigger) * 1023 + 127) // 255)
    rt = min(1023, (int(g.bRightTrigger) * 1023 + 127) // 255)
    w = int(g.wButtons)
    hat = hat_from_buttons(w)
    b13 = 0
    if w & XINPUT_GAMEPAD_A:
        b13 |= 0x01
    if w & XINPUT_GAMEPAD_B:
        b13 |= 0x02
    if w & XINPUT_GAMEPAD_X:
        b13 |= 0x08
    if w & XINPUT_GAMEPAD_Y:
        b13 |= 0x10
    if w & XINPUT_GAMEPAD_LEFT_SHOULDER:
        b13 |= 0x40
    if w & XINPUT_GAMEPAD_RIGHT_SHOULDER:
        b13 |= 0x80
    b14 = 0
    if w & XINPUT_GAMEPAD_BACK:
        b14 |= 0x04
    if w & XINPUT_GAMEPAD_START:
        b14 |= 0x08
    if w & XINPUT_GAMEPAD_GUIDE:
        b14 |= 0x10
    if w & XINPUT_GAMEPAD_LEFT_THUMB:
        b14 |= 0x20
    if w & XINPUT_GAMEPAD_RIGHT_THUMB:
        b14 |= 0x40
    return bytes([
        lx & 0xFF, (lx >> 8) & 0xFF,
        ly & 0xFF, (ly >> 8) & 0xFF,
        rx & 0xFF, (rx >> 8) & 0xFF,
        ry & 0xFF, (ry >> 8) & 0xFF,
        lt & 0xFF, (lt >> 8) & 0xFF,
        rt & 0xFF, (rt >> 8) & 0xFF,
        hat, b13, b14, 0x00,
    ])


_NEUTRAL_PROTO = bytes([0x00, 0x80] * 4 + [0x00, 0x00] * 2 + [0x00, 0x00, 0x00, 0x00])


def neutral_protocol() -> bytes:
    """无手柄时的中位报文：摇杆 0x8000，扳机 0，按键 0。"""
    return _NEUTRAL_PROTO


class XInputReader:
    """有状态读取器：记住槽位，掉线自动重找。仅限单轮询线程使用，非线程安全。"""

    def __init__(self) -> None:
        self.slot: int | None = find_controller()
        self._state = XINPUT_STATE()

    def poll(self) -> PadState:
        if _GET_STATE is None:
            return PadState(connected=False, proto=neutral_protocol())
        slots = [self.slot] if self.slot is not None else []
        slots += [i for i in range(4) if i not in slots]
        for idx in slots:
            try:
                rc = _GET_STATE(idx, ctypes.byref(self._state))
            except Exception:
                continue
            if rc == 0:
                self.slot = idx
                g = self._state.Gamepad
                return PadState(
                    connected=True,
                    slot=idx,
                    packet=int(self._state.dwPacketNumber),
                    buttons=int(g.wButtons),
                    lt8=int(g.bLeftTrigger),
                    rt8=int(g.bRightTrigger),
                    lx=int(g.sThumbLX),
                    ly=int(g.sThumbLY),
                    rx=int(g.sThumbRX),
                    ry=int(g.sThumbRY),
                    proto=xinput_to_protocol(g),
                )
        self.slot = None
        return PadState(connected=False, proto=neutral_protocol())


def parse_protocol(proto: bytes) -> dict:
    """16 字节报文 -> UI / /command 共用的解析字典。

    长度异常直接抛错由调用方处理；调用方如需容错请自行补中位。
    """
    p = bytes(proto)
    if len(p) != 16:
        raise ValueError(f"protocol must be 16 bytes, got {len(p)}")
    lx = int.from_bytes(p[0:2], "little")
    ly = int.from_bytes(p[2:4], "little")
    rx = int.from_bytes(p[4:6], "little")
    ry = int.from_bytes(p[6:8], "little")
    lt = int.from_bytes(p[8:10], "little")
    rt = int.from_bytes(p[10:12], "little")
    return {
        "raw_hex": p.hex().upper(),
        "lx": lx, "ly": ly, "rx": rx, "ry": ry,
        "lt": lt, "rt": rt,
        "hat": p[12], "btn": p[13], "sys": p[14], "share": p[15],
    }
