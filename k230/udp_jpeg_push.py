# 立创·庐山派-K230 图传 + 手柄命令桥（单脚本双任务）
# 1) UDP+JPEG 图传推流到桌面端 SERVER_UDP_PORT
# 2) _thread 独立线程收桌面端 UDP 命令推送（CMD_UDP_PORT），纯转发 UART3
#    UART3_TXD -> GPIO32, UART3_RXD -> GPIO33
#    每 0.5s 发注册包 b"CQ"+hz，服务端按 hz（默认 150Hz）推送 UART 帧
#    （不走 TCP：K230 固件 TCP connect 慢且会 socket 泄漏 errno 12）
#
# UART 命令帧 v2（二进制变长，总长<=32 字节）：
#   [0]=0xAA [1]=0x55 [2]=N（条目数，1..8）
#   { [id][len][payload...] }×N
#   [末字节]=crc8（从[0]到 payload 末所有字节异或）
#   0x01 MOVE，len=2：payload = speed i8, turn i8（左摇杆 前+/后-，左-/右+）
#   0x02 TURRET，len=2：payload = yaw i8, pitch i8（右摇杆 左-/右+，下-/上+）
# 无激活命令时服务端整周期不发送任何字节；K230 只做纯转发，
# 无动作时串口零输出（失联也不补零速帧，仅翻 ok=False 状态）。
import gc
import socket
import struct
import time

import network

try:
    from media.sensor import *
except ImportError:
    pass

# ==================== 配置区 ====================
WIFI_SSID = "TF-Laptop"
WIFI_PASSWORD = "TF@HTR.Hello"
SERVER_IP = None            # None = 用网关地址（连热点时网关就是电脑）
SERVER_UDP_PORT = 8001      # 图传
CMD_UDP_PORT = 8002         # 命令推送
CMD_POLL_HZ = 150           # 期望命令推送频率，>=120Hz
CMD_REG_INTERVAL_MS = 500   # 注册包间隔
CMD_RX_TIMEOUT_MS = 100     # recv 超时
CMD_STALE_MS = 200          # 超过此时长没收到帧视为失联
UART3_BAUD = 115200
SENSOR_ID = 2
WIDTH = 640                 # 稳定优先：640x480；要清晰再试 800x600 / 1280x720
HEIGHT = 480
JPEG_QUALITY = 65           # 50~75 之间最稳
FPS = 12                    # WiFi 下 10~15 最稳
CHUNK_SIZE = 1100           # payload 上限（<=1200，留余量）
CHUNK_GAP_MS = 2            # 片间 pacing，WiFi 稳定关键
GC_COLLECT_EVERY = 100      # 每 N 帧强制一次 gc（100 帧 ≈ 8s @ 12fps）
# ================================================

_MAGIC = 0x4A50
_VER = 1
_HEADER = ">HBBHHHH"
_HEADER_SIZE = 12

sensor = None
_sta = None
poller = None  # CommandPoller，main() 内赋值

# 预分配发送缓冲：所有分片复用同一块内存，避免每片新分配 bytes 造成堆碎片
_SEND_BUF = bytearray(_HEADER_SIZE + CHUNK_SIZE)
_SEND_MV = memoryview(_SEND_BUF)


def init_camera():
    global sensor
    sensor = Sensor(id=SENSOR_ID)
    sensor.reset()
    try:
        sensor.set_framesize(width=WIDTH, height=HEIGHT, chn=CAM_CHN_ID_0)
    except TypeError:
        sensor.set_framesize(width=WIDTH, height=HEIGHT)
    try:
        sensor.set_pixformat(Sensor.RGB888, chn=CAM_CHN_ID_0)
    except TypeError:
        sensor.set_pixformat(Sensor.RGB888)
    except Exception as e:
        print("pixformat warn: " + str(e))
    sensor.run()
    print("[1/3] camera init done (%dx%d)" % (WIDTH, HEIGHT))


def get_sta():
    """WLAN 单例，避免多次 network.WLAN 造成重复分配。"""
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
    print("[2/3] connecting to " + WIFI_SSID)
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


def _compress_image(img):
    """在已抓取的 image 对象上尝试各种 JPEG 压缩 API。失败返回 None。"""
    for name in ("compress", "to_jpeg", "to_jpeg_bytes", "jpeg_encode"):
        fn = getattr(img, name, None)
        if fn is None:
            continue
        try:
            try:
                data = fn(JPEG_QUALITY)
            except TypeError:
                data = fn(quality=JPEG_QUALITY)
            if data is not None and len(data) > 4:
                # 已经是 bytes 就不再拷贝
                return data if isinstance(data, bytes) else bytes(data)
        except Exception as e:
            print("capture via %s failed: %s" % (name, str(e)))
            continue
    return None


