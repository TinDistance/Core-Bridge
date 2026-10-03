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

# 令牌桶定容（关键，见 stream_loop 的注释）：
#   PACER_BURST 必须 >= 一个 I 帧的包数。否则预检 pacer.has() 会把 I 帧
#   整帧丢掉，而 I 帧是丢包后唯一的画面恢复手段 —— 结果是桌面端永远卡在
#   wait_idr，一帧都出不来。
#
#   PACER_PPS 的作用不是"限平均码率"（那是 BIT_RATE 的事），而是把 I 帧
#   突发摊开到一帧多以内。定成平均包率(330)会掏空令牌桶：实测 3Mbps 下
#   GOP=15 需要约 557pps，而 330pps 发一个 44 包 I 帧要 133ms(4 个帧间隔)，
#   期间 has() 预检把 37% 的 P 帧全丢了。这里取 I 帧峰值在 ~2 个帧间隔
#   内发完，既摊开了突发，又永远饿不死 P 帧。
#   实测平均帧 ~13 包、I 帧 44~98 包、目标 ~322pps。
PACER_PPS = IFRAME_PKTS_MAX * FPS // 2
PACER_BURST = IFRAME_PKTS_MAX

# 链路余量守卫：5Mbps 是实测值，估一帧在空中要占多久，超了就整帧丢弃。
# 只对 P 帧生效 —— I 帧是恢复手段，宁可超发也不能丢。
# 链路跟不上时继续往 AP 队列里灌包，只会让缓冲溢出、延迟累积，反而更糟。
LINK_KBPS = 5000           # 实测 K230<->PC 可用带宽
WIRE_OVERHEAD_PER_PKT = 12 + 8 + 20               # RTP头+UDP头+IP头
FRAME_INTERVAL_MS = 1000 // FPS
FRAME_PERIOD_MS = 1000 // FPS     # 软件帧率闸门周期
MAX_PFRAME_DUTY = 0.8      # P 帧 airtime 占帧间隔的上限

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


def _airtime_ms(frame_bytes, npkts):
    """估一帧占满链路 airtime 的毫秒数（payload + 每包 40B 头开销）。"""
    wire = frame_bytes + npkts * WIRE_OVERHEAD_PER_PKT
    return wire * 8 / LINK_KBPS


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
    encoder.SetOutBufs(8, width, HEIGHT)
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


class Pacer:
    """令牌桶：PPS 上限，突发超余量的帧整帧丢弃（主循环预检）。"""

    def __init__(self, rate=PACER_PPS, burst=PACER_BURST):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last = time.ticks_us()

    def refill(self):
        now = time.ticks_us()
        self.tokens += time.ticks_diff(now, self.last) * self.rate / 1000000.0
        self.last = now
        if self.tokens > self.burst:
            self.tokens = float(self.burst)

    def has(self, n):
        self.refill()
        return self.tokens >= n

    def take(self, n=1):
        self.refill()
        while self.tokens < n:
            wait_us = int((n - self.tokens) / self.rate * 1000000.0) + 100
            time.sleep_us(wait_us)
            self.refill()
        self.tokens -= n


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
        self._last_ctrl = 0
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
                    # 桌面丢包/新接入 -> 立刻出新 IDR（限频 100ms）
                    if _ticks_diff(now, self._last_ctrl) > 100 or self._last_ctrl == 0:
                        self._last_ctrl = now
                        if self._on_ctrl:
                            self._on_ctrl(b"IDR")
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
    pacer = Pacer(PACER_PPS, PACER_BURST)
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
            # 出新 IDR：接收端无需等 GOP 到点即可解码
            encoder.RequestIDR()
            pending_idr[0] = False
            print("RTP: requested IDR")

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

            # 整帧预检：令牌不够就整帧丢弃，不发半帧。
            # 但 I 帧必须无条件放行 —— 它是丢包后唯一的画面恢复手段，
            # 丢了 I 帧桌面端就只能一直等下一个 IDR。
            if is_idr:
                last_iframe_pkts = len(packets)
                last_iframe_ms = _airtime_ms(frame_bytes, len(packets))
                idr_count += 1
            else:
                air_ms = _airtime_ms(frame_bytes, len(packets))
                last_pframe_ms = air_ms
                if air_ms > FRAME_INTERVAL_MS * MAX_PFRAME_DUTY:
                    dropped += 1
                    over_duty += 1
                    continue
                if not pacer.has(len(packets)):
                    dropped += 1
                    starved += 1
                    continue

            for pkt in packets:
                pacer.take(1)
                try:
                    sock.sendto(pkt, addr)
                except TypeError:
                    sock.sendto(bytes(pkt), addr)
                except OSError as e:
                    # sendto 失败必须计数：ENOBUFS/EAGAIN 说明本机发送队列
                    # 溢出，这是"帧在建网前就丢了"，不改代码永远看不见。
                    pkt_err += 1
                    print("sendto err: %s" % str(e))
                    break
                pkts_tx += 1
                bytes_tx += len(pkt)
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
            t_stat = now


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
