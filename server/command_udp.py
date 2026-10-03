"""K230 命令通道 UDP 反向推送服务（UART 帧 v2）。"""
from __future__ import annotations

import logging
import socket
import threading
import time

from server.routers.command import snapshot, _resolve_cmds

logger = logging.getLogger("command_udp")

ENDPOINT_STALE_S = 2.5
MIN_HZ, MAX_HZ = 30, 300
DEFAULT_HZ = 150
# 命令快照过期阈值：超过则视为失联，该周期不发送（K230 侧 200ms 即自停）。
COMMAND_STALE_S = 0.5

CMD_ACTIVE_ON = 30
CMD_ACTIVE_OFF = 28

_CMD_IDS = {"MOVE": 0x01, "TURRET": 0x02}
_CMD_AXES = {"MOVE": ("speed", "turn"), "TURRET": ("yaw", "pitch")}
_CMD_ORDER = ("MOVE", "TURRET")

_stop = threading.Event()
_thread: threading.Thread | None = None
_own_sock: socket.socket | None = None

_lock = threading.Lock()
_endpoint: tuple[str, int] | None = None
_endpoint_at = 0.0


def xor_checksum(payload: bytes) -> int:
    c = 0
    for b in payload:
        c ^= b
    return c & 0xFF


# 旧名保留兼容（实为 XOR 累加，非 CRC-8）。
def crc8(payload: bytes) -> int:
    return xor_checksum(payload)


def _safe_int(v: object) -> int:
    try:
        if v is None or isinstance(v, bool):
            return 0
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _clamp100(v: object) -> int:
    return max(-100, min(100, _safe_int(v)))


def _cmd_magnitude(cmd: dict, axes: tuple[str, ...]) -> int:
    """某命令各轴 abs 最大值（缺失/非数值按 0 计）。"""
    mag = 0
    for ax in axes:
        try:
            val = _safe_int(cmd.get(ax, 0) or 0)
        except (TypeError, ValueError, AttributeError):
            val = 0
        mag = max(mag, abs(val))
    return mag


def _known_cmds_by_name(snap: dict) -> dict[str, dict]:
    """snap -> {MOVE: cmd, TURRET: cmd}，未知命令名/坏形状忽略。"""
    try:
        cmds = _resolve_cmds(snap.get("raw_hex", ""), snap.get("cmds"))
    except Exception:
        return {}
    out: dict[str, dict] = {}
    try:
        for c in cmds or []:
            name = c.get("name") if isinstance(c, dict) else None
            if name in _CMD_IDS and name not in out:
                out[name] = c
    except Exception:
        return out
    return out


def update_active_state(prev: dict[str, bool],
                        cmds_by_name: dict[str, dict]) -> dict[str, bool]:
    """迟滞更新激活状态（_run 每周期调用，纯函数便于测试）。"""
    nxt: dict[str, bool] = {}
    for name in _CMD_ORDER:
        axes = _CMD_AXES[name]
        cmd = cmds_by_name.get(name, {})
        mag = _cmd_magnitude(cmd, axes)
        was = bool(prev.get(name, False))
        if was:
            nxt[name] = not (mag < CMD_ACTIVE_OFF)
        else:
            nxt[name] = mag > CMD_ACTIVE_ON
    return nxt


def build_uart_frame(snap: dict,
                     active: dict[str, bool] | None = None) -> bytes | None:
    """最新状态 -> v2 UART 帧；无激活条目返回 None（该周期不发送）。"""
    try:
        cmds_by_name = _known_cmds_by_name(snap)
    except Exception:
        return None
    try:
        if active is None:
            active = {n: _cmd_magnitude(cmds_by_name.get(n, {}), _CMD_AXES[n])
                      > CMD_ACTIVE_ON for n in _CMD_ORDER}

        entries = bytearray()
        count = 0
        for name in _CMD_ORDER:
            if not active.get(name, False):
                continue
            cmd = cmds_by_name.get(name, {})
            if not isinstance(cmd, dict):
                continue
            cid = _CMD_IDS[name]
            if name == "MOVE":
                payload = bytes([_clamp100(cmd.get("speed", 0) or 0) & 0xFF,
                                 _clamp100(cmd.get("turn", 0) or 0) & 0xFF])
            else:
                payload = bytes([_clamp100(cmd.get("yaw", 0) or 0) & 0xFF,
                                 _clamp100(cmd.get("pitch", 0) or 0) & 0xFF])
            entries += bytes([cid, len(payload)]) + payload
            count += 1

        if count == 0:
            return None
        body = bytes([0xAA, 0x55, count]) + bytes(entries)
        return body + bytes([xor_checksum(body)])
    except Exception:
        return None


