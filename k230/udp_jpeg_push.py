# K230 (CanMV / MicroPython) UDP+JPEG 分片推送 -> Core-Bridge server.
#
# 链路：Sensor snapshot -> JPEG 压缩 -> UDP 分片（每片 <=1200B）-> server:8001/udp
#       server 重组完整帧 -> desktop 经 HTTP /video/latest.jpg 轮询显示
#
# 为什么不用 WebRTC/H.264：
#   * K230 固件 webrtc 栈 + aiortc 对接脆弱（SDP/连接状态难排错）
#   * UDP+JPEG 无连接、无重传、丢包只丢一帧，WiFi 下更稳、延迟更低
#
# 稳定性关键（不要随意删）：
#   1. 分辨率默认 640x480 + quality 65：单帧约 20~40KB（约 20~35 包），
#      720p 下单帧 60~100KB+，WiFi 丢包率指数上升。如需清晰度再往上加。
#   2. 片间 pacing 2ms：避免 UDP 突发把路由器/PC 接收缓冲打爆。
#   3. 固定帧率 + 跳帧：capture+send 超时则直接下一帧，不堆积。
#   4. socket 复用 + 发送失败重建；WiFi 断线自动重连。
#   5. frame_id u16 循环，server 靠 (frame_id,total,idx) 重组，乱序/重复都安全。
#
# 服务端对应：server/video_hub.py + server/routers/video.py（协议 v1）
#   头 12B 大端 ">HBBHHHH"：magic=0x4A50, ver=1, flags=0,
#                          frame_id, total, idx, plen

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
# ================================================

_MAGIC = 0x4A50
_VER = 1
_HEADER = ">HBBHHHH"
_HEADER_SIZE = 12

sensor = None


def init_camera():
    global sensor
    sensor = Sensor(id=SENSOR_ID)
    sensor.reset()
    # snapshot 用 RGB888（JPEG 压缩输入）；不要用 YUV420SP（那是给 VENC 的）
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


def connect_wifi(timeout_s=15):
    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        sta.active(True)
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


def capture_jpeg():
    """返回 JPEG bytes；兼容不同固件的 compress/to_jpeg 命名。"""
    img = sensor.snapshot()
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
                return bytes(data)
        except Exception as e:
            print("capture via %s failed: %s" % (name, str(e)))
            continue
    # 有些固件 snapshot(compress=True) 直接回 JPEG
    try:
        img2 = sensor.snapshot(compress=True, quality=JPEG_QUALITY)
        if img2 is not None and len(img2) > 4:
            return bytes(img2)
    except Exception:
        pass
    raise RuntimeError("no JPEG API: image attrs=" + str([a for a in dir(img) if 'jpeg' in a.lower() or 'compress' in a.lower()]))


def make_sock():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return s


def send_frame(s, server_ip, frame_id, jpeg):
    total = (len(jpeg) + CHUNK_SIZE - 1) // CHUNK_SIZE
    if total < 1:
        return 0
    if total > 256:
        print("frame too big (%dB/%d chunks), drop" % (len(jpeg), total))
        return 0
    mv = memoryview(jpeg)
    for idx in range(total):
        st = idx * CHUNK_SIZE
        ed = st + CHUNK_SIZE
        if ed > len(jpeg):
            ed = len(jpeg)
        piece = mv[st:ed]
        hdr = struct.pack(_HEADER, _MAGIC, _VER, 0, frame_id & 0xFFFF, total, idx, (ed - st))
        try:
            s.sendto(hdr + piece, (server_ip, SERVER_UDP_PORT))
        except Exception as e:
            raise e
        if CHUNK_GAP_MS and idx != total - 1:
            time.sleep_ms(CHUNK_GAP_MS)
    return total


def main():
    global sensor
    init_camera()
    _ip, gateway = connect_wifi()
    server_ip = SERVER_IP or gateway
    print("[3/3] pushing udp+jpeg to %s:%d q=%d fps=%d" % (server_ip, SERVER_UDP_PORT, JPEG_QUALITY, FPS))

    sta = network.WLAN(network.STA_IF)
    s = make_sock()
    frame_id = 0
    interval_ms = int(1000 / FPS)
    sent_frames = 0
    dropped = 0
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
            continue

        try:
            send_frame(s, server_ip, frame_id, jpeg)
            frame_id = (frame_id + 1) & 0xFFFF
            sent_frames += 1
        except Exception as e:
            print("udp send err: " + str(e) + ", rebuild socket")
            try:
                s.close()
            except Exception:
                pass
            try:
                s = make_sock()
            except Exception:
                pass
            dropped += 1
            time.sleep_ms(100)
            continue

        now = time.time()
        if now - t_stat >= 10:
            print("stat: sent=%d dropped=%d last=%dB" % (sent_frames, dropped, len(jpeg)))
            t_stat = now

        # 帧率 pacing：capture+send 花掉的时间扣掉
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
