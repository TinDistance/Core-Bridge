"""发送端丢帧 + 链路乱序 联合测试（k230/rtp_push.py <-> h264_viewer.py）。

覆盖两个真实故障：

1) 发送端整帧丢弃不得在 RTP 序号空间留洞
   真机日志实测 37% 的帧被 PACER_PPS 饿死丢弃。旧实现在丢弃分支之前就
   推进了 seq，于是每个丢帧都在序号空间留下一个永久空洞；接收端每次都
   判丢包 -> wait_idr -> 等下一个 IDR，而下一个 IDR 前又有新空洞，
   结果桌面端永久"没信号"。本测试让发送端按 37% 丢帧，断言接收端
   loss_events == 0。

2) 链路乱序不得被误判为丢包
   WiFi 上 UDP 乱序到几十毫秒是常态。旧接收端一见 seq 跳变就 wait_idr，
   每个乱序包都变成一次黑屏。现在靠 REORDER_S 窗口吸收。

    python test/test_rtp_reorder.py
"""
from __future__ import annotations

import random
import socket
import struct
import sys
import time
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from desktop.streaming.h264_viewer import H264Viewer
from server.rtp_relay import RtpRelay
from test.test_rtp_loopback import split_nalus

PORT = 18004
DURATION_S = 6.0
FPS = 30
GOP = 15               # 对齐 k230/rtp_push.py 的 GOP_LEN=15
MAX_PAYLOAD = 1200
REORDER = 0.15         # 每个包被扣下来晚点发的概率
DELAY_S = 0.030        # 扣多久（真实 WiFi 抖动量级）
SENDER_DROP_RATE = 0.37  # 真机实测的发送端丢帧率


def packetize_nalu(nalu: bytes, ts: int, seq: int, ssrc: int,
                   out: list[bytes]) -> int:
    if len(nalu) <= MAX_PAYLOAD:
        out.append(struct.pack(">BBHII", 0x80, 0x60, seq & 0xFFFF, ts, ssrc) + nalu)
        return (seq + 1) & 0xFFFF
    indicator = (nalu[0] & 0xE0) | 28
    nal_type = nalu[0] & 0x1F
    off = 1
    first = True
    while off < len(nalu):
        chunk = nalu[off:off + MAX_PAYLOAD]
        off += MAX_PAYLOAD
        last = off >= len(nalu)
        fu = nal_type | (0x80 if first else 0) | (0x40 if last else 0)
        first = False
        out.append(struct.pack(">BBHII", 0x80, 0x60, seq & 0xFFFF, ts, ssrc)
                   + bytes([indicator, fu]) + chunk)
        seq = (seq + 1) & 0xFFFF
    return seq


def make_frame(n: int):
    import av
    img = Image.new("RGB", (320, 240), (n * 7 % 255, 100, 200))
    for y in range(0, 240, 20):
        for x in range(0, 320, 20):
            if (x + y + n * 5) % 40 < 20:
                img.paste((255, 255, 255), (x, y, x + 20, y + 20))
    return av.VideoFrame.from_image(img).reformat(format="yuv420p")


def main() -> int:
    import av

    relay = RtpRelay(PORT)
    if not relay.start():
        print("FAIL: relay 启动失败")
        return 1
    viewer = H264Viewer(f"http://127.0.0.1:{PORT}", port=PORT)
    viewer.start()

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = 320, 240, "yuv420p"
    enc.time_base = Fraction(1, FPS)
    enc.options = {"tune": "zerolatency", "profile": "baseline",
                   "g": str(GOP), "keyint": str(GOP)}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rng = random.Random(11)
    seq = 0
    ssrc = 0x54494E44
    target = ("127.0.0.1", PORT)
    delayed: list[tuple[float, bytes]] = []
    stats = dict(swaps=0, dropped=0, pkts=0, aus=0, encoded=0)

    def emit(data: bytes) -> None:
        """模拟 rtp_push.py：局部 seq_next 打包，真正发出才提交 seq。"""
        rtps: list[bytes] = []
        nxt = seq
        is_idr = False
        for nalu in split_nalus(data):
            if not nalu.strip(b"\x00"):
                continue
            if (nalu[0] & 0x1F) in (5, 7):   # IDR / SPS
                is_idr = True
            nxt = packetize_nalu(nalu, stats["encoded"] * (90000 // FPS),
                                 nxt, ssrc, rtps)
        if not rtps:
            return
        # 只丢 P 帧：I 帧是唯一恢复手段，rtp_push.py 里无条件放行。
        # 若连 I 帧一起丢，后续 P 帧因参考链断裂本就解不出来，那是发送端
        # 的 bug，不是接收端的。
        if not is_idr and rng.random() < SENDER_DROP_RATE:
            stats["dropped"] += 1
            return                       # 整帧丢弃：seq 不提交，无空洞
        emit.seq = nxt                     # type: ignore[attr-defined]
        stats["aus"] += 1
        rtps[-1] = bytearray(rtps[-1])
        rtps[-1][1] |= 0x80
        for p in rtps:
            if rng.random() < REORDER:
                stats["swaps"] += 1
                delayed.append((time.monotonic() + DELAY_S, bytes(p)))
            else:
                stats["pkts"] += 1
                sock.sendto(p, target)

    def flush_now() -> None:
        now = time.monotonic()
        keep = []
        for due, p in delayed:
            if due <= now:
                stats["pkts"] += 1
                sock.sendto(p, target)
            else:
                keep.append((due, p))
        delayed[:] = keep

    nonlocal_seq = [0]
    n = 0
    t0 = time.monotonic()
    last_report = 0.0
    while time.monotonic() - t0 < DURATION_S:
        viewer.events()
        flush_now()
        for pkt in enc.encode(make_frame(n)):
            emit(bytes(pkt) if pkt else b"")
        # 提交本帧序号（emit 内部保证 drop 分支不提交）
        nonlocal_seq[0] = getattr(emit, "seq", nonlocal_seq[0])
        seq = nonlocal_seq[0]
        n += 1
        now = time.monotonic()
        if now - last_report >= 1.0:
            last_report = now
            s = viewer.stats()
            print(f"t={now - t0:.0f}s frames={s['frames']} "
                  f"lost={s['pkts_lost']} gaps={s['gaps_seen']} "
                  f"swap={stats['swaps']}")
        time.sleep(1.0 / FPS)

    flush_now()
    for _due, p in delayed:
        stats["pkts"] += 1
        sock.sendto(p, target)
    try:
        for pkt in enc.encode(None):
            emit(bytes(pkt) if pkt else b"")
    except Exception:
        pass

    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        viewer.events()
        time.sleep(0.05)

    st = viewer.stats()
    relay.stop()
    viewer.stop()
    sock.close()

    all_recv = st["pkts_rx"] == stats["pkts"]
    ok = (st["loss_events"] == 0 and st["decode_fails"] == 0 and all_recv
          and stats["swaps"] > 0 and st["frames"] >= stats["aus"] * 0.9)
    print(f"发送 {stats['aus']} AU / {stats['pkts']} 包"
          f"（编码 {n}，发送端整帧丢弃 {stats['dropped']}）")
    print(f"  接收 {st['pkts_rx']} 包，丢 {st['pkts_lost']}，重复 {st['pkts_dup']}"
          f" -> {'全收齐' if all_recv else '有丢包!'}")
    print(f"  乱序空洞 {st['gaps_seen']} 次，窗口修复 {st['reorder_fixed']} 次，"
          f"真丢包 {st['loss_events']} 次")
    print(f"  显示 {st['frames']}/{stats['aus']} 帧，"
          f"解码失败 {st['decode_fails']}")
    print(f"  -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())