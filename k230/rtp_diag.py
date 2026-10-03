# 验证实验 E2/E3：VENC 直读 NALU 形态诊断（在 K230 上运行）。
#
# 目的：不动 RTP 实现前先确认三个前提——
#   E2-1 GetStream 出的字节流形态：Annex-B start code（00000001/000001）？
#   E2-2 一帧 pack_cnt / NALU 数量：SPS+PPS+IDR 多 NAL？I/P 帧各多少包？
#   E2-3 stream.pts 的单位（样本差值打印，与帧计数推算的 3000/帧对比）
#   E3  MediaManager 是否需要显式 init（webrtc_push.py 未显式调用也能跑，
#       怀疑 webrtc 模块代劳；裸 RTP 路径无模块代劳，需实测两种情况）
#
# 运行：CanMV IDE / 串口直接跑本文件，观察打印。
import os
import time
import uctypes

import network
from media.sensor import *
from media.media import *
from media.vencoder import *

# ==================== 配置区 ====================
WIFI_SSID = "TF-Laptop"
WIFI_PASSWORD = "TF@HTR.Hello"
SENSOR_ID = 2
WIDTH = 1280
HEIGHT = 720
BIT_RATE = 3072
GOP_LEN = 30
FPS = 30
DUMP_FRAMES = 60          # dump 帧数（够覆盖 2 个 GOP）
TRY_MEDIA_INIT = True     # False = 复刻 webrtc_push 的裸初始化，对照 E3
# ================================================

sensor = None
encoder = None
link = None


def hex_head(addr, size, n=24):
    b = bytes(uctypes.bytearray_at(addr, min(n, size)))
    return " ".join("%02X" % c for c in b)


def main():
    global sensor, encoder, link
    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        sta.active(True)
    print("diag: wifi", "up" if sta.isconnected() else "down（NALU 诊断不需要网络）")

    sensor = Sensor(id=SENSOR_ID)
    sensor.reset()
    width = ALIGN_UP(WIDTH, 16)
    sensor.set_framesize(width=width, height=HEIGHT, alignment=12, chn=CAM_CHN_ID_0)
    sensor.set_pixformat(Sensor.YUV420SP, chn=CAM_CHN_ID_0)

    encoder = Encoder()
    encoder.SetOutBufs(8, width, HEIGHT)
    profile = getattr(encoder, "H264_PROFILE_BASELINE",
                      getattr(encoder, "H264_PROFILE_MAIN", None))
    print("diag: profile =", profile)
    chnAttr = ChnAttrStr(
        encoder.PAYLOAD_TYPE_H264, profile, width, HEIGHT,
        bit_rate=BIT_RATE, gopLen=GOP_LEN,
        src_frame_rate=FPS, dst_frame_rate=FPS,
    )
    encoder.Create(chnAttr)
    link = MediaManager.link(
        sensor.bind_info()["src"],
        (VIDEO_ENCODE_MOD_ID, VENC_DEV_ID, encoder.chn),
    )
    encoder.Start()
    sensor.run()
    print("diag: encoder started")

    stream = StreamData()
    frames = 0
    t_last = time.ticks_ms()
    pts_prev = None
    i_frame_sizes = []
    while frames < DUMP_FRAMES:
        os.exitpoint()
        if encoder.GetStream(stream, timeout=100) != 0:
            continue
        try:
            frames += 1
            sizes = []
            types = []
            for i in range(stream.pack_cnt):
                sizes.append(stream.data_size[i])
                types.append(stream.stream_type[i])
            pts = stream.pts[0] if stream.pack_cnt else -1
            dt = (time.ticks_diff(time.ticks_ms(), t_last)) if pts_prev is None else pts - pts_prev
            pts_prev = pts
            print("frame#%d pack_cnt=%d sizes=%s types=%s pts=%d dt=%s" % (
                frames, stream.pack_cnt, sizes, types, pts, dt))
            for i in range(stream.pack_cnt):
                print("  pack[%d] head: %s" % (
                    i, hex_head(stream.data[i], stream.data_size[i])))
            if stream.pack_cnt and stream.stream_type[0] == encoder.STREAM_TYPE_I:
                i_frame_sizes.append(sum(sizes))
        finally:
            encoder.ReleaseStream(stream)
        t_last = time.ticks_ms()
    if i_frame_sizes:
        print("diag: I frames=%d avg=%dB max=%dB -> 约 %d RTP 片/帧(@1200B)" % (
            len(i_frame_sizes),
            sum(i_frame_sizes) // len(i_frame_sizes),
            max(i_frame_sizes),
            max(i_frame_sizes) // 1200 + 1))
    print("diag: done")


try:
    main()
except KeyboardInterrupt as e:
    print("User stop: " + str(e))
except BaseException as e:
    import sys
    sys.print_exception(e)
finally:
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
    if encoder is not None and encoder.chn >= 0:
        try:
            encoder.Stop()
            encoder.Destroy()
        except Exception:
            pass
