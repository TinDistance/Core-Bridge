"""H264 裸 RTP 中转（方案 A 的 server 段）。"""
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

SO_RCVBUF_BYTES = 256 << 10
RATE_ASSUMED_BPS = 3_000_000


class RtpRelay:
    def __init__(self, port: int = RTP_PORT) -> None:
        self._port = port
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._lock = threading.Lock()
        self._upstream: tuple[str, int] | None = None
        self._upstream_at = 0.0
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
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except OSError:
                pass
            try:
                self._sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, SO_RCVBUF_BYTES)
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
        # 自唤醒：阻塞在 recvfrom 的线程在 Windows 上也能及时退出
        try:
            if self._sock is not None:
                self._sock.sendto(b"", ("127.0.0.1", self._port))
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        with self._lock:
            self._upstream = None
            self._upstream_at = 0.0
            self._downstreams.clear()

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                if not self._stop.is_set():
                    logger.warning("RTP relay loop stopped: %s", e)
                break
            try:
                if len(data) < 4:
                    continue
                with self._lock:
                    self._pkts_rx += 1
                # 计数器在锁内更新，字节窗追加同样加锁（见 _on_rtp/status）
                with self._lock:
                    self._bytes_rx += len(data)
                    self._bytes_at.append((time.monotonic(), len(data)))
                if not data:
                    continue
                if data[:3] == CONTROL_MAGIC:
                    self._on_control(data, addr)
                elif len(data) >= 12 and (data[0] & 0xC0) == 0x80:
                    self._on_rtp(data, addr)
            except Exception as e:
                logger.warning("RTP packet handling failed: %s", e)
                continue

    def _on_rtp(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 12:
            return
        host, port = addr
        upstream_new = False
        now = time.monotonic()
        with self._lock:
            if self._upstream != (host, port):
                self._upstream = (host, port)
                upstream_new = True
            self._upstream_at = now
            for a in [a for a, t in self._downstreams.items()
                      if now - t > DOWNSTREAM_STALE_S]:
                del self._downstreams[a]
            targets = list(self._downstreams)
        if upstream_new:
            logger.info("RTP upstream learned: %s:%d", host, port)
        try:
            command_udp.note_video_sender(host, port)
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
        sock = self._sock
        if sock is None:
            return
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
        with self._lock:
            self._downstreams[addr] = now
            # 顺手 GC：上游停流后 downstream 不再永久 stale
            for a in [a for a, t in self._downstreams.items()
                      if now - t > DOWNSTREAM_STALE_S]:
                if a != addr:
                    del self._downstreams[a]
            self._downstream_at = now
            upstream = self._upstream
            upstream_stale = (
                self._upstream is None
                or now - self._upstream_at > UPSTREAM_STALE_S)
            self._ctrl_rx += 1
        sock = self._sock
        if sock is None:
            return
        if kind == CTRL_PING:
            try:
                sock.sendto(CONTROL_MAGIC + bytes([CTRL_PONG]), addr)
            except OSError:
                pass
        elif kind in (CTRL_IDR, CTRL_BITRATE):
            if upstream is not None and not upstream_stale:
                try:
                    sock.sendto(data, upstream)
                except OSError:
                    pass

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
        fps = len([t for t in marker_times if now - t <= 2.0]) / 2.0
        pps = len([t for t in pkt_times if now - t <= 2.0]) / 2.0
        win_bytes = sum(b for _t, b in bytes_at)
        win_span = 5.0
        if bytes_at:
            span = max(1e-3, now - bytes_at[0][0])
            if span < win_span:
                win_span = span
        kbps = win_bytes * 8 / win_span / 1000.0 if bytes_at else 0.0
        with self._lock:
            upstream = self._upstream
            upstream_age = (now - self._upstream_at) if self._upstream else -1
            downs = len(self._downstreams)
        live = (
            upstream is not None
            and upstream_age <= UPSTREAM_STALE_S
            and last_frame_at and now - last_frame_at <= UPSTREAM_STALE_S
        )
        return {
            "mode": "h264",
            "live": live,
            "fps": round(fps, 2),
            "pps": round(pps, 1),
            "frame_id": frame_id,
            "staleness_ms": round((now - last_frame_at) * 1000.0, 1)
            if last_frame_at else -1.0,
            "upstream": f"{upstream[0]}:{upstream[1]}" if upstream else None,
            "downstreams": downs,
            "pkts_rx": pkts_rx,
            "pkts_tx": pkts_tx,
            "bytes_rx": bytes_rx,
            "kbps": round(kbps, 1),
            "ctrl_rx": ctrl_rx,
            "port": self._port,
            "rcvbuf_bytes": SO_RCVBUF_BYTES,
            "rcvbuf_max_queue_ms": (
                round(SO_RCVBUF_BYTES * 8 / kbps, 1)
                if kbps > 0 else -1.0),
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
