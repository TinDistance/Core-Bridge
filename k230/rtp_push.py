import gc
import json
import os
import socket
import struct
import time
import uctypes

import network
from media.sensor import *
from media.media import *
from media.vencoder import *

# === WiFi 凭据 ===
# 已知的风险，团队决定接受：凭据硬编码在仓库里。
# /sdcard/tindistance.json 仍可覆盖（存在且合法时以文件为准），文件缺失则
# 直接用下面的默认值，不再报错退出 —— 换台机器 /sdcard 没这张卡也能跑起来。
WIFI_SSID = "TF-Laptop"
WIFI_PASSWORD = "TF@HTR.Hello"
CONFIG_PATH = "/sdcard/tindistance.json"
CONFIG_FIELDS = ("ssid", "password", "server_ip", "tier", "link_kbps")
# 凭据不再必填：有默认值兜底，配置文件只负责覆盖。
REQUIRED_CONFIG_FIELDS = ()

# === 接收端 IP：手动设置，不再默认推网关 ===
# 旧行为：server_ip 没配时退回 WiFi 网关地址（Windows 移动热点通常是
# 192.168.137.1）。那是个「猜」出来的地址 —— 推流照跑，但对端不是收流的那台
# 机器时表现为「一直黑屏 / 一直重连」，排查成本全在猜上。现在没有默认地址：
# 三处都没有就启动即报错退出，绝不往网关发包。
#
# 三种手动设置方式（优先级 env > 配置文件 > 这里的常量）：
#   1) 改下面这一行（最直接：改完再把脚本传上板子）
#   2) /sdcard/tindistance.json 里的 "server_ip"
#   3) 环境变量 SERVER_IP（rt-smart 上一般不存在，非主路径）
SERVER_IP = ""

# === 档位（K9/K10，文档 §1.1 带宽预算表）===
# tier -> (width, height, bit_rate_kbps)
TIERS = {
    "T1": (1280, 720, 3000),
    "T2": (1280, 720, 4500),
    "T3": (1920, 1080, 6000),
}
# 各档位要求的「实测可用带宽」（文档 §1.1 表最后一列）。低于它只警告不阻断。
TIER_MIN_LINK_KBPS = {"T1": 5000, "T2": 7500, "T3": 10000}
DEFAULT_TIER = "T3"
DEFAULT_WIFI_LINK_KBPS = 10000

RTP_PORT = 8002
SENSOR_ID = 2
FPS = 30

# === 单链路单拷贝（K1）===
# 旧设计按 15 Mbps 链路做 3 份冗余拷贝（4200 kbps × 3 ≈ 13 Mbps 线上）。
# 5–10 Mbps 链路上那是根本性错误：排队直接转成无界延迟，而且三份拷贝走
# 同一块无线电，WiFi 突发丢包高度相关，冗余几乎不提供保护。
# 现在单拷贝单端口，带宽全部给视频；丢包靠 NACK 重传（阶段 2）而不是靠拷贝。
LINK_COPIES = 1
IDR_COPIES = 1
# 单端口优先；保留元组形态是为了 fanout_sink 的多端口能力（单端口时长度 1）。
RTP_PORTS = (RTP_PORT,)

GOP_LEN = 60              # 长 GOP：IDR 流量占比从约 22% 降到约 6%（文档 §5）
MAX_PAYLOAD = 1400        # MTU 可选项：FU-A 2 + RTP 12 + UDP/IP 28 = 1442 < 1500
CMD_STALE_MS = 200
UART3_BAUD = 115200
GC_COLLECT_EVERY = 100
WIFI_RECONNECT_BUDGET_MS = 3000   # K13：单次重连阻塞预算（原 10s）

FRAME_INTERVAL_MS = 1000 // FPS
FRAME_PERIOD_MS = FRAME_INTERVAL_MS   # 仅诊断/测试参考，K2 已删除按它的丢帧闸门
MAX_PFRAME_DUTY = 0.95
STARVE_HOLD_MAX_MS = 500   # K7：hold 到下一个 IDR 为止，但冻结上界 ~0.5s

# 节流池 = 实测可用带宽 × duty（K6）。WIFI_LINK_KBPS 必须来自实测，
# 不再是硬编码的 15000。
PACER_DUTY = 0.9
PACER_SPAN_MAX_MS = 80    # IDR 最长摊开 80ms，原 300ms 直接冲破 100ms 预算
BURST_SEND_BYTES = 20000  # K5：整帧线速字节 ≤ 20KB 时突发直发，不逐包 sleep

# NACK 重传（阶段 2，文档 §4.3）
CONTROL_MAGIC = b"CBR"
CTRL_PING = 0x00
CTRL_IDR = 0x01
CTRL_BITRATE = 0x02
CTRL_NACK = 0x03
CTRL_PONG = 0x10
NACK_MAX_SEQS = 32
RETRANS_BUDGET_RATIO = 0.15   # 重传带宽 ≤ 码率预算的 15%
RETRANS_WINDOW_MS = 1000

IDR_REQ_MIN_INTERVAL_MS = 400
IDR_REQ_SETTLE_MS = 500

# === 运行期设置（由 load_config + resolve_settings 推导）===