def capture_jpeg():
    """返回 JPEG bytes；兼容不同固件的 compress/to_jpeg 命名。"""
    # 主路径：抓帧 + image.compress()
    img = sensor.snapshot()
    try:
        data = _compress_image(img)
        if data is not None:
            return data
    finally:
        # 显式断开帧缓冲引用，帮助 GC 尽早回收（K230 帧缓冲走 MMZ，回收越早越好）
        del img

    # 回退：有些固件 snapshot(compress=True) 直接返回 JPEG
    try:
        data = sensor.snapshot(compress=True, quality=JPEG_QUALITY)
    except TypeError:
        data = sensor.snapshot(compress=True)
    if data is not None and len(data) > 4:
        return data if isinstance(data, bytes) else bytes(data)
    raise RuntimeError("no JPEG API")


def make_sock():
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


# ==================== UART3 手柄命令桥 ====================
_uart = None


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


def crc8(payload):
    c = 0
    for b in payload:
        c ^= b
    return c & 0xFF


def valid_frame(data):
    """UART 命令帧 v2 通用校验：长度>=4、AA 55 头、1<=N<=8、按 [id][len]
    逐条目 walk 长度自洽、总长<=32、crc8（从[0]到 payload 末异或）正确。
    不假设具体 id，只校验结构。"""
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
        if off + 2 > len(data) - 1:  # 至少 [id][len]，且留 1 字节 crc
            return False
        ln = data[off + 1]
        off += 2 + ln
        if off > len(data) - 1:  # payload 不得吞掉 crc 字节
            return False
    if off != len(data) - 1:  # 长度必须自洽：条目末 == crc 位置
        return False
    c = 0
    for b in data[:off]:
        c ^= b
    return (c & 0xFF) == data[off]


def _ticks_ms():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _ticks_diff(a, b):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(a, b)
    return a - b


