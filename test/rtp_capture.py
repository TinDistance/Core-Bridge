"""E4 抓包：把 K230 直推的裸 RTP 落盘，供 analyze_rtp_dump.py 离线复现。

用法（桌面 PC 上）：
  1. 停掉 server（uvicorn），否则 8002 被 relay 占用；
  2. `python test/rtp_capture.py [秒数=5]`，默认写 test/rtp_dump.bin；
  3. K230 上运行 k230/rtp_push.py（它会直接推本机 :8002）。

本脚本不做任何解码，只统计 + 落盘，尽量贴近真实接收路径。
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

PORT = 8002
DUMP = Path(__file__).resolve().parent / "rtp_dump.bin"


def main() -> int:
    import socket

    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(0.5)
    print(f"capturing :{PORT} for {seconds}s -> {DUMP}")

    pkts: list[bytes] = []
    seen = 0
    ctrl = 0
    t0 = time.monotonic()
    first_ts = None
    markers = 0
    payloads: dict[int, int] = {}
    while time.monotonic() - t0 < seconds:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        seen += 1
        if data[:3] == b"CBR":
            ctrl += 1
            continue
        pkts.append(data)
        if len(data) < 13 or (data[0] & 0xC0) != 0x80:
            continue
        pt = data[1] & 0x7F
        marker = bool(data[1] & 0x80)
        payloads[pt] = payloads.get(pt, 0) + 1
        ts = struct.unpack_from(">I", data, 4)[0]
        if first_ts is None:
            first_ts = ts
        if marker:
            markers += 1
        if seen <= 8:
            print(f"  pkt#{seen} from={addr} len={len(data)} "
                  f"head={data[:16].hex()} pt={pt} marker={marker}")
    sock.close()

    DUMP.write_bytes(b"LP" + b"".join(
        struct.pack(">H", len(p)) + p for p in pkts))
    dur = time.monotonic() - t0
    print(f"total={seen} rtp={len(pkts)} ctrl={ctrl} markers={markers} "
          f"pts={ {k: v for k, v in payloads.items()} }")
    print(f"approx fps(markers)={markers / dur:.1f}  dump={DUMP} "
          f"({DUMP.stat().st_size} bytes)")
    return 0 if pkts else 1


if __name__ == "__main__":
    raise SystemExit(main())
