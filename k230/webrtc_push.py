# K230 (CanMV) WebRTC push -> Core-Bridge server.
#
# Pipeline: Sensor(chn0, YUV420SP) -> VENC(H.264) -> webrtc.PeerConnection
#           -> POST offer to Core-Bridge /webrtc/push -> raw SDP answer.
#
# Fixes vs. the previous draft:
#   * sensor chn0 is YUV420SP (VENC input), not RGB888
#   * real VENC encode loop (GetStream/ReleaseStream), not an undefined `venc_data`
#   * SPS/PPS (STREAM_TYPE_HEADER) cached and re-sent before every I-frame
#   * RequestIDR() once connected so the server can decode the first frame
#   * timestamps from stream.pts, not time.ticks_us()
#   * correct resource teardown (link.destroy / encoder.Stop+Destroy)
#
# Local IDE preview was removed: chn0 must stay YUV420SP for the encoder.
# If you want a preview, add a second sensor channel + Display bind and view
# that channel instead of chn0.

import os
import socket
import time
import uctypes

import network
from media.sensor import *
from media.media import *
from media.vencoder import *
import webrtc

# ==================== 配置区 ====================
WIFI_SSID = "TF-Laptop"
WIFI_PASSWORD = "TF@HTR.Hello"
SERVER_IP = None            # None = 用网关地址（连热点时网关就是电脑）
SERVER_PORT = 8000
PUSH_PATH = "/webrtc/push"
SENSOR_ID = 2
WIDTH = 1280                # 编码宽度（会自动 16 对齐）
HEIGHT = 720
BIT_RATE = 2048             # Kbit/s
GOP_LEN = 30
# ================================================

sensor = None
encoder = None
link = None
peer = None


def init_camera():
    global sensor
    sensor = Sensor(id=SENSOR_ID)
    sensor.reset()
    width = ALIGN_UP(WIDTH, 16)
    # chn0 提供给 VENC 编码，必须是 YUV420SP
    sensor.set_framesize(width=width, height=HEIGHT, alignment=12, chn=CAM_CHN_ID_0)
    sensor.set_pixformat(Sensor.YUV420SP, chn=CAM_CHN_ID_0)
    print("[1/4] camera init done")


def connect_wifi():
    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        sta.active(True)
    print("WiFi active:", sta.active())
    sta.scan()
    print("[2/4] connecting to " + WIFI_SSID)
    sta.connect(WIFI_SSID, WIFI_PASSWORD)

    timeout = 15
    while timeout > 0:
        if sta.isconnected():
            break
        timeout -= 1
        time.sleep(1)

    if not sta.isconnected():
        raise RuntimeError("WiFi connect failed")

    ip, _netmask, gateway, _dns = sta.ifconfig()
    print("Connected! IP: %s Gateway: %s" % (ip, gateway))
    return ip, gateway


def init_encoder():
    global encoder, link
    width = ALIGN_UP(WIDTH, 16)
    encoder = Encoder()
    encoder.SetOutBufs(8, width, HEIGHT)
    chnAttr = ChnAttrStr(
        encoder.PAYLOAD_TYPE_H264,
        encoder.H264_PROFILE_MAIN,
        width,
        HEIGHT,
        bit_rate=BIT_RATE,
        gopLen=GOP_LEN,
    )
    encoder.Create(chnAttr)
    link = MediaManager.link(
        sensor.bind_info()["src"],
        (VIDEO_ENCODE_MOD_ID, VENC_DEV_ID, encoder.chn),
    )
    encoder.Start()
    sensor.run()
    print("[3/4] encoder started (chn=%d, %dx%d)" % (encoder.chn, width, HEIGHT))


def _send_all(s, data):
    # MicroPython socket.send may send partially; loop until done.
    sent = 0
    while sent < len(data):
        n = s.send(data[sent:])
        if not n:
            raise RuntimeError("socket send returned 0")
        sent += n


def _body_complete(data):
    """True once headers are in and Content-Length body bytes arrived."""
    idx = data.find(b"\r\n\r\n")
    if idx < 0:
        return False
    body_len = len(data) - (idx + 4)
    for line in data[:idx].split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            num = line.split(b":", 1)[1].strip()
            n = 0
            for ch in num:
                if 48 <= ch <= 57:
                    n = n * 10 + (ch - 48)
                else:
                    return False
            return body_len >= n
    return False


