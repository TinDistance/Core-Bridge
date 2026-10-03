"""H265 裸 RTP 多链中转（方案 A 的 server 段）。

K230 同帧同 seq 同 SSRC 多拷贝发往多入口端口（8002/8003/8004），
每条入口独立 socket/独立内核缓冲，互为备份；本中转不按内容去重
（桌面端按 seq 去重、先到先用），任意入口的包都会扇出到所有下游。
"""
from __future__ import annotations

import logging
import os
import select
import socket
import threading
import time
from collections import deque

from server import command_udp

logger = logging.getLogger("rtp_relay")


def _parse_ports(raw: str) -> list[int]:
    out: list[int] = []
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            p = int(part)
            if p not in out:
                out.append(p)
    if not out:
        out = [8002]
    return out


RTP_PORTS = _parse_ports(
    os.environ.get("CORE_BRIDGE_RTP_PORTS")
    or os.environ.get("CORE_BRIDGE_RTP_UDP_PORT", "8002,8003,8004"))
RTP_PORT = RTP_PORTS[0]

CONTROL_MAGIC = b"CBR"
CTRL_PING = 0x00
CTRL_IDR = 0x01
CTRL_BITRATE = 0x02
CTRL_PONG = 0x10

UPSTREAM_STALE_S = 5.0
DOWNSTREAM_STALE_S = 10.0

SO_RCVBUF_BYTES = 256 << 10
RATE_ASSUMED_BPS = 3_000_000