class CommandPoller:
    """UDP 收命令推送 -> UART3 纯转发（v2 变长帧）。

    每 CMD_REG_INTERVAL_MS 发注册包 b"CQ"+hz，服务端按 hz 定频推送；
    收到合法 v2 帧即原样写 UART；超过 CMD_STALE_MS 没收到帧视为失联，
    只翻 ok=False 并打印，不写 UART（无动作时串口零输出）。
    全部 UART 访问在本线程内，无并发问题。
    """

    def __init__(self, server_ip, hz):
        self.addr = (server_ip, CMD_UDP_PORT)
        self.hz = hz
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(CMD_RX_TIMEOUT_MS / 1000.0)
        self.ok = False
        self.rx = 0
        self._last_reg = 0
        self._last_rx = 0

    def _register(self):
        try:
            self.sock.sendto(bytes([0x43, 0x51, self.hz & 0xFF]), self.addr)  # "CQ"
        except Exception:
            pass

    def poll_once(self):
        now = _ticks_ms()
        if _ticks_diff(now, self._last_reg) >= CMD_REG_INTERVAL_MS:
            self._register()
            self._last_reg = now
        try:
            data = self.sock.recv(64)
        except Exception:
            data = None
        if valid_frame(data):
            _uart.write(data)
            self.rx += 1
            self._last_rx = now
            if not self.ok:
                print("[CMD] server streaming")
                self.ok = True
            return True
        if _ticks_diff(now, self._last_rx) > CMD_STALE_MS:
            if self.ok:
                print("[CMD] feed lost")
                self.ok = False
        return False

    def run(self):
        """轮询线程主体：5s 打一次接收速率统计。"""
        last_stat = time.time()
        n_win = 0
        while True:
            self.poll_once()
            n_win += 1
            now = time.time()
            if now - last_stat >= 5:
                print("[CMD] rate=%dHz ok=%s" % (self.rx // 5, self.ok))
                self.rx = 0
                last_stat = now
                gc.collect()


def start_cmd_thread(server_ip):
    """启动命令桥线程；_thread 不可用时返回 None（退化为插询模式）。"""
    init_uart3()
    poller = CommandPoller(server_ip, CMD_POLL_HZ)
    try:
        import _thread
        _thread.start_new_thread(poller.run, ())
        print("[CMD] thread started, expect %dHz push" % CMD_POLL_HZ)
        return poller
    except Exception as e:
        print("[CMD] _thread unavailable (%s), fallback to interleaved "
              "polling (rate will be lower)" % str(e))
        return poller


def send_frame(s, server_ip, frame_id, jpeg):
    """把一帧 JPEG 分片发出，返回分片数；异常由调用方处理。"""
    total = (len(jpeg) + CHUNK_SIZE - 1) // CHUNK_SIZE
    if total < 1:
        return 0
    if total > 256:
        print("frame too big (%dB/%d chunks), drop" % (len(jpeg), total))
        return 0

    mv = memoryview(jpeg)
    addr = (server_ip, SERVER_UDP_PORT)
    fid = frame_id & 0xFFFF
    buf = _SEND_BUF
    sbuf = _SEND_MV
    last = total - 1

    for idx in range(total):
        st = idx * CHUNK_SIZE
        ed = st + CHUNK_SIZE
        if ed > len(jpeg):
            ed = len(jpeg)
        plen = ed - st
        struct.pack_into(_HEADER, buf, 0,
                         _MAGIC, _VER, 0, fid, total, idx, plen)
        # 拷贝 payload 到复用缓冲
        buf[_HEADER_SIZE:_HEADER_SIZE + plen] = mv[st:ed]
        pkt = sbuf[:_HEADER_SIZE + plen]
        try:
            s.sendto(pkt, addr)
        except TypeError:
            # 少数固件的 sendto 不接受 memoryview，退回 bytes
            s.sendto(bytes(pkt), addr)
        if CHUNK_GAP_MS and idx != last:
            time.sleep_ms(CHUNK_GAP_MS)
    return total


def main():
    init_camera()
    _ip, gateway = connect_wifi()
    server_ip = SERVER_IP or gateway
    print("[3/3] pushing udp+jpeg to %s:%d q=%d fps=%d"
          % (server_ip, SERVER_UDP_PORT, JPEG_QUALITY, FPS))

    poller = start_cmd_thread(server_ip)
    globals()["poller"] = poller
    # _thread 可用时命令轮询跑独立线程，主循环原样 pacing；
    # 不可用时退化为视频 pacing 间隙插询（频率低，仅保底）
    use_thread = poller is not None
    try:
        import _thread  # noqa: F401
    except Exception:
        use_thread = False

    sta = get_sta()
    s = make_sock()
    frame_id = 0
    interval_ms = int(1000 / FPS)
    sent_frames = 0
    dropped = 0
    last_len = 0
    t_stat = time.time()

    while True:
        try:
            import os
            os.exitpoint()
        except Exception:
            pass

        t0 = time.ticks_ms() if hasattr(time, "ticks_ms") else 0

        if not ensure_wifi(sta):
            time.sleep(2)
            continue

        jpeg = None
        try:
            jpeg = capture_jpeg()
        except Exception as e:
            print("capture drop: " + str(e))
            dropped += 1
            time.sleep_ms(50)
            continue

        # 基础 JPEG 合法性检查，不过就丢帧（不发坏帧卡 server）
        if len(jpeg) < 4 or jpeg[0] != 0xFF or jpeg[1] != 0xD8:
            print("bad jpeg head, drop len=%d" % len(jpeg))
            dropped += 1
            del jpeg
            continue

        last_len = len(jpeg)
        try:
            send_frame(s, server_ip, frame_id, jpeg)
            frame_id = (frame_id + 1) & 0xFFFF
            sent_frames += 1
        except Exception as e:
            print("udp send err: " + str(e) + ", rebuild socket")
            # 先清引用，再重建，避免旧 socket 悬挂
            try:
                s.close()
            except Exception:
                pass
            s = None
            gc.collect()
            try:
                s = make_sock()
            except Exception:
                pass
            dropped += 1
            del jpeg
            time.sleep_ms(100)
            continue

        # 及时释放 JPEG 引用，让 GC 有机会在下一帧前回收
        del jpeg

        # 周期性强制回收：MicroPython 堆碎片在长时间运行下会拖慢分配
        if sent_frames and (sent_frames % GC_COLLECT_EVERY) == 0:
            gc.collect()

        now = time.time()
        if now - t_stat >= 10:
            try:
                free = gc.mem_free()
            except Exception:
                free = -1
            print("stat: sent=%d dropped=%d last=%dB free=%d"
                  % (sent_frames, dropped, last_len, free))
            t_stat = now

        # 帧率 pacing：扣掉 capture+send 花掉的时间
        if hasattr(time, "ticks_ms"):
            try:
                spent = time.ticks_diff(time.ticks_ms(), t0)
                rest = interval_ms - spent
                if rest > 0:
                    if use_thread or poller is None:
                        time.sleep_ms(rest)
                    else:
                        # 退化模式：pacing 间隙插询命令
                        step = 4
                        while rest > 0:
                            poller.poll_once()
                            d = step if rest >= step else rest
                            time.sleep_ms(d)
                            rest -= d
            except Exception:
                time.sleep_ms(interval_ms)
        else:
            time.sleep(1.0 / FPS)


try:
    main()
except KeyboardInterrupt as e:
    print("User stop: " + str(e))
except BaseException as e:
    try:
        import sys
        sys.print_exception(e)
    except Exception:
        print("fatal: " + str(e))
finally:
    try:
        if sensor is not None:
            sensor.stop()
    except Exception:
        pass
    try:
        if poller is not None:
            try:
                poller.sock.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        if _sta is not None:
            _sta.active(False)
    except Exception:
        pass
    gc.collect()