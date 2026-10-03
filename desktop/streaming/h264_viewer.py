"""Receive H.264 RTP, decode with PyAV, and emit the newest PIL frames."""
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
except ImportError:
    av = None

logger = logging.getLogger(__name__)

DEFAULT_RTP_PORT = 8002
CONTROL_MAGIC = b"CBR"
CTRL_PING = 0x00
CTRL_IDR = 0x01
CTRL_PONG = 0x10
PT_H264 = 96

Event = tuple[str, object]

IDR_RETRY_S = 0.2
NO_RTP_FALLBACK_S = 5.0
PING_INTERVAL_S = 1.0
REORDER_S = 0.060

BACKLOG_MAX_MS = 500
BACKLOG_KEEP_MS = 150
BACKLOG_MAX_BYTES = 2 << 20
BACKLOG_MIN_PKTS = 64
DECODE_BURST_MAX = 4
SEQ_RESET_DISTANCE = 0x8000
AU_MAX_PACKETS = 64
AU_MAX_BYTES = 512 << 10


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
    def __init__(self, server_url: str, port: int = DEFAULT_RTP_PORT,
                 fps: int = 30) -> None:
        url = (server_url or "").strip()
        if url and "://" not in url:
            url = "http://" + url
        host = urlparse(url).hostname or "127.0.0.1"
        self.server_host = host
        self.port = port
        self.fps = max(1, min(fps, 30))
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
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

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
            dropped = 0
            for s in [s for s in pending if ((s - start) & 0xFFFF) > span]:
                pending_bytes -= len(pending.pop(s))
                pending_at.pop(s, None)
                dropped += 1
            self._drops += dropped
            self._drop_events += 1
            playhead = start
            playhead_at = 0.0
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
            """Request an IDR at most once per retry interval."""
            t = time.monotonic()
            if t - last_idr[0] >= IDR_RETRY_S:
                last_idr[0] = t
                send_ctrl(CTRL_IDR)

        def declare_loss(nmiss: int) -> None:
            """Discard the incomplete access unit and wait for an IDR."""
            nonlocal wait_idr
            wait_idr = True
            depack.reset()
            self._lost_pkts += nmiss
            self._loss_events += 1
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
            if (not last_rtp_at and now - started_at > NO_RTP_FALLBACK_S) or (
                    last_rtp_at and now - last_rtp_at > NO_RTP_FALLBACK_S):
                self._set_streaming(False)
                # 信号中断：清掉旧包袱，避免恢复后 backlog 虚高误杀
                clear_pending()
                depack.reset()
                playhead = None
                max_seq = None
                self._emit(("status", "fallback"))
                self._stop.wait(0.5)
                last_rtp_at = 0.0
                started_at = time.monotonic()
                continue

            loop_t0 = time.perf_counter()
            sock.settimeout(0.0)
            got_bytes = 0
            for _ in range(512):
                try:
                    data, _addr = sock.recvfrom(2048)
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
            if playhead is not None and playhead not in pending:
                if not pending:
                    playhead_at = 0.0
                elif not playhead_at:
                    playhead_at = now
                    self._gaps += 1
                elif now - playhead_at >= REORDER_S:
                    ahead = [s for s in pending if _seq_after(s, playhead)]
                    nmiss = ((min(ahead) - playhead) & 0xFFFF) if ahead else 1
                    if nmiss < 1:
                        nmiss = 1
                    declare_loss(nmiss)
                    playhead = min(ahead) if ahead else None
                    playhead_at = now
            elif playhead is not None:
                if playhead_at:
                    self._reorder_fixed += 1
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
                    or pending_bytes > BACKLOG_MAX_BYTES:
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

            if not got_frame:
                self._stop.wait(0.002)

            if now - last_stat >= 10.0:
                last_stat = now
                st = self.stats()
                logger.warning(
                    "h264 rtp: rx=%d lost=%d(%.2f%%) gaps=%d fixed=%d "
                    "frames=%d dropped=%d fails=%d dup=%d | backlog=%d pkt/"
                    "%dB drops=%d(%d 次) resets=%d | render_age=%.0fms "
                    "queue=%.0fms %dkbps loop=%.0f/%.0fus",
                    st["pkts_rx"], st["pkts_lost"], st["loss_pct"],
                    st["gaps_seen"], st["reorder_fixed"], st["frames"],
                    st["dropped_wait_idr"], st["decode_fails"],
                    st["pkts_dup"], st["backlog_packets"], st["backlog_bytes"],
                    st["drops"], st["drop_events"], st["seq_resets"],
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
                "render_age_ms": round(render_age, 1),
                "queue_ms": round(queue, 1),
                "bitrate_kbps": round(self._bitrate_kbps, 1),
                "loop_us_avg": round(self._loop_us_avg, 1),
                "loop_us_max": round(self._loop_us_max, 1)}
