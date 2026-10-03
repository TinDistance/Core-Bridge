# K230 发送端节流器 / I 帧 airtime 守卫 / IDR 请求合并 的纯逻辑仿真。
#
# 为什么不用真机：K230 的 rtp_push.py 顶层 import network / media.*，
# 本机（Windows + CPython）无法 import。本测试只抽取 rtp_push.py 里
# `# >>> PURE-PACER-CORE v1 >>>` 与 `# <<< PURE-PACER-CORE v1 <<<` 之间
# 的源码，注入假时钟后 exec —— 单一事实来源仍然是 rtp_push.py 本身。
#
# 本测试**不占用任何 UDP 端口，不发包，不读真机**。它验证的是判定逻辑，
# 不能替代真机验证（真机要看的指标见 rtp_push.py 的 stat 输出）。

import os
import re
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "k230", "rtp_push.py")

BEGIN = "# >>> PURE-PACER-CORE v1 >>>"
END = "# <<< PURE-PACER-CORE v1 <<<"
CFG_BEGIN = "# ==================== 配置区 ===================="
CFG_END = "# ================================================"


def _slice(text, begin, end, last=False):
    """截取 begin...end 之间的源码。

    last=True 时取**最后一个** end：配置区有两个并列的
    「=...= 常量块 =...=」，节流器常量在第二个块里。
    """
    i = text.index(begin) + len(begin)
    j = text.rindex(end) if last else text.index(end, i)
    assert i < j, "标记 %r ... %r 顺序异常：%s" % (begin, end, SRC)
    return text[i:j]


def _load_core():
    """抽取纯逻辑段并 exec 到带假时钟的命名空间。"""
    with open(SRC, "r", encoding="utf-8") as f:
        text = f.read()
    ns = {"time": time}
    exec(compile(_slice(text, BEGIN, END), SRC, "exec"), ns)
    return ns


def _load_cfg():
    """抽取 rtp_push.py 的配置区（纯算术，无 K230 依赖）并 exec。

    这样测试里的每个数字都直接来自 rtp_push.py，不需要抄一份常量 ——
    抄一份必然漂移，漂移后的测试就是假绿。
    """
    with open(SRC, "r", encoding="utf-8") as f:
        text = f.read()
    ns = {}
    exec(compile(_slice(text, CFG_BEGIN, CFG_END, last=True), SRC, "exec"), ns)
    return ns


CORE = _load_core()
CFG = _load_cfg()
Pacer = CORE["Pacer"]
IdrGate = CORE["IdrGate"]
airtime_ms = CORE["airtime_ms"]
ms_diff = CORE["ms_diff"]
PacerWireOverhead = CORE["PacerWireOverhead"]
TxStats = CORE["TxStats"]
frame_admission = CORE["frame_admission"]
send_paced = CORE["send_paced"]
SendAborted = CORE["SendAborted"]
FRAME_SEND = CORE["FRAME_SEND"]
FRAME_DROP_DUTY = CORE["FRAME_DROP_DUTY"]
FRAME_DROP_STARVE = CORE["FRAME_DROP_STARVE"]


def admit(p, frame_bytes, npkts, is_idr):
    return frame_admission(frame_bytes, npkts, is_idr, CFG["LINK_KBPS"],
                           CFG["FRAME_INTERVAL_MS"], CFG["MAX_PFRAME_DUTY"], p)


class FakeClock:
    """假时钟：sleep 只推进虚拟时间，不真的阻塞。"""

    def __init__(self, start_us=0):
        self.now = start_us
        self.sleep_calls = 0
        self.slept_us = 0

    def now_us(self):
        return self.now

    def diff_us(self, a, b):
        return a - b

    def sleep_us(self, us):
        us = int(us)
        assert us > 0, "Pacer.sleep_us(<=0) 会退化成忙等"
        assert us <= 20000, "单次 sleep 超过 20ms 会挡住事件循环"
        self.now += us
        self.slept_us += us
        self.sleep_calls += 1


def pkt_wire(payload):
    """一个 RTP 包在链路上占多少字节（payload + 40B 头）。"""
    return payload + PacerWireOverhead


