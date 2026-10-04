"""Receive H.264 RTP, decode with PyAV, and emit the newest PIL frames."""
from __future__ import annotations

import logging
import random
import select
import socket
import struct
import sys
import threading
import time
from collections import deque
from typing import Iterable, Sequence
from urllib.parse import urlparse

from PIL import Image

try:
    import av
except ImportError:
    av = None

logger = logging.getLogger(__name__)

DEFAULT_RTP_PORT = 8002
CONTROL_MAGIC = b"CBR"
CTRL_PING = 0x00
CTRL_IDR = 0x01
CTRL_PONG = 0x10
CTRL_NACK = 0x03
PT_H264 = 96

Event = tuple[str, object]

IDR_RETRY_S = 0.2
NO_RTP_FALLBACK_S = 5.0
PING_INTERVAL_S = 1.0

# D2：原 0.060。序号出现空洞后等 60 ms 才宣布丢包，单这一项就吃掉 100 ms 预算的
# 60%，之后还要等 IDR（K230 侧 IDR 请求 400 ms 限频 + IDR 本身约 80 ms 摊发）。
# 40 ms 的含义变成「D3 的 NACK 往返（局域网 15–30 ms，doc §4.3）已经用完还
# 没补上」的兜底上限，只有 NACK 也失败时才走到它。
REORDER_S = 0.040

# D1：事件驱动接收。**有数据时** select 立即返回，不受 Windows 15.6 ms 定时器
# 粒度影响（附录 A.1：Event.wait(2ms) → p50 15.5 ms）；这正是选它而不是
# _stop.wait / 带超时 recv 的原因。
# 实测补充（本机）：Windows 上 select 的**超时值**同样按 ~15.6 ms 取整 ——
# select(0.004) → p50 15.6 ms，select(0.020) → p50 31.3 ms。所以这里取 12 ms，
# 实际阻塞 ≈ 15.6 ms（仍满足「无数据时最多等 20 ms」），而不是名义上的 20→31 ms。
SELECT_TIMEOUT_S = 0.012
SELECT_MIN_WAIT_S = 0.0005      # 计算出的等待下限
SELECT_SPIN_S = 0.008          # NACK 轮次在 8 ms 内到期时空转等待（见循环内注释）
RECV_DRAIN_MAX = 512
SIGNAL_LOSS_BACKOFF_S = 0.5
SIGNAL_LOSS_STEP_S = 0.02

# D3：NACK（doc §4.3）。线路契约由 K230 侧 parse_nack 单测钉死：
#   b"C" + b"B" + b"R" + b"\x03" + count(u8) + seq(u16 BE) * count
#   count ∈ [1,32]，且 len(data) 必须**恰好** == 5 + count*2，否则整包丢弃计入
#   nack_bad。所以一包最多 32 个 seq，超出必须分片成多包。
NACK_MAX_SEQS = 32
# 一个空洞单轮最多请求多少个 seq（会分片成 ceil(N/32) 个报文）。不设成 32 是因为
# 契约只是「**一包**最多 32 个」，超出要分片而不是丢弃；NACK_MAX_SEQS_PER_PLAN 只
# 是防止极端大空洞把一轮变成几百个报文，真正的总量上限是下面的 60 seq/s 限速。
NACK_MAX_SEQS_PER_PLAN = 96
NACK_FIRST_DELAY_S = 0.004      # 发现空洞后等 4 ms（吸收 WiFi 轻微乱序）就发
NACK_RETRY_DELAY_S = 0.025      # 第一轮发出后 25 ms 仍缺 → 重发一次
NACK_MAX_ROUNDS = 2             # 每个空洞最多 2 次（首轮 + 1 次重发）
NACK_RATE_LIMIT_SEQS_PER_S = 60  # 防拥塞时 NACK 风暴，按 count 累计的总量上限
NACK_HOLE_TTL_S = 1.0            # 空洞记录兜底过期，防止状态泄漏

# 积压队列（doc §4.5：以时间为准，不以包数为准）
BACKLOG_MAX_MS = 500
BACKLOG_KEEP_MS = 150
# T1 单链路约 11 包/帧（约 312 pps），4 帧约 44 包，取 48 作为「队列小到不值得
# 按时间裁剪」的下限（低于它时字节预算已经在起作用）。
BACKLOG_MIN_PKTS = 48
# BACKLOG_MAX_BYTES 不再是固定 2 MB：分档后单链路包率从 ~1180 pps 降到 ~312 pps，
# 2 MB 相当于约 6.5 秒队列，会把 trim_backlog 的保护彻底架空。改为按实测 bps 动态
# 计算（见 backlog_byte_budget），BACKLOG_MAX_BYTES 只当绝对上限兜底。
BACKLOG_BUDGET_S = 0.25
# 码率窗口（2s）还没填满时用下限。下限必须 ≥ 几帧：T3 平均帧就有 25 KB，
# 24 KB 会在「实测码率之前」把一个正常的一帧深度队列误判成积压并请求 IDR。
# 64 KB ≈ T3 的 2.5 帧 / T1 的 5 帧。
BACKLOG_BUDGET_MIN_BYTES = 64 << 10
BACKLOG_MAX_BYTES = 512 << 10

DECODE_BURST_MAX = 4
SEQ_RESET_DISTANCE = 0x8000
# SEQ_RESET_NOTE：`SEQ_RESET_DISTANCE`（半个序号空间 = 32768）是**取不到**的
# 上界 —— `max_seq` 只在 `_seq_after(seq, max_seq)` 为真时前进，而它要求差值落在
# (0, 0x8000)。所以 `nmiss`（空洞最深处）与 `backlog_packets()` 恒 < 0x8000。
# 这使两处「seq 重置 / 积压过半」判据都成了死代码：
#   1) 收包处的 `not _seq_after(...) and (max_seq - seq) & 0xFFFF > SEQ_RESET_DISTANCE`
#      —— 两条件互斥（若 seq 落在 max_seq 后方 ≥ 半空间，则反向差值 ≤ 半空间）；
#   2) `backlog_packets() > SEQ_RESET_DISTANCE` —— 由上面同一条不变量推出。
# 实测（真实 server.app 子进程 + 假 K230）：seq 从 45000 跳回 0 时
# seq_resets 始终为 0，而 pkts_lost 一次就 +20536。
# SEQ_RESET_MIN_DISTANCE 取半个序号空间的 1/4：任何真实链路都不可能连续丢
# 16384 个包（K230 重传环只有 128 个槽），所以 nmiss 达到这个量级只可能是
# 上游重启 / seq 空间回绕，与「真丢包」在语义上也可区分。
SEQ_RESET_MIN_DISTANCE = SEQ_RESET_DISTANCE // 4
AU_MAX_PACKETS = 64
AU_MAX_BYTES = 512 << 10

# D4：set_render_size 的上限兜底，防止调用方给出荒谬尺寸导致每帧巨额分配。
RENDER_SIZE_MAX = 8192


def backlog_byte_budget(bps: float) -> int:
    """按实测码率给出积压队列的字节预算（doc §4.5）。

    算式：``budget = bps * BACKLOG_BUDGET_S / 8``，下限 ``BACKLOG_BUDGET_MIN_BYTES``，
    上限 ``BACKLOG_MAX_BYTES``。

    以 T1（720p30 / 3000 kbps / 线上约 3.4 Mbps）为例：0.25 s 预算 ≈ 106 KB，
    约 88 个 1200 B 包 ≈ 8 帧；旧值 2 MB 相当于 6.5 秒队列。T3（线上约 6.8 Mbps）
    时预算 ≈ 212 KB。尚未测到码率（bps <= 0）时退回下限，宁可早裁剪。
    """
    if bps is None or bps <= 0:
        return BACKLOG_BUDGET_MIN_BYTES
    v = int(bps * BACKLOG_BUDGET_S / 8.0)
    return max(BACKLOG_BUDGET_MIN_BYTES, min(BACKLOG_MAX_BYTES, v))


def encode_nack(seqs: Sequence[int]) -> bytes:
    """把至多 ``NACK_MAX_SEQS`` 个序号编码成一个 NACK 报文（doc §4.3 线路契约）。

    ``b"CBR" + 0x03 + count(u8) + seq(u16 BE) * count``，总长恒等于
    ``5 + count * 2``，count ∈ [1, 32]。K230 侧对长度做**精确**校验，多一个字节
    就会整包计入 ``nack_bad``，所以调用方必须先分片。
    """
    picked = [int(s) & 0xFFFF for s in seqs][:NACK_MAX_SEQS]
    if not picked:
        raise ValueError("NACK 至少要 1 个 seq")
    return (CONTROL_MAGIC + bytes([CTRL_NACK, len(picked)])
            + struct.pack(">" + "H" * len(picked), *picked))


