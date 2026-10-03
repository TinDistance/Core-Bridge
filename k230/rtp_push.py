import gc
import os
import socket
import struct
import time
import uctypes

import network
from media.sensor import *
from media.media import *
from media.vencoder import *

WIFI_SSID = "TF-Laptop"
WIFI_PASSWORD = "TF@HTR.Hello"
# 注意：SSID/密码提交仅为校内赛默认；优先使用 wifi.cfg / 环境变量覆盖，避免改代码。
try:
    _cfg_ssid = os.environ.get("WIFI_SSID") if hasattr(os, "environ") else None
    _cfg_pass = os.environ.get("WIFI_PASSWORD") if hasattr(os, "environ") else None
    if _cfg_ssid:
        WIFI_SSID = _cfg_ssid
    if _cfg_pass:
        WIFI_PASSWORD = _cfg_pass
except Exception:
    pass
SERVER_IP = None
try:
    _cfg_ip = os.environ.get("SERVER_IP") if hasattr(os, "environ") else None
    if _cfg_ip:
        SERVER_IP = _cfg_ip
except Exception:
    pass
RTP_PORT = 8002
try:
    _cfg_port = os.environ.get("RTP_PORT") if hasattr(os, "environ") else None
    if _cfg_port:
        RTP_PORT = int(_cfg_port)
except Exception:
    pass
SENSOR_ID = 2
WIDTH = 1280
HEIGHT = 720

# === 15Mbps 多链冗余 RTP（同帧同包同 seq 同 SSRC，多目的端口互为备份）===
# 三链共用一条 WiFi 信道，所有拷贝共享同一个 pacer 令牌桶；
# 接收端按 seq 去重、先到先用，单链抖动/丢包不再影响画面。
WIFI_LINK_KBPS = 15000
try:
    _cfg_link = os.environ.get("WIFI_LINK_KBPS") if hasattr(os, "environ") else None
    if _cfg_link:
        WIFI_LINK_KBPS = int(_cfg_link)
except Exception:
    pass
LINK_COPIES = 3      # P 帧冗余拷贝数（实际拷贝数按 airtime 预算自适应下调）
IDR_COPIES = 2       # I 帧本体大，低拷贝 + 接收端 IDR 重试防节流长尾
try:
    _cfg_copies = os.environ.get("LINK_COPIES") if hasattr(os, "environ") else None
    if _cfg_copies:
        LINK_COPIES = max(1, int(_cfg_copies))
    _cfg_idr = os.environ.get("IDR_COPIES") if hasattr(os, "environ") else None
    if _cfg_idr:
        IDR_COPIES = max(1, int(_cfg_idr))
except Exception:
    pass
RTP_PORTS = (RTP_PORT, RTP_PORT + 1, RTP_PORT + 2)

GOP_LEN = 15
FPS = 30
MAX_PAYLOAD = 1200
CMD_STALE_MS = 200
UART3_BAUD = 115200
GC_COLLECT_EVERY = 100

# 码率预算：3 链共享 PACER_WIRE_KBPS 的节流池。
# P 帧准入要求 copies×single_airtime <= FRAME_INTERVAL_MS×MAX_PFRAME_DUTY，
# 反推 BIT_RATE ≈ PACER_WIRE_KBPS / LINK_COPIES × duty × 帧率分摊。
BIT_RATE = 4200
FRAME_INTERVAL_MS = 1000 // FPS
FRAME_PERIOD_MS = FRAME_INTERVAL_MS
MAX_PFRAME_DUTY = 0.95

PACER_DUTY = 0.92
PACER_WIRE_KBPS = int(WIFI_LINK_KBPS * PACER_DUTY)
_PACER_BYTES_PER_S = PACER_WIRE_KBPS * 1000 / 8.0
PACER_BURST_BYTES = int(_PACER_BYTES_PER_S * 2 * FRAME_INTERVAL_MS / 1000.0)

PACER_SPAN_MAX_MS = 300

IDR_REQ_MIN_INTERVAL_MS = 400
IDR_REQ_SETTLE_MS = 500

