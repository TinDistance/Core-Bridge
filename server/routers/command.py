"""手柄指令通道：桌面 XInput -> 16 字节 HID 协议 -> K230 轮询。

协议（与 ESP32_to_Xbox / xinput_gui.py 一致）：
  [0:2] joyLHori u16LE 中值0x8000  [2:4] joyLVert  [4:6] joyRHori  [6:8] joyRVert
  [8:10] trigLT 10bit  [10:12] trigRT
  [12] 帽子 0中 1上 2右上 3右 4右下 5下 6左下 7左 8左上
  [13] A=0x01 B=0x02 X=0x08 Y=0x10 LB=0x40 RB=0x80
  [14] View=0x04 Menu=0x08 Xbox=0x10 LS=0x20 RS=0x40
  [15] Share=0x01

HTTP：
  POST /command  桌面推送最新包 {raw_hex, slot?, packet?, client_ts?}
  GET  /command  K230/桌面轮询最新包（含解析字段 + age_ms）
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
    "slot": None,
    "packet": 0,
    "client_ts": None,
    "updated_at": 0.0,  # server wall clock，无推送时为 0
    "count": 0,
}


class CommandPush(BaseModel):
    raw_hex: str = Field(
        description="16 字节报文 hex（32 字符，可带空格），与 xinput_gui 协议视图一致")
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


@router.post("")
async def post_command(body: CommandPush) -> JSONResponse:
    cleaned = _clean_hex(body.raw_hex)
    if cleaned is None:
        return JSONResponse({"ok": False, "error": "raw_hex 须为 16 字节 hex（32 字符）"}, status_code=422)
    now = time.time()
    with _lock:
        _store["raw_hex"] = cleaned
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
        slot = _store["slot"]
        packet = _store["packet"]
        client_ts = _store["client_ts"]
        updated_at = _store["updated_at"]
        count = _store["count"]
    out = _decoded(raw_hex)
    out.update({
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