def _is_stale_seq(seq: int, playhead: int | None) -> bool:
    """判断包是否已经落后于 playhead（D8）。

    重复拷贝、重传、或任何在消费之后才到的包都会命中。旧代码只检查
    ``seq in pending``，这类包会作为新条目永久滞留在 ``pending`` 里累积到
    ``BACKLOG_MAX_BYTES``，误触发 ``trim_backlog`` + IDR 请求；引入 NACK 之后
    迟到的重传会更多，**必须**在 ``pending[seq] = data`` 之前挡掉。
    """
    if playhead is None:
        return False
    return ((seq - playhead) & 0xFFFF) >= 0x8000


class NackPlanner:
    """两级丢包恢复的第一级：空洞 → NACK（doc §4.3）。

    纯逻辑、无 IO、线程 confined（只有接收循环线程调用），便于单测：

    - ``plan(missing, now)`` 返回本轮该发的 NACK 报文列表；
    - 第一次看到空洞只登记，等 ``NACK_FIRST_DELAY_S``（吸收 WiFi 轻微乱序）才发；
    - 发完等 ``NACK_RETRY_DELAY_S`` 再重发一次，最多 ``NACK_MAX_ROUNDS`` 轮；
    - 超过 ``NACK_RATE_LIMIT_SEQS_PER_S`` 的部分丢弃并计入 ``dropped``；
    - ``resolve_below(seq)`` 在空洞被填上时结算，命中过的空洞计为 ``recovered``。

    ``REORDER_S`` 到期仍未补上的空洞由调用方走 ``declare_loss``（等 IDR），
    那是第二级回退；此时调用 ``reset()`` 把状态清干净。
    """

    def __init__(self, max_seqs: int = NACK_MAX_SEQS,
                 max_rounds: int = NACK_MAX_ROUNDS,
                 first_delay_s: float = NACK_FIRST_DELAY_S,
                 retry_delay_s: float = NACK_RETRY_DELAY_S,
                 rate_limit_seqs_per_s: int = NACK_RATE_LIMIT_SEQS_PER_S,
                 ttl_s: float = NACK_HOLE_TTL_S,
                 max_seqs_per_plan: int = NACK_MAX_SEQS_PER_PLAN) -> None:
        self.max_seqs = max(1, min(int(max_seqs), NACK_MAX_SEQS))
        self.max_rounds = max(1, int(max_rounds))
        self.first_delay_s = max(0.0, float(first_delay_s))
        self.retry_delay_s = max(0.0, float(retry_delay_s))
        self.rate_limit = max(0, int(rate_limit_seqs_per_s))
        self.ttl_s = max(0.0, float(ttl_s))
        self.max_seqs_per_plan = max(1, int(max_seqs_per_plan))
        # seq -> [first_seen, rounds, next_send_at]
        self._rec: dict[int, list[float]] = {}
        self._win: deque[tuple[float, int]] = deque()
        self.sent_packets = 0
        self.sent_seqs = 0
        self.recovered = 0
        self.dropped = 0

    # ---- 内部 ---------------------------------------------------------
    def _gc(self, now: float) -> None:
        if self.ttl_s <= 0:
            return
        for seq in [s for s, r in self._rec.items()
                    if now - r[0] > self.ttl_s]:
            del self._rec[seq]

    def _rate_ok(self, now: float) -> bool:
        """1 秒滑动窗口限速：按 count 累计的 seq 总数不超过 ``rate_limit``。"""
        if self.rate_limit <= 0:
            return True
        while self._win and self._win[0][0] <= now - 1.0:
            self._win.popleft()
        if sum(c for _t, c in self._win) >= self.rate_limit:
            return False
        self._win.append((now, 1))
        return True

    # ---- 对外 ---------------------------------------------------------
    def plan(self, missing: Iterable[int], now: float) -> list[bytes]:
        """为当前仍缺失的序号生成本轮 NACK 报文。

        所有序号都会登记（状态完整，超出 ``max_seqs_per_plan`` 的下一轮仍能被
        请求到），但单轮发出的 seq 数有上限，避免一个巨大空洞把一轮变成几百个
        报文；真正的总量上限是 ``rate_limit_seqs_per_s``。
        """
        self._gc(now)
        picked: list[int] = []
        for raw in missing:
            seq = int(raw) & 0xFFFF
            rec = self._rec.get(seq)
            if rec is None:
                # 首见：只登记，不立即发（给乱序包留窗口）
                self._rec[seq] = [now, 0.0, now + self.first_delay_s]
                continue
            if rec[1] >= self.max_rounds or now < rec[2]:
                continue
            if len(picked) >= self.max_seqs_per_plan:
                continue
            if not self._rate_ok(now):
                # 限速丢弃：不消耗轮次，但推到 retry 之后避免空转
                self.dropped += 1
                rec[2] = now + self.retry_delay_s
                continue
            rec[1] += 1.0
            rec[2] = now + self.retry_delay_s
            picked.append(seq)
        out: list[bytes] = []
        for i in range(0, len(picked), self.max_seqs):
            chunk = picked[i:i + self.max_seqs]
            out.append(encode_nack(chunk))
            self.sent_packets += 1
            self.sent_seqs += len(chunk)
        return out

    def next_due(self, now: float) -> float:
        """Return the absolute monotonic time of the earliest pending round.

        调用方用它把 select 超时收紧到「下一轮 NACK 该发的时刻」，这样 NACK 的
        4 ms / 25 ms 时序不受链路是否有流量影响（否则整帧发完后链路空闲，只能等
        满一个 SELECT_TIMEOUT_S）。没有待发轮次时返回 ``now + retry_delay_s``。
        """
        best = None
        for rec in self._rec.values():
            if rec[1] >= self.max_rounds:
                continue
            if best is None or rec[2] < best:
                best = rec[2]
        return now + self.retry_delay_s if best is None else best

    def resolve_below(self, seq: int) -> int:
        """清除 ``seq``（含）之前的空洞记录，返回其中命中过 NACK 的个数。"""
        ref = int(seq) & 0xFFFF
        hit = 0
        for s in [s for s in self._rec
                  if ((ref - s) & 0xFFFF) < 0x8000]:
            rec = self._rec.pop(s)
            if rec[1] > 0:
                hit += 1
        self.recovered += hit
        return hit

    def reset(self) -> None:
        self._rec.clear()
        self._win.clear()

    @property
    def tracked(self) -> int:
        return len(self._rec)


def _rtp_payload_offset(pkt: bytes) -> int | None:
    """按 RFC3550 计算 payload 偏移；非法返回 None。处理 CC/X/padding。"""
    if len(pkt) < 12 or (pkt[0] & 0xC0) != 0x80:
        return None
    cc = pkt[0] & 0x0F
    off = 12 + cc * 4
    if len(pkt) < off:
        return None
    if pkt[0] & 0x10:  # X extension
        if len(pkt) < off + 4:
            return None
        ext_len = struct.unpack_from(">H", pkt, off + 2)[0]
        off += 4 + ext_len * 4
        if len(pkt) < off:
            return None
    return off


def _seq_after(seq: int, ref: int) -> bool:
    """Return whether seq follows ref in the 16-bit sequence space."""
    return 0 < ((seq - ref) & 0xFFFF) < 0x8000


def available() -> bool:
    return av is not None


class _Depacketizer:
    """RFC 6184 子集：单 NAL 包 / STAP-A / FU-A -> Annex-B access unit。"""

    def __init__(self) -> None:
        self._au: list[bytes] = []
        self._fu_nal: bytes | None = None
        self._au_idr = False
        self.last_au_idr = False
        self._au_first_at = 0.0
        self.last_au_start_at = 0.0

    def reset(self) -> None:
        self._au.clear()
        self._fu_nal = None
        self._au_idr = False

    def push(self, pkt: bytes, now: float | None = None) -> bytes | None:
        """Append an RTP packet and return Annex-B bytes at the AU marker."""
        off = _rtp_payload_offset(pkt)
        if off is None:
            return None
        payload = pkt[off:]
        # padding 位：去掉尾部 padding 字节
        if pkt[0] & 0x20 and payload:
            pad_len = payload[-1]
            if 0 < pad_len <= len(payload):
                payload = payload[:-pad_len]
        if not self._au and self._fu_nal is None:
            self._au_first_at = time.monotonic() if now is None else now
        marker = bool(pkt[1] & 0x80)
        if not payload:
            return None
        # AU 上限：marker 丢失时强制丢弃，避免无限累积
        au_bytes = sum(len(n) for n in self._au) + (len(self._fu_nal or b""))
        if len(self._au) > AU_MAX_PACKETS or au_bytes > AU_MAX_BYTES:
            self.reset()
            return None
        nal_type = payload[0] & 0x1F
        if nal_type == 28:
            if len(payload) < 2:
                return None
            fu_hdr = payload[1]
            nal_hdr = (payload[0] & 0xE0) | (fu_hdr & 0x1F)
            if fu_hdr & 0x80:
                self._flush_fu()
                self._fu_nal = bytes([nal_hdr]) + payload[2:]
                if fu_hdr & 0x1F == 5:
                    self._au_idr = True
            elif self._fu_nal is not None:
                self._fu_nal += payload[2:]
            if fu_hdr & 0x40:
                self._flush_fu()
        elif nal_type == 24:
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
        else:
            if nal_type == 0 or nal_type >= 29:
                # 未支持的 NAL（STAP-B/MTAP/FU-B/保留）：复位并等 IDR
                self.reset()
                return None
            self._flush_fu()
            self._au.append(bytes(payload))
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
        self.last_au_start_at = self._au_first_at
        self._au_idr = False
        return out


