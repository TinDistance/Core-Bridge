# -*- coding: utf-8 -*-
"""V1.3 坦克串口命令回归（pytest）。

覆盖桌面语义 -> 服务端编码 -> K230 校验的整条链：
  - 差速混合 / 炮塔阈值 / 后臂 B顺Y逆 / A开火
  - 急停锁存：右键按住 2s 锁存，左键/Start 解除
  - 服务端 V1.3 15 字节帧：布局 / CRC16-CCITT-FALSE 标准向量 /
    文档 §9.1 示例向量
  - K230 源码抽取校验：与服务端编码逐字节互认
"""
import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from desktop.gamepad import commands as C
from desktop.gamepad import xinput as XI
from server import command_udp as CU


def _k230_function(name: str, globs: dict | None = None):
    path = os.path.join(REPO, "k230", "rtp_push.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    wanted = {"crc16_ccitt_false", "valid_uart_frame"}
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name in wanted]
    if not fns:
        raise RuntimeError("V1.3 crc/valid functions not found")
    ns = dict(globs or {})
    exec(compile(ast.Module(body=fns, type_ignores=[]), path, "exec"), ns)
    if name not in ns:
        raise RuntimeError(f"{name} not found in k230/rtp_push.py")
    return ns[name]


k230_crc = _k230_function("crc16_ccitt_false")
k230_valid = _k230_function("valid_uart_frame")


def _u16(center_offset: int) -> int:
    return (0x8000 + center_offset) & 0xFFFF


def test_mix_tracks_differential():
    assert C.mix_tracks(100, 0) == (100, 100)
    assert C.mix_tracks(0, 100) == (100, -100)
    assert C.mix_tracks(50, 20) == (70, 30)
    assert C.mix_tracks(90, 30) == (100, 60)


def test_rot_threshold():
    assert C.rot_from_axis(31) == 1
    assert C.rot_from_axis(30) == 0
    assert C.rot_from_axis(-31) == 2
    assert C.rot_from_axis(0) == 0


def test_rear_b_cw_y_ccw():
    assert C.rear_from_btn13(0x02) == 1
    assert C.rear_from_btn13(0x10) == 2
    assert C.rear_from_btn13(0x02 | 0x10) == 0
    assert C.rear_from_btn13(0x00) == 0
    assert C.fire_from_btn13(0x01) == 1
    assert C.fire_from_btn13(0x00) == 0


def test_estop_latch_needs_two_seconds():
    latch = C.EstopLatch(hold_s=2.0)
    assert latch.update(True, False, 0.0) is False
    assert latch.update(True, False, 1.9) is False
    assert latch.update(True, False, 2.0) is True
    assert latch.update(False, False, 3.0) is True
    assert latch.update(False, True, 3.1) is False


def test_builder_estop_zeroes_everything():
    b = C.TankCommandBuilder()
    t = b.update(_u16(16000), _u16(16000), _u16(16000), _u16(16000),
                 3, 0x01 | 0x02, 0, 0.0)
    assert t["estop"] == 0
    t = b.update(_u16(16000), _u16(16000), _u16(16000), _u16(16000),
                 3, 0x01 | 0x02, 0, 2.5)
    assert dict(t) == {"track_l": 0, "track_r": 0, "yaw_rot": 0,
                       "pitch_rot": 0, "rear_rot": 0, "fire": 0, "estop": 1}


def test_crc16_standard_vector():
    assert CU.crc16_ccitt_false(b"123456789") == 0x29B1
    assert k230_crc(b"123456789") == 0x29B1


def test_doc_example_frame():
    tank = {"track_l": 100, "track_r": -100, "yaw_rot": 1, "pitch_rot": 1,
            "rear_rot": 1, "fire": 1, "estop": 0}
    frame = CU.build_tank_frame(tank)
    assert len(frame) == 15
    assert frame[:6] == bytes([0xAA, 0x55, 0x00, 0x64, 0xFF, 0x9C])
    assert frame[6:11] == bytes([1, 1, 1, 1, 0])
    assert frame[13:] == bytes([0x0D, 0x0A])
    assert k230_valid(frame) is True


def test_k230_accepts_server_frames():
    for tank in (
        {"track_l": 0, "track_r": 0, "yaw_rot": 0, "pitch_rot": 0,
         "rear_rot": 0, "fire": 0, "estop": 0},
        {"track_l": -100, "track_r": 100, "yaw_rot": 2, "pitch_rot": 2,
         "rear_rot": 2, "fire": 0, "estop": 0},
        {"track_l": 0, "track_r": 0, "yaw_rot": 0, "pitch_rot": 0,
         "rear_rot": 0, "fire": 0, "estop": 1},
    ):
        frame = CU.build_tank_frame(tank)
        assert k230_valid(frame) is True, tank
    bad = bytearray(CU.build_tank_frame(
        {"track_l": 0, "track_r": 0, "yaw_rot": 0, "pitch_rot": 0,
         "rear_rot": 0, "fire": 0, "estop": 0}))
    bad[5] ^= 0x01
    assert k230_valid(bytes(bad)) is False
    assert k230_valid(bytes(bad[:14])) is False


def test_server_raw_fallback_matches_desktop():
    g = XI.XINPUT_GAMEPAD()
    g.sThumbLX = 16000
    g.sThumbLY = 16000
    g.sThumbRX = 16000
    g.sThumbRY = 0
    g.wButtons = XI.XINPUT_GAMEPAD_A | XI.XINPUT_GAMEPAD_RIGHT_SHOULDER
    proto = XI.xinput_to_protocol(g)
    parsed = XI.parse_protocol(proto)
    builder = C.TankCommandBuilder()
    expect = builder.update(parsed["lx"], parsed["ly"], parsed["rx"],
                            parsed["ry"], parsed["hat"], parsed["btn"],
                            parsed["sys"], 0.0)
    got = CU._tank_from_raw(parsed["raw_hex"])
    assert got == dict(expect)


def test_neutral_frame_is_all_zero_with_valid_crc():
    frame = CU.build_uart_frame(
        {"raw_hex": XI.neutral_protocol().hex().upper(), "cmds": None})
    assert frame is not None and len(frame) == 15
    assert frame[2:11] == bytes(9)
    assert k230_valid(frame) is True


@pytest.mark.parametrize("hat", [2, 3, 4])
def test_server_raw_hat_right_is_immediate_estop(hat):
    p = bytearray(XI.neutral_protocol())
    p[12] = hat
    tank = CU._tank_from_raw(bytes(p).hex().upper())
    assert tank is not None and tank["estop"] == 1
    frame = CU.build_uart_frame(
        {"raw_hex": bytes(p).hex().upper(), "cmds": None})
    assert frame is not None and k230_valid(frame) is True
