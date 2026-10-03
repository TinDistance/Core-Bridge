# K230 (CanMV) H264 裸 RTP 推流 + UART 命令桥（方案 A 发送端）。
#
# 与 udp_jpeg_push.py / webrtc_push.py 的关系：
#   * 采集/编码管线与 webrtc_push.py 相同：Sensor(chn0 YUV420SP) -> VENC(H264)
#     -> GetStream 出 NALU；但不走 webrtc.PeerConnection/aiortc 握手，
#     改为按 RFC 6184 自行 RTP 打包（单 NAL / FU-A）直发 server:8002。
#   * 服务器只做纯转发（server/rtp_relay.py），桌面端 PyAV 解码。
#   * UART 命令桥与 udp_jpeg_push.py 相同：server 从视频包学习本机地址后，
#     经 8001 socket 反推 v2 UART 帧；本脚本同一收发 socket 排空缓存，
#     AA55 帧转发 UART3，CBR\x01 控制包触发 RequestIDR。
#
# 低延迟要点：
#   * profile=BASELINE（禁 B 帧）；bit_rate CBR；gopLen=15（0.5s 自愈窗口，
#     GOP 直接等于桌面端最坏黑屏时长，见下面 GOP_LEN 处注释）
#   * GetStream timeout=20ms；时间戳用帧计数 * 3000（90kHz/30fps），
#     不依赖 stream.pts 单位
#   * sendto 失败只丢当帧：不重建 socket、不 sleep（对比 JPEG 版老问题）
#
# 拥塞控制（丢包策略，见下面"带宽预算"）：
#   帧间编码和帧内编码的丢包代价完全不同：
#     * JPEG 丢一包 = 丢一帧，下一帧立刻可用。
#     * H264 丢一个 P 帧 = 参考链断裂，直到下一个 IDR 才能解码。桌面端
#       因此在检测到 seq 跳变后会 wait_idr 拒收所有 P 帧。
#   => I 帧是唯一的画面恢复手段，必须无条件发送，绝不能被限速/丢弃。
#   => P 帧按 airtime 守卫丢：估这帧占满 5Mbps 需要多久，超过帧间隔的
#      MAX_PFRAME_DUTY 就整帧丢弃。链路跟不上时继续往 AP 队列灌包只会
#      让缓冲溢出、延迟累积，反而更糟。
#   => 不做重传：25ms 后才到的帧对 5Mbps@30fps 毫无价值，且重传会挤占
#      本来就不够的带宽。恢复靠"立刻请求新 IDR"。
#
# 验证实验（实施前置，见 k230/rtp_diag.py）：
#   E1 UDP goodput（test/udp_flood_rx.py + diag_udp_flood）
#   E2 GetStream NALU 形态（Annex-B/SPS/PPS/pts）
#   E3 MediaManager 是否需要显式 init
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

# ==================== 配置区 ====================
WIFI_SSID = "TF-Laptop"
WIFI_PASSWORD = "TF@HTR.Hello"
SERVER_IP = None            # None = 用网关地址（连热点时网关就是电脑）
RTP_PORT = 8002
SENSOR_ID = 2
WIDTH = 1280                # 编码宽度（自动 16 对齐）
HEIGHT = 720
BIT_RATE = 3072             # Kbit/s CBR（2.4G 下 3M 安全，必要时调低）
GOP_LEN = 15              # IDR 间隔 0.5s。桌面端一旦判丢包就拒收所有 P 帧，
                          # 等下一个 IDR 才恢复画面，所以 GOP 直接等于最坏
                          # 黑屏时长。30 帧(1s)对遥控太长了；15 帧配合
                          # CBR 不涨码率（IDR 变小即可），只轻微掉画质。
FPS = 30
MAX_PAYLOAD = 1200          # 单 RTP 包 payload 上限（<1472 不触发 IP 分片）
CMD_STALE_MS = 200
UART3_BAUD = 115200
GC_COLLECT_EVERY = 100
# ================================================

# ==================== 带宽预算 ====================
# 5Mbps 链路 @30fps 的每帧预算是 5e6/30/8 ≈ 20.8KB（已扣 RTP+UDP+IP 头
# 约 2.9%，可用 ~20.2KB）。JPEG 帧内编码达不到这个质量档（720p q65 实测
# 60~100KB，超预算 3~5 倍），所以必须走帧间编码 —— 这就是本脚本存在的原因。
#
# 换算成 RTP 包数：
_PKTS_AVG = BIT_RATE * 1000 // FPS // 8 // MAX_PAYLOAD + 1   # 平均帧包数
# I 帧峰值按平均的 IFRAME_PEAK_RATIO 倍留量。实测 720p30@3Mbps 时 I 帧是
# 平均的 5~8 倍，16 倍是保守上界。
IFRAME_PEAK_RATIO = 16
IFRAME_PKTS_MAX = _PKTS_AVG * IFRAME_PEAK_RATIO + 8