def pkt_gap_us(rate_kbps, payload=None):
    """目标线速下相邻包的最小间隔（us）。"""
    if payload is None:
        payload = CFG["MAX_PAYLOAD"]
    return pkt_wire(payload) * 8.0 / (rate_kbps * 1000.0) * 1000000.0


def new_pacer(clock=None, rate_kbps=None):
    """按 rtp_push.py 的真实配置造一个 pacer。"""
    clock = clock or FakeClock()
    p = Pacer(rate_kbps or CFG["PACER_WIRE_KBPS"], CFG["PACER_BURST_BYTES"],
              clock, CFG["PACER_SPAN_MAX_MS"])
    return p, clock


class TestConfigIsSelfConsistent(unittest.TestCase):
    """配置区的数字必须互相自洽，否则下面的仿真全是白跑。"""

    def test_derived_constants_match_formulas(self):
        self.assertEqual(CFG["PACER_WIRE_KBPS"],
                         int(CFG["LINK_KBPS"] * CFG["PACER_DUTY"]))
        self.assertEqual(CFG["FRAME_INTERVAL_MS"], 1000 // CFG["FPS"])
        self.assertEqual(
            CFG["PACER_BURST_BYTES"],
            int(CFG["PACER_WIRE_KBPS"] * 1000 / 8.0 * 2
                * CFG["FRAME_INTERVAL_MS"] / 1000.0))
        self.assertGreaterEqual(
            CFG["OUT_BUFS"],
            -(-CFG["PACER_SPAN_MAX_MS"] // CFG["FRAME_INTERVAL_MS"]) + 3)

    def test_span_max_covers_a_realistic_keyframe(self):
        """span 上限必须能摊平一个 100KB 的 I 帧，否则守卫名存实亡。"""
        air_ms = airtime_ms(100 * 1024, 100 * 1024 // CFG["MAX_PAYLOAD"],
                            CFG["LINK_KBPS"])
        self.assertLessEqual(
            air_ms, CFG["PACER_SPAN_MAX_MS"],
            "100KB 的 I 帧需要 %.0fms 节流，但 span 上限只有 %dms —— "
            "最该摊平的帧反而会突发" % (air_ms, CFG["PACER_SPAN_MAX_MS"]))

    def test_old_pacer_constants_would_not_have_worked(self):
        """旧参数留档：2760pps / 184 token，在真实负载下必然永不触发。"""
        old_rate = CFG["IFRAME_PKTS_MAX"] * CFG["FPS"] // 2
        old_burst = CFG["IFRAME_PKTS_MAX"]
        self.assertEqual(old_rate, 2760)
        self.assertEqual(old_burst, 184)
        # 补充速率必须显著低于消耗速率才可能限速；这里正好相反。
        self.assertGreater(old_rate, 4 * 390)


class TestPacerRealRateLimit(unittest.TestCase):
    """核心断言：修好之后的 pacer 是不是真的在限速。"""

    def _send(self, p, clock, n, begin_every=13, drain=False):
        """发 n 个包，记录每个包发出时的绝对时间戳。"""
        if drain:
            p.tokens = 0.0
        stamps = []
        for i in range(n):
            if begin_every and i % begin_every == 0:
                p.begin_frame()
            p.take(CFG["MAX_PAYLOAD"])
            stamps.append(clock.now_us())
        return stamps

    def test_1000_packets_respect_min_gap(self):
        """连续 1000 个包：相邻包间隔 >= 目标线速推出的最小间隔。"""
        p, clock = new_pacer()
        stamps = self._send(p, clock, 1000, drain=True)
        target = pkt_gap_us(CFG["PACER_WIRE_KBPS"])
        worst = None
        for i in range(1, len(stamps)):
            gap = stamps[i] - stamps[i - 1]
            if worst is None or gap < worst[0]:
                worst = (gap, i)
        self.assertGreaterEqual(
            worst[0], target - 1.0,
            "第 %d 个包只隔了 %.3fms，低于目标线速 %.3fms" % (
                worst[1], worst[0] / 1000.0, target / 1000.0))
        self.assertGreater(p.wait_us, 0,
                           "1000 个包竟然一次都没等 —— pacer 又变回空转了")

    def test_old_pacer_would_have_never_waited(self):
        """反证：旧参数(2760pps / 桶 184)在同一负载上等待次数必须为 0。

        这是"旧 pacer 形同虚设"的机器可验证版本。旧 pacer 按包数计费。
        """
        old_rate = CFG["IFRAME_PKTS_MAX"] * CFG["FPS"] // 2
        old_burst = float(CFG["IFRAME_PKTS_MAX"])
        interval_us = 1000000.0 / CFG["FPS"]
        tokens, last, waits, sent = old_burst, 0.0, 0, 0
        for f in range(300):
            npkts = 98 if f % CFG["GOP_LEN"] == 0 else 13
            for i in range(npkts):
                now = f * interval_us + i
                tokens = min(old_burst, tokens + (now - last) * old_rate / 1e6)
                last = now
                if tokens < 1:
                    waits += 1
                tokens -= 1
                sent += 1
        self.assertGreater(sent, 3000)
        self.assertEqual(waits, 0,
                         "旧 pacer 在这个负载下居然会等待，结论需要重算")

    def test_new_pacer_ceiling_matches_link_duty(self):
        """桶耗尽时，实际发包率被压在目标线速附近（±5%）。"""
        p, clock = new_pacer()
        n = 400
        stamps = self._send(p, clock, n, drain=True)
        elapsed_ms = (stamps[-1] - stamps[0]) / 1000.0
        observed_pps = n * 1000.0 / elapsed_ms
        ceiling_pps = 1000000.0 / pkt_gap_us(CFG["PACER_WIRE_KBPS"])
        self.assertLessEqual(observed_pps, ceiling_pps * 1.05)
        # 也不该浪费太多带宽：至少跑到上限的 90%
        self.assertGreaterEqual(observed_pps, ceiling_pps * 0.90)
        # 线速利用率应落在 duty 附近，而不是 5000Kbps
        wire_kbps = observed_pps * pkt_wire(CFG["MAX_PAYLOAD"]) * 8 / 1000.0
        self.assertLess(wire_kbps, CFG["LINK_KBPS"] * 0.90,
                        "节流后仍在用 %dKbps，等于没节流" % wire_kbps)

    def test_take_accounts_wire_overhead(self):
        """令牌必须按 payload+40B 计，而不是只按 payload。"""
        p, clock = new_pacer()
        p.tokens = float(pkt_wire(1200)) - 1.0
        waited = p.take(1200)
        self.assertGreater(waited, 0, "没算每包 40B 头开销")
        self.assertEqual(pkt_wire(1200), 1240)

    def test_take_without_begin_frame_still_paces(self):
        """忘调 begin_frame() 时必须仍然节流（fail-safe）。"""
        p, clock = new_pacer()
        p.tokens = 0.0
        p.take(1200)
        self.assertGreater(p.wait_us, 0,
                           "span_left 初值为 0 会让 take() 直接放行 —— "
                           "这是把守卫整个绕过的隐患")

    def test_content_rate_is_under_pacer_ceiling(self):
        """3.07Mbps 内容 / 1240B = 309pps < 428pps 上限：稳态由内容限速。

        若这条不成立（码率被调高），pacer 会变成常态限速器，
        那说明必须改码率 —— 超出本 agent 红线的决策，报告里说明。
        """
        content_pps = CFG["BIT_RATE"] * 1000 / 8.0 / pkt_wire(1200)
        ceiling_pps = 1000000.0 / pkt_gap_us(CFG["PACER_WIRE_KBPS"])
        self.assertLess(content_pps, ceiling_pps)
        self.assertLess(content_pps * pkt_wire(1200) * 8 / 1000.0,
                        CFG["LINK_KBPS"])


class TestKeyframeAirtimeGuard(unittest.TestCase):
    """I 帧必须同样受 airtime 守卫，且突发有上限。"""

    def _stamps(self, p, clock, npkts, drain=True):
        if drain:
            p.tokens = 0.0
        p.begin_frame()
        stamps = []
        for _ in range(npkts):
            p.take(CFG["MAX_PAYLOAD"])
            stamps.append(clock.now_us())
        return stamps

    def test_keyframe_is_paced_not_burst(self):
        """100KB 的 I 帧：包间隔必须被拉开，不能一次性灌进队列。"""
        p, clock = new_pacer()
        npkts = 100 * 1024 // CFG["MAX_PAYLOAD"]
        stamps = self._stamps(p, clock, npkts)
        target = pkt_gap_us(CFG["PACER_WIRE_KBPS"])
        for i in range(1, len(stamps)):
            gap = stamps[i] - stamps[i - 1]
            self.assertGreaterEqual(
                gap, target - 1.0,
                "I 帧第 %d 包只隔 %.3fms（目标 %.3fms）—— 关键帧绕过守卫了" % (
                    i, gap / 1000.0, target / 1000.0))
        self.assertEqual(p.cap_events, 0,
                         "100KB 的 I 帧不该触发 span 截断（见 "
                         "test_span_max_covers_a_realistic_keyframe）")

    def test_span_cap_bounds_blocking_time(self):
        """超大 I 帧（300KB）：单帧节流时长不得超过 PACER_SPAN_MAX_MS。"""
        p, clock = new_pacer()
        stamps = self._stamps(p, clock, 300 * 1024 // CFG["MAX_PAYLOAD"])
        span_ms = (stamps[-1] - stamps[0]) / 1000.0
        self.assertLessEqual(span_ms, CFG["PACER_SPAN_MAX_MS"] + 5.0,
                             "单帧阻塞 %.0fms，会把编码器 outbuf 撑爆" % span_ms)
        self.assertGreater(p.cap_events, 0, "应该记录 span 截断事件")

    def test_cap_creates_debt_that_starves_following_pframes(self):
        """span 耗尽后 token 转负 -> 后续 P 帧被 can_send 预检丢掉。

        这就是"关键帧优先但不无节制突发"的落地：不允许连续两个大突发。
        """
        p, clock = new_pacer()
        self._stamps(p, clock, 300 * 1024 // CFG["MAX_PAYLOAD"])
        self.assertLess(p.tokens, 0.0, "span 耗尽后应留下 token 债务")
        pframes = 13 * 1024 // CFG["MAX_PAYLOAD"]
        self.assertFalse(p.can_send(pframes * 1200, pframes),
                         "I 帧突发后紧随的 P 帧竟然还能过预检")
        # 债务必须能被填回来（不能永久饿死）
        p.refill()
        debt_ms = -p.tokens * 8 / (CFG["PACER_WIRE_KBPS"] * 1000.0) * 1000.0
        self.assertLess(debt_ms, 500,
                        "债务 %.0fms 太久，P 帧会长时间全丢" % debt_ms)

    def test_p_frames_pass_when_no_keyframe_debt(self):
        """稳态（无突发）下 P 帧必须能过预检，否则等于把流掐死。"""
        p, clock = new_pacer()
        p.tokens = float(CFG["PACER_BURST_BYTES"])
        avg = CFG["BIT_RATE"] * 1000 / CFG["FPS"] / 8.0
        npkts = int(avg) // CFG["MAX_PAYLOAD"] + 1
        for _ in range(50):
            self.assertTrue(p.can_send(int(avg), npkts))
            p.begin_frame()
            for _ in range(npkts):
                p.take(CFG["MAX_PAYLOAD"])
            clock.now += CFG["FRAME_INTERVAL_MS"] * 1000

    def test_airtime_math(self):
        """airtime = (payload + npkts*40) * 8 / link_kbps。"""
        self.assertAlmostEqual(
            airtime_ms(12000, 13, CFG["LINK_KBPS"]),
            (12000 + 13 * 40) * 8 / CFG["LINK_KBPS"])
        avg = CFG["BIT_RATE"] * 1000 / CFG["FPS"] / 8.0
        npkts = int(avg) // CFG["MAX_PAYLOAD"] + 1
        air = airtime_ms(int(avg), npkts, CFG["LINK_KBPS"])
        # 平均帧 airtime ≈ 21ms，占 33ms 帧间隔的 ~63%（与源注释的 61% 一致），
        # 且必须低于 MAX_PFRAME_DUTY 守卫预算，否则平均帧自己就会被丢。
        self.assertAlmostEqual(air, 21.0, places=0)
        self.assertLess(air, CFG["FRAME_INTERVAL_MS"] * CFG["MAX_PFRAME_DUTY"])


class TestStreamLoopWiring(unittest.TestCase):
    """守住最关键的一条：关键帧不能绕过 airtime 守卫。

    这组测试存在的理由：曾经把 stream_loop 里的 `pacer.take(len(pkt))`
    删掉换成 no-op，只测 Pacer 类的用例**全部照样通过** —— 类是对的，
    但发送路径没在用它。mutation 验证：改坏 take() 会被
    test_keyframe_goes_through_send_paced 抓住。
    """

    def _packets(self, n):
        return [b"\x00" * CFG["MAX_PAYLOAD"] for _ in range(n)]

    def test_keyframe_goes_through_send_paced(self):
        """100KB 关键帧经 send_paced 发出：每个包都被节流，最小间隔达标。"""
        p, clock = new_pacer()
        p.tokens = 0.0
        npkts = 100 * 1024 // CFG["MAX_PAYLOAD"]
        stamps = []
        sent = send_paced(p, [self._packets(1)[0] for _ in range(npkts)],
                          lambda pkt: stamps.append(clock.now_us()))
        self.assertEqual(sent, npkts)
        target = pkt_gap_us(CFG["PACER_WIRE_KBPS"])
        for i in range(1, len(stamps)):
            self.assertGreaterEqual(
                stamps[i] - stamps[i - 1], target - 1.0,
                "I 帧第 %d 包间隔 %.3fms < 目标 %.3fms —— 守卫被绕过了" % (
                    i, (stamps[i] - stamps[i - 1]) / 1000.0, target / 1000.0))
        self.assertEqual(p.cap_events, 0)

    def test_keyframe_admission_is_never_a_drop(self):
        """关键帧即使令牌为 0、airtime 爆表，也必须是 FRAME_SEND。"""
        p, _ = new_pacer()
        p.tokens = 0.0
        # 300KB / airtime 480ms，远超 33ms 帧间隔预算
        action, air = admit(p, 300 * 1024, 256, True)
        self.assertEqual(action, FRAME_SEND,
                         "关键帧被准入判定拒了 —— 桌面端会永远 wait_idr")
        self.assertGreater(air, CFG["FRAME_INTERVAL_MS"])

    def test_pframe_drops_for_duty_and_starvation(self):
        p, _ = new_pacer()
        avg = int(CFG["BIT_RATE"] * 1000 / CFG["FPS"] / 8.0)
        npkts = avg // CFG["MAX_PAYLOAD"] + 1
        # 满桶：平均 P 帧必须放行
        self.assertEqual(admit(p, avg, npkts, False)[0], FRAME_SEND)
        # 超 duty 预算 -> 丢
        big = int(avg * 2)
        self.assertEqual(admit(p, big, big // CFG["MAX_PAYLOAD"] + 1,
                               False)[0], FRAME_DROP_DUTY)
        # 桶被 I 帧抽干 -> 饿死（注意要用小于 duty 预算的尺寸，
        # 否则会先被 duty 拦下，测的就不是 starve 分支了）
        p2, _ = new_pacer()
        p2.tokens = -10000.0
        self.assertEqual(admit(p2, avg, npkts, False)[0], FRAME_DROP_STARVE)

    def test_send_paced_aborts_on_sink_error(self):
        """sink 抛 SendAborted 必须立刻停止本帧剩余包。"""
        p, clock = new_pacer()
        got = []

        def sink(pkt):
            got.append(pkt)
            if len(got) == 5:
                raise SendAborted()

        with self.assertRaises(SendAborted):
            send_paced(p, self._packets(20), sink)
        self.assertEqual(len(got), 5, "sink 报错后还在继续发包")

    def test_span_budget_resets_per_frame(self):
        """连续多个关键帧：span 预算必须每帧重置，不能跨帧累积。

        少了 send_paced 里的 begin_frame() 时，第一帧用掉 189ms 后预算只剩
        11ms，第二帧就会立刻触发 cap —— 于是"摊平 I 帧"退化回突发。
        """
        p, clock = new_pacer()
        p.tokens = 0.0
        npkts = 100 * 1024 // CFG["MAX_PAYLOAD"]
        pkts = [b"\x00" * CFG["MAX_PAYLOAD"] for _ in range(npkts)]
        for n in range(3):
            before = p.cap_events
            stamps = []
            send_paced(p, pkts, lambda pkt: stamps.append(clock.now_us()))
            self.assertEqual(p.cap_events, before,
                             "第 %d 个关键帧触发了 span 截断 —— 每帧预算"
                             "没有重置（begin_frame 丢了？）" % (n + 1))
            self.assertGreater(len(stamps), 0)

    def test_stream_loop_actually_calls_the_paced_path(self):
        """源码级守卫：stream_loop 必须走 send_paced/frame_admission。

        防止有人把节流内联回主循环、又只对 P 帧生效（历史 bug 的形态）。
        """
        with open(SRC, "r", encoding="utf-8") as f:
            text = f.read()
        body = text[text.index("def stream_loop("):]
        body = body[:body.index("\ndef cleanup(")]
        self.assertIn("send_paced(pacer, packets", body)
        self.assertIn("frame_admission(", body)
        # 主循环里不能有绕过 send_paced 的裸 sendto：所有 sendto 必须
        # 收敛在 _sink 这一个注入点里（其中 2 处是 TypeError 的 bytes 重试）。
        self.assertIn("def _sink(", body)
        before_sink, sink_and_after = body.split("def _sink(", 1)
        self.assertNotIn("sendto(", before_sink,
                         "send_paced 之外还有裸 sendto，发送路径被分叉了")
        self.assertEqual(sink_and_after.count("sendto("), 2)

    def test_pacer_take_is_inside_send_paced_only(self):
        """take() 只允许出现在 send_paced 与 Pacer 自身。"""
        with open(SRC, "r", encoding="utf-8") as f:
            text = f.read()
        core = _slice(text, BEGIN, END)
        self.assertEqual(core.count("pacer.take("), 1,
                         "节流点必须唯一，否则又会出现'某类帧不走守卫'")
        self.assertIn("pacer.take(len(pkt))", core)


class TestIdrRequestCoalescing(unittest.TestCase):
    """IDR 请求风暴必须在发送端被压住。"""

    def setUp(self):
        self.g = IdrGate(CFG["IDR_REQ_MIN_INTERVAL_MS"],
                         CFG["IDR_REQ_SETTLE_MS"])

    def test_50_requests_in_1s_are_coalesced(self):
        """1 秒内 50 次请求：有自然 IDR 时只能生效 0 次。"""
        granted = 0
        for i in range(50):
            now = i * 20
            if i % CFG["GOP_LEN"] == 0:
                self.g.note_idr_sent(now)
            if self.g.request(now):
                granted += 1
        self.assertEqual(granted, 0,
                         "有自然 IDR 还放行了 %d 次强制请求" % granted)
        self.assertEqual(self.g.rx, 50)
        self.assertEqual(self.g.merged_idr + self.g.merged_rate, 50)

    def test_storm_without_natural_idr_is_rate_limited(self):
        """最坏情况：编码器完全不出 IDR，50 次请求/s 也要被压到 <=1/interval。"""
        granted_at = []
        for i in range(50):
            now = i * 20
            if self.g.request(now):
                granted_at.append(now)
        self.assertLessEqual(
            len(granted_at), 1000 // CFG["IDR_REQ_MIN_INTERVAL_MS"] + 1)
        for a, b in zip(granted_at, granted_at[1:]):
            self.assertGreaterEqual(b - a, CFG["IDR_REQ_MIN_INTERVAL_MS"])

    def test_recovery_is_not_deadlocked(self):
        """settle 窗口过后，第一个请求必须立刻生效（不能死锁）。"""
        self.assertTrue(self.g.request(5000), "编码器停摆时请求被卡死")
        self.assertFalse(self.g.request(5020))
        self.assertTrue(self.g.request(5000 + CFG["IDR_REQ_MIN_INTERVAL_MS"]))

    def test_first_request_always_granted(self):
        """启动时必须能立刻出一个 IDR。"""
        self.assertTrue(self.g.request(0))

    def test_rolling_reset_keeps_coalescing_state(self):
        """统计清零不能把合并状态一起清掉，否则限频每 10s 重置一次。"""
        self.g.note_idr_sent(0)
        self.assertFalse(self.g.request(100))
        self.g.reset_rolling()
        self.assertEqual(self.g.rx, 0)
        self.assertFalse(self.g.request(200), "reset_rolling 丢了 settle 状态")
        self.assertTrue(self.g.request(600))

    def test_rolling_reset_keeps_rate_limit_state(self):
        """清统计不能让限频闸门每 10s 重置一次（_last_grant 必须保留）。

        编码器停摆（不出自然 IDR）时 settle 闸门不命中，唯一挡住风暴的就是
        interval 闸门；它的状态一旦被 reset_rolling 清掉，风暴就会每 10s
        重新放行一次。
        """
        self.assertTrue(self.g.request(10000), "首次请求应生效")
        self.g.reset_rolling()
        self.assertFalse(
            self.g.request(10100),
            "reset_rolling 丢了 _last_grant：限频闸门每 10s 失效一次")
        self.assertTrue(self.g.request(10000 + CFG["IDR_REQ_MIN_INTERVAL_MS"]))

    def test_forced_idr_bandwidth_budget(self):
        """限频后的强制 I 帧带宽必须留在链路余量内。

        旧实现 100ms 限频 = 10 次/s x 100KB = 1MB/s = 8.4Mbps > 5Mbps。
        新实现 800ms 限频 = 1.25 次/s x 100KB = 125KB/s = 1.0Mbps，
        加上内容 3.07Mbps 共约 4.1Mbps < 5Mbps。
        """
        forced_per_s = 1000.0 / CFG["IDR_REQ_MIN_INTERVAL_MS"]
        total = CFG["BIT_RATE"] + forced_per_s * 100.0 * 8
        self.assertLess(total, CFG["LINK_KBPS"],
                        "限频后仍需 %.0fKbps > 链路" % total)
        # 旧实现的对比数字：证明改动方向对
        old = CFG["BIT_RATE"] + 10.0 * 100.0 * 8
        self.assertGreater(old, CFG["LINK_KBPS"])

    def test_ms_diff_is_wrap_safe(self):
        self.assertIsNone(ms_diff(5, None))
        self.assertEqual(ms_diff(1000, 900), 100)
        self.assertEqual(ms_diff(5, (1 << 30) - 5), 10)


class TestStatsAreReadOnly(unittest.TestCase):
    """诊断统计不能改变任何发送行为。"""

    def test_stats_do_not_alter_tokens(self):
        p, _ = new_pacer()
        q, _ = new_pacer()
        for _ in range(20):
            p.take(1200)
            q.take(1200)
            _ = (p.packets, p.wait_us, p.min_gap_us, p.max_wait_us,
                 p.cap_events, p.wire_bytes, p.sleeps)
        self.assertAlmostEqual(p.tokens, q.tokens)
        self.assertEqual(p.min_gap_us, q.min_gap_us)
        self.assertEqual(p.packets, q.packets)

    def test_idr_share_and_rolling_reset(self):
        s = TxStats()
        s.note_frame(90000, 100, True, 190.0)
        s.note_frame(6000, 13, False, 12.0)
        s.bytes_tx = 96000 + 6 * 1240
        self.assertGreater(s.idr_share(), 80.0)
        self.assertAlmostEqual(s.last_iframe_pkts, 100)
        s.reset_rolling()
        self.assertEqual(s.sent_frames, 0)
        self.assertEqual(s.bytes_tx, 0)
        self.assertEqual(s.last_iframe_pkts, 100,
                         "reset_rolling 不该清掉单帧观测值")

    def test_stat_line_never_raises(self):
        """诊断输出在边界值（0 包、0 字节）下不能除零抛异常。"""
        p, _ = new_pacer()
        s = TxStats()
        g = IdrGate(CFG["IDR_REQ_MIN_INTERVAL_MS"], CFG["IDR_REQ_SETTLE_MS"])
        line = s.line(10.0, p, g, 12345)
        self.assertIn("stat:", line)
        self.assertIn("pacer:", s.detail(p, g))


if __name__ == "__main__":
    unittest.main()