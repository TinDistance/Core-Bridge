"""UDP + JPEG 分片视频通道：重组器 + 最新帧缓存 + 统计。

协议 v1（发往 UDP_PORT，默认 8001）：
  每个 UDP 包 = 12 字节头 + JPEG 切片
  头结构（大端）: >H B B H H H H
    magic      u16 = 0x4A50 ('JP')
    version    u8  = 1
    flags      u8  = 0（保留）
    frame_id   u16 （循环 0..65535）
    total      u16 （该帧总片数，1..512）
    idx        u16 （本片序号，0..total-1）
    plen       u16 （本片 payload 实际长度，需 == len(package)-12）

稳定性设计（接收端只收完整帧，丢包即丢整帧，保证实时性）：
  * 单片 payload <= 1200B（MTU 安全，WiFi 下避免 IP 分片）
  * 发送端片间 pacing 1~2ms，避免突发丢包
  * 未收齐的帧超时 0.6s 丢弃；同时只保留最近 3 个未完成帧
  * 单帧上限 1MB、total 上限 512，超限直接丢（防攻击/错包卡死）
  * 组装后校验 JPEG SOI(FFD8)/EOI(FFD9)，坏帧丢弃不发布
  * 只保留最新完整帧，desktop 通过 HTTP 轮询拉取（无长连接不断线）
"""
from __future__ import annotations

import struct
import time
from collections import deque
from dataclasses import dataclass, field

MAGIC = 0x4A50
VERSION = 1
HEADER_FMT = ">HBBHHHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 12

CHUNK_PAYLOAD_MAX = 1200
MAX_CHUNKS = 512
MAX_FRAME_BYTES = 1024 * 1024
FRAME_TIMEOUT_S = 0.6
MAX_INFLIGHT = 8


@dataclass
class _Partial:
    total: int
    first_seen: float
    chunks: dict[int, bytes] = field(default_factory=dict)
    size: int = 0