def note_video_sender(host: str, port: int) -> None:
    """收到合法视频分片时由 video 路由调用（asyncio 事件循环线程）。"""
    global _endpoint, _endpoint_at
    with _lock:
        prev = _endpoint
        _endpoint = (host, port)
        _endpoint_at = time.monotonic()
    if prev != _endpoint:
        logger.info("command UDP target from video sender: %s:%d", host, port)


def _current_endpoint() -> tuple[str, int] | None:
    with _lock:
        if _endpoint is None:
            return None
        if time.monotonic() - _endpoint_at > ENDPOINT_STALE_S:
            return None
        return _endpoint


def _snapshot_fresh(max_age_s: float = COMMAND_STALE_S) -> dict | None:
    """返回新鲜快照；过期返回 None（调用方该周期不发送，触发 K230 自停）。"""
    try:
        snap = snapshot()
    except Exception:
        return None
    try:
        updated = float(snap.get("updated_at") or 0.0)
    except Exception:
        updated = 0.0
    if not updated:
        return snap
    import time as _t
    if _t.time() - updated > max_age_s:
        return None
    return snap


def _run(sendto, hz: int) -> None:
    first_frame_logged_for: tuple[str, int] | None = None
    period = 1.0 / hz
    next_send = time.perf_counter()
    active: dict[str, bool] = {n: False for n in _CMD_ORDER}

    while not _stop.is_set():
        try:
            endpoint = _current_endpoint()
            now = time.perf_counter()
            if endpoint is not None:
                if now >= next_send:
                    try:
                        snap = _snapshot_fresh()
                        if snap is not None:
                            active = update_active_state(
                                active, _known_cmds_by_name(snap))
                            frame = build_uart_frame(snap, active)
                            if frame is not None:
                                try:
                                    sendto(frame, endpoint)
                                except (OSError, AttributeError):
                                    pass
                                if endpoint != first_frame_logged_for:
                                    logger.info("first command UDP frame sent to %s:%d (%d bytes)",
                                                endpoint[0], endpoint[1], len(frame))
                                    first_frame_logged_for = endpoint
                        else:
                            # 快照过期：不清 active 但不发送，K230 侧超时自停
                            active = {n: False for n in _CMD_ORDER}
                    except Exception:
                        pass
                    next_send += period
                    if next_send < now - 0.1:
                        next_send = now + period
                else:
                    _stop.wait(max(0.0, next_send - now))
            else:
                _stop.wait(0.05)
                next_send = time.perf_counter() + period
        except Exception:
            _stop.wait(0.01)


def _make_sendto(fallback_sendto=None):
    """优先使用独立 UDP socket；外部未提供时自建，避免跨线程复用 asyncio socket。"""
    global _own_sock
    if fallback_sendto is not None:
        return fallback_sendto
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _own_sock = s
        return s.sendto
    except OSError:
        return None


def start_command_udp(sendto=None, push_hz: int = DEFAULT_HZ) -> None:
    """启动命令推送线程。sendto 为空则自建独立 UDP socket。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    hz = max(MIN_HZ, min(MAX_HZ, int(push_hz or DEFAULT_HZ)))
    resolved = _make_sendto(sendto)
    if resolved is None:
        logger.error("command UDP: no usable socket, pusher not started")
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(resolved, hz),
                               daemon=True, name="command-udp")
    _thread.start()
    logger.info("command UDP pusher started (target learned from video packets, %dHz)",
                hz)


def stop_command_udp(timeout: float = 2.0) -> None:
    global _thread, _own_sock
    _stop.set()
    t, _thread = _thread, None
    if t is not None and t is not threading.current_thread():
        t.join(timeout=timeout)
    with _lock:
        global _endpoint
        _endpoint = None
    s, _own_sock = _own_sock, None
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