class H264Viewer:
    def __init__(self, server_url: str, port: int = DEFAULT_RTP_PORT) -> None:
        url = (server_url or "").strip()
        if url and "://" not in url:
            url = "http://" + url
        host = urlparse(url).hostname or "127.0.0.1"
        self.server_host = host
        self.port = port
        self._events: deque[Event] = deque(maxlen=2)
        self._lock = threading.Lock()
        self._slock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._streaming = False
        self._frames = 0
        self._decode_fails = 0
        self._dropped = 0
        self._pkts_rx = 0
        self._lost_pkts = 0
        self._dup = 0
        self._loss_events = 0
        self._gaps = 0
        self._reorder_fixed = 0
        self._drops = 0
        self._drop_events = 0
        self._seq_resets = 0
        self._backlog_packets = 0
        self._backlog_bytes = 0
        self._render_age_ms = 0.0
        self._queue_ms = 0.0
        self._bitrate_kbps = 0.0
        self._loop_us_avg = 0.0
        self._loop_us_max = 0.0
        # D3：NACK 调度（纯逻辑，可单测），由接收循环线程独占写入
        self._nack = NackPlanner()
        # D4：UI 线程写入目标渲染尺寸，解码线程读取（0,0 = 不缩放）
        self._render_size = (0, 0)
        # D1：stop() 自唤醒需要拿到 socket 和它绑定的端口
        self._sock: socket.socket | None = None
        self._local_port = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if av is None:
            self._emit(("status", "fallback"))
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="h264-viewer")
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wakeup()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def _wakeup(self) -> None:
        """自唤醒：往自己绑定的端口发一个空报文，把阻塞中的 select 立刻叫醒。

        D1 之前靠 ``settimeout(0.2)`` 保证停止响应；改成 ``select`` 阻塞等待后必须
        显式自唤醒，否则最坏要等满一个 select 超时（20 ms 仍然可接受，但这里是
        立即返回，不依赖超时值）。做法与 ``server/rtp_relay.py`` 的 ``stop()`` 一致。
        """
        with self._slock:
            sock, port = self._sock, self._local_port
        if sock is None or not port:
            return
        try:
            sock.sendto(b"", ("127.0.0.1", port))
        except OSError:
            pass

    def set_render_size(self, w: int, h: int) -> None:
        """设置目标渲染尺寸（线程安全，D4）。

        语义
        ----
        - ``w > 0 且 h > 0``：解码线程在 ``codec.decode()`` 之后、``to_image()``
          之前用 ``frame.reformat(width=w, height=h, format="rgb24")`` 一次完成
          色彩转换 + 缩放；``("frame", img)`` 事件的 payload 尺寸已经是 ``(w, h)``。
        - ``w <= 0 或 h <= 0``（含默认的 ``(0, 0)``）：不缩放，直接
          ``frame.to_image()``，输出解码原始尺寸。

        调用方约定
        ----------
        - **调用方负责保持宽高比**。这里做的是无脑拉伸到 ``(w, h)``，不做等比
          适配；如果传入的比例与画面不一致，画面会被拉变形。等比适配（例如按
          canvas 尺寸 ``min(cw/fw, ch/fh)``）应在调用方算好再传进来。
        - 应该在 canvas 尺寸变化时调用，不需要每帧调用。尺寸没变时不会重复转换。
        - 宽高会夹到 ``RENDER_SIZE_MAX``(8192) 以内，防止调用方给出荒谬值。
        """
        try:
            wi, hi = int(w), int(h)
        except (TypeError, ValueError):
            return
        if wi <= 0 or hi <= 0:
            wi = hi = 0
        else:
            wi = min(wi, RENDER_SIZE_MAX)
            hi = min(hi, RENDER_SIZE_MAX)
        with self._slock:
            self._render_size = (wi, hi)

    @property
    def render_size(self) -> tuple[int, int]:
        """Current target render size; ``(0, 0)`` means no scaling."""
        with self._slock:
            return self._render_size

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

    def _thread_main(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 15Mbps×冗余拷贝多入口扇出，加大内核缓冲吸收解码抖动
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 << 10)
            sock.bind(("0.0.0.0", 0))
            # D1：靠 select 阻塞等待，不再靠 recv 超时；recv 本身保持非阻塞，
            # 这样 drain 循环不会被 15.6 ms 的超时粒度拖慢。
            sock.settimeout(0.0)
            with self._slock:
                self._sock = sock
                self._local_port = sock.getsockname()[1]
            # stop() 可能发生在 bind 之后、循环之前
            if self._stop.is_set():
                return
            self._run_loop(sock)
        except Exception as e:
            logger.exception("h264 viewer crashed: %s", e)
            self._emit(("status", f"H264 接收异常: {e}"))
            self._set_streaming(False)
        finally:
            sock.close()
            with self._slock:
                self._sock = None
                self._local_port = 0
            self._thread = None

    def _run_loop(self, sock: socket.socket) -> None:
        target = (self.server_host, self.port)
        depack = _Depacketizer()
        codec = av.CodecContext.create("h264", "r")
        codec.options = {"flags": "low_delay"}
        # D9：显式固定 SLICE 线程。FRAME 线程会给解码器加 N−1 帧缓冲，也就是凭空
        # 多一帧延迟；本机 av 17.1.0 默认就是 SLICE，但 requirements.txt 只写了
        # av>=11.0，不能依赖默认值。
        try:
            codec.thread_type = "SLICE"
        except Exception:  # pragma: no cover - 老版本 PyAV 属性名不同
            logger.warning("设置 codec.thread_type=SLICE 失败，沿用默认")
        planner = self._nack
        last_ping = 0.0
        idr_until_frame = True
        last_idr = [0.0]
        last_rtp_at = 0.0
        started_at = time.monotonic()
        last_warn = 0.0
        last_stat = time.monotonic()
        wait_idr = True

        pending: dict[int, bytes] = {}
        pending_at: dict[int, float] = {}
        playhead: int | None = None
        max_seq: int | None = None
        playhead_at = 0.0
        pending_bytes = 0
        rate_win: deque[tuple[float, int]] = deque()
        bps = 0.0
        loop_us_avg = 0.0
        loop_us_max = 0.0

        def backlog_packets() -> int:
            """Return the pending sequence distance in O(1)."""
            if playhead is None or max_seq is None:
                return 0
            d = (max_seq - playhead) & 0xFFFF
            return 0 if d > 0x8000 else d

        def backlog_ms(now: float) -> float:
            """Return the oldest queued packet's age in milliseconds."""
            if playhead is None:
                return 0.0
            at = pending_at.get(playhead)
            return 0.0 if at is None else max(0.0, (now - at) * 1000.0)

        def queue_ms_of(nbytes: int) -> float:
            """Estimate queued time from bytes and measured bitrate."""
            return nbytes * 8.0 / bps * 1000.0 if bps > 0 else 0.0

        def clear_pending() -> int:
            """Clear the reorder buffer and return the discarded packet count."""
            nonlocal pending_bytes
            n = len(pending)
            pending.clear()
            pending_at.clear()
            pending_bytes = 0
            # 队列被整体清空（信号中断 / seq 重置）后，NACK 空洞状态不再有意义
            planner.reset()
            return n

        def trim_backlog() -> int:
            """Discard old queued packets and request an IDR for recovery."""
            nonlocal playhead, playhead_at, wait_idr
            nonlocal pending_bytes, last_warn
            if not pending or playhead is None or max_seq is None:
                return 0
            now = time.monotonic()
            was_pkts = backlog_packets()
            was_ms = int(backlog_ms(now))
            was_bytes = pending_bytes
            floor_at = now - BACKLOG_KEEP_MS / 1000.0
            start = max_seq
            walked = 0
            while pending_at.get(start, now) > floor_at:
                prev = (start - 1) & 0xFFFF
                if prev not in pending:
                    break
                start = prev
                walked += 1
            span = (max_seq - start) & 0xFFFF
            if start == playhead:
                return 0
            # 待丢集合 = start 之前的包。**空集合必须直接返回 0**：
            # 此时 start > playhead 只可能是「playhead 上有洞」，而那个洞还没到
            # REORDER_S 判决时刻、正在被 D3 第一级（NACK）修复。旧代码在这里
            # 无条件 wait_idr=True + request_idr()，等于把 NACK 恢复作废：
            # 实测冷启动（bps 未测出 -> 字节预算取 64KB 下限，而 T1 的 IDR 帧
            # 就有 50~100KB）在 IDR 帧里丢 1 个包就会命中，drop_events=1、
            # drops=0、frames=0 —— 整个首帧黑屏，还要再等一次 IDR 往返。
            doomed = [s for s in pending if ((s - start) & 0xFFFF) > span]
            if not doomed:
                return 0
            dropped = 0
            for s in doomed:
                pending_bytes -= len(pending.pop(s))
                pending_at.pop(s, None)
                dropped += 1
            self._drops += dropped
            self._drop_events += 1
            playhead = start
            playhead_at = 0.0
            # 队列被丢过一轮，NACK 空洞记录不再有对应关系（否则被丢掉的空洞
            # 会在下一次 resolve_below 时被误计成 recovered，污染 §8.1 的命中率）
            planner.reset()
            depack.reset()
            wait_idr = True
            request_idr()
            t = time.monotonic()
            if t - last_warn >= 1.0:
                last_warn = t
                logger.warning(
                    "drop-old: 积压 %d 包/~%dms/%d 字节，丢弃 %d 包，"
                    "playhead->%d，保留 %d 包，请求 IDR",
                    was_pkts, was_ms, was_bytes, dropped, start, walked)
            return dropped

        def send_ctrl(kind: int) -> None:
            try:
                sock.sendto(CONTROL_MAGIC + bytes([kind]), target)
            except OSError:
                pass

        def send_nack(datagrams: list[bytes]) -> None:
            """发出本轮 NACK（D3 第一级恢复）。计数在 NackPlanner 内。"""
            for dg in datagrams:
                try:
                    sock.sendto(dg, target)
                except OSError:
                    pass

        def request_idr() -> None:
            """Request an IDR at most once per retry interval."""
            t = time.monotonic()
            if t - last_idr[0] >= IDR_RETRY_S:
                last_idr[0] = t
                send_ctrl(CTRL_IDR)

        def declare_loss(nmiss: int) -> None:
            """Discard the incomplete access unit and wait for an IDR.

            D3 的**第二级回退**：``REORDER_S`` 到期仍缺（NACK 也没补上）才走到这里。
            """
            nonlocal wait_idr
            wait_idr = True
            depack.reset()
            self._lost_pkts += nmiss
            self._loss_events += 1
            planner.reset()
            request_idr()

        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_ping >= PING_INTERVAL_S:
                last_ping = now
                try:
                    sock.sendto(CONTROL_MAGIC + bytes([CTRL_IDR if idr_until_frame
                                                      else CTRL_PING]), target)
                except OSError:
                    pass
                if idr_until_frame:
                    send_ctrl(CTRL_IDR)
            # 等 IDR 期间主动重试请求。
            #
            # 上面那次「首帧前的 IDR 请求」必然是**发不出去的**：
            # `rtp_relay._on_control` 只把 CBR 转发给 `age <= UPSTREAM_STALE_S`
            # 的上游，而上游是 viewer 起来之后由第一个 RTP 包学到的（实测约
            # 3 ms 之后）。也就是说这一包 100% 被中继静默丢掉。之后
            # `idr_until_frame` 变 False，1 Hz 的 ping 就只发 CTRL_PING
            # （中继回 PONG 给 viewer，viewer 不看），**再也不会上行 IDR**。
            # 于是首帧只能等 K230 的下一个周期 IDR —— GOP_LEN = 60 时最长 2 s
            # （§5 的取舍）。实测（真实 server.app 子进程 + 假 K230）：整个
            # 会话 `idr_req == 0`。
            #
            # request_idr() 自带 IDR_RETRY_S(200ms) 限频，比 K230 侧 IdrGate 的
            # 400 ms 更密也不会造成额外 IDR；正常播放时 wait_idr 为 False，
            # 这一句不产生任何流量。
            if wait_idr:
                request_idr()
            if (not last_rtp_at and now - started_at > NO_RTP_FALLBACK_S) or (
                    last_rtp_at and now - last_rtp_at > NO_RTP_FALLBACK_S):
                self._set_streaming(False)
                # 信号中断：清掉旧包袱，避免恢复后 backlog 虚高误杀
                clear_pending()
                depack.reset()
                playhead = None
                max_seq = None
                self._emit(("status", "fallback"))
                # D1：不能用 _stop.wait(0.5) —— 那会让 stop() 最坏等 500 ms。
                # 拆成短片并检查停止标志，最坏退出延迟 ≈ SIGNAL_LOSS_STEP_S。
                backoff_until = time.monotonic() + SIGNAL_LOSS_BACKOFF_S
                while not self._stop.is_set() and time.monotonic() < backoff_until:
                    time.sleep(SIGNAL_LOSS_STEP_S)
                last_rtp_at = 0.0
                started_at = time.monotonic()
                continue

            loop_t0 = time.perf_counter()
            # D1：事件驱动。旧实现是 settimeout(0.0) 忙轮询 + 循环末尾
            # _stop.wait(0.002)：Windows 上后者 p50 15.5 ms（附录 A.1），每个包
            # 平均多等 ~8 ms、最多 16 ms。现在改成「有包立即唤醒、无包最多阻塞一个
            # 15.6 ms 量子」，超时只兜底 ping / 统计 / 信号丢失检查。
            #
            # 待发的 NACK 轮次若在 SELECT_SPIN_S 内到期，就**不阻塞**、直接空转到
            # 那一刻：Windows 上 select 的短超时会被取整到 15.6 ms，靠它发 4 ms 的
            # 首轮会迟到 12 ms（实测补包时延 17 ms vs 5 ms）。空转窗口有界 ——
            # 空洞最迟 REORDER_S(40 ms) 会被 declare_loss 清掉并 planner.reset()。
            t_wait = time.perf_counter()
            due = planner.next_due(t_wait)
            readable: Sequence[socket.socket] = ()
            if not (planner.tracked
                    and due - t_wait <= SELECT_SPIN_S):
                wait_s = SELECT_TIMEOUT_S
                if due - t_wait < wait_s:
                    wait_s = max(SELECT_MIN_WAIT_S, due - t_wait)
                try:
                    readable, _, _ = select.select([sock], [], [], wait_s)
                except (OSError, ValueError):
                    if self._stop.is_set():
                        break
                    logger.warning("h264 viewer select 失败，退出接收循环")
                    break
            # 等待不算「循环耗时」，从等待结束之后再计时
            loop_t0 = time.perf_counter()
            got_bytes = 0
            # 有数据可读时才 drain；非阻塞，最多 RECV_DRAIN_MAX 个包一轮，
            # 避免单轮饿死周期性任务。
            for _ in range(RECV_DRAIN_MAX if readable else 0):
                try:
                    data, _addr = sock.recvfrom(2048)
                except ConnectionResetError:
                    # Windows S1：上一次 sendto 收到 ICMP 端口不可达后，同一 socket
                    # 的下一次 recvfrom 抛 WinError 10054。包还在内核缓冲里，必须
                    # continue 而不是 break，否则整轮突发会被丢掉并白等一个 select
                    # 周期。与 server/rtp_relay.py 的处理一致。
                    continue
                except (BlockingIOError, socket.timeout):
                    break
                except OSError:
                    break
                if data[:3] == CONTROL_MAGIC:
                    continue
                if len(data) < 12:
                    continue
                last_rtp_at = time.monotonic()
                try:
                    seq = struct.unpack_from(">H", data, 2)[0]
                except struct.error:
                    continue
                if max_seq is not None and _seq_after(seq, max_seq) is False \
                        and (max_seq - seq) & 0xFFFF > SEQ_RESET_DISTANCE:
                    self._seq_resets += 1
                    logger.warning("seq 重置 (max=%d -> %d)：丢弃 %d 包残留，"
                                   "从 %d 重新同步",
                                   max_seq, seq, clear_pending(), seq)
                    playhead = None
                    max_seq = None
                    playhead_at = 0.0
                    wait_idr = True
                    depack.reset()
                newer = max_seq is None or _seq_after(seq, max_seq)
                if newer:
                    max_seq = seq
                if playhead is None:
                    if not newer:
                        continue
                    playhead = seq
                # D8（先于 NACK 修的 bug）：已消费/已落后于 playhead 的包（重复拷贝、
                # 迟到的重传）绝不能再进 pending —— 进去就永不被消费，累积到字节预算
                # 会误触发 trim_backlog + IDR 请求。NACK 引入后迟到重传变多，必须挡。
                if _is_stale_seq(seq, playhead):
                    self._dup += 1
                    continue
                if seq in pending:
                    self._dup += 1
                    continue
                pending[seq] = data
                pending_at[seq] = time.monotonic()
                pending_bytes += len(data)
                got_bytes += len(data)
                self._pkts_rx += 1

            t_now = time.monotonic()
            if got_bytes:
                rate_win.append((t_now, got_bytes))
            while rate_win and rate_win[0][0] < t_now - 2.0:
                rate_win.popleft()
            if len(rate_win) >= 2:
                span = rate_win[-1][0] - rate_win[0][0]
                if span > 0.2:
                    bps = sum(b for _t, b in rate_win) * 8.0 / span
                    with self._slock:
                        self._bitrate_kbps = bps / 1000.0
                        self._queue_ms = queue_ms_of(pending_bytes)

            now = time.monotonic()
            # D3 两级恢复。
            #   第一级：发现空洞 → 等 4 ms 立即 NACK，25 ms 后仍缺重发一次（每洞 ≤2 次）
            #   第二级：REORDER_S(40 ms) 到期仍缺 → declare_loss（等 IDR）回退
            if playhead is not None and playhead not in pending:
                if not pending:
                    playhead_at = 0.0
                    planner.resolve_below(playhead)
                else:
                    ahead = [s for s in pending if _seq_after(s, playhead)]
                    nmiss = ((min(ahead) - playhead) & 0xFFFF) if ahead else 1
                    if nmiss < 1:
                        nmiss = 1
                    if not playhead_at:
                        playhead_at = now
                        self._gaps += 1
                    if nmiss >= SEQ_RESET_MIN_DISTANCE:
                        # 上游（K230 脚本）重启：seq 跳回 0，于是 playhead 与
                        # 新流之间看起来是「一个 nmiss 巨大的空洞」。
                        #
                        # 这**不是**丢包，走 declare_loss 会把 nmiss 原样记进
                        # pkts_lost（实测 ~20536），把 loss_pct 永久污染成一个
                        # 无意义的数；而且 NACK 出去的 96 个 seq 永远补不回来，
                        # 白白占掉 60 seq/s 的限速额度、白等满 REORDER_S。
                        #
                        # 原先的 seq 重置分支（两条件互斥，见 SEQ_RESET_NOTE）
                        # 和 `backlog_packets() > SEQ_RESET_DISTANCE` 分支都是
                        # **死代码**：nmiss 与 backlog_packets 恒 < 0x8000
                        # （max_seq 只在 _seq_after 为真时前进），所以真发生
                        # 重置时 seq_resets 计数器一次都不涨。这里补上活的判据。
                        new_head = min(ahead) if ahead else None
                        self._seq_resets += 1
                        logger.warning(
                            "seq 重置：playhead=%d 落后 %d 个序号（上游重启？），"
                            "丢弃 %d 包残留，从 %s 重新同步",
                            playhead, nmiss, clear_pending(), new_head)
                        playhead = new_head
                        if new_head is not None:
                            max_seq = new_head
                        playhead_at = 0.0
                        wait_idr = True
                        depack.reset()
                        request_idr()
                    else:
                        # 空洞请求前 NACK_MAX_SEQS_PER_PLAN(96) 个 seq：再深的空洞在
                        # REORDER_S 内也补不回来（K230 环形缓冲只有 128 包），而且总量
                        # 本来就被 60 seq/s 限速卡住。
                        send_nack(planner.plan(
                            [(playhead + i) & 0xFFFF
                             for i in range(min(nmiss, NACK_MAX_SEQS_PER_PLAN))],
                            now))
                        if now - playhead_at >= REORDER_S:
                            declare_loss(nmiss)
                            playhead = min(ahead) if ahead else None
                            playhead_at = now
            elif playhead is not None:
                if playhead_at:
                    self._reorder_fixed += 1
                # 空洞被填上：结算 NACK，命中过的空洞算一次恢复
                planner.resolve_below(playhead)
                playhead_at = 0.0

            with self._slock:
                self._backlog_packets = backlog_packets()
                self._backlog_bytes = pending_bytes

            if backlog_packets() > SEQ_RESET_DISTANCE:
                self._seq_resets += 1
                logger.warning("playhead 落后 %d 个序号（>半个序号空间）："
                               "丢弃 %d 包残留并重新同步",
                               backlog_packets(), clear_pending())
                playhead = None
                max_seq = None
                playhead_at = 0.0
                wait_idr = True
                depack.reset()
            elif (backlog_packets() > BACKLOG_MIN_PKTS
                    and backlog_ms(now) > BACKLOG_MAX_MS) \
                    or pending_bytes > backlog_byte_budget(bps):
                # 字节预算按实测码率算（bps * 0.25s），不再是固定 2 MB —— 见
                # backlog_byte_budget 的算式。T1 实测约 3.4 Mbps 时约 106 KB。
                trim_backlog()
                with self._slock:
                    self._backlog_packets = backlog_packets()
                    self._backlog_bytes = pending_bytes

            got_frame = False
            burst = 0
            while playhead is not None and playhead in pending:
                data = pending.pop(playhead)
                packet_at = pending_at.pop(playhead, now)
                pending_bytes -= len(data)
                au = depack.push(data, packet_at)
                playhead = (playhead + 1) & 0xFFFF
                if au is None:
                    continue
                if wait_idr and not depack.last_au_idr:
                    self._dropped += 1
                    continue
                try:
                    frames = codec.decode(av.Packet(au))
                except Exception as e:
                    self._decode_fails += 1
                    wait_idr = True
                    t = time.monotonic()
                    if t - last_warn >= 2.0:
                        last_warn = t
                        logger.warning(
                            "decode failed #%d: %s | au_head=%s",
                            self._decode_fails, e, au[:16].hex())
                    request_idr()
                    continue
                for frame in frames:
                    # D4：色彩转换 + 缩放在解码线程里一次做完。swscale 的
                    # reformat 只要 0.50/0.60 ms（720p/1080p，附录 A.3），
                    # 而 to_image() 5.0/9.5 ms + UI 线程 resize 8.3 ms。
                    # 目标尺寸由 UI 线程通过 set_render_size() 写入；
                    # 未设置时保持原样直接 to_image()。
                    with self._slock:
                        cw, ch = self._render_size
                    if cw > 0 and ch > 0:
                        img = frame.reformat(
                            width=cw, height=ch, format="rgb24").to_image()
                    else:
                        img = frame.to_image()
                    rendered_at = time.monotonic()
                    with self._slock:
                        if depack.last_au_start_at > 0:
                            self._render_age_ms = max(
                                0.0, (rendered_at - depack.last_au_start_at) * 1000.0)
                        self._frames += 1
                    idr_until_frame = False
                    wait_idr = False
                    got_frame = True
                    self._set_streaming(True)
                    self._emit(("frame", img))
                    break
                if got_frame:
                    burst += 1
                    if burst >= DECODE_BURST_MAX:
                        break

            with self._slock:
                self._backlog_packets = backlog_packets()
                self._backlog_bytes = pending_bytes

            loop_us = (time.perf_counter() - loop_t0) * 1e6
            loop_us_avg += (loop_us - loop_us_avg) * 0.1
            if loop_us > loop_us_max:
                loop_us_max = loop_us
            with self._slock:
                self._loop_us_avg = loop_us_avg
                self._loop_us_max = loop_us_max

            # D1：原来的 `if not got_frame: self._stop.wait(0.002)` 已删除。
            # 它在 Windows 上实测 p50 15.5 ms（附录 A.1），而本轮开头的 select
            # 已经在「无数据」时阻塞最多 SELECT_TIMEOUT_S；有数据时 select 立即
            # 返回，不存在那层 15.6 ms 取整延迟。

            if now - last_stat >= 10.0:
                last_stat = now
                st = self.stats()
                logger.warning(
                    "h264 rtp: rx=%d lost=%d(%.2f%%) gaps=%d fixed=%d "
                    "frames=%d dropped=%d fails=%d dup=%d | backlog=%d pkt/"
                    "%dB drops=%d(%d 次) resets=%d | nack=%d pkt/%d seq "
                    "命中=%d 限速丢=%d | render_age=%.0fms "
                    "queue=%.0fms %dkbps loop=%.0f/%.0fus",
                    st["pkts_rx"], st["pkts_lost"], st["loss_pct"],
                    st["gaps_seen"], st["reorder_fixed"], st["frames"],
                    st["dropped_wait_idr"], st["decode_fails"],
                    st["pkts_dup"], st["backlog_packets"], st["backlog_bytes"],
                    st["drops"], st["drop_events"], st["seq_resets"],
                    st["nack_sent"], st["nack_seqs"],
                    st["nack_recovered"], st["nack_rounds_dropped"],
                    st["render_age_ms"], st["queue_ms"], st["bitrate_kbps"],
                    st["loop_us_avg"], st["loop_us_max"])

    @property
    def render_age_ms(self) -> float:
        """Age of the rendered access unit since its first packet arrived."""
        with self._slock:
            return self._render_age_ms

    @property
    def queue_ms(self) -> float:
        """Estimated queued time in milliseconds."""
        with self._slock:
            return self._queue_ms

    @property
    def backlog_packets(self) -> int:
        """Pending packet count."""
        with self._slock:
            return self._backlog_packets

    @property
    def backlog_bytes(self) -> int:
        """Pending byte count."""
        with self._slock:
            return self._backlog_bytes

    @property
    def drops(self) -> int:
        """Number of packets discarded to cap the backlog."""
        with self._slock:
            return self._drops

    @property
    def loop_us_avg(self) -> float:
        """Average receive/decode loop duration in microseconds."""
        with self._slock:
            return self._loop_us_avg

    @property
    def loop_us_max(self) -> float:
        """Maximum receive/decode loop duration in microseconds."""
        with self._slock:
            return self._loop_us_max

    @property
    def bitrate_kbps(self) -> float:
        """Measured receive rate in kbps."""
        with self._slock:
            return self._bitrate_kbps

    def stats(self) -> dict:
        with self._slock:
            frames, streaming = self._frames, self._streaming
            decode_fails, dropped = self._decode_fails, self._dropped
            pkts_rx, lost, dup = self._pkts_rx, self._lost_pkts, self._dup
            loss_events, gaps = self._loss_events, self._gaps
            fixed, drops, drop_ev = self._reorder_fixed, self._drops, self._drop_events
            resets = self._seq_resets
            bp, bb = self._backlog_packets, self._backlog_bytes
            render_age, queue = self._render_age_ms, self._queue_ms
            bitrate, lavg, lmax = self._bitrate_kbps, self._loop_us_avg, self._loop_us_max
        planner = self._nack
        total = pkts_rx + lost
        return {"mode": "h264", "frames": frames,
                "streaming": streaming,
                "decode_fails": decode_fails,
                "dropped_wait_idr": dropped,
                "pkts_rx": pkts_rx,
                "pkts_lost": lost,
                "pkts_dup": dup,
                "loss_pct": round(100.0 * lost / total, 3)
                if total else 0.0,
                "loss_events": loss_events,
                "gaps_seen": gaps,
                "reorder_fixed": fixed,
                "backlog_packets": bp,
                "backlog_bytes": bb,
                "drops": drops,
                "drop_events": drop_ev,
                "seq_resets": resets,
                "nack_sent": planner.sent_packets,
                "nack_seqs": planner.sent_seqs,
                "nack_recovered": planner.recovered,
                "nack_rounds_dropped": planner.dropped,
                "render_age_ms": round(render_age, 1),
                "queue_ms": round(queue, 1),
                "bitrate_kbps": round(bitrate, 1),
                "loop_us_avg": round(lavg, 1),
                "loop_us_max": round(lmax, 1)}

    def nack_stats(self) -> dict:
        """NACK 相关计数（与 ``stats()`` 中的同名字段同源）。"""
        p = self._nack
        return {"nack_sent": p.sent_packets, "nack_seqs": p.sent_seqs,
                "nack_recovered": p.recovered,
                "nack_rounds_dropped": p.dropped,
                "nack_pending_holes": p.tracked}


