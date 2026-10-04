"""桌面端 <-> K230 / 服务端 的线路契约回归（pytest）。

为什么这些用例不放在各自的源文件里
--------------------------------
`desktop/streaming/h264_viewer.py`、`desktop/gamepad/pusher.py`、
`desktop/modules/stream_panel.py` 各自带了一套 `_check_*` 自检（可以
`python <file>` 直接跑）。但**跨端字节契约**不属于任何单文件：NACK 要对着
`k230/rtp_push.py` 的 `parse_nack` 验，CBC 要对着
`server/command_udp_in.py` 的 `_parse` 验，遥测 key 要对着
`desktop/telemetry/latency_monitor.py` 与 `server/routers/video.py` 验。
任何一侧改字段而另一侧没跟上，单文件自检**全部照样 PASS** —— 这正是最容易
上线才发现的坑，所以集中放这里。

K230 侧无法 import（模块顶层就 `import _thread` / CanMV），因此用 `ast` 把
`parse_nack` / `build_nack` 两个纯函数从源码里原样抽出来 exec：验的是**固件里
真正那段代码**，而不是审核者重打一遍的副本。
"""
from __future__ import annotations

import ast
import importlib
import os
import socket
import struct
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from desktop.streaming import h264_viewer as HV            # noqa: E402
from desktop.gamepad import pusher as GP                   # noqa: E402
from desktop.gamepad import xinput as XI                   # noqa: E402


