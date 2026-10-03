"""真机链路探针：不开 GUI，直接挂到 relay 上看 K230 的流到底怎么样。

用途：桌面端黑屏时，用它把"K230/网络问题"和"桌面 GUI 问题"分开。
它和 desktop/streaming/h264_viewer.py 用的是同一套接收+解码逻辑，
所以这里能出帧、GUI 就一定能出帧。

    # 先启动 server（start_desktop.bat 或 python -m uvicorn server.app:app）
    python test/probe_h264.py                 # 默认 127.0.0.1:8002
    python test/probe_h264.py --port 8002 --seconds 20

判读：
  rx=0            -> 包没到 PC：查 K230 目的 IP/端口、Windows 防火墙 8002
  rx 涨 frames=0  -> 收得到但解不出：K230 码流或 wait_idr 卡住，看 fails/lost
  frames 正常涨    -> 链路没问题，问题在 GUI 层
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop.streaming.h264_viewer import H264Viewer, available


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8002)
    ap.add_argument("--seconds", type=float, default=20.0)
    args = ap.parse_args()

    if not available():
        print("FAIL: 没装 PyAV(av)，H264 链路无法解码")
        return 1

    viewer = H264Viewer(f"http://{args.host}", port=args.port)
    viewer.start()
    print(f"probe -> {args.host}:{args.port}  ({args.seconds:.0f}s)")
    print("  t(s)   rx   lost   gaps fixed  frames dropped fails")
    t0 = time.monotonic()
    next_tick = 1.0
    while time.monotonic() - t0 < args.seconds:
        time.sleep(0.2)
        now = time.monotonic()
        if now >= next_tick:
            next_tick += 1.0
            s = viewer.stats()
            print(f"  {now - t0:4.0f} {s['pkts_rx']:5d} {s['pkts_lost']:6d} "
                  f"{s['gaps_seen']:6d} {s['reorder_fixed']:5d} "
                  f"{s['frames']:7d} {s['dropped_wait_idr']:7d} "
                  f"{s['decode_fails']:5d}", flush=True)
    viewer.stop()

    s = viewer.stats()
    print()
    if s["pkts_rx"] == 0:
        print("结论: 一个包都没收到 —— 查 K230 的 SERVER_IP/RTP_PORT 和 "
              "Windows 防火墙(UDP 8002 入站)。")
        return 2
    if s["frames"] == 0:
        print(f"结论: 收到 {s['pkts_rx']} 包但一帧都没解出来 —— "
              f"lost={s['pkts_lost']} fails={s['decode_fails']} "
              f"dropped={s['dropped_wait_idr']}。链路层问题。")
        return 3
    print(f"结论: 正常，显示 {s['frames']} 帧，丢包率 {s['loss_pct']}%。"
          f"若 GUI 仍黑屏，问题在 GUI 层。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())