class RtpRelay:
    def __init__(self, ports: list[int] | tuple[int, ...] = RTP_PORTS) -> None:
        self._ports = tuple(ports)
        self._socks: dict[int, socket.socket] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._lock = threading.Lock()
        # 上游按端口学习：RTP 包来自哪个入口端口，就更新哪个端口的上游
        self._upstreams: dict[int, tuple[str, int]] = {}
        self._upstream_at: dict[int, float] = {}
        self._downstreams: dict[tuple[str, int], float] = {}
        self._downstream_at = 0.0

        self._pkts_rx = 0
        self._pkts_tx = 0
        self._bytes_rx = 0
        self._ctrl_rx = 0
        self._frame_id = -1
        self._last_frame_at = 0.0
        self._marker_times: deque[float] = deque(maxlen=240)
        self._pkt_times: deque[float] = deque(maxlen=1200)
        self._bytes_at: deque[tuple[float, int]] = deque(maxlen=20000)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        # 旧线程已死但对象残留：先清理再重建，支持重启
        self._thread = None
        self._socks.clear()
        try:
            for port in self._ports:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                except OSError:
                    pass
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                 SO_RCVBUF_BYTES)
                except OSError:
                    pass
                s.bind(("0.0.0.0", port))
                s.settimeout(0)
                self._socks[port] = s
        except OSError as e:
            logger.error("RTP relay bind %s failed: %s", self._ports, e)
            for s in self._socks.values():
                try:
                    s.close()
                except Exception:
                    pass
            self._socks.clear()
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="rtp-relay")
        self._thread.start()
        logger.info("RTP relay listening on %s",
                    ", ".join("0.0.0.0:%d" % p for p in self._ports))
        return True

    def stop(self) -> None:
        self._stop.set()
        # 自唤醒：阻塞在 select 的线程在 Windows 上也能及时退出
        for port, s in list(self._socks.items()):
            try:
                s.sendto(b"", ("127.0.0.1", port))
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        for s in self._socks.values():
            try:
                s.close()
            except Exception:
                pass
        self._socks.clear()
        with self._lock:
            self._upstreams.clear()
            self._upstream_at.clear()
            self._downstreams.clear()

    def _loop(self) -> None:
        while not self._stop.is_set():
            socks = list(self._socks.values())
            if not socks:
                break
            try:
                readable, _, _ = select.select(socks, [], [], 0.5)
            except (OSError, ValueError):
                if not self._stop.is_set():
                    logger.warning("RTP relay select stopped")
                break
            for sock in readable:
                # 非 blocking drain：清空该 socket 后立即轮转，多链同发不互相拖
                while True:
                    try:
                        data, addr = sock.recvfrom(2048)
                    except BlockingIOError:
                        break
                    except OSError as e:
                        if not self._stop.is_set():
                            logger.warning("RTP relay recv stopped: %s", e)
                        return
                    try:
                        self._on_datagram(data, addr, sock)
                    except Exception as e:
                        logger.warning("RTP packet handling failed: %s", e)

    def _on_datagram(self, data: bytes, addr: tuple[str, int],
                     sock: socket.socket) -> None:
        if len(data) < 4:
            return
        with self._lock:
            self._pkts_rx += 1
            self._bytes_rx += len(data)
            self._bytes_at.append((time.monotonic(), len(data)))
        if data[:3] == CONTROL_MAGIC:
            self._on_control(data, addr)
        elif len(data) >= 12 and (data[0] & 0xC0) == 0x80:
            self._on_rtp(data, addr, sock)

    def _on_rtp(self, data: bytes, addr: tuple[str, int],
                sock: socket.socket) -> None:
        port = sock.getsockname()[1]
        host = addr[0]
        upstream_new = False
        now = time.monotonic()
        with self._lock:
            if self._upstreams.get(port) != (host, addr[1]):
                self._upstreams[port] = (host, addr[1])
                upstream_new = True
            self._upstream_at[port] = now
            for a in [a for a, t in self._downstreams.items()
                      if now - t > DOWNSTREAM_STALE_S]:
                del self._downstreams[a]
            targets = list(self._downstreams)
        if upstream_new:
            logger.info("RTP upstream learned on port %d: %s:%d",
                        port, host, addr[1])
        try:
            command_udp.note_video_sender(host, addr[1])
        except Exception:
            pass
        marker = bool(data[1] & 0x80)
        with self._lock:
            if marker:
                self._frame_id += 1
                self._last_frame_at = time.monotonic()
                self._marker_times.append(self._last_frame_at)
            self._pkt_times.append(time.monotonic())
        if not targets:
            return
        sent = 0
        for target in targets:
            try:
                sock.sendto(data, target)
                sent += 1
            except OSError:
                pass
        if sent:
            with self._lock:
                self._pkts_tx += sent

    def _on_control(self, data: bytes, addr: tuple[str, int]) -> None:
        kind = data[3] if len(data) > 3 else CTRL_PING
        now = time.monotonic()
        sock: socket.socket | None = None
        upstreams: dict[tuple[str, int], float] = {}
        with self._lock:
            self._downstreams[addr] = now
            # 顺手 GC：上游停流后 downstream 不再永久 stale
            for a in [a for a, t in self._downstreams.items()
                      if now - t > DOWNSTREAM_STALE_S]:
                if a != addr:
                    del self._downstreams[a]
            self._downstream_at = now
            for port, up in self._upstreams.items():
                age = now - self._upstream_at.get(port, 0.0)
                if age <= UPSTREAM_STALE_S:
                    upstreams[up] = age
            self._ctrl_rx += 1
        if kind == CTRL_PING:
            try:
                self._send_from_lock(data[:3] + bytes([CTRL_PONG]), addr)
            except OSError:
                pass
        elif kind in (CTRL_IDR, CTRL_BITRATE):
            # CBR 指令转发给所有活跃上游（通常同一 K230 源 socket，
            # 重复无害：IdrGate 在 K230 侧限频合并）
            for up in upstreams:
                try:
                    self._send_from_lock(data, up)
                except OSError:
                    pass

    def _send_from_lock(self, data: bytes, target: tuple[str, int]) -> None:
        """从任一可用的入口 socket 发出（线程安全视角：仅 sendto 原子性）。"""
        for port in self._ports:
            s = self._socks.get(port)
            if s is not None:
                try:
                    s.sendto(data, target)
                except OSError:
                    continue
                return

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            marker_times = list(self._marker_times)
            pkt_times = list(self._pkt_times)
            while self._bytes_at and now - self._bytes_at[0][0] > 5.0:
                self._bytes_at.popleft()
            bytes_at = list(self._bytes_at)
            pkts_rx, pkts_tx, bytes_rx, ctrl_rx = (
                self._pkts_rx, self._pkts_tx, self._bytes_rx, self._ctrl_rx)
            frame_id, last_frame_at = self._frame_id, self._last_frame_at
            # status 里也 GC downstream，避免上游停流后计数永久 stale
            for a in [a for a, t in self._downstreams.items()
                      if now - t > DOWNSTREAM_STALE_S]:
                del self._downstreams[a]
            upstreams = {
                p: (self._upstreams.get(p),
                    round(now - self._upstream_at.get(p, 0.0), 2)
                    if p in self._upstreams else -1)
                for p in self._ports}
        fps = len([t for t in marker_times if now - t <= 2.0]) / 2.0
        pps = len([t for t in pkt_times if now - t <= 2.0]) / 2.0
        win_bytes = sum(b for _t, b in bytes_at)
        win_span = 5.0
        if bytes_at:
            span = max(1e-3, now - bytes_at[0][0])
            if span < win_span:
                win_span = span
        kbps = win_bytes * 8 / win_span / 1000.0 if bytes_at else 0.0
        primary = self._ports[0]
        upstream, upstream_age = upstreams.get(primary, (None, -1))
        live = (
            upstream is not None
            and upstream_age >= 0 and upstream_age <= UPSTREAM_STALE_S
            and last_frame_at and now - last_frame_at <= UPSTREAM_STALE_S
        )
        return {
            "mode": "h265",
            "live": live,
            "fps": round(fps, 2),
            "pps": round(pps, 1),
            "frame_id": frame_id,
            "staleness_ms": round((now - last_frame_at) * 1000.0, 1)
            if last_frame_at else -1.0,
            "upstream": f"{upstream[0]}:{upstream[1]}" if upstream else None,
            "upstream_age": upstream_age,
            "upstreams": {
                str(p): (f"{u[0]}:{u[1]}" if u else None, age)
                for p, (u, age) in upstreams.items()},
            "ports": list(self._ports),
            "downstreams": len(self._downstreams),
            "pkts_rx": pkts_rx,
            "pkts_tx": pkts_tx,
            "bytes_rx": bytes_rx,
            "kbps": round(kbps, 1),
            "ctrl_rx": ctrl_rx,
            "port": primary,
            "rcvbuf_bytes": SO_RCVBUF_BYTES,
            "rcvbuf_max_queue_ms": (
                round(SO_RCVBUF_BYTES * 8 / kbps, 1)
                if kbps > 0 else -1.0),
            "server_time": round(time.time(), 3),
        }


_relay: RtpRelay | None = None


def start_relay(port: int | None = None) -> bool:
    global _relay
    if _relay is None:
        ports = [port] if port is not None else RTP_PORTS
        _relay = RtpRelay(ports)
    return _relay.start()


def stop_relay() -> None:
    global _relay
    if _relay is not None:
        _relay.stop()


def relay_status() -> dict:
    if _relay is None:
        return {"mode": "h265", "live": False, "started": False}
    return _relay.status()
