"""H264 裸 RTP 接收端（方案 A 的 desktop 段）。

链路：K230 硬编 H264 -> RTP over UDP -> server 纯转发(:8002) -> 本模块
FU-A 解包 -> PyAV(av, 随 aiortc 装好) 同线程解码 -> PIL 帧事件。

延迟纪律（对应评审报告）：
  * 接收/解包/解码全在一个 daemon 线程，零 jitter buffer，来帧即解。
  * 事件 deque(maxlen=2)，StreamPanel 只画最新一帧。
  * 序列号跳变即向 server 回 CBR IDR 请求（300ms 限频），自愈上限
    = 1 个 GOP；不引入任何重传等待。
  * 启动即持续发 PING（学习地址用）直到收到 RTP；relay/K230 5s 无帧
    则发 ("status","fallback")，StreamPanel 自动切回 JPEG Viewer。

Events 与 viewer.Viewer 完全一致：
  ("frame", PIL.Image) | ("status", "streaming"|"no_stream"|"fallback"|text)
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from collections import deque
from urllib.parse import urlparse

from PIL import Image

try:
    import av
except ImportError:  # aiortc 未安装等
    av = None

logger = logging.getLogger(__name__)

DEFAULT_RTP_PORT = 8002
CONTROL_MAGIC = b"CBR"
CTRL_PING = 0x00
CTRL_IDR = 0x01
CTRL_PONG = 0x10
PT_H264 = 96

Event = tuple[str, object]

IDR_RETRY_S = 0.3
NO_RTP_FALLBACK_S = 5.0
PING_INTERVAL_S = 1.0


def available() -> bool:
    return av is not None


class _Depacketizer:
    """RFC 6184 子集：单 NAL 包 / STAP-A / FU-A -> Annex-B access unit。"""

    def __init__(self) -> None:
        self._au: list[bytes] = []  # 当前 access unit 的 NAL（不含起始码）
        self._fu_nal: bytes | None = None
        self._au_idr = False
        # 最近一个完整 AU 是否含 IDR NAL（丢包后只解 IDR 起的帧）
        self.last_au_idr = False

    def reset(self) -> None:
        self._au.clear()
        self._fu_nal = None
        self._au_idr = False

    def push(self, pkt: bytes) -> bytes | None:
        """喂一个 RTP 包；access unit 完整（marker=1）时返回 Annex-B 字节。"""
        if len(pkt) < 13 or (pkt[0] & 0xC0) != 0x80:
            return None
        marker = bool(pkt[1] & 0x80)
        payload = pkt[12:]
        if not payload:
            return None
        nal_type = payload[0] & 0x1F
        if nal_type == 28:  # FU-A
            if len(payload) < 2:
                return None
            fu_hdr = payload[1]
            nal_hdr = (payload[0] & 0xE0) | (fu_hdr & 0x1F)
            if fu_hdr & 0x80:  # S
                self._flush_fu()
                self._fu_nal = bytes([nal_hdr]) + payload[2:]
                if fu_hdr & 0x1F == 5:
                    self._au_idr = True
            elif self._fu_nal is not None:
                self._fu_nal += payload[2:]
            if fu_hdr & 0x40:  # E
                self._flush_fu()
        elif nal_type == 24:  # STAP-A
            off = 1
            n = len(payload)
            while off + 2 <= n:
                size = struct.unpack_from(">H", payload, off)[0]
                off += 2
                if size and off + size <= n:
                    nal = bytes(payload[off:off + size])
                    self._au.append(nal)
                    if nal[0] & 0x1F == 5:
                        self._au_idr = True
                off += size
        else:  # 单 NAL 包
            self._flush_fu()
            self._au.append(payload)
            if nal_type == 5:
                self._au_idr = True
        if marker:
            return self._take_au()
        return None

    def _flush_fu(self) -> None:
        if self._fu_nal is not None:
            self._au.append(self._fu_nal)
            self._fu_nal = None

    def _take_au(self) -> bytes | None:
        self._flush_fu()
        if not self._au:
            return None
        out = b"".join(
            b"\x00\x00\x00\x01" + nal for nal in self._au)
        self._au.clear()
        self.last_au_idr = self._au_idr
        self._au_idr = False
        return out


class H264Viewer:
    def __init__(self, server_url: str, port: int = DEFAULT_RTP_PORT,
                 fps: int = 30) -> None:
        host = urlparse(server_url).hostname or "127.0.0.1"
        self.server_host = host
        self.port = port
        self.fps = max(1, min(fps, 30))
        self._events: deque[Event] = deque(maxlen=2)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._streaming = False
        self._frames = 0
        self._decode_fails = 0
        self._dropped = 0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread is not None:
            return
        if av is None:
            self._emit(("status", "fallback"))
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="h264-viewer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def events(self) -> list[Event]:
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out

    def _emit(self, event: Event) -> None:
        with self._lock:
            self._events.append(event)

    def _set_streaming(self, on: bool) -> None:
        if on == self._streaming:
            return
        self._streaming = on
        self._emit(("status", "streaming" if on else "no_stream"))

    # ---------- 接收/解码线程 ----------
    def _thread_main(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            sock.bind(("0.0.0.0", 0))
            sock.settimeout(0.2)
            self._run_loop(sock)
        except Exception as e:
            logger.exception("h264 viewer crashed: %s", e)
            self._emit(("status", f"H264 接收异常: {e}"))
            self._set_streaming(False)
        finally:
            sock.close()
            self._thread = None

    def _run_loop(self, sock: socket.socket) -> None:
        target = (self.server_host, self.port)
        depack = _Depacketizer()
        codec = av.CodecContext.create("h264", "r")
        codec.options = {"flags": "low_delay"}
        last_ping = 0.0
        idr_until_frame = True  # 收到首帧前每次 PING 都带 IDR 请求
        last_idr = 0.0
        expected_seq: int | None = None
        last_rtp_at = 0.0
        started_at = time.monotonic()
        last_warn = 0.0
        interval = 1.0 / self.fps
        # 丢包/解码异常后：丢弃后续 P 帧直到 IDR 到达，杜绝绿斑/马赛克
        wait_idr = True

        def send_ctrl(kind: int) -> None:
            try:
                sock.sendto(CONTROL_MAGIC + bytes([kind]), target)
            except OSError:
                pass

        while not self._stop.is_set():
            now = time.monotonic()
            # PING 让 server 学习桌面地址；relay 不在（无 PONG/无 RTP）
            # 持续 NO_RTP_FALLBACK_S 则建议降级到 JPEG 链路
            if now - last_ping >= PING_INTERVAL_S:
                last_ping = now
                try:
                    sock.sendto(CONTROL_MAGIC + bytes([CTRL_PING]), target)
                except OSError:
                    pass
                if idr_until_frame:
                    send_ctrl(CTRL_IDR)
            if (not last_rtp_at and now - started_at > NO_RTP_FALLBACK_S) or (
                    last_rtp_at and now - last_rtp_at > NO_RTP_FALLBACK_S):
                # relay/K230 5s 无 RTP：降级到 JPEG 链路
                self._set_streaming(False)
                self._emit(("status", "fallback"))
                self._stop.wait(2.0)
                continue

            t0 = time.monotonic()
            got_frame = False
            deadline = t0 + max(0.005, interval)
            while time.monotonic() < deadline and not got_frame:
                try:
                    data, _addr = sock.recvfrom(2048)
                except socket.timeout:
                    break
                except OSError:
                    break
                if data[:3] == CONTROL_MAGIC:
                    continue  # PONG 等，忽略
                last_rtp_at = time.monotonic()
                seq = struct.unpack_from(">H", data, 2)[0]
                if expected_seq is not None and seq != expected_seq:
                    # 丢包/乱序：残缺 AU 一律不解，等下一个 IDR 再恢复画面
                    wait_idr = True
                    depack.reset()
                    now = time.monotonic()
                    if now - last_idr >= IDR_RETRY_S:
                        last_idr = now
                        send_ctrl(CTRL_IDR)
                expected_seq = (seq + 1) & 0xFFFF
                au = depack.push(data)
                if au is None:
                    continue
                if wait_idr and not depack.last_au_idr:
                    self._dropped += 1
                    continue
                try:
                    # PyAV 17 的 decode 只接受 av.Packet（旧版可直接喂 bytes）
                    frames = codec.decode(av.Packet(au))
                except Exception as e:
                    # 限频告警：真实 K230 码流解码失败时在控制台直接可见
                    self._decode_fails += 1
                    wait_idr = True
                    now = time.monotonic()
                    if now - last_warn >= 2.0:
                        last_warn = now
                        logger.warning(
                            "decode failed #%d: %s | au_head=%s",
                            self._decode_fails, e, au[:16].hex())
                    send_ctrl(CTRL_IDR)
                    continue
                for frame in frames:
                    img = frame.to_image()
                    self._frames += 1
                    idr_until_frame = False
                    wait_idr = False
                    got_frame = True
                    self._set_streaming(True)
                    self._emit(("frame", img))
                    break  # 一个 access unit 只取最新一帧

    # ---------- 统计 ----------
    def stats(self) -> dict:
        return {"mode": "h264", "frames": self._frames,
                "streaming": self._streaming,
                "decode_fails": self._decode_fails,
                "dropped_wait_idr": self._dropped}
