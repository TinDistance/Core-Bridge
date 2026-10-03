"""离线复现：rtp_dump.bin -> _Depacketizer -> PyAV 解码，打印每步结果。

用法：`python test/analyze_rtp_dump.py [test/rtp_dump.bin]`
不依赖网络，直接定位是解包问题还是解码问题。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import av

from desktop.streaming.h264_viewer import _Depacketizer


def main() -> int:
    dump = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent / "rtp_dump.bin")
    raw = dump.read_bytes()
    print(f"dump={dump} bytes={len(raw)}")

    # 按 1212B 上限把连续流切回包（抓包时是 recvfrom，落盘是拼接，
    # 这里按 FU-A/单 NAL 的长度边界重切：直接逐包扫 0x80.. RTP 头）
    # 简化：抓包脚本按包拼接无分隔符，因此这里用启发式切分不可靠。
    # => 约定 rtp_capture.py 用 2 字节长度前缀落盘。
    pkts: list[bytes] = []
    off = 0
    if raw[:2] == b"LP":  # 长度前缀格式
        off = 2
        while off + 2 <= len(raw):
            (n,) = struct.unpack_from(">H", raw, off)
            off += 2
            pkts.append(raw[off:off + n])
            off += n
    else:
        print("提示：dump 无长度前缀，按包长 1212 均分不可靠，"
              "请用新版 rtp_capture.py 重新抓取")
        return 1
    print(f"packets={len(pkts)}")

    depack = _Depacketizer()
    codec = av.CodecContext.create("h264", "r")
    codec.options = {"flags": "low_delay"}

    aus: list[bytes] = []
    fails = 0
    decoded = 0
    fail_head = None
    fail_err = None
    for i, p in enumerate(pkts):
        au = depack.push(p)
        if au is None:
            continue
        aus.append(au)
        try:
            frames = codec.decode(av.Packet(au))
            if frames:
                decoded += 1
        except Exception as e:
            fails += 1
            if fail_head is None:
                fail_head = au[:24].hex()
                fail_err = repr(e)
    nalu0 = aus[0][:48].hex() if aus else "-"
    print(f"aus={len(aus)} decoded_frames={decoded} decode_fails={fails}")
    print(f"first au head: {nalu0}")
    if fail_err:
        print(f"first fail: {fail_err} | head={fail_head}")
    return 0 if decoded else 1


if __name__ == "__main__":
    raise SystemExit(main())
