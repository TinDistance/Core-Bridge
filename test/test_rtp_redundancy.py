# -*- coding: utf-8 -*-
"""15Mbps 多链冗余 RTP 纯逻辑仿真 + relay 多端口扇出集成测试。

不依赖 K230 硬件：micropython 模块用 stub 替身；relay 用 localhost 真 socket。
"""
import os
import socket
import struct
import sys
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------- micropython stubs ----------
def _install_mpy_stubs():
    if "k230.rtp_push" in sys.modules:
        return
    if not hasattr(sys, "print_exception"):
        sys.print_exception = lambda e, f=None: None  # noqa: F401
    for name in ("network", "uctypes", "machine"):
        sys.modules.setdefault(name, types.ModuleType(name))
    media = types.ModuleType("media")
    for sub in ("sensor", "media", "vencoder"):
        m = types.ModuleType(f"media.{sub}")
        sys.modules[f"media.{sub}"] = m
        setattr(media, sub, m)
    sys.modules["media"] = media
    sensor = sys.modules["media.sensor"]
    sensor.Sensor = lambda **kw: (_ for _ in ()).throw(RuntimeError("stub"))
    sensor.CAM_CHN_ID_0 = 0
    venc = sys.modules["media.vencoder"]
    venc.Encoder = lambda **kw: (_ for _ in ()).throw(RuntimeError("stub"))
    venc.CHN_STR = None


def _load_k230():
    import importlib
    _install_mpy_stubs()
    import k230.rtp_push as rp
    importlib.reload(rp)
    return rp


class FakeClock:
    def __init__(self):
        self.us = 0

    def now_us(self):
        return self.us

    def diff_us(self, a, b):
        return a - b

    def sleep_us(self, us):
        self.us += max(0, int(us))


class FakePacer:
    """纯逻辑替身：只模拟令牌存量与 can_send/take 语义。"""

    def __init__(self, stock):
        self.tokens = float(stock)
        self.burst = float(stock)
        self.calls = []

    def refill(self):
        return self.tokens

    def begin_frame(self):
        pass

    def can_send(self, frame_bytes, npkts):
        return self.tokens >= frame_bytes + npkts * 40

    def take(self, nbytes):
        self.calls.append(nbytes)
        return 0


