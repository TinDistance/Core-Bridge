"""K230 命令通道 UDP 推送服务（UART 帧 v2）。

背景：K230 固件的 TCP connect 慢且不稳定（驱动层反复 connect timeout，
socket 泄漏导致 errno 12），150Hz HTTP 轮询不可行；图传链路已证明 UDP
在该 WiFi 上稳定，故命令通道改为 UDP 推送。K230 收 UDP 推送后转 UART3，
不再走 TCP。

协议（UDP，默认端口 8002，环境变量 CORE_BRIDGE_CMD_UDP_PORT 覆盖）：
  注册：K230 周期性(0.5s)发送 b"CQ" + bytes([hz])，hz = 期望推送频率
  推送：服务端按 hz 向注册地址推送 v2 UART 帧（见 build_uart_frame）；
    无激活条目时该周期不发送任何字节（但 next_send 照常推进）
  注册 60s 未刷新则停止推送；K230 掉线自动重注册。

UART 命令帧 v2（K230 -> MCU UART3 原样转发）：
  [0]=0xAA [1]=0x55 [2]=N（条目数，1..8）
  { [id][len][payload...] }×N
  [末字节]=crc8（从[0]到payload末所有字节异或）
  - 0x01 MOVE，len=2：payload = speed i8, turn i8（补码，范围-100~100）
  - 0x02 TURRET，len=2：payload = yaw i8, pitch i8
  未知命令名忽略（只认 MOVE/TURRET）。

激活规则（含迟滞防抖，状态由 _run 维护 dict[name->bool]）：
  - 某命令各轴 abs 最大值 mag = max(abs(轴...))；
  - 未激活时 mag > CMD_ACTIVE_ON（30）则激活；
  - 激活后需全部轴 abs < CMD_ACTIVE_OFF（28，即 mag < 28）才失活。
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time

from server.routers.command import snapshot, _resolve_cmds

logger = logging.getLogger("command_udp")

UDP_PORT = int(os.environ.get("CORE_BRIDGE_CMD_UDP_PORT", "8002"))
UDP_HOST = os.environ.get("CORE_BRIDGE_CMD_UDP_HOST", "0.0.0.0")
REG_TTL = 60.0
MIN_HZ, MAX_HZ = 30, 300
DEFAULT_HZ = 150

# 激活迟滞阈值：ON 为 mag > 30 激活；OFF 为 mag < 28 失活。
CMD_ACTIVE_ON = 30
CMD_ACTIVE_OFF = 28

# 已知命令 -> 帧 id / 轴字段（未知名忽略）
_CMD_IDS = {"MOVE": 0x01, "TURRET": 0x02}
_CMD_AXES = {"MOVE": ("speed", "turn"), "TURRET": ("yaw", "pitch")}
_CMD_ORDER = ("MOVE", "TURRET")

_stop = threading.Event()
_thread: threading.Thread | None = None


def crc8(payload: bytes) -> int:
    c = 0
    for b in payload:
        c ^= b
    return c & 0xFF


def _clamp100(v: int) -> int:
    return max(-100, min(100, int(v)))


def _cmd_magnitude(cmd: dict, axes: tuple[str, ...]) -> int:
    """某命令各轴 abs 最大值（缺失/非数值按 0 计）。"""
    mag = 0
    for ax in axes:
        try:
            val = int(cmd.get(ax, 0) or 0)
        except (TypeError, ValueError):
            val = 0
        mag = max(mag, abs(val))
    return mag


def _known_cmds_by_name(snap: dict) -> dict[str, dict]:
    """snap -> {MOVE: cmd, TURRET: cmd}，未知命令名忽略。"""
    cmds = _resolve_cmds(snap["raw_hex"], snap.get("cmds"))
    out: dict[str, dict] = {}
    for c in cmds:
        name = c.get("name") if isinstance(c, dict) else None
        if name in _CMD_IDS and name not in out:
            out[name] = c
    return out


def update_active_state(prev: dict[str, bool],
                        cmds_by_name: dict[str, dict]) -> dict[str, bool]:
    """迟滞更新激活状态（_run 每周期调用，纯函数便于测试）。

    - 未激活：mag > CMD_ACTIVE_ON(30) 则激活；
    - 已激活：全部轴 abs < CMD_ACTIVE_OFF(28)（即 mag < 28）才失活，否则保持。
    未出现在 cmds_by_name 中的已知命令按全零（mag=0）处理。
    """
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
    """最新状态 -> v2 UART 帧；无激活条目返回 None（该周期不发送）。

    active 为 None 时按 ON 阈值（mag > 30）无状态判定，便于单帧测试；
    _run 推送循环应先经 update_active_state 维护迟滞状态再传入。
    帧：AA 55 N {id len payload}×N crc8(异或)。
    """
    cmds_by_name = _known_cmds_by_name(snap)
    if active is None:
        active = {n: _cmd_magnitude(cmds_by_name.get(n, {}), _CMD_AXES[n])
                  > CMD_ACTIVE_ON for n in _CMD_ORDER}

    entries = bytearray()
    count = 0
    for name in _CMD_ORDER:
        if not active.get(name, False):
            continue
        cmd = cmds_by_name.get(name, {})
        cid = _CMD_IDS[name]
        if name == "MOVE":
            payload = bytes([_clamp100(cmd.get("speed", 0) or 0) & 0xFF,
                             _clamp100(cmd.get("turn", 0) or 0) & 0xFF])
        else:  # TURRET
            payload = bytes([_clamp100(cmd.get("yaw", 0) or 0) & 0xFF,
                             _clamp100(cmd.get("pitch", 0) or 0) & 0xFF])
        entries += bytes([cid, len(payload)]) + payload
        count += 1

    if count == 0:
        return None
    body = bytes([0xAA, 0x55, count]) + bytes(entries)
    return body + bytes([crc8(body)])


def _run(sock: socket.socket, default_hz: int) -> None:
    client: tuple[str, int] | None = None
    last_reg = 0.0
    hz = default_hz
    next_send = time.perf_counter()
    period = 1.0 / hz
    active: dict[str, bool] = {n: False for n in _CMD_ORDER}

    while not _stop.is_set():
        # 收注册包（非阻塞，排空）
        sock.settimeout(0)
        try:
            while True:
                data, addr = sock.recvfrom(64)
                if data[:2] == b"CQ":
                    hz = MAX_HZ
                    if len(data) >= 3:
                        hz = max(MIN_HZ, min(MAX_HZ, data[2]))
                    client = (addr[0], addr[1])
                    last_reg = time.monotonic()
                    period = 1.0 / hz
        except (BlockingIOError, socket.timeout):
            pass
        except OSError:
            pass

        now = time.perf_counter()
        if client is not None and time.monotonic() - last_reg < REG_TTL:
            if now >= next_send:
                try:
                    snap = snapshot()
                    active = update_active_state(
                        active, _known_cmds_by_name(snap))
                    frame = build_uart_frame(snap, active)
                    if frame is not None:
                        sock.sendto(frame, client)
                    # frame 为 None 也不补发：next_send 照常推进，避免恢复时突发
                except OSError:
                    pass
                next_send += period
                if next_send < now - 0.1:  # 落后太多则重新对齐
                    next_send = now + period
            else:
                time.sleep(min(0.001, max(0.0, next_send - now)))
        else:
            time.sleep(0.005)
            next_send = now + period


def start_command_udp(host: str = UDP_HOST, port: int = UDP_PORT,
                      push_hz: int = DEFAULT_HZ) -> None:
    """在 server lifespan 中调用；端口被占则记错但不让 HTTP 挂掉。"""
    global _thread
    if _thread is not None:
        return
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((host, port))
    except OSError as e:
        logger.error("command UDP bind %s:%d failed: %s", host, port, e)
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(sock, push_hz),
                               daemon=True, name="command-udp")
    _thread.start()
    logger.info("command UDP listening on %s:%d (push %dHz)",
                host, port, push_hz)


def stop_command_udp() -> None:
    global _thread
    _stop.set()
    _thread = None