# --------------------------------------------------------------- K230 源码抽取
def _k230_function(name: str, globs: dict):
    path = os.path.join(REPO, "k230", "rtp_push.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            ns = dict(globs)
            exec(compile(ast.Module(body=[node], type_ignores=[]), path, "exec"),
                 ns)
            return ns[name]
    raise RuntimeError(f"{name} not found in k230/rtp_push.py")


K230_CTRL = {"CONTROL_MAGIC": b"CBR", "CTRL_NACK": 0x03, "NACK_MAX_SEQS": 32}
k230_parse_nack = _k230_function("parse_nack", K230_CTRL)
k230_build_nack = _k230_function("build_nack", K230_CTRL)


# ============================================================ NACK 线路契约
@pytest.mark.parametrize("count", [1, 2, 5, 16, 31, 32])
def test_nack_length_and_content_match_k230_parser(count):
    """b'CBR' + 0x03 + count(u8) + seq(u16 BE)*count，长度恰好 5+2*count。"""
    seqs = [(1000 + i) & 0xFFFF for i in range(count)]
    dg = HV.encode_nack(seqs)
    assert len(dg) == 5 + count * 2
    assert dg[:3] == b"CBR" and dg[3] == 0x03 and dg[4] == count
    got, ok = k230_parse_nack(dg)
    assert ok, f"K230 rejected our NACK: {dg.hex()}"
    assert got == seqs


@pytest.mark.parametrize("seqs", [
    [0], [0xFFFF], [0xFFFF, 0x0000], [0x8000, 0x7FFF],
    list(range(0xFFF8, 0x10000)),          # 跨 16 位回绕
])
def test_nack_survives_u16_wrap(seqs):
    got, ok = k230_parse_nack(HV.encode_nack(seqs))
    assert ok and got == [s & 0xFFFF for s in seqs]


def test_k230_build_nack_is_byte_identical_to_desktop():
    """两侧构造器必须逐字节一致（K230 侧主要用于联调）。"""
    for chunk in ([0], list(range(32)), list(range(31)), [0xFFFF, 0]):
        assert GP and k230_build_nack(chunk) == HV.encode_nack(chunk)


def test_desktop_never_sends_an_over_long_nack():
    """K230 对长度做**精确**校验，超 32 必须分片，不能截断。"""
    dg = HV.encode_nack(list(range(96)))
    assert dg[4] == 32 and len(dg) == 69
    got, ok = k230_parse_nack(dg)
    assert ok and got == list(range(32))


def test_nack_chunking_covers_every_hole_of_a_deep_gap():
    """96 个空洞 -> 3 个报文，K230 逐个都能解，且并集无重无漏。"""
    over = list(range(96))
    dgs = [HV.encode_nack(over[i:i + HV.NACK_MAX_SEQS])
           for i in range(0, len(over), HV.NACK_MAX_SEQS)]
    assert [d[4] for d in dgs] == [32, 32, 32]
    flat = []
    for d in dgs:
        seqs, ok = k230_parse_nack(d)
        assert ok
        flat += seqs
    assert flat == over


def test_planner_caps_one_round_and_respects_the_rate_limit():
    """单轮不超过 NACK_MAX_SEQS_PER_PLAN；总量由 60 seq/s 限速兜底。"""
    p = HV.NackPlanner(rate_limit_seqs_per_s=0)
    miss = list(range(300, 300 + HV.NACK_MAX_SEQS_PER_PLAN + 60))
    assert p.plan(miss, 1000.0) == []                  # 首见只登记
    out = p.plan(miss, 1000.005)
    assert p.sent_seqs == HV.NACK_MAX_SEQS_PER_PLAN
    assert len(out) == HV.NACK_MAX_SEQS_PER_PLAN // HV.NACK_MAX_SEQS
    for d in out:
        assert k230_parse_nack(d)[1]

    q = HV.NackPlanner(rate_limit_seqs_per_s=60)
    many = list(range(1000, 1200))
    q.plan(many, 300.0)
    total = sum(d[4] for _ in range(6) for d in q.plan(many, 300.030))
    assert total <= 60 and q.dropped > 0


def test_rate_limiter_is_a_true_sliding_window():
    """固定窗口能打出 2x 瞬时；这里必须验证它是滑窗（1s 外的额度会还回来）。"""
    p = HV.NackPlanner(rate_limit_seqs_per_s=10)
    p.plan([1], 0.0)
    assert p.plan([1], 0.005)                       # 用掉 1 个额度
    fresh = list(range(50, 60))
    p.plan(fresh, 1.0)
    assert p.plan(fresh, 1.005), "1s 之后额度必须恢复"


# ============================================================ CBC 命令包契约
def test_cbc_packet_parses_with_the_real_server_parser():
    cui = importlib.import_module("server.command_udp_in")
    for raw16, seq, ts in ((bytes(range(16)), 1, 2),
                           (XI.neutral_protocol(), 0xFFFFFFFF, 0xFFFFFFFF),
                           (bytes(16), 0, 0)):
        pkt = GP.build_cmd_packet(raw16, seq, ts)
        assert len(pkt) == GP.CMDIN_PACKET_LEN == 28
        got = cui._parse(pkt)
        assert got is not None
        assert got == (seq & 0xFFFFFFFF, ts & 0xFFFFFFFF, raw16)


def test_cbc_24_byte_compat_branch_does_not_mis_slice():
    """服务端额外接受缺 ts_ms 的 24 字节包；桌面端不发，但不许错位。"""
    cui = importlib.import_module("server.command_udp_in")
    compat = GP.CMDIN_MAGIC + bytes([1]) + struct.pack(">I", 7) + bytes(16)
    got = cui._parse(compat)
    assert got == (7, None, bytes(16))


def test_cbc_rejects_bad_length_version_and_magic():
    cui = importlib.import_module("server.command_udp_in")
    pkt = GP.build_cmd_packet(bytes(16), 5, 6)
    assert cui._parse(pkt[:-1]) is None
    assert cui._parse(pkt[:3] + b"\x02" + pkt[4:]) is None
    assert cui._parse(b"XBC" + pkt[3:]) is None
    assert GP.parse_cmd_packet(pkt[:-1]) is None


def test_cbc_seq_and_ts_are_big_endian_u32_with_wraparound():
    pkt = GP.build_cmd_packet(bytes(16), 0x1_0000_0005, 0x2_FFFF_FFFF)
    assert pkt[4:8] == b"\x00\x00\x00\x05"
    assert pkt[8:12] == b"\xff\xff\xff\xff"
    assert GP.parse_cmd_packet(pkt) == (5, 0xFFFFFFFF, bytes(16))


def test_cbc_ts_ms_is_process_uptime_not_wall_clock():
    """ts_ms 语义：客户端进程启动以来的毫秒。服务端只把它显示成 client_ts，
    不参与新鲜度判断，所以不需要和服务器对时 —— 但它绝不能是 epoch 秒。"""
    a = GP.uptime_ms()
    assert 0 <= a <= 0xFFFFFFFF
    assert a < 24 * 3600 * 1000, "看起来像 wall-clock，不是 uptime"
    time.sleep(0.02)
    assert GP.uptime_ms() > a


# ============================================================ 遥测 key 完整性
def test_viewer_stats_exposes_exactly_the_telemetry_keys():
    lm = importlib.import_module("desktop.telemetry.latency_monitor")
    vid = importlib.import_module("server.routers.video")
    v = HV.H264Viewer("127.0.0.1", 9)
    st = v.stats()
    for key in lm.STREAM_METRIC_KEYS:
        assert key in st, f"latency_monitor.STREAM_METRIC_KEYS 缺 {key}"
        assert key in vid.CLIENT_TIMING_KEYS, f"/video/timing/client 缺 {key}"
    assert set(lm.STREAM_METRIC_KEYS) == set(vid.CLIENT_TIMING_KEYS), \
        "桌面端与 /video/timing/client 的 key 集合必须一致"
    assert lm.STREAM_PRIMARY_KEY == "render_age_ms"


def test_nack_stats_keys_are_present():
    v = HV.H264Viewer("127.0.0.1", 9)
    for key in ("nack_sent", "nack_seqs", "nack_recovered",
                "nack_rounds_dropped"):
        assert key in v.stats() and key in v.nack_stats()


# ============================================================ set_render_size 契约
def test_set_render_size_contract():
    v = HV.H264Viewer("127.0.0.1", 9)
    assert v.render_size == (0, 0)                    # 默认不缩放
    v.set_render_size(0, 720)
    assert v.render_size == (0, 0)                    # <=0 视为不缩放
    v.set_render_size(10 ** 9, 10 ** 9)
    assert v.render_size == (HV.RENDER_SIZE_MAX, HV.RENDER_SIZE_MAX)
    v.set_render_size("x", "y")
    assert v.render_size == (HV.RENDER_SIZE_MAX, HV.RENDER_SIZE_MAX)


def test_fit_render_size_is_even_contained_and_aspect_correct():
    from desktop.modules.stream_panel import fit_render_size, ratio_changed
    for sw, sh in ((1280, 720), (1920, 1080), (640, 480)):
        for cw, ch in ((1280, 720), (1279, 719), (801, 601), (400, 400)):
            w, h = fit_render_size(sw, sh, cw, ch)
            assert w % 2 == 0 and h % 2 == 0, (sw, sh, cw, ch, w, h)
            assert w >= 2 and h >= 2 and w <= cw and h <= ch
            assert abs(w * sh - h * sw) <= 2 * sw
    assert fit_render_size(1280, 720, 0, 0) == (0, 0)
    assert ratio_changed(640, 480, 1280, 720)
    assert not ratio_changed(1920, 1080, 1280, 720)


# ================================== 接收端：seq 重置（本轮修复的活判据）
def _rtp_packets(au: bytes, seq: int, ts: int):
    """直接复用 h264_viewer 自带的打���器 —— 不要重打一遍：marker 位必须只打在
    **最后一个 NAL 的最后一个分片**上，重打很容易漏掉 last_nal 判定。"""
    return HV._rtp_packets(au, seq, ts)


class _Source:
    """假中继：把 viewer 的 NACK 当作 K230 环形缓冲命中来回答。

    注意：本类**直连** viewer，所以 viewer 的第一个 IDR 请求一定能送到 ——
    真实链路里它必然丢在 relay 上（上游还没学到）。因此 IDR 重试这条用例
    不能断言「请求次数 >= N」（one-shot 实现的 1Hz ping 也能凑够），必须
    断言**请求间隔上界**，那才是真正的要求。
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(5.0)
        self.ring: dict[int, bytes] = {}
        self.viewer = None
        self.idr = 0
        self.idr_at: list[float] = []

    def send(self, pkt, transmit=True):
        s = struct.unpack_from(">H", pkt, 2)[0]
        self.ring[s] = pkt
        if transmit:
            self.sock.sendto(pkt, self.viewer)

    def pump(self, seconds):
        end = time.monotonic() + seconds
        self.sock.settimeout(0.05)
        while time.monotonic() < end:
            try:
                d, _ = self.sock.recvfrom(2048)
            except (socket.timeout, BlockingIOError):
                continue
            if len(d) < 4 or d[:3] != b"CBR":
                continue
            if d[3] == 0x01:
                self.idr += 1
                self.idr_at.append(time.monotonic())
            elif d[3] == 0x03:
                seqs, ok = k230_parse_nack(d)
                if not ok:
                    continue
                for s in seqs:
                    pkt = self.ring.get(s)
                    if pkt is not None and self.viewer is not None:
                        self.sock.sendto(pkt, self.viewer)

    def max_idr_gap(self) -> float:
        return max((b - a for a, b in zip(self.idr_at, self.idr_at[1:])),
                   default=0.0)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture(scope="module")
def aus():
    if HV.av is None:
        pytest.skip("PyAV unavailable")
    return HV._encode_test_aus(320, 240, 40, noise=True)


def test_seq_restart_resyncs_without_polluting_final_loss(aus):
    """K230 脚本重启 -> seq 跳回 0。

    这是现场真实场景。三件事必须成立：
      1. 能重新同步（帧数继续增长）；
      2. `final_lost` 不被伪空洞灌水（旧实现一次就 +2 万，`final_loss_pct`
         永久失真），`wire_lost` 同样不能 —— 两者都在 seq 重置分支**之后**记账；
      3. `seq_resets` 计数器真的会涨 —— 原先那两处判据互斥 / 取不到，是死代码。
    """
    src = _Source()
    v = HV.H264Viewer("127.0.0.1", src.sock.getsockname()[1])
    v.start()
    try:
        time.sleep(0.15)
        src.viewer = (v.server_host, v._local_port)
        seq = 45000
        for i in range(3):
            pk, seq = _rtp_packets(aus[i], seq, i * 3000)
            for p in pk:
                src.send(p)
            src.pump(0.05)
        before = v.stats()
        assert before["frames"] >= 2, before

        seq = 0                                   # K230 脚本重启
        t0 = time.monotonic()
        for i in range(6):
            pk, seq = _rtp_packets(aus[i], seq, (900 + i) * 3000)
            for p in pk:
                src.send(p)
            src.pump(0.04)
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            src.pump(0.05)
            if v.stats()["frames"] >= before["frames"] + 3:
                break
        after = v.stats()

        assert after["frames"] > before["frames"], \
            f"没有重新同步: {before['frames']} -> {after['frames']}"
        assert after["seq_resets"] > before["seq_resets"], \
            "seq 重置没有计数（死代码回归）"
        for key in ("final_lost", "wire_lost"):
            assert after[key] - before[key] < 200, \
                f"{key} 被伪空洞灌水: +{after[key]-before[key]}"
        assert time.monotonic() - t0 < 1.5, "重同步太慢"
    finally:
        v.stop()
        src.close()


def test_trim_backlog_does_not_preempt_an_in_flight_nack(aus):
    """回归：字节预算触发的 trim_backlog 不得作废正在进行的 NACK 恢复。

    旧行为（冷启动、bps 未测出 -> 预算取 64KB 下限，而 T1 的 IDR 帧就有
    50~100KB）：IDR 帧里丢 1 个包 -> trim 触发、**一个包都没丢**，却置
    wait_idr=True 并请求 IDR -> `drop_events=1 / drops=0 / frames=0`，
    整个首帧黑屏，还要再等一次 IDR 往返。
    """
    src = _Source()
    v = HV.H264Viewer("127.0.0.1", src.sock.getsockname()[1])
    v.set_render_size(320, 240)
    v.start()
    try:
        time.sleep(0.15)
        src.viewer = (v.server_host, v._local_port)
        pkts, _ = _rtp_packets(aus[0], 0, 0)
        assert sum(len(p) for p in pkts) > HV.BACKLOG_BUDGET_MIN_BYTES, \
            "本用例需要一帧就超过冷启动字节预算的素材"
        for j, p in enumerate(pkts):
            src.send(p, transmit=(j != 1))          # 第 2 个包在空中丢了
        src.pump(1.2)
        st = v.stats()
        assert st["frames"] >= 1, f"首帧黑屏: {st}"
        assert st["drop_events"] == 0, \
            f"trim 在 NACK 还在飞的时候触发了: {st}"
        assert st["loss_events"] == 0, \
            f"不该走 declare_loss 回退（NACK 已补上）: {st}"
        assert st["nack_sent"] >= 1 and st["nack_recovered"] >= 1, st
    finally:
        v.stop()
        src.close()


def test_trim_backlog_still_drops_genuinely_stale_packets(aus):
    """反向保护：真正的积压仍然必须被裁掉（否则修复把保护架空了）。

    把渲染目标放大到 1080p 让解码侧变慢，再不给节流地灌 120 帧，
    队列深度就会超过 BACKLOG_KEEP_MS = 150ms，trim 必须真的丢包。
    """
    pytest.importorskip("av")
    src = _Source()
    v = HV.H264Viewer("127.0.0.1", src.sock.getsockname()[1])
    v.set_render_size(1920, 1080)
    v.start()
    try:
        time.sleep(0.15)
        src.viewer = (v.server_host, v._local_port)
        seq = 0
        for i in range(120):
            pk, seq = _rtp_packets(aus[i % len(aus)], seq, i * 3000)
            for p in pk:
                src.send(p)
        end = time.monotonic() + 6.0
        while time.monotonic() < end:
            src.pump(0.05)
            if v.stats()["drop_events"] > 0 and v.stats()["backlog_packets"] < 40:
                break
        st = v.stats()
        assert st["drop_events"] > 0 and st["drops"] > 0, \
            f"trim_backlog 不再裁剪积压，保护被架空了: {st}"
    finally:
        v.stop()
        src.close()


def test_viewer_keeps_requesting_idr_while_starved(aus):
    """回归：等 IDR 期间必须**持续**请求，而不是只发一次。

    旧行为：首帧前那一次 IDR 请求必然发不出去 —— `rtp_relay._on_control` 只把
    CBR 转发给 `age <= UPSTREAM_STALE_S` 的上游，而上游是 viewer 起来之后由
    第一个 RTP 包学到的（实测 ~3ms 之后）。之后 `idr_until_frame` 变 False，
    1Hz 的 ping 只发 CTRL_PING（中继回 PONG，viewer 不看），**再也不会上行
    IDR**。于是首帧只能等 K230 的下一个周期 IDR —— GOP_LEN=60 时最长 2s。
    实测（真实 server.app 子进程 + 假 K230）：整个会话 `idr_req == 0`。
    """
    mid = aus[:20]                      # keyint=30 -> AU[0] 之后仍无 IDR
    assert all(5 not in [x[0] & 0x1F for x in HV._annexb_nals(a)]
               for a in mid[1:]), "素材必须在 AU[0] 之后不含 IDR"

    src = _Source()
    v = HV.H264Viewer("127.0.0.1", src.sock.getsockname()[1])
    v.set_render_size(320, 240)
    v.start()
    try:
        time.sleep(0.15)
        src.viewer = (v.server_host, v._local_port)
        seq = 20000
        # 饿死 1.6s：one-shot 实现里 1Hz 的 ping 只发 2 个 IDR/秒，
        # 中间会出现 ~1s 的空档；重试实现受 IDR_RETRY_S(200ms) 约束。
        for i in range(1, 16):                       # 只有 P 帧
            pk, seq = _rtp_packets(mid[i], seq, i * 3000)
            for p in pk:
                src.send(p)
            src.pump(0.1)
        st = v.stats()
        assert st["frames"] == 0, "只喂 P 帧时不应出画"
        assert st["dropped_wait_idr"] > 0
        assert src.idr >= 4, f"只请求了 {src.idr} 次 IDR"
        assert src.max_idr_gap() < 0.45, (
            f"IDR 请求之间出现了 {src.max_idr_gap()*1000:.0f} ms 空档 —— "
            f"one-shot 实现在这里会等满一个 PING_INTERVAL_S(1000ms)；"
            f"请求时刻={[round((t - src.idr_at[0])*1000) for t in src.idr_at]}")
    finally:
        v.stop()
        src.close()


# ============================================================ D8 过期包过滤
def test_stale_seq_classification():
    assert not HV._is_stale_seq(100, 100)
    assert not HV._is_stale_seq(101, 100)
    assert HV._is_stale_seq(99, 100)
    assert HV._is_stale_seq(0xFFFF, 0)               # 回绕
    assert not HV._is_stale_seq(0, 0xFFFF)
    assert not HV._is_stale_seq(123, None)


def test_seq_reset_guards_were_dead_code():
    """`max_seq` 只在 `_seq_after` 为真时前进，而它要求差值落在 (0, 0x8000)，
    所以 `nmiss` 与 `backlog_packets` 恒小于半个序号空间 —— 旧的两处判据
    （收包处的两条件互斥、以及 backlog > SEQ_RESET_DISTANCE）都取不到。
    这条用例把结论钉死，防止有人「修」回去。"""
    for _ in range(2000):
        max_seq = 40000
        seq = (max_seq + 1) & 0xFFFF
        if HV._seq_after(seq, max_seq):
            max_seq = seq
        assert (max_seq - 40000) & 0xFFFF < 0x8000
        assert (max_seq - 40001) & 0xFFFF < 0x8000
    assert HV.SEQ_RESET_MIN_DISTANCE < HV.SEQ_RESET_DISTANCE
