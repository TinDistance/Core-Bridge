"""H264 裸 RTP 链路本地回环测试（不需要 K230 硬件）。

模拟 K230：用 PyAV/libx264 编合成测试帧 -> RTP(FU-A) 打包 -> 打到
relay(:18002)；relay 纯转发；H264Viewer 接收解码出 PIL 帧。
验证：relay 状态 live/fps 正常、viewer 能解出帧。

    python test/test_rtp_loopback.py
"""
from __future__ import annotations

import struct
import sys
import time
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from desktop.streaming.h264_viewer import H264Viewer, available
from server.rtp_relay import RtpRelay

PORT = 18002
DURATION_S = 3.0
FPS = 30
MAX_PAYLOAD = 1200


def rtp_header(seq: int, ts: int, marker: bool, ssrc: int) -> bytes:
    return struct.pack(">BBHII", 0x80, 0x60 | (0x80 if marker else 0),
                       seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)


def split_nalus(data: bytes) -> list[bytes]:
    """Annex-B（3/4 字节起始码混合）-> NALU 列表，与 k230 rtp_push 一致。"""
    n = len(data)
    payload = data.find(b"\x00\x00\x01")
    marks = []
    while payload >= 0 and payload + 3 <= n:
        sc_start = payload - 1 if payload > 0 and data[payload - 1] == 0 else payload
        marks.append((sc_start, payload + 3))
        payload = data.find(b"\x00\x00\x01", payload + 3)
    out: list[bytes] = []
    for j in range(len(marks)):
        start = marks[j][1]
        end = marks[j + 1][0] if j + 1 < len(marks) else n
        if end > start:
            out.append(data[start:end])
    return out


def packetize_nalu(nalu: bytes, ts: int, seq: int, ssrc: int,
                   out: list[bytes]) -> int:
    if len(nalu) <= MAX_PAYLOAD:
        out.append(rtp_header(seq, ts, False, ssrc) + nalu)
        return (seq + 1) & 0xFFFF
    indicator = (nalu[0] & 0xE0) | 28
    nal_type = nalu[0] & 0x1F
    off = 1
    first = True
    while off < len(nalu):
        chunk = nalu[off:off + MAX_PAYLOAD]
        off += MAX_PAYLOAD
        last = off >= len(nalu)
        fu = nal_type
        if first:
            fu |= 0x80
            first = False
        if last:
            fu |= 0x40
        out.append(rtp_header(seq, ts, False, ssrc)
                   + bytes([indicator, fu]) + chunk)
        seq = (seq + 1) & 0xFFFF
    return seq


def main() -> int:
    if not available():
        print("FAIL: PyAV(av) 未安装")
        return 1
    import av

    relay = RtpRelay(PORT)
    if not relay.start():
        print("FAIL: relay 启动失败")
        return 1

    viewer = H264Viewer(f"http://127.0.0.1:{PORT}", port=PORT)
    viewer.start()

    enc = av.CodecContext.create("libx264", "w")
    enc.width = 320
    enc.height = 240
    enc.pix_fmt = "yuv420p"
    enc.time_base = Fraction(1, FPS)
    enc.options = {"tune": "zerolatency", "profile": "baseline"}

    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    got: list = []
    statuses: list = []

    # 边喂边收（viewer 事件 deque(maxlen=2) 只留最新，必须并发消费）
    def drain() -> None:
        for kind, payload in viewer.events():
            if kind == "frame":
                got.append(payload)
            elif kind == "status":
                statuses.append(payload)

    seq = 0
    ssrc = 0x54494E44
    target = ("127.0.0.1", PORT)
    frames_sent = 0
    t0 = time.monotonic()
    n = 0
    while time.monotonic() - t0 < DURATION_S:
        drain()
        # 合成动态画面（滚动条纹，编码器有东西可压）
        img = Image.new("RGB", (320, 240), (n % 255, 100, 200))
        for y in range(0, 240, 20):
            for x in range(0, 320, 20):
                if (x + y + n * 5) % 40 < 20:
                    img.paste((255, 255, 255), (x, y, x + 20, y + 20))
        frame = av.VideoFrame.from_image(img).reformat(format="yuv420p")
        packets = enc.encode(frame)
        ts = n * (90000 // FPS)
        for pkt in packets:
            data = bytes(pkt) if pkt else b""
            rtp_packets: list[bytes] = []
            for nalu in split_nalus(data):
                if nalu.strip(b"\x00"):
                    seq = packetize_nalu(nalu, ts, seq, ssrc, rtp_packets)
            if rtp_packets:
                rtp_packets[-1] = (bytearray(rtp_packets[-1]))
                rtp_packets[-1][1] |= 0x80
                for p in rtp_packets:
                    sock.sendto(p, target)
                frames_sent += 1
        n += 1
        time.sleep(1.0 / FPS)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        drain()
        time.sleep(0.05)

    relay_status = relay.status()
    relay.stop()
    viewer.stop()
    sock.close()

    print(f"sent={frames_sent} decoded={len(got)} statuses={statuses}")
    print(f"relay: live={relay_status.get('live')} fps={relay_status.get('fps')} "
          f"pkts_rx={relay_status.get('pkts_rx')} pkts_tx={relay_status.get('pkts_tx')} "
          f"up={relay_status.get('upstream')} down={relay_status.get('downstream')}")
    ok = len(got) >= 10 and relay_status.get("live") and frames_sent >= DURATION_S * FPS * 0.8
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