class VideoHub:
    """单线程 asyncio 内使用即可，无需加锁。"""

    def __init__(self) -> None:
        self.latest_jpeg: bytes | None = None
        self.latest_frame_id: int = -1
        self.latest_at: float = 0.0  # monotonic
        self.latest_wall: float = 0.0
        self.sender: str = ""
        self._boot: float = time.monotonic()
        # 统计
        self.chunks_rx = 0
        self.chunks_bad = 0
        self.frames_ok = 0
        self.frames_dropped = 0
        self.frames_bad_jpeg = 0
        self._frame_times: deque[float] = deque(maxlen=30)
        self._intervals_ms: deque[float] = deque(maxlen=120)
        self._partials: dict[int, _Partial] = {}

    # ---------- 组包入口（UDP 协议回调调用） ----------
    def feed_datagram(self, data: bytes, addr: str) -> bool:
        """校验头部并组包；头部合法返回 True（命令推送学习地址用）。"""
        if len(data) < HEADER_SIZE:
            self.chunks_bad += 1
            return False
        try:
            magic, ver, _flags, frame_id, total, idx, plen = struct.unpack(
                HEADER_FMT, data[:HEADER_SIZE]
            )
        except struct.error:
            self.chunks_bad += 1
            return False
        payload = data[HEADER_SIZE:]
        if magic != MAGIC or ver != VERSION:
            self.chunks_bad += 1
            return False
        if not (1 <= total <= MAX_CHUNKS):
            self.chunks_bad += 1
            return False
        if not (0 <= idx < total):
            self.chunks_bad += 1
            return False
        if plen != len(payload) or len(payload) > CHUNK_PAYLOAD_MAX or len(payload) == 0:
            self.chunks_bad += 1
            return False
        # 头部合法：来源地址可作为命令反向推送目标
        self.sender = addr

        now = time.monotonic()
        self.chunks_rx += 1
        self._gc(now)

        part = self._partials.get(frame_id)
        if part is None:
            if len(self._partials) >= MAX_INFLIGHT:
                # 挤掉最旧的未完成帧，保证内存/实时性
                oldest = min(self._partials, key=lambda k: self._partials[k].first_seen)
                del self._partials[oldest]
                self.frames_dropped += 1
            part = self._partials[frame_id] = _Partial(total=total, first_seen=now)
        elif part.total != total:
            # 同一 frame_id 复用但 total 不一致：旧回绕残留，直接丢弃旧项重建
            self.frames_dropped += 1
            part = self._partials[frame_id] = _Partial(total=total, first_seen=now)

        if idx in part.chunks:
            return True  # 重复片，忽略
        part.chunks[idx] = payload
        part.size += len(payload)
        if part.size > MAX_FRAME_BYTES:
            del self._partials[frame_id]
            self.frames_dropped += 1
            return True

        if len(part.chunks) == part.total:
            del self._partials[frame_id]
            jpeg = b"".join(part.chunks[i] for i in range(part.total))
            if not _looks_like_jpeg(jpeg):
                self.frames_bad_jpeg += 1
                return True
            self.latest_jpeg = jpeg
            self.latest_frame_id = frame_id
            self.latest_at = now
            self.latest_wall = time.time()
            self.frames_ok += 1
            if self._frame_times:
                self._intervals_ms.append((now - self._frame_times[-1]) * 1000.0)
            self._frame_times.append(now)
        return True

    # ---------- 查询 ----------
    @property
    def live(self) -> bool:
        if self.latest_jpeg is None:
            return False
        return (time.monotonic() - self.latest_at) < 3.0

    @property
    def fps(self) -> float:
        if len(self._frame_times) < 2:
            return 0.0
        dt = self._frame_times[-1] - self._frame_times[0]
        if dt <= 0:
            return 0.0
        return (len(self._frame_times) - 1) / dt

    @property
    def jitter_ms(self) -> float:
        """最近帧间隔的抖动（总体标准差），样本不足时返回 0。"""
        n = len(self._intervals_ms)
        if n < 3:
            return 0.0
        tail = list(self._intervals_ms)[-30:]
        mean = sum(tail) / len(tail)
        var = sum((x - mean) ** 2 for x in tail) / len(tail)
        return round(var**0.5, 1)

    def recent_intervals_ms(self, n: int = 60) -> list[float]:
        n = max(1, min(n, 120))
        return [round(x, 1) for x in list(self._intervals_ms)[-n:]]

    def staleness_ms(self) -> int:
        """最新帧落 server 距今的毫秒数（无帧 -1）。

        这是**陈旧度**不是延迟：latest_at 在分片重组完成那一刻打戳，只描述
        server 收包节奏。WiFi 发送队列里压了几秒的数据它一点都看不见，30fps
        满流时恒在 [0,33]ms。调用方不得把它当作链路延迟上报。
        """
        if self.latest_jpeg is None:
            return -1
        return int((time.monotonic() - self.latest_at) * 1000)

    def status(self, udp_port: int) -> dict:
        return {
            "live": self.live,
            "udp_port": udp_port,
            "frame_id": self.latest_frame_id,
            # 陈旧度，不是延迟：见 staleness_ms() 注释。
            "staleness_ms": self.staleness_ms(),
            "fps": round(self.fps, 2),
            "jitter_ms": self.jitter_ms,
            "jpeg_bytes": len(self.latest_jpeg) if self.latest_jpeg else 0,
            "frames_ok": self.frames_ok,
            "frames_dropped": self.frames_dropped,
            "frames_bad_jpeg": self.frames_bad_jpeg,
            "chunks_rx": self.chunks_rx,
            "chunks_bad": self.chunks_bad,
            "inflight": len(self._partials),
            "uptime_s": round(time.monotonic() - self._boot, 1),
            "server_time": round(time.time(), 3),
            "sender": self.sender,
            "protocol": "udp-jpeg-v1",
        }

    def _gc(self, now: float) -> None:
        expired = [fid for fid, p in self._partials.items() if now - p.first_seen > FRAME_TIMEOUT_S]
        for fid in expired:
            del self._partials[fid]
            self.frames_dropped += 1


def _looks_like_jpeg(data: bytes) -> bool:
    return len(data) >= 4 and data[0] == 0xFF and data[1] == 0xD8 and data[-2] == 0xFF and data[-1] == 0xD9


def pack_chunk(frame_id: int, total: int, idx: int, payload: bytes) -> bytes:
    """供单测/文档使用的打包函数（发送端 K230 侧用 micropython struct 实现相同格式）。"""
    header = struct.pack(HEADER_FMT, MAGIC, VERSION, 0, frame_id & 0xFFFF, total, idx, len(payload))
    return header + payload


hub = VideoHub()
