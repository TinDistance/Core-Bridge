"""验证实验 E1：UDP goodput 接收端（桌面/服务器上运行）。

配合 k230/diag_udp_flood.py 使用：K230 泛洪 UDP，本端统计
每秒包数 / 吞吐 / 估计有效带宽。用法：

    python test/udp_flood_rx.py [--port 9001] [--seconds 30]
"""
from __future__ import annotations

import argparse
import socket
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--seconds", type=int, default=30)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(1.0)
    print(f"listening on udp:{args.port} for {args.seconds}s ...")

    pkts = 0
    total = 0
    t_start = time.monotonic()
    t_win = t_start
    win_pkts = 0
    win_bytes = 0
    peak_pps = 0.0
    while time.monotonic() - t_start < args.seconds:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        pkts += 1
        win_pkts += 1
        win_bytes += len(data)
        total += len(data)
        now = time.monotonic()
        dt = now - t_win
        if dt >= 1.0:
            pps = win_pkts / dt
            mbps = win_bytes * 8 / dt / 1e6
            peak_pps = max(peak_pps, pps)
            print(f"[{now - t_start:5.1f}s] {pps:7.0f} pps  {mbps:6.2f} Mbps  "
                  f"(from {addr[0]}:{addr[1]})")
            t_win = now
            win_pkts = 0
            win_bytes = 0
    dt = time.monotonic() - t_start
    print(f"\nsummary: {pkts} pkts, {total * 8 / dt / 1e6:.2f} Mbps avg, "
          f"peak {peak_pps:.0f} pps")


if __name__ == "__main__":
    main()