OUT_BUFS = max(8, -(-PACER_SPAN_MAX_MS // FRAME_INTERVAL_MS) + 3)


sensor = None
encoder = None
link = None
_sta = None
_uart = None


def _ticks_ms():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _ticks_diff(a, b):
    return time.ticks_diff(a, b) if hasattr(time, "ticks_diff") else a - b


def init_camera():
    global sensor
    sensor = Sensor(id=SENSOR_ID)
    sensor.reset()
    width = ALIGN_UP(WIDTH, 16)
    sensor.set_framesize(width=width, height=HEIGHT, alignment=12, chn=CAM_CHN_ID_0)
    sensor.set_pixformat(Sensor.YUV420SP, chn=CAM_CHN_ID_0)
    print("[1/4] camera init done (%dx%d)" % (width, HEIGHT))


def get_sta():
    global _sta
    if _sta is None:
        _sta = network.WLAN(network.STA_IF)
    if not _sta.active():
        _sta.active(True)
    return _sta


def connect_wifi(timeout_s=15):
    sta = get_sta()
    if sta.isconnected():
        ip, _nm, gw, _dns = sta.ifconfig()
        print("WiFi already up. IP: %s GW: %s" % (ip, gw))
        return ip, gw
    print("[2/4] connecting to " + WIFI_SSID)
    sta.connect(WIFI_SSID, WIFI_PASSWORD)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if sta.isconnected():
            break
        time.sleep(1)
    if not sta.isconnected():
        raise RuntimeError("WiFi connect failed")
    ip, _nm, gw, _dns = sta.ifconfig()
    print("Connected! IP: %s Gateway: %s" % (ip, gw))
    return ip, gw


def ensure_wifi(sta):
    if sta.isconnected():
        return True
    print("WiFi lost, reconnecting...")
    try:
        sta.connect(WIFI_SSID, WIFI_PASSWORD)
    except Exception as e:
        print("reconnect err: " + str(e))
        return False
    for _ in range(10):
        if sta.isconnected():
            print("WiFi back")
            return True
        time.sleep(1)
    return sta.isconnected()


def pick_profile(enc):
    for name in ("H264_PROFILE_BASELINE", "H264_PROFILE_MAIN"):
        val = getattr(enc, name, None)
        if val is not None:
            return val, name
    raise RuntimeError("no H264 profile constant")


def init_encoder():
    global encoder, link
    width = ALIGN_UP(WIDTH, 16)
    encoder = Encoder()
    encoder.SetOutBufs(OUT_BUFS, width, HEIGHT)
    profile, profile_name = pick_profile(encoder)
    chnAttr = ChnAttrStr(
        encoder.PAYLOAD_TYPE_H264,
        profile,
        width,
        HEIGHT,
        bit_rate=BIT_RATE,
        gopLen=GOP_LEN,
        src_frame_rate=FPS,
        dst_frame_rate=FPS,
    )
    encoder.Create(chnAttr)
    link = MediaManager.link(
        sensor.bind_info()["src"],
        (VIDEO_ENCODE_MOD_ID, VENC_DEV_ID, encoder.chn),
    )
    encoder.Start()
    sensor.run()
    print("[3/4] encoder started (chn=%d, %dx%d, %s, %dKbps)" % (
        encoder.chn, width, HEIGHT, profile_name, BIT_RATE))


def split_nalus(data):
    """Annex-B（00 00 00 01 / 00 00 01）-> NALU payload 列表（memoryview）。"""
    mv = memoryview(data)
    n = len(data)
    payload = data.find(b"\x00\x00\x01")
    marks = []
    while payload >= 0 and payload + 3 <= n:
        sc_start = payload - 1 if payload > 0 and data[payload - 1] == 0 else payload
        marks.append((sc_start, payload + 3))
        payload = data.find(b"\x00\x00\x01", payload + 3)
    out = []
    for j in range(len(marks)):
        start = marks[j][1]
        end = marks[j + 1][0] if j + 1 < len(marks) else n
        if end > start:
            out.append(mv[start:end])
    return out


def rtp_header(seq, ts, marker, ssrc):
    return struct.pack(
        ">BBHII", 0x80, 0x60 | (0x80 if marker else 0), seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)


def packetize_nalu(nalu, ts, seq, ssrc, out_packets):
    """单 NAL / FU-A 打包，返回更新后的 seq。"""
    ln = len(nalu)
    if ln <= MAX_PAYLOAD:
        out_packets.append(rtp_header(seq, ts, False, ssrc) + bytes(nalu))
        return (seq + 1) & 0xFFFF
    indicator = (nalu[0] & 0xE0) | 28
    nal_type = nalu[0] & 0x1F
    off = 1
    first = True
    while off < ln:
        chunk = nalu[off:off + MAX_PAYLOAD]
        off += MAX_PAYLOAD
        last = off >= ln
        fu = nal_type
        if first:
            fu |= 0x80
            first = False
        if last:
            fu |= 0x40
        out_packets.append(
            rtp_header(seq, ts, False, ssrc) + bytes([indicator, fu]) + bytes(chunk))
        seq = (seq + 1) & 0xFFFF
    return seq


PacerWireOverhead = 12 + 8 + 20
TICKS_MS_PERIOD = 1 << 30


def ms_diff(now, then):
    """time.ticks_ms 差值，回绕安全（then 可以是 None）。"""
    if then is None:
        return None
    d = now - then
    if d < 0:
        d += TICKS_MS_PERIOD
    return d


def airtime_ms(frame_bytes, npkts, link_kbps):
    """估一帧占满链路 airtime 的毫秒数（payload + 每包 40B 头开销）。"""
    wire = frame_bytes + npkts * PacerWireOverhead
    return wire * 8.0 / link_kbps


class SysClock:
    """生产环境时钟：rt-smart MicroPython 的 ticks_us/sleep_us。"""

    def now_us(self):
        if hasattr(time, "ticks_us"):
            return time.ticks_us()
        return int(time.time() * 1000000)

    def diff_us(self, a, b):
        if hasattr(time, "ticks_diff"):
            return time.ticks_diff(a, b)
        return a - b

    def sleep_us(self, us):
        if us <= 0:
            return
        if hasattr(time, "sleep_us"):
            time.sleep_us(int(us))
        else:
            time.sleep(us / 1000000.0)


class Pacer:
    """字节级 airtime 令牌桶。**每一个**发出的包（含关键帧）都必须 take()。"""

    def __init__(self, rate_kbps, burst_bytes, clock, span_max_ms):
        self.clock = clock
        self.rate_bps = rate_kbps * 1000.0
        self.burst = float(burst_bytes)
        self.tokens = float(burst_bytes)
        self.last = clock.now_us()
        self.span_max_us = int(span_max_ms * 1000)
        self.span_left = self.span_max_us
        self.packets = 0
        self.wire_bytes = 0
        self.wait_us = 0
        self.sleeps = 0
        self.max_wait_us = 0
        self.cap_events = 0
        self.min_gap_us = None

    def wire_bytes_for(self, nbytes):
        return nbytes + PacerWireOverhead

    def refill(self):
        now = self.clock.now_us()
        d_us = self.clock.diff_us(now, self.last)
        self.last = now
        if d_us > 0:
            self.tokens += d_us * (self.rate_bps / 8.0) / 1000000.0
            if self.tokens > self.burst:
                self.tokens = self.burst
        return self.tokens

    def begin_frame(self):
        """每帧调一次：重置单帧节流时长预算。"""
        self.span_left = self.span_max_us

    def can_send(self, frame_bytes, npkts):
        """非阻塞预检：整帧的线速字节是否拿得到。用于 P 帧的丢弃决策。"""
        return self.refill() >= frame_bytes + npkts * PacerWireOverhead

    def take(self, nbytes):
        """为 nbytes payload 阻塞取用线速时间，返回本次等待 us。"""
        need = self.wire_bytes_for(nbytes)
        t0 = self.clock.now_us()
        self.packets += 1
        self.wire_bytes += need
        gap = self.clock.diff_us(t0, self.last)
        if gap > 0 and (self.min_gap_us is None or gap < self.min_gap_us):
            self.min_gap_us = gap
        waited = 0
        while True:
            self.refill()
            if self.tokens >= need:
                break
            deficit = need - self.tokens
            wait_us = int(deficit / (self.rate_bps / 8.0) * 1000000.0) + 50
            if self.span_left <= 0:
                self.tokens = max(self.tokens - need, -self.burst)
                self.cap_events += 1
                return waited
            if wait_us > self.span_left:
                wait_us = self.span_left
            self.clock.sleep_us(wait_us)
            self.sleeps += 1
            waited += wait_us
            self.span_left -= wait_us
        self.tokens -= need
        self.wait_us += waited
        if waited > self.max_wait_us:
            self.max_wait_us = waited
        return waited


class IdrGate:
    """IDR 请求合并/限频。"""

    def __init__(self, interval_ms, settle_ms):
        self.interval_ms = interval_ms
        self.settle_ms = settle_ms
        self._last_idr_sent = None
        self._last_grant = None
        self.rx = 0
        self.granted = 0
        self.merged_idr = 0
        self.merged_rate = 0

    def note_idr_sent(self, now_ms):
        self._last_idr_sent = now_ms

    def reset_rolling(self):
        """只清本窗口计数；合并状态（_last_idr_sent/_last_grant）必须保留。"""
        self.rx = 0
        self.granted = 0
        self.merged_idr = 0
        self.merged_rate = 0

    def request(self, now_ms):
        """返回 True = 这次请求应该真的调 encoder.RequestIDR()。"""
        self.rx += 1
        d_idr = ms_diff(now_ms, self._last_idr_sent)
        if d_idr is not None and d_idr < self.settle_ms:
            self.merged_idr += 1
            return False
        d_grant = ms_diff(now_ms, self._last_grant)
        if d_grant is not None and d_grant < self.interval_ms:
            self.merged_rate += 1
            return False
        self._last_grant = now_ms
        self.granted += 1
        return True


class SendAborted(Exception):
    """send_paced 的 sink 抛这个来中止本帧（发送队列溢出时用）。"""


FRAME_SEND = 0
FRAME_DROP_DUTY = 1
FRAME_DROP_STARVE = 2


def frame_admission(frame_bytes, npkts, is_idr, link_kbps,
                    frame_interval_ms, max_pframe_duty, pacer,
                    max_copies=1, idr_copies=1):
    """整帧准入判定 + 冗余拷贝数自适应（纯函数）。

    返回 (action, copies, air_ms)。I 帧低拷直发；P 帧按 duty 预算选最大
    拷贝数（a×n <= interval×duty），pacer 令牌不够继续降拷，拷到 1 都不够
    才判 STARVE 丢弃。
    """
    single = airtime_ms(frame_bytes, npkts, link_kbps)
    budget = frame_interval_ms * max_pframe_duty
    if is_idr:
        return (FRAME_SEND, max(1, min(max_copies, idr_copies)), single)
    best = 0
    for n in range(max_copies, 0, -1):
        if single * n > budget:
            continue
        if pacer is None or pacer.can_send(frame_bytes * n, npkts * n):
            return FRAME_SEND, n, single * n
        best = n
    if best:
        return FRAME_DROP_STARVE, 0, single * best
    return FRAME_DROP_DUTY, 0, single


def fanout_sink(targets, sock, stats):
    """多端口扇出 sink：单包发往所有目标端口；任一 OSError 即中止本帧。"""
    n = len(targets)

    def _send(pkt):
        try:
            for t in targets:
                sock.sendto(pkt, t)
        except TypeError:
            pkt = bytes(pkt)
            for t in targets:
                sock.sendto(pkt, t)
        except OSError as e:
            stats.pkt_err += 1
            raise SendAborted(str(e))
        stats.bytes_tx += len(pkt) * n
    return _send


def send_paced(pacer, packets, sink, copies=1):
    """逐包节流发送，返回实际发出的包数。

    令牌按 copies×包长计费：三链拷贝共用同一个节流池，线速预算不超发。
    """
    sent = 0
    pacer.begin_frame()
    for pkt in packets:
        pacer.take(len(pkt) * copies)
        sink(pkt)
        sent += 1
    return sent


class TxStats:
    """发送侧诊断统计（只读，不影响任何行为）。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.reset_rolling()
        self.last_len = 0
        self.last_iframe_pkts = 0
        self.last_iframe_ms = 0.0
        self.last_pframe_ms = 0.0

    def note_frame(self, frame_bytes, npkts, is_idr, air_ms):
        self.sent_frames += 1
        self.last_len = frame_bytes
        if frame_bytes > self.max_frame_bytes:
            self.max_frame_bytes = frame_bytes
        if is_idr:
            self.idr_frames += 1
            self.idr_bytes += frame_bytes
            self.last_iframe_pkts = npkts
            self.last_iframe_ms = air_ms
        else:
            self.last_pframe_ms = air_ms

    def idr_share(self):
        """I 帧字节占本窗口已发字节的比例（诊断 I 帧开销有多重）。"""
        return 100.0 * self.idr_bytes / self.bytes_tx if self.bytes_tx else 0.0

    def reset_rolling(self):
        """只清本窗口的累计量；last_* 单帧观测值保留。"""
        self.sent_frames = 0
        self.dropped = 0
        self.over_duty = 0
        self.starved = 0
        self.overfps = 0
        self.idr_frames = 0
        self.idr_bytes = 0
        self.bytes_tx = 0
        self.pkt_err = 0
        self.forced_idr = 0
        self.resyncs = 0
        self.max_frame_bytes = 0
        self.fanout_copies = 0

    def line(self, span, pacer, gate, free):
        kbps = self.bytes_tx * 8 / span / 1000.0
        fps = self.sent_frames / span
        return (
            "stat: sent=%d drop=%d(duty=%d starve=%d) overfps=%d idr=%d "
            "(forced=%d resync=%d) last=%dB max=%dB kbps=%.0f fps=%.1f pps=%.0f "
            "txerr=%d idrshare=%.0f%% free=%d" % (
                self.sent_frames, self.dropped, self.over_duty, self.starved,
                self.overfps, self.idr_frames, self.forced_idr, self.resyncs,
                self.last_len, self.max_frame_bytes, kbps, fps,
                pacer.packets / span, self.pkt_err, self.idr_share(), free))

    def detail(self, pacer, gate):
        return (
            "      pacer: wait=%.0fms sleep=%d cap=%d maxwait=%.1fms "
            "minkgap=%s | I帧 %d包 %.0fms | IDR请求 rx=%d 生效=%d "
            "(合并:近期IDR=%d 限频=%d)" % (
                pacer.wait_us / 1000.0, pacer.sleeps, pacer.cap_events,
                pacer.max_wait_us / 1000.0,
                "n/a" if pacer.min_gap_us is None else "%.2fms" % (
                    pacer.min_gap_us / 1000.0),
                self.last_iframe_pkts, self.last_iframe_ms,
                gate.rx, gate.granted, gate.merged_idr, gate.merged_rate))


SYS_CLOCK = None


def get_clock():
    global SYS_CLOCK
    if SYS_CLOCK is None:
        SYS_CLOCK = SysClock()
    return SYS_CLOCK


def crc8(payload):
    c = 0
    for b in payload:
        c ^= b
    return c & 0xFF


def valid_frame(data):
    if data is None or len(data) < 4:
        return False
    if len(data) > 32:
        return False
    if data[0] != 0xAA or data[1] != 0x55:
        return False
    n = data[2]
    if n < 1 or n > 8:
        return False
    off = 3
    for _ in range(n):
        if off + 2 > len(data) - 1:
            return False
        ln = data[off + 1]
        off += 2 + ln
        if off > len(data) - 1:
            return False
    if off != len(data) - 1:
        return False
    c = 0
    for b in data[:off]:
        c ^= b
    return (c & 0xFF) == data[off]


def init_uart3():
    global _uart
    from machine import UART, FPIOA
    fpioa = FPIOA()
    fpioa.set_function(32, FPIOA.UART3_TXD)
    fpioa.set_function(33, FPIOA.UART3_RXD)
    _uart = UART(
        UART.UART3,
        baudrate=UART3_BAUD,
        bits=UART.EIGHTBITS,
        parity=UART.PARITY_NONE,
        stop=UART.STOPBITS_ONE
    )
    print("[CMD] UART3 ready @%d" % UART3_BAUD)
    return _uart


class CommandLink:
    """反向控制：排空 socket 缓冲；AA55 UART 帧写 UART3，CBR 控制包回调。"""

    def __init__(self, uart, on_ctrl=None):
        self.uart = uart
        self.ok = False
        self.rx = 0
        self.ctrl_rx = 0
        self._last_rx = 0
        self._on_ctrl = on_ctrl

    def pump(self, sock):
        now = _ticks_ms()
        latest = None
        try:
            sock.settimeout(0)
            while True:
                data = sock.recv(64)
                if not data:
                    break
                if len(data) >= 4 and bytes(data[:3]) == b"CBR" and data[3] == 0x01:
                    if self._on_ctrl:
                        self._on_ctrl(b"IDR")
                    self.ctrl_rx += 1
                    continue
                if valid_frame(data):
                    latest = data
        except OSError:
            pass
        if latest is not None:
            try:
                result = self.uart.write(latest)
                # MicroPython UART.write 可返回实际写入字节数；必须等于帧长才算成功
                wrote = len(latest) if result is None else int(result)
                if wrote is False or wrote != len(latest):
                    print("[CMD] UART short write %s/%d" % (result, len(latest)))
                    self.ok = False
                    return
            except Exception as e:
                print("[CMD] UART write error: %s" % str(e))
                self.ok = False
                return
            self.rx += 1
            self._last_rx = now
            if not self.ok:
                print("[CMD] server streaming")
                self.ok = True
        elif _ticks_diff(now, self._last_rx) > CMD_STALE_MS and self._last_rx:
            if self.ok:
                print("[CMD] feed lost")
                self.ok = False

    def stats_line(self):
        return "[CMD] rx=%d ok=%s" % (self.rx, self.ok)


def stream_loop(sock, server_ip, sta):
    stream = StreamData()
    parameter_sets = None
    pacer = Pacer(PACER_WIRE_KBPS, PACER_BURST_BYTES, get_clock(), PACER_SPAN_MAX_MS)
    gate = IdrGate(IDR_REQ_MIN_INTERVAL_MS, IDR_REQ_SETTLE_MS)
    st = TxStats()
    targets = [(server_ip, p) for p in RTP_PORTS]
    ssrc = 0x54494E44
    seq = 0
    ts_step = 90000 // FPS
    frame_count = 0
    last_sent_ms = _ticks_ms() - FRAME_PERIOD_MS
    t_stat = time.time()
    pending_idr = [True]
    # 丢过 P 帧后置位：参考链已断，而丢帧不消耗 seq，接收端看不到缺口，
    # 不会自行请求 IDR。下一帧成功发出后据此补一个 IDR 请求重新起链。
    need_resync = False

    def _on_ctrl(_data):
        pending_idr[0] = True

    cmdline = CommandLink(init_uart3(), on_ctrl=_on_ctrl)

    while True:
        os.exitpoint()

        if not ensure_wifi(sta):
            time.sleep(2)
            continue

        cmdline.pump(sock)

        if pending_idr[0]:
            pending_idr[0] = False
            if gate.request(_ticks_ms()):
                encoder.RequestIDR()
                st.forced_idr += 1
                print("RTP: requested IDR")

        if encoder.GetStream(stream, timeout=20) != 0:
            continue

        now_ms = _ticks_ms()
        if _ticks_diff(now_ms, last_sent_ms) < FRAME_PERIOD_MS:
            st.overfps += 1
            encoder.ReleaseStream(stream)
            continue
        last_sent_ms = now_ms

        try:
            packets = []
            frame_bytes = 0
            is_idr = False
            seq_next = seq
            for i in range(stream.pack_cnt):
                data = uctypes.bytearray_at(stream.data[i], stream.data_size[i])
                stype = stream.stream_type[i]
                if stype == encoder.STREAM_TYPE_HEADER:
                    parameter_sets = bytes(data)
                    continue
                if stype == encoder.STREAM_TYPE_I:
                    is_idr = True
                data_bytes = bytes(data)
                frame_bytes += len(data_bytes)
                if stype == encoder.STREAM_TYPE_I and parameter_sets:
                    for nalu in split_nalus(parameter_sets):
                        seq_next = packetize_nalu(nalu, frame_count * ts_step, seq_next, ssrc, packets)
                for nalu in split_nalus(data_bytes):
                    seq_next = packetize_nalu(nalu, frame_count * ts_step, seq_next, ssrc, packets)
            if not packets:
                continue
            packets[-1] = bytearray(packets[-1])
            packets[-1][1] |= 0x80

            action, copies, air_ms = frame_admission(
                frame_bytes, len(packets), is_idr, WIFI_LINK_KBPS,
                FRAME_INTERVAL_MS, MAX_PFRAME_DUTY, pacer,
                max_copies=LINK_COPIES, idr_copies=IDR_COPIES)
            if action == FRAME_DROP_DUTY:
                st.dropped += 1
                st.over_duty += 1
                need_resync = True
                continue
            if action == FRAME_DROP_STARVE:
                st.dropped += 1
                st.starved += 1
                need_resync = True
                continue
            if is_idr:
                gate.note_idr_sent(_ticks_ms())
                need_resync = False

            _sink = fanout_sink(targets[:copies], sock, st)

            try:
                send_paced(pacer, packets, _sink, copies)
                st.note_frame(frame_bytes * copies, len(packets), is_idr,
                              air_ms)
                if copies > 1:
                    st.fanout_copies += copies - 1
            except SendAborted as e:
                st.pkt_err += 1
                print("sendto err: %s" % str(e))
            seq = seq_next
            frame_count = (frame_count + 1) & 0xFFFFFFFF
            if need_resync and not is_idr:
                # 参考链断过：等下一帧真的发出去了再要 IDR，避免空转刷请求。
                # IdrGate 自带限频/合并，不会退化成 IDR 风暴。
                pending_idr[0] = True
                need_resync = False
                st.resyncs += 1
        except Exception as e:
            print("send drop: " + str(e))
            st.dropped += 1
        finally:
            encoder.ReleaseStream(stream)

        if st.sent_frames and (st.sent_frames % GC_COLLECT_EVERY) == 0:
            gc.collect()

        now = time.time()
        if now - t_stat >= 10:
            try:
                free = gc.mem_free()
            except Exception:
                free = -1
            span = max(1e-6, now - t_stat)
            kbps = st.bytes_tx * 8 / span / 1000.0
            fps = st.sent_frames / span
            print(st.line(span, pacer, gate, free) + " (%s)" % cmdline.stats_line())
            util = kbps / WIFI_LINK_KBPS
            print(st.detail(pacer, gate))
            print("      P帧airtime %.1fms×拷%d / 预算 %.1fms | 节流上限 %dKbps | "
                  "链路占用 %.0f%%%s" % (
                      st.last_pframe_ms, LINK_COPIES,
                      FRAME_INTERVAL_MS * MAX_PFRAME_DUTY,
                      PACER_WIRE_KBPS, util * 100.0,
                      "  << 接近链路上限，拷贝数将自适应下调" if util > 0.9 else ""))
            if fps > FPS * 1.15:
                print("      !! 实际 %.1f fps 超过配置 %d，码率分母错，"
                      "软件闸门失效" % (fps, FPS))
            if pacer.cap_events:
                print("      !! 节流被 span 上限截断 %d 次：I 帧超过 %dms 的"
                      "节流预算，突发未被完全摊平（需真机确认 I 帧大小）" % (
                          pacer.cap_events, PACER_SPAN_MAX_MS))
            if cmdline.ctrl_rx:
                print("      IDR 请求 %d 次 -> 生效 %d 次（合并 %d），"
                      "强制 I 帧 %.0fKB/s" % (
                          gate.rx, gate.granted, gate.rx - gate.granted,
                          st.idr_bytes * 8 / span / 1000.0))
            t_stat = now
            st.reset_rolling()
            gate.reset_rolling()
            pacer.wait_us = 0
            pacer.sleeps = 0
            pacer.packets = 0


def cleanup():
    global sensor, link, encoder
    if sensor is not None:
        try:
            sensor.stop()
        except Exception:
            pass
    if link is not None:
        try:
            link.destroy()
        except Exception:
            pass
        link = None
    if encoder is not None and encoder.chn >= 0:
        try:
            encoder.Stop()
            encoder.Destroy()
        except Exception:
            pass
        encoder = None


def make_sock():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rcvbuf = getattr(socket, "SO_RCVBUF", None)
    if rcvbuf is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, rcvbuf, 4 << 20)
        except Exception:
            pass
    try:
        sock.bind(("0.0.0.0", 0))
    except Exception:
        pass
    return sock


def main():
    init_camera()
    _ip, gateway = connect_wifi()
    server_ip = SERVER_IP or gateway
    init_encoder()
    sock = make_sock()
    print("[4/4] pushing RTP h264 to %s ports=%s (%dKBps enc, %dKBps link, "
          "copies=%d, gop=%d)" % (
              server_ip, "/".join(str(p) for p in RTP_PORTS),
              BIT_RATE * 1000 // 8, WIFI_LINK_KBPS, LINK_COPIES, GOP_LEN))
    stream_loop(sock, server_ip, get_sta())


try:
    main()
except KeyboardInterrupt as e:
    print("User stop: " + str(e))
except BaseException as e:
    import sys
    sys.print_exception(e)
finally:
    cleanup()
    try:
        if _uart is not None:
            _uart.deinit()
    except Exception:
        pass
    try:
        if _sta is not None:
            _sta.active(False)
    except Exception:
        pass
    gc.collect()
