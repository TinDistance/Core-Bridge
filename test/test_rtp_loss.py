"""丢包恢复测试：模拟 25% 随机丢包，验证 viewer 的 wait_idr 策略。

预期：丢包后不出"绿斑残帧"（不发任何 frame 事件直到 IDR 到达并解出），
IDR 请求触发后恢复出帧。

    python test/test_rtp_loss.py
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

PORT = 18003
DURATION_S = 6.0
FPS = 30
LOSS = 0.25


def packetize_nalu(nalu: bytes, ts: int, seq: int, ssrc: int,
                   out: list[bytes]) -> int:
    if len(nalu) <= 1200:
        out.append(struct.pack(">BBHII", 0x80, 0x60, seq & 0xFFFF, ts, ssrc) + nalu)
        return (seq + 1) & 0xFFFF
    indicator = (nalu[0] & 0xE0) | 28
    nal_type = nalu[0] & 0x1F
    off = 1
    first = True
    while off < len(nalu):
        chunk = nalu[off:off + 1200]
        off += 1200
        last = off >= len(nalu)
        fu = nal_type | (0x80 if first else 0) | (0x40 if last else 0)
        first = False
        out.append(struct.pack(">BBHII", 0x80, 0x60, seq & 0xFFFF, ts, ssrc)
                   + bytes([indicator, fu]) + chunk)
        seq = (seq + 1) & 0xFFFF
    return seq


def main() -> int:
    import av

    relay = RtpRelay(PORT)
    relay.start()
    viewer = H264Viewer(f"http://127.0.0.1:{PORT}", port=PORT)
    viewer.start()

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = 320, 240, "yuv420p"
    enc.time_base = Fraction(1, FPS)
    enc.options = {"tune": "zerolatency", "profile": "baseline",
                   "g": "30", "keyint": "30"}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rng = random.Random(7)
    seq = 0
    ssrc = 0x54494E44
    target = ("127.0.0.1", PORT)
    got = 0
    n = 0
    t0 = time.monotonic()
    last_report = 0.0
    while time.monotonic() - t0 < DURATION_S:
        for kind, _payload in viewer.events():
            if kind == "frame":
                got += 1
        img = Image.new("RGB", (320, 240), (n * 7 % 255, 100, 200))
        f = av.VideoFrame.from_image(img).reformat(format="yuv420p")
        ts = n * (90000 // FPS)
        for pkt in enc.encode(f):
            data = bytes(pkt)
            rtps: list[bytes] = []
            for nalu in split_nalus(data):
                if nalu.strip(b"\x00"):
                    seq = packetize_nalu(nalu, ts, seq, ssrc, rtps)
            if rtps:
                rtps[-1] = bytearray(rtps[-1])
                rtps[-1][1] |= 0x80
                for p in rtps:
                    if rng.random() >= LOSS:
                        sock.sendto(p, target)
        n += 1
        now = time.monotonic()
        if now - last_report >= 1.0:
            last_report = now
            st = viewer.stats()
            print(f"t={now - t0:.0f}s frames={st['frames']} "
                  f"dropped={st['dropped_wait_idr']} fails={st['decode_fails']}")
        time.sleep(1.0 / FPS)

    st = viewer.stats()
    relay.stop()
    viewer.stop()
    sock.close()
    ok = st["frames"] >= 20 and st["dropped_wait_idr"] > 0
    print(f"frames={st['frames']} dropped_wait_idr={st['dropped_wait_idr']} "
          f"decode_fails={st['decode_fails']} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