def load_config(path):
    """纯函数：读 JSON 配置（**可选**）并校验字段类型。

    文件缺失 / 不可读 / 不是合法 JSON / 顶层不是对象 → 返回 None，表示
    「没有配置文件」，调用方回落到硬编码默认值（凭据、tier、link_kbps）。

    文件存在且合法 → 只返回出现在文件里的字段（部分覆盖也算数）。
    字段类型非法（`{"ssid": 123}`）→ 抛 RuntimeError，因为这通常是配置写错了
    而不是「没配置」，静默回落会把问题藏到 sta.connect() 才炸。
    """
    try:
        with open(path, "r") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        cfg = json.loads(raw)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    out = {}
    for k in CONFIG_FIELDS:
        if cfg.get(k) is not None:
            out[k] = cfg[k]
    for k in ("ssid", "password", "tier", "server_ip"):
        if k in out and not isinstance(out[k], str):
            raise RuntimeError('config field "%s" must be a string' % k)
    if "link_kbps" in out:
        try:
            out["link_kbps"] = int(out["link_kbps"])
        except (TypeError, ValueError):
            raise RuntimeError("config field \"link_kbps\" must be an integer")
        if out["link_kbps"] <= 0:
            raise RuntimeError("config field \"link_kbps\" must be > 0")
    return out


def valid_server_ip(ip):
    """纯函数：server_ip 必须是 IPv4 点分十进制字面量。

    刻意**不支持域名**：rt-smart 上一次 DNS 解析会阻塞主循环，而「手写错一个
    字符的 IP」如果放行，表现是 UDP 静默发进黑洞（对端永远收不到，也不会
    有任何报错）。宁可在启动时炸掉，也不要黑屏一整晚。
    """
    if not isinstance(ip, str):
        return False
    parts = ip.strip().split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part or len(part) > 3:
            return False
        for ch in part:
            if ch not in "0123456789":
                return False
        if int(part) > 255:
            return False
    return True


def resolve_settings(cfg, env=None):
    """纯函数：默认值 + 配置覆盖 + 可选 env 覆盖 → 运行期设置 dict。

    优先级 env > 配置文件 > 硬编码默认值。`cfg` 为 None 表示没有配置文件。
    env 是普通 dict（模块层从 os.environ 收集，单测直接传字面量）。
    server_ip 没有默认值：没配就是 None，由 main() 报错退出，绝不退回网关地址。
    """
    cfg = cfg or {}
    env = env or {}
    tier = env.get("tier") or cfg.get("tier") or DEFAULT_TIER
    if tier not in TIERS:
        raise RuntimeError("unknown tier %r (expect %s)"
                           % (tier, ",".join(sorted(TIERS))))
    width, height, bit_rate = TIERS[tier]
    try:
        link_kbps = int(env.get("link_kbps") or cfg.get("link_kbps")
                        or DEFAULT_WIFI_LINK_KBPS)
    except (TypeError, ValueError):
        raise RuntimeError("link_kbps must be an integer number")
    if link_kbps <= 0:
        raise RuntimeError("link_kbps must be > 0")
    server_ip = env.get("server_ip") or cfg.get("server_ip") or SERVER_IP or None
    if server_ip is not None:
        server_ip = server_ip.strip()
        if not server_ip:
            server_ip = None
        elif not valid_server_ip(server_ip):
            raise RuntimeError("server_ip %r is not a valid IPv4 address"
                               % server_ip)
    warnings = []
    need = TIER_MIN_LINK_KBPS[tier]
    if link_kbps < need:
        warnings.append(
            "tier %s 要求实测可用带宽 >= %d kbps（docs §1.1），配置只有 %d kbps；"
            "%d kbps 码率很可能压不动，帧会持续触发丢弃"
            % (tier, need, link_kbps, bit_rate))
    return {
        "ssid": env.get("ssid") or cfg.get("ssid") or WIFI_SSID,
        "password": env.get("password") or cfg.get("password") or WIFI_PASSWORD,
        "server_ip": server_ip,
        "tier": tier,
        "width": width,
        "height": height,
        "bit_rate": bit_rate,
        "link_kbps": link_kbps,
        "warnings": warnings,
    }


def _env_overrides():
    """可选调试通道：os.environ 覆盖（rt-smart 上通常不存在，非主路径）。"""
    out = {}
    for key, name in (("ssid", "WIFI_SSID"), ("password", "WIFI_PASSWORD"),
                      ("server_ip", "SERVER_IP"), ("tier", "TIER"),
                      ("link_kbps", "WIFI_LINK_KBPS")):
        try:
            val = os.environ.get(name) if hasattr(os, "environ") else None
        except Exception:
            val = None
        if val:
            out[key] = val
    return out


def _env_int(name):
    try:
        val = os.environ.get(name) if hasattr(os, "environ") else None
        return int(val) if val else None
    except Exception:
        return None


class ConfigError(RuntimeError):
    """配置没对（没设接收端 IP 等）。

    单独一个类型，只为了**不打调用栈**：配置问题不是 bug，`sys.print_exception`
    在 rt-smart 的 REPL 上打一串 traceback 会让人以为脚本炸了，而正确的处理
    只是照着提示改一行。继承 RuntimeError 是为了不破坏调用方（和单测）按
    RuntimeError 捕获的习惯。
    """


CONFIG = None
CONFIG_ERROR = None
try:
    CONFIG = load_config(CONFIG_PATH)
except Exception as e:
    # 文件存在但字段类型非法：这是配置写错了，不该被默认值悄悄掩盖。
    CONFIG_ERROR = str(e)
try:
    SETTINGS = resolve_settings(CONFIG, _env_overrides())
except Exception as e:
    # env 里的 tier/link_kbps 非法等；仍然不能炸掉模块导入。
    if CONFIG_ERROR is None:
        CONFIG_ERROR = str(e)
    SETTINGS = resolve_settings(None, {})

_env_port = _env_int("RTP_PORT")
if _env_port:
    RTP_PORT = _env_port
    RTP_PORTS = (RTP_PORT,)   # 端口只有 1 个；多端口形态保留给 fanout_sink

WIFI_SSID = SETTINGS["ssid"]
WIFI_PASSWORD = SETTINGS["password"]
SERVER_IP = SETTINGS["server_ip"]
TIER = SETTINGS["tier"]
WIDTH = SETTINGS["width"]
HEIGHT = SETTINGS["height"]
BIT_RATE = SETTINGS["bit_rate"]
WIFI_LINK_KBPS = SETTINGS["link_kbps"]

