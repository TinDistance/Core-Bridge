"""H264 裸 RTP 中转（方案 A 的 server 段）。

背景：K230 硬编 H264 -> RTP over UDP:8002 -> 本模块纯转发 -> 桌面端
PyAV 解码。与 video.py 的 JPEG 分片重组（8001）完全独立，两链路并存，
桌面端可自动降级。

设计要点（对应评审方案 A）：
  * 纯 recvfrom -> sendto 转发，不解码、不重组、不进 asyncio 事件循环
    （独立 daemon 线程，对齐 command_udp.py 的线程模式），转发开销 <1ms。
  * 上游（K230）地址从 RTP 包学习（首字节高两位 == 0b10 即 RTP v2）；
    下游（桌面）地址从控制包学习（前缀 b"CBR"）。无需信令/注册。
  * 学到上游地址后调用 command_udp.note_video_sender，让 UART 命令
    反向推送（走 8001 socket）即使 K230 只跑 RTP 推流也能工作。
  * 控制：桌面 -> server 的 b"CBR\\x01"（请求 IDR）/ b"CBR\\x02"（预留
    码率档）直接转发给 K230；b"CBR\\x00"（PING）回 b"CBR\\x10"（PONG），
    供桌面探测 relay 存活。
  * 统计按 RTP marker 位聚合帧，且只跟上游走（下游是否有人在看不影响记账，
    否则桌面端刚重启还没握手时 live 会假阴性）：GET /video/rtp_status 可读
    （live/fps/age_ms 与 /video/status 字段兼容，LatencyMonitor 可复用）。
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections import deque

from server import command_udp

logger = logging.getLogger("rtp_relay")

RTP_PORT = int(os.environ.get("CORE_BRIDGE_RTP_UDP_PORT", "8002"))

CONTROL_MAGIC = b"CBR"
CTRL_PING = 0x00
CTRL_IDR = 0x01
CTRL_BITRATE = 0x02
CTRL_PONG = 0x10

UPSTREAM_STALE_S = 5.0
DOWNSTREAM_STALE_S = 10.0


class RtpRelay:
    def __init__(self, port: int = RTP_PORT) -> None:
        self._port = port
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._lock = threading.Lock()
        self._upstream: tuple[str, int] | None = None
        self._upstream_at = 0.0
        # 下游可以有多个（GUI + 探针 + 第二个监视器）。旧实现只存一个
        # _downstream，后到的控制包会覆盖前面的，于是先到的客户端突然
        # 收不到画面 —— 表现为"随机黑屏"，极难查。
        self._downstreams: dict[tuple[str, int], float] = {}
        self._downstream_at = 0.0

        self._pkts_rx = 0
        self._pkts_tx = 0
        self._bytes_rx = 0
        self._ctrl_rx = 0
        self._frame_id = -1
        self._last_frame_at = 0.0
        # 滑动 2s 窗口统计 fps/pps：停流后 status() 读数平滑归零
        self._marker_times: deque[float] = deque(maxlen=240)
        self._pkt_times: deque[float] = deque(maxlen=1200)
        # 吞吐率：K230->PC 实际占用多少带宽。这是验证"5Mbps 够不够"的唯一
        # 直接证据，之前链路上没有任何地方能读到 kbps。
        self._bytes_at: deque[tuple[float, int]] = deque(maxlen=20000)

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        if self._thread is not None:
            return True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # 3Mbps@30fps ≈ 313pps，缓冲抗几秒突发
                self._sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            except OSError:
                pass
            self._sock.bind(("0.0.0.0", self._port))
            self._sock.settimeout(0.5)
        except OSError as e:
            logger.error("RTP relay bind :%d failed: %s", self._port, e)
            if self._sock is not None:
                self._sock.close()
                self._sock = None
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="rtp-relay")
        self._thread.start()
        logger.info("RTP relay listening on 0.0.0.0:%d", self._port)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # ---------- 转发主循环 ----------
    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            self._pkts_rx += 1
            self._bytes_rx += len(data)
            self._bytes_at.append((time.monotonic(), len(data)))
            if not data:
                continue
            if data[:3] == CONTROL_MAGIC:
                self._on_control(data, addr)
            elif (data[0] & 0xC0) == 0x80:
                self._on_rtp(data, addr)

    def _on_rtp(self, data: bytes, addr: tuple[str, int]) -> None:
        host, port = addr
        upstream_new = False
        now = time.monotonic()
        with self._lock:
            if self._upstream != (host, port):
                self._upstream = (host, port)
                upstream_new = True
            self._upstream_at = now
            # 清理超时下游，再取一份快照在锁外发送
            for a in [a for a, t in self._downstreams.items()
                      if now - t > DOWNSTREAM_STALE_S]:
                del self._downstreams[a]
            targets = list(self._downstreams)
        # 上游地址（含变化）喂给命令通道：UART 推送走 8001 socket 反向直达
        if upstream_new:
            logger.info("RTP upstream learned: %s:%d", host, port)
        command_udp.note_video_sender(host, port)
        # 上游统计与"有没有人在看"无关，必须先于下游扇出记账：桌面端刚重启
        # 还没发第一个 PING、或下游全部超时老化时 targets 为空，旧实现在
        # 早退里把这些包整个漏掉，于是 K230 明明在推流、status() 却报
        # live=false / frame_id 卡死，运维会误判成 K230 掉线。
        if data[1] & 0x80:  # marker：一帧（access unit）结束
            self._frame_id += 1
            self._last_frame_at = time.monotonic()
            self._marker_times.append(self._last_frame_at)
        self._pkt_times.append(time.monotonic())
        if not targets:
            return
        sent = 0
        for target in targets:
            try:
                self._sock.sendto(data, target)
                sent += 1
            except OSError:
                pass
        if sent:
            self._pkts_tx += sent

    def _on_control(self, data: bytes, addr: tuple[str, int]) -> None:
        kind = data[3] if len(data) > 3 else CTRL_PING
        now = time.monotonic()
        with self._lock:
            self._downstreams[addr] = now
            self._downstream_at = now
            upstream = self._upstream
            upstream_stale = (
                self._upstream is None
                or now - self._upstream_at > UPSTREAM_STALE_S)
        self._ctrl_rx += 1
        if kind == CTRL_PING:
            try:
                self._sock.sendto(CONTROL_MAGIC + bytes([CTRL_PONG]), addr)
            except OSError:
                pass
        elif kind in (CTRL_IDR, CTRL_BITRATE):
            if upstream is not None and not upstream_stale:
                try:
                    self._sock.sendto(data, upstream)
                except OSError:
                    pass

    # ---------- 状态 ----------
    def status(self) -> dict:
        now = time.monotonic()
        fps = len([t for t in self._marker_times if now - t <= 2.0]) / 2.0
        pps = len([t for t in self._pkt_times if now - t <= 2.0]) / 2.0
        # 5s 滑动窗口吞吐率（应用层字节数，不含 UDP/IP 头）
        while self._bytes_at and now - self._bytes_at[0][0] > 5.0:
            self._bytes_at.popleft()
        win_bytes = sum(b for _t, b in self._bytes_at)
        win_span = 5.0
        if self._bytes_at:
            span = max(1e-3, now - self._bytes_at[0][0])
            if span < win_span:
                win_span = span
        kbps = win_bytes * 8 / win_span / 1000.0 if self._bytes_at else 0.0
        with self._lock:
            upstream = self._upstream
            upstream_age = (now - self._upstream_at) if self._upstream else -1
            downs = len(self._downstreams)
        live = (
            upstream is not None
            and upstream_age <= UPSTREAM_STALE_S
            and now - self._last_frame_at <= UPSTREAM_STALE_S
        )
        return {
            "mode": "h264",
            "live": live,
            "fps": round(fps, 2),
            "pps": round(pps, 1),
            "frame_id": self._frame_id,
            "age_ms": round((now - self._last_frame_at) * 1000.0, 1)
            if self._last_frame_at else -1.0,
            "upstream": f"{upstream[0]}:{upstream[1]}" if upstream else None,
            "downstreams": downs,
            "pkts_rx": self._pkts_rx,
            "pkts_tx": self._pkts_tx,
            "bytes_rx": self._bytes_rx,
            "kbps": round(kbps, 1),
            "ctrl_rx": self._ctrl_rx,
            "port": self._port,
            "server_time": round(time.time(), 3),
        }


_relay: RtpRelay | None = None


def start_relay(port: int = RTP_PORT) -> bool:
    global _relay
    if _relay is None:
        _relay = RtpRelay(port)
    return _relay.start()


def stop_relay() -> None:
    global _relay
    if _relay is not None:
        _relay.stop()


def relay_status() -> dict:
    if _relay is None:
        return {"mode": "h264", "live": False, "started": False}
    return _relay.status()