def _recv_response(s, total_timeout=15):
    # K230 firmware recv() may return b"" while the reply is still in flight
    # (the 200 answer needs aiortc crypto first, slower than a 400 reject),
    # so only treat it as EOF once a full Content-Length body has arrived.
    data = b""
    start = time.time()
    while time.time() - start < total_timeout:
        try:
            chunk = s.recv(4096)
        except OSError:
            continue  # recv timeout; the overall deadline still guards us
        if chunk:
            data += chunk
            if _body_complete(data):
                break
        else:
            if _body_complete(data):
                break
            time.sleep(0.05)
    return data


def http_post_offer(server_ip, sdp_text, retries=3):
    sdp_bytes = sdp_text.encode()
    head = "POST " + PUSH_PATH + " HTTP/1.1\r\n"
    head += "Host: " + server_ip + "\r\n"
    head += "Content-Type: application/sdp\r\n"
    head += "Content-Length: " + str(len(sdp_bytes)) + "\r\n"
    head += "Connection: close\r\n"
    head += "\r\n"
    request = head.encode() + sdp_bytes

    for attempt in range(retries):
        s = socket.socket()
        s.settimeout(5)
        try:
            s.connect((server_ip, SERVER_PORT))
            _send_all(s, request)
            response = _recv_response(s)
            print("HTTP: got %d bytes (attempt %d/%d)" % (len(response), attempt + 1, retries))
            if not response:
                time.sleep(1)
                continue
            return _parse_offer_response(response)
        except Exception as e:
            print("POST failed: " + str(e))
            time.sleep(1)
        finally:
            s.close()
    return None


def _parse_offer_response(response):
    header, sep, body = response.decode().partition("\r\n\r\n")
    if not sep:
        print("HTTP: no header/body separator, raw=" + response.decode("utf-8", "replace")[:500])
        return None
    status_line = header.split("\r\n", 1)[0]
    print("HTTP:", status_line)
    body_text = body.strip()
    if " 200 " not in (" " + status_line + " "):
        # FastAPI 422/400 bodies contain the real reason (e.g. expected JSON
        # vs raw SDP, or aiortc SDP parse error). Must NOT treat as answer.
        print("Server rejected offer, body=" + body_text[:1000])
        return None
    if not body_text:
        print("Empty Answer SDP")
        return None
    return body_text


def start_webrtc_push(server_ip, local_ip):
    global peer
    try:
        peer = webrtc.PeerConnection(
            video_codec=webrtc.CODEC_H264,
            audio_codec=webrtc.CODEC_NONE,
            local_ip=local_ip,
        )
    except TypeError:
        # Older firmware without the local_ip argument.
        peer = webrtc.PeerConnection(
            video_codec=webrtc.CODEC_H264,
            audio_codec=webrtc.CODEC_NONE,
        )

    offer_sdp = peer.create_offer()
    print("Offer SDP created, len=" + str(len(offer_sdp)))
    print("Offer head: " + offer_sdp[:800].replace("\r\n", "|"))

    answer_sdp = http_post_offer(server_ip, offer_sdp)
    if answer_sdp is None:
        raise RuntimeError("No Answer SDP")
    print("Answer head: " + answer_sdp[:800].replace("\r\n", "|"))

    peer.set_remote_description(answer_sdp, webrtc.SDP_TYPE_ANSWER)
    print("[4/4] signaling done, waiting for connection...")


def stream_loop():
    stream = StreamData()
    parameter_sets = None
    requested_idr = False
    while True:
        os.exitpoint()

        if peer.is_connected() and not requested_idr:
            # Ask VENC for an immediate key frame so the receiver can decode.
            encoder.RequestIDR()
            requested_idr = True
            print("WebRTC connected, requested IDR")

        if encoder.GetStream(stream, timeout=100) != 0:
            continue
        try:
            for i in range(stream.pack_cnt):
                data = uctypes.bytearray_at(stream.data[i], stream.data_size[i])
                stream_type = stream.stream_type[i]
                timestamp = stream.pts[i]
                if stream_type == encoder.STREAM_TYPE_HEADER:
                    parameter_sets = bytes(data)
                elif peer.is_connected():
                    if stream_type == encoder.STREAM_TYPE_I and parameter_sets:
                        peer.send_video(parameter_sets, timestamp)
                    peer.send_video(data, timestamp)
        finally:
            encoder.ReleaseStream(stream)


def cleanup():
    global peer, sensor, link, encoder
    if peer is not None:
        try:
            peer.close()
        except Exception:
            pass
        peer = None
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


try:
    init_camera()
    local_ip, gateway = connect_wifi()
    init_encoder()
    start_webrtc_push(SERVER_IP or gateway, local_ip)
    stream_loop()
except KeyboardInterrupt as e:
    print("User stop: " + str(e))
except BaseException as e:
    import sys
    sys.print_exception(e)
finally:
    cleanup()
