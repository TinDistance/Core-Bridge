"""丢包率：口径算术 + 中继上游序号统计 + 接收端两级记账（pytest）。

为什么单独一个文件
------------------
丢包率是**跨三层**的一个数：``server/rtp_relay.py`` 数上游缺口、
``desktop/streaming/h264_viewer.py`` 数下游两级、``desktop/telemetry/loss_window.py``
把它们变成窗口值和曲线。少任何一层的字段或口径，数字仍然「看起来正常」——
只是变成了错的数。所以三层各自的行为和它们之间的词汇一致性都放这里。

三条不变量（各有对应用例）
--------------------------
1. **一个序号只记一次丢失**：同一空洞在 ``REORDER_S`` 窗口内被反复观测，
   ``nmiss`` 还会变深；按增量记账才不重复（``test_note_hole_*``）。
2. **两级有包含关系**：``wire >= final``；差值就是重传救回的量。
   恢复率因此恒在 [0, 100]（``test_wire_is_a_superset_of_final``）。
3. **复位不产生负丢包率**：K230 重启 / viewer 重建会让累计值变小，窗口差值
   必须夹到 0（``test_window_clamps_counter_reset_*``）。
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import time
from types import SimpleNamespace

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from desktop.streaming import h264_viewer as HV            # noqa: E402
from desktop.telemetry import latency_monitor as lm         # noqa: E402
from desktop.telemetry import loss_window as LW             # noqa: E402
from server import rtp_relay as RR                          # noqa: E402
from server.routers import video as vid                     # noqa: E402


def _s(t, rx=0, wire=0, final=0, up_rx=0, up_lost=0):
    """构造一条样本快照（鸭子类型，与 LatencySample 同字段名即可）。"""
    return SimpleNamespace(t=t, pkts_rx=rx, wire_lost=wire, final_lost=final,
                           up_rx=up_rx, up_lost=up_lost)


# ============================================================ 窗口算术
def test_window_is_a_plain_counter_delta():
    """10 s 窗口 = 窗口两端累计值之差，不做任何平滑。"""
    ss = [_s(float(i), rx=100 * i, wire=i, final=i // 2) for i in range(21)]
    w = LW.window(ss, span_s=10.0)
    # t=10..20 之间：rx +1000，wire +10，final +5
    assert w["rx"] == pytest.approx(1000.0)
    assert w["wire_lost"] == pytest.approx(10.0)
    assert w["final_lost"] == pytest.approx(5.0)
    assert w["wire_pct"] == pytest.approx(100 * 10 / 1010, abs=1e-3)
    assert w["final_pct"] == pytest.approx(100 * 5 / 1005, abs=1e-3)
    assert w["covered"] is True
    assert w["span_s"] == pytest.approx(10.0)


def test_window_reports_partial_span_instead_of_extrapolating():
    """启动不足一个窗口：不能外推，也不能假装满窗。"""
    ss = [_s(t, rx=100 * int(t), wire=0) for t in range(0, 7)]   # 只覆盖 6 s
    w = LW.window(ss, span_s=10.0)
    assert w["covered"] is False
    assert w["span_s"] == pytest.approx(6.0)
    assert w["samples"] == 7
    # 覆盖不足但有数据时，值仍可用（面板靠 covered 决定要不要标注）
    assert w["wire_lost"] == pytest.approx(0.0)


def test_window_clamps_counter_reset_to_zero_not_negative():
    """K230 重启 → 累计计数器变小 → 窗口差值必须为 0，不能是负数。"""
    ss = [_s(float(t), rx=1000, wire=4, final=2) for t in range(0, 11)]
    # 复位后要**真的收到包**才能验证分母为正的情形，所以计数必须重新爬过基线
    ss += [_s(float(t), rx=10 + (t - 11) * 200, wire=4, final=2)
           for t in range(11, 21)]              # 复位后恢复收流
    w = LW.window(ss, span_s=10.0)
    assert w["rx"] > 0.0
    assert w["wire_lost"] == 0.0
    assert w["final_lost"] == 0.0
    assert w["final_pct"] >= 0.0, "复位绝不能产出负丢包率"
    assert w["recovery_pct"] == LW.UNKNOWN, "复位后无线上丢包 -> 未知而非 0%"


def test_window_with_no_traffic_is_unknown_not_zero():
    """窗口里既没收到也没丢 -> UNKNOWN。画成 0% 会把「没数据」说成「没丢过」。"""
    ss = [_s(float(t), rx=1000) for t in range(0, 11)]
    ss += [_s(float(t), rx=10) for t in range(11, 21)]     # 复位后一片死寂
    w = LW.window(ss, span_s=10.0)
    assert w["rx"] == 0.0
    assert w["final_pct"] == LW.UNKNOWN


def test_window_drops_unreported_samples_instead_of_arithmetic_on_minus_one():
    """-1 是「没上报」哨兵，绝不能参与减法（否则会算出正的假丢包）。"""
    ss = [_s(0.0, rx=100)]
    ss.append(SimpleNamespace(t=1.0, pkts_rx=-1, wire_lost=-1, final_lost=-1,
                              up_rx=-1, up_lost=-1))
    ss.append(_s(2.0, rx=200))
    w = LW.window(ss, span_s=10.0)
    assert w["wire_lost"] == 0.0, "含 -1 的样本必须整条丢弃，而不是当 0 用"
    assert w["samples"] == 2


def test_window_empty_and_zero_span_are_unknown_not_zero():
    assert LW.window([])["final_pct"] == LW.UNKNOWN
    # 所有样本同一时刻：没有时间跨度就没有速率可言
    ss = [_s(5.0, rx=100, wire=3)]
    w = LW.window(ss, span_s=10.0)
    assert w["final_pct"] == LW.UNKNOWN
    assert w["samples"] == 0


def test_zero_rx_with_loss_is_full_loss_not_division_by_zero():
    """窗口里只丢了、没收到过（分母为 lost）—— 必须是 100%，不是崩或 0。"""
    w = LW.window([_s(0.0, rx=0, wire=0, final=0), _s(5.0, rx=0, wire=4, final=4)],
                  span_s=10.0)
    assert w["final_pct"] == 100.0
    assert w["wire_pct"] == 100.0


def test_wire_is_a_superset_of_final_and_recovery_is_bounded():
    """两级包含关系 + 恢复率有界。恢复率是「重传有没有白干」的唯一读数。"""
    assert LW.recovery_pct(0, 0) == LW.UNKNOWN, "没丢过 ≠ 救回 0%"
    assert LW.recovery_pct(10, 0) == 100.0
    assert LW.recovery_pct(10, 10) == 0.0
    assert LW.recovery_pct(10, 3) == 70.0
    ss = [_s(0.0, rx=1000, wire=0, final=0),
          _s(5.0, rx=2000, wire=20, final=5)]
    w = LW.window(ss, span_s=10.0)
    assert w["wire_lost"] >= w["final_lost"]
    assert 0.0 <= w["recovery_pct"] <= 100.0
    assert w["recovery_pct"] == 75.0


def test_totals_survives_a_restart_inside_the_history():
    """累计口径从最早一条起算：中间重启不该把累计丢包清零。"""
    ss = [_s(0.0, rx=1000, wire=8, final=4)]
    ss += [_s(1.0, rx=0, wire=0, final=0)]        # 复位
    ss += [_s(2.0, rx=50, wire=8, final=4)]       # 复位后重新计数
    t = LW.totals(ss)
    assert t["wire_lost"] == 0.0, "复位后的同值不算新增丢失"
    assert t["final_lost"] == 0.0


# ============================================================ 曲线序列
def test_series_buckets_are_ordered_and_right_aligned_to_now():
    ss = [_s(float(i) * 0.5, rx=50 * int(i), wire=int(i % 3 == 0),
             final=int(i % 3 == 0)) for i in range(121)]
    pts = LW.series(ss, span_s=60.0, bucket_s=1.0)
    assert len(pts) == 60, pts[:3]
    assert [t for t, _ in pts] == sorted(t for t, _ in pts), "最老在前"
    assert pts[-1][0] <= ss[-1].t + 1e-9


def test_series_marks_buckets_without_data_as_unknown():
    """桶里只有一条样本 = 没有速率可言 -> UNKNOWN（画成 0% 是在撒谎）。"""
    ss = [_s(float(i), rx=10 * i) for i in range(5)]     # 每秒 1 条
    pts = LW.series(ss, span_s=4.0, bucket_s=1.0)
    assert all(v == LW.UNKNOWN for _t, v in pts), pts


def test_series_rejects_bad_arguments():
    ss = [_s(0.0, rx=1)]
    with pytest.raises(ValueError):
        LW.series(ss, bucket_s=0.0)
    with pytest.raises(ValueError):
        LW.series(ss, metric="up", span_s=1.0), "上游段没有两级口径，不能画"


# ============================================================ 跨层词汇一致
def test_loss_keys_are_identical_across_all_three_layers():
    """三层字段名必须同一套词汇 —— 少一处不会报错，只会显示错数。"""
    assert set(lm.STREAM_METRIC_KEYS) == set(vid.CLIENT_TIMING_KEYS)
    for key in lm.STREAM_METRIC_KEYS:
        assert key in HV.STATS_KEYS, f"上报了 {key}，但 viewer.stats() 没有"


def test_only_the_two_percentages_are_derived_not_stored():
    """上报的 key 里，只有两个百分比是**推导**出来的，不在样本上。

    推导发生在 `_maybe_report_client_timing`（由 wire/final/rx 现算）。这条断言
    存在的意义：新加一个计数器时，如果忘了在 `LatencySample` 上落字段，窗口算术
    会一直拿到 -1，面板永远显示「—」，而全链路没有任何报错。
    """
    derived = {"wire_loss_pct", "final_loss_pct"}
    stored = set(LM_ALL_SAMPLE_FIELDS)
    assert set(lm.STREAM_METRIC_KEYS) - stored == derived
    for key in derived:
        assert key not in stored, f"{key} 应由计数器推导，不该在样本上存快照"


# 探针上报给 server 的 key，必须都能在样本上取到（否则 `_num` 恒给 -1，
# 面板永远显示「—」，而不会有任何报错）
LM_ALL_SAMPLE_FIELDS = frozenset(lm.LatencySample.__dataclass_fields__)


def test_sample_has_every_reported_counter():
    """`LatencySample` 必须带上窗口算术要用的全部累计计数器。"""
    for key in LW.ALL_COUNTERS:
        assert key in LM_ALL_SAMPLE_FIELDS, f"LatencySample 缺累计计数器 {key}"


def test_loss_metrics_are_reported_as_counts_not_rounded_ms():
    """计数型字段不能走 `_coerce_ms`（会变成 1234.0，读起来像带小数的量）。"""
    for key in ("wire_lost", "final_lost"):
        assert key in vid.CLIENT_TIMING_COUNT_KEYS
    assert "wire_loss_pct" not in vid.CLIENT_TIMING_COUNT_KEYS
    assert vid._coerce_count(1234.9) == 1234.0
    assert vid._coerce_count(-1) is None
    assert vid._coerce_count(float("nan")) is None
    assert vid._coerce_count(True) is None
    assert vid._coerce_ms(1234.9) == 1234.9


# ============================================================ 接收端两级记账
def _viewer():
    return HV.H264Viewer("127.0.0.1", 1)


def test_note_hole_counts_each_seq_once_even_when_observed_repeatedly():
    """同一空洞被反复观测（每轮 select 一次），且 nmiss 会变深 ——
    增量记账才不把一个序号记成多次丢失。"""
    v = _viewer()
    v._note_hole(1)
    v._note_hole(1)
    v._note_hole(1)
    assert v._wire_lost == 1, v._wire_lost
    v._note_hole(3)                       # 空洞变深：又丢了 2 个
    assert v._wire_lost == 3, v._wire_lost
    v._end_hole()
    v._note_hole(1)                       # 下一个空洞从 0 起算，不吃上一洞的基线
    assert v._wire_lost == 4, v._wire_lost


def test_note_hole_never_shrinks():
    """nmiss 变小（补包让 ahead 变近）不能把计数拉回来 —— 已经丢了就是丢了。"""
    v = _viewer()
    v._note_hole(5)
    v._note_hole(2)
    assert v._wire_lost == 5, v._wire_lost


def test_end_hole_clears_baseline_so_short_holes_are_not_undercounted():
    """回归风险：u16 的 playhead 每 ~55 s 绕回一次，若靠 playhead 值区分空洞，
    绕回后第一个空洞会拿上一洞的基线比，短空洞被短记。所以基线必须显式清。"""
    v = _viewer()
    for _ in range(3):
        v._note_hole(8)
        v._end_hole()
    assert v._wire_lost == 24, v._wire_lost


def test_viewer_stats_exposes_both_tiers_consistently():
    v = _viewer()
    v._pkts_rx = 1000
    v._wire_lost = 12
    v._lost_pkts = 3
    st = v.stats()
    assert st["wire_lost"] == 12 and st["final_lost"] == 3
    assert st["wire_loss_pct"] == pytest.approx(100 * 12 / 1012, abs=1e-3)
    assert st["final_loss_pct"] == pytest.approx(100 * 3 / 1003, abs=1e-3)
    assert st["recovery_pct"] == 75.0
    assert st["wire_loss_pct"] >= st["final_loss_pct"]


def test_recovery_pct_is_unknown_when_nothing_was_lost():
    """「没丢过」不能显示成「救回 0%」—— 两个结论完全不同。"""
    v = _viewer()
    v._pkts_rx = 500
    assert v.stats()["recovery_pct"] == -1.0
    assert v.stats()["wire_loss_pct"] == 0.0


# ============================================================ 中继上游序号
def _rtp(seq: int, marker: bool = False, ssrc: int = 0x1234) -> bytes:
    b1 = 0x60 | (0x80 if marker else 0)
    return struct.pack(">BBHII", 0x80, b1, seq & 0xFFFF, 0, ssrc) + b"\x00" * 8


def _relay_note(relay, port, seq) -> None:
    """`_note_upstream_seq_locked` 要求持锁，这里如实模拟调用方。"""
    with relay._lock:
        relay._note_upstream_seq_locked(port, seq)


def test_relay_counts_upstream_gaps():
    r = RR.RtpRelay([18110])
    _relay_note(r, 18110, 100)
    _relay_note(r, 18110, 101)
    _relay_note(r, 18110, 105)          # 丢了 102/103/104
    assert r._up_lost == 3
    assert r._up_gaps == 1
    assert r._rtp_rx == 3
    st = r.status()
    assert st["up_lost"] == 3
    assert st["up_rx"] == 3
    assert st["up_loss_pct"] == pytest.approx(50.0)
    assert st["up_seq_ports"] == 1


def test_relay_tracks_seq_per_port_so_multilink_is_not_a_gap():
    """K230 同一 seq 同一包发往多个入口：共享 max_seq 会把另一个端口判成巨大缺口。
    这就是必须按端口分桶的原因（曾经 real bug 的形态）。"""
    r = RR.RtpRelay([18110, 18111])
    for port in (18110, 18111):
        _relay_note(r, port, 7)
    assert r._up_lost == 0, r._up_lost
    _relay_note(r, 18110, 8)
    assert r._up_lost == 0
    _relay_note(r, 18111, 9)             # 只在 18111 上跳了一个
    assert r._up_lost == 1, r._up_lost


def test_relay_treats_same_seq_and_mild_reorder_as_dup_not_loss():
    r = RR.RtpRelay([18110])
    _relay_note(r, 18110, 100)
    _relay_note(r, 18110, 100)           # 同序号重传
    _relay_note(r, 18110, 98)            # 半空间内的乱序
    _relay_note(r, 18110, 101)
    assert r._up_lost == 0
    assert r._up_dup == 2
    assert r._up_resets == 0


def test_relay_counts_upstream_restart_as_reset_not_loss():
    """上游重启（seq 跳回 0）绝不能记成缺口 —— 实测那种口径下一次重启就是
    两万个假丢失，会把丢包率永久污染。"""
    r = RR.RtpRelay([18110])
    _relay_note(r, 18110, 45000)
    _relay_note(r, 18110, 0)
    assert r._up_resets == 1
    assert r._up_lost == 0, r._up_lost
    _relay_note(r, 18110, 1)             # 重同步后继续正常计数
    assert r._up_lost == 0


def test_relay_upstream_pct_is_unknown_before_any_packet():
    r = RR.RtpRelay([18110])
    st = r.status()
    assert st["up_loss_pct"] == -1.0, "没收到包时不能显示 0%（= 没丢过）"
    assert st["up_seq_ports"] == 0
    assert st["up_lost"] == 0


def test_relay_and_viewer_agree_on_the_reset_threshold():
    """两侧常量同值是靠约定维持的（server 不 import desktop），用测试钉死。"""
    assert RR.SEQ_RESET_DISTANCE == HV.SEQ_RESET_DISTANCE


def test_relay_survives_u16_seq_wrap():
    r = RR.RtpRelay([18110])
    _relay_note(r, 18110, 0xFFFF)
    _relay_note(r, 18110, 0x0000)        # 回绕，不算 reset
    assert r._up_resets == 0
    assert r._up_lost == 0
    _relay_note(r, 18110, 0x0003)        # 丢了 1、2
    assert r._up_lost == 2, r._up_lost


def test_relay_stop_clears_seq_baseline_so_restart_is_not_a_giant_gap():
    r = RR.RtpRelay([18110])
    _relay_note(r, 18110, 1000)
    _relay_note(r, 18110, 1001)
    with r._lock:
        r._up_seq.clear()
    _relay_note(r, 18110, 5)             # 重启后从低位重新开始
    assert r._up_lost == 0, r._up_lost


# ============================================================ 端到端两级丢包
class _RingSource:
    """假中继：维护一个「K230 重传环」，用 viewer 的 NACK 回答空洞。

    ``answer_nacks=False`` 就变成黑洞 —— 模拟环形缓冲未命中 / 重传预算被拦
    （K230 侧 ``retransmit_nack`` 的 15% 预算真的会这么干），丢包于是走到第二级
    回退 ``declare_loss``。

    注意 ``hold`` 只是**不发**，仍要入环：``answer_nacks=True`` 时才能补回来。
    """

    def __init__(self, answer_nacks: bool = True) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(5.0)
        self.ring: dict[int, bytes] = {}
        self.viewer = None
        self.answer_nacks = answer_nacks
        self.nacks = 0

    def send(self, pkt, transmit: bool = True) -> int:
        s = struct.unpack_from(">H", pkt, 2)[0]
        self.ring[s] = pkt
        if transmit:
            self.sock.sendto(pkt, self.viewer)
        return s

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        self.sock.settimeout(0.05)
        while time.monotonic() < end:
            try:
                d, _ = self.sock.recvfrom(2048)
            except (socket.timeout, BlockingIOError):
                continue
            if len(d) < 5 or d[:3] != b"CBR" or d[3] != 0x03:
                continue
            self.nacks += 1
            if not self.answer_nacks:
                continue
            count = d[4]
            for i in range(count):
                s = (d[5 + 2 * i] << 8) | d[6 + 2 * i]
                pkt = self.ring.get(s)
                if pkt is not None and self.viewer is not None:
                    self.sock.sendto(pkt, self.viewer)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _start_pair(src):
    v = HV.H264Viewer("127.0.0.1", src.sock.getsockname()[1])
    v.start()
    time.sleep(0.15)
    src.viewer = (v.server_host, v._local_port)
    return v


@pytest.fixture(scope="module")
def aus():
    if HV.av is None:
        pytest.skip("PyAV unavailable")
    return HV._encode_test_aus(320, 240, 40, noise=True)


@pytest.mark.parametrize("answer_nacks,expect_final", [(False, True), (True, False)])
def test_a_hole_moves_the_wire_tier_and_only_an_unanswered_one_moves_final(
        aus, answer_nacks, expect_final):
    """一次真实丢包的两条分支 —— 两级口径是不是同一个数，看这一条就够。

    关键机制：viewer 只有在**后续序号到达**时才发现空洞（丢的是帧尾包时，
    在下一帧到来之前 playhead 一直在 pending 里，不会有人报缺口）。所以用例
    必须再发一帧把空洞暴露出来，否则什么都没丢。
    """
    src = _RingSource(answer_nacks=answer_nacks)
    v = _start_pair(src)
    try:
        seq = 500
        pk, seq = HV._rtp_packets(aus[0], seq, 0)
        for p in pk:
            src.send(p)
        src.pump(0.25)

        # 帧 1 丢掉帧尾包（只入环、不发）
        pk, seq = HV._rtp_packets(aus[1], seq, 3000)
        assert len(pk) >= 3, "需要多包帧才能在帧尾制造空洞"
        for p in pk[:-1]:
            src.send(p)
        src.send(pk[-1], transmit=False)

        # 帧 2 完整送达 -> 空洞暴露 -> NACK
        pk2, seq = HV._rtp_packets(aus[2], seq, 6000)
        for p in pk2:
            src.send(p)
        src.pump(0.30)                     # > REORDER_S(40 ms)

        st = v.stats()
        assert st["wire_lost"] >= 1, ("线上口径必须记账", st)
        assert st["wire_lost"] >= st["final_lost"], st
        assert src.nacks >= 1, "应当发过 NACK"
        if expect_final:
            assert st["final_lost"] >= 1, ("NACK 无人回答时应判丢", st)
            assert st["loss_events"] >= 1, st
            assert 0.0 <= st["recovery_pct"] < 100.0, st
            assert st["final_loss_pct"] > 0.0, st
        else:
            assert st["final_lost"] == 0, ("补回后不应判丢", st)
            assert st["loss_events"] == 0, st
            assert st["recovery_pct"] == 100.0, st
    finally:
        v.stop()
        src.close()