# 链路余量守卫：5Mbps 是实测值，估一帧在空中要占多久，超了就整帧丢弃。
# 只对 P 帧生效 —— I 帧是恢复手段，宁可超发也不能丢。
# 链路跟不上时继续往 AP 队列里灌包，只会让缓冲溢出、延迟累积，反而更糟。
LINK_KBPS = 5000           # 实测 K230<->PC 可用带宽
WIRE_OVERHEAD_PER_PKT = 12 + 8 + 20               # RTP头+UDP头+IP头
FRAME_INTERVAL_MS = 1000 // FPS
FRAME_PERIOD_MS = 1000 // FPS     # 软件帧率闸门周期
MAX_PFRAME_DUTY = 0.8      # P 帧 airtime 占帧间隔的上限

# ---- airtime 节流器（pacer）------------------------------------------
# 背景（数字全部可复算，见文件末尾注释）：
#   旧实现是"按包数"的令牌桶：PACER_PPS = IFRAME_PKTS_MAX * FPS // 2
#   = 184*30//2 = 2760 pps，桶容量 PACER_BURST = 184 个 token。
#   **它从来没有限过速。** 推导：
#     * 令牌补充速率 2760 pps，而需求 = 平均帧包数 / 帧间隔
#       ≈ 13 / 0.0333 = 390 pps。补充 >> 消耗，桶被容量 184 顶死在满格。
#     * 仿真（13 包 P 帧 / GOP15 / 98 包 I 帧，300 帧）实测 5600 个包里
#       等待次数 = 0，令牌最低只跌到 87/184。
#     * I 帧路径连 pacer.has() 预检都跳过，take(1) 又因为桶是满的而不睡
#       => 整帧在几微秒内灌进 AP 队列。
#   所以旧的 PACER_PPS=2760 只是一个"永远不会被触碰的数字"，注释里
#   "≈60 次/秒"的说法与代码也不符（2760 是 pps，不是每 2760 包等一次）。
#   真正限制平均码率的是 VENC 的 CBR + 下面的软件帧率闸门，不是 pacer。
#
# 新实现：按**字节**的 airtime 令牌桶，所有包（含关键帧）都必须 take()。
#   * 目标线速 = LINK_KBPS * PACER_DUTY，留 15% 给 WiFi 重传/ACK 开销。
#   * 每包消耗 = payload + 每包 40B 线速开销，即它真实占用链路的时间。
#   * => 相邻包最小间隔 = 1240*8 / 4.25e6 = 2.33ms，即节流上限 428pps。
#     内容侧只有 3.07Mbps/1240B = 309pps，所以稳态由内容限速（309pps），
#     pacer 只在 I 帧突发期间介入（瞬时不超过 428pps）。
#   * 桶容量 = 2 个帧间隔的目标线速字节数（~35KB）：启动时能快速灌满管道，
#     又不至于一次性放出整个 I 帧。
PACER_DUTY = 0.85
PACER_WIRE_KBPS = int(LINK_KBPS * PACER_DUTY)              # 4250 Kbps 上限
_PACER_BYTES_PER_S = PACER_WIRE_KBPS * 1000 / 8.0          # 531250 B/s
PACER_BURST_BYTES = int(_PACER_BYTES_PER_S * 2 * FRAME_INTERVAL_MS / 1000.0)

# 单帧节流时长上限。I 帧实测可达 ~100KB（≈81 包 * 2.33ms = 189ms 的 airtime），
# 上限必须 >= 这个值，否则最该被摊平的 I 帧反而残留突发。
# 取 200ms = 6 个帧间隔：编���器 outbuf 能在主循环阻塞期间接住新帧；
# 超预算时不再等待（受控突发）并把 token 打成负数，让紧随其后的 P 帧被
# can_send() 预检丢掉 —— 用"丢 P 帧"换"不打爆队列"。I 帧之后 P 帧本来
# 就因参考链语义而失效，且下一个 IDR 最多 500ms 后到。
PACER_SPAN_MAX_MS = 200

