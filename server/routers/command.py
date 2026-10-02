"""手柄指令通道：桌面 XInput -> 16 字节 HID 协议 -> K230 轮询/UDP 推送。

协议（与 ESP32_to_Xbox / xinput_gui.py 一致）：
  [0:2] joyLHori u16LE 中值0x8000  [2:4] joyLVert  [4:6] joyRHori  [6:8] joyRVert
  [8:10] trigLT 10bit  [10:12] trigRT
  [12] 帽子 0中 1上 2右上 3右 4右下 5下 6左下 7左 8左上
  [13] A=0x01 B=0x02 X=0x08 Y=0x10 LB=0x40 RB=0x80
  [14] View=0x04 Menu=0x08 Xbox=0x10 LS=0x20 RS=0x40
  [15] Share=0x01

HTTP：
  POST /command  桌面推送最新包 {raw_hex, cmds?, slot?, packet?, client_ts?}
  GET  /command  桌面/调试轮询最新包（含解析字段 + cmds 命令 + age_ms）

UDP（K230 主链路，见 server/command_udp.py）：
  K230 0.5s 发注册包 "CQ"+hz，服务端按 hz（默认 150Hz）推送 v2 UART 帧，
  K230 收到后原样写 UART3，不再走 TCP 轮询。

UART 命令帧 v2（只含激活命令、无动作不发送）：
  [0]=0xAA [1]=0x55 [2]=N（条目数，1..8）
  { [id][len][payload...] }×N
  [末字节]=crc8（从[0]到payload末所有字节异或）
  - 0x01 MOVE，len=2：payload = speed i8, turn i8（补码，-100~100）
  - 0x02 TURRET，len=2：payload = yaw i8, pitch i8
  未知命令名忽略（只认 MOVE/TURRET）。

激活规则（含迟滞防抖，阈值见 server/command_udp.py）：
  某命令各轴 abs 最大值 mag > 30（CMD_ACTIVE_ON）则激活；
  激活后需全部轴 abs < 28（CMD_ACTIVE_OFF，即 mag < 28）才失活。
  无激活条目时该推送周期不发送任何字节。

cmds 语义（桌面端打包，数值 -100~100，中心死区=0）：
  [{"name":"MOVE","speed":±100,"turn":±100},      左摇杆：前+/后-，左-/右+
   {"name":"TURRET","yaw":±100,"pitch":±100}]     右摇杆：左-/右+，下-/上+
"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/command", tags=["command"])

_NEUTRAL_HEX = "00800080008000800000000000000000"

_lock = threading.Lock()
_store: dict = {
    "raw_hex": _NEUTRAL_HEX,
    "cmds": None,
    "slot": None,
    "packet": 0,
    "client_ts": None,
    "updated_at": 0.0,  # server wall clock，无推送时为 0
    "count": 0,
}


class CommandPush(BaseModel):
    raw_hex: str = Field(
        description="16 字节报文 hex（32 字符，可带空格），与 xinput_gui 协议视图一致")
    cmds: list[dict] | None = Field(
        default=None,
        description="桌面端打包的语义命令列表，如 MOVE/TURRET")
    slot: int | None = None
    packet: int | None = None
    client_ts: float | None = None


def _clean_hex(raw: str) -> str | None:
    s = "".join(raw.split()).upper()
    if len(s) != 32:
        return None
    try:
        bytes.fromhex(s)
    except ValueError:
        return None
    return s


def _decoded(raw_hex: str) -> dict:
    p = bytes.fromhex(raw_hex)
    return {
        "raw_hex": raw_hex,
        "lx": int.from_bytes(p[0:2], "little"),
        "ly": int.from_bytes(p[2:4], "little"),
        "rx": int.from_bytes(p[4:6], "little"),
        "ry": int.from_bytes(p[6:8], "little"),
        "lt": int.from_bytes(p[8:10], "little"),
        "rt": int.from_bytes(p[10:12], "little"),
        "hat": p[12], "btn": p[13], "sys": p[14], "share": p[15],
    }


def _pct(v: int) -> int:
    """u16 偏移 -> -100~100，含死区（服务端兜底解析用，与桌面端同语义）。"""
    val = int(round(v / 32767.0 * 100.0))
    val = max(-100, min(100, val))
    return 0 if -4 < val < 4 else val


def _resolve_cmds(raw_hex: str, cmds: list[dict] | None) -> list[dict]:
    """cmds 为空时按原始协议兜底解析（ly/ry XInput 原生即上为正，无需取反）。"""
    if cmds is not None:
        return cmds
    p = bytes.fromhex(raw_hex)
    lx = int.from_bytes(p[0:2], "little")
    ly = int.from_bytes(p[2:4], "little")
    rx = int.from_bytes(p[4:6], "little")
    ry = int.from_bytes(p[6:8], "little")
    return [
        {"name": "MOVE", "speed": _pct(ly - 32768), "turn": _pct(lx - 32768)},
        {"name": "TURRET", "yaw": _pct(rx - 32768), "pitch": _pct(ry - 32768)},
    ]


def snapshot() -> dict:
    """当前状态快照（command_udp 推送线程用）。"""
    with _lock:
        return dict(_store)


@router.post("")
async def post_command(body: CommandPush) -> JSONResponse:
    cleaned = _clean_hex(body.raw_hex)
    if cleaned is None:
        return JSONResponse({"ok": False, "error": "raw_hex 须为 16 字节 hex（32 字符）"}, status_code=422)
    now = time.time()
    with _lock:
        _store["raw_hex"] = cleaned
        _store["cmds"] = body.cmds
        _store["slot"] = body.slot
        _store["packet"] = body.packet
        _store["client_ts"] = body.client_ts
        _store["updated_at"] = now
        _store["count"] += 1
        count = _store["count"]
    return JSONResponse({"ok": True, "count": count, "server_time": round(now, 3)})


@router.get("")
async def get_command() -> JSONResponse:
    now = time.time()
    with _lock:
        raw_hex = _store["raw_hex"]
        cmds = _store["cmds"]
        slot = _store["slot"]
        packet = _store["packet"]
        client_ts = _store["client_ts"]
        updated_at = _store["updated_at"]
        count = _store["count"]
    out = _decoded(raw_hex)
    cmds = _resolve_cmds(raw_hex, cmds)
    out.update({
        "cmds": cmds,
        "slot": slot,
        "packet": packet,
        "client_ts": client_ts,
        "server_time": round(now, 3),
        "updated_at": round(updated_at, 3) if updated_at else 0.0,
        "age_ms": round((now - updated_at) * 1000.0, 1) if updated_at else -1.0,
        "count": count,
        "connected": count > 0,
    })
    return JSONResponse(out)