# ==================================================================== 自检
# 不拥有 test/ 目录，所以纯逻辑（NackPlanner / 过期包判定 / 字节预算 / 渲染尺寸
# 语义 / 端到端 localhost 解码）都在这里自跑。风格参考 test/test_rtp_redundancy.py。
#
#   python desktop/streaming/h264_viewer.py

STATS_KEYS = (
    "mode", "frames", "streaming", "decode_fails", "dropped_wait_idr",
    "pkts_rx", "pkts_lost", "pkts_dup", "loss_pct", "loss_events",
    "gaps_seen", "reorder_fixed", "backlog_packets", "backlog_bytes",
    "drops", "drop_events", "seq_resets", "render_age_ms", "queue_ms",
    "bitrate_kbps", "loop_us_avg", "loop_us_max",
)
NACK_STATS_KEYS = ("nack_sent", "nack_seqs", "nack_recovered",
                   "nack_rounds_dropped")


class _SkipCheck(Exception):
    """环境不具备该用例的前置条件。"""


def _parse_nack(data: bytes) -> tuple[list[int], bool]:
    """K230 侧 ``parse_nack`` 的等价实现：长度必须**精确**等于 5 + count*2。"""
    if data is None or len(data) < 5:
        return [], False
    if bytes(data[:3]) != CONTROL_MAGIC or data[3] != CTRL_NACK:
        return [], False
    count = data[4]
    if count < 1 or count > NACK_MAX_SEQS:
        return [], False
    if len(data) != 5 + count * 2:
        return [], False
    return [((data[5 + 2 * i] << 8) | data[6 + 2 * i]) & 0xFFFF
            for i in range(count)], True


