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
SERVER_UDP_PORT = 8001
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
                    time.sleep_ms(rest)
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
        if _sta is not None:
            _sta.active(False)
    except Exception:
        pass
    gc.collect()