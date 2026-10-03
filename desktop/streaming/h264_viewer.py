"""H264 裸 RTP 接收端（方案 A 的 desktop 段）。

链路：K230 硬编 H264 -> RTP over UDP -> server 纯转发(:8002) -> 本模块
FU-A 解包 -> PyAV(av) 同线程解码 -> PIL 帧事件。

延迟纪律：
  * 接收/解包/解码全在一个 daemon 线程，零 jitter buffer，来帧即解。
  * 事件 deque(maxlen=2)，StreamPanel 只画最新一帧。
  * 序列号缺口先等 REORDER_S 重排窗口，确认真丢包才拒收 P 帧直到 IDR
    （见 REORDER_S 与 declare_loss）。不引入任何重传等待。
  * 启动即持续发 PING（学习地址用）；relay/K230 长时间无流时只报
    no_stream 并继续等待 —— K230 独立上电，静默不等于链路不可用。

Events 与 viewer.Viewer 完全一致：
  ("frame", PIL.Image) | ("status", "streaming"|"no_stream"|text)
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
REORDER_S = 0.100          # 乱序重排窗口。必须 > 一帧间隔(33ms)：帧与帧之间
                          # pending 本来就是空的，不能当成丢包（否则会误判
                          # 丢包 -> wait_idr -> 白等一个 GOP）。
                          # 取 100ms 是因为实测 WiFi 上 UDP 乱序/抖动到几十
                          # 毫秒是常态（test_rtp_reorder.py 里人为延迟
                          # 33ms 就会被判成丢包）。窗口只在真的缺包时才
                          # 贡献延迟，顺序正常时零额外延迟。
                          # 真正的黑屏代价由发送端 GOP 决定（见 rtp_push.py）。

# 积压上限。桌面解码追不上到达时，pending 会单调增长：先把 4MB socket 缓冲
# 撑满（接收停住），再把延迟推到秒级。这里给积压一个硬上限，超过就丢老保新。
#
# 取 750ms 的依据（经验值，非实测标定）：
#   * 下界必须 > 突发抖动。REORDER_S 是 100ms，K230 发送端整帧丢弃（实测
#     37%）会在序号空间外留洞但不进 pending，所以正常抖动不推高积压；750ms
#     足够吸收一个 GOP 级的解码打嗝而完全不触发。
#   * 上界由"还能不能追回来"决定：30fps 下 750ms ≈ 22 帧，而每轮循环允许
#     连解 DECODE_BURST_MAX(=4) 个 AU，追平 22 帧只要 ~6 轮循环 ≈ 十几毫秒
#     墙钟。即"落在上限内的积压是可恢复的，落在线上就一直可恢复"；再往上
#     加阈值只是把同一个死螺旋推迟几秒发生。
BACKLOG_MAX_MS = 750
BACKLOG_KEEP_MS = 150          # 丢老后保留的尾部窗口（必须 >= REORDER_S）
BACKLOG_MAX_BYTES = 2 << 20    # 兜底：无论时长多少，pending 内存不超过 2MB
# 包数下限。只防"刚开流、每 AU 包数还没估准"这一种假触发：估算是滑动均值，
# 开流头几帧会偏低（首帧是 IDR，随后小 P 帧可能只有 1~2 包），于是几十个包
# 会被算成好几秒。真正的判据是 BACKLOG_MAX_MS，这个下限只是护栏。
BACKLOG_MIN_PKTS = 64
DECODE_BURST_MAX = 4           # 每轮循环最多解几个 AU（原来恒为 1）
SEQ_RESET_DISTANCE = 0x8000    # playhead 落后超过半个序号空间 = 序号已重置


def _seq_after(seq: int, ref: int) -> bool:
    """RTP seq 是 16 位循环的，判断 seq 是否在 ref 之后（含半个序号空间）。"""
    return 0 < ((seq - ref) & 0xFFFF) < 0x8000


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
        self._pkts_rx = 0
        self._lost_pkts = 0
        self._dup = 0
        self._loss_events = 0
        self._gaps = 0
        self._reorder_fixed = 0
        self._drops = 0            # drop-old 丢弃的包数
        self._drop_events = 0      # drop-old 触发次数
        self._seq_resets = 0       # 序号重置次数
        self._late_pkts = 0        # 复位后到达的过期包
        self._pending_bytes = 0
        self._backlog_packets = 0
        self._backlog_bytes = 0
        self._pkts_per_au = 1.0

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
        last_idr = [0.0]        # 单元素 list：供内嵌函数 nonlocal 改写
        last_rtp_at = 0.0
        started_at = time.monotonic()
        last_warn = 0.0
        last_stat = time.monotonic()
        # 丢包/解码异常后：丢弃后续 P 帧直到 IDR 到达，杜绝绿斑/马赛克
        wait_idr = True

        # 重排缓冲：WiFi 上 UDP 乱序是常态，不是丢包。原来一见到 seq 跳变
        # 就 wait_idr，会把每个乱序包都变成一次"等到下个 IDR"的黑屏 ——
        # 1~2s 的画面卡死比丢几帧糟糕得多。这里按 seq 排序并等一个短窗口，
        # 只有窗口内仍缺的 seq 才判定为真丢包。
        pending: dict[int, bytes] = {}
        playhead: int | None = None      # 下一个期望消费的 seq
        max_seq: int | None = None       # 已收到的最大 seq —— 积压度量的锚点
        playhead_at = 0.0                # playhead 缺失时的计时起点
        pending_bytes = 0                # 增量维护，避免 sum() 的 O(n) 扫描
        # 每 AU 包数（滑动均值）：把 backlog 包数换算成毫秒的依据。
        # EMA(0.2) 约 5 个 AU 收敛。跳 IDR / 序号重置都不重置它 —— 流的形状
        # 没变，重置只会让估算退回种子值，在开流瞬间虚报几秒积压。
        pkts_per_au = 1.0
        pkts_since_au = 0

        def backlog_packets() -> int:
            """O(1) 积压包数 = max_seq 与 playhead 的序号距离。

            必须夹住 0：队列排空后 playhead 会停在 max_seq+1，此时
            (max_seq - playhead) & 0xFFFF = 65535，不夹的话会把"刚好追平"
            读成"积压 9 分钟"，进而疯狂触发 drop-old。距离超过半个序号空间
            同样按"排空"处理 —— 那不是积压，是序号已经对不上了。
            """
            if playhead is None or max_seq is None:
                return 0
            d = (max_seq - playhead) & 0xFFFF
            return 0 if d > 0x8000 else d

        def backlog_ms() -> float:
            """把积压包数换算成毫秒：包数 / 每 AU 包数 * 帧间隔。

            这是"追上实时还要多久"的下界估计（因为 pkts_per_au 取上界）。
            """
            n = backlog_packets()
            if n <= 0 or pkts_per_au <= 0:
                return 0.0
            return n / pkts_per_au * (1000.0 / self.fps)

        def clear_pending() -> int:
            """清空重排缓冲。返回丢弃的包数。

            wait_idr 期间残留的 pending 全是残缺 GOP 的片段：既解不出画面，
            又会持续推高积压度量（进而误触发 drop-old），还会让 O(1) 的
            max_seq/playhead 距离虚高。清掉是纯收益。
            """
            nonlocal pending_bytes
            n = len(pending)
            pending.clear()
            pending_bytes = 0
            return n

        def trim_backlog() -> int:
            """积压超限 -> 丢老保新：只留最新一段连续序号，playhead 直接跳过去。

            跳过的区间一定包含参考帧缺失的 P 帧，硬解就是花屏，所以跳完必须
            走 wait_idr + request_idr，让发送端立刻补一个 IDR。
            """
            nonlocal playhead, playhead_at, wait_idr, pkts_since_au
            nonlocal pending_bytes, last_warn
            if not pending or playhead is None or max_seq is None:
                return 0
            was_pkts = backlog_packets()
            was_ms = int(backlog_ms())
            was_bytes = pending_bytes
            keep = max(1, int(self.fps * BACKLOG_KEEP_MS / 1000.0) * pkts_per_au)
            # 从 max_seq 往回找连续段，最多 keep 个包。走不到 keep 就停了，
            # 所以这里是 O(keep)，与积压总量无关。
            start = max_seq
            walked = 0
            while walked < keep:
                prev = (start - 1) & 0xFFFF
                if prev not in pending:
                    break
                start = prev
                walked += 1
            span = (max_seq - start) & 0xFFFF
            # 积压已经全落在保留窗口内，trim 没有意义（不该浪费一次跳 IDR）
            if start == playhead:
                return 0
            dropped = 0
            # 只在真正要丢的时候扫一遍 pending。drop-old 之后积压被砍到
            # BACKLOG_KEEP_MS，所以这段扫描的摊销成本有界。
            for s in [s for s in pending if ((s - start) & 0xFFFF) > span]:
                pending_bytes -= len(pending.pop(s))
                dropped += 1
            self._drops += dropped
            self._drop_events += 1
            self._pending_bytes = pending_bytes
            playhead = start
            playhead_at = 0.0
            pkts_since_au = 0
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

        def request_idr() -> None:
            """限频请求 IDR：300ms 一次，避免丢包风暴时刷爆反向通道。"""
            t = time.monotonic()
            if t - last_idr[0] >= IDR_RETRY_S:
                last_idr[0] = t
                send_ctrl(CTRL_IDR)

        def declare_loss(nmiss: int) -> None:
            """playhead 窗口内确实丢了包：残缺 AU 一律不解，等下个 IDR。"""
            nonlocal wait_idr
            wait_idr = True
            depack.reset()
            self._lost_pkts += nmiss
            self._loss_events += 1
            request_idr()

        while not self._stop.is_set():
            now = time.monotonic()
            # PING 让 server 学习桌面地址；relay 不在（无 PONG/无 RTP）
            # 持续 NO_RTP_FALLBACK_S 则建议降级到 JPEG 链路
            if now - last_ping >= PING_INTERVAL_S:
                last_ping = now
                try:
                    sock.sendto(CONTROL_MAGIC + bytes([CTRL_IDR if idr_until_frame
                                                      else CTRL_PING]), target)
                except OSError:
                    pass
                if idr_until_frame:
                    send_ctrl(CTRL_IDR)
            if (not last_rtp_at and now - started_at > NO_RTP_FALLBACK_S) or (
                    last_rtp_at and now - last_rtp_at > NO_RTP_FALLBACK_S):
                # 长时间没有 RTP。K230 是独立上电的，开机/重启期间本来就没
                # 流，所以这不等于"H264 不可用"—— 只报无信号并继续等。
                # 绝不能在这里报 "fallback"：旧实现一报 fallback，StreamPanel
                # 就把 H264 viewer 换成 JPEG viewer 且永不切回，而跑
                # rtp_push.py 时 JPEG 链路根本没有流，于是永久"没信号"。
                self._set_streaming(False)
                self._stop.wait(0.5)
                continue

            # 1) 收包入重排缓冲（不设超时，短窗口内尽量补齐）
            sock.settimeout(0.0)
            for _ in range(512):          # 一轮最多收 512 包，防止饿死解码
                try:
                    data, _addr = sock.recvfrom(2048)
                except (BlockingIOError, socket.timeout):
                    break
                except OSError:
                    break
                if data[:3] == CONTROL_MAGIC:
                    continue              # PONG 等，忽略
                last_rtp_at = time.monotonic()
                seq = struct.unpack_from(">H", data, 2)[0]
                # 序号重置检测：playhead 落后超过半个序号空间不可能是真积压
                # （那需要 65536 包 ≈ 9 分钟的流），只能是发送端重启 / seq
                # 跳变。丢掉残留 pending，从新流的第一个包重新锚定。
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
                    pkts_since_au = 0
                newer = max_seq is None or _seq_after(seq, max_seq)
                if newer:
                    max_seq = seq
                if playhead is None:
                    # 复位/丢包后重新锚定，只认"不比已知最新包旧"的包。
                    # 无条件 `playhead = seq` 会让一个迟到的乱序包把 playhead
                    # 拽回几百个序号之前，backlog_packets() 直接读出 65535，
                    # 于是 drop-old 被误触发成死循环（真机上就是"永远等 IDR，
                    # 永远出不了画面"）。这种过期包已经没有价值，直接扔。
                    if not newer:
                        self._late_pkts += 1
                        continue
                    playhead = seq
                if seq in pending:
                    self._dup += 1
                    continue
                pending[seq] = data
                pending_bytes += len(data)
                pkts_since_au += 1
                self._pkts_rx += 1

            now = time.monotonic()
            # 2) 按 seq 顺序消费；playhead 缺失且超过重排窗口 -> 判丢包。
            #    前提是 pending 非空：帧与帧之间 pending 天然为空，那不是丢包。
            if playhead is not None and playhead not in pending:
                if not pending:
                    playhead_at = 0.0      # 空闲，不计时
                elif not playhead_at:
                    playhead_at = now
                    self._gaps += 1        # 发现乱序空洞，开始计时
                elif now - playhead_at >= REORDER_S:
                    # 从最小的可用 seq 继续。**这里保留 pending 的 O(n) 扫描是
                    # 有意的**：它只在确认真丢包后触发（每秒几次），不在每轮
                    # 正常路径上；而积压度量已经换成 O(1) 的 backlog_packets()。
                    # 曾经试过改成"清空 pending + playhead 复位"，实测会把
                    # 重排缓冲里"恰好完整到达的 IDR"一起扔掉，25% 丢包下
                    # 出帧从 20 掉到 5（GOP=30，一个 6 包 IDR 完整存活率只有
                    # 0.75^6≈18%），得不偿失。
                    ahead = [s for s in pending if _seq_after(s, playhead)]
                    nmiss = ((min(ahead) - playhead) & 0xFFFF) if ahead else 1
                    if nmiss < 1:
                        nmiss = 1
                    declare_loss(nmiss)
                    playhead = min(ahead) if ahead else None
                    playhead_at = now
            elif playhead is not None:
                # playhead 还在：刚才那个空洞被补上了 = 乱序被成功修复
                if playhead_at:
                    self._reorder_fixed += 1
                playhead_at = 0.0

            # 2b) drop-old：积压换算成时间后超上限，就丢老保新。这是把
            #     "延迟 4~10s 且单调增长"变成"延迟有上界"的那一步。放在收包
            #     之后、消费之前，保证判断用的是最新的 max_seq。
            #     backlog > 半个序号空间不可能是真积压（那要 9 分钟的流），
            #     直接按序号重置处理，别拿它去当 drop-old 的触发条件。
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
                pkts_since_au = 0
            elif (backlog_packets() > BACKLOG_MIN_PKTS
                    and backlog_ms() > BACKLOG_MAX_MS) \
                    or pending_bytes > BACKLOG_MAX_BYTES:
                trim_backlog()
                self._backlog_packets = backlog_packets()
                self._backlog_bytes = pending_bytes

            got_frame = False
            burst = 0
            while playhead is not None and playhead in pending:
                data = pending.pop(playhead)
                pending_bytes -= len(data)
                au = depack.push(data)
                playhead = (playhead + 1) & 0xFFFF
                if au is None:
                    continue
                # 每 AU 包数（衰减最大值）：把 backlog 包数换算成毫秒的依据。
                # 跳 IDR / 序号重置都不重置它 —— 流的形状没变，重置只会让
                # 估算退回种子值 1.0，在开流瞬间虚报几秒积压。
                pkts_per_au += (pkts_since_au - pkts_per_au) * 0.2
                pkts_per_au = max(1.0, pkts_per_au)
                pkts_since_au = 0
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
                    t = time.monotonic()
                    if t - last_warn >= 2.0:
                        last_warn = t
                        logger.warning(
                            "decode failed #%d: %s | au_head=%s",
                            self._decode_fails, e, au[:16].hex())
                    request_idr()
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
                if got_frame:
                    burst += 1
                    # 每轮最多连解 DECODE_BURST_MAX 个 AU（原来恒为 1）。
                    # 上限让"轻度积压"能在几轮内追平；真正的解不动由 drop-old
                    # 兜底，两者不冲突：trim 先把积压砍到 BACKLOG_KEEP_MS，
                    # 剩下的量 burst 一定吃得下。
                    if burst >= DECODE_BURST_MAX:
                        break

            self._backlog_packets = backlog_packets()
            self._backlog_bytes = pending_bytes

            if not got_frame:
                self._stop.wait(0.002)

            # 周期性把链路健康度打进 server 日志。图传调试全靠这几个数：
            # pkts_rx 有涨但 frames 不涨 = 解码/等待 IDR；pkts_rx 也不涨 =
            # 包根本没到桌面（relay/地址/防火墙问题）。
            # 用 WARNING：仓库里没有 basicConfig，root logger 默认级别是
            # WARNING，INFO 会被静默丢弃。
            if now - last_stat >= 10.0:
                last_stat = now
                st = self.stats()
                logger.warning(
                    "h264 rtp: rx=%d lost=%d(%.2f%%) gaps=%d fixed=%d "
                    "frames=%d dropped=%d fails=%d dup=%d | backlog=%d pkt/"
                    "%dB drops=%d(%d 次) resets=%d",
                    st["pkts_rx"], st["pkts_lost"], st["loss_pct"],
                    st["gaps_seen"], st["reorder_fixed"], st["frames"],
                    st["dropped_wait_idr"], st["decode_fails"],
                    st["pkts_dup"], st["backlog_packets"], st["backlog_bytes"],
                    st["drops"], st["drop_events"], st["seq_resets"])

    # ---------- 统计 ----------
    def stats(self) -> dict:
        total = self._pkts_rx + self._lost_pkts
        return {"mode": "h264", "frames": self._frames,
                "streaming": self._streaming,
                "decode_fails": self._decode_fails,
                "dropped_wait_idr": self._dropped,
                "pkts_rx": self._pkts_rx,
                "pkts_lost": self._lost_pkts,
                "pkts_dup": self._dup,
                "loss_pct": round(100.0 * self._lost_pkts / total, 3)
                if total else 0.0,
                "loss_events": self._loss_events,
                "gaps_seen": self._gaps,
                "reorder_fixed": self._reorder_fixed,
                "backlog_packets": self._backlog_packets,
                "backlog_bytes": self._backlog_bytes,
                "drops": self._drops,
                "drop_events": self._drop_events,
                "seq_resets": self._seq_resets}