def _check_nack_wire_format():
    """线路契约：b"CBR"+0x03+count(u8)+seq(u16 BE)*count，长度恰好 5+2*count。"""
    for count in (1, 2, 5, 31, 32):
        seqs = [(1000 + i) & 0xFFFF for i in range(count)]
        dg = encode_nack(seqs)
        assert len(dg) == 5 + count * 2, (count, len(dg))
        assert dg[:4] == CONTROL_MAGIC + bytes([CTRL_NACK]), dg[:4]
        assert dg[4] == count, (dg[4], count)
        got, ok = _parse_nack(dg)
        assert ok and got == seqs, (got, seqs)
    try:
        encode_nack([])
    except ValueError:
        pass
    else:
        raise AssertionError("空 seq 列表必须报错，不能发出 count=0 的包")
    # 多于 32 个必须由调用方分片（encode_nack 自己只取前 32）
    many = list(range(70))
    dg = encode_nack(many)
    assert dg[4] == NACK_MAX_SEQS and len(dg) == 5 + 2 * 32


def _check_nack_chunking():
    """70 个空洞 → 分片成 32/32/6 三个报文，每个长度都合法。

    这里把限速关掉（0 = 不限），否则 70 > 60/s 的总量上限会被限速丢掉 10 个 ——
    那是限速用例要验证的事。
    """
    p = NackPlanner(rate_limit_seqs_per_s=0)
    now = 100.0
    missing = list(range(500, 570))
    assert p.plan(missing, now) == []          # 首见只登记
    out = p.plan(missing, now + 0.005)
    assert [(len(d) - 5) // 2 for d in out] == [32, 32, 6], \
        [(len(d) - 5) // 2 for d in out]
    assert [d[4] for d in out] == [32, 32, 6]
    for d in out:
        seqs, ok = _parse_nack(d)
        assert ok and len(seqs) == d[4]
    assert p.sent_packets == 3 and p.sent_seqs == 70
    # 每洞已用掉第 1 轮，再调一次会发第 2 轮（3 报文 → 仍是 32/32/6）
    assert [(len(d) - 5) // 2 for d in p.plan(missing, now + 0.05)] == [32, 32, 6]
    assert p.sent_packets == 6 and p.sent_seqs == 140
    # 第 3 轮被 max_rounds 挡住（同时验证 encode_nack 自身的 32 上限）
    assert p.plan(missing, now + 0.20) == []


def _check_nack_plan_cap():
    """NACK_MAX_SEQS_PER_PLAN 之上不再请求（限速才是真正的总量上限）。"""
    assert NACK_MAX_SEQS_PER_PLAN % NACK_MAX_SEQS == 0
    p = NackPlanner(rate_limit_seqs_per_s=0)
    missing = list(range(300, 300 + NACK_MAX_SEQS_PER_PLAN + 50))
    p.plan(missing[:NACK_MAX_SEQS_PER_PLAN + 50], 500.0)
    out = p.plan(missing, 500.005)
    assert p.sent_seqs == NACK_MAX_SEQS_PER_PLAN, p.sent_seqs
    assert len(out) == NACK_MAX_SEQS_PER_PLAN // NACK_MAX_SEQS
    for d in out:
        assert d[4] == NACK_MAX_SEQS and len(d) == 5 + 2 * NACK_MAX_SEQS


def _check_nack_two_stage():
    """时序：+4ms 首轮 → +25ms 重发一次 → 之后不再发（第二轮是回退前的最后机会）。"""
    p = NackPlanner()
    t = 100.0
    assert p.plan([7], t) == []                       # 首见
    r1 = p.plan([7], t + 0.0039)
    assert r1 == [], "未到 4ms 不应发"
    r1 = p.plan([7], t + 0.004)
    assert len(r1) == 1 and _parse_nack(r1[0])[0] == [7]
    assert p.plan([7], t + 0.020) == [], "首轮后 20ms 不应重发"
    r2 = p.plan([7], t + 0.030)
    assert len(r2) == 1 and _parse_nack(r2[0])[0] == [7], r2
    assert p.plan([7], t + 0.060) == [], "最多 2 轮"
    assert p.plan([7], t + 5.0) == []
    assert p.sent_packets == 2 and p.sent_seqs == 2 and p.max_rounds == 2


def _check_nack_resolve():
    """空洞补上 → 记一次 recovered；reset()（declare_loss）不算恢复。"""
    p = NackPlanner()
    t = 200.0
    p.plan([10, 11], t)
    assert p.plan([10, 11], t + 0.005) and p.sent_seqs == 2
    # 只补上 10（playhead 推到 11）→ 结算 <= 11 的记录
    assert p.resolve_below(11) == 2 and p.recovered == 2
    assert p.tracked == 0
    # 空洞在发出前就被乱序包补上 → 不算恢复
    p.plan([12], t + 1.0)
    p.resolve_below(13)
    assert p.recovered == 2
    # 发出过 NACK 但最终 declare_loss → reset，不计恢复
    p.plan([14], t + 2.0)
    p.plan([14], t + 2.005)
    p.reset()
    assert p.recovered == 2 and p.tracked == 0


def _check_nack_rate_limit():
    """限速：1 秒内按 count 累计不超过 NACK_RATE_LIMIT_SEQS_PER_S。"""
    limit = 60
    p = NackPlanner(rate_limit_seqs_per_s=limit)
    t = 300.0
    many = list(range(1000, 1200))       # 200 个空洞
    p.plan(many, t)
    total = 0
    for _ in range(6):                    # 6 轮 × 每洞 2 轮上限
        for dg in p.plan(many, t + 0.030):
            total += dg[4]
    assert total <= limit, total
    assert p.dropped > 0, "限速丢弃必须计数"
    # 窗口滑动 1s 之后额度恢复（新 seq 需要登记 + 到期两轮）
    fresh = list(range(5000, 5005))
    assert p.plan(fresh, t + 2.0) == []
    out = p.plan(fresh, t + 2.005)
    assert len(out) == 1, "1s 之后应恢复额度"


def _check_nack_ttl_gc():
    """空洞记录必须会过期回收，不能泄漏。"""
    p = NackPlanner(ttl_s=0.05)
    p.plan([1, 2, 3], 400.0)
    assert p.tracked == 3
    p.plan([], 400.6)
    assert p.tracked == 0


def _check_stale_seq():
    """D8：落后于 playhead 的包（含重复/迟到重传）必须判为过期。"""
    assert not _is_stale_seq(100, 100)          # 就是 playhead
    assert not _is_stale_seq(101, 100)
    assert not _is_stale_seq(0x8000 - 1, 0x8000 - 1)
    assert _is_stale_seq(99, 100)
    assert _is_stale_seq(0xFFFF, 0)            # 回绕：0xFFFF 在 0 之前
    assert _is_stale_seq(65500, 3)              # 回绕：旧包
    assert not _is_stale_seq(5, 0xFFFF)         # 回绕：新包（0xFFFF 的下一个是 0）
    assert not _is_stale_seq(0, 0xFFFF)
    assert not _is_stale_seq(123, None)         # playhead 未建立时不过滤
    assert _is_stale_seq(0, 32768)              # 恰好半个序号空间算过期


def _check_backlog_budget():
    """字节预算按码率算（doc §4.5），不再固定 2MB。"""
    assert backlog_byte_budget(0.0) == BACKLOG_BUDGET_MIN_BYTES
    assert backlog_byte_budget(-1.0) == BACKLOG_BUDGET_MIN_BYTES
    # T1: 3.4 Mbps * 0.25s / 8 = 106250 B
    assert backlog_byte_budget(3.4e6) == 106250, backlog_byte_budget(3.4e6)
    # T3: 6.8 Mbps -> 212500 B
    assert backlog_byte_budget(6.8e6) == 212500, backlog_byte_budget(6.8e6)
    # 上限兜底：100 Mbps -> 512KB
    assert backlog_byte_budget(100e6) == BACKLOG_MAX_BYTES
    # 下限：极低码率不退到 0，且必须容得下几帧（否则会误杀正常队列）
    assert backlog_byte_budget(1e3) == BACKLOG_BUDGET_MIN_BYTES
    assert BACKLOG_BUDGET_MIN_BYTES >= 64 << 10
    assert backlog_byte_budget(0.0) > (25 << 10), "下限要容得下 T3 的一帧"
    # 关键回归：旧 2MB 固定值相当于 6.5s 队列，现在同码率下只有 0.25s
    assert backlog_byte_budget(2.6e6) < (2 << 20)


def _check_render_size_api():
    """set_render_size 语义：默认 (0,0)；<=0 视为不缩放；线程安全。"""
    v = H264Viewer("127.0.0.1", 9)
    assert v.render_size == (0, 0)
    v.set_render_size(1280, 720)
    assert v.render_size == (1280, 720)
    v.set_render_size(1280, 720)                 # 幂等
    assert v.render_size == (1280, 720)
    v.set_render_size(0, 720)
    assert v.render_size == (0, 0)
    v.set_render_size(-3, -3)
    assert v.render_size == (0, 0)
    v.set_render_size(10 ** 9, 10 ** 9)
    assert v.render_size == (RENDER_SIZE_MAX, RENDER_SIZE_MAX)
    v.set_render_size("x", "y")                  # 非法输入忽略
    assert v.render_size == (RENDER_SIZE_MAX, RENDER_SIZE_MAX)


def _annexb_nals(au: bytes) -> list[bytes]:
    """把 Annex-B 字节流按起始码切成 NAL 列表（不含起始码）。"""
    out: list[bytes] = []
    n = len(au)
    i = 0
    start = -1
    while i + 3 <= n:
        if au[i:i + 4] == b"\x00\x00\x00\x01":
            sc = 4
        elif au[i:i + 3] == b"\x00\x00\x01":
            sc = 3
        else:
            i += 1
            continue
        if start >= 0:
            out.append(au[start:i])
        i += sc
        start = i
    if start >= 0:
        out.append(au[start:])
    return [x for x in out if x]


def _rtp_packets(au: bytes, seq: int, ts: int, ssrc: int = 0xDEADBEEF,
                 max_payload: int = 1180) -> tuple[list[bytes], int]:
    """把一个 Annex-B AU 打成 RTP 报文（单 NAL 或 FU-A 分片）。

    PyAV 的 libx264 编码器输出自带起始码，所以先切 NAL；marker 只打在 AU 的最后
    一个分片上，这正是 ``_Depacketizer`` 判定 AU 结束的方式。
    """
    pkts: list[bytes] = []
    nals = _annexb_nals(au)
    for ni, nal in enumerate(nals):
        last_nal = ni == len(nals) - 1
        if len(nal) <= max_payload:
            frags = [(nal, True)]
        else:
            step = max_payload - 2
            body = nal[1:]
            frags = []
            i = 0
            first = True
            while i < len(body):
                chunk = body[i:i + step]
                i += step
                last = i >= len(body)
                b0 = (nal[0] & 0x60) | 28
                b1 = (nal[0] & 0x1F) | (0x80 if first else 0) | (0x40 if last else 0)
                frags.append((bytes([b0, b1]) + chunk, last))
                first = False
        for payload, last_frag in frags:
            marker = last_nal and last_frag
            pkts.append(struct.pack(">BBHII", 0x80,
                                    0x80 if marker else 0x00,
                                    seq & 0xFFFF, ts, ssrc) + payload)
            seq += 1
    return pkts, seq & 0xFFFF


def _encode_test_aus(w: int, h: int, n: int, noise: bool = False) -> list[bytes]:
    """用 PyAV 造一段小的 H.264 流（不是 K230 码流，只验证接收/解码/渲染路径）。

    ``noise=True`` 时画面是随机噪点，帧足够大（会被 FU-A 分片），用来挖空洞；
    否则纯色帧只有 100 字节左右，根本切不出多包。
    """
    cc = av.CodecContext.create("libx264", "w")
    cc.width = w
    cc.height = h
    cc.pix_fmt = "yuv420p"
    cc.options = {"preset": "ultrafast", "tune": "zerolatency",
                  "profile": "baseline",
                  "x264-params": "keyint=30:min-keyint=30:bframes=0:"
                                 "repeat-headers=1:scenecut=0"}
    out: list[bytes] = []
    rng = random.Random(1234)
    for i in range(n):
        if noise:
            raw = bytes(rng.randrange(256) for _ in range(w * h * 3))
            img = Image.frombytes("RGB", (w, h), raw)
        else:
            img = Image.new("RGB", (w, h), ((i * 17) % 256, (i * 37) % 256, 200))
        vf = av.VideoFrame.from_image(img).reformat(format="yuv420p")
        for p in cc.encode(vf):
            out.append(bytes(p))
    for p in cc.encode(None):
        out.append(bytes(p))
    return out


def _check_end_to_end_localhost():
    """localhost 端到端：假中继 + 真实 UDP + 真解码 + NACK 补包 + D4 尺寸。"""
    if av is None:
        raise _SkipCheck("PyAV 不可用")
    w, h = 160, 120
    tw, th = 80, 60                        # 与源同宽高比，避免拉伸
    n_aus = 14
    relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    relay.bind(("127.0.0.1", 0))
    relay.settimeout(5.0)
    rport = relay.getsockname()[1]
    v = H264Viewer("127.0.0.1", rport)
    v.set_render_size(tw, th)
    v.start()
    try:
        # 1) 等 viewer 用 CBR 包向中继注册（真实 server 就是这样学习下游的）
        vaddr = None
        deadline = time.monotonic() + 5.0
        while vaddr is None and time.monotonic() < deadline:
            data, addr = relay.recvfrom(2048)
            if data[:3] == CONTROL_MAGIC:
                vaddr = addr
        assert vaddr is not None, "viewer 未发出 CBR 注册包"

        aus = _encode_test_aus(w, h, n_aus, noise=True)
        assert len(aus) >= n_aus, len(aus)
        seq = 1000
        frames_seen = 0
        img_size = None
        first_packets: list[bytes] = []

        def pump_events():
            nonlocal frames_seen, img_size
            for kind, payload in v.events():
                if kind == "frame":
                    frames_seen += 1
                    img_size = payload.size

        # 2) 发前 4 帧；第 4 帧（噪点，几十个包）挖掉第 2 个包
        drop_seq = None
        drop_pkt = None
        for i, au in enumerate(aus[:4]):
            pkts, seq = _rtp_packets(au, seq, i * 3000)
            if i == 0:
                first_packets = list(pkts)
            for k, pkt in enumerate(pkts):
                if i == 3 and k == 1 and len(pkts) > 2:
                    drop_seq = struct.unpack_from(">H", pkt, 2)[0]
                    drop_pkt = pkt
                    continue
                relay.sendto(pkt, vaddr)
            pump_events()
            time.sleep(0.008)
        assert drop_pkt is not None, "噪点帧应该被切成多个 RTP 包，才能挖出空洞"

        # 3) 收 CBR：NACK 到达就补发那一个包（模拟 K230 环形缓冲的重传）
        deadline = time.monotonic() + 2.0
        nack_ok = False
        while not nack_ok and time.monotonic() < deadline:
            try:
                data, _ = relay.recvfrom(2048)
            except socket.timeout:
                break
            if data[:3] != CONTROL_MAGIC or data[3] != CTRL_NACK:
                continue
            seqs, valid = _parse_nack(data)
            assert valid, "viewer 发出的 NACK 不符合线路契约: %r" % (data,)
            assert 1 <= len(seqs) <= NACK_MAX_SEQS
            if drop_seq in seqs:
                relay.sendto(drop_pkt, vaddr)
                nack_ok = True

        # 4) 补齐剩余帧
        for i, au in enumerate(aus[4:], start=4):
            pkts, seq = _rtp_packets(au, seq, i * 3000)
            for pkt in pkts:
                relay.sendto(pkt, vaddr)
            pump_events()
            time.sleep(0.008)

        # 5) 收尾：等解码出帧
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            pump_events()
            if frames_seen >= 10:
                break
            time.sleep(0.01)
        pump_events()

        st = v.stats()
        missing_keys = [k for k in STATS_KEYS + NACK_STATS_KEYS if k not in st]
        assert not missing_keys, "stats() 缺 key: %s" % missing_keys

        assert nack_ok, "没有收到覆盖空洞序号的 NACK"
        assert st["nack_sent"] >= 1, st
        assert st["nack_seqs"] >= 1, st
        assert st["nack_recovered"] >= 1, ("NACK 补包后应记一次恢复", st)
        assert frames_seen >= 10, ("帧数不足：%d" % frames_seen)
        assert st["frames"] >= 10, st
        assert st["decode_fails"] == 0, st
        assert img_size == (tw, th), ("D4: 帧尺寸应为 set_render_size 的目标尺寸, "
                                      "实际 %s" % (img_size,))
        assert st["render_age_ms"] > 0.0, st
        assert st["streaming"] is True, st
        assert st["loss_events"] == 0, ("补包成功不应走 declare_loss 回退", st)
        assert st["nack_rounds_dropped"] == 0, st

        # 6) D8：重发早就被消费掉的第 0 帧，必须计入 dup 且不入队
        st0 = v.stats()
        for pkt in first_packets:
            relay.sendto(pkt, vaddr)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if v.stats()["pkts_dup"] > st0["pkts_dup"]:
                break
            time.sleep(0.01)
        st1 = v.stats()
        assert st1["pkts_dup"] > st0["pkts_dup"], ("过期包必须计入 dup", st0, st1)
        assert st1["backlog_packets"] <= 64, ("过期包不能滞留在队列里", st1)
        assert st1["drops"] == 0, st1

        # 7) stop() 必须在 100ms 内返回（D1 自唤醒）
        t0 = time.perf_counter()
        v.stop()
        stop_ms = (time.perf_counter() - t0) * 1000.0
        assert stop_ms < 100.0, "stop() 耗时 %.1f ms" % stop_ms
    finally:
        try:
            v.stop()
        except Exception:
            pass
        relay.close()


def _check_depacketizer():
    """脱包 + 解码的最小闭环（RTP → Annex-B → PyAV）。"""
    if av is None:
        raise _SkipCheck("PyAV 不可用")
    aus = _encode_test_aus(64, 48, 4)
    seq = 7
    dep = _Depacketizer()
    got = []
    for i, au in enumerate(aus):
        pkts, seq = _rtp_packets(au, seq, i * 3000)
        for pkt in pkts:
            out = dep.push(pkt)
            if out is not None:
                got.append(out)
    assert len(got) == len(aus), (len(got), len(aus))
    codec = av.CodecContext.create("h264", "r")
    codec.options = {"flags": "low_delay"}
    codec.thread_type = "SLICE"          # D9
    assert int(codec.thread_type.value) == \
        av.codec.context.ThreadType.SLICE.value
    n = 0
    for au in got:
        for _f in codec.decode(av.Packet(au)):
            n += 1
    for _f in codec.decode(None):
        n += 1
    assert n >= 4, "解码帧数不足: %d" % n
    # FU-A 路径：噪点大帧一定会分片
    big = _encode_test_aus(320, 240, 3, noise=True)
    dep2 = _Depacketizer()
    seq = 1
    got2 = 0
    for i, au in enumerate(big):
        pkts, seq = _rtp_packets(au, seq, i * 3000)
        assert len(pkts) > 1, "大帧应被 FU-A 分片"
        for pkt in pkts:
            if dep2.push(pkt) is not None:
                got2 += 1
    assert got2 == len(big), (got2, len(big))


def main() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("_check_") and callable(v)]
    failed = 0
    skipped = 0
    for fn in fns:
        try:
            fn()
        except _SkipCheck as e:
            skipped += 1
            print("SKIP %s (%s)" % (fn.__name__, e))
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAIL %s: %s" % (fn.__name__, e))
        else:
            print("PASS %s" % fn.__name__)
    print("%d/%d passed, %d skipped" % (len(fns) - failed - skipped,
                                         len(fns), skipped))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