PACER_WIRE_KBPS = int(WIFI_LINK_KBPS * PACER_DUTY)
_PACER_BYTES_PER_S = PACER_WIRE_KBPS * 1000 / 8.0
PACER_BURST_BYTES = int(_PACER_BYTES_PER_S * 2 * FRAME_INTERVAL_MS / 1000.0)

OUT_BUFS = max(8, -(-PACER_SPAN_MAX_MS // FRAME_INTERVAL_MS) + 3)


sensor = None
encoder = None
link = None
_sta = None
_uart = None


def _ticks_us():
    if hasattr(time, "ticks_us"):
        return time.ticks_us()
    return int(time.time() * 1000000)


def _ticks_ms():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _ticks_diff(a, b):
    return time.ticks_diff(a, b) if hasattr(time, "ticks_diff") else a - b


def _ticks_diff_us(a, b):
    return time.ticks_diff(a, b) if hasattr(time, "ticks_diff") else a - b


def init_camera():
    global sensor
    sensor = Sensor(id=SENSOR_ID)
    sensor.reset()
    width = ALIGN_UP(WIDTH, 16)
    sensor.set_framesize(width=width, height=HEIGHT, alignment=12, chn=CAM_CHN_ID_0)
    sensor.set_pixformat(Sensor.YUV420SP, chn=CAM_CHN_ID_0)
    print("[1/4] camera init done (%dx%d, tier %s)" % (width, HEIGHT, TIER))


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


def ensure_wifi(sta, retry_ms=WIFI_RECONNECT_BUDGET_MS):
    """K13：旧版最长阻塞主循环 10 s。现在只做一个短轮询窗口就交还控制权，
    剩下的重连由主循环 `continue` 反复重试（视频/控制只是停，不会死等）。"""
    if sta.isconnected():
        return True
    print("WiFi lost, reconnecting...")
    try:
        sta.connect(WIFI_SSID, WIFI_PASSWORD)
    except Exception as e:
        print("reconnect err: " + str(e))
        return False
    waited = 0
    while waited < retry_ms:
        if sta.isconnected():
            print("WiFi back")
            return True
        time.sleep(1)
        waited += 1000
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
    print("[3/4] encoder started (chn=%d, %dx%d, %s, %dKbps, tier %s, gop %d)" % (
        encoder.chn, width, HEIGHT, profile_name, BIT_RATE, TIER, GOP_LEN))


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


RTP_CLOCK_HZ = 90000
RTP_TICKS_PER_MS = RTP_CLOCK_HZ // 1000   # 90 kHz 时钟：1 ms = 90 tick


def rtp_timestamp(ticks_ms):
    """K12：RTP 时间戳 = 采集时钟（ms）× 90，回绕掩码到 32 位（纯函数）。

    旧实现是 frame_count × 3000：丢帧时计数不推进 -> 时间轴被压缩，
    采集到显示的真实延迟无法测量。
    """
    return (int(ticks_ms) * RTP_TICKS_PER_MS) & 0xFFFFFFFF


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

# sendto 遇到「TX 队列瞬时满」时的退避重试（见 fanout_sink）。
# posix/lwip 的 EAGAIN/EWOULDBLOCK 及各平台取值；errno 拿不到时退化为旧行为。
_EAGAIN_ERRNOS = (11, 115, 10035, 35)
SEND_RETRY_TRIES = 3
SEND_RETRY_GAP_US = 500


def _sleep_short_us(us):
    """亚毫秒退避：有 sleep_us 用 sleep_us，否则退化到 time.sleep。"""
    if hasattr(time, "sleep_us"):
        time.sleep_us(int(us))
    else:
        time.sleep(us / 1000000.0)


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

    K5/K6 突发模式：整帧线速字节 ≤ burst_threshold（默认 20 KB ≈ 15 包）
    时只扣令牌、不 sleep。P 帧 ≤ 20 KB 在 5-10 Mbps 上空口只要 2-3 ms，
    逐包 sleep_us 在 rt-smart 上既慢又不确定（文档 §4.2）。
    突发债务最多到 -burst（即令牌桶本身的突发容差，约 2 个帧间隔的字节量）；
    越过就必须回到等待循环，所以长期平均速率仍受 PACER_WIRE_KBPS 约束。
    """

    def __init__(self, rate_kbps, burst_bytes, clock, span_max_ms,
                 burst_threshold=BURST_SEND_BYTES, idle=None):
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
        # K4：节流 sleep 期间服务控制 socket（控制指令 + NACK）。
        self.idle = idle
        # K5：突发阈值与本帧是否处于突发模式。
        self.burst_threshold = burst_threshold
        self.bursting = False
        self.burst_packets = 0

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
        """每帧调一次：重置单帧节流时长预算与突发标志。"""
        self.span_left = self.span_max_us
        self.bursting = False

    def set_burst(self, frame_wire_bytes):
        """按整帧线速字节决定本帧走突发还是逐包 sleep；返回是否突发。"""
        self.bursting = frame_wire_bytes <= self.burst_threshold
        return self.bursting

    def burst_ok(self, need):
        """突发模式能否立刻放行：债务不得越过 -burst。

        -burst 就是令牌桶本身的突发容差（约 2 个帧间隔的字节量）。越过它
        就必须回到等待循环 —— 否则持续超速的发送方可以无限透支，长期平均
        速率就不再受 PACER_WIRE_KBPS 约束。
        """
        return self.tokens - need >= -self.burst

    def can_send(self, frame_bytes, npkts):
        """非阻塞预检：整帧的线速字节是否拿得到。用于 P 帧的丢弃决策。"""
        need = frame_bytes + npkts * PacerWireOverhead
        tokens = self.refill()
        if tokens >= need:
            return True
        return self.would_burst(need) and self.burst_ok(need)

    def would_burst(self, need):
        return need <= self.burst_threshold

    def _idle(self):
        if self.idle:
            self.idle()

    def take(self, nbytes):
        """为 nbytes payload 取用线速时间，返回本次等待 us。"""
        need = self.wire_bytes_for(nbytes)
        t0 = self.clock.now_us()
        self.packets += 1
        self.wire_bytes += need
        gap = self.clock.diff_us(t0, self.last)
        if gap > 0 and (self.min_gap_us is None or gap < self.min_gap_us):
            self.min_gap_us = gap
        waited = 0
        # K5：整帧线速字节 ≤ burst_threshold（P 帧 ≤20KB）时直接突发：
        # 不逐包 sleep_us。债务最多到 -burst，超出才回到等待循环。
        if self.bursting:
            self.refill()
            if self.burst_ok(need):
                self.tokens -= need
                self.burst_packets += 1
                return 0
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
            self._idle()
            self.clock.sleep_us(wait_us)
            self._idle()
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


def starve_hold_should_request_idr(elapsed_ms, hold_max_ms=STARVE_HOLD_MAX_MS):
    """K7 的安全阀（纯函数）：hold 超过上限就重新允许一次 IDR 请求。

    「丢到下一个 IDR 为止」在 GOP=60（2 s）下最坏会黑屏 2 s，比原来那个
    正反馈还糟。所以 hold 不是无界的：超过 STARVE_HOLD_MAX_MS 就走一次
    IdrGate（它自带 400ms 限频 + 近期 IDR 合并），把冻结上界压到 ~0.5 s。
    """
    return elapsed_ms > hold_max_ms


def admit_frame_by_clock(now_ms, last_sent_ms, frame_period_ms=FRAME_PERIOD_MS):
    """K2：按到达时钟的丢帧闸门**已删除**，这里恒返回 FRAME_SEND（纯函数）。

    旧主循环里有：
        if ticks_diff(now_ms, last_sent_ms) < FRAME_PERIOD_MS:
            st.overfps += 1; encoder.ReleaseStream(stream); continue
    编码器自己已按 dst_frame_rate 出帧，到达间隔天然有正负几毫秒抖动，
    间隔 < 33ms 的**已编码帧**会被静默丢弃：P 帧丢失 = 参考链断裂，而该分支
    既不置 need_resync 也不消耗 seq，接收端看不到缺口也就不会请求 IDR，
    画面带着错误一直持续到下一个周期 IDR。

    参数保留只为让单测能钉住这个不变式；**运行期已不再调用它**（main loop 里
    连调用都删掉了，免得把「已删除的闸门」留成一个看起来还能用的钩子）。
    """
    return FRAME_SEND


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
    """多端口扇出 sink：单包发往所有目标端口；任一 OSError 即中止本帧。

    「TX 队列瞬时满」（EAGAIN）会短退避重试而不是直接丢帧：丢帧会让接收端
    看到序号空洞 -> 40 ms 后宣布丢失 -> 请求 IDR，正是本轮改造要消灭的反馈环。
    只有非 EAGAIN 的 OSError（网卡消失、路由不可达）才中止本帧。
    """
    n = len(targets)

    def _send(pkt):
        attempts = 0
        while True:
            attempts += 1
            try:
                for t in targets:
                    sock.sendto(pkt, t)
            except TypeError:
                # 某些固件的 sendto 不接受 memoryview/bytearray
                if attempts >= SEND_RETRY_TRIES:
                    stats.pkt_err += 1
                    raise SendAborted("sendto rejected packet type")
                pkt = bytes(pkt)
                continue
            except OSError as e:
                if (attempts >= SEND_RETRY_TRIES
                        or getattr(e, "errno", None) not in _EAGAIN_ERRNOS):
                    stats.pkt_err += 1
                    raise SendAborted(str(e))
                _sleep_short_us(SEND_RETRY_GAP_US)
                continue
            stats.bytes_tx += len(pkt) * n
            return
    return _send


def send_paced(pacer, packets, sink, copies=1, ring=None, first_seq=0):
    """逐包节流发送，返回实际发出的包数。

    令牌按 copies×包长计费：多链拷贝共用同一个节流池，线速预算不超发。
    K5：先按整帧线速字节决定突发/节流（P 帧 ≤20KB 直接突发，不 sleep）。
    ring 不为 None 时，发包的同时把 (seq, pkt) 引用存进环形缓冲供 NACK 重传
    （必须在发包时写，不是收到 NACK 时才找）。
    """
    sent = 0
    pacer.begin_frame()
    total = 0
    for pkt in packets:
        total += len(pkt) * copies
    setter = getattr(pacer, "set_burst", None)
    if setter is not None:
        setter(total)   # 旧替身（无 set_burst）退化为默认逐包节流
    for i in range(len(packets)):
        pkt = packets[i]
        pacer.take(len(pkt) * copies)
        sink(pkt)
        sent += 1
        if ring is not None:
            ring.put((first_seq + i) & 0xFFFF, pkt)
    return sent


class PacketRing:
    """重传环形缓冲：固定容量，只存 (seq, pkt_bytes) **引用**，不拷贝。

    packetize_nalu 产出的 bytes/bytearray 发出后不再被修改，所以直接持引用
    是安全的；128 槽在 T3(6 Mbps) 下约覆盖 200ms，足够 NACK 往返。
    命中判定：slot = seq & mask，且槽里记的 seq 必须完全相等（环绕覆盖后
    旧 seq 会被新 seq 顶掉，从而正确判为未命中）。
    """

    def __init__(self, capacity=128):
        cap = int(capacity)
        if cap < 2 or (cap & (cap - 1)) != 0:
            raise ValueError("PacketRing capacity must be a power of two")
        self.capacity = cap
        self.mask = cap - 1
        self.slots = [None] * cap
        self.puts = 0

    def put(self, seq, pkt):
        seq = seq & 0xFFFF
        self.slots[seq & self.mask] = (seq, pkt)
        self.puts += 1

    def get(self, seq):
        entry = self.slots[seq & self.mask]
        if entry is not None and entry[0] == (seq & 0xFFFF):
            return entry[1]
        return None

    def __len__(self):
        return self.capacity


def retransmit_limit_bytes(bit_rate_kbps, ratio=RETRANS_BUDGET_RATIO,
                           window_ms=RETRANS_WINDOW_MS):
    """重传预算（纯函数）：窗口内允许重传的字节数 = 码率 × ratio × 窗口。

    例：T1 3000 kbps、15%、1s -> int(3000*1000/8*0.15) = 56250 B/s。
    """
    return int(bit_rate_kbps * 1000.0 / 8.0 * ratio * window_ms / 1000.0)


class RetransmitLimiter:
    """重传字节滑窗（纯逻辑，无时钟依赖，now_ms 由调用方给）。

    超预算就整轮丢弃本次 NACK 的重传 —— 拥塞时重传只会加重拥塞。
    """

    def __init__(self, limit_bytes, window_ms=RETRANS_WINDOW_MS):
        self.limit = int(limit_bytes)
        self.window_ms = window_ms
        self.used = 0
        self.window_start_ms = None
        self.sent_bytes = 0
        self.blocked = 0

    def roll(self, now_ms):
        d = ms_diff(now_ms, self.window_start_ms)
        if d is None or d >= self.window_ms:
            self.window_start_ms = now_ms
            self.used = 0

    def allow(self, now_ms, nbytes):
        self.roll(now_ms)
        if self.used + nbytes > self.limit:
            self.blocked += 1
            return False
        self.used += nbytes
        self.sent_bytes += nbytes
        return True


def retransmit_nack(seqs, ring, limiter, sock, dst, now_ms, stats=None,
                    link=None):
    """处理一包 NACK 的 seq 列表（阶段 2）。

    命中环形缓冲就用**同一 socket、同一目的地**（RTP_PORTS[0]）重发原始
    字节 —— 不重新打包、不改 seq/ts，所以接收端的重排逻辑完全不用改。
    未命中 = 已被环绕覆盖或从未发出；超重传预算 = 直接放弃本轮（拥塞时
    重传只会加重拥塞）。返回 (hits, misses, budget_dropped, send_errors)。
    """
    hits = 0
    misses = 0
    dropped = 0
    errors = 0
    for sq in seqs:
        pkt = ring.get(sq)
        if pkt is None:
            misses += 1
            continue
        if not limiter.allow(now_ms, len(pkt)):
            dropped += 1
            continue
        try:
            sock.sendto(pkt, dst)
        except OSError:
            errors += 1
            continue
        hits += 1
        if stats is not None:
            stats.retx_bytes += len(pkt)
    if link is not None:
        link.nack_hits += hits
        link.nack_misses += misses
        link.nack_budget += dropped
    if stats is not None and errors:
        stats.pkt_err += errors
    return (hits, misses, dropped, errors)


class TxStats:
    """发送侧诊断统计（只读，不影响任何行为）。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.reset_rolling()
        # 单调帧计数，**不**随统计窗口清零：GC 触发不能依赖 sent_frames，
        # 否则一旦有效帧率掉到 10 fps 以下（正是 STARVE 丢帧时会发生的事），
        # 每个 10 s 窗口都到不了 GC_COLLECT_EVERY，gc.collect() 永久停摆，
        # 堆只增不减 -> MemoryError。
        self.gc_frames = 0
        self.last_len = 0
        self.last_iframe_pkts = 0
        self.last_iframe_ms = 0.0
        self.last_pframe_ms = 0.0
        # K11：只观测不改策略。真机读到数字再决定是否调 GC 频率。
        self.gc_last_us = 0
        self.gc_max_us = 0
        self.gc_count = 0

    def note_gc(self, us):
        self.gc_count += 1
        self.gc_window_us += us
        self.gc_last_us = us
        if us > self.gc_max_us:
            self.gc_max_us = us

    def note_frame(self, frame_bytes, npkts, is_idr, air_ms):
        self.sent_frames += 1
        self.gc_frames += 1
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
        self.starve_hold = 0
        # K2：闸门已删，overfps 恒为 0；字段与统计位保留，避免破坏别处引用。
        self.overfps = 0
        self.idr_frames = 0
        self.idr_bytes = 0
        self.bytes_tx = 0
        self.retx_bytes = 0
        self.pkt_err = 0
        self.forced_idr = 0
        self.resyncs = 0
        self.max_frame_bytes = 0
        self.fanout_copies = 0
        self.gc_window_us = 0
        self.gc_window_n = 0

    def line(self, span, pacer, gate, free):
        kbps = self.bytes_tx * 8 / span / 1000.0
        fps = self.sent_frames / span
        return (
            "stat: sent=%d drop=%d(duty=%d starve=%d hold=%d) overfps=%d idr=%d "
            "(forced=%d resync=%d) last=%dB max=%dB kbps=%.0f fps=%.1f pps=%.0f "
            "txerr=%d retx=%.1fkbps idrshare=%.0f%% gc=%.1fms/%d free=%d" % (
                self.sent_frames, self.dropped, self.over_duty, self.starved,
                self.starve_hold, self.overfps, self.idr_frames,
                self.forced_idr, self.resyncs,
                self.last_len, self.max_frame_bytes, kbps, fps,
                pacer.packets / span, self.pkt_err,
                self.retx_bytes * 8 / span / 1000.0, self.idr_share(),
                self.gc_window_us / 1000.0, self.gc_window_n, free))

    def detail(self, pacer, gate):
        return (
            "      pacer: wait=%.0fms sleep=%d cap=%d maxwait=%.1fms burst=%dpkt "
            "minkgap=%s | I帧 %d包 %.0fms | IDR请求 rx=%d 生效=%d "
            "(合并:近期IDR=%d 限频=%d)" % (
                pacer.wait_us / 1000.0, pacer.sleeps, pacer.cap_events,
                pacer.max_wait_us / 1000.0, pacer.burst_packets,
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


def parse_nack(data):
    """纯函数：解析一包 NACK -> (seq 列表, ok)。

    线路格式（服务端中继原样上行，桌面端按此发送）：
        b"C" + b"B" + b"R" + b"\\x03" + count(u8) + seq(u16 BE) × count
    只接受 count ∈ [1,32] 且**实际字节数正好** 5 + count*2 的包；
    越界、截断、count 与字节数不符一律整包丢弃（返回 (None, False)）。
    多包 NACK 由接收端分片，这里只处理单包。
    """
    if data is None or len(data) < 5:
        return None, False
    if bytes(data[:3]) != CONTROL_MAGIC or data[3] != CTRL_NACK:
        return None, False
    count = data[4]
    if count < 1 or count > NACK_MAX_SEQS:
        return None, False
    if len(data) != 5 + count * 2:
        return None, False
    seqs = []
    off = 5
    for _ in range(count):
        seqs.append(((data[off] << 8) | data[off + 1]) & 0xFFFF)
        off += 2
    return seqs, True


def build_nack(seqs):
    """纯函数：造一包 NACK（服务端/桌面端联调用）。只取前 NACK_MAX_SEQS 个。"""
    picked = [s & 0xFFFF for s in seqs[:NACK_MAX_SEQS]]
    if not picked:
        return None
    out = bytearray(CONTROL_MAGIC)
    out.append(CTRL_NACK)
    out.append(len(picked))
    for s in picked:
        out.append((s >> 8) & 0xFF)
        out.append(s & 0xFF)
    return bytes(out)


class CommandLink:
    """反向控制：排空 socket 缓冲；AA55 UART 帧写 UART3，CBR 控制包回调。

    控制包与 AA55 命令帧共用同一个 socket（服务端下行统一走 RTP 端口）。
    CBR 已实现两种：0x01 IDR 请求、0x03 NACK（重传请求）。
    """

    # 最大一包 NACK = 3 + 1 + 1 + 32*2 = 69 B；64 会被截断导致整包丢弃。
    RECV_BYTES = 128
    # 排空接收缓冲时用 settimeout(0)（非阻塞），**排空后必须还原**。
    # MicroPython 的 settimeout(0) 会把 socket 切成非阻塞（lwip_setblocking(0)）
    # 而后续 settimeout(正数) 是否恢复该标志依固件而异；控制包与视频共用同一个
    # socket（K4 之后 pump 每秒跑上百次），一旦留在非阻塞模式，视频的 sendto
    # 在 WiFi TX 队列满时会抛 EAGAIN -> SendAborted -> 整帧中止，但 seq 照样
    # 推进 -> 接收端看到序号空洞 -> 40 ms 后宣布丢失 -> 请求 IDR。也就是
    # 「自己制造丢包」。还原成带超时的阻塞模式，同时也让重传的 sendto 不再非阻塞。
    #
    # 注意这个值实际只约束 sendto（pump 里 recv 一律先 settimeout(0)，不受它影响）。
    # 它是一个「晚发 vs 丢帧」的权衡：设小了 -> TX 队列一满就 EAGAIN -> 丢帧 ->
    # 接收端 40ms 后宣布丢失 + 请求 IDR；设大了 -> 单个包阻塞很久。
    # 5ms 的理由：够穿过 lwIP pbuf 池的瞬时耗尽，又把「一帧 18 个包全部阻塞」的
    # 最坏情况压在 90ms 以内。**这是最需要真机标定的常数**（见报告的 P0-1）。
    BACK_TIMEOUT_S = 0.005

    def __init__(self, uart, on_ctrl=None, on_nack=None):
        self.uart = uart
        self.ok = False
        self.rx = 0
        self.ctrl_rx = 0
        self._last_rx = 0
        self._on_ctrl = on_ctrl
        self._on_nack = on_nack
        self.nack_rx = 0        # 收到且解析成功的 NACK 包数
        self.nack_seqs = 0      # 这些包里的 seq 总数
        self.nack_hits = 0      # 环形缓冲命中并重发成功
        self.nack_misses = 0    # 环形缓冲未命中（已被覆盖/从未发出）
        self.nack_budget = 0    # 因 15% 重传预算被拦
        self.nack_bad = 0       # 格式错/截断/越界，整包丢弃

    def _restore_blocking(self, sock):
        """排空后把 socket 还原成带超时的阻塞模式（见 BACK_TIMEOUT_S）。"""
        try:
            sock.settimeout(self.BACK_TIMEOUT_S)
        except Exception:
            pass

    def pump(self, sock):
        now = _ticks_ms()
        latest = None
        nacks = []
        try:
            sock.settimeout(0)
            while True:
                data = sock.recv(self.RECV_BYTES)
                if not data:
                    break
                if len(data) >= 4 and bytes(data[:3]) == CONTROL_MAGIC:
                    if data[3] == CTRL_IDR:
                        if self._on_ctrl:
                            self._on_ctrl(b"IDR")
                        self.ctrl_rx += 1
                        continue
                    if data[3] == CTRL_NACK:
                        seqs, ok = parse_nack(data)
                        if not ok:
                            self.nack_bad += 1
                            continue
                        self.nack_rx += 1
                        self.nack_seqs += len(seqs)
                        # 排队，等 socket 还原成阻塞模式后再重发（重传要走
                        # sendto，非阻塞下会 EAGAIN 直接丢掉这次重传）。
                        nacks.append(seqs)
                        continue
                if valid_frame(data):
                    latest = data
        except OSError:
            pass
        finally:
            self._restore_blocking(sock)
        if nacks and self._on_nack:
            for seqs in nacks:
                try:
                    self._on_nack(seqs)
                except Exception as e:
                    self.nack_bad += 1
                    print("[CMD] NACK handling error: %s" % str(e))
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
        return ("[CMD] rx=%d ok=%s nack rx=%d seq=%d hit=%d miss=%d "
                "budget=%d bad=%d" % (
                    self.rx, self.ok, self.nack_rx, self.nack_seqs,
                    self.nack_hits, self.nack_misses, self.nack_budget,
                    self.nack_bad))


def stream_loop(sock, server_ip, sta):
    stream = StreamData()
    parameter_sets = None
    gate = IdrGate(IDR_REQ_MIN_INTERVAL_MS, IDR_REQ_SETTLE_MS)
    st = TxStats()
    targets = [(server_ip, p) for p in RTP_PORTS]
    if not targets:
        raise RuntimeError("no RTP target port")
    ssrc = 0x54494E44
    seq = 0
    t_stat = time.time()
    pending_idr = [True]
    # 丢过 P 帧后置位：参考链已断，而丢帧不消耗 seq，接收端看不到缺口，
    # 不会自行请求 IDR。下一帧成功发出后据此补一个 IDR 请求重新起链。
    need_resync = False
    # K7：STARVE 丢弃后「丢到下一个 IDR 为止」。不发 IDR 请求 ——
    # 丢帧 → IDR → 更拥塞 → 再丢帧 的正反馈就是这么形成的。
    # 可恢复的丢包交给 NACK 在接收端重排消化；只有解码器真正报错
    # （接收端主动请求 IDR）才走 IDR 路径。
    # 逃生阀：hold 期间若接收端发来 CBR 0x01，pending_idr 会触发一次
    # RequestIDR，编码器给出的 IDR 同样会清掉 hold。
    starve_pending = False
    starve_since_ms = 0
    # 阶段 2：重传环形缓冲 + 15% 重传预算滑窗。
    ring = PacketRing()
    retx = RetransmitLimiter(retransmit_limit_bytes(BIT_RATE))
    retx_dst = targets[0]

    def _on_ctrl(_data):
        pending_idr[0] = True

    def _on_nack(seqs):
        # K4 因果：NACK 由 Pacer.idle 在节流 sleep 前后调用 pump 服务，
        # 所以大帧发送期间不会被饿死。
        retransmit_nack(seqs, ring, retx, sock, retx_dst, _ticks_ms(),
                        stats=st, link=cmdline)

    cmdline = CommandLink(init_uart3(), on_ctrl=_on_ctrl, on_nack=_on_nack)
    # K4：节流 sleep 期间持续泵控制 socket（AA55 命令帧 + IDR + NACK）。
    # 放在 pump 建好之后再构造 Pacer，因为 idle 要闭包引用它。
    pacer = Pacer(PACER_WIRE_KBPS, PACER_BURST_BYTES, get_clock(),
                  PACER_SPAN_MAX_MS,
                  idle=lambda: cmdline.pump(sock))

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

        # K4：超时缩到 4ms（原 20ms）。取不到帧的这段时间不是「空闲」，
        # 而是最该用来泵控制 socket 的时间，否则控制延迟被编码器阻塞。
        if encoder.GetStream(stream, timeout=4) != 0:
            cmdline.pump(sock)
            continue

        # K2：按时钟丢帧的闸门**整段删除**（编码器自己按 dst_frame_rate 出帧）。
        # 这里刻意不再调用 admit_frame_by_clock()：那个函数恒返回 FRAME_SEND，
        # 留着调用等于把「已删除的闸门」包装成一个看起来还能用的钩子，只会
        # 诱使人把丢帧逻辑再加回去。它降级成纯函数 + 单测钉住不变式。
        now_ms = _ticks_ms()

        try:
            packets = []
            frame_bytes = 0
            is_idr = False
            seq_next = seq
            ts = rtp_timestamp(now_ms)     # K12：时间戳来自采集时钟，不来自帧计数
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
                        seq_next = packetize_nalu(nalu, ts, seq_next, ssrc, packets)
                for nalu in split_nalus(data_bytes):
                    seq_next = packetize_nalu(nalu, ts, seq_next, ssrc, packets)
            if not packets:
                continue
            packets[-1] = bytearray(packets[-1])
            packets[-1][1] |= 0x80

            # K7：hold 期间只放 IDR 通过，P 帧一律丢（不消耗 seq，
            # 因此不制造接收端可见的缺口）。冻结上界由
            # starve_hold_should_request_idr() 兜在 STARVE_HOLD_MAX_MS，
            # 不会真的等到 GOP 末尾。
            if starve_pending and not is_idr:
                st.dropped += 1
                st.starved += 1
                st.starve_hold += 1
                if starve_hold_should_request_idr(
                        _ticks_diff(now_ms, starve_since_ms)):
                    # 冻结上界：hold 太久就重新要一次 IDR（IdrGate 会限频）。
                    pending_idr[0] = True
                    starve_since_ms = now_ms
                continue

            action, copies, air_ms = frame_admission(
                frame_bytes, len(packets), is_idr, WIFI_LINK_KBPS,
                FRAME_INTERVAL_MS, MAX_PFRAME_DUTY, pacer,
                max_copies=LINK_COPIES, idr_copies=IDR_COPIES)
            if action == FRAME_DROP_DUTY:
                st.dropped += 1
                st.over_duty += 1
                # DUTY 是「有明确 airtime 超预算」的丢弃，请求 IDR 仍然合理。
                need_resync = True
                continue
            if action == FRAME_DROP_STARVE:
                st.dropped += 1
                st.starved += 1
                # K7：不再置 need_resync。改为丢到下一个 IDR 为止。
                starve_pending = True
                starve_since_ms = now_ms
                continue
            if is_idr:
                gate.note_idr_sent(_ticks_ms())
                need_resync = False
                starve_pending = False
                starve_since_ms = 0

            _sink = fanout_sink(targets[:copies], sock, st)

            try:
                send_paced(pacer, packets, _sink, copies, ring=ring,
                           first_seq=seq)
                st.note_frame(frame_bytes * copies, len(packets), is_idr,
                              air_ms)
                if copies > 1:
                    st.fanout_copies += copies - 1
            except SendAborted as e:
                st.pkt_err += 1
                print("sendto err: %s" % str(e))
            seq = seq_next
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

        if (st.gc_frames and (st.gc_frames % GC_COLLECT_EVERY) == 0):
            # K11：先观测再决策。真机读 gc= 的 max 再决定是否改 GC 策略；
            # 这里不改变任何行为。
            _gc0 = _ticks_us()
            gc.collect()
            _gc1 = _ticks_us()
            st.note_gc(_ticks_diff_us(_gc1, _gc0))
            st.gc_window_n += 1

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
                      "  << 接近链路上限（单链路已无拷贝可降，只能降档位）"
                      if util > 0.9 else ""))
            if st.starve_hold:
                print("      !! K7 hold 期间又丢了 %d 个 P 帧（等 IDR 中）；"
                      "若持续出现说明档位高于实测带宽，应降 tier 或查 WiFi" % (
                          st.starve_hold,))
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
            if cmdline.nack_rx or cmdline.nack_misses:
                hit = cmdline.nack_hits
                tot = hit + cmdline.nack_misses
                print("      NACK %d 包/%d seq -> 命中 %d(%.0f%%) 未命中 %d "
                      "预算拦 %d 坏包 %d | 重传预算 %dB/%dms" % (
                          cmdline.nack_rx, cmdline.nack_seqs, hit,
                          100.0 * hit / tot if tot else 0.0,
                          cmdline.nack_misses, cmdline.nack_budget,
                          cmdline.nack_bad, retx.limit, retx.window_ms))
            t_stat = now
            st.reset_rolling()
            gate.reset_rolling()
            pacer.wait_us = 0
            pacer.sleeps = 0
            pacer.packets = 0
            pacer.burst_packets = 0
            cmdline.nack_rx = 0
            cmdline.nack_seqs = 0
            cmdline.nack_hits = 0
            cmdline.nack_misses = 0
            cmdline.nack_budget = 0
            cmdline.nack_bad = 0


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
    # 凭据/tier 默认值在代码里；配置文件可选。文件存在但字段写错了要报出来
    # （静默用默认值会把配置问题藏到连不上 WiFi 才暴露）。
    if CONFIG_ERROR:
        print("[cfg] ERROR: " + CONFIG_ERROR)
        print("[cfg] 已回落到内置默认值；请修正 %s，例如："
              % CONFIG_PATH)
        print('[cfg] {"ssid":"...","password":"...","server_ip":"192.168.1.50",'
              '"tier":"%s","link_kbps":%d}' % (DEFAULT_TIER,
                                              DEFAULT_WIFI_LINK_KBPS))
    elif CONFIG is None:
        print("[cfg] %s 不存在，使用内置默认值（SSID=%s，接收端 IP 需手动设置）"
              % (CONFIG_PATH, WIFI_SSID))
    for w in SETTINGS["warnings"]:
        print("[cfg] WARN: " + w)
    # 接收端 IP 必须手动设置。旧版这里没配就退回 WiFi 网关地址，于是「推流
    # 正常启动」但对端可能根本不是收流的那台机器 —— 表现是纯黑屏，排查全靠
    # 猜。现在没有默认地址：没配就在碰摄像头/WiFi 之前直接退出。
    server_ip = SERVER_IP
    if not server_ip:
        print("[cfg] ERROR: 未设置接收端 IP（server_ip），已停止，不推送。")
        print("[cfg] 请手动设置以下任意一处：")
        print('[cfg]   1) 本文件顶部 SERVER_IP = "192.168.x.x"')
        print('[cfg]   2) %s 里的 "server_ip": "192.168.x.x"' % CONFIG_PATH)
        print("[cfg]   3) 环境变量 SERVER_IP=192.168.x.x")
        raise ConfigError("server_ip not set")
    print("[cfg] tier=%s %dx%d %dkbps | link=%dkbps (pacer %dkbps) | target %s | %s" % (
        TIER, WIDTH, HEIGHT, BIT_RATE, WIFI_LINK_KBPS, PACER_WIRE_KBPS,
        server_ip, CONFIG_PATH))
    init_camera()
    connect_wifi()
    init_encoder()
    sock = make_sock()
    print("[4/4] pushing RTP h264 to %s ports=%s (tier=%s %dKBps enc, "
          "%dKBps link, copies=%d, gop=%d, mtu=%d)" % (
              server_ip, "/".join(str(p) for p in RTP_PORTS), TIER,
              BIT_RATE * 1000 // 8, WIFI_LINK_KBPS, LINK_COPIES, GOP_LEN,
              MAX_PAYLOAD))
    stream_loop(sock, server_ip, get_sta())


try:
    main()
except KeyboardInterrupt as e:
    print("User stop: " + str(e))
except ConfigError as e:
    # 上面已经把「怎么改」打全了，这里只补一句结论，**不再打调用栈**：
    # 配置没写对不是 bug，打 traceback 会让人以为脚本炸了。
    print("[cfg] 未推流（%s）。按上面任一方式设置后重新运行即可。" % e)
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