def test_frame_admission_copies_adaptive():
    rp = _load_k230()
    # P 帧 17.5KB/15 包：单拷 airtime = (17500+15*40)*8/15000kbps ≈ 9.65ms
    # 3 拷 = 28.9ms <= 31.6ms 预算 -> 发 3 拷
    action, copies, air = rp.frame_admission(
        17500, 15, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, None, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_SEND
    assert copies == 3
    assert abs(air - 3 * (17500 + 15 * 40) * 8.0 / 15_000_000 * 1000) < 0.01

    # 大 P 帧：3 拷超 duty，2 拷也超，1 拷 -> 只发 1 拷
    action, copies, air = rp.frame_admission(
        42000, 35, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, None, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_SEND and copies == 1

    # 巨帧连 1 拷都超 duty -> 丢
    action, copies, air = rp.frame_admission(
        200_000, 170, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, None, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_DROP_DUTY and copies == 0

    # I 帧直发（IDR_COPIES 拷）
    action, copies, air = rp.frame_admission(
        200_000, 170, True, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, None, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_SEND and copies == 2


def test_frame_admission_starve_steps_down_copies():
    rp = _load_k230()
    # 令牌只够 2 拷：应自动降 3->2 而不是丢帧
    p = FakePacer(stock=2 * (17500 + 15 * 40))
    action, copies, _ = rp.frame_admission(
        17500, 15, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, p, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_SEND and copies == 2

    # 令牌连 1 拷都不够 -> STARVE
    p = FakePacer(stock=100)
    action, copies, _ = rp.frame_admission(
        17500, 15, False, rp.WIFI_LINK_KBPS, rp.FRAME_INTERVAL_MS,
        rp.MAX_PFRAME_DUTY, p, max_copies=3, idr_copies=2)
    assert action == rp.FRAME_DROP_STARVE and copies == 0


def test_send_paced_bills_copies():
    rp = _load_k230()
    p = FakePacer(stock=1 << 20)
    pkts = [b"x" * 100, b"y" * 50]
    sent = []

    def sink(pkt):
        sent.append(pkt)

    n = rp.send_paced(p, pkts, sink, copies=3)
    assert n == 2 and len(sent) == 2
    # 令牌按 copies×包长计费
    assert p.calls == [300, 150]


def test_fanout_sink_sends_all_ports():
    rp = _load_k230()
    tx = []

    class FakeSock:
        def sendto(self, data, addr):
            tx.append(addr)

    st = rp.TxStats()
    sink = rp.fanout_sink([("h", 8002), ("h", 8003), ("h", 8004)],
                          FakeSock(), st)
    sink(b"1234")
    assert tx == [("h", 8002), ("h", 8003), ("h", 8004)]
    assert st.bytes_tx == 4 * 3
    assert st.pkt_err == 0


def test_fanout_sink_aborts_on_oserror():
    rp = _load_k230()

    class DeadSock:
        def sendto(self, data, addr):
            raise OSError("down")

    st = rp.TxStats()
    sink = rp.fanout_sink([("h", 8002), ("h", 8003)], DeadSock(), st)
    try:
        sink(b"1234")
    except rp.SendAborted:
        pass
    else:
        raise AssertionError("must abort")
    assert st.pkt_err == 1


def test_bandwidth_budget_math():
    rp = _load_k230()
    # 15Mbps 预算下的完整链路账：稳态每帧总 wire 不超过节流池分摊
    wire_kbps = rp.PACER_WIRE_KBPS
    assert wire_kbps == int(15000 * rp.PACER_DUTY)
    p_payload = rp.BIT_RATE * 1000 / 8 // rp.FPS  # ~17.5KB
    npkts = -(-p_payload // 1200)
    single_air = rp.airtime_ms(p_payload, npkts, rp.WIFI_LINK_KBPS)
    assert single_air * rp.LINK_COPIES <= rp.FRAME_INTERVAL_MS * rp.MAX_PFRAME_DUTY


# ---------- relay 多端口扇出集成（真 socket） ----------
def _rtp_pkt(seq=1, marker=True, payload=b"Z" * 20):
    return struct.pack(">BBHII", 0x80, 0x60 | (0x80 if marker else 0),
                       seq & 0xFFFF, 1000, 0x54494E44) + payload


def _bind_ephemeral():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1.0)
    return s


def test_relay_multiport_fanout_and_ctrl():
    from server.rtp_relay import RtpRelay, CONTROL_MAGIC, CTRL_IDR, CTRL_PONG

    relay = RtpRelay([18102, 18103])
    assert relay.start() is True
    try:
        ds = _bind_ephemeral()
        us = _bind_ephemeral()

        # 下游注册（发 CBR ping 到 18102）
        ds.sendto(CONTROL_MAGIC + bytes([0x00]), ("127.0.0.1", 18102))
        # 等 pong
        got_pong = False
        for _ in range(50):
            try:
                data, _ = ds.recvfrom(2048)
            except socket.timeout:
                break
            if data[:3] == CONTROL_MAGIC and data[3] == CTRL_PONG:
                got_pong = True
                break
            time.sleep(0.02)
        assert got_pong

        # K230 两链同 seq 同包分别发到两个入口
        pkt = _rtp_pkt(seq=7)
        us.sendto(pkt, ("127.0.0.1", 18102))
        us.sendto(pkt, ("127.0.0.1", 18103))

        # 下游应收齐 2 份拷贝
        seen = []
        for _ in range(50):
            try:
                data, _ = ds.recvfrom(2048)
            except socket.timeout:
                break
            if (data[0] & 0xC0) == 0x80:
                seen.append(data)
            time.sleep(0.01)
        assert len(seen) == 2 and seen[0] == seen[1] == pkt

        # 下游发 IDR -> 中继转发到上游
        ds.sendto(CONTROL_MAGIC + bytes([CTRL_IDR]), ("127.0.0.1", 18102))
        data, _ = us.recvfrom(2048)
        assert data[:3] == CONTROL_MAGIC and data[3] == CTRL_IDR

        # 状态聚合（pkts_rx 含 CBR ping+IDR，原语义：所有入站数据报都计；
        # 两份同 seq marker 拷贝 -> frame_id 从 -1 加到 1）
        time.sleep(0.1)
        st = relay.status()
        if (st["pkts_rx"], st["pkts_tx"], st["frame_id"]) != (4, 2, 1):
            print("debug status:", st)
        assert st["pkts_rx"] == 4 and st["pkts_tx"] == 2
        assert st["frame_id"] == 1
        assert st["upstreams"]["18102"][0] is not None
        assert st["upstreams"]["18103"][0] is not None
    finally:
        relay.stop()
    assert relay.start() is True  # 停止后可重启
    relay.stop()


def test_relay_port_collision_reports_false():
    from server.rtp_relay import RtpRelay
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("0.0.0.0", 18109))
    relay = RtpRelay([18109])
    try:
        assert relay.start() is False
    finally:
        blocker.close()
        relay.stop()


def main():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