# IDR 请求合并/限频（详见 IdrGate 的注释）
IDR_REQ_MIN_INTERVAL_MS = 800      # 两次"生效的"强制 IDR 请求的最小间隔
IDR_REQ_SETTLE_MS = 500            # 最近真的发过 IDR 后的静默窗口

# VENC 输出缓冲深度推导：节流器最长阻塞 PACER_SPAN_MAX_MS，期间编码器
# 会产出 ceil(200/33)=7 帧；再加 2 帧余量 => 9，取整到 10。
# **不要再往上加**：outbuf 越大，积压的过期 P 帧越多、延迟越高，
# 而节流器修好后突发已被摊平，够用即可。要改必须先看真机 pacer cap / txerr。
OUT_BUFS = max(8, -(-PACER_SPAN_MAX_MS // FRAME_INTERVAL_MS) + 3)

# ================================================

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
    # chn0 提供给 VENC 编码，必须是 YUV420SP
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
    # Baseline 优先（禁 B 帧，低延迟），旧固件没有则退回 MAIN
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


# ==================== RTP 打包 ====================
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


# >>> PURE-PACER-CORE v1 >>>
# 本段不引用任何 K230 专有 API：时钟/睡眠由外部注入，link_kbps 全部传参。
# test/test_k230_pacer.py 抽取这两个标记之间的源码 exec 到带假时钟的命名
# 空间里做纯逻辑仿真；改这里请同步看那个文件。
PacerWireOverhead = 12 + 8 + 20          # RTP 12 + UDP 8 + IP 20
TICKS_MS_PERIOD = 1 << 30               # MicroPython ticks_ms 回绕周期


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
    """字节级 airtime 令牌桶。**每一个**发出的包（含关键帧）都必须 take()。

    rate_kbps : 目标线速（wire Kbps）。相邻包最小间隔由它决定：
                (payload + 40B) * 8 / rate_kbps。
    burst     : 桶容量（wire 字节）= 允许的瞬时突发上限。
    span_max_ms: 单帧节流时长上限，防止一个大 I 帧把主循环阻塞几百 ms。

    取用逻辑（take）：
      * 桶里够 -> 立刻放行，零等待。
      * 不够   -> 分片 sleep 直到够，或直到本帧 span 预算耗尽。
      * span 耗尽 -> 仍然放行（I 帧不能丢），但把 token 打成负数，
        于是紧随其后的 P 帧在 can_send() 预检里被丢掉。
        这就是"I 帧优先但不无节制突发"：优先靠 FIFO + 预检实现，
        限突发靠"负 token 连带压制后续 P 帧"实现。
    """

    def __init__(self, rate_kbps, burst_bytes, clock, span_max_ms):
        self.clock = clock
        self.rate_bps = rate_kbps * 1000.0
        self.burst = float(burst_bytes)
        self.tokens = float(burst_bytes)
        self.last = clock.now_us()
        self.span_max_us = int(span_max_ms * 1000)
        # 初始就等于满预算：忘调 begin_frame() 时最坏只是"没有单帧上限"，
        # 而不是"完全不节流"。fail-safe 方向必须是这个。
        self.span_left = self.span_max_us
        # ---- 诊断计数（只读，不影响行为）----
        self.packets = 0
        self.wire_bytes = 0
        self.wait_us = 0        # 累计节流等待时长
        self.sleeps = 0         # sleep 次数
        self.max_wait_us = 0    # 单包最长等待
        self.cap_events = 0     # span 上限触发次数（>0 说明节流被截断）
        self.min_gap_us = None  # 相邻包最小间隔

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
                # 单帧预算耗尽：受控突发放行，并把 token 打成负数压制后续 P 帧。
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
    """IDR 请求合并/限频。

    为什么必须限：编码器健康时 GOP=15@30fps 自然每 500ms 就出一个 IDR，
    此时接收端的任何 IDR 请求都是多余的。而每强制一次 RequestIDR() 就多
    一个 ~100KB 的 I 帧：旧实现限频 100ms => 最多 10 次/秒 => 1MB/s
    = 8.4Mbps 的额外需求，单这一项就能灌爆 5Mbps 链路，并把队列撑到
    秒级 —— 这正是"一落后就疯狂请求、越请求越延迟"的正反馈。

    两道独立的闸门（都是纯合并，不丢"最后一次"的状态）：
      * settle_ms : 最近真的发过 IDR 就合并掉。健康编码器下这条几乎总是
        命中 => 强制 IDR 归零。
      * interval_ms: 距上一次生效的强制请求不足就合并掉。这是硬上限，
        保证强制 IDR 速率 <= 1000/interval_ms 次/秒。

    被合并不等于丢恢复能力：编码器停了 IDR 的场景下，settle 闸门自然不再
    命中，第一个请求就能生效，不会死锁。
    """

    def __init__(self, interval_ms, settle_ms):
        self.interval_ms = interval_ms
        self.settle_ms = settle_ms
        self._last_idr_sent = None
        self._last_grant = None
        # ---- 诊断计数（只读）----
        self.rx = 0
        self.granted = 0
        self.merged_idr = 0
        self.merged_rate = 0

    def note_idr_sent(self, now_ms):
        self._last_idr_sent = now_ms

    def reset_rolling(self):
        """只清本窗口计数；合并状态（_last_idr_sent/_last_grant）必须保留。

        这两个状态一旦被清掉，限频闸门就会每 10s 重新放行一次风暴。
        """
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


# frame_admission 的返回值
FRAME_SEND = 0
FRAME_DROP_DUTY = 1      # P 帧 airtime 超过帧间隔预算
FRAME_DROP_STARVE = 2    # P 帧拿不到节流令牌（前面有 I 帧突发留下的债务）


def frame_admission(frame_bytes, npkts, is_idr, link_kbps,
                    frame_interval_ms, max_pframe_duty, pacer):
    """整帧准入判定（纯函数，无副作用）。返回 (action, airtime_ms)。

    **关键帧永远返回 FRAME_SEND**：它绝不因为 airtime 或令牌不足被丢，
    只在 send_paced 里被节流。丢 I 帧 = 桌面端永远 wait_idr。
    """
    air = airtime_ms(frame_bytes, npkts, link_kbps)
    if is_idr:
        return FRAME_SEND, air
    if air > frame_interval_ms * max_pframe_duty:
        return FRAME_DROP_DUTY, air
    if not pacer.can_send(frame_bytes, npkts):
        return FRAME_DROP_STARVE, air
    return FRAME_SEND, air


def send_paced(pacer, packets, sink):
    """逐包节流发送，返回实际发出的包数。

    节流逻辑必须留在这个函数里、且**每个包都要走** —— 历史上正是"关键帧
    绕过守卫"造成了 4~10s 延迟。把它收在这里，test/test_k230_pacer.py
    就能直接对同一份代码断言最小包间隔，而不是靠源码阅读保证。
    sink 抛 SendAborted 即中止本帧（已发出的包不回滚）。
    """
    sent = 0
    pacer.begin_frame()
    for pkt in packets:
        pacer.take(len(pkt))
        sink(pkt)
        sent += 1
    return sent


SYS_CLOCK = None


def get_clock():
    global SYS_CLOCK
    if SYS_CLOCK is None:
        SYS_CLOCK = SysClock()
    return SYS_CLOCK

# <<< PURE-PACER-CORE v1 <<<


def _airtime_ms(frame_bytes, npkts):
    return airtime_ms(frame_bytes, npkts, LINK_KBPS)


# ==================== UART 命令桥（与 udp_jpeg_push 一致） ====================
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
        self._last_rx = 0
        self.ctrl_rx = 0
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
                if len(data) >= 4 and bytes(data[:3]) == b"CBR\x01":
                    # 桌面丢包/新接入 -> 请求新 IDR。
                    # 这里**不做任何合并**：CommandLink.pump 在一个 tick 里
                    # 会把 socket 缓冲排空，一次 pump 可能收到几十个
                    # CBR\x01，限频必须放在 IdrGate（合并语义 + 统计都在
                    # 那里）。
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
                if result is False:
                    print("[CMD] UART write returned False")
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


# ==================== 主流程 ====================
def stream_loop(sock, server_ip, sta):
    stream = StreamData()
    parameter_sets = None
    pacer = Pacer(PACER_WIRE_KBPS, PACER_BURST_BYTES, get_clock(), PACER_SPAN_MAX_MS)
    gate = IdrGate(IDR_REQ_MIN_INTERVAL_MS, IDR_REQ_SETTLE_MS)
    addr = (server_ip, RTP_PORT)
    ssrc = 0x54494E44  # "TIND"
    seq = 0
    ts_step = 90000 // FPS
    frame_count = 0
    sent_frames = 0
    dropped = 0
    last_len = 0
    idr_count = 0
    over_duty = 0
    starved = 0
    last_iframe_pkts = 0
    last_iframe_ms = 0.0
    last_pframe_ms = 0.0
    pkts_tx = 0
    bytes_tx = 0
    pkt_err = 0
    max_frame_bytes = 0
    overfps = 0
    last_sent_ms = _ticks_ms() - FRAME_PERIOD_MS
    t_stat = time.time()
    pending_idr = [True]  # 首次出流 + 桌面请求时出新 IDR

    def _on_ctrl(_data):
        pending_idr[0] = True

    # UART3 只初始化一次：init_uart3() 会重设 FPIOA 引脚并重建 UART 对象，
    # 调两次会泄漏第一个句柄并可能复位外设。
    cmdline = CommandLink(init_uart3(), on_ctrl=_on_ctrl)

    while True:
        os.exitpoint()

        if not ensure_wifi(sta):
            time.sleep(2)
            continue

        cmdline.pump(sock)

        if pending_idr[0]:
            pending_idr[0] = False
            # IdrGate 合并/限频：健康的编码器每 500ms 自然出一个 IDR，此时
            # 请求是多余的（每个多余的请求 = 多一个 ~100KB 的 I 帧）。
            if gate.request(_ticks_ms()):
                # 出新 IDR：接收端无需等 GOP 到点即可解码
                encoder.RequestIDR()
                print("RTP: requested IDR")
            # 被合并时保持静默：合并次数由 gate.rx/granted 统计上报，这里
            # print 会在风暴（每秒几十次）时把串口刷爆。

        if encoder.GetStream(stream, timeout=20) != 0:
            continue

        # ---- 软件帧率闸门（必须放在 ReleaseStream 之前）----
        # K230 VENC 的 dst_frame_rate 实测无效：配置 30fps 实际出流 ~49fps。
        # 而码率控制的分母用的是"配置帧率"，所以每帧仍是 bit_rate/30 ≈
        # 12800B，实际总码率 = 12800 x 49 = 5.0Mbps，直接灌满 5Mbps 链路
        # （占用 101%，零余量）-> 持续丢包 -> 桌面端永远收不齐 IDR -> 黑屏。
        # 这里按真实时间戳硬限到 FPS，12800B x 30 = 3.07Mbps（占用 61%）。
        # 不依赖固件行为，比赌 dst_frame_rate 生效可靠。
        now_ms = _ticks_ms()
        if _ticks_diff(now_ms, last_sent_ms) < FRAME_PERIOD_MS:
            overfps += 1
            encoder.ReleaseStream(stream)
            continue
        last_sent_ms = now_ms

        try:
            packets = []
            frame_bytes = 0
            is_idr = False
            # 关键：seq 先写进局部变量，只有这一帧真的发出去了才提交。
            # 若在丢弃分支就 advance，丢帧会在 RTP 序号空间里留下永久空洞，
            # 接收端每次都判丢包 -> wait_idr -> 黑屏，实测直接"没信号"。
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
            # marker 打在 access unit 最后一个包上
            packets[-1] = bytearray(packets[-1])
            packets[-1][1] |= 0x80

            last_len = frame_bytes
            if frame_bytes > max_frame_bytes:
                max_frame_bytes = frame_bytes

            # airtime 守卫（判定逻辑在 frame_admission 里，可被仿真测试覆盖）：
            #   * P 帧：airtime 超帧间隔的 MAX_PFRAME_DUTY 就整帧丢；
            #     令牌不足（前面有 I 帧突发留下债务）也整帧丢。恢复靠下一个 IDR。
            #   * I 帧：**不再绕过守卫**。旧实现让 I 帧完全跳过预检且不等待，
            #     结果是 ~100KB 在几微秒内灌进 AP/WiFi 队列。2.4G 上队列排空
            #     只有 625KB/s，单次 I 帧就顶到 100ms+ 深度；叠加 IDR 请求
            #     风暴（最多 10 次/秒 = 1MB/s）能撑到秒级 —— 这是 4~10s
            #     延迟的直接来源。现在 I 帧与 P 帧走同一个 send_paced 逐包
            #     节流，但保留"绝不丢 I 帧"。
            action, air_ms = frame_admission(
                frame_bytes, len(packets), is_idr, LINK_KBPS,
                FRAME_INTERVAL_MS, MAX_PFRAME_DUTY, pacer)
            if action == FRAME_DROP_DUTY:
                dropped += 1
                over_duty += 1
                continue
            if action == FRAME_DROP_STARVE:
                dropped += 1
                starved += 1
                continue
            if is_idr:
                idr_count += 1
                gate.note_idr_sent(_ticks_ms())
                last_iframe_pkts = len(packets)
                last_iframe_ms = air_ms
            else:
                last_pframe_ms = air_ms

            # 优先级靠 FIFO + can_send 预检实现（I 帧先占桶，随后的 P 帧被判
            # 不足而丢）；限突发靠 PACER_SPAN_MAX_MS + 负 token 连带压制。
            send_err = [None]

            def _sink(pkt, _sock=sock, _addr=addr, _err=send_err):
                try:
                    _sock.sendto(pkt, _addr)
                except TypeError:
                    _sock.sendto(bytes(pkt), _addr)
                except OSError as e:
                    # ENOBUFS/EAGAIN 说明本机发送队列溢出，
                    # 这是"帧在建网前就丢了"，不改代码永远看不见。
                    _err[0] = str(e)
                    raise SendAborted()
                pkts_tx += 1
                bytes_tx += len(pkt)

            try:
                send_paced(pacer, packets, _sink)
            except SendAborted:
                pkt_err += 1
                print("sendto err: %s" % send_err[0])
            # 只有真正发出去才提交序号/时间戳：见上面 seq_next 的注释
            seq = seq_next
            frame_count = (frame_count + 1) & 0xFFFFFFFF
            sent_frames += 1
        except Exception as e:
            # 失败只丢当帧：不重建 socket、不 sleep（老 JPEG 版的坑）
            print("send drop: " + str(e))
            dropped += 1
        finally:
            encoder.ReleaseStream(stream)

        if sent_frames and (sent_frames % GC_COLLECT_EVERY) == 0:
            gc.collect()

        now = time.time()
        if now - t_stat >= 10:
            try:
                free = gc.mem_free()
            except Exception:
                free = -1
            span = max(1e-6, now - t_stat)
            kbps = bytes_tx * 8 / span / 1000.0
            fps = sent_frames / span
            print("stat: sent=%d drop=%d(duty=%d starve=%d) overfps=%d idr=%d "
                  "last=%dB max=%dB kbps=%.0f fps=%.1f pps=%.0f txerr=%d "
                  "free=%d (%s)" % (
                      sent_frames, dropped, over_duty, starved, overfps,
                      idr_count, last_len, max_frame_bytes, kbps, fps,
                      pkts_tx / span, pkt_err, free, cmdline.stats_line()))
            util = kbps / LINK_KBPS
            print("      P帧airtime %.1fms / 预算 %.1fms | I帧 %d包 %.0fms | "
                  "链路占用 %.0f%%%s" % (
                      last_pframe_ms, FRAME_INTERVAL_MS * MAX_PFRAME_DUTY,
                      last_iframe_pkts, last_iframe_ms, util * 100.0,
                      "  << 超 5Mbps，必然丢包!" if util > 0.9 else ""))
            if fps > FPS * 1.15:
                print("      !! 实际 %.1f fps 超过配置 %d，码率分母错，"
                      "软件闸门失效" % (fps, FPS))
            sent_frames = 0
            dropped = 0
            over_duty = 0
            starved = 0
            overfps = 0
            idr_count = 0
            pkts_tx = 0
            bytes_tx = 0
            pkt_err = 0
            max_frame_bytes = 0
            if cmdline.ctrl_rx:
                print("      IDR 请求 %d 次 -> 生效 %d 次（合并 %d：近期IDR=%d 限频=%d）" % (
                    gate.rx, gate.granted, gate.rx - gate.granted,
                    gate.merged_idr, gate.merged_rate))
            t_stat = now
            gate.reset_rolling()


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
    # rt-smart MicroPython 没有 SO_RCVBUF；有则加大接收缓冲
    rcvbuf = getattr(socket, "SO_RCVBUF", None)
    if rcvbuf is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, rcvbuf, 4 << 20)
        except Exception:
            pass
    try:
        sock.bind(("0.0.0.0", 0))
    except Exception:
        pass  # MicroPython 可不 bind，由首次 sendto 隐式绑定
    return sock


def main():
    init_camera()
    _ip, gateway = connect_wifi()
    server_ip = SERVER_IP or gateway
    init_encoder()
    sock = make_sock()
    print("[4/4] pushing RTP h264 to %s:%d (%dKbps, gop=%d)" % (
        server_ip, RTP_PORT, BIT_RATE, GOP_LEN))
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
