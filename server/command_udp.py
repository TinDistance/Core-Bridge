"""K230 命令通道 UDP 反向推送服务（UART 帧 v2）。

职责：把 `server.routers.command` 里的最新快照编码成 v2 UART 帧，UDP 发给
K230（本进程就是那个 UDP socket 的对端）。目标端点由视频包学习
（`note_video_sender()`）。

本模块只负责「往 K230 发」。桌面 → 服务端方向现在有两条路：
- `server/command_udp_in.py`（§4.4 方案 A）：桌面 UDP 直发，到达即转发，是主通道；
- HTTP `POST /command`（本模块的推送器服务的路径）：保留供面板/旧客户端调试。
两条路写同一个 store，所以本线程在 UDP 通道活跃时充当「保活重发 + UDP 丢包
自愈」；UDP 静默 300 ms 后 store 也会随之过期（`updated_at`），本线程自动停发。

⚠️ 关于「停车」的真实机制（不要被 K230 侧的常量误导）
    K230 的 `CMD_STALE_MS = 200` **不是安全联锁**：`CommandLink.pump` 在静默
    超过它之后只做两件事——打印 "[CMD] feed lost" 并把 `self.ok = False`。
    `cmdline.ok` 在 `k230/rtp_push.py` 里只被 `stats_line()` 读，没有任何地方
    用它去停止运动。也就是说 K230 收到什么就照样写 UART，MCU 执行上一条指令。
    同理 `build_uart_frame()` 在无激活条目时返回 None（= 本周期**不发帧**），
    所以「操作员松手」不会产生一帧显式的零速指令——停车完全依赖
    「服务端不再发」+ **UART 下游 MCU 固件自己的超时**（该固件不在本仓库，
    无法在此验证）。
    结论：本文件的 `COMMAND_STALE_S` 是**唯一在服务端侧可控的停车超时**，
    它就是 fail-safe 的实际时延上界（当前 300 ms）。要更短必须同时确认 MCU
    固件的 UART 超时，否则只是把风险挪到固件侧。

相关：S3（过期阈值 0.5 → 0.3）、S5（节拍改 `time.sleep`）、S6（`access_log`，
接线在 `server/app.py`，不属本文件）。
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time

from server.routers.command import snapshot, _resolve_cmds

logger = logging.getLogger("command_udp")

ENDPOINT_STALE_S = 2.5
MIN_HZ, MAX_HZ = 30, 300


def _env_hz(name: str, default: int) -> int:
    """环境变量覆盖频率；非法值退回默认（import 期不能抛）。"""
    try:
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            return default
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


# 兜底推送速率（HTTP `POST /command` 路径），可用 CORE_BRIDGE_CMD_FALLBACK_HZ 覆盖。
#
# 原 150 Hz 是在「HTTP 轮询是唯一命令通道」的前提下定的：服务端必须靠轮询
# 才能看到最新指令。§4.4 方案 A 的 UDP 通道（server/command_udp_in.py）上线后
# 主通道变成「到达即转发」，此线程退化为两件事：
#   1. HTTP `POST /command` 兼容路径的推送器（面板/旧客户端调试）；
#   2. K230 侧的保活重发器——即使某个 UDP 包丢了，下一周期仍会把最新帧补上。
# 此时 150 Hz 只会与「到达即转发」重复下发同一帧（150 + 桌面 ~20 pps ≈ 170 pps
# 的 40 B 小包，对空口约 0.6%），而 30 Hz 已经足够：周期 33 ms，在
# COMMAND_STALE_S = 0.3 s 窗口内可发约 9 次。K230 每 ≤4 ms 泵一次控制 socket
# 且每次只把**最后一条**有效帧写 UART，所以 UART 写入频率由 pump 决定、不由
# 本线程决定；115200 baud 下即使 30 Hz + 50 Hz 双路也只占约 10% 带宽。
#
# 代价（本机实测 150 Hz vs 30 Hz，各 25 次随机相位注入，见报告）：
# HTTP 兼容路径的「store 变化→发帧」p50 3.6 → 24.2 ms、p95 6.0 → 28.8 ms
# （这就是半个周期）。主 UDP 通道完全不受影响（到达即转发，轮询延迟 0）。
# 要旧行为就设 CORE_BRIDGE_CMD_FALLBACK_HZ=150。
DEFAULT_HZ = max(MIN_HZ, min(MAX_HZ, _env_hz("CORE_BRIDGE_CMD_FALLBACK_HZ", 30)))
# 命令快照过期阈值：超过则视为失联，该周期不发送。
#
# 这就是**服务端侧的停车超时**（见模块 docstring：K230 的 CMD_STALE_MS=200 只是
# 日志，不是联锁）。
#
# S3：原 0.5 s，与桌面端 `GamepadPusher.HEARTBEAT_S = 0.5` **完全相等**。
# 保持输入不变时（摇杆打满到机械限位、按住方向键），旧桌面端每 0.5 s 才重发一次
# 同一份指令，服务端也恰好每 0.5 s 判过期，两边在同一时刻翻转，指令流会间歇中断；
# 更糟的是过期分支会把迟滞 active 清零，下一周期还要重新越过 CMD_ACTIVE_ON
# 阈值才恢复发帧，于是「保持按住」比「反复点动」响应更差。
# 0.3 s 给了 1.6 倍余量。前提是客户端推送快于 0.3 s：UDP 通道是 50 ms 保活
# （桌面端负责），HTTP 兼容路径要求客户端 ≥ 5 Hz，否则它只会一直被判过期
# （这是刻意的：宁可明确判失联，也不要在过期的旧偏转上继续下发）。
COMMAND_STALE_S = 0.3

# 端点未学习时的空转间隔。保持 50 ms：这段时间本来就没有任何下游可发，
# 15.6 ms 的定时器取整（S5）在这里没有意义。
IDLE_WAIT_S = 0.05

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
    """兜底推送线程主循环。

    S5：节拍一律用 `time.sleep`，不用 `Event.wait`。
    附录 A.1 在本机（Windows / Python 3.12）实测：
        `Event.wait(6.67ms)` → p50 15.33 ms / p95 15.81 / max 16.23（≈64Hz，抖动 16ms）
        `time.sleep(6.67ms)` → p50  6.95 ms / p95  7.21 / max  7.73
    Windows 上带超时的等待按约 15.6 ms 的系统定时器粒度取整，`Event.wait`
    在 150 Hz（6.67ms）目标下实际只跑到约 64 Hz，且每轮抖 16 ms——正好落在
    §1.2 延迟预算里「任何一个 15 ms 量级问题都会让预算失守」的那一档。
    Python 3.11+ 的 `time.sleep` 走高精度定时器，接近目标值。

    代价：停止延迟从「立即唤醒」变成「最多一个 sleep 周期」。这里最长的
    sleep 是 IDLE_WAIT_S = 50 ms（端点未学习时），join 超时 2 s，绰绰有余。
    每轮都检查 `_stop.is_set()`（循环条件 + sleep 后的 continue）。
    """
    first_frame_logged_for: tuple[str, int] | None = None
    period = 1.0 / hz
    next_send = time.perf_counter()
    active: dict[str, bool] = {n: False for n in _CMD_ORDER}

    while not _stop.is_set():
        try:
            endpoint = _current_endpoint()
            now = time.perf_counter()
            if endpoint is None:
                # 无下游可发：低频空转，并重置节拍基准
                time.sleep(IDLE_WAIT_S)
                next_send = time.perf_counter() + period
                continue
            if now < next_send:
                # 还没到发送时刻：睡到点，然后重新评估（endpoint 可能已过期）
                time.sleep(next_send - now)
                continue
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
        except Exception:
            time.sleep(0.01)


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
    """启动兜底推送线程。sendto 为空则自建独立 UDP socket。

    `push_hz` 默认 DEFAULT_HZ = 30：UDP 通道（`command_udp_in`）上线后本线程
    不是主通道，30 Hz 只做 HTTP 兼容路径 + 保活重发，理由见 DEFAULT_HZ 注释。
    需要 150 Hz 行为时显式传 `push_hz=150`（仍受 MIN_HZ/MAX_HZ 夹取）。
    """
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
