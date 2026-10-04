"""H264 裸 RTP 多链中转（方案 A 的 server 段）。

K230 同帧同 seq 同 SSRC 多拷贝发往多入口端口（8002/8003/8004），
每条入口独立 socket/独立内核缓冲，互为备份；本中转不按内容去重
（桌面端按 seq 去重、先到先用），任意入口的包都会扇出到所有下游。

S1（WinError 10054）
    Windows 上向已关闭端口 `sendto` 会收到 ICMP 端口不可达，同一 socket 的
    下一次 `recvfrom` 抛 `ConnectionResetError`。旧代码把它当成致命错误直接
    `return`，线程永久退出；桌面端关闭/重启窗口内必然触发。现在它是**非致命**
    的（`continue`，继续收），其他 `OSError` 也只 `break` 掉当前 drain 循环
    而不再退出线程。

S2（S1 的级联）与本模块的 supervision 边界
    `command_udp.note_video_sender()` 的唯一调用点在 `_on_rtp` 里，也就是
    「端点学习」和「视频扇出」共用这一个线程。线程一旦异常退出，端点 2.5 s
    后过期，控制链路随之静默停止——机器人在丢视频的同时丢控制。修好 S1 之后，
    端点会随下一个 RTP 包自动重新学习，所以 S2 不需要额外代码。
    但这条不变式依赖于「线程不会死」：任何未预料的新异常都可能重新同时打掉
    视频和控制。因此 `server/relay_supervisor.py` 的 supervisor 是最后一道保险
    ——它周期检查线程存活，发现已死就 `start_relay()` 重启并记 error 日志，
    而不是让链路静默消失。
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

CONTROL_MAGIC = b"CBR"
CTRL_PING = 0x00
CTRL_IDR = 0x01
CTRL_BITRATE = 0x02
CTRL_NACK = 0x03
CTRL_PONG = 0x10

UPSTREAM_STALE_S = 5.0
# S7: 原 10.0。桌面端每 1 s ping 一次，10 s 只是把 S1 的触发窗口放大 10 倍；
# 收到新包才会重新注册，缩短到 3 s 不影响扇出连续性（ping 周期仍是阈值的 1/3）。
DOWNSTREAM_STALE_S = 3.0

# S4: downstream GC 的最小间隔。每包全表扫 + 重建列表是 O(n) 分配，
# 没必要在每包路径上做；下游集合是慢变量，1 Hz 足够。
DOWNSTREAM_GC_INTERVAL_S = 1.0

SO_RCVBUF_BYTES = 256 << 10


class RtpRelay:
    def __init__(self, ports: list[int] | tuple[int, ...] = RTP_PORTS) -> None:
        self._ports = tuple(ports)
        self._socks: dict[int, socket.socket] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._lock = threading.Lock()
        # S4: 端口在 start() 时一次性记录，避免每包 getsockname() 系统调用
        self._port_of: dict[socket.socket, int] = {}
        # S4: downstream GC 的上次执行时刻（monotonic）
        self._last_gc_at: float = 0.0
        # 上游按端口学习：RTP 包来自哪个入口端口，就更新哪个端口的上游
        self._upstreams: dict[int, tuple[str, int]] = {}
        self._upstream_at: dict[int, float] = {}
        self._downstreams: dict[tuple[str, int], float] = {}

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
        self._port_of.clear()
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
                self._port_of[s] = port
        except OSError as e:
            logger.error("RTP relay bind %s failed: %s", self._ports, e)
            for s in self._socks.values():
                try:
                    s.close()
                except Exception:
                    pass
            self._socks.clear()
            self._port_of.clear()
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
        self._port_of.clear()
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
            except (OSError, ValueError) as e:
                # 线程在这里退出，交给 relay_supervisor 重启。**必须打出异常本身**：
                # 只写 "select stopped" 的话，运维只能看到 supervisor 每 2s 刷一条
                # "thread died"，完全不知道根因（fd 被关？socket 被替换？select 被
                # monkeypatch？）。supervisor 兜得住存活，但兜不住可观测性。
                if not self._stop.is_set():
                    logger.warning("RTP relay select stopped (%d ports): %r",
                                   len(socks), e)
                break
            for sock in readable:
                port = self._port_of.get(sock)
                if port is None:
                    continue
                # 非 blocking drain：清空该 socket 后立即轮转，多链同发不互相拖
                while True:
                    try:
                        data, addr = sock.recvfrom(2048)
                    except ConnectionResetError:
                        # S1 Windows: 上一次 sendto 的 ICMP 端口不可达，非致命。
                        # 必须先于 OSError 判（它是子类）。continue 而不是
                        # break/return：丢包还在内核缓冲里，继续收。
                        continue
                    except BlockingIOError:
                        break
                    except OSError as e:
                        if self._stop.is_set():
                            return
                        logger.warning("RTP relay recv error: %s", e)
                        break
                    try:
                        self._on_datagram(data, addr, sock, port)
                    except Exception as e:
                        logger.warning("RTP packet handling failed: %s", e)

    def _gc_downstreams_locked(self, now: float, force: bool = False,
                               keep: tuple[str, int] | None = None) -> None:
        """丢弃过期的下游。调用方必须持有 self._lock。

        S4: 非 force 时按 DOWNSTREAM_GC_INTERVAL_S 节流，避免每包一次
        全表扫 + 列表重建（O(n) 分配）。
        """
        if not force and now - self._last_gc_at < DOWNSTREAM_GC_INTERVAL_S:
            return
        self._last_gc_at = now
        for a in [a for a, t in self._downstreams.items()
                  if now - t > DOWNSTREAM_STALE_S]:
            if a != keep:
                del self._downstreams[a]

    def _on_datagram(self, data: bytes, addr: tuple[str, int],
                     sock: socket.socket, port: int) -> None:
        if len(data) < 4:
            return
        with self._lock:
            self._pkts_rx += 1
            self._bytes_rx += len(data)
            self._bytes_at.append((time.monotonic(), len(data)))
        if data[:3] == CONTROL_MAGIC:
            self._on_control(data, addr)
        elif len(data) >= 12 and (data[0] & 0xC0) == 0x80:
            self._on_rtp(data, addr, sock, port)

    def _on_rtp(self, data: bytes, addr: tuple[str, int],
                sock: socket.socket, port: int) -> None:
        host = addr[0]
        upstream_new = False
        now = time.monotonic()
        with self._lock:
            if self._upstreams.get(port) != (host, addr[1]):
                self._upstreams[port] = (host, addr[1])
                upstream_new = True
            self._upstream_at[port] = now
            self._gc_downstreams_locked(now)
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
        upstreams: dict[tuple[str, int], float] = {}
        with self._lock:
            self._downstreams[addr] = now
            # 顺手 GC：上游停流后 downstream 不再永久 stale（NACK 让这条
            # 路径的包率不再可忽略，所以同样走节流 GC）
            self._gc_downstreams_locked(now, keep=addr)
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
        elif kind in (CTRL_IDR, CTRL_BITRATE, CTRL_NACK):
            # CBR 指令转发给所有活跃上游（通常同一 K230 源 socket，
            # 重复无害：IdrGate 在 K230 侧限频合并）。
            # NACK(0x03) 走同一条路：接收端发往服务端 RTP 端口，被当作
            # CBR 包注册为下游后原样上行（整个 data，不截断）。
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
            self._gc_downstreams_locked(now, force=True)
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
            "mode": "h264",
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
        return {"mode": "h264", "live": False, "started": False}
    return _relay.status()
