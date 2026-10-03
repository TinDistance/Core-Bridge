"""手柄指令通道：桌面 XInput -> 16 字节 HID 协议 -> K230 轮询/UDP 推送。"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/command", tags=["command"])

_NEUTRAL_HEX = "00800080008000800000000000000000"

_VALID_CMD_NAMES = ("MOVE", "TURRET")

_lock = threading.Lock()
_store: dict = {
    "raw_hex": _NEUTRAL_HEX,
    "cmds": None,
    "slot": None,
    "packet": 0,
    "client_ts": None,
    "updated_at": 0.0,
    "count": 0,
    "seq": 0,
}


class CommandPush(BaseModel):
    raw_hex: str = Field(
        description="16 字节报文 hex（32 字符，可带空格/0x 前缀），与 xinput_gui 协议视图一致")
    cmds: list[dict] | None = Field(
        default=None,
        description="桌面端打包的语义命令列表，如 MOVE/TURRET")
    slot: int | None = None
    packet: int | None = None
    client_ts: float | None = None
    seq: int | None = None


def _clean_hex(raw: str) -> str | None:
    s = "".join(str(raw).split()).upper()
    if s.startswith("0X"):
        s = s[2:]
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
    try:
        val = int(round(int(v) / 32767.0 * 100.0))
    except (TypeError, ValueError):
        return 0
    val = max(-100, min(100, val))
    return 0 if -4 < val < 4 else val


def _sanitize_cmds(cmds: list[dict] | None) -> list[dict] | None:
    """校验桌面端 cmds 形状；非法返回 None 交由兜底解析，避免坏数据杀死推送线程。"""
    if cmds is None:
        return None
    if not isinstance(cmds, list) or len(cmds) > 8:
        return None
    out: list[dict] = []
    for c in cmds:
        if not isinstance(c, dict):
            return None
        name = c.get("name")
        if name not in _VALID_CMD_NAMES:
            continue
        clean: dict = {"name": name}
        for k, v in c.items():
            if k == "name":
                continue
            try:
                if isinstance(v, bool):
                    return None
                iv = int(v)
            except (TypeError, ValueError):
                return None
            if iv < -100 or iv > 100:
                return None
            clean[k] = iv
        out.append(clean)
    return out


def _resolve_cmds(raw_hex: str, cmds: list[dict] | None) -> list[dict]:
    """cmds 为空时按原始协议兜底解析（ly/ry XInput 原生即上为正，无需取反）。

    注意： hat/btn 按键位在无 cmds 时无法表达，这是已知取舍（仅保摇杆）。
    """
    sanitized = _sanitize_cmds(cmds)
    if sanitized is not None:
        return sanitized
    # cmds 非法或为空：尝试按 raw_hex 兜底；失败返回中位
    try:
        p = bytes.fromhex(raw_hex)
        if len(p) != 16:
            raise ValueError("bad len")
    except Exception:
        return [
            {"name": "MOVE", "speed": 0, "turn": 0},
            {"name": "TURRET", "yaw": 0, "pitch": 0},
        ]
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
    sanitized = _sanitize_cmds(body.cmds)
    if body.cmds is not None and sanitized is None:
        return JSONResponse({"ok": False, "error": "cmds 非法：须为 MOVE/TURRET 列表，值域 -100~100"}, status_code=422)
    now = time.time()
    with _lock:
        _store["raw_hex"] = cleaned
        _store["cmds"] = sanitized
        _store["slot"] = body.slot
        _store["packet"] = body.packet
        _store["client_ts"] = body.client_ts
        if body.seq is not None:
            try:
                _store["seq"] = int(body.seq)
            except (TypeError, ValueError):
                pass
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
        seq = _store.get("seq", 0)
    out = _decoded(raw_hex)
    cmds = _resolve_cmds(raw_hex, cmds)
    age_ms = round((now - updated_at) * 1000.0, 1) if updated_at else -1.0
    connected = bool(updated_at and (now - updated_at) < 1.0)
    out.update({
        "cmds": cmds,
        "slot": slot,
        "packet": packet,
        "client_ts": client_ts,
        "server_time": round(now, 3),
        "updated_at": round(updated_at, 3) if updated_at else 0.0,
        "age_ms": age_ms,
        "count": count,
        "seq": seq,
        "connected": connected,
    })
    return JSONResponse(out)
